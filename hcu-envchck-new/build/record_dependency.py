#!/usr/bin/env python3
"""显式构建后记录来源：比较wheel与当前导入文件，不安装依赖或修改Python路径。"""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import importlib
import json
import subprocess
import zipfile


def record(library: str, source: Path, wheel: Path, output: Path) -> None:
    """把固定Git提交、wheel和实际运行库绑定，供检测阶段只读核验。"""
    commit = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    top = subprocess.check_output(["git", "-C", str(source), "rev-parse", "--show-toplevel"], text=True).strip()
    if Path(top).resolve() != source.resolve():
        raise ValueError("--source 必须是实际构建的独立源码仓根目录")
    if subprocess.check_output(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"], text=True).strip():
        raise ValueError("源码含未提交修改，不能用 HEAD 声称构建来源")
    names = ("deep_ep", "deep_ep.deep_ep_cpp") if library == "DeepEP" else ("flux", "flux_ths_pybind")
    modules = {name: Path(importlib.import_module(name).__file__).resolve() for name in names}
    base = modules[names[0]].parent.parent
    files = {}
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.startswith(("deep_ep/", "flux/", "flux_ths_pybind")) and name.endswith((".py", ".so")):
                if ".." in PurePosixPath(name).parts:
                    raise ValueError("wheel 包含非法文件路径")
                data = archive.read(name)
                path = base / name
                if not path.is_file() or path.read_bytes() != data:
                    raise ValueError(f"实际安装与wheel不同：{name}")
                files[name] = hashlib.sha256(data).hexdigest()
    if not files or not any(name.endswith(".so") for name in files):
        raise ValueError("wheel未提供目标扩展")
    document = json.loads(output.read_text()) if output.exists() else {"schema_version": 1, "libraries": {}}
    document["libraries"][library] = dict(source_commit=commit, source_repository=str(source),
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(), files=files,
        modules={name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in modules.items()})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(f"[BUILD-RECORDED] {library} {commit} -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", choices=["DeepEP", "flux"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record(args.library, args.source, args.wheel, args.output)
