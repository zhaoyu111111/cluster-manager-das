#!/usr/bin/env bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：卸载本用户指定 prefix 的工具，默认保留配置，--purge 才删除配置。
# 职责边界：不停止集群任务、不修改容器或依赖；只删除经标记核验的安装目录。
set -euo pipefail
script_root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ -f $script_root/install-common.sh ]]; then source "$script_root/install-common.sh"
else source "$script_root/build/user_install_common.sh"; fi
prefix=$HOME/.local purge=0
if [[ -r $script_root/INSTALLATION ]]; then
    mapfile -t installed < "$script_root/INSTALLATION"
    prefix=${installed[1]:-$prefix}
fi
while (($#)); do
    case $1 in
        -h|--help) printf '用法：bash uninstall.sh [--prefix DIR] [--purge]\n默认保留配置；--purge 删除本工具安装目录及配置。\n'; exit 0 ;;
        --prefix) (($#>=2)) || hcu_install_fail '--prefix 缺少值'; prefix=$2; shift 2 ;;
        --purge) purge=1; shift ;;
        *) hcu_install_fail "未知参数：$1" ;;
    esac
done
[[ $(uname -s) == Linux ]] || hcu_install_fail '此卸载器用于 Linux'
hcu_install_paths "$prefix"
hcu_install_lock
hcu_validate_installation
if [[ -L $hcu_command ]]; then
    case $(readlink -- "$hcu_command") in "$hcu_base"/releases/*/bin/hcu-cluster-run) rm -f -- "$hcu_command" ;;
        *) printf '[KEEP] 命令已被其他安装替换，保留 %s\n' "$hcu_command" ;; esac
fi
hcu_update_paths remove || hcu_install_fail '部分 PATH 块未清理，保留安装信息，请处理权限后重试'
[[ ! -L $hcu_base/releases ]] || hcu_install_fail 'releases 为外部链接，拒绝删除'
# 替换读取卸载脚本的 Shell，释放安装目录内的打开文件；继承 fd 9 保持安装锁。
exec bash -c '
    set -euo pipefail
    cd /
    hcu_base=$1
    purge=$2
    rm -rf -- "$hcu_base/releases"
    [[ ! -L $hcu_base/current ]] || rm -f -- "$hcu_base/current"
    if ((purge)); then
        # 路径、用户所有权和安装标记已在持锁状态核验。
        rm -rf -- "$hcu_base"
        printf "[UNINSTALLED] 程序和配置已移除\n"
    else
        printf "[UNINSTALLED] 程序已移除；配置保留在 %s\n" "$hcu_base/config"
    fi
' hcu-uninstall "$hcu_base" "$purge"
