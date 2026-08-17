# 多节点测试框架设计与使用指南

## 1. 文档目的

本文说明仓库中多节点测试框架的职责、运行架构、可执行中间态、裸机验证方式，以及下一阶段
Recipe YAML 到中间态转换器的设计计划。

框架当前解决的是“可靠执行一个已经展开完成的多节点计划”，而不是解析任意 Recipe YAML。
当前目录中的两个 config 是测试 fixture，用于验证 Runtime 能力；它们不是未来 Recipe schema 的
样例，也不应被当作 Recipe 格式约束。

核心边界如下：

```text
Recipe YAML（格式待确定）
        │
        │  future converter：理解语义、补默认值、展开拓扑、完整校验
        ▼
可执行中间态（当前 plan.yaml + plan-local scripts）
        │
        │  current runtime：按声明执行，不猜测业务拓扑
        ▼
多节点服务、Gateway、检查、AISBench、结果与日志
```

一句话原则：**Converter 负责理解，Intermediate 负责显式表达，Runtime 负责可靠执行。**

## 2. 代码与目录

```text
test/recipe/multi_node/
├── scripts/                         # 多节点 Runtime 与基础设施适配器
│   ├── plan.py                      # 读取可信可执行中间态和运行时 hosts
│   ├── runner.py                    # 单节点生命周期与 leader-only stages
│   ├── coordinator.py               # 节点间 ready/stop/outcome 控制面
│   ├── process.py                   # 进程组、信号、超时和清理
│   ├── result.py                    # 不可变结果协议与原子 JSON 写入
│   ├── aisbench.py                  # AISBench 配置渲染、执行与结果转换
│   ├── install_aisbench.sh          # 可复用的固定版本 AISBench cache
│   ├── run_online_dp.py             # external-DP launcher 退出码适配
│   ├── run.sh                       # 裸机、容器、LWS 共用入口
│   └── k8s/
│       ├── lws.yaml.tmpl            # LeaderWorkerSet 模板
│       ├── render_lws.py             # 严格字符串渲染与 YAML 结构检查
│       └── run_lws.sh               # LWS 环境到通用 run.sh 的适配
└── configs/                         # 已展开的可执行 fixture
    ├── deepseek-v2-lite-pd-2n2c/
    └── qwen3-30b-a3b-dp-2n2c/

test/ut/multi_node_framework/        # 不依赖 NPU 的 Runtime 单元测试
.github/workflows/verify_multi_node.yaml
.github/workflows/_verify_multi_node.yaml
```

仓库还存在 `scripts/multinode/` 和 `multinode-recipe-verify.yml`。它们属于已有的 Recipe
解析/验证流水线，不是本文描述的新 Runtime。由于新 converter 尚未完成，这套已有能力当前仍被
workflow 和前端代码使用，不能作为“无引用旧代码”删除。

## 3. 总体架构

### 3.1 一个 Pod/节点只有一个 Runner

每个逻辑节点执行同一个 `run.sh`，再启动一个 `runner.py`。Runner 不等于一个 vLLM rank：

```text
物理节点或 LWS Pod
└── run.sh
    └── runner.py
        └── node.launch 声明的 launcher process group
            ├── 单个 vllm serve（internal DP 常见形式）
            └── 多个 vllm serve（external DP launcher 常见形式）
```

Runtime 只管理 node launcher 及其整个进程组。DP/TP、P/D、rank 数量、设备映射、KV Connector
和 vLLM 参数均由可执行中间态中的脚本明确给出，Runner 不重新推导。

### 3.2 控制面与数据面分离

控制面由 leader（固定为 `plan.nodes[0]`）上的 HTTP Coordinator 提供，只传输：

- 每个节点是否 ready；
- 第一个全局 stop signal；
- 每个节点清理后的 `NodeOutcome`；
- leader 聚合后的 `RunOutcome`。

Coordinator 不传输模型请求、KV 数据或日志。模型服务、HCCL/Gloo、P/D Connector 和 Gateway
属于数据面，按 plan-local scripts 的参数直接通信。

### 3.3 Leader 与 Worker 分工

所有节点都执行：

1. 解析同一份 plan 和按节点生成的 hosts；
2. 建立或等待 Coordinator；
3. 启动本节点 service launcher；
4. 检查本地 readiness；
5. 上报 ready；
6. 收到 stop 后清理本节点进程组；
7. 生成并上报不可变 `NodeOutcome`。

只有 leader 执行：

1. 等待全部节点 ready；
2. 启动可选 Gateway 并检查健康状态；
3. 按 plan 顺序运行所有 stages/steps；
4. 发布完成、失败或取消 stop signal；
5. 收集节点 outcome，生成全局 `result.json`。

Worker 不会重复启动 Gateway，也不会重复执行 completion、accuracy 或 performance。

### 3.4 生命周期与失败收敛

```text
load plan/hosts
  -> start/wait coordinator
  -> start local launcher
  -> local readiness
  -> all nodes ready
  -> optional gateway readiness
  -> ordered stages
  -> first stop signal
  -> TERM process groups
  -> bounded wait
  -> KILL remaining groups
  -> write/report NodeOutcome
  -> leader writes RunOutcome
```

StopSignal 是提前通知，不是最终结果。最终结果必须等待清理完成后的事实：例如评测通过但进程组
无法清理，节点和全局结果仍应失败。观察到其他节点失败的节点记为 `aborted`，不能被错误归因为
primary failure。

所有 service、gateway 和 step 都以独立 session/process group 启动。清理不使用 `pkill` 或
`killall`，避免影响同机其他任务。

## 4. 可执行中间态

### 4.1 Config bundle 结构

一个 config 是可以直接执行的完整 bundle：

```text
configs/<case>/
├── plan.yaml
├── nodes/
│   ├── node0/run.sh
│   └── nodeN/run.sh
├── gateway/run.sh                   # 可选
├── checks/                          # 通用服务检查脚本
├── evaluations/                     # AISBench 等评测适配脚本
└── README.md                        # fixture 的资源和运行说明
```

external DP 可以在节点目录中额外包含 `run_dp_template.sh`。这些文件属于中间态生成物，可以显式
重复，不要求人工抽象复用；确定性和可审阅性优先于减少少量重复代码。

### 4.2 `plan.yaml` 顶层内容

当前协议版本为 `multi-node/v1`，kind 为 `MultiNodePlan`。Runtime 消费以下信息：

| 区域 | 必要内容 | Runtime 用途 |
| --- | --- | --- |
| `metadata` | 唯一、稳定的 plan 名称 | artifact 根目录和结果标识 |
| `model` | 模型 ID、cache 相对路径、served name | 计算模型路径和请求模型名 |
| `resources` | 每节点 NPU 数量 | LWS resource request/limit |
| `nodes[]` | id、role、launch、可选 readiness | 节点顺序、启动脚本和健康检查 |
| `gateway` | 可选 launch、port、health path | leader 侧统一入口 |
| `stages[]` | id、failure category、steps | leader-only 有序验证流程 |

`nodes[0]` 是控制 leader。节点数组顺序也是 hosts、LWS worker index 和
`MULTI_NODE_NODE_<index>_IP` 的顺序，因此 converter 必须输出稳定顺序。

### 4.3 Node 与 Readiness

每个 node 必须有唯一 id、描述性 role 和相对 config 目录的 launch 脚本。readiness 可声明：

- `port_start`：第一个 HTTP 服务端口；
- `count`：连续端口数量，默认 1；
- `health_path`：健康检查路径，默认 `/health`。

没有 HTTP 服务的 headless 节点可以省略 readiness。此时 launcher 在启动阶段持续存活即表示本地
启动成功，全局连接是否完成通常由 API 节点的 readiness 或后续检查确认。

### 4.4 Gateway

Gateway 是 leader 上的普通受管进程，不是 Runtime 内置的 P/D 概念。plan 只声明启动脚本、端口
和健康路径。Gateway 存在时，stages 使用 Gateway endpoint；否则使用 leader readiness 的第一个
端口。

### 4.5 Stage 与 Step

Stage 按数组顺序执行，step 在 stage 内也按数组顺序执行。每个 step 声明：

- 唯一 id；
- 相对 config 目录的可执行脚本；
- 单 step 超时；
- 任意 JSON-compatible `inputs`。

Runner 将 inputs 原样写入 `MULTI_NODE_STEP_INPUT_FILE`，并提供：

- `MULTI_NODE_STEP_ARTIFACT_DIR`；
- `MULTI_NODE_STEP_RESULT_FILE`；
- 当前模型、endpoint、节点和 artifact 的通用环境变量。

Step 退出前必须写 JSON object，至少包含 `{"status": "passed"}`。Runtime 不解析 AISBench
私有格式；具体适配器负责把工具输出翻译成公共结果。

### 4.6 当前两个 fixture 覆盖的能力

- `deepseek-v2-lite-pd-2n2c`：两个节点、每节点两个 external-DP rank、P/D Gateway。
- `qwen3-30b-a3b-dp-2n2c`：vLLM internal DP，API 节点加 headless 节点，无 Gateway。

它们用于覆盖两类 Runtime 启动方式，不代表未来 Recipe 必须出现这些字段或拓扑。

## 5. 本地或裸机验证

### 5.1 前置条件

- 每台机器使用相同仓库版本和运行镜像；
- 模型已存在于 `/root/.cache/modelscope/hub/models/<model.cache_path>`；
- 节点间端口、HCCL/Gloo 网卡和所需服务端口互通；
- vLLM 和 vLLM Ascend 已安装；
- 所有节点看到相同 plan，但各自设置不同 node index；
- config 包含 AISBench stages 时，先准备 AISBench 环境。

### 5.2 只验证中间态可读取

该模式不启动服务、不访问 NPU，也不需要 hosts：

```bash
MULTI_NODE_PLAN=test/recipe/multi_node/configs/<case>/plan.yaml \
MULTI_NODE_VALIDATE_ONLY=true \
test/recipe/multi_node/scripts/run.sh
```

它只用于快速检查 plan 解码和打印拓扑，不替代未来 converter validator。

### 5.3 必需环境变量

真实执行每个节点至少需要：

| 环境变量 | 含义 |
| --- | --- |
| `MULTI_NODE_PLAN` | 相对仓库根目录或绝对 plan 路径 |
| `MULTI_NODE_CLUSTER_IPS` | 按 `plan.nodes` 顺序排列、逗号分隔的节点地址 |
| `MULTI_NODE_NODE_INDEX` | 当前节点在 `plan.nodes` 中的零基索引 |
| `ASCEND_RT_VISIBLE_DEVICES` | 当前节点分配的物理 NPU；也可显式使用 `MULTI_NODE_VISIBLE_DEVICES` |

强烈建议显式设置：

| 环境变量 | 含义 |
| --- | --- |
| `MULTI_NODE_INTERFACE` | 当前节点用于 HCCL/Gloo 的网卡名 |

如果未设置 interface，Runner 会根据当前节点 IP 反查本机网卡；无法唯一找到时直接失败，不静默
选择其他网卡。

常用可选项：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `MULTI_NODE_CONTROL_PORT` | `29599` | Coordinator 端口 |
| `MULTI_NODE_STARTUP_TIMEOUT_SECONDS` | `1800` | 整体启动期限 |
| `MULTI_NODE_RUN_TIMEOUT_SECONDS` | `7200` | leader stages 总执行期限 |
| `MULTI_NODE_PROGRESS_INTERVAL_SECONDS` | `30` | startup/stage 周期心跳间隔；`0` 关闭周期心跳 |
| `MULTI_NODE_ARTIFACT_ROOT` | `/tmp/multi-node` | 结果与日志根目录 |
| `MULTI_NODE_PLOG_ROOT` | 未设置 | 设置后退出时复制 `/root/ascend/log` |
| `VLLM_ASCEND_ROOT` | `/vllm-workspace/vllm-ascend` | 上游 launcher/proxy 源码根目录 |

### 5.4 准备 AISBench

本地 `run.sh` 不隐式联网安装 AISBench。包含评测 stages 时先执行一次：

```bash
AIS_BENCH_ENVIRONMENT_IDENTITY='runtime=<image identity>' \
test/recipe/multi_node/scripts/install_aisbench.sh \
  --env-file /tmp/multi-node-aisbench.env

source /tmp/multi-node-aisbench.env
export MULTI_NODE_AISBENCH_BIN
export MULTI_NODE_AISBENCH_CACHE_KEY
export MULTI_NODE_AISBENCH_SOURCE
```

Installer 默认安装固定版本 `ais_bench_benchmark==3.1.20260630`。cache key 包含包版本、
运行环境 identity、Python 主次版本、CPU 架构和 constraints 摘要。冷 cache 通过临时目录构建
并原子发布，只有命令和 datasets、vLLM API 模板都校验通过才会复用。Kubernetes/LWS 通过
`PIP_INDEX_URL` 和 `PIP_TRUSTED_HOST` 使用集群内部 PyPI；本地执行则使用调用者已有的 pip
配置。需要验证其他版本时可显式设置 `AIS_BENCH_PACKAGE_VERSION`，不应在正式 CI 中使用浮动
版本。pip 连接默认重试 3 次，可通过 `AIS_BENCH_PIP_RETRIES` 调整。

### 5.5 两节点启动示例

两台机器的公共配置：

```bash
export MULTI_NODE_PLAN=test/recipe/multi_node/configs/<case>/plan.yaml
export MULTI_NODE_CLUSTER_IPS='<node0_ip>,<node1_ip>'
export MULTI_NODE_INTERFACE='<local_interface>'
export ASCEND_RT_VISIBLE_DEVICES=4,5
```

节点 0：

```bash
export MULTI_NODE_NODE_INDEX=0
test/recipe/multi_node/scripts/run.sh
```

节点 1：

```bash
export MULTI_NODE_NODE_INDEX=1
test/recipe/multi_node/scripts/run.sh
```

启动顺序没有要求。node0 会启动 Coordinator；其他节点在共享 startup deadline 内等待它可访问。

### 5.6 `run.sh` 内部流程

1. 定位仓库根目录并加载 Ascend/ATB 环境；
2. 读取 plan，核对可选的 `MULTI_NODE_NODE_COUNT`；
3. 将 cluster IP 和当前 interface 写入节点私有 hosts YAML；
4. 从显式变量或 Ascend visible devices 生成统一设备列表；
5. 启动 `runner.py`，安装 TERM/INT 转发；
6. Runner 建立控制面并启动本地 node launcher；
7. readiness 完成后，leader 等待全组，worker 等待 stop；
8. leader 启动可选 Gateway 并执行 stages；
9. 任一节点失败或 leader 完成时发布 stop；
10. 所有节点清理进程组、写 node result；leader 再写全局 result；
11. `run.sh` 退出 trap 可选复制 Ascend plog。

Startup 是共享总期限，不会为 Coordinator、service、all-ready 和 Gateway 分别重新计算完整超时。
Stages 也共享 run deadline；单 step 实际超时是自身声明值与剩余总期限的较小值。

### 5.7 输出结构

```text
<artifact-root>/<plan-name>/
├── node0/
│   ├── service.log                  # node launcher 输出
│   ├── servers/rank-0.log           # external DP 可按 rank 拆分
│   ├── gateway.log                  # 可选
│   ├── <stage>/<step>.log
│   ├── <stage>/<step>/input.json
│   ├── <stage>/<step>/result.json
│   └── node-result.json
├── node1/
└── result.json                      # 仅 leader
```

internal DP 通常由一个 `vllm serve` 管理本节点内部 ranks，因此服务输出集中在
`service.log`。external DP 会启动多个独立 server，fixture 将它们拆到
`servers/rank-<rank>.log`，避免日志交错。

Runner 不把服务或 stage 的原始日志重复灌入控制台。每个节点在本地轮询自己的 HTTP rank
endpoints：状态变化时立即打印，未 Ready 时按 `MULTI_NODE_PROGRESS_INTERVAL_SECONDS` 打印
心跳；没有独立 endpoint 的 internal-DP headless 节点只报告 launcher 存活。Leader 额外输出
`cluster nodes ready=<ready>/<total>`，但不代替各节点执行跨节点 rank 探测。

Stage（包括 AISBench）原始 stdout/stderr 始终完整写入 `<stage>/<step>.log`。GitHub 实时日志只
打印 step 开始、周期运行心跳、退出状态，以及验证后的 `result.json` 单行摘要；摘要最多 4096
字符。成功和失败使用同一策略，不在失败分支额外输出完整日志。

## 6. Kubernetes / LWS 执行

Reusable workflow 从 plan 读取 node count 和每节点 NPU 数，渲染一个 LeaderWorkerSet。LWS
worker index 被 `run_lws.sh` 转成 `MULTI_NODE_NODE_INDEX`，成员 DNS 被解析成有序 cluster IP，
随后仍进入同一个 `run.sh`。

所有 Pod 在 15 分钟总期限内并行等待创建和 Ready。Pod 内 AISBench 准备、DNS、Coordinator、
service、全组 ready 和 Gateway 共享 30 分钟 startup deadline；stages 共享 120 分钟 run
deadline。LWS 从集群内部 PyPI 安装固定版本 AISBench；node0 安装失败时会原子发布失败状态，
其他节点停止等待并立即退出。

Workflow 已持续执行 `kubectl logs -f` 并为每个 Pod 添加 `[node<index>]` 前缀，因此上述本地
rank、集群 barrier、Gateway 和 stage 心跳会直接显示在 GitHub Actions，无需从 PVC 反向 tail
日志文件。

Actions 始终打印精简的 LWS、Pod 和 Event reason。完整 Kubernetes YAML 上传能力由
`MULTI_NODE_UPLOAD_K8S_DIAGNOSTICS` 控制，默认 `"false"`；启用后成功和失败仍采用同一采集
策略。完整对象可能包含节点、IP、镜像、volume 和环境变量，只应在确认 artifact/OBS 权限后
打开。

## 7. 下一阶段：Recipe 到中间态 Converter

### 7.1 不提前绑定 Recipe YAML 形状

Recipe 最终字段名、层级和复用机制尚未确定。Converter 的第一步不是直接访问散落的 YAML key，
而是定义一个与文本格式解耦的规范化语义模型。未来 Recipe schema 可以演进，只要 reader 能把它
转换成该模型，后续 topology planner、validator 和 emitter 不需要同步重写。

建议数据流：

```text
Recipe document
  -> syntax/schema reader
  -> normalized semantic model
  -> defaults and reference resolution
  -> topology expansion
  -> resource/port allocation
  -> semantic validation
  -> executable plan + scripts
  -> emitted-bundle validation
```

### 7.2 Recipe 至少需要表达的语义

以下是语义要求，不预设未来 YAML key 或嵌套方式：

1. **身份与版本**：Recipe identity、格式版本、目标运行栈或兼容范围。
2. **模型**：模型标识、served name、权重来源/cache identity、必要的模型能力约束。
3. **部署意图**：单体、internal DP、external DP、P/D 等部署模式，以及用户确实想表达的逻辑规模。
4. **资源预算**：节点数、每节点 NPU/CPU/内存需求，或足以让 planner 唯一计算这些值的信息。
5. **并行与角色语义**：DP/TP/PP、P/D 角色、rank 分布、headless/API、expert parallel 等必要约束。
6. **服务入口**：是否需要 Gateway、暴露协议、健康检查语义；端口可以由用户声明或 allocator 分配。
7. **运行配置**：vLLM 参数、环境变量、量化、KV Connector 等不能由 Runtime 猜测的业务配置。
8. **验证意图**：需要哪些服务检查、准确率/性能评测、数据集 identity、样本限制、baseline/tolerance。
9. **依赖与安全约束**：运行镜像能力、上游 launcher 依赖、是否允许远程代码、需要的共享 cache。

如果某项可以从标准默认值唯一推导，Recipe 可以省略；如果存在多种合法解释，schema 必须要求用户
明确选择，Converter 不应静默猜测。

### 7.3 Converter 代码职责规划

建议新增独立模块，而不是把转换逻辑放进 `runner.py`：

```text
test/recipe/multi_node/converter/
├── schema.py          # Recipe 输入 schema/version 分派
├── reader.py          # YAML -> normalized semantic model
├── model.py           # 与 YAML 布局无关的强类型语义对象
├── defaults.py        # 显式、可测试的默认值和引用解析
├── topology.py        # 角色、节点、rank 和服务实例展开
├── allocation.py      # 设备、端口、资源和 endpoint 规划
├── validation.py      # 跨字段和拓扑语义检查
├── emitter.py         # 确定性生成 plan/scripts/README metadata
└── cli.py             # convert / validate 命令入口
```

各阶段输入输出应为不可变或可比较的结构化对象，避免通过环境变量或临时文件在 converter 内部传递
状态。

### 7.4 必须实现的验证

Converter 输出前至少检查：

- schema/version、未知字段、字段类型、必填项；
- model identity、cache path 和 served name 的一致性；
- 节点 id、role、顺序和 leader 唯一性；
- DP/TP/rank 总量、本地 rank 范围、headless/API 组合；
- P/D 两侧规模、KV role、Connector 参数和 Gateway backend；
- 每节点 NPU 需求与 rank/TP 展开结果一致；
- 服务、RPC、Connector、Gateway 和 control port 不冲突且范围合法；
- readiness count 与实际启动 server 数一致；
- stage/step id 唯一，脚本路径不能逃出 bundle；
- dataset、request config、样本数、baseline 和 tolerance 合法；
- 生成脚本不包含未解析 placeholder；
- 禁止把凭证、kubeconfig、节点真实 IP 等运行时秘密写入中间态；
- 相同 Recipe 和 converter 版本生成字节稳定或语义稳定的 bundle。

Runtime 仍保留必要的防御性检查，例如文件能否读取、hosts 是否与 plan nodes 完全一致、进程是否
异常退出、HTTP 是否可达和结果 JSON 是否有效。但 Runtime 不应重复实现 Recipe 业务语义校验。

### 7.5 Emitter 输出要求

生成器必须输出完整、自包含、可审阅的 config bundle，并记录：

- source Recipe identity 和内容摘要；
- converter/schema 版本；
- 所有已应用默认值；
- 稳定 node/rank/port 分配；
- plan 和脚本；
- 不含运行时 IP、设备编号和凭证的 provenance metadata。

生成过程写入临时目录，全部验证通过后再原子替换目标 bundle，避免半生成目录被 CI 执行。

### 7.6 Converter 测试计划

Converter 测试不需要 NPU，至少包含：

1. reader/schema 正反例；
2. defaults 的显式覆盖和缺省行为；
3. internal/external DP、headless、P/D 等拓扑展开单测；
4. 资源和端口冲突负例；
5. placeholder/path containment/secret rejection；
6. golden bundle 测试，保证输出确定性；
7. `load_plan()` 回读所有生成 bundle；
8. 使用 fake launcher 跑 Runtime 的本地双节点生命周期集成测试；
9. 少量真实 NPU fixture 验证 converter 输出与 Runtime 接口，不把大模型结果写进 unit tests。

Golden 测试应比较规范化结构或经过明确格式化的文本，避免无意义的 YAML key 顺序导致脆弱测试。

### 7.7 建议实施顺序

1. 确定 Recipe semantic contract 和版本策略；
2. 实现 reader + normalized model，只做解析和错误定位；
3. 实现 defaults/reference resolution；
4. 实现 topology/resource/port planner；
5. 实现完整 validator；
6. 实现确定性 emitter 和 provenance；
7. 将当前手工 fixture 变成 converter golden output；
8. 在 CI 中先运行 converter validate/golden，再运行 Runtime unit tests；
9. 最后让 LWS workflow 消费 converter 输出，而不是手工维护的中间态。

在第 7 步之前，不应为了迁就当前两个 fixture 而提前固化 Recipe YAML 结构。

## 8. 维护约束

- Runtime core 不导入 Kubernetes SDK，也不读取 GitHub Actions context。
- Infrastructure adapter 不理解 P/D、DP/TP 或 AISBench 业务指标。
- Plan-local scripts 不重新解析 Recipe。
- Coordinator 不传输日志或模型数据。
- 成功和失败使用相同日志采集策略，不增加失败专用分支。
- 新增业务函数必须有与复杂度匹配的英文 docstring；关键 deadline、进程组和协议边界应有英文注释。
- Unit tests 验证可观察契约，不为保留旧实现而反向要求业务代码存在。
- 未完成的设计讨论放在 issue/PR，不把过程性说明长期留在 Runtime 业务代码中。
