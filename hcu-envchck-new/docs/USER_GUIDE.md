# hcu-envcheck 使用命令

所有命令在控制节点宿主机执行。按以下顺序使用工具，先替换节点、镜像和路径：

| 顺序 | 步骤 | 章节 |
| --- | --- | --- |
| 1 | 选择启动方式（构建后的安装包或代码工程），准备节点文件和目标环境 | 第 1 节 |
| 2 | 容器操作：创建、查询、重建、删除测试容器 | 第 2 节 |
| 3 | platform 平台资源检测：并发采集各节点硬件与软件包信息，按汇总报告确认各节点环境一致 | 第 3 节 |
| 4 | 空闲资源检测：确认各节点是否有空闲资源 | 第 4 节 |
| 5 | 一键释放：资源不空闲时释放全部节点的占卡容器 | 第 5 节 |
| 6 | RCCL 测试：单机、双机、四机等规模由 `--group-size` 控制 | 第 6 节 |
| 7 | DeepEP 测试 | 第 7 节 |
| 8 | byte-flux 测试 | 第 8 节 |

查看结果和常用选项见第 9 节。Conda 场景与容器场景执行相同的测试，仅入口参数不同（见 1.4 节）。

## 1. 启动方式与准备

### 1.1 使用构建后的安装包（推荐）

安装包自带完整私有 Python runtime 和 ClusterShell（clush 并发能力），控制节点无需安装系统 Python、pip 或 clush，只需 Bash 和可免密 SSH 登录的目标节点：

```bash
sha256sum -c hcu-envcheck-0.3.1-linux-x86_64.run.sha256
chmod +x hcu-envcheck-0.3.1-linux-x86_64.run
./hcu-envcheck-0.3.1-linux-x86_64.run
export PATH="$HOME/.local/bin:$PATH"
TOOL_ROOT="$HOME/.local/share/hcu-envcheck/current"
hcu-cluster-run --version
```

默认安装到 `~/.local/share/hcu-envcheck`，命令位于 `~/.local/bin/hcu-cluster-run`。安装器为当前用户的 Bash 启动文件添加 PATH；上面的 export 用于当前终端立即生效。可传 `--prefix /absolute/user/prefix` 指定目录，或 `--no-path` 自行管理 PATH。

升级重新执行新安装包，保留原配置。卸载：

```bash
bash "$HOME/.local/share/hcu-envcheck/uninstall.sh"
# 连同配置一起删除时：
bash "$HOME/.local/share/hcu-envcheck/uninstall.sh" --purge
```

### 1.2 使用代码工程

源码方式依赖：控制节点需要 Python 3.10+（或完整私有 runtime），且该 Python 已安装 ClusterShell（提供 clush 并发，`python3 -m pip install ClusterShell`，版本 ≥1.9）；目标节点可免密 SSH 登录。

从 Git 仓库根目录初始化本工程的第三方子模块，Python 路径替换为实际的 Python 3.10+：

```bash
git submodule update --init --recursive -- \
  hcu-envchck-new/third_party/DeepEP hcu-envchck-new/third_party/flux
cd hcu-envchck-new
TOOL_ROOT="$PWD"
chmod +x "$TOOL_ROOT/bin/hcu-cluster-run"
export PATH="$TOOL_ROOT/bin:$PATH"
export HCU_CONTROL_PYTHON=/path/to/python3
hcu-cluster-run --version
hcu-cluster-run --help
```

没有系统 Python 时，用完整私有 runtime 替代（选择优先级：工程内 `runtime/` → `HCU_CONTROL_RUNTIME` → `HCU_CONTROL_PYTHON`）：

```bash
export HCU_CONTROL_RUNTIME=/path/to/full-python-prefix
```

源码方式下，后文所有 `hcu-cluster-run` 命令也可写作 `bash bin/hcu-cluster-run`。

### 1.3 准备节点文件与公共参数

```bash
cat > hostfile <<'EOF'
node01
node02
node03
node04
EOF

HOSTFILE="$PWD/hostfile"
CONTAINER=hcu-test
IMAGE=YOUR_IMAGE:TAG
ENV_SCRIPT=/absolute/target/env.sh
OUT="$PWD/verify_results"
BASELINE="$TOOL_ROOT/config/rccl_baseline.conf"
DEPENDENCY_MANIFEST=/tmp/hcu-dependencies.json
```

hostfile 每行一个实际节点名，支持空行和 `#` 注释。

### 1.4 选择执行场景

所有功能命令的格式为 `hcu-cluster-run <场景> <操作> [参数]`。场景决定测试在哪种目标环境中执行，共三种：

| 场景 | 执行环境 | 必需参数 | 说明 |
| --- | --- | --- | --- |
| `shared-conda` | 各节点加载共享存储中同一个 Conda 环境 | `--env-script` | Conda 环境路径需各节点可见 |
| `node-local-conda` | 各节点加载各自本地的 Conda 环境 | `--env-script` | 各节点按同一路径的脚本分别激活本地环境 |
| `per-node-container` | 各节点进入指定 Docker 容器 | `--container`、`-i` | 宿主需已有 Docker 操作权限 |

三种场景共用分组、并发、空闲门禁、测试载荷和最终报告；同一测试在不同场景下除入口参数外完全一致。

各操作对场景的支持：

| 操作 | shared-conda | node-local-conda | per-node-container |
| --- | --- | --- | --- |
| container-create / status / recreate / delete | — | — | 仅容器 |
| platform | ✓ | ✓ | ✓ |
| rccl / deepep / byte-flux | ✓ | ✓ | ✓ |
| checkdeepep | — | — | 仅容器 |
| idle-check | ✓ | ✓ | ✓ |

参数随场景的替换规则（第 3、6～8 章示例默认容器场景）：

- 换 Conda 场景：把 `per-node-container` 换成 `shared-conda` 或 `node-local-conda`，删除 `--container`、`-i`、`--container-ssh-port`，追加 `--env-script`。Conda 场景不接受容器管理参数（`--port`、`-v`、`--docker-arg` 等）。
- 宿主及 Conda MPI SSH 默认 22，非默认端口统一传 `--ssh-port PORT`；`--target-python` 可指定激活后的解释器，`--target-workdir` 可设置 source 前的工作目录。
- 容器管理和 checkdeepep 仅容器场景可用。
- idle-check 检测的是宿主机本身：三种场景入口命令一致，都不需要容器参数或环境脚本。

### 1.5 准备目标环境脚本 env.sh

Conda 场景必须提供环境脚本；容器场景可选（容器内已配置好环境时可省略）。在每个目标 Conda/容器环境准备相同绝对路径的 `env.sh`（容器内文件须可读）：

```bash
# env.sh：替换为目标环境的实际路径；Conda 场景先激活环境。
# source /path/to/miniconda3/etc/profile.d/conda.sh
# conda activate /path/to/conda/envs/train
source /path/to/toolkit/env.sh
# 按实际环境设置库路径、网卡和通信参数。
```

后文使用 `--env-script "$ENV_SCRIPT"`；工具不自动复制脚本；目标环境及 MPI 远端命令按配置加载脚本，子进程继承环境。

使用本工程 `config/env.sh` 模板时，网卡参数在 `env.sh` 中配置；同目录的 `site_env.sh` 用于加载依赖环境，也必须在目标环境中可读。模板加载失败会返回非零退出码。

## 2. 容器操作

容器场景先在这里创建测试容器；后续检测和测试都以它为执行环境。以下为独立操作；重建/删除会移除原容器。创建端口与后续 `--container-ssh-port` 保持一致。每批一次 clush 在目标宿主调用 `container.sh`，脚本按已有 `stages.tsv` 在本节点顺序执行；各节点独立推进，阶段失败后本节点剩余阶段记 BLOCKED。`--timeout` 仍限制每个容器阶段，原始退出码、各阶段日志和容器/镜像完整 ID 保留在结果中；本批结果回收后才启动下一批。

```bash
# 创建。
hcu-cluster-run per-node-container container-create \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --port 25901 -o "$OUT"

# 查询。
hcu-cluster-run per-node-container container-status \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" -o "$OUT"

# 重建：需要的挂载和附加参数要重新传入。
hcu-cluster-run per-node-container container-recreate \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --port 25901 --yes -o "$OUT"

# 删除。
hcu-cluster-run per-node-container container-delete \
  -f "$HOSTFILE" --container "$CONTAINER" --yes -o "$OUT"
```

创建/重建可按需追加 `--ssh-dir /absolute/host/ssh-dir`、`--image-tar /absolute/host/image.tar`、`-v /host/data:/container/data:ro`。不传 ssh-dir 时复制各宿主登录账号的 `$HOME/.ssh`。

创建阶段验证本机 SSH 回环；跨节点容器连接由 RCCL/DeepEP 组预检检查。连接使用复制进容器的 SSH 客户端配置，该配置须允许指定端口的非交互登录。

删除时如额外提供 `-i IMAGE`，会先核对实际镜像；不匹配则不停止或删除容器。查询/删除不接受 `--container-user`。

## 3. 平台资源检测（platform）

对 hostfile 全部节点并发执行只读采集：硬件信息（CPU、内存、内核、计算卡型号/架构/计算单元/显存）和软件包信息（HCU 驱动、DTK、Python/Torch、RCCL、MPI、UCX、Transformer Engine、DeepEP 等版本），以及 RDMA/网络配置。汇总报告以“节点集合为行、检测项为列”的状态矩阵和版本对比展示，用于确认各节点环境一致。

按 hostfile 逐节点检测，不传 group-size；源码和安装包使用相同命令。

```bash
# 共享 Conda。
hcu-cluster-run shared-conda platform -f "$HOSTFILE" \
  --env-script /shared/hcu-config/env.sh -o "$OUT"

# 节点本地 Conda。
hcu-cluster-run node-local-conda platform -f "$HOSTFILE" \
  --env-script /opt/hcu-config/env.sh -o "$OUT"

# 已有容器。
hcu-cluster-run per-node-container platform -f "$HOSTFILE" \
  --container "$CONTAINER" -i "$IMAGE" --env-script "$ENV_SCRIPT" -o "$OUT"
```

查看 `summary.md`、`platform-checks.tsv`、`platform-findings.tsv` 和 `platform-consistency.md`。采集完成不等于平台健康通过；缺依赖、命令或权限会保留具体原因。

配置分组忽略系统内存总量和 `/dev/shm` 容量差异；原始数值保留在 `platform-nodes.json`。其余已采集的静态环境差异按节点或配置组展示，供用户自行判断。

`summary.md`、`platform-consistency.md` 和终端按“状态”“硬件信息”“软件信息”展示；`platform-overview.json` 保留分组及完整节点集合。
状态矩阵以节点集合为行、检测项为列；所有检测项状态一致才合并一行。正常项也展示，失败、提示、未知和不适用分别标明；异常原因另按检测项、状态和具体原因归并。连接或采集未完成的节点显示未知，不当作硬件已确认故障。所有检测项保持在同一矩阵中，不自动拆表；内容过宽时横向查看。硬件和软件信息表使用“类型、配置组、节点数、节点集合……”列序，版本差异仍由用户判断。
硬件信息从 `rocminfo` 读取计算卡型号、架构、计算单元和显存；CPU agent 不计入计算卡，重复 GLOBAL 内存池取最大值，不累加。型号按工具原值展示，不凭架构猜型号。硬件规格一致的节点合并；缺失或截断信息明确标为未采集或采集不完整。
主要软件版本按 HCU 驱动、DTK、Torch、Transformer Engine、DeepEP 分组展示；与参考节点不同的已采集版本加粗。新增组件查询包元数据，不导入扩展、不据版本差异判定健康失败。DeepEP 兼容 `deep-ep` 和 `deep-ep-shca`，Transformer Engine 同时采集 `transformer-engine`、`transformer-engine-torch`；并存且版本不同则完整列出，不猜测实际加载版本。未检出版本不等于未安装。
连续节点名称使用范围压缩；终端和报告直接显示各组节点集合与数量，逐节点明细仍保存在 `results.tsv`、`platform-checks.tsv` 和 `platform-nodes.json`。
参考节点取名称排序中首个采集成功且三个版本主项完整的节点，仅用于展示差异，不规定应采用哪个版本；参考节点名称同时保存在一致性 JSON 的 `reference_node` 中。
缺失信息标为“未采集”，包版本无法取得时另标“未检出版本”，不把缺失当成版本一致；采集流程完成情况与原有检测结果分别展示，健康 WARN 不直接计为采集未完成。
其他静态字段差异继续列在报告后部，检测异常和原因仍见 `platform-findings.tsv`，完整采集信息见 `platform-nodes.json`。

### 附：checkdeepep 通信环境检查（可选，仅容器）

一次执行 ACS、XDP、RDMA 和 ibv_devinfo 四项检查：

```bash
hcu-cluster-run per-node-container checkdeepep \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" -o "$OUT"
```

## 4. 空闲资源检测（idle-check，只读）

测试前逐节点并发检查宿主机全部计算卡是否空闲，并查询占卡进程的容器归属；无需 `--container`、`-i`、`--env-script`，不需要目标 Python。三种场景入口均可，检测都在宿主机执行、效果一致；以下示例沿用 `per-node-container` 入口：

```bash
hcu-cluster-run per-node-container idle-check -f "$HOSTFILE" -o "$OUT"
```

全部宿主卡 VRAM%=0、HCU%=0 才判定空闲；无效采样为 UNKNOWN。普通用户需已有 hy-smi 采样权限；查询归属还需已有 Docker 查询权限，工具不提权、不停止宿主进程。报告见 `summary.md`、`idle-results.tsv`，完整采样、归属和原始退出码见 `report.json`、`logs/idle-check/`。命令空闲返回 0，繁忙/未知返回 1（节点 BUSY 判定码保留为 2）。

## 5. 一键释放集群节点资源（idle-check --release）

资源不空闲时，用 `--release` 对全部节点执行“采样 → 核实占卡容器归属 → 停止整个容器 → 复检”的完整工作流：

```bash
hcu-cluster-run per-node-container idle-check -f "$HOSTFILE" --release -o "$OUT"

# 只查看释放计划，不连接节点或停止容器：
hcu-cluster-run per-node-container idle-check -f "$HOSTFILE" --release --dry-run
```

释放边界：仅停止已核实占卡的整个容器，不删除容器、不停止宿主进程、不调用 sudo。归属不明、PID/容器身份变化时不释放；释放前核验完整容器 ID、启动时间和进程归属，释放后最多复检五次，仍忙或未知均判失败。查询/停止容器需当前账号已有相应 Docker 权限。首次判断、每次采样、每次动作参数和退出码全部保留在报告中。

## 6. RCCL 测试

RCCL 十项 collective：all_reduce、all_gather、broadcast、reduce、reduce_scatter、gather、scatter、alltoall、alltoallv、sendrecv。省略 `--tests` 即执行全部十项。

测试规模由 `--group-size N` 控制：N=1 为单机一组，N=2 为双机一组，N=4 为四机一组，以此类推。工具把 hostfile 中的节点按顺序每 N 个切为一组，各组并发执行；剩余节点不足 N 个时尾组整组跳过，不计入结果。每节点卡数由 `--gpus-per-node` 指定，MPI 进程数为 `节点数×每节点卡数`，每卡一个 rank（单机组也走 MPI）。

RCCL 单项执行退出码非零（含超时）时，保留该项原始退出码并停止本组后续测试；未执行项记为 BLOCKED / GROUP_SKIPPED，并说明前项失败原因。其他组和后续批次继续。性能不达标由最终报告记为 BELOW_BASELINE，原生命令退出码仍为 0，不触发运行中停组。

以下示例基准适用于 **BW1300_LC、同一 40 卡 Hyswitch 超节点**；其他环境须更换为符合实际型号和布局的 profile。

### 单机测试：--group-size 1

hostfile 中每个节点各自一组，并发执行：

```bash
hcu-cluster-run per-node-container rccl \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" --container-ssh-port 25901 \
  --group-size 1 --gpus-per-node 4 \
  --baseline-file "$BASELINE" --baseline-profile supernode_bw1300_40_local4 \
  -o "$OUT"
```

### 多机测试：--group-size 2 双机（四机改 4）

```bash
hcu-cluster-run per-node-container rccl \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" --container-ssh-port 25901 \
  --group-size 2 --gpus-per-node 4 \
  --baseline-file "$BASELINE" --baseline-profile supernode_bw1300_40_2x4 \
  -o "$OUT"
```

四机十六卡把 `--group-size` 改为 4、profile 换成 `supernode_bw1300_40_4x4`，其余参数不变。hostfile 有 8 个节点、`--group-size 2` 时共 4 组双机测试，按 `--concurrency` 分批执行。

### 只执行指定项

`--tests` 可填写上述任一单项名称，或逗号连接多项：

```bash
hcu-cluster-run per-node-container rccl \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" --container-ssh-port 25901 \
  --group-size 1 --gpus-per-node 4 --tests all_reduce \
  --baseline-file "$BASELINE" --baseline-profile supernode_bw1300_40_local4 \
  -o "$OUT"
```

普通 BW1000 节点测试时，按下表替换对应参数；hostfile 填入实际普通节点：

| 每组规模 | group-size | gpus-per-node | baseline-profile |
| --- | --- | --- | --- |
| 单机八卡 | 1 | 8 | other_bw1000_single8 |
| 双机十六卡 | 2 | 8 | other_bw1000_dual16 |

### Conda 场景执行

shared-conda（各节点加载共享存储中同一个 Conda 环境）：

```bash
hcu-cluster-run shared-conda rccl -f "$HOSTFILE" \
  --env-script /shared/hcu-config/env.sh --group-size 2 --gpus-per-node 4 \
  --baseline-file "$BASELINE" --baseline-profile supernode_bw1300_40_2x4 \
  -o "$OUT"
```

node-local-conda（各节点加载各自本地 Conda 环境）：把场景词换成 `node-local-conda`、`--env-script` 指向各节点本地脚本路径（如 `/opt/hcu-config/env.sh`），其余参数相同。

## 7. DeepEP 测试

三项测试：单机 `intranode`、多机 `lowl`（低时延）与 `norm`（常态）。未指定 `--tests` 时，单机默认只执行 `intranode`，多机默认执行 `lowl,norm`。

支持范围按本次请求项分别校验：单机 `intranode` 要求 `--group-size 1` 且每节点 2/4/8 卡；多机 `lowl`/`norm` 要求 `--group-size ≥2`、每节点 8 卡，且总 GPU 数整除专家数（norm 256、lowl 288）。支持项继续执行；不支持项不启动第三方程序，报告记为 `FAIL / DEEPEP_UNSUPPORTED_CONFIG` 并列明原因，单项返回 64，最终命令返回 1。请求项全部不支持时，控制端列明各项原因并返回 2，不连接节点。未知测试名和重复项作为参数错误拒绝。

DeepEP、byte-flux 需要构建来源依赖记录，`--dependency-manifest` 指向目标环境内的绝对路径；首次使用按下文准备，已有有效记录时直接填写路径。

### 首次使用：准备 DeepEP / byte-flux 依赖记录

仅 DeepEP、byte-flux 需要；目标环境应已安装与本工程 third_party 源码匹配的扩展。对每个目标节点分别操作。

```bash
# 控制节点：把记录器复制到一个目标容器。
scp "$TOOL_ROOT/build/record_dependency.py" node01:/tmp/hcu-record_dependency.py
ssh node01 docker cp /tmp/hcu-record_dependency.py "${CONTAINER}:/tmp/hcu-record_dependency.py"
ssh -t node01 docker exec -it "$CONTAINER" bash
```

Conda 场景无需 docker cp，改为登录宿主后加载 Conda：

```bash
scp "$TOOL_ROOT/build/record_dependency.py" node01:/tmp/hcu-record_dependency.py
ssh -t node01 bash
```

在选定的 Conda/容器环境中执行，填写实际构建源码仓、wheel 和环境脚本路径：

```bash
source /absolute/target/env.sh
python3 /tmp/hcu-record_dependency.py \
  --library DeepEP --source /path/to/actual-build/DeepEP \
  --wheel /path/to/deep_ep_shca.whl --output /tmp/hcu-dependencies.json
python3 /tmp/hcu-record_dependency.py \
  --library flux --source /path/to/actual-build/flux \
  --wheel /path/to/byte_flux.whl --output /tmp/hcu-dependencies.json
exit
```

已有构建者提供的有效记录时，直接把 `DEPENDENCY_MANIFEST` 设置为该记录在各目标环境内的绝对路径。不要用 `pip install flux` 代替安装 byte-flux。

### 单机八卡：intranode

对 hostfile 中每个节点分别执行；单机也支持把卡数改为 2 或 4：

```bash
hcu-cluster-run per-node-container deepep \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" --group-size 1 --gpus-per-node 8 \
  --tests intranode \
  --dependency-manifest "$DEPENDENCY_MANIFEST" -o "$OUT"
```

### 双机十六卡：lowl 与 norm

一次执行两项（也可只写其中一项）：

```bash
hcu-cluster-run per-node-container deepep \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" --container-ssh-port 25901 \
  --group-size 2 --gpus-per-node 8 --tests norm,lowl \
  --dependency-manifest "$DEPENDENCY_MANIFEST" -o "$OUT"
```

四节点 hostfile 配合 `--group-size 2` 会并发执行两组双机测试；各组 rendezvous 端口固定为组首节点的 29501。

算法、网卡及 rocSHMEM 选项写入自己的 env.sh；工具不会强制设置 `DEEPEP_ENABLE_LL_LAYERED_OPT`。

### Conda 场景执行

shared-conda 单机八卡 intranode（对 hostfile 中每个节点分别执行）：

```bash
hcu-cluster-run shared-conda deepep -f "$HOSTFILE" \
  --env-script /shared/hcu-config/env.sh --group-size 1 --gpus-per-node 8 \
  --tests intranode --dependency-manifest "$DEPENDENCY_MANIFEST" -o "$OUT"
```

node-local-conda 双机十六卡，一次执行两项：

```bash
hcu-cluster-run node-local-conda deepep -f "$HOSTFILE" \
  --env-script /opt/hcu-config/env.sh --group-size 2 --gpus-per-node 8 \
  --tests norm,lowl --dependency-manifest "$DEPENDENCY_MANIFEST" -o "$OUT"
```

## 8. byte-flux 测试

每节点执行一次 FP16 AG 用例，TP 规模由 `--gpus-per-node N` 指定：4 卡运行 TP4，8 卡运行 TP8，其他 N 运行 TPN。逐节点执行，`--group-size` 必须为 1，不接受 `--tests`。使用目标环境可见的前 N 张卡，保留 env.sh 的设备可见性配置。矩阵形状固定为 M=2048、N=10240、K=8192；原生用例要求这些维度均能被 TP 规模整除。

byte-flux 同样需要第 7 章准备好的依赖记录。下例使用 4 卡，可按现场卡数调整：

```bash
hcu-cluster-run per-node-container byte-flux --gpus-per-node 4 \
  -f "$HOSTFILE" --container "$CONTAINER" -i "$IMAGE" \
  --env-script "$ENV_SCRIPT" \
  --dependency-manifest "$DEPENDENCY_MANIFEST" -o "$OUT"
```

### Conda 场景执行

node-local-conda 每节点一项；也可把场景词换成 `shared-conda` 使用共享环境：

```bash
hcu-cluster-run node-local-conda byte-flux -f "$HOSTFILE" \
  --env-script /opt/hcu-config/env.sh --gpus-per-node 4 \
  --dependency-manifest "$DEPENDENCY_MANIFEST" -o "$OUT"
```

## 9. 查看结果和常用选项

终端会输出本次运行目录，打开其中的 `summary.md`：

```bash
RUN_DIR=/absolute/path/printed/by/tool
cat "$RUN_DIR/summary.md"
cat "$RUN_DIR/results.tsv"
cat "$RUN_DIR/prepare-nodes.tsv"   # RCCL/DeepEP/byte-flux 逐节点预检原因和退出码
ls "$RUN_DIR/logs"
```

组摘要显示首个失败节点的原因及退出码；全部节点详见 `prepare-nodes.tsv` 和报告“各节点预检”。`exit_code` 为节点判定码，`command_exit_code` 为原始命令退出码；空白表示未执行或未取得回执。

按需在测试命令末尾添加：

| 选项 | 用法 |
| --- | --- |
| `--dry-run` | 仅查看计划，不运行测试 |
| `--skip-idle-check` | 明确允许非空闲测试时跳过宿主空闲预检 |
| `--concurrency 1024` | 每批最多执行的目标数，同时设置 clush 并发连接数（-f）；逐节点操作按节点计，分组测试按组计，超出后逐批执行 |
| `--timeout 1800` | 设置单目标当前阶段超时秒数 |
| `--ssh-user USER --ssh-port PORT` | 指定宿主 SSH 用户和端口 |
| `--target-python /absolute/target/python3` | 指定 source 后的目标 Python（容器也支持原参数 `--container-python`） |

platform 的完整节点配置另见 `platform-nodes.json`，原始采集输出保存在 `logs/platform/`。

完整参数：

```bash
hcu-cluster-run --help
```

`--gpus-per-node N` 控制每节点测试卡数。工具只在预检时确认运行时可用、可见卡数足够，不自动选卡、不修改设备可见性变量；特殊选卡配置写入各目标环境的 `--env-script`。DeepEP 单机支持 2/4/8 卡、多机仍要求每节点 8 卡并满足模式约束；byte-flux 每节点运行一次 FP16 AG，TP 规模、worker 数和报告 rank 覆盖均取 `--gpus-per-node N`。byte-flux 原生用例要求固定矩阵维度 M=2048、N=10240、K=8192 均能被 TP 规模整除。环境脚本在一次运行中应保持设备配置一致。

命令结束后，查看 `<运行目录>/logs/groups/group-001.log`，同组各项测试依次追加，按 `[TEST-BEGIN]` / `[TEST-END]` 区分。收集失败时查看 `.state/log-collection/group-001.json`。各节点本次运行的任务目录 `<remote-workdir>/<运行ID>/` 默认保留，可按需查看其中 `results/group-001.log` 等已生成的原始文件，确认无需排查后自行清理。

所有功能共用“分组与批次计划 → 本批组首节点操作 → 等待并回收本批结果 → 下一批 → 最后统一报告”流程。逐节点操作一个节点一组，RCCL/DeepEP 按 group-size 分组。`--concurrency C` 同时决定每份 `plan/clushhostfile.NNNN` 最多包含的组首节点数和 clush 的 `-f C`；G 个组生成 ceil(G/C) 份清单，按编号逐批执行，某组失败不阻断后续批次，取消后不启动新批次。全节点预检、分发和回收仍覆盖实际参与节点，预检失败的组在执行时从本批清单中过滤，不重新分组。

RCCL/DeepEP 只执行完整分组；不足 `--group-size` 的尾组跳过，不参与分发、预检、测试和基准比较，批次数按完整组数计算。跳过节点保存到 `plan/skipped.hosts`，终端、`summary.md` 和 `results.json` 列明节点及原因，不计入成功或失败统计。节点总数不足一个完整组时，提示“没有可执行的完整分组”并结束，不启动测试。逐节点操作仍覆盖 hostfile 全部节点。

RCCL 始终使用 `mpirun -np 节点数×每节点卡数`，各 rank 执行 `-g 1`；单节点组也需要 MPI，并校验 MPI 使用的 SSH 目标身份。DeepEP 根据测试类型运行原生 intranode Python 或 lowl/norm MPI+torchrun；byte-flux 直接 torchrun。检查和容器管理运行自身命令，不额外使用 MPI。目标环境按配置加载；任务 token 用于关联测试日志与执行事实。

所有目标的累计日志均保存为 `<运行目录>/logs/groups/<目标ID>.log`，每项以 TEST-BEGIN/TEST-END 和 token 分隔。公共模块负责追加、收集、完整性校验和退出码记录；报告仍按各功能指标解析。主动测试在目标环境追加原始日志，其他操作汇总已回传的阶段输出，避免无意义的重复 SSH。收集失败不能判定通过，原始执行事实及退出码仍单独保留。
