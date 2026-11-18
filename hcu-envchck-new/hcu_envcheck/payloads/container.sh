#!/bin/bash
# Copyright (c) 2026 Hygon Information Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
# 宿主机容器操作：按既有 stages.tsv 顺序执行，每阶段独立超时和落盘。
# entry.sh container 调用完整流程；本脚本的阶段子进程只执行本机 Docker 命令。
set -uo pipefail
umask 077
PAYLOAD=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit 70
source "${PAYLOAD}/../config.env"
source "${PAYLOAD}/common.sh"
STAGE=${1:-}
CID='' IMG='' RUNNING=''
STARTED=$(date +%s)

metadata() {
    printf '%s\n' "$HCU_RUN_ID" "$HCU_TARGET" "$STAGE" "${HCU_RUN_ID}_${HCU_TARGET}_${STAGE}" \
        "$CID" "$IMG" "$STARTED" "$(date +%s)" > "${RESULTS}/${STAGE}.meta"
}
done_stage() {
    hcu_status "${STAGE}.status" "$1" "$2" "$3" "$4"
    metadata
    exit "$3"
}
fail() { done_stage FAIL "$1" "$2" "$3"; }
pass() { done_stage "${1:-PASS}" "${2:-OK}" 0 "$3"; }

# 外层只在本节点遍历计划阶段，失败后给本节点剩余阶段写 BLOCKED。
if [[ -z $STAGE ]]; then
    first_code=0 first_reason=OK first_detail='全部阶段完成'
    while IFS=$'\t' read -r order STAGE; do
        [[ $order =~ ^[0-9]+$ ]] || continue
        case $STAGE in inspect|preflight|replace|create|delete|ssh-identity) ;; *) exit 64 ;; esac
        CID='' IMG='' STARTED=$(date +%s)
        : > "${RESULTS}/${STAGE}.log"
        : > "${RESULTS}/${STAGE}.stderr.log"
        if (( first_code )); then
            hcu_status "${STAGE}.status" BLOCKED PREVIOUS_STAGE_FAILED 1 '本节点前置阶段失败，未执行'
            metadata
            continue
        fi
        code=0
        timeout --signal=TERM --kill-after=5s "$HCU_TEST_TIMEOUT" bash "${BASH_SOURCE[0]}" "$STAGE" \
            > "${RESULTS}/${STAGE}.log" 2> "${RESULTS}/${STAGE}.stderr.log" || code=$?
        if [[ ! -s ${RESULTS}/${STAGE}.status ]] || { (( code )) && grep -Eq '^(PASS|WARN)' "${RESULTS}/${STAGE}.status"; }; then
            (( code )) || code=1
            reason=CONTAINER_STAGE_FAILED
            (( code != 124 )) || reason=TIMEOUT
            hcu_status "${STAGE}.status" FAIL "$reason" "$code" '阶段中断或未取得结束记录，详见阶段日志'
            metadata
        fi
        IFS=$'\t' read -r status reason stage_code detail < "${RESULTS}/${STAGE}.status"
        if [[ $status != PASS && $status != WARN ]]; then
            first_code=${stage_code:-1} first_reason=$reason first_detail=$detail
            (( first_code )) || first_code=1
        fi
    done < "${HCU_RUN_DIR}/plan/stages.tsv"
    hcu_status node.status "$( (( first_code )) && echo FAIL || echo PASS )" "$first_reason" "$first_code" "$first_detail"
    exit "$first_code"
fi

[[ $(cat "${HCU_RUN_DIR}/RUN_ID") == "$HCU_RUN_ID" ]] || fail RUN_ID_MISMATCH 69 '任务身份不匹配'
if [[ $HCU_OPERATION == container-delete || $HCU_OPERATION == container-recreate ]]; then
    (( HCU_YES )) || fail CONFIRMATION_REQUIRED 2 '删除或重建需要 --yes'
fi
id_pattern='^[0-9a-f]{64}$'
image_pattern='^sha256:[0-9a-f]{64}$'
ERR=${RESULTS}/${STAGE}.command.stderr
command_output() {
    OUT=$("$@" 2>"$ERR")
    CODE=$?
    ERROR=$(cat "$ERR")
    [[ -z $OUT ]] || printf '%s\n' "$OUT"
    [[ -z $ERROR ]] || printf '%s\n' "$ERROR" >&2
}
require() {
    local reason=$1; shift
    command_output "$@"
    if (( CODE )); then
        if [[ ${ERROR,,} == *'permission denied'* && ${ERROR,,} == *docker* ]]; then reason=DOCKER_PERMISSION_DENIED; fi
        fail "$reason" "$CODE" "${ERROR:-$reason}"
    fi
}
inspect_container() {
    CID='' RUNNING=''
    command_output docker inspect --type container -f '{{.Id}}|{{.State.Running}}|{{.Image}}' "$1"
    if (( CODE )); then
        if (( CODE == 1 )) && [[ $ERROR == *'No such container:'* || $ERROR == *'No such object:'* ]]; then return; fi
        fail CONTAINER_INSPECT_FAILED "$CODE" "$ERROR"
    fi
    IFS='|' read -r CID RUNNING IMG <<< "$OUT"
    [[ $CID =~ $id_pattern && $IMG =~ $image_pattern && ( $RUNNING == true || $RUNNING == false ) ]] \
        || fail CONTAINER_ID_INVALID 1 'Docker 未返回完整容器身份'
}
inspect_image() {
    command_output docker image inspect -f '{{.Id}}' "$1"
    if (( CODE )); then
        if [[ ${2:-} == allow_missing ]] && (( CODE == 1 )) && [[ $ERROR == *'No such image:'* ]]; then FOUND_IMAGE=''; return; fi
        fail IMAGE_INSPECT_FAILED "$CODE" "$ERROR"
    fi
    [[ $OUT =~ $image_pattern ]] || fail IMAGE_ID_INVALID 1 'Docker 未返回完整镜像 ID'
    FOUND_IMAGE=$OUT
}
check_port() {
    require PORT_INSPECT_FAILED ss -H -ltn "sport = :${HCU_PORT}"
    [[ -z $OUT ]] || fail PORT_BUSY 1 "宿主端口 ${HCU_PORT} 已占用"
}
receipt=${RESULTS}/container.receipt
write_receipt() {
    printf '%s\n' "$HCU_RUN_ID" "$HCU_TARGET" "$PRE_IMAGE" "$OLD_ID" "$NEW_ID" "$SSH_SOURCE" "$SSH_USER" "$SSH_TOKEN" > "${receipt}.tmp" \
        && mv -- "${receipt}.tmp" "$receipt" || fail RECEIPT_WRITE_FAILED 1 '容器身份回执写入失败'
}
read_receipt() {
    local -a rows=()
    [[ -f $receipt ]] || fail PREFLIGHT_RECEIPT_INVALID 1 '预检身份回执不存在'
    mapfile -t rows < "$receipt"
    (( ${#rows[@]} == 8 )) || fail PREFLIGHT_RECEIPT_INVALID 1 '预检身份回执字段数错误'
    [[ ${rows[0]} == "$HCU_RUN_ID" && ${rows[1]} == "$HCU_TARGET" && ${rows[2]} =~ $image_pattern \
       && ( -z ${rows[3]} || ${rows[3]} =~ $id_pattern ) && ( -z ${rows[4]} || ${rows[4]} =~ $id_pattern ) ]] \
        || fail PREFLIGHT_RECEIPT_INVALID 1 '预检回执运行、目标或完整 ID 不匹配'
    PRE_IMAGE=${rows[2]} OLD_ID=${rows[3]} NEW_ID=${rows[4]} SSH_SOURCE=${rows[5]} SSH_USER=${rows[6]} SSH_TOKEN=${rows[7]}
    IMG=$PRE_IMAGE
}

case $STAGE in
inspect)
    inspect_container "$HCU_CONTAINER"
    [[ -n $CID ]] || fail CONTAINER_ABSENT 1 '容器不存在'
    inspect_image "$HCU_IMAGE"
    [[ $IMG == "$FOUND_IMAGE" ]] || fail IMAGE_MISMATCH 1 '容器实际镜像与请求镜像不一致'
    if [[ $RUNNING == true ]]; then pass PASS OK '容器正在运行且镜像身份匹配'; fi
    pass WARN CONTAINER_STOPPED '容器及镜像存在，但容器已停止'
    ;;
preflight)
    require DOCKER_UNAVAILABLE docker info
    inspect_container "$HCU_CONTAINER"
    OLD_ID=$CID NEW_ID='' SSH_SOURCE='' SSH_USER='' SSH_TOKEN=''
    if [[ $HCU_OPERATION == container-delete ]]; then
        [[ -n $OLD_ID ]] || fail CONTAINER_ABSENT 1 '容器不存在，未执行删除'
        PRE_IMAGE=$IMG
        if [[ -n $HCU_IMAGE ]]; then
            inspect_image "$HCU_IMAGE"
            [[ $PRE_IMAGE == "$FOUND_IMAGE" ]] || fail IMAGE_MISMATCH 1 '删除前镜像身份不一致，未停止或删除容器'
        fi
    else
        [[ $HCU_OPERATION != container-create || -z $OLD_ID ]] || fail CONTAINER_EXISTS 1 '同名容器已存在，请使用 container-recreate --yes'
        SSH_SOURCE=${HCU_SSH_DIR:-${HOME%/}/.ssh}
        [[ $SSH_SOURCE == /* && -d $SSH_SOURCE && -r $SSH_SOURCE && -x $SSH_SOURCE ]] || fail SSH_SOURCE_MISSING 1 '宿主 SSH 来源目录不可读'
        if [[ -n $HCU_IMAGE_TAR ]]; then
            require IMAGE_LOAD_FAILED docker load -i "$HCU_IMAGE_TAR"
            inspect_image "$HCU_IMAGE"
        else
            inspect_image "$HCU_IMAGE" allow_missing
        fi
        if [[ -z $FOUND_IMAGE ]]; then require IMAGE_PULL_FAILED docker pull "$HCU_IMAGE"; inspect_image "$HCU_IMAGE"; fi
        PRE_IMAGE=$FOUND_IMAGE
        require IMAGE_SSH_TOOLS_MISSING docker run --rm --network=none --entrypoint=/bin/bash "$PRE_IMAGE" \
            -c 'command -v ssh >/dev/null && command -v ssh-keygen >/dev/null && test -x /usr/sbin/sshd'
        [[ $HCU_OPERATION != container-create ]] || check_port
    fi
    IMG=$PRE_IMAGE
    write_receipt
    pass PASS OK '预检身份已记录，旧容器尚未改动'
    ;;
delete|replace)
    read_receipt
    [[ $STAGE != delete || -n $OLD_ID ]] || fail PREFLIGHT_RECEIPT_INVALID 1 '删除缺少预检容器 ID'
    inspect_container "$HCU_CONTAINER"
    [[ -z $CID || $CID == "$OLD_ID" ]] || fail CONTAINER_ID_CHANGED 1 '预检后出现同名新容器，拒绝删除'
    if [[ -n $CID ]]; then
        command_output docker stop "$OLD_ID"
        stop_code=$CODE
        inspect_container "$HCU_CONTAINER"
        [[ -z $CID || $CID == "$OLD_ID" ]] || fail CONTAINER_ID_CHANGED 1 '停止期间出现同名新容器，未删除新容器'
        inspect_container "$OLD_ID"
        if [[ -n $CID ]]; then
            (( stop_code == 0 )) || fail CONTAINER_STOP_FAILED "$stop_code" '容器停止失败'
            command_output docker rm "$OLD_ID"
            remove_code=$CODE
            if (( remove_code )); then inspect_container "$OLD_ID"; [[ -z $CID ]] || fail CONTAINER_REMOVE_FAILED "$remove_code" '容器删除失败'; fi
        fi
    fi
    inspect_container "$HCU_CONTAINER"
    [[ -z $CID ]] || fail CONTAINER_ID_CHANGED 1 '删除后出现同名容器，未对其进行操作'
    if [[ -n $OLD_ID ]]; then inspect_container "$OLD_ID"; [[ -z $CID ]] || fail CONTAINER_NAME_CHANGED 1 '原容器被改名且仍存在'; fi
    CID=$OLD_ID IMG=$PRE_IMAGE
    [[ $STAGE != replace ]] || check_port
    pass PASS OK '原容器已不存在，未删除其他容器'
    ;;
create)
    read_receipt
    inspect_container "$HCU_CONTAINER"
    [[ -z $CID ]] || fail CONTAINER_EXISTS 1 '创建前同名容器已存在，未覆盖'
    check_port
    args=(run -dit --name "$HCU_CONTAINER" --restart unless-stopped --shm-size=64G
          --device=/dev/kfd --device=/dev/mkfd --device=/dev/dri --cap-add=SYS_PTRACE
          --security-opt seccomp=unconfined --ulimit memlock=-1:-1 --ipc=host --network=host
          --group-add video --privileged --label "org.hcu.envcheck.run=${HCU_RUN_ID}_${HCU_TARGET}_${STAGE}"
          -v /opt/hyhal:/opt/hyhal:ro -v "${HCU_REMOTE_WORKDIR}:${HCU_REMOTE_WORKDIR}")
    for volume in "${HCU_VOLUMES[@]}"; do args+=(-v "$volume"); done
    [[ -z $HCU_CONTAINER_USER ]] || args+=(--user "$HCU_CONTAINER_USER")
    require CONTAINER_CREATE_FAILED docker "${args[@]}" "${HCU_DOCKER_ARGS[@]}" "$PRE_IMAGE" /bin/bash
    created=$OUT
    inspect_container "$HCU_CONTAINER"
    [[ $CID == "$created" && $IMG == "$PRE_IMAGE" && $RUNNING == true ]] || fail CONTAINER_CREATE_STATE_INVALID 1 '创建后容器身份、镜像或状态不符'
    NEW_ID=$CID
    write_receipt
    pass PASS OK '容器已按预检镜像创建'
    ;;
ssh-identity)
    read_receipt
    [[ -n $NEW_ID && $SSH_SOURCE == /* ]] || fail PREFLIGHT_RECEIPT_INVALID 1 'SSH 初始化缺少创建身份或来源目录'
    inspect_container "$HCU_CONTAINER"
    [[ $CID == "$NEW_ID" ]] || fail CONTAINER_ID_CHANGED 1 'SSH 初始化前容器身份改变'
    [[ $RUNNING == true ]] || fail CONTAINER_STOPPED 1 'SSH 初始化前容器已停止'
    user_args=()
    [[ -z $HCU_CONTAINER_USER ]] || user_args=(--user "$HCU_CONTAINER_USER")
    require CONTAINER_USER_INVALID docker exec "${user_args[@]}" "$NEW_ID" bash -c \
        'set -e; entry=$(getent passwd "$(id -u)"); IFS=: read -r name pass uid gid info account_home shell <<< "$entry"; printf "%s\t%s\t%s\t%s\n" "$name" "$uid" "$gid" "$account_home"'
    IFS=$'\t' read -r SSH_USER uid gid account_home <<< "$OUT"
    [[ -n $SSH_USER && $uid =~ ^[0-9]+$ && $gid =~ ^[0-9]+$ && $account_home == /* ]] || fail CONTAINER_USER_INVALID 1 '容器执行用户或 HOME 无效'
    require SSH_DESTINATION_INVALID docker exec --user 0 "$NEW_ID" readlink -m "$account_home/.ssh"
    resolved=$OUT
    require CONTAINER_INSPECT_FAILED docker inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' "$NEW_ID"
    while IFS= read -r destination; do
        [[ -n $destination ]] || continue
        [[ $destination != / && $resolved != "$destination" && $resolved != "${destination%/}/"* ]] || fail SSH_DESTINATION_MOUNTED 1 '容器 SSH 目录位于挂载卷，拒绝修改'
    done <<< "$OUT"
    require SSH_COPY_FAILED docker exec --user 0 "$NEW_ID" mkdir -p "$account_home/.ssh"
    require SSH_COPY_FAILED docker cp "${SSH_SOURCE%/}/." "$NEW_ID:$account_home/.ssh/"
    SSH_TOKEN=${HCU_RUN_ID}_${HCU_TARGET}_${STAGE}
    require SSH_INIT_FAILED docker exec --user 0 -i "$NEW_ID" bash -s -- "$HCU_PORT" "$SSH_TOKEN" "$NEW_ID" "$account_home" "$uid" "$gid" <<'SSH_SETUP'
set -euo pipefail
port=$1 token=$2 identity=$3 account_home=$4 uid=$5 gid=$6
chown -R "$uid:$gid" "$account_home/.ssh"
chmod 700 "$account_home/.ssh"
find "$account_home/.ssh" -maxdepth 1 -type f -exec chmod 600 {} \;
mkdir -p /run/sshd /var/lib/hcu-envcheck
ssh-keygen -A
printf '%s %s\n' "$token" "$identity" > /run/hcu-container-identity
chmod 644 /run/hcu-container-identity
printf '%s\n' "Port $port" 'ListenAddress 0.0.0.0' \
  'HostKey /etc/ssh/ssh_host_rsa_key' 'PidFile /run/hcu-envcheck-sshd.pid' \
  'AuthorizedKeysFile %h/.ssh/authorized_keys' 'PermitRootLogin prohibit-password' \
  'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PubkeyAuthentication yes' \
  'UsePAM no' 'StrictModes yes' 'LogLevel VERBOSE' 'Subsystem sftp internal-sftp' \
  > /var/lib/hcu-envcheck/sshd_config
/usr/sbin/sshd -t -f /var/lib/hcu-envcheck/sshd_config
/usr/sbin/sshd -f /var/lib/hcu-envcheck/sshd_config -E /var/lib/hcu-envcheck/sshd.log
SSH_SETUP
    write_receipt
    for ((attempt=1; attempt<=10; attempt++)); do
        command_output docker exec --user "$SSH_USER" "$NEW_ID" ssh -n -p "$HCU_PORT" \
            -o BatchMode=yes -o ConnectTimeout=3 -o ConnectionAttempts=1 "$SSH_USER@127.0.0.1" 'cat /run/hcu-container-identity'
        if (( CODE == 0 )); then
            [[ $OUT == "$SSH_TOKEN $NEW_ID" ]] || fail SSH_IDENTITY_FAILED 69 'SSH 回环身份令牌不匹配'
            pass PASS OK 'SSH 已进入指定容器，身份令牌回环匹配'
        fi
        if (( CODE != 255 )) || [[ ${ERROR,,} != *'connection refused'* && ${ERROR,,} != *'connection timed out'* ]] || (( attempt == 10 )); then
            fail SSH_CONNECT_FAILED "$CODE" 'SSH 回环连接失败，详见阶段日志'
        fi
        sleep 2
    done
    ;;
*) fail INVALID_STAGE 2 "未知容器阶段：$STAGE" ;;
esac
