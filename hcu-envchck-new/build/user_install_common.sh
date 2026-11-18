#!/usr/bin/env bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：普通用户安装/卸载共用的路径、所有权、锁和 Bash PATH 块管理。
# 职责边界：只管理指定 prefix 下的工具文件及本用户的已登记 PATH 块，不提权。

# 功能：报告安装输入或文件冲突，避免覆盖其他程序。
hcu_install_fail() { printf '[INSTALL-ERROR] %s\n' "$*" >&2; exit 1; }

# 功能：规范化安装前缀，并拒绝目录链接导致的安装/删除范围漂移。
hcu_install_paths() {
    [[ $1 == /* && $1 != *$'\n'* && $1 != *$'\r'* && $1 != *$'\t'* ]] || hcu_install_fail 'prefix 必须是无控制字符的绝对路径'
    hcu_prefix=$(realpath -m -- "$1")
    case $hcu_prefix in /|/usr|/bin|/sbin|/lib|/lib64|/etc|/var|/home|/opt) hcu_install_fail '请指定独立用户前缀，例如 ~/.local' ;; esac
    hcu_base=$hcu_prefix/share/hcu-envcheck
    hcu_bindir=$hcu_prefix/bin
    hcu_command=$hcu_bindir/hcu-cluster-run
    [[ $(realpath -m -- "$hcu_base") == "$hcu_base" && $(realpath -m -- "$hcu_bindir") == "$hcu_bindir" ]] || hcu_install_fail 'prefix 下的 bin/share/hcu-envcheck 不能通过链接指向其他位置'
    hcu_path_id=$(printf '%s' "$hcu_prefix" | sha256sum); hcu_path_id=${hcu_path_id:0:16}
    hcu_path_begin="# >>> hcu-envcheck PATH $hcu_path_id >>>"
    hcu_path_end="# <<< hcu-envcheck PATH $hcu_path_id <<<"
}

# 功能：以 prefix 外层稳定锁串行化安装和卸载；卸载不会删除活动锁文件。
hcu_install_lock() {
    mkdir -p -- "$hcu_prefix/share"
    local lock=$hcu_prefix/share/.hcu-envcheck.install.lock
    [[ ! -L $lock && (! -e $lock || -f $lock && -O $lock) ]] || hcu_install_fail '安装锁不属于当前用户'
    exec 9>"$lock"
    flock -x -w 60 9 || hcu_install_fail '其他安装/卸载仍在进行'
}

# 功能：验证安装标记和目录归属，不把普通目录当作本工具安装目录。
hcu_validate_installation() {
    local marker=$hcu_base/INSTALLATION
    [[ -d $hcu_base && ! -L $hcu_base && -O $hcu_base && -f $marker && ! -L $marker && -O $marker ]] || hcu_install_fail '安装目录或标记无效，拒绝覆盖/删除'
    local -a record=()
    mapfile -t record < "$marker"
    [[ ${#record[@]} == 3 && ${record[0]} == hcu-envcheck-user-install-v1 && ${record[1]} == "$hcu_prefix" && ${record[2]} == "$EUID" ]] || hcu_install_fail '安装标记的前缀或用户不匹配'
}

# 功能：只移除本 prefix 的 PATH 块，保留用户其余 Shell 配置及文件模式。
hcu_path_file() {
    local file=$1 action=$2 temporary
    if [[ -L $file ]]; then file=$(readlink -f -- "$file") || return 1; fi
    if [[ -e $file ]]; then
        [[ -f $file && -O $file && -w $file ]] || return 1
    elif [[ $action == remove ]]; then return 0
    fi
    temporary=$(mktemp "${file}.hcu-XXXXXXXX") || return 1
    if [[ -f $file ]]; then
        if ! awk -v begin="$hcu_path_begin" -v end="$hcu_path_end" '
            $0==begin {if (inside) exit 2; inside=1; next}
            $0==end {if (!inside) exit 2; inside=0; next}
            !inside {print}
            END {if (inside) exit 2}' "$file" > "$temporary"; then rm -f -- "$temporary"; return 1; fi
        chmod --reference="$file" "$temporary" || { rm -f -- "$temporary"; return 1; }
    fi
    if [[ $action == add ]]; then
        {
            printf '\n%s\n' "$hcu_path_begin"
            printf '_hcu_user_bin=%q\n' "$hcu_bindir"
            printf '%s\n' 'case "${PATH:-}:" in "${_hcu_user_bin}:"*) ;; *) export PATH="${_hcu_user_bin}${PATH:+:$PATH}" ;; esac' 'unset _hcu_user_bin'
            printf '%s\n' "$hcu_path_end"
        } >> "$temporary"
    fi
    mv -fT -- "$temporary" "$file"
}

# 功能：登记并更新交互/登录 Bash 的 PATH 块；无法修改时保留手动生效提示。
hcu_update_paths() {
    local action=$1 file login failed=0
    local -a files=()
    [[ ! -f $hcu_base/PATH-FILES ]] || mapfile -t files < "$hcu_base/PATH-FILES"
    if [[ $action == add ]]; then
        login=$HOME/.profile
        if [[ -e $HOME/.bash_profile || -L $HOME/.bash_profile ]]; then login=$HOME/.bash_profile
        elif [[ -e $HOME/.bash_login || -L $HOME/.bash_login ]]; then login=$HOME/.bash_login; fi
        files+=("$HOME/.bashrc" "$login")
    fi
    local -A seen=()
    local temporary
    temporary=$(mktemp "$hcu_base/.path-files-XXXXXXXX")
    for file in "${files[@]}"; do
        [[ -z ${seen[$file]+yes} ]] || continue
        seen[$file]=1
        if hcu_path_file "$file" "$action"; then
            [[ $action != add ]] || printf '%s\n' "$file" >> "$temporary"
        else
            [[ $action != remove ]] || printf '%s\n' "$file" >> "$temporary"
            failed=1; printf '[PATH-WARN] 未修改 %s\n' "$file" >&2; fi
    done
    if [[ $action == add || $failed == 1 ]]; then mv -fT -- "$temporary" "$hcu_base/PATH-FILES"
    else rm -f -- "$temporary" "$hcu_base/PATH-FILES"; fi
    return "$failed"
}

# 功能：排除用户可修改 config，验证已安装 release 的不可变程序文件。
hcu_verify_release() {
    (cd -- "$1" && awk '$2 !~ /^config\//' INSTALL-MANIFEST.sha256 | sha256sum --check --status -)
}
