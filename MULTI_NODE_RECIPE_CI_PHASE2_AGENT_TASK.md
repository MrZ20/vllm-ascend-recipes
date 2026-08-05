# Multi-node Recipe CI 第二阶段 Agent 执行任务书

> 2026-08-05 执行决策覆盖：本文原始任务中的“三/四节点回归”“每节点一个 GitHub
> Actions job”和“不实现 Kubernetes adapter”已被后续用户指令替代。本阶段只做
> DeepSeek V4 双节点 P/D 真实 CI；手动入口调用 reusable workflow，后者复用 vLLM Ascend
> 的单 controller Job + 动态 size `LeaderWorkerSet`。K8s/PVC 只是源码暂存、Pod 调度和
> artifact 传输适配层，核心 Runner 仍使用 HTTP coordinator，且本地手动模式仍不依赖
> K8s 或共享文件系统。后续又统一了入口：本地和 LWS Pod 都只执行
> `scripts/recipe_ci/run.sh`；CI 自动注入 LWS 环境，本地手动设置
> `LWS_WORKER_INDEX`、`RECIPE_CI_CLUSTER_IPS`、网卡和可见卡。任务书后文中的每节点
> Actions job、`--hosts`、`--node-id` 公开调用等旧建议不再适用。最终状态以
> `MULTI_NODE_RECIPE_CI_HANDOFF.md` 和 `docs/MULTI_NODE_RECIPE_CI.md` 为准。

## 0. 基本信息

- 仓库：`https://github.com/MrZ20/vllm-ascend-recipes`
- 开发基线提交：`c7d5fa448d680d80ae0a88da1a0ce79ae0e849ec`
- 主要代码目录：`scripts/recipe_ci/`
- plan 目录：`configs/recipe_ci/plans/`
- 当前阶段：第一阶段主链路已完成，开始第二阶段加固
- 输入仍为手工维护的 `plan.yaml`
- 本阶段不实现 Recipe YAML/文档到 plan 的转换器

本文是可直接交给 coding agent 执行的任务书。Agent 应按本文顺序实施，避免未经要求的大规模重构。

---

## 1. 第二阶段总体目标

将现有多节点 Recipe CI 从“主链路可以运行”提升到：

- 可重复
- 可诊断
- 可扩展到三、四节点
- 任一节点失败后能够尽快传播失败并清理
- 有稳定、结构化、机器可读的结果
- 能通过手动 GitHub Actions workflow 执行
- 能在无 NPU 环境完成主要生命周期集成测试

第二阶段完成后，应达到：

> 在干净的 vLLM Ascend 镜像中，从固定 `plan.yaml` 开始，手动 workflow 能稳定完成两节点和至少一个三节点验证；成功时生成结构化结果，失败时能指出具体节点、阶段和原因；所有节点均无本次任务遗留的子进程。

---

## 2. 必须保持的架构边界

以下设计已经确定，除非发现无法继续实现的阻断问题，否则不要改变。

### 2.1 Runner 不理解推理拓扑

Runner 不得：

- 自动识别 Prefill/Decode 拓扑
- 根据 `role` 自动生成 gateway backend
- 自动计算 DP rank、start rank、local size
- 解析 KV Connector
- 复制或复刻 vLLM Ascend 的 launcher 逻辑

每个节点继续运行自己的 plan-local `run.sh`。拓扑、rank、端口和服务参数保留在节点脚本中。

### 2.2 节点 ID 和角色分离

- 节点 ID 必须为 `node0...nodeN`
- `role` 必填
- 多个节点允许拥有相同 `role`
- 第一个节点 `node0` 是控制面 leader
- leader 不等同于 DP master、API 节点或 Prefill 节点
- `role` 目前只用于描述、日志和环境变量，不驱动 Runner 行为

### 2.3 不引入新的基础设施依赖

本阶段继续：

- 使用 HTTP coordinator
- 不依赖共享文件系统
- 不实现 Kubernetes adapter
- 不实现独立外部 coordinator 服务
- 不上传或通过 coordinator 转发节点日志
- 不自动下载大型模型或完整数据集

### 2.4 不维护 AISBench fork

recipes 仓库只负责：

- 选择 accuracy/performance
- 提供 plan-local AISBench 配置
- 执行 preflight
- 调用 AISBench
- 提取机器可读结果
- 应用 baseline/tolerance
- 收集 artifact

通用 AISBench 能力后续推动到 AISBench 或 vllm-ascend 上游。

---

## 3. 代码重构原则

### 3.1 不创建泛化 `utils.py`

不要把无关功能集中到 `utils.py`。

允许新增的模块最多优先考虑：

```text
scripts/recipe_ci/
├── runner.py
├── plan.py
├── coordinator.py
├── process.py      # 子进程、信号、日志尾部、清理
├── result.py       # 结果模型、原子写入、时间和结果合并
├── run.sh
└── install_aisbench.sh
```

不要仅为几行代码新增：

- `errors.py`
- `constants.py`
- `network.py`
- `environment.py`
- 大量单函数文件

### 3.2 保持 Runner 主流程线性可读

`runner.py` 应主要展示生命周期：

1. 加载并校验输入
2. 创建运行上下文
3. 启动 coordinator
4. 启动本地服务
5. readiness
6. 全节点 ready
7. gateway
8. checks
9. evaluations
10. 发布 terminal 状态
11. 清理
12. 上报 cleaned
13. 写最终结果

防御性细节可放入 `process.py` 或 `result.py`，避免主流程出现大段嵌套。

### 3.3 不为短小、只使用一次的逻辑过度拆函数

满足以下条件时，可以直接保留在主流程并用注释分段：

- 代码行数很少
- 当前没有复用
- 逻辑不复杂
- 不影响主流程阅读
- 拆分后只会增加跳转

相反，以下逻辑应拆出：

- 信号处理
- 子进程轮询
- 超时
- 日志尾部读取
- 进程组清理
- 结果原子写入
- 重试和 HTTP 错误分类

### 3.4 plan-local 脚本允许显式重复

节点脚本中的 vLLM 参数、rank、KV role 等重复通常具有拓扑审查价值。

不要为了去重，把以下内容强行抽成公共 shell 工具：

- Qwen node0/node1 的完整 `vllm serve` 命令
- Prefill/Decode 的 `run_dp_template.sh`
- gateway backend 列表

可以通过以下方式改善可读性：

- 参数分组注释
- 将 `$1...$7` 转换为命名 shell 变量
- 测试关键差异
- 对完全相同的生成产物增加静态一致性检查

---

# 4. 阶段一：冻结执行契约

在修改核心实现前，先在代码或文档中明确以下契约。

## 4.1 Plan API 兼容策略

`recipe-ci/v1` 采用严格 schema：

- 未知字段报错
- v1 内只做不改变语义的 bug fix
- 新增改变执行语义的字段时升级到 `recipe-ci/v2`
- v1 中第一个节点永远是 leader
- v1 中 gateway 若存在，只由 leader 启动
- v1 中无 gateway 时，leader 必须有 readiness，且第一个 readiness 端口是统一 endpoint
- v1 当前只正式支持 IPv4，除非本阶段完整补齐 IPv6

建议在 `docs/MULTI_NODE_RECIPE_CI.md` 增加兼容策略章节。

## 4.2 Coordinator 状态机

运行状态统一使用：

```text
running
passed
failed
cancelled
```

允许的 terminal 转换：

```text
running -> passed
running -> failed
running -> cancelled
```

terminal 状态不可再改变。

节点级状态至少包括：

```text
pending
ready
failed
cleaned
```

重复上报相同状态必须幂等。

### 关键语义

`cleaned` 表示：

- 本节点启动的所有受管进程组已经收到清理
- 超时进程已经 SIGKILL
- 日志文件已经关闭
- 已验证本次任务的 process group 不再存在
- 本节点 `node-result.json` 已写入

不得在实际清理前上报 `cleaned`。

## 4.3 错误分类

不要创建大量异常类。可使用单一结构化错误对象，例如：

```python
RunFailure(
    category="evaluation_failed",
    stage="accuracy",
    node_id="node0",
    message="...",
)
```

允许的主要 category：

```text
validation_failed
launch_failed
startup_timeout
readiness_failed
gateway_failed
node_failed
check_failed
evaluation_failed
coordinator_unreachable
cancelled
cleanup_failed
internal_error
```

规则：

- 第一个真实执行错误记录为 `primary_failure`
- 后续清理错误进入 `cleanup_errors`
- 后续诊断信息进入 `warnings`
- 清理错误不能覆盖原始失败
- 原执行成功但清理失败时，最终状态为 `failed`，category 为 `cleanup_failed`

## 4.4 Artifact 所有权

因为没有共享文件系统，必须区分：

1. 每个节点本地产物
2. leader 的结构化摘要
3. GitHub Actions 聚合后的完整 artifact bundle

Coordinator 不负责传输文件。

---

# 5. 阶段二：Plan schema 和 validate-only

主要修改：

```text
scripts/recipe_ci/plan.py
scripts/recipe_ci/runner.py
tests/recipe_ci/
docs/MULTI_NODE_RECIPE_CI.md
```

## 5.1 完善 schema 校验

### 节点校验

必须校验：

- 至少两个节点
- 节点严格连续命名为 `node0...nodeN`
- 节点列表中的位置与 ID 一致
- `role` 必填，但不限制唯一
- 每个节点有独立 launch 文件
- 节点 ID 不允许额外空格或其他字符

错误示例：

```text
nodes[1].id must be node1, got node2
```

### 标识符校验

建议对以下字段使用安全 slug：

- `metadata.name`
- check id
- evaluation id

建议允许：

```regex
[A-Za-z0-9][A-Za-z0-9._-]*
```

同一个 stage 内的 step id 必须唯一，防止日志或结果文件覆盖。

### 路径校验

所有 plan 引用的文件必须：

- 存在
- 是普通文件
- 解析后的真实路径位于 plan 目录内
- symlink 最终目标也不能逃逸 plan 目录

包括：

- node launch
- gateway launch
- check script
- evaluation script

禁止：

```yaml
script: ../../outside.sh
```

错误信息必须包含字段路径和实际值。

### readiness 校验

必须校验：

- `port_start` 在 `1..65535`
- `count >= 1`
- `port_start + count - 1 <= 65535`
- `count` 有合理上限，例如 1024
- `health_path` 以 `/` 开头

### gateway 校验

必须校验：

- gateway port 在 `1..65535`
- health path 以 `/` 开头
- gateway port 不与 leader 本机 readiness 端口范围冲突

不要把不同机器上的相同端口视为冲突。

### hosts 校验

保留并完善：

- hosts key 与 plan 节点完全一致
- address 必填
- interface 可选
- v1 明确只接受 IPv4 地址，或完整支持 IPv6

推荐使用 `ipaddress.ip_address()`，若暂只支持 IPv4则明确拒绝 IPv6。

### 未知字段

v1 对关键层级进行 unknown field 检查：

- 顶层
- metadata
- model
- node
- readiness
- gateway
- step
- evaluations
- hosts

拼写错误必须在启动前被发现。

## 5.2 改进 `--validate-only`

行为：

### 未传 `--hosts`

- 只校验 plan
- 不要求模型存在
- 不要求 NPU
- 不启动任何进程
- 输出展开后的静态拓扑

### 传入 `--hosts`

- 同时校验 hosts
- 输出每个节点的地址和 interface
- 输出最终 endpoint
- 不启动任何进程

当前 validate-only 不得在解析 plan 后立即返回而跳过可选 hosts 校验。

### 输出示例

```text
Plan: deepseek-v2-lite-pd-2n2c
API version: recipe-ci/v1
Leader: node0
Model: vllm-ascend/DeepSeek-V2-Lite-W8A8
Served name: deepseek-v2-lite

Nodes:
  node0 role=prefill launch=nodes/node0/run.sh readiness=7100-7101
  node1 role=decode launch=nodes/node1/run.sh readiness=7100-7101

Gateway:
  leader=node0 port=38085 health=/healthcheck

Checks:
  completion timeout=300s

Evaluations:
  accuracy: gsm8k timeout=7200s
  performance: gsm8k-perf timeout=7200s
```

## 5.3 验收标准

- 非连续 node id 在启动前失败
- 缺少 role 在启动前失败
- hosts 不匹配在启动前失败
- 越界端口在启动前失败
- 路径逃逸在启动前失败
- duplicate step id 在启动前失败
- unknown field 在启动前失败
- 错误信息包含具体字段和原因
- `--validate-only` 输出完整拓扑摘要
- validate-only 不启动 NPU 或服务进程

---

# 6. 阶段三：Runner 生命周期和进程监管

主要修改：

```text
scripts/recipe_ci/runner.py
scripts/recipe_ci/process.py
scripts/recipe_ci/result.py
tests/recipe_ci/
```

## 6.1 新增 `process.py`

建议提供少量高内聚能力。

### `ManagedProcess`

保留或迁移：

```python
@dataclass
class ManagedProcess:
    name: str
    process: subprocess.Popen[bytes]
    log_path: Path
    log_file: BinaryIO
```

可增加：

- stage
- node_id
- started_at

不要发展成复杂进程框架。

### 启动进程

所有服务、gateway、check、evaluation：

- 使用新 session/process group
- stdout/stderr 写入 artifact log
- 记录 PID、process group、启动时间

### 日志尾部

服务异常退出时，错误输出中包含最后一段日志。

建议：

- 默认最后 50 行或最多 16 KiB
- 保留完整日志文件
- 终端输出只打印尾部
- 二进制或无效 UTF-8 使用替换模式解码

### 统一清理

按逆序清理受管进程：

1. 对 process group 发送 SIGTERM
2. 等待固定 grace period
3. 对仍存活的 group 发送 SIGKILL
4. 等待退出
5. 关闭日志
6. 验证该 process group 不存在

不要：

- `pkill vllm`
- `killall python`
- 按进程名清理整台机器
- 误杀其他 CI 任务

如发现子进程逃逸出本次 process group：

- 记录诊断
- 本阶段可标为 cleanup warning/error
- 不进行全局进程名清理

## 6.2 信号处理

捕获：

- SIGINT
- SIGTERM

实现方式：

- signal handler 只设置 cancellation event
- 主轮询检测 event
- 抛出 `cancelled`
- 进入统一 terminal 和 cleanup 流程

不要在 signal handler 中直接：

- 发 HTTP 请求
- 写复杂 JSON
- 等待子进程
- 执行长时间清理

## 6.3 修复 check/evaluation 期间失去监管的问题

不能继续使用阻塞式 `subprocess.run(..., timeout=...)` 完成长时间步骤。

应实现 supervised step：

```text
启动 step 子进程
  ↓
周期性检查：
  - step 是否退出
  - 本地 service/gateway 是否异常退出
  - coordinator 是否已有远端失败
  - 是否收到 cancellation
  - step 是否超时
  ↓
任一条件失败则终止 step，并进入统一失败流程
```

建议轮询间隔 0.5 到 1 秒。

必须保证：

- evaluation 运行期间远端节点失败能尽快终止
- evaluation 运行期间本地服务退出能尽快终止
- step 超时与 service 退出使用不同 category
- step 非零退出记录 stage、id、return code 和 log tail

## 6.4 endpoint 和环境变量整理

节点严格为 `nodeN` 后，仅保留：

```text
RECIPE_NODE_0_IP
RECIPE_NODE_1_IP
...
```

删除无价值的重复形式：

```text
RECIPE_NODE_NODE0_IP
```

建议明确 artifact 环境变量：

```text
RECIPE_ARTIFACT_ROOT
RECIPE_NODE_ARTIFACT_DIR
RECIPE_STEP_ARTIFACT_DIR
RECIPE_STEP_RESULT_FILE
```

兼容已有 `RECIPE_ARTIFACT_DIR` 时，可以在 v1 内保留别名并在文档注明。

## 6.5 `vllm-ascend-root` 处理

Runner 不应无条件要求所有 plan 都存在完整 vllm-ascend source tree。

推荐：

- 解析用户传入或默认路径
- 将路径注入环境
- 仅当 plan 的脚本实际使用该路径时，由 plan-local preflight 或脚本报错
- 不在通用 Runner 启动前硬编码检查某个 vLLM example 文件

如决定保留硬性要求，必须把它写成 `recipe-ci/v1` 的明确运行环境契约，并让测试不再用隐含 fake 目录绕过。

## 6.6 清理完成后才上报

worker 流程应为：

```text
收到 terminal 状态
  ↓
停止本地受管进程
  ↓
验证清理
  ↓
写 node-result.json
  ↓
上报 cleaned
  ↓
退出
```

leader 流程应为：

```text
checks/evaluations 完成或失败
  ↓
发布 passed/failed/cancelled
  ↓
清理 leader 本地进程
  ↓
写 leader node-result.json
  ↓
上报 leader cleaned
  ↓
等待其他节点 cleaned
  ↓
写最终 result.json
  ↓
关闭 coordinator
```

清理必须放在可控生命周期中，不能只依赖当前函数末尾的简单 `finally`。

## 6.7 验收标准

- SIGINT/SIGTERM 后所有受管进程组退出
- 服务异常退出时打印日志尾部
- check/evaluation 期间本地服务失败能尽快终止
- check/evaluation 期间远端失败能尽快终止
- 启动超时、readiness 失败、gateway 失败、check 失败、evaluation 失败分类明确
- 主错误不被清理错误覆盖
- 节点在实际清理后才上报 cleaned
- 不误杀机器上其他任务进程

---

# 7. 阶段四：Coordinator 协议加固

主要修改：

```text
scripts/recipe_ci/coordinator.py
tests/recipe_ci/
```

## 7.1 保留现有 HTTP 实现

不要引入：

- Flask/FastAPI
- 数据库
- 共享文件系统
- 消息队列
- 外部服务

标准库 HTTP server 足够。

## 7.2 幂等请求

以下重复调用返回成功：

- ready
- failed，且失败信息与首个失败不冲突
- cleaned
- 查询 terminal 状态

重复失败请求不能覆盖第一个真实错误。

## 7.3 非法状态转换

terminal 状态不可改变。

示例：

- `running -> failed`：允许
- `failed -> failed`：幂等
- `passed -> failed`：409
- `failed -> passed`：409
- cleaned 在节点未被识别时：400
- 未知节点：400

HTTP body 返回机器可读错误：

```json
{
  "error": {
    "code": "invalid_transition",
    "message": "cannot change terminal state passed to failed"
  }
}
```

## 7.4 客户端重试

只对以下情况重试：

- connection refused
- connection reset
- timeout
- HTTP 408
- HTTP 429
- HTTP 5xx

以下情况立即失败，不重试：

- 400
- 404
- 409
- JSON 协议错误

退避应短且有上限，例如：

```text
0.2s, 0.5s, 1s, 2s
```

不进行无限重试。

## 7.5 leader 硬退出

不能承诺 leader 被 SIGKILL 或机器断电后还能发布失败。

应实现：

- worker 查询 coordinator 连续失败时，先进入短 grace period
- 超过 grace period仍不可达，产生 `coordinator_unreachable`
- worker 主动清理本地服务
- worker 写本地失败结果并退出

优雅 SIGTERM/SIGINT 时，leader 应先发布 cancelled，再清理。

## 7.6 coordinator 日志

leader artifact 中增加：

```text
node0/coordinator.log
```

记录：

- server 启动/停止
- node/action
- 状态变化
- request retry 或错误
- terminal 状态
- cleaned 进度
- 不记录 secret 或完整环境变量

## 7.7 验收标准

测试至少覆盖：

- 节点延迟启动
- 重复 ready
- 重复 cleaned
- 重复 failed
- terminal 状态不可改变
- 4xx 不重试
- 5xx/连接失败有限重试
- 节点中途失败
- leader 优雅取消
- leader coordinator 不可达时 worker 提前退出
- 首个真实错误不被后续错误覆盖

---

# 8. 阶段五：结构化结果和 artifact

主要修改：

```text
scripts/recipe_ci/result.py
scripts/recipe_ci/runner.py
tests/recipe_ci/
```

## 8.1 结果 schema

建议最终结果：

```json
{
  "schema_version": "recipe-ci-result/v1",
  "plan": "deepseek-v2-lite-pd-2n2c",
  "status": "passed",
  "failure": null,
  "nodes": {},
  "checks": {},
  "evaluations": {},
  "warnings": [],
  "started_at": "",
  "finished_at": ""
}
```

失败结构：

```json
{
  "category": "evaluation_failed",
  "stage": "accuracy",
  "node_id": "node0",
  "step_id": "gsm8k",
  "message": "accuracy gsm8k exited with 1",
  "return_code": 1,
  "log_path": "node0/accuracy/gsm8k.log"
}
```

时间使用 UTC ISO 8601，例如：

```text
2026-08-05T03:20:10Z
```

## 8.2 每节点结果

每个节点写：

```text
artifacts/<plan>/nodeN/node-result.json
```

至少包含：

- node id
- role
- local status
- service PID/process group
- ready 时间
- terminal 时间
- cleaned 时间
- primary failure
- cleanup errors
- artifact 文件相对路径

## 8.3 leader 最终结果

leader 写：

```text
artifacts/<plan>/result.json
```

该文件只承诺包含 coordinator 能掌握的结构化状态，不承诺包含远端日志文件。

## 8.4 环境信息

每个节点可写：

```text
nodeN/environment.json
```

只允许 allowlist，例如：

- Python version
- pip version
- OS/kernel
- architecture
- vLLM version
- vllm-ascend version/commit
- CANN version
- AISBench version/commit
- NPU device summary
- image identifier，如 CI 显式提供
- plan commit

禁止直接 dump 全部 `os.environ`，避免泄露：

- token
- secret
- credential
- proxy authentication
- GitHub Actions secret

## 8.5 原子写入

JSON 结果使用：

1. 写临时文件
2. flush/fsync（合理情况下）
3. rename/replace

确保 workflow 取消或进程中断时不会留下半个 JSON。

## 8.6 Artifact 目录

节点本地：

```text
artifacts/<plan>/
└── nodeN/
    ├── service.log
    ├── gateway.log
    ├── coordinator.log
    ├── checks/
    ├── accuracy/
    ├── performance/
    ├── node-result.json
    └── environment.json
```

CI 聚合后：

```text
recipe-ci-bundle/
├── result.json
├── node0/
├── node1/
├── node2/
└── ci-metadata.json
```

完整 bundle 由 GitHub Actions aggregator 生成，不由 coordinator 传输。

## 8.7 验收标准

- 成功和失败都生成可解析 JSON
- 所有状态字段使用统一词汇
- primary failure 保留
- cleanup error 单独记录
- JSON 写入为原子操作
- 不泄露环境 secret
- 无共享文件系统时设计仍成立

---

# 9. 阶段六：AISBench 稳定化

主要修改：

```text
scripts/recipe_ci/install_aisbench.sh
configs/recipe_ci/plans/*/evaluations/
configs/recipe_ci/plans/*/aisbench/
tests/recipe_ci/
docs/MULTI_NODE_RECIPE_CI.md
```

## 9.1 幂等安装

支持：

### 目标目录不存在

- clone 固定 tag
- 校验 expected commit
- 安装依赖
- 验证 `ais_bench -h`

### 目标目录存在且版本正确

- 验证 remote
- 验证 HEAD commit
- 验证工作区是否干净
- 验证 `ais_bench -h`
- 不重复安装

### 目标目录版本不一致

默认明确报错，提示使用：

```text
--force-reinstall
```

只有显式 force 时才删除或重装。

## 9.2 固定 commit

不仅设置 tag，还设置 expected commit：

```text
AIS_BENCH_TAG
AIS_BENCH_EXPECTED_COMMIT
```

tag 解析结果与 expected commit 不一致时失败。

## 9.3 pip 配置

不要默认硬编码某个特定 PyPI mirror。

行为：

- 默认尊重已有 pip 配置
- 用户显式提供 `PIP_INDEX_URL` 时才传对应参数
- 不以全局 `pip check` 作为失败门槛
- 输出 Python、pip、安装路径和 AISBench 信息

## 9.4 安装输出

输出：

- AISBench tag
- AISBench commit
- repository URL
- Python executable/version
- pip version
- installation path
- command path
- `ais_bench -h` 结果

## 9.5 Evaluation preflight

在启动 accuracy/performance 前检查：

- `ais_bench` 命令存在且可执行
- `ais_bench -h` 成功
- plan-local accuracy config 可 import/load
- plan-local performance config 可 import/load
- 数据集目录存在
- artifact 目录可写
- endpoint 和 served model 环境变量存在

preflight 失败使用明确 category，不进入正式 evaluation。

## 9.6 smoke 与质量验收分离

明确两种模式：

### 流程 smoke

目标：

- AISBench 命令能运行
- 请求能成功
- 产物能生成
- 不对模型质量做强结论

### 准确率验收

必须配置：

- baseline
- allowed regression/tolerance
- 合理 `max_out_len`
- 足够样本数
- 机器可读 score

不要把少量样本和极短输出长度的 smoke 分数当成准确率结论。

## 9.7 Step result contract

Runner 不解析 AISBench 内部日志。

每个 evaluation step 接收：

```text
RECIPE_STEP_RESULT_FILE
```

AISBench wrapper 负责写入：

```json
{
  "status": "passed",
  "type": "accuracy",
  "metrics": {
    "accuracy": 0.82,
    "baseline": 0.80,
    "allowed_drop": 0.02
  },
  "artifacts": []
}
```

performance 至少记录：

- TTFT
- TPOT
- E2E latency
- output token/s
- request/s

Runner 只读取通用 step result，并合并到总结果。

## 9.8 plan-local 重复

当前多个 plan 的 AISBench shell 和 Python 配置可能相同。

本阶段优先保持 plan 自包含：

- 不强制移动到公共 utils
- 可增加注释说明是生成/同步产物
- 增加静态一致性测试，防止副本漂移
- 未来由 Recipe 转换器生成

## 9.9 验收标准

- 正确版本重复执行安装脚本不会重复安装
- 错误版本默认失败
- force reinstall 可恢复
- 不安装 moving master
- preflight 能在正式评测前发现缺失命令、配置或数据集
- accuracy/performance 使用各自配置
- smoke 与 accuracy gate 明确区分
- 指标写入结构化 step result 和最终 result.json

---

# 10. 阶段七：三节点和四节点扩展

## 10.1 P/D 三节点用例

新增至少一个：

```yaml
nodes:
  - id: node0
    role: prefill

  - id: node1
    role: decode

  - id: node2
    role: decode
```

必须验证：

- 多个 Decode 节点允许同 role
- gateway 显式展开 node1、node2 backend
- Runner 不根据 role 自动理解 P/D
- 每节点有独立启动脚本
- DP rank/start rank/local size 正确
- 端口不冲突
- 任一节点失败后全部节点退出并清理

重点是验证 `nodeN + role` 的表达能力，不追求模型性能。

## 10.2 普通内部 DP 三节点用例

建议：

```text
node0: API + rank 0/1
node1: headless rank 2/3
node2: headless rank 4/5
```

验证：

- node0 有 readiness
- node1/node2 无 readiness
- 全局 DP size 正确
- 每节点 start rank 正确
- headless 节点生命周期正确
- 任一 headless 节点失败能传播

## 10.3 不进行自动拓扑生成

gateway backend、rank 和端口继续由 plan-local 脚本显式表达。

## 10.4 验收标准

- 至少一个三节点 fixture 在无 NPU 测试中通过
- 至少一个三节点真实 NPU 用例通过
- 重复 role 不影响 coordinator
- 任一节点失败后所有节点完成 cleanup
- result.json 能包含三节点状态

---

# 11. 阶段八：GitHub Actions 手动多节点 workflow

新增：

```text
.github/workflows/recipe_verify_multi_node.yaml
```

先只支持 `workflow_dispatch`。

## 11.1 Inputs

至少支持：

- plan
- image
- evaluation
- commit/ref
- startup timeout
- run timeout

基础设施信息来自 CI 环境或 secrets，不写入仓库：

- hosts/IP
- interface
- NPU card list
- runner labels
- 模型路径

## 11.2 同一 commit

所有节点必须 checkout 完全相同的 commit SHA。

建议 workflow 开始时解析一次 SHA，后续 job 全部显式 checkout 该 SHA。

## 11.3 Job 模型

推荐：

- 每个节点一个 job
- 每个 job 运行对应 `--node-id`
- Runner 作为 job 前台主进程
- 每节点始终上传 artifact
- 最后 aggregator job 下载并汇总

不要：

- 通过一个 job SSH 到所有机器并隐藏节点状态，除非目标 runner 基础设施只能这样工作
- 在 workflow 中复制 Runner 的生命周期
- 在 background shell 中启动 Runner 后让 step 提前结束

## 11.4 NPU 资源锁

使用 concurrency group 防止不同 workflow 抢占同一组 NPU。

锁粒度应与实际 runner/NPU 资源绑定，而不只是 workflow 名称。

## 11.5 取消和超时

workflow 被取消或 job timeout 时：

- GitHub 发送终止信号
- Runner 捕获 SIGTERM
- coordinator 发布 cancelled（leader仍存活时）
- 各节点清理受管 process group
- artifact 上传步骤使用 `if: always()`

leader 硬退出时，worker 通过 coordinator unreachable grace period 自行清理。

## 11.6 Artifact

每节点上传：

```text
nodeN/
```

leader 上传：

```text
result.json
```

aggregator：

- 下载所有节点 artifact
- 组装完整 bundle
- 生成 `ci-metadata.json`
- 即使部分节点 job 失败也尝试执行
- 始终上传 bundle

## 11.7 静态检查

增加：

- `actionlint`
- workflow YAML 解析
- workflow input 静态检查

## 11.8 验收标准

- workflow_dispatch 可选择 plan、镜像、evaluation
- 所有节点使用同一 commit
- IP、网卡、卡号不进入仓库
- 两个或更多节点并行启动 Runner
- leader/worker 日志都上传
- result.json 和 AISBench artifact 始终上传
- workflow 取消后远端受管进程被清理
- actionlint 通过
- 稳定前不接 PR/nightly 自动触发

---

# 12. 测试计划

优先使用本地无 NPU 集成测试验证框架生命周期。

## 12.1 Plan 测试

增加：

- 非连续 node id
- 缺少 role
- role 重复合法
- hosts 缺少节点
- hosts 多余节点
- launch 路径逃逸
- symlink 路径逃逸
- gateway 路径逃逸
- check/evaluation 路径逃逸
- duplicate step id
- unknown field
- readiness count 越界
- readiness 端口溢出
- gateway 与 leader readiness 冲突
- 非法 health path
- validate-only 带 hosts
- validate-only 拓扑摘要

## 12.2 Coordinator 测试

增加：

- 三节点全部 ready
- 延迟 ready
- 重复 ready
- 重复 cleaned
- 重复 failed
- unknown node
- illegal terminal transition
- 首个失败保留
- 4xx 不重试
- 5xx 有限重试
- coordinator unreachable

## 12.3 Runner 无 NPU 集成测试

增加：

- 三节点全部成功
- 无 gateway 的内部 DP 生命周期
- headless 节点无 readiness
- 服务启动失败
- 服务 readiness 超时
- gateway 启动失败
- gateway readiness 超时
- check 非零退出
- evaluation 非零退出
- evaluation 超时
- evaluation 期间本地服务退出
- evaluation 期间远端节点失败
- coordinator 超时
- 节点重复 ready
- SIGTERM 清理
- SIGINT 清理
- leader 优雅失败
- leader coordinator 突然不可达
- cleanup 失败不覆盖主错误
- result.json 内容
- node-result.json 内容
- JSON 原子写入
- artifact 文件存在

## 12.4 Shell 和静态检查

增加：

- ShellCheck
- Python lint/format
- actionlint
- plan 示例静态检查
- `bash -n`
- `python -m py_compile`
- `git diff --check`

不要为测试过早拆出大量测试文件。可以先在现有测试模块中按测试类分组；当文件明显过大时再拆为：

```text
test_plan.py
test_coordinator.py
test_runner_integration.py
test_examples.py
```

---

# 13. 建议实现顺序

严格按以下顺序执行，避免同时修改所有模块导致难以回归。

## 第 1 批：契约和 schema

1. 文档化 v1 契约
2. 增加错误 category 和 result schema
3. 完成 plan 严格校验
4. 改进 validate-only
5. 补 plan 测试

## 第 2 批：进程和结果基础设施

1. 新增 `process.py`
2. 新增 `result.py`
3. 实现 log tail
4. 实现原子 JSON
5. 实现统一 signal cancellation
6. 测试进程组清理

## 第 3 批：Runner 生命周期

1. supervised check/evaluation
2. primary failure/cleanup error
3. terminal 后 cleanup
4. cleanup 后 cleaned
5. per-node result
6. leader final result
7. 补失败路径测试

## 第 4 批：Coordinator

1. terminal immutable
2. illegal transition
3. 请求幂等
4. HTTP 错误分类
5. 有限重试
6. unreachable grace
7. coordinator 日志
8. 补协议测试

## 第 5 批：AISBench

1. 幂等安装
2. tag + commit pin
3. preflight
4. step result contract
5. accuracy baseline/tolerance
6. performance 指标
7. 静态一致性测试

## 第 6 批：扩展和 CI

1. 三节点 P/D fixture
2. 三节点普通 DP fixture
3. 两节点实机回归
4. 三节点实机回归
5. 手动 GitHub workflow
6. actionlint
7. 取消和 artifact 回归

---

# 14. 暂时继续不做

本阶段不得顺手实现：

- Recipe YAML 到 plan 的正式转换器
- Kubernetes 执行适配器
- 共享文件系统 coordinator
- 独立 coordinator 服务
- 在 Runner 中自动解析 P/D 拓扑
- 在 Runner 中自动生成 DP rank
- role 自动聚合 IP
- 把 vLLM Ascend examples 复制进 recipes
- 自维护 AISBench fork
- 自动下载大型模型
- 自动下载完整数据集
- PR/nightly 自动多节点触发
- 大规模重命名或目录重组
- 为所有简单代码创建类和函数
- 全局 `pkill`/`killall`

---

# 15. Agent 工作方式要求

## 15.1 开始前

Agent 必须：

1. checkout 基线或目标开发分支
2. 查看当前 `git status`
3. 阅读：
   - `scripts/recipe_ci/plan.py`
   - `scripts/recipe_ci/runner.py`
   - `scripts/recipe_ci/coordinator.py`
   - `tests/recipe_ci/test_multi_node_ci.py`
   - `docs/MULTI_NODE_RECIPE_CI.md`
   - 两个现有 plan
4. 运行现有测试，记录基线结果
5. 不假设 handoff 文档中的临时机器/IP仍有效

## 15.2 每批修改后

必须执行适用的检查：

```bash
.venv/bin/python -m unittest discover -s tests/recipe_ci -v
python3 -m py_compile \
  scripts/recipe_ci/plan.py \
  scripts/recipe_ci/runner.py \
  scripts/recipe_ci/coordinator.py \
  scripts/recipe_ci/process.py \
  scripts/recipe_ci/result.py
bash -n scripts/recipe_ci/run.sh
bash -n scripts/recipe_ci/install_aisbench.sh
git diff --check
```

如仓库配置了对应工具，再运行：

```bash
ruff check .
ruff format --check .
shellcheck scripts/recipe_ci/*.sh
actionlint
```

不要为了让测试通过而弱化真实校验。

## 15.3 提交粒度

推荐按阶段形成独立提交：

1. `Tighten recipe CI plan validation`
2. `Add supervised process and result handling`
3. `Harden runner cleanup and failure propagation`
4. `Harden coordinator state transitions`
5. `Stabilize AISBench installation and results`
6. `Add three-node fixtures and manual workflow`

避免一个提交同时包含 schema、Runner、AISBench、workflow 和大量格式化。

---

# 16. 最终交付清单

Agent 最终回复必须包含：

## 16.1 修改摘要

按模块说明：

- plan/schema
- Runner/process
- coordinator
- result/artifact
- AISBench
- fixtures
- workflow
- tests
- docs

## 16.2 关键架构决策

说明：

- 为什么没有创建 `utils.py`
- 为什么 plan-local 拓扑脚本保留显式重复
- 为什么 coordinator 不传输日志
- 为什么 artifact 由 CI aggregator 聚合
- leader 硬退出时的实际失败保证
- terminal 与 cleaned 的区别

## 16.3 测试结果

列出实际执行命令和结果。

不得声称未执行的测试已通过。

## 16.4 未完成项

明确列出：

- 需要真实 NPU 才能验证的内容
- 需要 CI secret/runner 才能验证的内容
- AISBench 输出格式不稳定导致的限制
- 任何剩余风险

---

# 17. 第二阶段最终验收标准

满足以下全部条件后，第二阶段可视为完成。

## Plan

- 错误 plan 在启动任何 NPU 服务前失败
- 错误信息包含具体字段和值
- `--validate-only` 输出拓扑摘要
- node ID、role、hosts、路径、端口均严格校验
- `recipe-ci/v1` 兼容策略清晰

## Runner

- SIGINT/SIGTERM 能清理所有本次受管进程组
- 服务异常退出输出日志尾部
- check/evaluation 期间持续监管所有节点
- 失败 category 清晰
- 主错误不被清理错误覆盖
- 清理完成后才上报 cleaned

## Coordinator

- ready/failed/cleaned 幂等
- terminal 状态不可改变
- 客户端有限重试
- 4xx 不重试
- leader 优雅失败能传播
- leader 硬退出时 worker 能在 grace period 后自行失败清理
- coordinator 有独立日志

## Result 和 artifact

- 成功和失败均生成结构化结果
- 每节点有 `node-result.json`
- leader 有 `result.json`
- 环境信息使用 allowlist
- JSON 原子写入
- CI 能聚合多节点 artifact

## AISBench

- 安装脚本幂等
- 固定 tag 和 commit
- preflight 完整
- smoke 与准确率验收分离
- accuracy 有 baseline/tolerance
- performance 指标进入 result.json
- 不维护 AISBench fork

## 扩展和 CI

- 两节点 P/D 回归通过
- 两节点普通 DP 回归通过
- 至少一个三节点真实用例通过
- 手动 workflow 可运行
- 取消或超时后无本次任务残留进程
- actionlint、ShellCheck、Python 检查和无 NPU 集成测试通过

---

## 18. 首要实施提醒

以下问题优先级最高，不要被一般代码风格工作取代：

1. 当前 check/evaluation 阻塞执行期间无法及时发现本地或远端节点失败。
2. 当前 completed 在实际清理前上报，语义不正确。
3. 当前 terminal 状态可能被延迟失败请求改变。
4. 无共享文件系统时，Runner 不能直接生成包含所有远端日志的单一 artifact 目录。
5. leader 被硬杀死时，worker 只能检测 coordinator 不可达，不能收到 leader 主动发布的失败。
6. AISBench 结果解析不能直接耦合进 Runner，应通过通用 step result contract。
7. 不要用全局进程名清理替代 process group 清理。
8. 不要以提取函数、创建类或消除重复本身作为重构目标；以执行可靠性和阅读连续性为准。
