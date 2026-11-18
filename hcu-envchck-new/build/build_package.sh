#!/usr/bin/env bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：把工具源码与完整私有 Python 打成普通用户 .run 安装包，可选 tar.gz 或 RPM。
# 职责边界：仅使用构建机已有运行时，不下载依赖、不修改目标容器、不执行集群测试。
set -euo pipefail

# 功能：说明构建参数及运行时约束。
usage() {
    cat <<'EOF'
用法：bash build/build_package.sh --runtime-src /opt/python-prefix [选项]
  --runtime-src PATH    完整、独立的 Python 安装前缀，不能使用 venv 或 /usr
  --wheel-dir PATH      离线 wheel 目录，含 ClusterShell 及 PyYAML，用于安装到私有运行时
  --format run|tar.gz|rpm 默认 run（自解压安装包）；rpm 另需 rpmbuild
  --output-dir PATH     默认项目 dist/
  --target-arch ARCH    默认构建机 uname -m；不支持跨架构构建
  --install-prefix DIR RPM 安装目录，默认 /opt/hcu-envcheck
  --release N          RPM Release，默认 1
EOF
}

# 功能：报告构建输入或工具依赖问题并终止，避免产出不完整发行包。
fail() { printf '[BUILD-ERROR] %s\n' "$*" >&2; exit 1; }

build_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
runtime_source='' wheel_directory='' package_format=run output_directory=$build_root/dist
target_arch=$(uname -m) install_prefix=/opt/hcu-envcheck package_release=1
while (($#)); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --runtime-src|--wheel-dir|--format|--output-dir|--target-arch|--install-prefix|--release)
            (($# >= 2)) && [[ -n $2 ]] || fail "$1 缺少值"
            case $1 in
                --runtime-src) runtime_source=$2 ;;
                --wheel-dir) wheel_directory=$2 ;;
                --format) package_format=$2 ;;
                --output-dir) output_directory=$2 ;;
                --target-arch) target_arch=$2 ;;
                --install-prefix) install_prefix=$2 ;;
                --release) package_release=$2 ;;
            esac
            shift 2 ;;
        *) fail "未知参数：$1" ;;
    esac
done
[[ $(uname -s) == Linux ]] || fail '请在兼容目标系统的 Linux 构建机运行'
[[ $package_format == run || $package_format == tar.gz || $package_format == rpm ]] || fail 'format 仅支持 run、tar.gz 或 rpm'
[[ $target_arch == "$(uname -m)" && $target_arch =~ ^[A-Za-z0-9_]+$ ]] || fail 'target-arch 必须与构建机一致'
[[ $install_prefix =~ ^/[A-Za-z0-9_./-]+$ && $install_prefix != / && $install_prefix != *..* ]] || fail 'install-prefix 必须为安全的绝对目录'
[[ $package_release =~ ^[1-9][0-9]*$ ]] || fail 'release 必须为正整数'
for tool in tar cp find readlink realpath sha256sum; do command -v "$tool" >/dev/null || fail "缺少 $tool"; done
[[ $package_format != rpm ]] || command -v rpmbuild >/dev/null || fail '缺少 rpmbuild'
[[ -n $runtime_source && -d $runtime_source ]] || fail '请提供 --runtime-src 完整 Python 安装前缀'
runtime_source=$(realpath -- "$runtime_source")
case $runtime_source in /|/usr|/usr/local|/bin|/opt|/lib|/lib64) fail '拒绝打包宽泛系统目录，请使用独立 Python 前缀' ;; esac
[[ ! -f $runtime_source/pyvenv.cfg ]] || fail 'venv 不能代替完整私有 Python'
runtime_python=$runtime_source/bin/python3
[[ -x $runtime_python ]] || fail 'runtime-src 缺少 bin/python3'
case $(readlink -f -- "$runtime_python") in "$runtime_source"/*) ;; *) fail 'Python 可执行文件指向 runtime-src 之外' ;; esac

package_version=$(tr -d '\r\n' < "$build_root/VERSION")
[[ $package_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail 'VERSION 必须使用 x.y.z'
package_name=hcu-envcheck
mkdir -p -- "$output_directory"
output_directory=$(realpath -- "$output_directory")
build_stage=$(mktemp -d "$output_directory/.staging-XXXXXXXX")
# 功能：只清理本次 mktemp 创建的构建目录，不处理用户源码或 Python 安装。
cleanup() { [[ -n ${build_stage:-} && $build_stage == "$output_directory"/.staging-* ]] && rm -rf -- "$build_stage"; }
trap cleanup EXIT
package_directory=$build_stage/$package_name-$package_version
mkdir -p -- "$package_directory"
for entry in bin hcu_envcheck config docs build VERSION README.md requirements.txt install.sh uninstall.sh; do
    [[ -e $build_root/$entry ]] || fail "源码缺少 $entry"
    tar -C "$build_root" --exclude=__pycache__ --exclude='*.py[co]' -cf - "$entry" | tar -C "$package_directory" -xf -
done
cp -a -- "$runtime_source" "$package_directory/runtime"

# 功能：拒绝依赖前缀外文件的运行时，并把内部绝对符号链接改为可迁移的相对链接。
normalize_runtime_links() {
    local link resolved staged_link staged_target
    while IFS= read -r -d '' link; do
        resolved=$(readlink -f -- "$link") || fail "运行时存在失效链接：$link"
        case $resolved in "$runtime_source"/*) ;; *) fail "运行时符号链接越出安装前缀：$link" ;; esac
        staged_link=$package_directory/runtime/${link#"$runtime_source/"}
        staged_target=$package_directory/runtime/${resolved#"$runtime_source/"}
        ln -sfn -- "$(realpath --relative-to="$(dirname -- "$staged_link")" -- "$staged_target")" "$staged_link"
    done < <(find "$runtime_source" -type l -print0)
}
normalize_runtime_links
# 功能：在移动后的运行时内执行 Python，不依赖构建机原安装路径。
staged_python() {
    env PYTHONHOME="$package_directory/runtime" \
        LD_LIBRARY_PATH="$package_directory/runtime/lib:$package_directory/runtime/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        "$package_directory/runtime/bin/python3" "$@"
}
if [[ -n $wheel_directory ]]; then
    [[ -d $wheel_directory ]] || fail "wheel 目录不存在：$wheel_directory"
    staged_python -m pip install --no-index --find-links "$wheel_directory" -r "$build_root/requirements.txt" \
        || fail 'ClusterShell 安装失败'
fi
staged_python -c 'import ClusterShell.CLI.Clush' \
    || fail '运行时缺少 ClusterShell；请提供 --wheel-dir 或使用已安装 ClusterShell 的 --runtime-src'
chmod 0755 "$package_directory/bin/hcu-cluster-run" "$package_directory/install.sh" "$package_directory/uninstall.sh"
# 此检查使用移动后的 Python，不能仅凭原安装目录可运行就认定发行包可运行。
env PYTHONHOME="$package_directory/runtime" PYTHONPATH="$package_directory" \
    LD_LIBRARY_PATH="$package_directory/runtime/lib:$package_directory/runtime/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$package_directory/runtime/bin/python3" -B -s - "$package_directory" "$target_arch" "$build_root" <<'PY'
"""核对私有运行时和标准库，保存构建系统信息；不启动集群检查。"""
import json
from pathlib import Path
import platform
import ssl
import subprocess
import sys
import tarfile

if sys.version_info < (3, 10):
    raise SystemExit('运行时需要 Python 3.10+')
if platform.machine() != sys.argv[2]:
    raise SystemExit('运行时架构与目标架构不一致')
Path(sys.argv[1], 'BUILD-MANIFEST.json').write_text(json.dumps({
    'application_version': Path(sys.argv[1], 'VERSION').read_text(encoding='utf-8').strip(),
    'python': sys.version, 'architecture': platform.machine(),
    'build_platform': platform.platform(), 'libc': platform.libc_ver(),
    'delivery': 'source-with-private-runtime',
}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
from hcu_envcheck.dependencies import export_sources
export_sources(Path(sys.argv[3]),Path(sys.argv[1]))

PY
"$package_directory/bin/hcu-cluster-run" --version >/dev/null
"$package_directory/bin/hcu-cluster-run" --help >/dev/null
# 功能：生成包内文件清单，用于安装复制校验和相同版本的内容身份判定。
env PYTHONHOME="$package_directory/runtime" PYTHONPATH="$package_directory" \
    LD_LIBRARY_PATH="$package_directory/runtime/lib:$package_directory/runtime/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$package_directory/runtime/bin/python3" -B -s - "$package_directory" <<'PY'
import hashlib
from pathlib import Path
import sys
root = Path(sys.argv[1])
lines = []
for file in sorted(root.rglob('*')):
    if not file.is_file() or file.is_symlink() or file.name == 'INSTALL-MANIFEST.sha256':
        continue
    name = file.relative_to(root).as_posix()
    if any(char in name for char in ('\\', '\n', '\r')):
        raise SystemExit('发行文件名不能包含反斜杠或换行：' + repr(name))
    digest = hashlib.file_digest(file.open('rb'), 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(file.read_bytes()).hexdigest()
    lines.append(digest + '  ' + name + '\n')
(root / 'INSTALL-MANIFEST.sha256').write_text(''.join(lines), encoding='utf-8')
PY
archive=$output_directory/$package_name-$package_version-linux-$target_arch-runtime.tar.gz
[[ ! -e $archive ]] || fail "产物已存在，换输出目录以保留备份：$archive"
tar -C "$build_stage" -czf "$archive" "$package_name-$package_version"
(cd -- "$output_directory" && sha256sum -- "$(basename -- "$archive")" > "$(basename -- "$archive").sha256")
printf '[BUILD] %s\n' "$archive"

if [[ $package_format == run ]]; then
    installer=$output_directory/$package_name-$package_version-linux-$target_arch.run
    [[ ! -e $installer ]] || fail "产物已存在：$installer"
    payload_sha=$(sha256sum "$archive"); payload_sha=${payload_sha%% *}
    sed -e "s|@VERSION@|$package_version|g" -e "s|@ARCH@|$target_arch|g" \
        -e "s|@PAYLOAD_SHA@|$payload_sha|g" "$build_root/build/installer_header.sh.in" > "$installer"
    cat -- "$archive" >> "$installer"
    chmod 0755 "$installer"
    (cd -- "$output_directory" && sha256sum -- "$(basename -- "$installer")" > "$(basename -- "$installer").sha256")
    # help 不解包、不安装；构建过程不改变构建机 PATH 或用户启动文件。
    "$installer" --help >/dev/null
    printf '[BUILD] %s\n' "$installer"
fi

if [[ $package_format == rpm ]]; then
    rpm_root=$build_stage/rpmbuild
    mkdir -p "$rpm_root"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS}
    cp -- "$archive" "$rpm_root/SOURCES/$package_name-$package_version.tar.gz"
    sed -e "s|@VERSION@|$package_version|g" -e "s|@RELEASE@|$package_release|g" \
        -e "s|@ARCH@|$target_arch|g" -e "s|@INSTALL_PREFIX@|$install_prefix|g" \
        "$build_root/build/hcu-envcheck.spec.in" > "$rpm_root/SPECS/hcu-envcheck.spec"
    rpmbuild -bb --define "_topdir $rpm_root" --target "$target_arch" "$rpm_root/SPECS/hcu-envcheck.spec"
    while IFS= read -r -d '' rpm_file; do
        [[ ! -e $output_directory/$(basename -- "$rpm_file") ]] || fail '同名 RPM 产物已存在'
        cp -- "$rpm_file" "$output_directory/"
        printf '[BUILD] %s/%s\n' "$output_directory" "$(basename -- "$rpm_file")"
    done < <(find "$rpm_root/RPMS" -type f -name '*.rpm' -print0)
fi
