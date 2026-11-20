<!--
Copyright (c) 2026 Hygon Information Technology Co., Ltd.
SPDX-License-Identifier: Apache-2.0
-->

# hcu-training-reliability 配置与参数参考

当前配置由命令行参数、环境变量和 `MEGATRON_SCRIPT_PATH` 三部分组成。

## 1. 训练命令行参数

裸机和 Slurm 训练都必须在命令行显式选择一种节点来源。仅设置环境变量 `SUPERNODES_CONFIG`、同时传入两种来源或两种都未传，都会报错。使用 `--hostfile` 前，如已设置 `SUPERNODES_CONFIG`，请执行 `unset SUPERNODES_CONFIG`。

| 参数 | 裸机 | Slurm | 说明 |
|---|---:|---:|---|
| `--nodes_num` | 必填 | 必填 | 每轮训练需要的节点数，必须大于 0 |
| `--slots` | 必填 | 必填 | 每个节点的训练进程数，必须大于 0 |
| `--mpi-ranks-per-node` | 可选 | 可选 | 训练脚本中每节点的 MPI 进程数；也可用 `MPI_RANKS_PER_NODE` 设置，命令行优先。MPI 退出后备定位需要此配置 |
| `--exec` | 必填 | 必填 | 训练启动 Shell 脚本；必须存在且可读 |
| `--hostfile PATH` | 二选一 | 二选一 | 使用普通节点池。hostfile 每行写一个计算节点名，含作业的运行和备用节点 |
| `--supernodes [JSON]` | 二选一 | 二选一 | 使用 Pod 分配和替补策略。兼容别名 `--supernodes-config`。配置文件优先取显式路径，其次取环境变量 `SUPERNODES_CONFIG`，最后取包内 `cluster_manager/config/supernodes.json` |
| `--job_id` | 不需要 | 必填 | 已有 Slurm Job ID |
| `--job_name` | 可选 | 必填 | Slurm 作业名；Slurm 模式必须与 sbatch 脚本一致 |
| `--sbatch_script` | 不需要 | 必填 | Slurm 提交脚本 |

示例：

```bash
hcu-cluster-inspect \
  --nodes_num 4 \
  --slots 8 \
  --exec /path/to/run.sh \
  --hostfile /path/to/hostfile
```

### 超节点布置与恢复

在裸机超节点模式下，作业独占 JSON 描述的所有 Pod。管理器按规则展开全部计算节点，并写入 `$WORK_DIR/workspace/allocation.hostfile`。在 Slurm 超节点模式下，管理器把 Job ID 实际获配的节点写入同一文件，并校验这些节点在 JSON 中的归属。每轮训练由管理器另行生成有序的 `slots_nodes.txt`，只列本轮参训节点和每节点 slots。

包内示例用一条规则描述 p5、p6 两个物理 Pod，`p{pod}c{node}` 展开为每个 Pod 的 c0 至 c9。编号不连续时可写 `"pod_range": "5-6,9,12-14"`，其它命名方式可单独写规则。

`spec` 使用 `M*N`，表示每 Pod 有 M 个计算节点、每节点有 N 张卡。`active_nodes_per_pod` 给出首次每 Pod 参训台数 A，另外 M−A 台留作本地备用。若配置包含 `local_standby_nodes_per_pod`，其值应等于 M−A。首次布置需要选中 Pod 的 M 台均在可用池中，作业的 `--nodes_num` 要能由完整的训练组组成，`--slots` 要在各参与节点的卡数范围内。

```bash
hcu-cluster-inspect --supernodes --nodes_num 8 --slots 4 --exec /shared/job/train.sh
```

若该作业获配 p5、p6 的全部 20 台，首次使用 p5c0 至 p5c7，并对 p5 全部 10 台执行健康检查。p5c2 隔离后，p5c8 接替第 3 个位置。若 p5c4、p5c5 随后同时隔离，p5 只剩 p5c9 一台备用，原有 8 个训练位置整体迁到 p6c0 至 p6c7。迁移目标的每节点卡数与源 Pod 相同，可用节点数和配置的训练台数上限均须容纳原组。恢复后本地备用可降到零。各轮训练名单的每行均为 `主机名 slots=4`，未迁移的组保留原顺序。若没有可接下整组的兼容 Pod，该轮分配失败，保留上次发布的训练名单。

对于 4,000 张参训卡，同样的规则需要 125 个参训 Pod，参数为 `--nodes_num 1000 --slots 4`。首次用 8 备 2 需要获配 1,250 台、共 5,000 张物理卡。若还要在每个参训 Pod 都已布置作业时支持整组外迁，可以给作业多分配一个备用 Pod。

## 2. 核心环境变量

MPI rank 与训练 global rank 必须分别配置。对于 `mpirun` 每节点启动一个
`torchrun --nproc_per_node=4` 的两节点任务，管理器参数为：

```bash
hcu-cluster-inspect --nodes_num 2 --slots 4 --mpi-ranks-per-node 1 \
  --exec /path/to/run.sh --hostfile /path/to/hostfile
```

也可以在启动管理器前 `export MPI_RANKS_PER_NODE=1`。
`start.sh` 和 `examples/start_docker.sh` 默认将其设为 `SLOTS`，适用于 MPI 直接启动各训练进程，并通过
`--mpi-ranks-per-node "${MPI_RANKS_PER_NODE}"` 显式传给管理器；外部环境变量可覆盖默认值。
当使用 MPI 每节点启动一个 `torchrun` 时，必须显式设置 `MPI_RANKS_PER_NODE=1`，
与 `torchrun` 启动的 GPU/训练进程数无关。直接运行管理器入口时，此参数仍默认为未配置。

| 训练脚本的 MPI 启动方式 | `MPI_RANKS_PER_NODE` | `--slots` |
|---|---:|---:|
| 每节点一个 `torchrun --nproc_per_node=4` | `1` | `4` |
| 每节点一个 `torchrun --nproc_per_node=8` | `1` | `8` |
| MPI 直接每节点启动 4 个训练进程 | `4` | `4` |
| MPI 直接每节点启动 8 个训练进程 | `8` | `8` |

配置好训练脚本和镜像后，启动脚本也需要显式选择节点来源。下面的普通节点池示例使用 `--hostfile`。

```bash
# 每节点一个 torchrun：当前 DeepSeek 启动方式。
MPI_RANKS_PER_NODE=1 bash start.sh --hostfile /shared/job/hostfile
# Docker 模式，每节点一个 torchrun，申请 2 个节点，每节点 4 个训练进程。
MPI_RANKS_PER_NODE=1 NODES_NUM=2 SLOTS=4 bash examples/start_docker.sh --hostfile /shared/job/hostfile
# MPI 直接每节点启动 4 个训练进程，未设置 MPI_RANKS_PER_NODE 时默认跟随 SLOTS：
NODES_NUM=2 SLOTS=4 bash examples/start_docker.sh --hostfile /shared/job/hostfile
```

`MPI_RANKS_PER_NODE` 必须为正整数，只用于监控映射，不会改写训练脚本中的
`mpirun` 命令，也不决定训练节点数或 GPU 数。`NODES_NUM` 用于管理器申请节点，
`SLOTS` 描述每节点训练进程数；训练脚本仍须按管理器分配的 hostfile 启动相应节点。

`Root Cause` 仍优先使用明确的 `host`；MPI 退出后备路径使用
`MPI rank // mpi_ranks_per_node`，训练 global rank 使用 `rank // slots`。
映射要求每节点进程数一致、MPI ranks 按当前分配节点顺序连续排列；轮转、重排或非均匀布局
不能使用此算式，应依赖明确的 host 信息。未配置 MPI 布局时不会回退使用训练 slots 猜测节点，
而是记录无法定位并进入现有人工介入告警路径。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CLUSTER_LAUNCH_MODE` | `mpi` | 启动器类型；支持 `mpi`、`docker`（以及兼容别名 `docker_exec`） |
| `CLUSTER_SCHEDULE` | `NONE` | `NONE` 表示裸机，`SLURM` 表示 Slurm |
| `SUPERNODES_CONFIG` | 未设置 | 显式选择 `--supernodes` 时可用它指定 JSON 路径。使用 `--hostfile` 时必须取消设置 |
| `WORK_DIR` | 当前目录 | 控制器工作目录；状态写入其 `workspace/` |
| `LOG_DIR` | `${WORK_DIR}/hcu_megatron/examples/aibenchmark` | 训练日志目录 |
| `MEGATRON_SCRIPT_PATH` | 空 | 训练参数来源，可指向 Shell、JSON 或 YAML 文件 |
| `LOG_PARSER_TYPE` | `base` | `base` 为 Megatron；`special` 为特定训练框架日志 |
| `MPI_LAUNCH_TIMEOUT` | `300` | MPI 启停命令超时，单位秒 |
| `MPI_RANKS_PER_NODE` | 管理器未配置；`start.sh` / `examples/start_docker.sh` 为 `${SLOTS}` | 每节点 MPI 进程数，仅用于 MPI 退出后备定位；每节点一个 `torchrun` 时必须显式改为 `1`，命令行 `--mpi-ranks-per-node` 优先 |
| `RUN_NHC_EXEC_MODE` | `host` | `host` 在目标宿主机执行 `run_nhc`；`container` 通过 `docker exec` 在目标节点的训练容器内执行 |
| `INTERVAL_MONITOR` | `60` | 节点和恢复轮询基础间隔，单位秒 |
| `TRAIN_ALERT_THRESHOLD` | `20000` | 单步耗时告警阈值，单位毫秒 |
| `TRAIN_NO_UPDATE_THRESHOLD` | `1800` | 已有日志停止更新的阈值，单位秒 |
| `STARTUP_NO_LOG_TIMEOUT_SEC` | `1800` | 启动后首条有效日志等待时间，单位秒 |
| `FEISHU_WEBHOOK_URL` | 空 | 可选的飞书告警地址 |
| `BLACKLIST_PERSISTENCE_PATH` | `${WORK_DIR}/blacklist.json` | 黑名单持久化文件 |
| `BLACKLIST_PERSISTENCE_BACKUP_PATH` | `${WORK_DIR}/blacklist.json.bak` | 黑名单备份文件 |

Docker launcher 变量（仅 `CLUSTER_LAUNCH_MODE=docker` 使用）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CONTAINER_NAME` / `DOCKER_CONTAINER_NAME` | `cluster-manager` | 每个节点上的容器名；`DOCKER_CONTAINER_NAME` 优先 |
| `DOCKER_IMAGE` / `IMAGE` | 空（必填） | 容器镜像；节点本地不存在时自动 `docker pull` |
| `DOCKER_IMAGE_TAR` / `IMAGE_TAR` | 空 | 镜像 tar 包；存在时优先 `docker load` |
| `DOCKER_REUSE_CONTAINER` | `false` | 开启后复用同名容器：运行中直接使用，已停止则 `docker start`；不存在时按镜像配置创建。默认删除后重建 |
| `DOCKER_EXEC_PATH` | `--exec` 的路径 | 容器内训练脚本路径；宿主机与容器路径不同时必须显式设置 |
| `DOCKER_SLOTSFILE_PATH` | 本轮训练的 slots 文件路径 | 容器内可读的 slots 文件路径，通常要求挂载共享目录 |
| `DOCKER_WORKDIR` | 训练脚本父目录 | 容器内执行 `bash` 前切换到的工作目录 |
| `DOCKER_CONTAINER_SSH_PORT` | `36000` | 容器内 `sshd` 监听端口，供训练脚本的容器间 MPI 使用；这是容器端口，不是宿主机 SSH 端口 |
| `MPI_HOST_SSH_PORT` | `22` | 连接训练宿主机的 SSH 端口；优先于旧变量 `MPIRUN_PLM_RSH_ARGS` |
| `DOCKER_HOST_SHARE_ROOT` | 空 | 宿主机共享目录；与容器目录同时设置时自动挂载 |
| `DOCKER_CONTAINER_SHARE_ROOT` | 空 | 容器内共享目录 |
| `DOCKER_RUN_ARGS` | GPU/共享内存默认参数 | 追加或覆盖 `docker run` 参数 |
| `DOCKER_STOP_PATTERN` | 训练脚本文件名 | 容器内停止时传给 `pkill -f` 的匹配串 |
| `DOCKER_REMOVE_CONTAINER_ON_STOP` | `true` | 停止后是否执行 `docker rm -f`；`DOCKER_REUSE_CONTAINER` 开启时不生效 |
| `MPI_TCP_IF_INCLUDE` | 空 | 限定 PRTE/OMPI 的 TCP 选路（网卡名或网段）。不设置时 HNP 会连同 `docker0`、CNI 地址一起对外通告，远端 daemon 可能选到不可达地址并超时 |
| `MPI_RANK_ENV` | 空 | `VAR=value,VAR2=value2`，下发给**所有节点所有 rank** 的环境变量 |
| `MPI_FORWARD_ENV` | 空 | 变量名列表（逗号分隔），取**协调节点容器内的当前值**下发给所有 rank |

在 `CLUSTER_LAUNCH_MODE=docker` 下，容器准备由 `clush --hostfile` 并发执行到所有训练节点，训练脚本只在
hostfile 第一台节点的容器内通过 `docker exec -d` 启动。

容器准备流程会自动建立 root 用户的容器间 SSH 免密访问：

1. 各容器在本地生成独立的 `/root/.ssh/id_ed25519_cluster_manager`，复用容器时保留已有私钥。
2. 管理器通过宿主机 SSH 收集容器公钥和 SSH 服务端公钥，将当前节点集合的公钥写入各容器
   `authorized_keys` 的管理区块。私钥不离开容器，不依赖共享目录交换密钥。
3. 更新 `/root/.ssh/config` 的管理区块，配置 root、`DOCKER_CONTAINER_SSH_PORT`、专用私钥和严格主机密钥校验；
   服务端公钥存放在 `known_hosts_cluster_manager`。管理区块之外的已有授权及客户端配置保留。
4. 验证协调节点到所有节点（含自身），以及各节点到协调节点的免密连接。任何阶段失败都会阻止本轮训练启动。
   故障替换节点时，对新分配的整个节点集合重新交换公钥并验证，更新管理区块中的旧节点授权。

镜像必须提供 `python3`、`ssh`、`ssh-keygen` 和 `/usr/sbin/sshd`。管理器到宿主机仍需事先具备免密 SSH
及执行 Docker 的权限；训练脚本的容器 SSH 端口应与 `DOCKER_CONTAINER_SSH_PORT` 一致，例如 `11452`。
自动配置适用于容器内 root 用户，按 hostfile 中的节点名称/IP 建立连接。

新建容器不再挂载宿主机 `/root/.ssh`。复用旧容器时，若 `/root/.ssh` 或其父目录仍是挂载目录，
准备流程会报错，避免改写宿主机或共享密钥。应先结束旧训练，再移除该 SSH 挂载并重建容器；
设置 `DOCKER_REUSE_CONTAINER=1` 不会自动改变既有容器的挂载。

NHC 的分发始终由宿主机上的 `clush` 完成。设置 `RUN_NHC_EXEC_MODE=container` 后，各目标宿主机执行
`docker exec -u root ${DOCKER_CONTAINER_NAME} bash -lc run_nhc`；因此容器必须已经存在且可运行，并且容器内
能够通过 `PATH` 找到 `run_nhc`。保持默认值 `host` 时，各目标宿主机直接执行 `run_nhc`。

`MPI_RANK_ENV` / `MPI_FORWARD_ENV` 通过 Open MPI 的 `mca_base_env_list` 下发，而不是简单
`export`：mpirun 不会把任意环境变量转发给远端 rank，只 `export` 的话仅协调节点的 rank 生效。
多网卡集群上这一点很关键——例如 `NCCL_SOCKET_IFNAME` 只在一侧生效时，两个节点会各自
自动选网卡，bootstrap 连接直接被对端 reset。

`MPI_FORWARD_ENV` 针对的是 docker 模式特有的环境不对称：协调节点的 rank 由 `docker exec`
拉起，带着镜像的 `ENV`；对端节点的 rank 由 mpirun 经容器 sshd 拉起，**不带镜像 ENV**。
典型后果是 `LD_LIBRARY_PATH` 丢失，对端找不到 RCCL 网络插件而退化成 `NET/Socket`，
协调节点却在用 IB 插件，两边传输类型不一致，报 `socketFinalizeAccept: wrong type 3 != 4`：

```bash
export MPI_FORWARD_ENV="LD_LIBRARY_PATH"
export MPI_RANK_ENV="NCCL_SOCKET_IFNAME=ib1,GLOO_SOCKET_IFNAME=ib1"
```

注意优先级：rank 启动后如果训练脚本再 `source` 一份 env 并导出同名变量，会覆盖这里的值，
同一个变量不要两处都设。

Docker 模式会先通过 `clush --hostfile` 并发在每个训练节点执行镜像检查、
`docker load`/`docker pull`、旧容器清理和 `docker run -dit`；容器准备完成后，
通过宿主机 SSH 连接到实际 slots hostfile 的第一台训练节点，在该节点的容器内执行一次
`docker exec -d` 启动训练脚本。

设置 `DOCKER_REUSE_CONTAINER=1` 时，先检测同名容器；存在则跳过镜像准备和容器重建，保留原镜像、挂载和启动参数。`DOCKER_IMAGE` 仍需配置，供没有容器的节点创建使用。若停止训练后也需要保留容器，同时设置 `DOCKER_REMOVE_CONTAINER_ON_STOP=0`。复用运行中的容器不会判断其中是否已有训练任务。

`CLUSTER_SCHEDULE` 只接受 `NONE` 或 `SLURM`，`LOG_PARSER_TYPE` 只接受 `base` 或 `special`。配置拼写错误会导致启动失败。

## 3. 监控开关

下列布尔值接受 `true`、`1` 或 `yes`：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `ENABLE_HW_CHECK` | `true` | 是否执行硬件信息检查 |
| `ENABLE_NHC_FAULT_HANDLE` | `true` | 是否处理 NHC 故障 |
| `ENABLE_SLURM_CHECK` | `true` | 是否处理 Slurm 队列消失和重新提交；裸机模式没有 Slurm 管理器，因此不会查询 Slurm |
| `ENABLE_LOSS_GRAD_CHECK` | `true` | 是否检查 loss/grad 异常 |
| `ENABLE_REGULAR_NOTIFY` | `true` | 是否发送周期通知 |
| `ENABLE_ITER_DUMPER` | `true` | 是否落盘迭代信息 |
| `LOG_MONITOR_ENABLE_NO_UPDATE` | `true` | 是否启用日志无更新检测 |

## 4. 训练参数加载

配置文件路径只能通过环境变量提供：

```bash
export MEGATRON_SCRIPT_PATH=/path/to/train.sh
```

支持扩展名：

- `.sh`、`.bash`
- `.json`
- `.yaml`、`.yml`

主要读取的 Megatron 参数包括：

- TP、PP、CP、EP、ETP 和 sequence parallel。
- 模型层数、hidden size、attention heads、FFN、sequence length。
- micro/global batch size。
- save interval、train samples、eval interval。

Shell 文件使用 `--parameter value` 形式，例如：

```bash
TRAIN_ARGS=" \
  --tensor-model-parallel-size 8 \
  --pipeline-model-parallel-size 2 \
  --micro-batch-size 1 \
  --global-batch-size 1024
"
```

当前 Shell 解析器不保证识别 `--parameter=value`、复杂命令替换或未解析变量。加载异常会回退为空配置并记录日志，因此必须检查启动日志。

## 5. node_check 参数

节点性能筛机使用位置参数 `node_check`：

```bash
hcu-cluster-inspect node_check \
  --clushnode /path/to/nodes \
  --nodenum 4 \
  --tflops 185
```

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--clushnode` | 无 | 待检查节点列表，必填 |
| `--nodenum` | `4` | 每组节点数 |
| `--tflops` | `185` | 通过 CLI 调用时的 TFLOPs 阈值 |
| `--healthy` | 脚本默认路径 | 健康节点输出文件 |
| `--fault` | 脚本默认路径 | 异常节点输出文件 |
| `--only-horizontal` | 关闭 | 仅运行横向阶段 |

直接执行 `cluster_manager/node_check/run_check.sh` 时，脚本自身默认阈值是 100；建议始终显式传入 `--tflops`，避免两种入口默认值不同。

## 6. 当前实现边界

- `MAX_RESTART_TIMES` 虽然可配置，但当前训练启动失败路径尚未统一使用该值限制重试次数。
- MPI 停止实现按 `python` 关键字终止目标节点进程，只适用于独占节点。
- `WORK_DIR` 中的状态文件不是配置文件，不能手工当作启动参数输入。
- 修改训练规模或模型参数前，应备份并检查 `WORK_DIR/workspace/` 的历史状态，避免错误恢复旧任务。
