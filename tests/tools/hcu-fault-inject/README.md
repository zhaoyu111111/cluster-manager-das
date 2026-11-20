<!-- Copyright (c) 2026 Hygon Information Technology Co., Ltd. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# hcu-fault-inject

在 Linux 上对指定训练进程、作业 cgroup、本机 HCU、专用测试网络接口或自有 RDMA QP 注入故障。每轮选择一个场景和一个目标，默认持续 30 秒，到期自动恢复，也可以提前手动恢复。

## 执行注入

在目标进程所在的 PID namespace 中运行。把下面的 12345 换成本次要测试的训练进程 PID。

每轮选择一个场景，执行 `start 场景 目标`。观察完注入效果后，使用同一个场景名和目标执行 `stop 场景 目标`。CPU、NUMA 和内存场景的目标是训练 PID，HCU 场景的目标是 HCU 编号，网络场景使用接口名或接口名加端口。中途按下 `Ctrl+C` 时，直接执行对应的 `stop`。

```bash
pid=12345
python3 -B ./hcu-fault-inject list

# 降低作业 CPU 配额
python3 -B ./hcu-fault-inject start cpu-low "$pid"
python3 -B ./hcu-fault-inject stop cpu-low "$pid"

# 缩小进程可用的 CPU 核集合
python3 -B ./hcu-fault-inject start cpu-cores "$pid"
python3 -B ./hcu-fault-inject stop cpu-cores "$pid"

# 加入同核计算争用
python3 -B ./hcu-fault-inject start cpu-contend "$pid"
python3 -B ./hcu-fault-inject stop cpu-contend "$pid"

# 移到远端 NUMA 节点的 CPU
python3 -B ./hcu-fault-inject start numa-remote "$pid"
python3 -B ./hcu-fault-inject stop numa-remote "$pid"

# 加入同 NUMA 内存带宽争用
python3 -B ./hcu-fault-inject start numa-bandwidth "$pid"
python3 -B ./hcu-fault-inject stop numa-bandwidth "$pid"

# 施加内存回收压力
python3 -B ./hcu-fault-inject start memory-reclaim "$pid"
python3 -B ./hcu-fault-inject stop memory-reclaim "$pid"

# 降低硬内存上限，可能终止目标作业内的进程
python3 -B ./hcu-fault-inject start memory-oom "$pid"
python3 -B ./hcu-fault-inject stop memory-oom "$pid"

# 限制作业新建进程和线程
python3 -B ./hcu-fault-inject start pids-low "$pid"
python3 -B ./hcu-fault-inject stop pids-low "$pid"
```

| 场景 | 作用范围 | 默认行为 | 恢复后核对 |
| --- | --- | --- | --- |
| `cpu-low` | PID 所属 CPU cgroup | 降低当前配额。无配额时设为一个逻辑 CPU 的 25%。 | `restored.setting` 等于 `baseline.setting`。 |
| `cpu-cores` | 指定进程的线程 | 缩小当前可用 CPU 集合。 | 仍存活的原线程亲和性恢复，核对 `restored.thread_affinity`。 |
| `cpu-contend` | 指定进程和一个同核辅助进程 | 选择可用的同核执行位置，必要时临时绑定目标线程。 | 原线程亲和性恢复，`restored.helpers` 中的辅助进程均已退出。 |
| `numa-remote` | 指定进程的线程 | 根据已有匿名页的位置，选取另一 NUMA 节点的允许 CPU。 | 仍存活的原线程恢复到原 CPU 绑定。 |
| `numa-bandwidth` | 指定进程和内存辅助进程 | 在目标内存所在节点选择目标亲和性以外的允许 CPU，施加有界负载。 | `restored.helpers` 中的辅助进程均已退出。 |
| `memory-reclaim` | PID 所属内存 cgroup | cgroup v2 下将 `memory.high` 调到当前用量的约 75%，受已有上限约束。 | `restored.setting` 等于 `baseline.setting`。已回收的缓存由后续访问重建。 |
| `memory-oom` | PID 所属内存 cgroup | 降低硬内存上限，默认以当前用量的一半为目标。实际 OOM 由内核判定。 | `restored.setting` 等于 `baseline.setting`。已被 OOM 终止的进程由作业重新启动。 |
| `pids-low` | PID 所属 PID cgroup | 将额度限制在当前任务数附近，阻止继续增长。 | `restored.setting` 等于 `baseline.setting`。 |

cgroup 类场景作用于 **PID 所属的整个作业组**。请选取本次试验的专用作业，调用用户需有对应控制文件的写权限，工具和监督进程应位于目标组之外。进程类场景需要相应的进程操作权限。场景缺少必要的 CPU、NUMA 或 cgroup 条件时，工具会报出原因。

## HCU 设备与资源争用

在训练节点或容器中运行 `devices`，按输出选择 HCU 编号。工具使用本地 `hy-smi --showbus` 的编号并核对设备的 PCI 地址。以下命令使用 HCU 0。

```bash
python3 -B ./hcu-fault-inject devices

# 核心低频档
python3 -B ./hcu-fault-inject start clock-low 0
python3 -B ./hcu-fault-inject stop clock-low 0

# 核心高性能档
python3 -B ./hcu-fault-inject start clock-high 0
python3 -B ./hcu-fault-inject stop clock-high 0

# 核心低频脉冲
python3 -B ./hcu-fault-inject start clock-pulse 0
python3 -B ./hcu-fault-inject stop clock-pulse 0

# 降低功率上限
python3 -B ./hcu-fault-inject start power-low 0
python3 -B ./hcu-fault-inject stop power-low 0

# 功率上限脉冲
python3 -B ./hcu-fault-inject start power-pulse 0
python3 -B ./hcu-fault-inject stop power-pulse 0

# 单卡计算争用
python3 -B ./hcu-fault-inject start hcu-compute-contend 0
python3 -B ./hcu-fault-inject stop hcu-compute-contend 0

# 单卡显存带宽争用
python3 -B ./hcu-fault-inject start hcu-memory-contend 0
python3 -B ./hcu-fault-inject stop hcu-memory-contend 0
```

| 场景 | 默认行为 |
| --- | --- |
| `clock-low`、`clock-high` | 切到驱动提供的低频档或高性能档。记录原档位和实测频率，恢复原档位并再次采样。自动档的实时频率随负载变化。 |
| `clock-pulse` | 每 2 秒在低频档和原档之间切换。 |
| `power-low`、`power-pulse` | 默认将功率上限降低到原值的一半，受设备最低上限约束。脉冲场景每 2 秒切换。可用 `--value 343` 直接指定 343 W。 |
| `hcu-compute-contend` | 在选中 HCU 启动有时限的 HIP 计算负载，报告辅助进程的计算吞吐。 |
| `hcu-memory-contend` | 在选中 HCU 循环进行设备内拷贝，报告辅助进程的拷贝吞吐。 |

频率和功率场景需要选中卡的设备控制文件可写，作用于该卡上的所有进程。驱动的 `manual` 档在无法读回完整频率掩码时会被拒绝，以保留原设置。争用场景需要可用的 HIP 运行时及 `hipcc`。结束时停止本会话的辅助进程。

### 设备操作延迟与错误

需要模拟训练进程收到 HIP 同步延迟或错误时，用 `wrap` 启动训练。保留原有训练启动命令和参数，在其前加 `python3 -B ./hcu-fault-inject wrap --`。该命令用 `cc` 编译并加载 HIP 钩子。

```bash
# 训练终端。按实际情况替换 train.py 和它的参数
python3 -B ./hcu-fault-inject wrap -- python3 train.py

# 训练运行期间，在同一节点、同一账号的另一个终端测试延迟
python3 -B ./hcu-fault-inject start hcu-op-delay 0
python3 -B ./hcu-fault-inject stop hcu-op-delay 0

# 确认上一项恢复后，再测试错误返回
python3 -B ./hcu-fault-inject start hcu-op-error 0
python3 -B ./hcu-fault-inject stop hcu-op-error 0
```

`hcu-op-delay` 在目标卡的 HIP 同步调用前增加最多 350 毫秒等待。`hcu-op-error` 在目标卡同步成功后模拟返回 HIP 错误 719。钩子覆盖 `hipDeviceSynchronize` 和能确认设备归属的 `hipStreamSynchronize`。调用发生后，可在 `current.first_hit` 或 `restored.first_hit` 中查看命中的 PID 和 API。已在运行且通过 `wrap` 启动的训练可随时注入和恢复。多个作业需要独立作用域时，给同一作业的 `wrap`、`start`、`stop` 和 `status` 使用同一个 `--state-dir`。

## TCP、IPoIB 与隔离链路

在测试接口所在的私有网络命名空间中执行。链路切换、TCP 弱网和连接重置使用已经启用、无默认路由的专用 veth，发送限速也支持专用 IPoIB 子接口。给选中的接口设置测试标记。调用账号需要该命名空间的 `CAP_NET_ADMIN` 权限，环境中需要 iproute2 的 `ip`、`tc` 和 `ss`。链路切换使用 IPv4 直连测试网段，接口可保留系统自动生成的 IPv6 链路本地地址。

链路切换和发送限速使用接口名。其余 TCP 场景使用 `接口:端口` 匹配发送方向的 TCP 目的端口，或使用 `接口:src:端口` 匹配发送方向的 TCP 源端口。下面以私有接口 `fi-veth0` 和测试服务端口 9000 为例。每次选择一组命令，观察注入效果后再执行对应的 `stop`。

```bash
iface=fi-veth0
flow="$iface:9000"
reply="$iface:src:9000"
ip link set dev "$iface" alias hcu-fault-inject

# 关闭测试链路
python3 -B ./hcu-fault-inject start rail-down "$iface"
python3 -B ./hcu-fault-inject stop rail-down "$iface"

# 周期性切换测试链路
python3 -B ./hcu-fault-inject start rail-flap "$iface"
python3 -B ./hcu-fault-inject stop rail-flap "$iface"

# TCP 延迟
python3 -B ./hcu-fault-inject start tcp-delay "$flow"
python3 -B ./hcu-fault-inject stop tcp-delay "$flow"

# TCP 全丢包
python3 -B ./hcu-fault-inject start tcp-loss "$flow"
python3 -B ./hcu-fault-inject stop tcp-loss "$flow"

# TCP/IPoIB 发送限速
python3 -B ./hcu-fault-inject start tcp-rate "$iface"
python3 -B ./hcu-fault-inject stop tcp-rate "$iface"

# TCP 延迟抖动
python3 -B ./hcu-fault-inject start tcp-jitter "$flow"
python3 -B ./hcu-fault-inject stop tcp-jitter "$flow"

# TCP 乱序
python3 -B ./hcu-fault-inject start tcp-reorder "$flow"
python3 -B ./hcu-fault-inject stop tcp-reorder "$flow"

# TCP 随机丢包
python3 -B ./hcu-fault-inject start tcp-random-loss "$flow"
python3 -B ./hcu-fault-inject stop tcp-random-loss "$flow"

# TCP 突发丢包
python3 -B ./hcu-fault-inject start tcp-burst-loss "$flow"
python3 -B ./hcu-fault-inject stop tcp-burst-loss "$flow"

# TCP 单向中断，在服务端的发送接口按源端口拦截回包
python3 -B ./hcu-fault-inject start tcp-drop "$reply"
python3 -B ./hcu-fault-inject stop tcp-drop "$reply"

# 在客户端的测试接口重置一条已建立的连接
python3 -B ./hcu-fault-inject start tcp-reset "$flow"
python3 -B ./hcu-fault-inject stop tcp-reset "$flow"
```

| 场景 | 默认行为 |
| --- | --- |
| `rail-down`、`rail-flap` | 将接口置为管理关闭状态，或每 2 秒在关闭和启用之间切换。 |
| `tcp-delay`、`tcp-loss` | 为匹配的 TCP 分组增加 80 ms 延迟，或丢弃全部匹配的发送分组。 |
| `tcp-rate` | 将整个专用接口的发送速率限制为 40 Mbit/s。 |
| `tcp-jitter`、`tcp-reorder` | 在 45 ms 基础延迟上增加 ±25 ms 抖动，或用 40 ms 延迟让每三个分组中的一个提前发送。 |
| `tcp-random-loss`、`tcp-burst-loss` | 以 10% 概率随机丢包，或以 5% 概率进入连续丢包状态、33% 概率返回正常状态。 |
| `tcp-drop` | 丢弃本接口发送方向匹配的 TCP 分组。 |
| `tcp-reset` | 在测试接口上唯一匹配的已建立 TCP 连接上执行一次重置。恢复时结束故障会话，应用通过新建连接恢复通信。 |

实际 IPoIB 路径的限速可直接施加在专用 IPoIB 子接口。测试该路径上的 TCP 弱网时，在数据经过的私有转发 veth 上施加故障。这些场景面向 TCP/IP 流量。

TCP 弱网需要内核支持 `sch_prio`、`sch_netem` 和 `cls_flower`。发送限速需要 `sch_tbf`，连接重置需要内核启用 `CONFIG_INET_DIAG_DESTROY`。

恢复时使用相同的网络命名空间、目标和会话目录。链路切换可核对 `restored.admin_up`、`restored.addresses` 和 `restored.ipv4_routes`，队列场景可核对 `restored.active` 为 `false` 且 `restored.queue_fingerprint` 等于 `baseline.queue_fingerprint`。TCP 重置可通过新连接成功收发来验证恢复。

## 原生 RDMA 与 RCCL

在拥有训练进程的节点和 PID、网络命名空间中运行。先用 `rdma-path` 查训练 PID 使用的 HCA、端口、QPN 和端口计数。端口总计数包含该端口上全部作业的流量。测试目标使用命令返回的本地 QPN。

```bash
pid=12345
python3 -B ./hcu-fault-inject rdma-path "$pid"

# 暂停和恢复已有 RDMA QP 的测试进程
python3 -B ./hcu-fault-inject start rdma-send-stop "$pid"
python3 -B ./hcu-fault-inject stop rdma-send-stop "$pid"

# 为已确认归属的 QP 临时绑定计数器
qp="$pid:9242"
python3 -B ./hcu-fault-inject start rdma-counter "$qp"
python3 -B ./hcu-fault-inject stop rdma-counter "$qp"
```

`rdma-send-stop` 暂停整个进程，恢复后可用 `rdma-path` 查看原 QP。`rdma-counter` 需要驱动支持、命名空间内的 `CAP_NET_ADMIN` 权限和关闭自动绑定的端口，使用尚未绑定的测试 QP。若同一 PID 在不同 HCA 上存在相同 QPN，在目标后写明链路，例如 `12345:9242@shca_0/1`。

### 指定 QP 和完成事件

用 `rdma-wrap` 启动要注入的 verbs 测试程序，环境需要 `cc` 和 libibverbs 开发库。注入终端使用相同账号、节点和会话目录，先用 `rdma-path` 找到实际 QPN，再选一组命令。

```bash
# 测试程序所在终端，换成实际启动命令
python3 -B ./hcu-fault-inject rdma-wrap -- ./verbs-test

# 注入终端，替换成 rdma-path 查到的 PID 和 QPN
qp=12345:9242@shca_0/1
python3 -B ./hcu-fault-inject start rdma-qp-wait "$qp"
python3 -B ./hcu-fault-inject stop rdma-qp-wait "$qp"

python3 -B ./hcu-fault-inject start rdma-qp-error "$qp"
python3 -B ./hcu-fault-inject stop rdma-qp-error "$qp"

python3 -B ./hcu-fault-inject start rdma-cq-delay "$qp"
python3 -B ./hcu-fault-inject stop rdma-cq-delay "$qp"
```

| 场景 | 生效与恢复检查 |
| --- | --- |
| `rdma-qp-wait` | 目标 RC QP 下一次提交接收前等待 200 ms，之后继续提交。 |
| `rdma-qp-error` | 目标 RC QP 下一次提交发送或接收前进入 ERR。双轨测试可指定一轨的 QP，并独立观察另一轨。停止后清除规则，故障 QP 由作业重建。 |
| `rdma-cq-delay` | 将目标 QP 的下一批轮询完成结果延后约 550 ms 交给应用。批次中混有其他 QP 时记录为不支持。 |

用 `status SESSION_ID --json` 检查 `current.hit_status` 和 `current.hit_detail`。记录为 `applied` 表示实际命中。钩子通过 libibverbs 上下文的提交和轮询入口工作，RCCL 作业需要以实际 QP、调用路径和命中记录验收。

### 持续和突发拥塞

两端使用已准备好的隔离测试路径，控制地址位于标记为 `hcu-fault-inject`、无默认路由的直连私网接口。选择每端的 HCA 和端口，接收端授权一个发送地址。环境需要支持指定本地绑定地址和软件限速的 `ib_write_bw`。

```bash
# 接收端，输出一次性 IP:PORT:TOKEN
python3 -B ./hcu-fault-inject rdma-listen shca_0/1 --bind 198.18.0.2 --peer 198.18.0.1

# 发送端，粘贴接收端输出后选择一种注入
read -r peer
target="shca_0/1@$peer"
python3 -B ./hcu-fault-inject start rdma-congest "$target"
python3 -B ./hcu-fault-inject stop rdma-congest "$target"

# 新一轮使用接收端重新生成的令牌
python3 -B ./hcu-fault-inject start rdma-burst "$target"
python3 -B ./hcu-fault-inject stop rdma-burst "$target"
```

默认 30 秒，最长 120 秒。默认发送上限 25 Gbit/s，可用 `--value` 设置 1 至 50 Gbit/s。突发场景以 1 秒发送、1 秒暂停切换。结束时核对 `restored.load_active` 为 `false`。训练链路是否出现争用需结合端口计数和训练通信时间测量。

### RCCL 网卡选择

在启动作业时按本节点实际端口设置 `NCCL_IB_HCA`。多端口用逗号分隔。作业开始后可用 `rdma-path` 核对实际 QP 所在链路。

```bash
python3 -B ./hcu-fault-inject rccl-run shca_0/1,shca_1/1 -- python3 train.py
```

RCCL 使用作业环境已有的网络插件。811 的 DTK 镜像内，SHCA 插件位于 `/opt/rccl-rdma-sharp-plugins/lib`，该目录加入 `LD_LIBRARY_PATH` 后可通过 `NCCL_NET_PLUGIN=shca` 选择。

原生 RDMA 精确限速需要支持 pacing 且可读回、恢复原速率的站点驱动接口。当前通用 verbs 后端提供配对测试流的软件限速。

## 查看结果和恢复

用执行 `start` 时的同一节点、容器和账号执行 `stop`。若注入时指定了 `--state-dir`，恢复和查询也使用同一路径。默认在 30 秒后自动恢复，提前结束时执行该场景的 `stop`。

```bash
python3 -B ./hcu-fault-inject stop cpu-low "$pid" --json
python3 -B ./hcu-fault-inject status --json
```

`stop --json` 的状态为 `restored` 时，可在 `restored` 字段核对恢复值。`restore_reason` 显示手动或到期恢复。注入已到期恢复，或中断发生在会话创建之前时，`stop` 返回 `inactive` 并成功退出。`stop` 使用已记录的注入配置。`memory-oom` 终止目标进程后，只要原 cgroup 仍存在，就可用原 PID 恢复限额。因故障退出的训练进程由作业或测试调用方重启。

HCU 频率场景核对 `restored.mode` 与 `baseline.mode`，功率场景核对 `restored.power_cap_uw` 与 `baseline.power_cap_uw`。两项 HCU 争用核对 `restored.load_active` 为 `false`。两项 HIP 同步故障核对 `restored.armed` 为 `false`，再观察后续同步调用是否正常。

### 中断后的恢复步骤

在执行 `start` 的终端按 `Ctrl+C` 时，前台命令以退出码 130 结束，并提示对应的 `stop` 命令。监督进程若已启动，会独立继续执行和计时。即使前台还没打印会话 ID，也可以直接用原场景名和目标执行 `stop`。

```bash
# start cpu-low "$pid" 被中断后，使用相同的场景和 PID
python3 -B ./hcu-fault-inject stop cpu-low "$pid" --json
```

`stop` 可以取消尚未开始的会话。如果注入仍在启动，`stop` 会等待启动步骤释放会话锁，再撤销注入。若中断的是 `stop`，用原场景和目标重试。若显示 `restore_failed`，查看 `restore_error`。需要追踪会话时，可用 `status --json` 查找 `session_id`，再用 `status SESSION_ID --json` 查看记录。已有脚本也可使用 `apply 场景 目标` 和 `restore SESSION_ID`。

## 可选参数

- `--for 15s` 设置本次持续时间。`--json` 输出结构化结果。
- `--value` 覆盖功率上限或 cgroup 限额。功率单位为瓦。CPU 配额用整数百分比，100 表示一个逻辑 CPU。内存支持字节及 `KiB`、`MiB`、`GiB` 后缀，进程额度用整数。`tcp-delay` 使用毫秒，`tcp-rate` 使用 Mbit/s，`rdma-congest` 和 `rdma-burst` 使用 Gbit/s。
- `--cpus 0,1` 覆盖 CPU 核数或 NUMA 场景选择的 CPU。`--cpu 0` 指定同核争用的 CPU。`--mib` 可调整 NUMA 内存辅助进程的用量。
- `--state-dir` 可指定调用用户拥有、权限为 0700 的独立会话目录。注入、查询和恢复使用同一路径。
- cgroup v1 的内存回收仅在显式传入 `--v1-reclaim-via-limit` 和 `--value` 时使用硬上限方式。它适用于隔离的文件缓存负载，持续增加不可回收内存仍可能触发 OOM。

```bash
python3 -B ./hcu-fault-inject start cpu-low "$pid" --value 50 --for 15s --json
python3 -B ./hcu-fault-inject stop cpu-low "$pid"
```
