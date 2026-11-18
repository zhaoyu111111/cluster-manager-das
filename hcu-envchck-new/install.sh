#!/usr/bin/env bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：把离线包安装到普通用户前缀，提供可直接调用的 hcu-cluster-run。
# 职责边界：不调用 sudo、不安装系统/测试依赖；升级保留配置和旧 release。
set -euo pipefail
package_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
source "$package_root/build/user_install_common.sh"

# 功能：显示用户安装选项，不执行安装。
usage() {
    cat <<'EOF'
用法：bash install.sh [--prefix DIR] [--no-path]
  --prefix DIR  用户安装前缀，默认 $HOME/.local
  --no-path     不更新本用户 Bash 启动文件
升级使用相同命令，保留已有配置；不需要 root 或系统 Python。
EOF
}
prefix=$HOME/.local path_setup=1
while (($#)); do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --prefix) (($#>=2)) || hcu_install_fail '--prefix 缺少值'; prefix=$2; shift 2 ;;
        --no-path) path_setup=0; shift ;;
        *) hcu_install_fail "未知参数：$1" ;;
    esac
done
[[ $(uname -s) == Linux ]] || hcu_install_fail '此安装包用于 Linux'
for tool in realpath readlink sha256sum cp mv ln flock awk mktemp; do command -v "$tool" >/dev/null || hcu_install_fail "缺少 $tool"; done
[[ -x $package_root/runtime/bin/python3 && -f $package_root/INSTALL-MANIFEST.sha256 ]] || hcu_install_fail '请从构建后的离线包安装；源码工程需先执行 build/build_package.sh'
(cd -- "$package_root" && sha256sum --check --status INSTALL-MANIFEST.sha256) || hcu_install_fail '安装包文件校验失败'
version=$(tr -d '\r\n' < "$package_root/VERSION")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || hcu_install_fail '版本格式错误'
digest=$(sha256sum "$package_root/INSTALL-MANIFEST.sha256"); digest=${digest%% *}
hcu_install_paths "$prefix"
hcu_install_lock
if [[ -e $hcu_base ]]; then
    hcu_validate_installation
else
    mkdir -p -- "$hcu_base"
    printf 'hcu-envcheck-user-install-v1\n%s\n%s\n' "$hcu_prefix" "$EUID" > "$hcu_base/INSTALLATION"
fi
mkdir -p -- "$hcu_bindir" "$hcu_base/releases" "$hcu_base/config"
[[ ! -L $hcu_base/releases && -O $hcu_base/releases && ! -L $hcu_base/config && -O $hcu_base/config ]] || hcu_install_fail '程序或配置目录归属无效'
if [[ -e $hcu_command || -L $hcu_command ]]; then
    [[ -L $hcu_command ]] || hcu_install_fail "命令已存在且不是受管链接：$hcu_command"
    case $(readlink -- "$hcu_command") in "$hcu_base"/releases/*/bin/hcu-cluster-run) ;; *) hcu_install_fail '拒绝覆盖其他安装来源的命令' ;; esac
fi
release=$hcu_base/releases/$version-${digest:0:16}
stage=''
temporary_link=''
# 功能：只清理本次创建的未发布目录和命令临时链接。
cleanup() {
    [[ -z $stage || $stage != "$hcu_base/releases/".incoming-* ]] || rm -rf -- "$stage"
    [[ -z $temporary_link ]] || rm -f -- "$temporary_link"
}
trap cleanup EXIT
if [[ -e $release || -L $release ]]; then
    [[ -d $release && ! -L $release && -O $release && -f $release/USER-RELEASE ]] || hcu_install_fail '同名 release 不是有效安装'
    [[ $(cat "$release/USER-RELEASE") == "$digest" ]] || hcu_install_fail '同名 release 的指纹不匹配'
    hcu_verify_release "$release" || hcu_install_fail '已安装程序被修改，请使用独立 prefix，避免覆盖'
else
    stage=$(mktemp -d "$hcu_base/releases/.incoming-XXXXXXXX")
    cp -a -- "$package_root/." "$stage/"
    # 用户配置独立保存，升级只补充缺少的默认文件。
    cp -an -- "$package_root/config/." "$hcu_base/config/"
    mv -- "$stage/config" "$stage/config.default"
    ln -s ../../config "$stage/config"
    printf '%s\n' "$digest" > "$stage/USER-RELEASE"
    hcu_verify_release "$stage" || hcu_install_fail '复制后的程序校验失败'
    env -u PYTHONHOME -u PYTHONPATH -u LD_LIBRARY_PATH "$stage/bin/hcu-cluster-run" --version >/dev/null
    mv -T -- "$stage" "$release"
    stage=''
fi
cp -- "$package_root/uninstall.sh" "$hcu_base/uninstall.sh"
cp -- "$package_root/build/user_install_common.sh" "$hcu_base/install-common.sh"
chmod 0755 "$hcu_base/uninstall.sh"
[[ ! -e $hcu_base/current || -L $hcu_base/current ]] || hcu_install_fail 'current 已被普通文件占用'
temporary_link=$(mktemp "$hcu_base/.current-XXXXXXXX"); rm -f -- "$temporary_link"
ln -s "releases/$(basename -- "$release")" "$temporary_link"
mv -fT -- "$temporary_link" "$hcu_base/current"; temporary_link=''
temporary_link=$(mktemp "$hcu_bindir/.hcu-link-XXXXXXXX"); rm -f -- "$temporary_link"
# 链接固定到真实 release，运行中的任务不会随 current 升级混用代码/runtime。
ln -s "$release/bin/hcu-cluster-run" "$temporary_link"
mv -fT -- "$temporary_link" "$hcu_command"; temporary_link=''
if ((path_setup)); then hcu_update_paths add || true; fi
printf '[INSTALLED] %s\n[CONFIG] %s\n' "$hcu_command" "$hcu_base/config"
printf '当前终端生效：export PATH=%q:"$PATH"\n' "$hcu_bindir"
printf '使用：hcu-cluster-run --help\n卸载：bash %q\n' "$hcu_base/uninstall.sh"
