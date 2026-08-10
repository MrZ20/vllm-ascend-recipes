# vLLM Ascend Recipes Multi-Node CI：Agent 执行任务说明

> 适用范围：`vllm-ascend/vllm-ascend-recipes` PR #34 及后续同方向重构  
> PR: https://github.com/vllm-ascend/vllm-ascend-recipes/pull/34  
> 上游参考仓库: https://github.com/vllm-project/vllm-ascend  
>
> 本文用于直接交给 coding agent 执行。  
> 当前阶段只处理 **“最终中间态 -> 测试执行”**。  
> **“Recipe YAML -> 最终中间态”只记录任务，不在本阶段实现。**

---

## 1. 背景与目标

当前 PR 引入了一套 multi-node Recipe CI，包括：

- 多节点 plan/intermediate representation；
- 节点启动；
- Leader/Worker 协调；
- readiness；
- gateway；
- checks；
- accuracy/performance evaluation；
- AISBench；
- LWS/Kubernetes；
- artifact/result；
- cleanup；
- vLLM Ascend 上游 example 复用。

当前代码处于 **中间态到测试执行链路的验证阶段**。

本轮重构目标不是增加更多功能，而是：

1. 收敛职责边界；
2. 删除不必要的防御性编程；
3. 修正运行状态和最终结果的一致性问题；
4. 降低与 vLLM Ascend 上游实现的重复维护；
5. 保持生成后中间态的可读性和可直接执行性；
6. 为后续 `Recipe YAML -> intermediate bundle` 留下清晰接口。

---

# 2. 本阶段必须遵守的前提

这些前提属于本任务的设计约束，Agent **不要重新讨论，也不要为了兼容旧逻辑破坏这些约束**。

## 2.1 Intermediate 是最终测试输入

当前阶段假设：

```text
Recipe YAML
    ↓
[未来 converter]
    ↓
Validated Executable Intermediate Bundle
    ↓
Runtime Executor
```

当前工作从：

```text
Validated Executable Intermediate Bundle
```

开始。

也就是说：

- 输入已经完成格式校验；
- 输入已经完成静态结构校验；
- 输入已经完成业务语义校验；
- 不需要兼容旧版本 intermediate；
- 不需要容忍不完整 intermediate；
- 不需要在 runtime 再实现一遍 schema validator；
- intermediate 后续由 converter 生成；
- generated intermediate **不会作为手工长期维护源码保存到仓库**。

---

## 2.2 Generated intermediate 中允许重复

不要为了 DRY 强行抽象 generated bundle。

例如下面这种重复是合理的：

```text
nodes/node0/run.sh
nodes/node0/run_dp_template.sh

nodes/node1/run.sh
nodes/node1/run_dp_template.sh
```

即使两者只有少数参数不同，也可以保留。

理由：

1. 每个节点最终执行内容直观可见；
2. 调试时不用跨多层模板推导参数；
3. generated artifact 不需要承担手工维护成本；
4. 后续 converter 可以保证生成一致性；
5. 节点级显式配置比 runtime inference 更容易审查。

**规则：**

> Generated artifact 中的重复不是当前需要解决的问题。  
> Framework/core source 中的重复才需要重点控制。

---

# 3. 核心架构原则

Agent 修改代码时必须维护以下 invariant。

---

## 3.1 Runtime Executor 不理解业务拓扑语义

Runtime core 不应该理解：

- Prefill；
- Decode；
- PD；
- DP；
- TP；
- EP；
- Mooncake；
- LMCache；
- GSM8K；
- AISBench 业务配置；
- 某种模型特例；
- 某种 KV Connector 组合。

Runtime core 只负责：

```text
启动进程
等待 readiness
节点状态协调
顺序执行 stage
监督进程
处理 timeout / signal
cleanup
收集 outcome
写结果
```

例如：

```yaml
role: prefill
```

最多用于：

- 人类可读描述；
- 环境变量；
- 日志；

**禁止 Runner 根据 role 自动推导 gateway backend、rank、DP topology 等。**

---

## 3.2 优先调用 vLLM Ascend 上游实现

如果能力属于：

```text
vLLM / vLLM Ascend runtime
```

原则上不要复制到 recipes 仓库。

优先级：

```text
1. 直接使用容器中 vllm-ascend 的实现
2. 必要时写极薄 adapter
3. 如果 adapter 是修复上游缺陷，应优先 upstream fix
4. 不要长期维护 vendored copy
```

当前合理案例：

```text
/vllm-workspace/vllm-ascend/examples/external_online_dp/launch_online_dp.py

/vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

这类实现应直接消费。

---

# 4. 本阶段目标架构

目标结构建议如下：

```text
scripts/recipe_ci/
├── core/
│   ├── runner.py
│   ├── coordinator.py
│   ├── process.py
│   └── result.py
│
├── adapters/
│   ├── aisbench.py
│   └── k8s_lws.py
│
└── run.sh
```

Generated intermediate：

```text
<generated-plan>/
├── manifest / plan
├── nodes/
│   ├── node0/
│   ├── node1/
│   └── ...
├── gateway/
├── checks/
└── evaluations/
```

不要求本轮一定完成目录物理迁移。

优先保证 **职责边界**。

---

# 5. P0：必须优先完成的 correctness 修复

---

## P0-1. 修复“远端失败被错误标记为本节点失败”

### 当前问题

Follower 当前大致逻辑：

```python
state = client.wait_terminal(...)
if state["status"] != "passed":
    raise StageFailure(...)
```

后续统一 failure handler 又会：

```python
client.mark_failed(node.id, ...)
```

因此：

```text
node0 evaluation failed
        ↓
global failed
        ↓
node1 观察到 global failed
        ↓
node1 把它转换成自己的 node_failed
        ↓
coordinator 记录 node1 failed
```

最终会把：

```text
node0 真失败
node1 正常运行后被全局终止
```

错误记录成：

```text
node0 failed
node1 failed
```

### 要求

区分至少三种概念：

```text
LocalFailure
ObservedRemoteFailure / GlobalTerminal
Cancellation
```

Follower 观察到全局失败时：

- 结束等待；
- 停止本地 service；
- cleanup；
- 写自己的 node outcome；
- **不得调用 `mark_failed(node_id)`**。

### 建议状态

允许类似：

```text
passed
failed
cancelled
aborted
```

或者：

```text
execution_status=passed
termination_reason=global_failure
```

具体 schema 可自行设计，但必须明确：

> 本节点自身失败 与 因其他节点失败导致本节点终止 不是一回事。

### 验收

增加测试：

```text
node0 reports failure
node1 observes global failure
node1 must not be included in coordinator.failures
```

---

## P0-2. Final result 必须在各节点 cleanup 后统一收敛

### 当前问题

目前 global terminal 在 cleanup 前发布：

```text
execution finish
    ↓
publish passed / failed
    ↓
cleanup
```

因此可能出现：

```text
global status = passed
worker cleanup = failed
```

最后产生：

```text
GitHub Job: failed

result.json:
  status: passed

node1/node-result.json:
  status: failed
  cleanup_failed
```

这是不允许的。

### 目标协议

改成：

```text
service ready
    ↓
execute
    ↓
local execution outcome
    ↓
cleanup
    ↓
build final local NodeOutcome
    ↓
report NodeOutcome to leader
    ↓
leader waits for all final outcomes
    ↓
leader builds result.json
```

推荐概念：

```python
@dataclass
class NodeOutcome:
    node_id: str
    execution_status: str
    failure: RunFailure | None
    cleanup_errors: list[RunFailure]
```

Coordinator 不应该只知道：

```text
ready
failed
cleaned
```

还应该最终知道每个 node 的完整 outcome。

### 验收

增加场景：

```text
node0 execution passed
node1 execution passed
node1 cleanup failed
```

最终必须满足：

```text
result.json.status == failed
node1 outcome == cleanup failure
GitHub exit status == failed
```

且所有结果一致。

---

## P0-3. Leader 自己的 node-result 不得早于最终 cleanup outcome 固化

当前 leader 可能：

```text
先写 node-result.json
    ↓
等待其他节点 cleanup
    ↓
leader 自己后续又新增 cleanup failure
```

导致：

```text
node-result.json
```

与内存状态不一致。

### 要求

Node result 只应在本节点最终 outcome 确定后写。

原则：

> 同一个 NodeOutcome 应作为：
>
> - node-result.json；
> - coordinator final report；
> - final result aggregation；
>
> 的唯一来源。

不要让三套状态分别推导。

---

# 6. P1：职责边界与代码结构重构

---

## P1-1. 大幅缩减 `plan.py` runtime validation

当前 `plan.py` 包含大量：

```text
mapping type check
required field
non-empty string
positive integer
port range
health path
api_version
kind
node id continuity
launch file check
duplicate launch script
gateway conflict
leader readiness
step schema
...
```

本阶段假设 intermediate 已经过 converter validation。

因此 Runtime 不应该重新验证这些静态事实。

### 应从 runtime 前移的内容

后续由 YAML -> intermediate converter 负责：

```text
字段 required
字段类型
unknown fields
api_version
kind
node id 连续性
node count
slug
role schema
step id uniqueness
script path containment
script file existence（生成阶段）
port range
port collision
gateway/readiness conflict
DP/TP/rank 一致性
resource consistency
KV connector semantic validation
gateway backend expansion
baseline/tolerance normalization
```

### Runtime 仍应处理

必须保留：

```text
文件在真实 runtime 被删除/挂载失败导致的 OSError
进程启动失败
service crash
gateway crash
readiness timeout
HTTP error
signal
coordinator unreachable
step timeout
cleanup failure
artifact I/O failure
AISBench output parse failure
```

不要把 runtime fault handling 当成“多余防御”。

---

## P1-2. 去掉或弱化 `--validate-only`

Validator 应属于 converter/build 阶段，而不是 Runtime Runner 的核心职责。

目标：

```text
converter validates
runner executes
```

如果短期测试仍需要 validate-only，可以保留临时入口，但：

- 不继续扩展功能；
- 标记为 transitional；
- 后续迁移出 Runner。

---

## P1-3. 去掉不必要的 node auto-detection

如果稳定 CI/LWS 路径始终传：

```text
--node-id
```

那么 Runner 不需要同时支持：

```text
自动读取 ip command
fallback ioctl
hostname getaddrinfo
猜测当前 node
```

除非有明确的本地用户场景要求。

### 原则

如果 runtime context 已经知道：

```text
当前 node id
当前 node IP
当前 interface
```

则直接传入。

不要让 Runner 猜。

### 推荐后续接口

可考虑统一成：

```text
runner
  --plan <final-plan>
  --runtime-context <runtime.json>
```

runtime context 包含：

```yaml
node_id: node1
local_interface: eth0
hosts:
  node0: 10.x.x.x
  node1: 10.x.x.x
artifact_root: ...
vllm_ascend_root: ...
```

本轮可渐进实现，不要求一次完成。

---

# 7. P1：Stage 执行模型泛化

当前 Runner 硬编码：

```text
checks
accuracy
performance
```

这不应该成为 Executor schema。

未来还可能有：

```text
latency
throughput
long-context
multimodal
stability
memory
prefix-cache
speculative decoding
```

### 推荐 intermediate

由 converter 输出 generic stages：

```yaml
stages:
  - id: completion
    failure_category: check_failed
    steps:
      - ...

  - id: gsm8k-accuracy
    failure_category: evaluation_failed
    steps:
      - ...

  - id: gsm8k-performance
    failure_category: evaluation_failed
    steps:
      - ...
```

Runner 只做：

```python
for stage in plan.stages:
    run_stage(stage)
```

Runner 不需要知道：

```text
这个 stage 是 accuracy 还是 performance
```

具体分类可以保存在：

```text
stage metadata
result metadata
generated script
```

而不是写死在 core。

---

# 8. P1：Mooncake 改为 runtime image contract

当前 runtime 逻辑：

```text
如果 import mooncake 失败
    ↓
pip install mooncake-transfer-engine-npu
```

不建议保留。

问题：

```text
固定 vllm-ascend image
+
运行当天最新 Mooncake package
=
不可复现环境
```

### 要求

优先：

```text
CI/runtime image
    ↓
已经包含正确 Mooncake
```

Runtime 直接使用。

如果需要 sanity check：

```bash
python3 -c 'import mooncake'
```

即可。

不要在 testcase execution 中动态 provisioning runtime dependency。

---

# 9. P1：`run_online_dp.py` 标记为临时 upstream workaround

当前 wrapper 依赖上游：

```python
namespace = runpy.run_path(...)
processes = namespace.get("processes", [])
```

这依赖上游内部变量：

```text
processes
```

不是稳定接口。

### 当前可接受

因为它解决了：

```text
launch_online_dp.py worker failure 未正确传播 exit code
```

这一真实问题。

### 但必须做

1. 添加清晰注释：
   `TEMPORARY UPSTREAM WORKAROUND`
2. 记录对应 upstream issue / PR；
3. 如果尚无 issue，建议创建；
4. 上游修复后 recipes 删除 wrapper；
5. 不要继续在 wrapper 上增加业务逻辑。

最终目标：

```text
exec upstream launch_online_dp.py
```

---

# 10. P1：AISBench adapter 保留，但缩薄

AISBench 属于外部测试工具。

合理边界：

```text
Generated testcase-specific config
        ↓
AISBench execution
        ↓
AISBench output adapter
        ↓
Recipe CI result
```

保留：

```text
AISBench output parsing
metric normalization
result contract conversion
```

可以删除/前移重复 preflight：

```text
command exists
config exists
dataset exists
```

尤其当：

```text
workflow 已经验证 ais_bench executable
shell set -euo pipefail
生成脚本失败自然退出
真正执行 ais_bench 不存在也会立即失败
```

时，不需要重复多层检查。

---

# 11. P1：AISBench cache 需要绑定 runtime environment

当前 cache 若使用：

```text
venv --system-site-packages
```

则 cache 实际依赖 base image 的 Python package environment。

因此 cache key 不能只包含：

```text
AISBench commit
Python major.minor
architecture
constraints hash
```

至少考虑加入：

```text
runtime image digest / immutable image identity
```

或者改为：

```text
AISBench bake into runtime image
```

推荐优先级：

```text
1. Bake into runtime image
2. Shared pinned cache + image digest
3. Fully isolated venv
```

不要在多个仓库各维护复杂的工具安装器。

---

# 12. P1：K8s/LWS adapter 与 core 解耦

以下内容属于 cluster policy，不属于 Recipe execution semantics：

```text
runner label
namespace
PVC
nodeSelector
tolerations
NPU resource key
privileged
memory
ephemeral storage
container image
artifact uploader
OBS
```

例如：

```text
910B4
linux-aarch64-a2b4-8
dedicated=night
固定 PVC
```

应属于：

```text
K8s/LWS adapter / workflow / cluster profile
```

而不是 core Runner。

### 要求

即使本轮不物理拆文件，也应做到：

```text
core 不 import / 不依赖 Kubernetes
core 不理解 LWS
```

K8s adapter 负责把：

```text
intermediate + cluster profile
```

映射成 LWS workload。

---

# 13. P2：可读性与整洁性改进

---

## P2-1. 模板系统保持一致

当前文件叫：

```text
lws.yaml.jinja2
```

但 renderer 是：

```python
str.replace()
```

二选一：

### 方案 A

真正使用：

```text
Jinja2 + StrictUndefined
```

### 方案 B

不用 Jinja2 命名，改成明确的：

```text
simple manifest template
```

不要保持“看起来是 Jinja2、实际上不是”的状态。

---

## P2-2. 错误类型简化

不要同时维护过多：

```text
RunnerError
StageFailure
CoordinatorError
ManagedProcessExited
RunFailure
terminal_status
primary_failure
cleanup_failures
coordinator status
node status
```

异常类型可以存在，但最终 runtime state 应收敛到少量显式 domain object。

优先：

```text
RunFailure
NodeOutcome
RunOutcome
```

异常用于控制流，Outcome 用于结果表达。

不要使用同一事实的多套状态变量。

---

## P2-3. `result.py` 不应做过多二次状态推断

`result.py` 应主要负责：

```text
serialization
schema
atomic write
```

不要让它根据：

```text
status
failure
cleanup_errors
```

重新推断另一套真实状态。

业务状态应该在 Runner/Coordinator 生命周期结束时已经确定。

---

# 14. 明确保留的现有设计

Agent 不要因为重构而破坏以下设计。

---

## 14.1 保留 `process.py` 独立模块

以下能力适合单独存在：

```text
start_new_session
process group
SIGTERM
graceful wait
SIGKILL
log tail
signal cancellation
```

不要为了减少文件数量塞回 Runner。

---

## 14.2 保留 coordinator 独立模块

Coordinator 是：

```text
local / bare-metal / LWS
```

都可使用的轻量状态通道。

不要改成：

```text
依赖 PVC 文件轮询
依赖 Kubernetes API
```

否则可移植性反而下降。

---

## 14.3 保留上游 proxy 直接调用

Gateway 应继续使用上游：

```text
vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

Generated gateway script 显式生成：

```text
prefiller hosts
prefiller ports
decoder hosts
decoder ports
```

Runner 不参与推导。

---

## 14.4 保留 generated node script 显式参数

例如：

```text
DP size
TP size
rank
port
KV connector
environment variables
vllm serve flags
```

最终都可以展开到 node-local script。

不要为了复用重新引入 runtime template engine。

---

# 15. 当前 intermediate fixture 的处理

当前 PR 放入多套：

```text
configs/recipe_ci/plans/<case>/
```

完整 intermediate bundle。

未来这些 bundle 不应手工长期维护。

### 本阶段建议

保留：

```text
1 个 minimal fixture
1 个 realistic complex fixture
```

例如：

```text
minimal-2node
realistic-pd
```

用于：

- unit/integration tests；
- snapshot；
- CI smoke。

其余 generated bundle 如无必要可以减少。

后续 converter 完成后：

```text
Recipe YAML
    ↓ converter
generated fixture
    ↓ runtime test
```

而不是人工维护多套 intermediate。

---

# 16. YAML -> Intermediate：后续任务清单

> **本章节只记录，不在当前阶段实现。**

这一阶段未来单独做。

---

## 16.1 Converter 的最终职责

Converter 输入：

```text
Recipe YAML
```

输出：

```text
Validated Executable Intermediate Bundle
```

输出必须做到：

> Runtime 拿到就可以执行，不需要再理解 Recipe semantics。

---

## 16.2 Schema / Format Validation

Converter 需要完成：

- required fields；
- unknown fields；
- scalar/list/map 类型；
- enum；
- string 非空；
- integer range；
- slug/id；
- API version；
- kind；
- schema version；
- duplicate id；
- unsupported configuration；
- deprecated field detection。

Runtime 不重复做。

---

## 16.3 Node Topology Validation

需要验证：

```text
node 数量
node id
node order
node role
node resource profile
leader definition
```

如果 v1 规定：

```text
node0 == control leader
```

就在 converter 固化。

Runtime 不再推导。

---

## 16.4 Resource Validation

当前 v1 可以明确只支持：

```text
homogeneous npu_per_node
```

Converter 验证：

```text
resource number > 0
DP/TP 与资源关系
每节点所需 NPU 数
设备数量是否与 launch topology 匹配
```

不要为了未来理论上的异构节点提前复杂化 runtime。

未来真实需要：

```text
Prefill 16 NPU
Decode 8 NPU
```

时再升级 schema/adapter。

---

## 16.5 Port Planning

Converter 负责：

```text
service port
readiness port
gateway port
DP RPC port
KV transfer port
control port（若属于 plan）
```

并完成：

```text
range validation
collision detection
per-node expansion
```

最终 intermediate 直接包含明确 port。

Runtime 不重新计算。

---

## 16.6 DP / TP / Rank Expansion

Recipe YAML 可能描述高层：

```text
DP = 8
TP = 1
nodes = 2
prefill / decode
```

Converter 负责生成：

```text
dp_size
dp_size_local
dp_rank_start
tensor_parallel_size
dp address
rpc port
per-process device mapping
```

最终写入 node-local script。

Runtime 不理解这些概念。

---

## 16.7 P/D Topology Expansion

Converter 负责：

```text
Prefill node group
Decode node group
backend list
gateway argument
KV connector role
```

例如最终直接生成：

```bash
--prefiller-hosts ...
--prefiller-ports ...
--decoder-hosts ...
--decoder-ports ...
```

不要让 Runner 根据 `role` 动态构造。

---

## 16.8 KV Connector Validation

Converter 负责：

```text
connector name
producer / consumer role
port
extra config
prefill/decode topology
connector-specific required fields
```

如果 Recipe 表达非法组合，应在 converter 阶段失败。

---

## 16.9 vLLM Command Generation

Converter 最终生成：

```bash
vllm serve ...
```

包括：

```text
model path placeholder
served model name
host
port
DP
TP
EP
quantization
max model length
max num seqs
KV transfer config
other vLLM flags
```

Generated node script 应尽量可以直接复制执行和审查。

---

## 16.10 Gateway Generation

Converter 决定：

```text
是否需要 gateway
gateway implementation
host
port
health path
backend list
```

Intermediate 只保存最终 launcher/script。

Runtime 只启动。

---

## 16.11 Readiness Generation

Converter 根据最终 service topology 生成：

```text
health endpoint
port list / port range
health path
```

Runtime 只执行 HTTP readiness check。

---

## 16.12 Stage Generation

Recipe 中的：

```text
completion check
accuracy
performance
其他 evaluation
```

转换成 generic stage：

```yaml
stages:
  - id: ...
    steps:
      ...
```

同时生成：

```text
timeout
failure category
script
artifact contract
```

Runtime generic execute。

---

## 16.13 AISBench Config Generation

Converter 负责生成 testcase-specific：

```text
model config
dataset config/reference
stream/non-stream config
model name
endpoint placeholder
generation config
num prompts
summarizer
```

不要让 runtime 根据 Recipe 再拼 AISBench config。

---

## 16.14 Accuracy Baseline / Tolerance

如果 Recipe 声明：

```text
baseline
allowed_drop
threshold
```

Converter 直接生成到最终 evaluation command / metadata。

Runtime 不通过：

```text
兼容环境变量
默认 fallback
```

覆盖。

---

## 16.15 Static File / Path Validation

在 intermediate 生成完成后检查：

```text
所有 generated script 存在
所有 referenced path 存在
path 不逃逸 bundle directory
symlink target 合法
step id 唯一
node script 唯一
```

这是 build-time validation。

---

## 16.16 Intermediate Manifest

建议 converter 最后生成一个明确 manifest。

例如：

```yaml
api_version: recipe-ci/v1
kind: ExecutablePlan

metadata:
  name: ...

model:
  id: ...
  cache_path: ...
  served_name: ...

resources:
  npu_per_node: 8

nodes:
  - id: node0
    launch: nodes/node0/run.sh
    readiness:
      endpoints:
        - port: 7100
          path: /health

stages:
  - id: completion
    failure_category: check_failed
    steps:
      - id: completion
        script: checks/completion.sh
        timeout_seconds: 300
```

此 manifest 已经是 final executable contract。

---

# 17. Upstream 复用 / Copy 原则

Agent 遇到“recipes 是否应该自己保存一份”时按下面判断。

---

## 应直接使用上游

如果内容是：

```text
vLLM Ascend runtime behavior
官方 launcher
官方 gateway/proxy
官方 deployment example
runtime dependency
```

优先直接使用容器中的上游。

---

## 可以保留薄 adapter

如果 recipes 需要：

```text
统一结果协议
统一 artifact
统一 exit code
统一 metric
CI-specific wrapper
```

可以写薄 adapter。

要求：

```text
不复制完整实现
不重新实现业务语义
尽量只做 protocol translation
```

---

## 可以 Copy / Freeze 的内容

只有当：

```text
上游内容是纯数据/模板
变化极少
运行时不方便获取
便携性收益明显
维护成本低
```

才考虑 copy。

例如：

```text
少量 smoke dataset
极小的 generated fixture
固定 testcase sample
```

---

## 不建议 Copy

不建议 copy：

```text
launcher implementation
proxy implementation
Mooncake/runtime library
vLLM helper
复杂 benchmark framework
```

否则形成双源维护。

---

# 18. 每周 AI Dependency Drift Analysis（后续任务）

> 本阶段可先记录设计，不要求立即实现。

目标不是：

```text
自动同步 / copy 上游代码
```

而是：

```text
自动分析 upstream drift
```

---

## 18.1 输入

记录 pinned dependencies：

```text
vllm-ascend image tag / digest
vllm-ascend commit
AISBench commit
Mooncake version
referenced upstream files
```

---

## 18.2 每周检查

检查：

```text
referenced upstream file 是否移动
referenced upstream file 是否删除
CLI 参数是否变化
vllm serve flags 是否变化
proxy argument 是否变化
launch_online_dp.py 是否已修复 exit propagation
Mooncake 是否已进入官方 image
AISBench artifact schema 是否变化
AISBench model config API 是否变化
官方推荐 deployment path 是否变化
```

---

## 18.3 输出

Agent 自动输出：

```text
COMPATIBLE
ADAPTER_UPDATE_NEEDED
LOCAL_WORKAROUND_CAN_BE_REMOVED
BREAKING_CHANGE
UPSTREAM_FIX_AVAILABLE
```

---

## 18.4 原则

AI 只：

```text
分析
归类
给建议
```

不要默认：

```text
自动 copy upstream source
自动修改 production path
```

实际同步仍由人工 review。

---

# 19. 建议执行顺序

Agent 按以下顺序工作。

---

## Phase 1 — Correctness

1. 重新设计 node/global outcome；
2. 修复 remote failure 污染；
3. cleanup 后 report final outcome；
4. final result 统一从 node outcomes 聚合；
5. 补充 failure/cleanup integration tests。

完成后再进入下一阶段。

---

## Phase 2 — Runtime Core Simplification

1. 删除重复 static validation；
2. 弱化 validate-only；
3. 去掉不必要 auto node detection/fallback；
4. generic stage executor；
5. 简化 status/failure types。

---

## Phase 3 — Dependency Boundary

1. Mooncake 改为 image contract；
2. `run_online_dp.py` 标记 temporary workaround；
3. 检查可直接调用的 vllm-ascend existing implementation；
4. AISBench adapter 缩薄；
5. cache 与 runtime image identity 对齐。

---

## Phase 4 — Infrastructure Boundary

1. LWS/K8s 与 core 解耦；
2. cluster-specific 参数外移；
3. 清理模板 renderer；
4. 减少手工 intermediate fixtures。

---

# 20. 测试要求

修改后至少覆盖：

---

## Unit Tests

### Coordinator

```text
ready
local failure
remote failure
final outcome
cleanup outcome
idempotency
unknown node
```

### Result

```text
passed
execution failed
cleanup failed
cancelled
aborted due to global failure
```

### Process

```text
normal exit
unexpected exit
timeout
SIGTERM
SIGKILL fallback
cleanup error
```

---

## Integration Tests

### Case A

```text
2 nodes
all pass
```

期望：

```text
all node outcomes passed
global passed
```

---

### Case B

```text
node0 evaluation fails
node1 runtime healthy
```

期望：

```text
node0 failed
node1 not marked local failure
global failed
```

---

### Case C

```text
worker service crashes
```

期望：

```text
worker local failure
leader observes remote failure
global failed
```

---

### Case D

```text
execution passes
worker cleanup fails
```

期望：

```text
worker cleanup failure
global failed
result.json == process exit state
```

---

### Case E

```text
leader receives SIGTERM
```

期望：

```text
cancelled
all reachable nodes cleanup
result/artifact internally consistent
```

---

# 21. Non-Goals

当前 Agent **不要做**：

```text
Recipe YAML parser
Recipe YAML schema 重设计
完整 YAML -> intermediate converter
支持所有现有 Recipe
支持历史 intermediate compatibility
自动修复所有 vllm-ascend 上游问题
重新实现 upstream launcher
重新实现 P/D proxy
为了消除 generated script 重复而重构模板系统
为了理论扩展性立即支持异构节点资源
```

YAML -> intermediate 的任务已经记录在本文第 16 节，后续单独实现。

---

# 22. 最终验收标准

本轮完成后应满足：

### Architecture

- Runtime core 不理解 P/D/DP/TP；
- Runtime core 不理解 AISBench 业务语义；
- Runtime core 不理解 Kubernetes；
- Generated scripts 允许显式重复；
- vLLM Ascend runtime 能力优先复用上游。

### Correctness

- local failure 与 remote/global failure 明确区分；
- cleanup failure 能影响最终结果；
- `node-result.json`、`result.json`、process exit status 一致；
- Leader 不通过半完成状态推导最终结果；
- Node final outcome 有单一事实来源。

### Maintainability

- static validation 主要前移；
- Runner 显著减少 branch / defensive checks；
- stage executor generic；
- cluster-specific policy 不污染 core；
- local upstream workaround 有清晰删除路径。

### Portability

同一 final intermediate + runtime core 可以原则上支持：

```text
LWS
普通 Kubernetes adapter
裸机
本地多机
```

差异只应位于 runtime context / infrastructure adapter，而不是 testcase semantics。

---

# 23. Agent 工作输出要求

每完成一个 Phase，输出：

```text
1. 修改文件
2. 删除了哪些职责
3. 新增了哪些 invariant
4. 测试覆盖
5. 尚未处理的问题
6. 是否发现 upstream 可复用实现
7. 是否发现应提交 upstream fix 的本地 workaround
```

如果发现某段代码可以：

```text
直接调用 vllm-ascend upstream
```

不要立即复制。

先记录：

```text
current local implementation
upstream equivalent
compatibility difference
recommendation
```

再决定删除/adapter/upstream fix。

---

# 24. 最终目标一句话

> **Recipe converter 负责“理解和展开”，Intermediate 负责“显式表达”，Runtime Executor 负责“可靠执行”；vLLM Ascend 上游负责 runtime 实现本身。**

