<!--
Copyright (c) 2026 Hygon Information Technology Co., Ltd.
SPDX-License-Identifier: Apache-2.0
-->

# HCU 训练可靠性工具集端到端使用流程

本文描述套件级的执行顺序与故障闭环衔接：训练前环境验收 → 长稳训练与自动恢复 → hang 根因诊断 → 节点恢复与回归验收，覆盖 Slurm、裸机 MPI 与预建容器 Docker 三种训练环境。具体命令参数、恢复边界和故障排查以各子项目 docs 为准，本文只保留每个阶段的最短路径命令与判读要点。

> Kubernetes（PyTorchJob/Volcano Job）场景的运行期容错由 [hygon-ft-k8s](../hygon-ft-k8s/README.md) 提供，暂不在本文范围内，需要时直接参考其 README 与 docs。

## 端到端总览

```text
训练前环境验收            长稳训练与自动恢复                  hang 根因诊断
   hcu-envcheck      →   hcu-training-reliability      →   stack-analyzer
   （空闲确认/环境检查     （Slurm/裸机/Docker 启动训练，       （py-spy 采集堆栈，
     /通信算子基线）        故障后自动隔离并重启）               按并行拓扑定位异常 rank）
        ↑                                                         │
        │                                                         │ 隔离建议
        └───────────── 节点恢复与回归验收（故障闭环） ←──────────────┘
          控制面自动拉黑异常节点，修复后解封回归备用池
          回池前：hcu-envcheck idle-check + platform,resource 复检
```

四个阶段环环相扣：验收通过的镜像与节点进入训练；训练期故障由控制面自动隔离与重启；控制面只能"隔离＋换节点重启"，回答不了"为什么 hang"，根因定位交给 stack-analyzer；诊断与恢复的结论最终回到节点池与黑名单，形成闭环。

## 阶段零：通用准备

- Python ≥3.10、Bash、控制节点到目标节点免密 SSH。
- 克隆后先初始化子模块（hcu-envcheck 的 DeepEP/byte-flux 主动测试与 nvidia_resiliency_ext 构建依赖）：

```bash
git clone https://github.com/HYGON-AI/cluster-manager-das.git
cd cluster-manager-das
git submodule update --init --recursive
```

| 子项目 | 一句话定位 | 部署形态 |
| --- | --- | --- |
| hcu-envcheck | 训练前环境验收与通信/算子基线 | 登录节点源码运行 |
| hcu-training-reliability | Slurm/裸机/Docker 训练启动＋守护＋自动恢复 | 控制节点常驻进程 |
| stack-analyzer | hang 根因诊断（py-spy＋Megatron 拓扑） | 按需安装使用 |
| nvidia_resiliency_ext | PyTorch 韧性库 HCU 适配（可选，作业内快速恢复） | 构建 wheel 安装 |

## 第一步：训练前环境验收（hcu-envcheck）

工具在登录节点运行，读取 hostfile，不申请节点；节点由 Slurm 等外部系统分配。三种执行场景（以工具实际 token 为准）：`shared-conda`（共享 Conda）、`node-local-conda`（节点本地 Conda）、`per-node-container`（各节点已运行的独立容器）。

推荐顺序：先确认空闲，再做基础检查，最后按需执行主动测试。

```bash
cd /shared/cluster-manager-das/hcu-envcheck
cat > hostfile <<'EOF'
node001
node002
node003
EOF

# 1) 空闲确认（--release 为显式授权停止占用容器，仅在确认有权释放时使用）
bash bin/hcu-cluster-run per-node-container idle-check -f hostfile

# 2) 容器状态核对＋平台/资源检查（已有容器场景）
bash bin/hcu-cluster-run per-node-container container-status \
  -f hostfile --container hcu-test -i registry.example.com/hcu/train:validated
bash bin/hcu-cluster-run per-node-container platform,resource \
  -f hostfile --container hcu-test -i registry.example.com/hcu/train:validated

# 3) 按需：RCCL 集合通信基线对比（共享 Conda 场景去掉 --container/-i）
bash bin/hcu-cluster-run per-node-container rccl \
  -f hostfile --container hcu-test -i registry.example.com/hcu/train:validated \
  --env-script /shared/hcu-config/train-env.sh \
  --group-size 1 --gpus-per-node 8 \
  --baseline-file /shared/hcu-config/rccl_baseline.conf
```

判读要点：

- 分阶段结果位于 `hcu-envcheck/verify_results/<scenario>_<operation>_stages_<timestamp>_<pid>/`：先看 `summary.md` 与 `failures.tsv`，再按索引进 `.state/` 查原始日志；采集进程退出码为 0 不代表检查通过。
- 主动测试（rccl/deepep/byte-flux/ib-write-bw）占用算力与网络，只能在已确认空闲的节点执行；DeepEP/byte-flux 依赖 `third-party/` 子模块。
- 容器创建/重建/删除是显式维护操作（`container-create|recreate|delete`，实际执行需 `--yes`），检测命令不会隐式创建容器；当前公共入口不提供 Kubernetes/Pod 场景。

完整说明：

- [快速使用说明](../hcu-envcheck/README.md)
- [命令示例与结果目录](../hcu-envcheck/docs/USER_GUIDE.md)
- [总体设计与运行约定](../hcu-envcheck/docs/DESIGN.md)
- [能力与验证边界](../hcu-envcheck/docs/KNOWN_LIMITATIONS.md)

### 补充：训练前性能筛机（可选）

环境检查之外，hcu-training-reliability 自带两阶段性能筛机（横向分组＋纵向交叉取交集精确定位故障节点），按 TFLOPs 阈值剔除性能劣化节点，与环境检查互补：

```bash
cd ../hcu-training-reliability
hcu-cluster-inspect node_check -c ./candidate_nodes -n 4 -t 185
```

健康/故障节点清单输出在 `cluster_manager/node_check/node_checklog/` 下的 `host_check_pass` 与 `host_check_error`；`-t` 阈值建议显式指定（CLI 默认 185 与脚本默认 100 不一致）。详见[筛机说明](../hcu-training-reliability/cluster_manager/node_check/README.md)。

## 第二步：长稳训练与自动恢复（hcu-training-reliability）

在登录/控制节点运行，支持三种训练环境，从 examples 复制对应启动脚本并按现场修改；不要直接使用项目目录的 `start.sh`（其中是占位路径模板，不能直接运行）。

| 训练环境 | 启动示例 | 说明 |
| --- | --- | --- |
| 裸机 MPI | `examples/start_none.sh` | 控制面经 mpirun/torchrun 直接拉起训练 |
| Slurm | `examples/start_slurm.sh` | 需已有 Slurm 作业，控制面校验作业并监控队列 |
| 预建容器 Docker | `examples/start_docker.sh` | 每节点预建同名容器，控制面经容器内 sshd 扇出 |

```bash
cd hcu-training-reliability
python -m pip install -e .          # 提供 hcu-cluster-inspect 入口（也可源码直接运行）
hcu-cluster-inspect --help

cp examples/start_none.sh start.local.sh      # Slurm 用 start_slurm.sh，Docker 用 start_docker.sh
# 编辑 start.local.sh：LOG_DIR、训练脚本与 hostfile 路径、FEISHU_WEBHOOK_URL 等
bash -n start.local.sh && bash start.local.sh
tail -f "${LOG_DIR}/cluster_manager.log"
```

启动参数（`hcu-cluster-inspect`）：`--nodes_num --slots --exec --hostfile` 必填；SLURM 模式另需 `--job_id --job_name --sbatch_script`（裸机与 Docker 模式不需要）。关键环境变量：`MPI_RANKS_PER_NODE`（每节点一个 torchrun 时设为 1）、`MEGATRON_SCRIPT_PATH`（训练参数文件，sh/yaml/json）、`TRAIN_NO_UPDATE_THRESHOLD`（hang 判定阈值，默认 1800 秒）、`FEISHU_WEBHOOK_URL`（飞书通知，未设置则告警关闭）。

启动后控制面自动执行：监控训练日志、节点 NHC、硬件指标与（Slurm 模式）队列状态；识别 hang、进程退出、loss/grad 异常与 NHC 故障后，停止当前训练、隔离异常节点（写入黑名单 `${WORK_DIR}/blacklist.json`，按故障类型加权、指数衰减，48 小时无新故障自动解封）、从备用池选择节点重新拉起，并通过飞书推送故障快照与训练进度。

边界：恢复只保证"换个健康节点重新执行启动脚本"，训练能否从正确位置继续取决于训练框架自身的 checkpoint 逻辑；当前停止实现按 `python` 关键字终止目标节点进程（`pkill -9 -f python`），只可用于独占节点。详见[部署与启动](../hcu-training-reliability/docs/deployment_guide.md) · [故障恢复与日常操作](../hcu-training-reliability/docs/recovery_workflow.md) · [配置参考](../hcu-training-reliability/docs/config_guide.md)。

### 可选：nvidia_resiliency_ext（作业内快速恢复）

定位与控制面恢复互补：控制面恢复＝停止整个任务再重建（分钟级）；nvidia_resiliency_ext＝作业内检测 hang rank 并 in-process 重启（秒级，常配合异步/本地 checkpoint）。适合对重启代价敏感的大规模训练，在训练脚本层接入，不改变上述部署方式。功能与上游 nvidia-resiliency-ext 保持一致（hang rank 检测与恢复、in-process 重启、异步/本地 checkpoint、掉队者检测）。

```bash
cd nvidia_resiliency_ext
python -m hcu_resiliency_ext.build_nvidia_resiliency_ext --outdir dist-nvidia
python -m pip install dist-nvidia/nvidia_resiliency_ext-*.whl
export NVRX_ENABLE_TELEMETRY=1    # 可选：启用 HCU telemetry profiling（需 nv-one-logger 依赖）
```

构建原理、安装与验证见[构建与使用](../nvidia_resiliency_ext/README.md) · [安装与验证](../nvidia_resiliency_ext/docs/INSTALLATION_CN.md)；训练脚本接入示例见 `hcu-training-reliability/examples/ft_megatron/`。

## 第三步：hang 根因诊断（stack-analyzer）

触发时机：控制面已自动隔离/重启，或收到 hang 告警后，需要回答"哪个 rank 在等谁、根因在哪台机器"。安装后先离线熟悉输出格式：

```bash
cd stack-analyzer
python -m pip install .            # 或 pip install -r requirements.txt 后用 python3 main.py
stack-analyzer demo --scenario pp --method trie
stack-analyzer demo --scenario eval --method auto
```

真实诊断（两步：采集 → 分析；以 Ansible 采集为例，Docker SSH 与本机方式见子项目文档）：

```bash
# 1) 批量采集各 rank 的 py-spy 堆栈，生成 stacks.json
stack-analyzer collect -H hostfile --ansible-user train -o diagnosis_out/stacks.json
# 每个容器内运行 sshd 的 Docker 场景改为：
stack-analyzer collect-docker -H docker-hostfile --ssh-user train \
  --identity-file /path/to/id_ed25519 -o diagnosis_out/stacks.json

# 2) 结合 hostfile 与 PP/TP/DP 拓扑定位异常 rank 与建议隔离机器
stack-analyzer analyze -i diagnosis_out/stacks.json -H hostfile \
  --pp-size 2 --tp-size 8 --dp-size 4 --method auto --json
```

也可一步完成采集与分析：

```bash
stack-analyzer diagnose -H hostfile --pp-size 2 --tp-size 8 --dp-size 4 \
  --output-dir diagnosis_out --method auto
```

要点与边界：

- hostfile 的节点顺序与 `slots=N` 必须与训练实际 rank 分配一致；`--pp-size/--tp-size/--dp-size`（必要时加 `--parallel-order` 或 `--use-tp-pp-dp-mapping`）必须与训练并行配置一致，否则异常 rank 会映射到错误机器。
- 输出 `outlier_ranks`（异常 rank）与 `machines_to_evict`（结合最小共享并行组给出的隔离建议）。建议不等于根因证明，也不会自动驱逐；处置前结合训练日志、RCCL 通信日志、NHC 与 hy-smi 复核，并建议间隔采集多次确认。
- py-spy 采集需要 SYS_PTRACE 权限；默认 nonblocking 模式，不暂停训练进程。

详见[采集与分析手册](../stack-analyzer/docs/COLLECTION_AND_ANALYSIS_CN.md) · [分析链路与误判边界](../stack-analyzer/docs/ANALYSIS_PIPELINE_CN.md)。

## 第四步：节点恢复与回归验收（闭环收尾）

诊断与修复后，把节点安全地送回资源池。被控制面隔离的节点已写入异常池与黑名单（`${WORK_DIR}/blacklist.json`，按故障类型加权、指数衰减，48 小时无新故障自动解封）；硬件修复并确认 `run_nhc` 通过后，再让其回归备用池，黑名单状态与人工处置原则见[故障恢复与日常操作](../hcu-training-reliability/docs/recovery_workflow.md)。

回池前复检：回到第一步，对修复节点执行 `idle-check` 与 `platform,resource` 复检，通过后再投入训练。不要跳过复检直接把节点加回 hostfile。

## 可选：容错能力演练

上线前建议先用注入手段验证闭环，而不是等真实故障：

- 细粒度故障注入：`tests/tools/hcu-fault-inject` 提供 30 个可自动恢复的故障档（cgroup CPU/内存、HCU 频率/功率/操作延迟、TCP 弱网、RDMA QP/拥塞），例如 `python3 -B ./hcu-fault-inject start clock-low 0 --for 15s`；仅限已确认可中断的测试节点。
- 套件级系统测试：`tests/st/` 内置"环境检查 → 训练 → 注入 worker 故障 → 备用节点 checkpoint 恢复"完整闭环（ENV-001/REL-010），平台方回归可参考[系统测试说明](../tests/st/README.md)。

## 故障场景快速索引

| 症状 / 需求 | 责任工具 | 最短入口 |
| --- | --- | --- |
| 新集群/新节点/新镜像上线前验收 | hcu-envcheck | `hcu-cluster-run <场景> platform,resource -f hostfile` |
| 剔除性能劣化节点 | node_check 筛机 | `hcu-cluster-inspect node_check -c <节点文件> -n 4 -t 185` |
| 训练 hang/节点故障自动恢复 | hcu-training-reliability | `bash start.local.sh`（复制自 examples） |
| 定位 hang 根因（谁在等谁） | stack-analyzer | `stack-analyzer diagnose -H hostfile --pp-size ...` |
| 修复后的节点回池 | 黑名单＋复检 | 解封/`run_nhc` 通过＋envcheck 复检 |
| 验证容错链路是否有效 | 演练工具 | `hcu-fault-inject` / `tests/st` |

## 全局安全边界

- hcu-training-reliability 停止训练时按 `python` 关键字终止目标节点进程，要求训练节点独占。
- 主动带宽、RCCL、完整筛机、故障模拟与恢复测试只能在已确认空闲的节点上执行。
- stack-analyzer 需要 ptrace 权限，只输出诊断建议、不自动驱逐。
- nvidia_resiliency_ext 只能在已验证的上游库版本上使用。
