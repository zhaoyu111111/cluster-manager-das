#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 文件功能：宿主机空闲采样、占用归属查询和受控释放；原始证据输出到 stdout。
# 用法：idle.sh gate   采样一次，退出码 0=IDLE 2=BUSY 1=UNKNOWN
#       idle.sh run    采样并查询归属；HCU_RELEASE=1 时停止占卡容器后复检
# 职责边界：此处判断只决定是否进入释放；最终结论由控制端解析原始采样。不删除容器、不强杀宿主进程。

set -uo pipefail
PAYLOAD=$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )
source "${PAYLOAD}/../config.env"

MODE=${1:-}
LIMIT=60
RECHECKS=5

# ===================================== Sampling =====================================
SMI=''
for candidate in hy-smi Hy-smi /opt/hyhal/bin/hy-smi /opt/dtk/.hyhal/bin/hy-smi /opt/dtk/bin/hy-smi; do
    if command -v "${candidate}" >/dev/null 2>&1; then SMI=${candidate}; break; fi
done

capture() {
    local name=$1 code=0
    shift
    RAW=$(timeout 15 "${SMI}" "$@" 2>&1) || code=$?
    printf 'HCU_IDLE_RAW\t%s\t%s\t%s\n' "${name}" "${code}" "$(printf '%s' "${RAW}" | base64 | tr -d '\n')"
    return ${code}
}

envelope_begin() { printf 'HCU_IDLE_BEGIN\t1\nHCU_IDLE_META\t%s\thost\t%s\n' "$(hostname)" "$(date +%s)"; }
envelope_end() { printf 'HCU_IDLE_END\t1\n'; }

# 每卡 VRAM%（第6列）、HCU%（第7列）全为 0 才判 IDLE。
judge() {
    awk '
        /VRAM%.*HCU%/ {header = 1}
        $1 ~ /^[0-9]+$/ {
            cards++
            if (NF < 7 || $6 !~ /^[0-9.]+%$/ || $7 !~ /^[0-9.]+%$/) bad = 1
            if ($6 + 0 > 0 || $7 + 0 > 0) busy = 1
        }
        END {
            if (!header || !cards || bad) print "UNKNOWN"
            else if (busy) print "BUSY"
            else print "IDLE"
        }' <<< "${RAW}"
}

sample() {
    envelope_begin
    if [[ -z ${SMI} ]]; then
        printf 'HCU_IDLE_RAW\tmetrics\t127\t%s\n' "$(printf 'hy-smi unavailable' | base64 | tr -d '\n')"
        envelope_end
        STATE=UNKNOWN
        return
    fi
    if capture metrics; then STATE=$(judge); else STATE=UNKNOWN; fi
    envelope_end
}

sample_processes() {
    local both
    envelope_begin
    capture showpids --showpids
    both=${RAW}
    capture showpid --showpid
    RAW=${both}$'\n'${RAW}
    envelope_end
}

busy_pids() {
    awk '
        tolower($0) ~ /kfd +pid|^[ #]*pid +(process|name|hcu|gpu|vram)/ {table = 1; next}
        /^ *PID:? +[0-9]+/ {gsub(/[^0-9 ]/, ""); print $1; next}
        table && /^ *[0-9]+( |$)/ {print $1}' <<< "${RAW}" | sort -un
}

# ===================================== Owner =====================================
pid_start() {
    local value
    value=$(cat "/proc/$1/stat" 2>/dev/null) || return 1
    value=${value##*) }
    local -a fields
    read -r -a fields <<< "${value}"
    [[ ${fields[19]:-} =~ ^[0-9]+$ ]] || return 1
    echo "${fields[19]}"
}

declare -A OWNER=() NAME=() STARTED=()
load_containers() {
    local cid info name started running member
    command -v docker >/dev/null || return 1
    for cid in $(timeout "${LIMIT}" docker ps -q --no-trunc); do
        info=$(timeout "${LIMIT}" docker inspect -f '{{.Name}}|{{.State.StartedAt}}|{{.State.Running}}' "${cid}") || return 1
        IFS='|' read -r name started running <<< "${info}"
        [[ ${running} == true ]] || continue
        NAME[${cid}]=${name#/}
        STARTED[${cid}]=${started}
        while read -r member _; do
            [[ ${member} =~ ^[0-9]+$ ]] && OWNER[${member}]=${cid}
        done < <(timeout "${LIMIT}" docker top "${cid}" -eo pid)
    done
}

# 释放前再核对容器启动时间和 PID 启动标识，防止 PID 复用后误停其他容器。
stop_container() {
    local cid=$1 pid running
    [[ $(timeout "${LIMIT}" docker inspect -f '{{.State.StartedAt}}|{{.State.Running}}' "${cid}") == "${STARTED[${cid}]}|true" ]] || return 1
    for pid in ${CONTAINER_PIDS[${cid}]}; do
        [[ $(pid_start "${pid}") == "${PID_START[${pid}]}" ]] || return 1
    done
    timeout "$((LIMIT + 30))" docker stop --time 30 "${cid}" >/dev/null || return 1
    running=$(timeout "${LIMIT}" docker inspect -f '{{.State.Running}}' "${cid}" 2>/dev/null) || return $?
    [[ ${running} == false ]]
}

# ===================================== Execution =====================================
case "${MODE}" in
    gate)
        sample
        case ${STATE} in IDLE) exit 0 ;; BUSY) exit 2 ;; *) exit 1 ;; esac
        ;;
    run)
        sample
        [[ ${STATE} == BUSY ]] || { [[ ${STATE} == IDLE ]] && exit 0 || exit 1; }
        sample_processes
        PIDS=$(busy_pids)
        [[ -n ${PIDS} ]] || { printf 'IDLE_ERROR\t指标非零但未找到可识别宿主 PID，未释放\n'; exit 1; }
        load_containers || { printf 'IDLE_ERROR\tDocker 不可用或查询失败，未释放\n'; exit 1; }
        declare -A CONTAINER_PIDS=() PID_START=()
        complete=1
        for pid in ${PIDS}; do
            start=$(pid_start "${pid}") || { printf 'EXITED\t%s\n' "${pid}"; complete=0; continue; }
            cid=${OWNER[${pid}]:-}
            [[ -n ${cid} ]] || { printf 'UNKNOWN\t%s\n' "${pid}"; complete=0; continue; }
            PID_START[${pid}]=${start}
            CONTAINER_PIDS[${cid}]+=" ${pid}"
            printf 'OWNER\t%s\t%s\t%s\t%s\t%s\n' "${pid}" "${cid}" "${NAME[${cid}]}" "${STARTED[${cid}]}" "${start}"
        done
        (( HCU_RELEASE )) || exit 2
        (( complete )) || { printf 'IDLE_ERROR\t占用归属不完整，未停止任何容器\n'; exit 1; }
        for cid in "${!CONTAINER_PIDS[@]}"; do
            if stop_container "${cid}"; then
                printf 'RELEASE\t%s\t%s\tCONFIRMED\n' "${cid}" "${NAME[${cid}]}"
            else
                printf 'RELEASE\t%s\t%s\tUNCONFIRMED\n' "${cid}" "${NAME[${cid}]}"
                printf 'IDLE_ERROR\t容器停止未确认：%s\n' "${NAME[${cid}]}"
            fi
        done
        for (( attempt = 1; attempt <= RECHECKS; attempt++ )); do
            sleep 2
            sample
            [[ ${STATE} == IDLE ]] && exit 0
        done
        printf 'IDLE_ERROR\t释放后仍繁忙或指标未知\n'
        exit 1
        ;;
    *)
        echo "ERROR: unknown mode '${MODE}'. Use gate or run."
        exit 64
        ;;
esac
