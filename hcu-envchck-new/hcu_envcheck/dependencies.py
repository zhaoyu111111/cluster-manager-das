"""读取third_party已提交源码或离线快照；不更新仓库、不安装目标依赖。"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
from .clush import subprocess_environment

LIBRARIES = {"DeepEP": ("tests", "LICENSE"), "flux": ("test/python", "README_ORIGIN.md", "README.md")}


def source_snapshot(root: Path, library: str, override: str = "") -> tuple[dict[str, bytes], dict]:
    """以子模块HEAD的Git对象为准，拒绝未提交修改；离线快照逐文件验哈希。"""
    directory = Path(override) if override else root / "third_party" / library
    manifest = directory / "SOURCE_MANIFEST.json"
    if manifest.is_file():
        meta = json.loads(manifest.read_text(encoding="utf-8"))
        if meta.get("library") != library or not re.fullmatch(r"[0-9a-f]{40}", meta.get("commit", "")):
            raise ValueError(f"{library} 源码清单身份无效")
        files = {}
        for name, digest in meta["files"].items():
            path = PurePosixPath(name)
            if (path.is_absolute() or not path.parts or ".." in path.parts
                    or not (directory / name).resolve().is_relative_to(directory.resolve())
                    or any((directory.joinpath(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1))):
                raise ValueError("源码快照路径不合法")
            content = (directory / name).read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"{library} 源码快照已变化：{name}")
            files[name] = content
    else:
        environment = subprocess_environment()
        def git(*args: str) -> bytes:
            """只读仓库对象，失败时提供可定位原因。"""
            result = subprocess.run(["git", "-C", str(directory), *args], env=environment, capture_output=True)
            if result.returncode:
                raise ValueError(f"{library} 子模块不可读取，请初始化 third_party：{result.stderr.decode('utf-8', 'replace').strip()}")
            return result.stdout
        top = Path(git("rev-parse", "--show-toplevel").decode().strip())
        if top.resolve() != directory.resolve():
            raise ValueError(f"{library} 尚未初始化为独立子模块")
        if git("status", "--porcelain", "--untracked-files=no").strip():
            raise ValueError(f"{library} 存在未提交修改，请先明确要使用的提交")
        commit = git("rev-parse", "HEAD").decode().strip()
        files = {}
        with tarfile.open(fileobj=io.BytesIO(git("archive", commit, *LIBRARIES[library]))) as archive:
            for member in archive.getmembers():
                if member.isfile() and (member.name.endswith(".py") or member.name in ("LICENSE", "README_ORIGIN.md", "README.md")):
                    files[member.name] = archive.extractfile(member).read()
        links = {}
        for line in git("ls-tree", "-r", commit).decode().splitlines():
            if line.startswith("160000 "):
                fields, name = line.split("\t", 1)
                links[name] = fields.split()[2]
        meta = dict(schema_version=1, library=library, commit=commit, submodules=links,
                    files={name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())})
    required = "tests/test_intranode.py" if library == "DeepEP" else "test/python/test_ag_kernel.py"
    if required not in files or (library == "DeepEP" and "LICENSE" not in files):
        raise ValueError(f"{library} 测试快照或许可证缺失")
    return files, dict(meta, source=str(directory))


def export_sources(root: Path, destination: Path) -> None:
    """发行包保留third_party布局和完整来源清单，不携带Git凭据或控制运行时到容器。"""
    for library in LIBRARIES:
        files, meta = source_snapshot(root, library)
        target = destination / "third_party" / library
        target.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        meta.pop("source", None)
        (target / "SOURCE_MANIFEST.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出固定third_party源码快照")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    options = parser.parse_args()
    export_sources(options.root, options.destination)
