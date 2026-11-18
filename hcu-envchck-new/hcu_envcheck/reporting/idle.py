# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：解析宿主机空闲采样证据，并为各调用方提供同一套空闲判断标准。
# 职责边界：所有卡的显存占用和利用率均为空闲才判为 IDLE；缺失或无效证据判为 UNKNOWN。
"""Pure idle evidence parsing and operation-aware decisions. No execution or I/O."""
from __future__ import annotations

import base64
import binascii
import math
import re

OPERATIONS = ('idle-check', 'rccl', 'deepep', 'byte-flux')


# 功能：解析带版本的 Shell 采样封装，忽略 SSH 提示等非协议文本。
def parse_capture(text: str, *, node: str, scope: str) -> dict:
    """Decode the versioned Bash envelope; SSH banners are outside the envelope."""
    evidence = dict(schema_version=1, node=node, scope=scope, sampled_at=None,
                    samples={}, cards=[], processes=None, errors=[])
    started = ended = False
    meta = False
    try:
        for line in text.splitlines():
            if line.startswith('HCU_IDLE_BEGIN\t'):
                if started or line != 'HCU_IDLE_BEGIN\t1':
                    raise ValueError('duplicate or unsupported sample envelope')
                started = True
            elif line.startswith('HCU_IDLE_END\t'):
                if not started or ended or line != 'HCU_IDLE_END\t1':
                    raise ValueError('invalid sample end')
                ended = True
            elif line.startswith('HCU_IDLE_'):
                if not started or ended:
                    raise ValueError('sample record outside envelope')
                cells = line.split('\t')
                if cells[0] == 'HCU_IDLE_META' and len(cells) == 4 and not meta:
                    if cells[2] != scope or not cells[3].isdigit() or not cells[1]:
                        raise ValueError('invalid sample identity')
                    evidence.update(observed_hostname=cells[1], sampled_at=int(cells[3]))
                    meta = True
                elif cells[0] == 'HCU_IDLE_RAW' and len(cells) == 4:
                    name, code, encoded = cells[1:]
                    if name not in {'metrics', 'showpids', 'showpid'} or name in evidence['samples']:
                        raise ValueError('duplicate or unknown raw sample')
                    if not code.isdigit() or not 0 <= int(code) <= 255:
                        raise ValueError('invalid sample exit code')
                    raw = base64.b64decode(encoded, validate=True).decode('utf-8', errors='replace')
                    evidence['samples'][name] = dict(exit_code=int(code), stdout=raw)
                else:
                    raise ValueError('invalid sample record')
        if not (started and ended and meta):
            raise ValueError('incomplete sample envelope')
    except (ValueError, binascii.Error) as exc:
        evidence['errors'].append(str(exc))
    return evidence


# 功能：解析每张可见卡的指标，拒绝格式错误或无效数值。
def parse_metrics(raw: str) -> list[dict]:
    """Extract every visible card; a malformed numeric row invalidates the sample."""
    cards, seen = [], set()
    if not re.search(r'VRAM%.*HCU%', raw):
        raise ValueError('missing VRAM%/HCU% header')
    for line in raw.splitlines():
        cells = line.split()
        if not cells or not cells[0].isdigit():
            continue
        card = int(cells[0])
        if card in seen or len(cells) < 7:
            raise ValueError('duplicate card or truncated metric row')
        seen.add(card)
        values = []
        for cell in cells[5:7]:
            if not re.fullmatch(r'\d+(?:\.\d+)?%', cell):
                raise ValueError(f'card {card}: invalid percentage {cell}')
            value = float(cell[:-1])
            if not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f'card {card}: percentage out of range')
            values.append(value)
        cards.append(dict(card=card, vram_pct=values[0], util_pct=values[1]))
    if not cards:
        raise ValueError('no card metric rows')
    return cards


# 功能：解析 KFD 进程表，区分有效空表和无法确认的采样。
def parse_pids(raw: str) -> list[int]:
    known = table = False
    pids = set()
    for line in raw.splitlines():
        lower = line.lower()
        if re.search(r'unrecognized|unknown argument|invalid argument|unsupported|not supported|permission denied|traceback|failed|error', lower):
            raise ValueError('unrecognized KFD process response')
        if re.search(r'(^|\s)no\s.*(kfd|process)', lower) or re.match(r'^[\s#=]*kfd\s+process', lower):
            known = True
        if re.match(r'^[\s#=]*kfd\s+pid', lower) or re.match(r'^[\s#]*pid\s+(process|name|hcu|gpu|vram)', lower):
            known = table = True
        match = re.match(r'^\s*PID:\s*(\d+)(?:\s|$)', line)
        if re.match(r'^\s*PID:', line) and not match:
            raise ValueError('invalid PID row')
        match = match or re.match(r'^\s*PID\s+(\d+)(?:\s|$)', line)
        if match:
            known = True
            pids.add(int(match[1]))
        elif re.match(r'^\s*\d+(?:\s|$)', line):
            if not table:
                raise ValueError('numeric PID row without process table header')
            pids.add(int(line.split()[0]))
    if not known or any(pid <= 0 for pid in pids):
        raise ValueError('missing recognizable KFD process evidence')
    return sorted(pids)


# 功能：合并卡指标、命令退出码及采样错误，形成结构化证据。
def parse_sample(text: str, *, node: str, scope: str) -> dict:
    evidence = parse_capture(text, node=node, scope=scope)
    sample = evidence['samples'].get('metrics')
    if not sample or sample['exit_code'] != 0:
        evidence['errors'].append('metric collection failed or missing')
    elif not evidence['errors']:
        try:
            evidence['cards'] = parse_metrics(sample['stdout'])
        except ValueError as exc:
            evidence['errors'].append(str(exc))
    return evidence


# 功能：解析并校验 KFD 进程采样结果。
def parse_process_sample(text: str, *, node: str, scope: str) -> dict:
    evidence = parse_capture(text, node=node, scope=scope)
    if not evidence['errors']:
        for key in ('showpids', 'showpid'):
            sample = evidence['samples'].get(key)
            if sample and sample['exit_code'] == 0:
                try:
                    evidence['processes'] = parse_pids(sample['stdout'])
                    return evidence
                except ValueError:
                    pass
        evidence['errors'].append('cannot parse KFD process evidence')
    return evidence


# 功能：按统一标准将节点判为 IDLE、BUSY 或 UNKNOWN，不执行后续动作。
def evaluate_idle(evidence: dict, *, operation: str) -> dict:
    """All current operations share zero-percent policy; dispatch is explicit."""
    if operation not in OPERATIONS:
        raise ValueError(f'unsupported idle operation: {operation}')
    decision = dict(schema_version=1, operation=operation, node=evidence['node'],
                    scope=evidence['scope'], sampled_at=evidence['sampled_at'],
                    policy='host-all-zero-v1', vram_max_pct=0, util_max_pct=0,
                    cards=evidence['cards'], busy_cards=[], status='UNKNOWN',
                    reason_code='IDLE_EVIDENCE_INVALID', detail='')
    if evidence['scope'] != 'host':
        decision['detail'] = 'host-scope evidence required'
        return decision
    if evidence['errors'] or not evidence['cards']:
        decision['detail'] = '; '.join(evidence['errors']) or 'no card evidence'
        return decision
    busy = [card for card in evidence['cards'] if card['vram_pct'] > 0 or card['util_pct'] > 0]
    decision.update(busy_cards=busy, status='BUSY' if busy else 'IDLE',
                    reason_code='HCU_BUSY' if busy else 'HCU_IDLE',
                    detail=f'{len(busy)} cards have nonzero readings' if busy else 'all cards VRAM=0% HCU=0%')
    return decision
