# 离线单元测试与调用链测试

本目录为离线单测与调用链测试，不修改业务代码。用例是否通过以实际运行日志为准；未测量行覆盖率或分支覆盖率。

## 目标与边界

覆盖参数解析、操作声明、节点文件、分组、逐批调度、回执收集、日志解析、状态/性能判定以及终端和报告生成。

- 函数单测：调用真实 Python 函数，外部命令和第三方扩展在边界处模拟。
- 离线调用链：`cli.main → operations → planning → controller → dispatch → reporting.engine → 专用报告`，传输替身只提供预设节点结果包；不把整条业务链模拟为成功。
- Shell 离线测试：在临时目录复制原脚本，执行真实脚本逻辑。Docker、SSH、MPI、目标 Python、hy-smi 等使用本目录的命令替身，只记录参数并输出固定证据。替身不执行收到的命令。
- 所有输出都在测试临时目录，结束后清理。不连接服务器、不使用真实容器或 GPU，不编译或运行第三方 GPU 测试。
- 安装包范围：验证原启动器的源码/安装目录布局、运行时选择、符号链接和安装标记；不构建、安装 `.run`/RPM，不代替真实安装包验收。

## 环境与运行入口

完整测试建议在 Linux、Python 3.12 和项目已有的 ClusterShell 依赖下运行。Shell 用例还使用 bash、GNU timeout 及常见 coreutils。无需额外安装 pytest、Torch、DeepEP 或 byte-flux。

当前调度代码使用 `tarfile.extractall(filter="data")`，完整回收用例应使用支持该接口的 Python（建议沿用工具私有 Python 3.12）；本目录没有修复旧 Python 兼容性问题。

在工程根目录运行：

```bash
python3 -B -m unittest discover -s tests -t . -v
```

单独运行一组：

```bash
python3 -B -m unittest tests.test_platform -v
python3 -B -m unittest tests.test_payloads -v
python3 -B -m unittest tests.test_controller -v
```

Windows 可运行可移植的 Python 用例；`test_launcher` 和 `test_payloads` 会明确跳过并说明需要 Linux。不能将跳过的脚本用例算作验证通过。

## 覆盖清单

| 需求/共用逻辑 | 测试文件 | 模块与场景 |
| --- | --- | --- |
| 1. 启动与目标环境 | `test_launcher.py`、`test_cli.py` | 原启动器；私有运行时/显式 Python 优先级；缺失解释器；符号链接；保留工作目录；clush 子入口；安装标记；所有已注册操作 dry-run；容器/两种 Conda 场景；参数范围、参数归属、目标路径、显式默认值 |
| 节点、分组与批次 | `test_planning.py` | BOM/注释/顺序；空表/重复/非法节点；单/双/四机；无完整组报错；尾组不进执行清单；组首节点批次；1000 节点、250 组、128/122 两批；容器批次无静默 64 限制；RCCL/DeepEP slots 差异 |
| 2. 容器四项操作 | `test_container.py`、`test_payloads.py` | 参数/阶段声明；删除重建确认；身份、时间、日志完整性；原创建/查询/重建/删除流程；端口占用；阶段失败阻断；容器身份变化拒绝删除；停止容器状态 WARN；SSH 回环 token 错误 |
| 3. platform | `test_platform.py`、`test_network.py` | rocminfo 型号/架构/CU/显存；CPU 过滤、显存池不重复累加、单位转换；硬件采集完整/部分/失败；TE/DeepEP 元数据和并存版本；设备/Torch 状态；完整采集区间；原始执行失败保留；内存差异忽略；版本差异只分信息组；正常/失败/未知节点矩阵；1000 节点合并与成员完整性；单张状态矩阵、信息列序；IB 计数器、RDMA 协议分类 |
| 4. 空闲检测 | `test_idle.py`、`test_payloads.py` | metrics/PID/envelope 解析；全零空闲；显存或利用率非零繁忙；无证据、命令失败、错误范围、损坏格式、重复卡判未知；宿主范围；逐节点 JSON/TSV/终端/Markdown |
| 5. 一键释放 | `test_release.py`、`test_payloads.py` | 初始/最终判定；同容器多 PID 只停一次；未占卡容器不停止；未知归属、PID 复用拒绝停止；停止失败、停止后查询失败、仍繁忙不能成功；不带 release 不停止；原始动作回执保留 |
| 6. RCCL | `test_rccl.py`、`test_payloads.py`、`test_dispatch.py` | 默认十项/选择顺序；真实 `np=节点×卡数` 命令；MPI 端口/slots/env 转发；首次 SSH stderr；执行失败/超时停组；低性能不提前停组；其他组/后续批次继续；四列带宽独立；尺寸覆盖、校验、结束、规模证据；基准布局/阈值；执行失败与 BELOW_BASELINE 区分 |
| 7. DeepEP | `test_deepep.py`、`test_dependencies.py`、`test_payloads.py` | 单机 2/4/8 卡；多机每节点 8 卡及专家数限制；混合支持/不支持项；全部不支持报错；单机与多机启动模型；每节点 GPU workers；依赖 API/缺失/OOM/正确性分类；lowl 全 rank 覆盖；最差带宽；低带宽不推断执行失败 |
| checkdeepep | `test_deepep.py`、`test_controller.py` | ACS/XDP/RDMA/ibv_devinfo 四段；缺段和退出码冲突；每节点检查完整性；真实操作声明/interpret/汇总调用链 |
| 8. byte-flux | `test_byte_flux.py`、`test_dependencies.py`、`test_payloads.py` | TP 跟随参数，不固定八卡；启动 worker 数；不接受自定义 tests 或多节点组；launch 与节点身份；rank/torch/flux 指标覆盖；耗时合法性；正确性执行失败保留；依赖构建来源/实际扩展/动态库映射 |
| 依赖与预检 | `test_dependencies.py`、`test_payloads.py` | 离线源码清单/导出；身份、提交、哈希、必需文件、路径越界；DeepEP/flux 实际模块指纹；构建清单缺失/来源不符/二进制不符；GPU 查询 HIP/Torch 替身；可见卡不足和运行时不可用 |
| 控制与传输 | `test_controller.py`、`test_dispatch.py`、`test_transport.py` | 全部十个已注册操作与两种 Conda RCCL 的真实离线调用链；逐批执行与回收顺序；组员失败不重组；回收缺失只重试缺失节点；损坏包；取消；报告异常保留证据；默认保留远端任务目录；clush 参数与无静默 64 上限；原始环境恢复；超时和信号退出码 |
| 公共报告 | `test_reporting.py` | 原始事实身份/字段/日志；缺失事实；收集哈希、token、退出码；重复/缺失标记；执行关闭屏障；PASS/WARN/FAIL 退出码；派生失败不覆盖原始执行事实；尾组单独列明且不计执行数；转义、原子写入、非有限指标 |

## 数据与局限

`fixtures/` 均为人工构造的精简样例，节点名、型号和版本仅供测试，不代表真实现场。1000 节点测试为本地数据规模验证。

用例存在不等于已验证通过。本目录不宣称 100% 函数、行或分支覆盖；真实驱动、sshd 可用性、集群连通性、第三方算法正确性和性能仍需真实环境验收。测试替身不会证明镜像内的依赖已经可用。
