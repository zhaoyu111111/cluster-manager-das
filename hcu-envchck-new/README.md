# hcu-envcheck

面向 HCU 集群的平台健康检测、Conda/容器通信测试与容器管理工具。本目录 `hcu-envchck-new` 为 Python 版工程，当前版本 **0.3.1**。

在控制节点执行一个命令，工具通过包内 clush 并发访问目标节点，在 Conda 环境或指定容器中执行检测，收集原始日志并生成报告。发行包自带独立 Python runtime，控制节点和目标宿主机无需安装系统 Python。

## 功能特性

三种执行场景共用分组、并发、测试载荷、空闲门禁和最终报告。

| 场景 | 执行环境 |
| --- | --- |
| `shared-conda` | 各节点加载共享存储中的 Conda 环境 |
| `node-local-conda` | 各节点加载各自本地 Conda 环境 |
| `per-node-container` | 各节点进入指定容器 |

| 操作 | 功能 | 执行范围 |
| --- | --- | --- |
| `container-status` | 查询指定容器状态，核对镜像身份 | hostfile 中各节点 |
| `container-create` | 创建容器、复制 SSH 材料并验证本机 SSH 回环 | hostfile 中各节点 |
| `container-recreate` | 删除旧容器并按本次参数重新创建，需要 `--yes` | hostfile 中各节点 |
| `container-delete` | 按已确认的完整 ID 删除指定容器，需要 `--yes` | hostfile 中各节点 |
| `platform` | 系统/驱动/DTK、软件包与网络健康检测，节点配置一致性 | hostfile 中各节点 |
| `idle-check` | 宿主机全部卡空闲检测、占用容器归属；`--release` 显式停止并复检 | hostfile 中各节点 |
| `checkdeepep` | 整体检查 ACS、XDP、RDMA 端口和 ibv_devinfo | hostfile 中各节点 |
| `rccl` | 十项 collective 测试，对比所选性能基准 | 按 `--group-size` 分组 |
| `deepep` | 单机 `intranode`、多机 `norm` / `lowl` | 按 `--group-size` 分组 |
| `byte-flux` | FP16、TPN 的 AG 用例，检查全部 rank 输出 | 每节点独立 N 卡 |

`resource`、`ib-write-bw` 尚未接入；RCCL、DeepEP、byte-flux 共用宿主机空闲预检，可用 `--skip-idle-check` 跳过。

## 环境要求

- **控制节点**：兼容的 Linux、Bash、SSH。安装包方式自带私有 Python 与 ClusterShell（clush 并发）；源码方式需 Python 3.10+ 并已安装 ClusterShell。
- **目标宿主机**：可免密 SSH 登录；空闲检测需已有可用的 hy-smi；仅容器场景需要当前账号已有 Docker 操作权限。工具不调用 sudo，不修改宿主用户组、驱动或服务。
- **目标 Conda/容器环境**：已有相应测试依赖；多节点测试需目标环境间免密 SSH、兼容的 MPI 和相同登录用户名。

## 快速开始

### 使用安装包

```bash
sha256sum -c hcu-envcheck-0.3.1-linux-x86_64.run.sha256
chmod +x hcu-envcheck-0.3.1-linux-x86_64.run
./hcu-envcheck-0.3.1-linux-x86_64.run
export PATH="$HOME/.local/bin:$PATH"
TOOL_ROOT="$HOME/.local/share/hcu-envcheck/current"
hcu-cluster-run --version
```

默认安装到 `~/.local/share/hcu-envcheck`；升级、卸载及 `--prefix` 等选项见[用户手册](docs/USER_GUIDE.md)。

### 使用源码

```bash
git submodule update --init --recursive -- \
  hcu-envchck-new/third_party/DeepEP hcu-envchck-new/third_party/flux
cd hcu-envchck-new
TOOL_ROOT="$PWD"
export PATH="$PWD/bin:$PATH"
export HCU_CONTROL_PYTHON=/path/to/python3   # Python 3.10+，需已安装 ClusterShell
hcu-cluster-run --version
```

### 示例

```bash
# 容器场景 RCCL 双机测试；其余操作与参数见用户手册。
hcu-cluster-run per-node-container rccl -f ./hostfile \
  --container hcu-test -i YOUR_IMAGE:TAG --env-script /target/env.sh \
  --group-size 2 --gpus-per-node 4 \
  --baseline-file "$TOOL_ROOT/config/rccl_baseline.conf" \
  --baseline-profile supernode_bw1300_40_2x4
```

容器管理、platform、空闲检测与释放、RCCL/DeepEP/byte-flux 全部命令、三种场景的参数差异及报告解读见[用户使用手册](docs/USER_GUIDE.md)。

## 构建发行包

在与目标系统兼容的 Linux 上构建：初始化 `third_party/DeepEP`、`third_party/flux` 子模块，并准备完整独立的 Python 3.10+ 安装前缀（不能用 venv）：

```bash
bash build/build_package.sh \
  --runtime-src /path/to/full-python-prefix \
  --format run --output-dir "$PWD/dist/run"
```

交付自解压安装包及 SHA256 校验文件；`--format tar.gz|rpm`、`--install-prefix` 等全部参数见 `build/build_package.sh` 脚本内说明。不支持 Windows 或跨架构构建。

## 文档

- [用户使用手册](docs/USER_GUIDE.md)：启动方式、全部操作命令、三种场景参数差异、报告解读。
- [架构说明](docs/ARCHITECTURE.md)：模块职责、执行接口与数据流。
- [平台迁移说明](docs/PLATFORM_MIGRATION.md)：platform 规则来源与默认策略。

## 许可证

本工程遵循 [Apache License 2.0](../LICENSE)。`third_party/` 子模块（DeepEP、flux）为第三方测试源码，遵循其自身许可声明。
