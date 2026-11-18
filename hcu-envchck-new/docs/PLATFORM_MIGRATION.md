# platform 迁移范围

公共入口 `hcu-cluster-run <scenario> platform` 支持 shared-conda、node-local-conda、per-node-container，按 hostfile 逐节点执行，每节点一个执行目标，不按 `--group-size` 分组，也不做空闲门禁。控制端生成批次节点清单，由 clush 按 `--concurrency` 并发采集。

| 原工程 | 新工程 | 保留职责 |
| --- | --- | --- |
| hcu_envcheck/pod_probe.py 的 main(("platform",)) | payloads/platform_probe.py | 全部平台采集：系统/CPU/内存和cgroup、驱动及设备节点、DTK/工具、Python/Torch、动态库、网卡/RDMA及原始命令证据 |
| environment.py | environment.py | 平台健康规则、静态字段投影、可见设备及软件/网络摘要 |
| rdma.py、roce_health.py、ib_counters.py | 同名模块 | RDMA用户态、协议、IB端点及计数器、RoCE链路/拓扑/QoS完整规则 |
| reporting/consistency.py | reporting/consistency.py | 静态配置比较，状态、硬件与软件信息分别归并，列出差异和缺失，未知不能视为一致 |
| reporting/parsers.py 的 PLATFORM_PROFILE | reporting/platform_report.py | 原公共入口固定默认策略和规则结果映射 |

上述核心文件保留原平台采集和规则职责，并扩展硬件规格、软件版本及集群总览。已移除未调用的资源采样分支、兼容包装和模型，平台入口不接受旧 categories 参数。规则模型 Finding 位于 platform_types.py，不引入旧控制器或旧调度模型。原版权和SPDX保留。

原公共入口的默认策略保持：required_python_packages=(torch,)，不指定设备数；compiler/RDMA/RCCL/UCX为可选；minimum_rdma_devices=0，RDMA协议auto，未指定额外站点RDMA策略。缺失可选证据按原规则显示WARN/UNKNOWN；缺少必选Torch等按原规则FAIL。未增加自动安装、版本修复或网络配置动作，也未将旧内部策略参数自动开放为公共CLI。

平台采集在选定目标环境中进行，由 `entry.sh node` 调用 `node_run.sh`。Conda检查先source显式环境脚本，再使用目标Python；容器通过docker exec运行同一载荷，须能以相同绝对路径看到宿主远端任务目录。容器只描述容器可见系统与设备；仅在Docker证实host网络且privileged时确认网络宿主范围，否则使用原规则的NETWORK_HOST_SCOPE_UNVERIFIED。普通用户无法读取的证据保留命令失败和缺失，不提权采集。

硬件规格从rocminfo读取每张卡的型号、架构、计算单元和显存；型号保留原始值，同一卡的多个GLOBAL内存池取最大容量，不重复累加。软件信息包含驱动、DTK、Torch等版本，以及Transformer Engine、DeepEP的安装包版本元数据；未检出版本不等于未安装，版本信息也不代替包导入或通信测试。

`dispatch.py` 批量分发、执行并回收节点结果，保存阶段日志与执行事实；本次执行和结果回收结束后，`controller.py` 写入 `.state/execution.closed`，再调用报告。报告并发解析原始JSON并应用健康规则和一致性算法；日志中的token用于标识与核对本次采集。远端任务目录默认保留，便于复查采集脚本、配置和原始日志，使用完毕后由用户按需清理。

`execution.tsv` 保留采集执行事实及原始退出码；`results.tsv`、`results.json`、`summary.md` 展示解析后的健康结论。采集退出0仅表示采集完成，健康规则仍可判为FAIL或WARN；单项证据未知时保留UNKNOWN，不补造通过结果。

终端和Markdown报告均展示三类总览：状态检查以节点集合为行、检查项为列，合并完整状态组合相同的节点，列出通过、提示、失败、未知及不适用状态；硬件信息按相同规格合并；软件信息按相同版本合并。各组均显示节点数和压缩后的节点集合，完整成员列表保存在JSON中。软件差异相对参考组在Markdown中加粗、终端中用 `≠` 标记；异常和未知原因另按相同原因汇总。

硬件信息和软件版本差异供用户判断，不因静态值不同就判为健康失败。MemTotal与 `/dev/shm` 容量不参与一致性分组，原值仍保留；信息缺失不能视为一致。健康规则与配置一致性分别报告，相同的缺陷也不能视为健康通过。

`platform-checks.tsv` 保留全部规则状态，`platform-findings.tsv` 保留问题码与原因，`platform-nodes.json` 保留节点摘要及证据，`platform-profile.json` 保存本次固定规则策略；`platform-consistency.json` 保存静态比较，`platform-overview.json` 保存三类分组总览，`platform-consistency.md` 保存总览与其他静态差异，并由公共报告合入 `summary.md`。

原工程的规则入口、缺失证据覆盖和站点策略限制仍适用，不声明已实现完整站点验收策略，也不接入resource。独立idle-check由宿主 `payloads/idle.sh run` 检测或按请求释放资源；主动测试的空闲门禁调用同脚本gate模式，platform不调用二者。平台命令采集标记输出截断；被截断的verbs证据不能当作完整通过。空字符串的RDMA数值证据按无效值处理，避免整份报告因索引异常中断。
