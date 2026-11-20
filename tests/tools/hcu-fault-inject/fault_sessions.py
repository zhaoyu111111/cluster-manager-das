# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from fault_profiles import BACKENDS, PROFILES

VERSION = 2
SESSION_ID = re.compile(r"fi-[0-9a-f]{32}\Z")
HELD = {"pending", "applying", "active", "restore_failed"}
ERRORS = (OSError, ValueError, RuntimeError, KeyError, TypeError)
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def boot_id():
    return BOOT_ID.read_text(encoding="ascii").strip()


def public(session):
    return {key: value for key, value in session.items() if key not in {"step", "boot_id", "deadline"}}


class SessionStore:
    def __init__(self, directory):
        self.directory = directory.absolute()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            raise ValueError("state directory must be owned by the caller with mode 0700")

    @contextmanager
    def locked(self):
        fd = os.open(self.directory / ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def path(self, sid):
        if not SESSION_ID.fullmatch(sid):
            raise ValueError("invalid session ID")
        return self.directory / f"{sid}.json"

    def read(self, sid):
        with self.path(sid).open(encoding="utf-8") as file:
            data = json.load(file)
        if data.get("schema_version") != VERSION or data.get("session_id") != sid:
            raise ValueError("unsupported session record")
        return data

    def records(self):
        for path in sorted(self.directory.glob("fi-*.json")):
            try:
                yield self.read(path.stem)
            except ValueError:
                continue

    def save(self, session):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                         delete=False) as file:
            path = Path(file.name)
            try:
                json.dump(session, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
                os.replace(path, self.path(session["session_id"]))
            finally:
                path.unlink(missing_ok=True)

    def status(self, sid, live=True):
        with self.locked():
            if sid is None:
                return [public(item) for item in self.records()]
            session = self.read(sid)
            result = public(session)
            if live and session["status"] == "active":
                try:
                    result["current"] = BACKENDS[session["backend"]].observe(session["step"])
                except ERRORS as error:
                    result["observation_error"] = str(error)
            return result

    def restore(self, sid):
        with self.locked():
            session = self.read(sid)
            self.restore_locked(session, "manual")
            return public(session)

    def stop(self, profile, target):
        with self.locked():
            matches = [
                session for session in self.records()
                if session["status"] in HELD and session["profile"] == profile and session["target"] == target
            ]
            if not matches:
                return {"profile": profile, "target": target, "status": "inactive"}
            if len(matches) > 1:
                ids = ", ".join(session["session_id"] for session in matches)
                raise ValueError(f"multiple sessions match {profile} {target}; restore by session ID: {ids}")
            session = matches[0]
            self.restore_locked(session, "manual")
            return public(session)

    def restore_locked(self, session, reason, apply_failed=False):
        status = session["status"]
        if status not in HELD:
            return
        try:
            if status == "pending":
                session["restored"] = session["baseline"]
            elif session["boot_id"] != boot_id():
                raise ValueError("node boot identity changed; refusing to restore a stale session")
            else:
                session["restored"] = BACKENDS[session["backend"]].restore(session["step"])
            session["status"] = "apply_failed" if apply_failed else "restored"
            session.pop("restore_error", None)
        except ERRORS as error:
            session["status"] = "restore_failed"
            session["restore_error"] = str(error)
        session["restore_reason"] = reason
        self.save(session)


def launch(store, profile, alias, target, seconds):
    backend = PROFILES[profile]
    step = BACKENDS[backend].prepare(profile, target)
    step["seconds"] = seconds
    sid = f"fi-{uuid.uuid4().hex}"
    session = {
        "schema_version": VERSION,
        "session_id": sid,
        "profile": profile,
        "target": alias,
        "backend": backend,
        "resource": step["resource"],
        "status": "pending",
        "duration_seconds": seconds,
        "boot_id": boot_id(),
        "step": step,
        "baseline": BACKENDS[backend].observe(step),
    }
    entry = Path(__file__).resolve().with_name("hcu-fault-inject")
    with store.locked():
        for other in store.records():
            if other["status"] not in HELD:
                continue
            if other["resource"] == session["resource"]:
                raise ValueError(f"resource already held by {other['session_id']}")
            if other["profile"] == profile and other["target"] == alias:
                raise ValueError(f"{profile} {alias} already has a session to stop: {other['session_id']}")
        store.save(session)
        try:
            with (store.directory / f"{sid}.log").open("ab") as log:
                subprocess.Popen([sys.executable, "-B", str(entry), "_supervise", sid,
                                  "--state-dir", str(store.directory)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True)
        except OSError as error:
            session.update(status="apply_failed", error=str(error))
            store.save(session)
            return public(session)
    until = time.monotonic() + 15
    while True:
        result = store.status(sid, live=False)
        if result["status"] not in {"pending", "applying"} or time.monotonic() >= until:
            return result
        time.sleep(0.05)


def supervise(store, sid):
    with store.locked():
        session = store.read(sid)
        if session["status"] != "pending":
            return
        session["status"] = "applying"
        store.save(session)
        try:
            if session["boot_id"] != boot_id():
                raise ValueError("node boot identity changed")
            session["observed"] = BACKENDS[session["backend"]].apply(session["step"])
            session["deadline"] = time.monotonic() + session["duration_seconds"]
            session["expires_at"] = time.time() + session["duration_seconds"]
            session["status"] = "active"
            store.save(session)
        except ERRORS as error:
            session["error"] = str(error)
            store.restore_locked(session, "apply_failed", apply_failed=True)
            return
    while True:
        with store.locked():
            session = store.read(sid)
            if session["status"] != "active":
                return
            remaining = session["deadline"] - time.monotonic()
            if remaining <= 0:
                store.restore_locked(session, "expired")
                return
            if session["backend"] in {"hcu", "ip-network", "rdma"}:
                try:
                    elapsed = session["duration_seconds"] - remaining
                    if BACKENDS[session["backend"]].tick(session["step"], elapsed):
                        store.save(session)
                except ERRORS as error:
                    session["error"] = str(error)
                    store.restore_locked(session, "apply_failed", apply_failed=True)
                    return
        time.sleep(min(0.25, remaining))
