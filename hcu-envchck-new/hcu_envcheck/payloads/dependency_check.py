"""核对实际导入扩展与声明的构建来源；只读，不安装或切换运行库。"""
from __future__ import annotations
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import sys

MODULES = {"DeepEP": ("deep_ep", "deep_ep.deep_ep_cpp"), "flux": ("flux", "flux_ths_pybind")}


def fingerprint(path: Path) -> str:
    """流式读取实际加载文件指纹，避免把大型 GPU 扩展整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def mapped_libraries() -> dict:
    """保存当前进程实际映射的通信/HIP 库及哈希；不修改 loader 路径。"""
    paths = set()
    for line in Path("/proc/self/maps").read_text().splitlines():
        cells = line.split(None, 5)
        if len(cells) != 6 or not cells[5].startswith("/"):
            continue
        path = cells[5]
        if any(name in Path(path).name.lower() for name in
               ("deep_ep_cpp", "libflux", "flux_ths", "rocshmem", "libamdhip", "librccl", "libnccl", "libibverbs", "libshca")):
            paths.add(path)
    return {name: dict(path=str(Path(name).resolve()), sha256=fingerprint(Path(name))) for name in sorted(paths)}


def inspect_dependency(library: str, source: dict, manifest_path: str) -> dict:
    """版本号仅作展示；用源码提交、实际模块及全部声明文件哈希判定一致性。"""
    result = dict(library=library, source_commit=source["commit"], status="UNKNOWN",
                  reason_code="DEPENDENCY_UNKNOWN", detail="需要目标扩展构建来源清单", modules={},
                  module_sha256={}, python=sys.executable, prefix=sys.prefix, search_path=list(sys.path))
    try:
        for name in MODULES[library]:
            module = importlib.import_module(name)
            path = Path(module.__file__).resolve()
            result["modules"][name] = str(path)
            result["module_sha256"][name] = fingerprint(path)
    except Exception as error:
        return dict(result, status="MISSING", reason_code="DEPENDENCY_MISSING", detail=f"{type(error).__name__}: {error}")
    try:
        result["mapped_libraries"] = mapped_libraries()
    except OSError as error:
        return dict(result, detail=f"无法确认实际映射的动态库：{error}")
    if not manifest_path:
        return result
    try:
        document = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        record = document["libraries"][library]
        if document.get("schema_version") != 1 or record["source_commit"] != source["commit"]:
            return dict(result, status="MISMATCH", reason_code="DEPENDENCY_SOURCE_MISMATCH", detail="构建提交与本次third_party测试源码不一致")
        expected_modules = record["modules"]
        if set(expected_modules) != set(MODULES[library]):
            raise ValueError("构建清单缺少实际模块指纹")
        for name in result["modules"]:
            if result["module_sha256"][name] != expected_modules[name]:
                return dict(result, status="MISMATCH", reason_code="DEPENDENCY_BINARY_MISMATCH", detail=f"实际导入模块与构建清单不同：{name}")
        base = Path(next(iter(result["modules"].values()))).parent.parent
        for name, digest in record["files"].items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("构建清单文件路径非法")
            if fingerprint(base / name) != digest:
                return dict(result, status="MISMATCH", reason_code="DEPENDENCY_BINARY_MISMATCH", detail=f"运行库文件与构建清单不同：{name}")
        if not record["files"] or not any(".so" in name for name in record["files"]):
            raise ValueError("构建清单缺少二进制文件")
        if library == "flux":
            actual = str((base / "flux/lib/libflux_cuda.so").resolve())
            mapped = {str(Path(name).resolve()) for name in result["mapped_libraries"] if Path(name).name.startswith("libflux_cuda.so")}
            if mapped != {actual}:
                return dict(result, status="MISMATCH", reason_code="DEPENDENCY_BINARY_MISMATCH", detail="实际映射的flux动态库与包目录不一致")
        return dict(result, status="MATCH", reason_code="DEPENDENCY_MATCH", detail="源码提交、实际模块与构建文件指纹一致", manifest=manifest_path)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return dict(result, detail=f"构建来源无法确认：{error}")


def main() -> int:
    """在source后的目标Python中运行，UNKNOWN与MISMATCH均阻断性能测试。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", default="")
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    result = inspect_dependency(source["library"], source, args.manifest or os.environ.get("HCU_DEPENDENCY_MANIFEST", ""))
    print("__HCU_DEPENDENCY__\t" + json.dumps(result, ensure_ascii=False), flush=True)
    return {"MATCH": 0, "MISMATCH": 65, "MISSING": 66, "UNKNOWN": 69}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
