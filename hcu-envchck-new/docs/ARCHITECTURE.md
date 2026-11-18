# 模块与执行接口

Python 负责参数校验、执行计划、clush 调度、证据保存和报告；Shell 载荷在目标宿主或容器中加载环境、执行原生命令并记录结果。控制端使用私有 Python 和 ClusterShell；目标宿主通过 Shell、SSH 和 tar 完成引导，需要 Python 的检测使用目标环境中的解释器。

## 主调用链

```text
bin/hcu-cluster-run
  → hcu_envcheck/__main__.py
  → cli.main()                     解析、校验请求，展示计划
      → registry.get_operation()   选择操作模块
      → planning.build_plan()      生成目标和阶段
      → controller.run()           管理运行目录、取消和报告
          → planning.write_plan()  保存计划和 clushhostfile.NNNN
          → dispatch.run()         分发载荷，执行并回收结果
              → clush.Transport    按节点清单批量执行、复制与回收
              → payloads/entry.sh  识别本机，选择目标环境和载荷
                  → container.sh   宿主执行完整容器操作
                  → node_run.sh    platform / checkdeepep
                  → idle.sh        宿主空闲检查或显式释放
                  → node_check.sh  主动测试的逐节点预检
                  → group_run.sh   组首依次执行原生测试项
              → save_fact()        发布各目标、各阶段执行事实
          → execution.closed       关闭执行证据
          → reporting.engine.generate()
```

`--dry-run` 完成参数校验、分组和计划展示后返回，不创建运行目录，也不连接服务器。常规运行先完成所有目标的执行和结果回收，再统一生成报告。各节点本次运行的远端任务目录默认保留，便于复查脚本、配置和原始日志，使用完毕后由用户按需清理。

## 数据模型与操作接口

| 接口 | 职责 |
| --- | --- |
| `Request` | 保存 CLI 参数与显式传入的参数集合，区分控制端路径和目标端路径 |
| `Target` | 保存目标 ID 和有序节点；`leader` 为该目标的首节点 |
| `Plan` | 保存目标、有序阶段、是否分组及跳过的尾组节点 |
| `NodeResult` | 读取单节点回传的状态和结果目录；无结果不能判定通过 |
| `TaskResult`、`FACT_FIELDS` | 定义每个目标阶段的执行事实、原始退出码和证据位置 |
| `validate(request)` | 校验操作特有参数；无效请求在连接服务器前报错 |
| `stages(request)` | 声明本次操作的有序阶段 |
| `validate_plan(request, plan)` | 可选接口，操作模块提供时由 `build_plan()` 调用，校验生成的计划 |
| `interpret(node, target, stage, root, run_id)` | 单节点操作、容器操作将回传证据转成阶段事实 |
| `Transport.fanout / copy / rcopy` | 使用 clush 批量执行、分发及回收，不判断业务是否通过 |

主动测试的准备与测试事实由 `dispatch.py` 统一生成。操作模块声明参数、支持范围和测试项，节点脚本执行原生命令，报告模块判断完整性、正确性和性能。

## 分组、批次与并发

`planning.read_hosts()` 保留 hostfile 顺序，允许空行和注释，拒绝重复节点、额外字段和空文件。仅 RCCL、DeepEP 按 `--group-size` 分组；容器管理、platform、checkdeepep、idle-check、byte-flux 均逐节点执行，每个节点一个目标。

设完整目标数为 G、`--concurrency` 为 C，计划生成 `ceil(G/C)` 份 `plan/clushhostfile.NNNN`。每份最多 C 个组首节点；逐节点操作的组首就是节点自身。默认 C 为 1024，clush 使用 `-f C`，当前执行路径没有额外的 64 线程上限。

```text
控制端 hcu-cluster-run
  → 本批一条 clush --hostfile clushhostfile.NNNN -f C
      → 节点或组首 → entry.sh → 对应载荷
  → 等待本批结束并回收结果
  → 下一批
  → 全部批次完成后统一报告
```

控制端不再逐节点启动一个 clush 进程。SSH 连接由 clush 的传输层处理；MPI 所需的组内 SSH 由组首节点及 MPI 运行时使用。`concurrency.parallel_map()` 的线程池用于最终报告解析，不承担容器操作调度。

RCCL、DeepEP 只处理完整分组。不满 `group-size` 的尾组写入 `plan/skipped.hosts`，不分发、不预检、不测试，也不参与基准比较；终端及报告列出节点和原因。如果没有一个完整组，直接报错结束。逐节点操作仍处理 hostfile 全部节点。

## 分发与目标环境

`dispatch.py` 将 payload、计划、`RUN_ID`、`config.env` 及本次需要的第三方测试源码打为 `bundle.tar`，通过 clush 分发至实际参与节点。远端目录为 `<remote-workdir>/<run-id>`。`--remote-workdir` 应是各节点本地目录，各节点使用相同绝对路径；结果文件在该目录中按本机执行写入。

`entry.sh` 将本机 hostname、短名、全名和 IP 与 `plan/all.hosts` 匹配，要求唯一对应一行。它按 `check / node / group / container` 模式调用载荷，并将结果打包为 `out.tgz`。控制端通过 clush 回收，未回传的节点重试一次；缺少有效状态文件时记录 `NODE_NO_RESULT` 等失败原因。

`per-node-container` 的 Docker 命令在目标宿主执行，宿主账号须已有 Docker 权限。测试前检查容器运行状态、实际镜像 ID，以及容器内是否可读本次 `RUN_ID`。容器必须以相同绝对路径看到宿主的远端任务目录；工具的 `container-create` 会挂载 `--remote-workdir`。无需将整个代码工程挂入容器，私有控制端 Python 也不进入容器。

`shared-conda` 和 `node-local-conda` 共用宿主执行路径。环境由 `--env-script` 指定，工具不猜测 Conda 路径。容器管理和独立 checkdeepep 仅接受容器场景；idle-check 的三种场景入口均在宿主执行。

`--env-script` 指目标环境已有的绝对路径，工具不自动分发。`common.sh::hcu_source_env()` 切换到目标工作目录后 source 脚本，检查返回码，并将配置导出给子进程；MPI 远端命令也会加载该脚本。`config/env.sh` 和 `config/site_env.sh` 是环境配置文件，`config/rccl_baseline.conf` 是控制端读取的性能基准。

工具继承用户配置的设备可见性，不改写 `HIP_VISIBLE_DEVICES` 或 `CUDA_VISIBLE_DEVICES`。`gpu_check.py` 只确认 HIP（RCCL）或 Torch（DeepEP、byte-flux）可用且可见卡数不少于请求值；不选卡、不预留资源。用户环境脚本应在同一次运行中保持设备配置一致。

## 节点端执行流程

| 操作 | 节点载荷与流程 |
| --- | --- |
| 容器查询 | `container.sh`：inspect |
| 容器创建 | `container.sh`：preflight → create → ssh-identity |
| 容器重建 | `container.sh`：preflight → replace → create → ssh-identity |
| 容器删除 | `container.sh`：preflight → delete |
| platform | `node_run.sh` → 目标 Python 执行 `platform_probe.py`；不做空闲门禁 |
| checkdeepep | `node_run.sh` → `check_deepep_env.sh`，保留原检查输出 |
| idle-check | 宿主 `idle.sh run`，按请求只读检查或停止占卡容器后复检 |
| RCCL / DeepEP / byte-flux | 全节点 `entry.sh check` → 合格目标的首节点 `group_run.sh` → 对应原生测试脚本 |

容器各阶段在同一节点内依次执行，每阶段单独记录状态、元数据和日志；某阶段失败则阻断该节点后续阶段，其他节点继续。控制端 `operations/container.py` 负责校验、阶段声明与回执解析；`operations/_docker.py` 仅提供容器名、ID 正则和公共参数校验。

创建容器时复制宿主指定的 SSH 材料至容器执行账号的 HOME，拒绝写入宿主挂载的 SSH 目标目录，启动 sshd 并进行本机回环验证。创建阶段不做跨容器验收；主动测试的组预检检查各成员的 `RUN_ID`。组内 SSH 与 MPI 使用目标账号已有的客户端配置及请求端口，工具不额外采集或分发 known_hosts。当前未实现容器重启后的 sshd 自动恢复。

主动测试先对全部参与节点进行宿主空闲门禁（可用 `--skip-idle-check` 跳过）及目标环境预检，再判断每组是否全员通过。容器组还要求实际镜像 ID 一致。失败组不重新分组，其他就绪组仍按原批次清单执行。逐节点预检结果保存在 `.state/prepared/`，报告输出 `prepare-nodes.tsv`。

## 原生测试与失败处理

| 操作 | 启动方式与规模 |
| --- | --- |
| RCCL | `rccl.sh` 在组首执行一次 `mpirun -np 节点数×gpus-per-node`，slots 为每节点卡数，每个 rank 使用 `-g 1`；单节点组也使用 MPI |
| DeepEP intranode | `deepep.sh` 直接运行原生 Python 用例，以 `--num-processes` 传入每节点卡数，由用例创建 GPU workers |
| DeepEP lowl / norm | 组首执行 `mpirun -np 节点数`，每节点一个控制 rank，启动一个单进程 torchrun，再由原生用例创建该节点 GPU workers |
| byte-flux | `byte_flux.sh` 在每节点运行一次 standalone torchrun，进程数和 TP 规模取 `--gpus-per-node`，执行 FP16 AG 用例 |

RCCL 程序由环境中的显式目录、PATH 和搜索根定位。默认参数为 `-b 4 -e 1G -f 2 -n 20 -w 5`，可通过 `RCCL_ARG_B/E/F/N/W` 调整。byte-flux 当前形状为 M=2048、N=10240、K=8192，warmup=5、iters=10；工具没有固定每节点必须 8 卡的校验，原生库或用例不支持时按实际执行结果报告。

DeepEP 支持范围由 `operations/deepep.py` 按项声明：intranode 为单节点 2/4/8 卡；lowl、norm 为多节点且每节点 8 卡，总卡数还须分别整除专家数 288、256。请求项全部不支持时在控制端报错；同时包含支持与不支持项时，保留请求顺序，支持项执行，不支持项记录退出码 64 及具体原因。

`group_run.sh` 顺序执行本组测试，每项使用 `timeout --kill-after=30s` 并写入 `[TEST-BEGIN]`、`[TEST-END]`、token 和原始退出码。RCCL 任意一项原生命令非零退出后停止本组后续项，未执行项记为 `BLOCKED / GROUP_SKIPPED`；其他组和后续批次继续。性能阈值在最终报告阶段比较，`BELOW_BASELINE` 不触发执行过程中的停组。DeepEP 不采用 RCCL 的执行失败停组规则。

取消会终止控制端已登记的 clush 进程组，并阻止新批次启动；回收仍可继续。当前没有 task_guard 或远端 rank 残留确认机制，不能保证异常退出后所有远端测试进程都已结束。本次运行的远端任务目录默认保留，按需手工清理；残留占卡应通过独立 idle-check 检查，再按用户请求释放。

## 第三方测试源码与依赖

`dependencies.py` 从 `third_party/DeepEP`、`third_party/flux` 的 Git 提交或发行包快照导出所需测试源码及元数据，不接受来源不明或校验不一致的快照。分发的是测试所需文件，不是目标 GPU 运行库。

`payloads/dependency_check.py` 在实际目标 Python 中导入包和扩展，核对源码提交、构建记录及安装文件指纹。`build/record_dependency.py` 用于构建安装后显式记录依赖来源，检测过程不自动调用它，也不自动安装或编译依赖。匹配、缺失、不匹配、来源未知均有明确结果；缺少可信来源证据会阻断主动测试。

## 独立空闲检测与释放

`idle.sh` 在宿主采样全部卡的利用率和显存信息，形成 IDLE、BUSY 或 UNKNOWN；无效证据不能当作空闲。主动测试只调用其只读 gate 模式，不调用释放动作。

独立 `idle-check --release` 查询占卡 PID 与容器归属，核对进程启动信息和容器身份后停止整个占卡容器，再确认容器已停止并复检资源。Docker 查询失败记为未确认，不能视为停止成功。工具不删除容器、不杀宿主普通进程，也不修改用户权限。一次空闲采样仅描述当时状态，不代表后续测试已独占设备。

## 执行证据与统一报告

```text
<scenario>_<operation>_<run-id>/
  run.json                     运行状态
  plan/                        请求、目标、阶段、批次与分组文件
  bundle.tar                   本次分发内容
  clush/                       分发、执行与回收的传输日志
  collected/<mode>/<node>/      节点回传结果
  logs/<stage>/                各目标阶段的 stdout / stderr
  logs/groups/<target-id>.log   目标累计原始日志
  .state/facts/                各目标阶段事实 TSV
  .state/log-collection/       累计日志收集状态和 SHA256
  .state/prepared/              主动测试逐节点预检回执
  .state/platform/              平台采集上下文
  .state/execution.closed       报告读取屏障
  execution.tsv                原始执行事实
  results.tsv / results.json   解析后的结论
  summary.md                   人可读总报告
  report-status.tsv            汇总状态与命令退出码
```

主动测试累计日志由组首写入，回收后保存至 `logs/groups/`；其他操作由 `dispatch.publish_stages()` 汇总已回传的阶段输出。`write_receipt()` 记录本地累计日志的 SHA256，报告核对边界、token、身份与内容完整性。日志缺失、无效或未完整结束不能判定通过，也不能覆盖原生命令退出码。

`reporting.engine.generate()` 只在 `execution.closed` 存在后运行，先写 `execution.tsv`，再派生 `results.tsv` 和各操作报告。命令退出 0 与健康、正确性、性能通过分别判断。RCCL 解析原始数据与带宽并对照公共基准；DeepEP、byte-flux 分别核对启动规模、原生正确性结果和指标；checkdeepep 核对原检查项与退出码；idle 报告保留采样、归属、释放及确认结果。

platform 由 `platform_probe.py` 采集，`environment.py`、`rdma.py`、`roce_health.py`、`ib_counters.py` 解释证据，`reporting/platform_report.py` 与 `consistency.py` 生成三类总览：健康状态按“节点集合为行、检查项为列”合并相同状态的节点；硬件信息按相同规格合并；软件信息按相同版本合并，并突出相对参考组的差异。正常、失败、未知状态及对应节点均保留，完整节点列表和原始值可在 JSON 中查看。

硬件规格和软件版本差异供用户判断，不因版本不同就判为硬件异常。MemTotal、`/dev/shm` 容量不参与一致性分组，但原值仍保存在 `platform-nodes.json`。平台额外输出 `platform-checks.tsv`、`platform-findings.tsv`、`platform-nodes.json`、`platform-profile.json`、`platform-consistency.json/md` 和 `platform-overview.json`。采集退出 0 只表示采集完成，健康规则仍可得到 FAIL、WARN 或 UNKNOWN。

最终汇总为 PASS、WARN 或 FAIL，命令退出码分别为 0、2、1；取消返回 130，报告生成异常返回 70。跳过的尾组在总报告中单独列明，不计入通过或失败目标。

## 源码、安装与发行

`bin/hcu-cluster-run` 优先使用发行包内 runtime，也支持显式指定控制端 runtime 或解释器。启动器隔离控制 Python 环境，`clush.py`、`clush_entry.py` 恢复启动前的环境供外部传输进程使用，避免私有 Python 的变量污染 SSH 等命令。

`build/build_package.sh` 将源码、配置、文档、第三方测试快照和私有 runtime 组合为离线发行物，不自动编译 GPU 依赖。`build/installer_header.sh.in` 负责自解压包的校验和解包；`install.sh`、`uninstall.sh` 与 `build/user_install_common.sh` 管理用户前缀、安装归属标记、锁及 PATH。安装过程不执行集群测试，也不操作容器。

默认安装位于 `~/.local/share/hcu-envcheck`，版本目录包含独立源码与 runtime，用户配置单独保留；`~/.local/bin/hcu-cluster-run` 指向固定 release 的入口。`tests/` 保存离线单测及所需样本、桩程序；`dist/` 用于生成发行物，`verify_results/` 用于默认运行结果，二者均不是业务源码。

使用参数与完整操作示例见 [用户手册](USER_GUIDE.md)，平台规则来源见 [平台迁移说明](PLATFORM_MIGRATION.md)。
