# Multi-node Recipe CI

该框架从手工维护的 `plan.yaml` 中间态开始。Recipe 文档到中间态的转换和自动拓扑生成
不在当前范围内。第二阶段的真实 CI 目标用例是 `deepseek-v4-flash-a3-pd` 双节点 P/D
分离；GitHub Actions 的基础设施层复用 vLLM Ascend 已有的 Kubernetes
`LeaderWorkerSet`（LWS）方案。

## 职责边界

```text
configs/recipe_ci/plans/<case>/
├── plan.yaml
├── nodes/
│   ├── node0/
│   │   ├── run.sh
│   │   └── run_dp_template.sh
│   └── node1/
├── gateway/run.sh
├── checks/completion.sh
├── evaluations/
└── aisbench/models/

scripts/recipe_ci/
├── plan.py          # 严格 schema 与 hosts 校验
├── coordinator.py   # 轻量 HTTP 状态机
├── process.py       # 进程组、信号、日志尾部与清理
├── result.py        # 结构化结果与原子 JSON
├── runner.py        # 线性节点生命周期
├── aisbench.py      # AISBench preflight 与指标转换
├── k8s/lws.yaml.jinja2  # N 个 16 卡 A3 Pod 和共享卷
└── run.sh           # 本地与 LWS 共用的唯一节点入口
```

- `plan.yaml` 只串联节点脚本、readiness、gateway、check 和 evaluation。
- `run.sh` 根据 `RECIPE_CI_CLUSTER_IPS` 或 LWS DNS 生成临时 `hosts.yaml`；真实地址不进入
  plan 或仓库。
- 节点严格按顺序命名为 `node0...nodeN`，`role` 只用于描述和环境变量。`node0` 是控制
  leader，但不自动成为 DP master、Prefill 或 API 节点。
- DP/TP、rank、端口、KV Connector、服务环境变量和 gateway backend 由 plan-local
  脚本显式表达。Runner 不理解 Prefill/Decode，也不复刻 vLLM launcher。
- 每个节点有自己的启动脚本和模板。显式重复便于直接审查不同节点的关键参数，未来由
  Recipe 转换器生成，而不是在本阶段抽成难以追踪的 shell 工具。
- Coordinator 只传输状态，不传输日志。本地模式的 artifact 留在各节点；K8s 模式把
  artifact 写入共享 PVC，由控制器 Job 在 LWS 结束后统一上传。PVC 不参与 Runner 协调。

## `recipe-ci/v1` 契约

v1 使用严格 schema，未知字段直接报错。v1 内只接受不改变执行语义的修复；新增会改变
执行语义的字段时升级为 `recipe-ci/v2`。

- 至少两个节点，按列表位置连续命名为 `node0...nodeN`。
- `role` 必填但可重复；每个节点必须引用不同的 plan 内普通文件。
- 所有 launch/check/evaluation 路径及 symlink 最终目标都必须留在 plan 目录。
- metadata 和 step id 使用安全 slug，同一 stage 的 step id 不得重复。
- v1 只接受 IPv4 hosts。
- `node0` 永远是控制 leader；gateway 若存在，只在 leader 启动。
- 无 gateway 时，leader 必须有 readiness，第一个 readiness 端口就是统一 endpoint。
- gateway 端口不得与 leader 本机 readiness 端口范围冲突。

验证 plan 不会检查模型、NPU 或启动进程：

```bash
RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/plan.yaml \
RECIPE_CI_VALIDATE_ONLY=true scripts/recipe_ci/run.sh
```

`validate-only` 不检查 NPU、模型或 cluster IP，也不启动进程。

## 运行时契约

本地与 LWS 都只执行 `scripts/recipe_ci/run.sh`，Runner 的 Python CLI 是该脚本的
内部实现，不作为第二套公开入口。

共同必填输入：

```text
RECIPE_CI_PLAN          plan.yaml 路径
RECIPE_CI_MODEL_PATH    容器内模型路径
LWS_WORKER_INDEX        当前节点序号，0...N
```

本地执行额外手动设置：

```text
RECIPE_CI_CLUSTER_IPS   按 node0...nodeN 排列的逗号分隔 IP
RECIPE_CI_INTERFACE     当前机器用于节点通信的网卡（可选）
ASCEND_RT_VISIBLE_DEVICES  当前机器的可用卡
```

LWS 自动注入 `LWS_WORKER_INDEX` 和 `LWS_LEADER_ADDRESS`；Workflow 注入 plan、
模型、可见逻辑设备等其余输入。`run.sh` 在未提供
`RECIPE_CI_CLUSTER_IPS` 时通过 LWS DNS 生成相同的 IP 列表。
Workflow 还显式设置 `RECIPE_CI_INSTALL_AISBENCH=true`；本地可以预先执行
`install_aisbench.sh` 或按需设置该变量。普通自定义 evaluation 不会被入口脚本
自动当作 AISBench。

推荐在 vLLM Ascend 镜像中将 recipes 仓库放在同级目录：

```text
/vllm-workspace/
├── vllm-ascend/          # 镜像自带，与安装包版本一致
└── vllm-ascend-recipes/  # CI checkout、挂载或本地 clone
```

主流程从 recipes 根目录执行。`VLLM_ASCEND_ROOT` 默认是
`/vllm-workspace/vllm-ascend`，并注入为 `RECIPE_VLLM_ASCEND_ROOT`。只有实际引用上游
example 的 plan 才需要：

```text
examples/external_online_dp/launch_online_dp.py
examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

这些工具跟随镜像中的 vLLM Ascend 源码使用，不复制进 recipes 仓库，也不在每次运行时
下载。Runner 不清空代理变量；内部协调 HTTP 显式绕过代理，并把集群 IP 加入
`NO_PROXY`。

Runner 注入的主要环境变量：

```text
RECIPE_NODE_ID / RECIPE_NODE_INDEX / RECIPE_NODE_ROLE
RECIPE_LOCAL_IP / RECIPE_LOCAL_INTERFACE / RECIPE_LEADER_IP
RECIPE_NODE_0_IP / RECIPE_NODE_1_IP / ...
RECIPE_MODEL_PATH / RECIPE_SERVED_MODEL_NAME
RECIPE_ENDPOINT / RECIPE_ENDPOINT_HOST / RECIPE_ENDPOINT_PORT
RECIPE_ARTIFACT_ROOT / RECIPE_NODE_ARTIFACT_DIR
RECIPE_STEP_ARTIFACT_DIR / RECIPE_STEP_RESULT_FILE
```

`RECIPE_ARTIFACT_DIR` 暂时作为 v1 兼容别名指向当前 stage 目录。

## 生命周期与失败保证

```text
启动本节点 service
  -> 本机 readiness
  -> 上报 ready
  -> leader 等待全部 ready
  -> leader 启动并等待 gateway
  -> supervised checks
  -> supervised evaluations
  -> 发布 passed / failed / cancelled
  -> 每节点清理自己的受管进程组
  -> 写 node-result.json
  -> 上报 cleaned
  -> leader 等待全部 cleaned 并写 result.json
```

check 和 evaluation 都在独立 process group 中运行。等待长步骤期间，Runner 会持续检查：

- step 是否退出或超时；
- 本机 service/gateway 是否异常退出；
- coordinator 是否收到远端失败；
- 是否收到 SIGINT/SIGTERM。

清理仅针对本次 Runner 创建的 process group，顺序为 SIGTERM、有限等待、SIGKILL、关闭
日志和存活验证，不使用 `pkill` 或 `killall`。第一个执行错误保存为 `primary_failure`，
清理错误单独进入 `cleanup_errors`，不会覆盖原始原因。

Coordinator 的运行状态是 `running/passed/failed/cancelled`，节点状态是
`pending/ready/failed/cleaned`。相同请求幂等，terminal 不可改变。`terminal` 表示执行
结果已确定；`cleaned` 表示该节点已经清理进程、关闭日志并写完本地结果，两者不能混用。

leader 被 SIGINT/SIGTERM 时可先发布 `cancelled`。leader 被 SIGKILL、机器掉电或容器被
强制删除时无法主动发布失败；worker 会在 coordinator 连续不可达超过有限 grace period
后产生 `coordinator_unreachable`、清理本机并写本地结果。这是无外部高可用协调服务时
能够提供的实际保证。

## Result 与 artifact

每个节点本地生成：

```text
artifacts/<plan>/
├── node0/
│   ├── service.log
│   ├── gateway.log
│   ├── coordinator.log
│   ├── checks/
│   ├── accuracy/
│   ├── performance/
│   ├── environment.json
│   └── node-result.json
├── node1/
└── result.json            # 仅 leader
```

JSON 通过同目录临时文件、fsync 和原子 replace 写入。`environment.json` 只保存 Python、
平台、明确允许的软件版本、镜像标识和 commit，不 dump 全部环境变量。leader 的
`result.json` 只承诺包含 coordinator 可见的节点状态和 leader step 结果。本地运行时各
节点自行保留日志；K8s 模式中所有 Pod 直接写入共享 artifact 根目录，控制器再组成 bundle。

## AISBench

vLLM Ascend 的 A3 CI Dockerfile 在 `/vllm-workspace/vllm-ascend/benchmark` 安装固定
tag。普通运行时镜像不一定包含它，可显式执行：

```bash
scripts/recipe_ci/install_aisbench.sh
```

默认固定：

```text
tag:    v3.1-20260609-master
commit: 0da56eadb2ac85c31c2540f4f5b69af3ec5717a5
```

目录已是正确 remote、commit、tracked-clean 且 `ais_bench -h` 成功时重复执行不会安装。
版本不一致默认失败，只有显式 `--force-reinstall` 才替换。脚本尊重已有 pip 配置，不写死
镜像源。可用 `AIS_BENCH_VENV` 安装到独立虚拟环境；这能避免本地长期环境被 AISBench
依赖约束影响，而 nightly CI 仍可沿用其干净镜像中的系统 Python 安装方式。

GSM8K 数据集按 AISBench 约定放在：

```text
/vllm-workspace/vllm-ascend/benchmark/ais_bench/datasets/gsm8k
```

每个 plan 分别携带 accuracy 的 `vllm_api_general_chat.py` 和 performance 的
`vllm_api_stream_chat.py`。evaluation 正式运行前检查命令、`-h`、模型配置可加载、数据集
目录、endpoint 环境和 artifact 可写性。AISBench wrapper 从产物提取指标并写
`RECIPE_STEP_RESULT_FILE`；Runner 不解析 AISBench 私有日志。

默认少量样本是流程 smoke，只验证请求和产物。设置以下变量才启用 accuracy gate：

```bash
export RECIPE_AISBENCH_ACCURACY_BASELINE=80
export RECIPE_AISBENCH_ACCURACY_ALLOWED_DROP=2
```

baseline 与 tolerance 使用 AISBench summary CSV 的原始 score 单位，不由 wrapper 归一化。

performance 结果至少包含 TTFT、TPOT、E2E latency、output token/s 和 request/s。

## DeepSeek V4 双节点 A3 用例

`configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/` 是
`models/en/DeepSeek/DeepSeek-V4-Flash.yaml` 的 A3 1P1D 手工中间态：

```text
node0 prefill: DP4 x TP4，16 NPU，7100-7103
node1 decode:  DP16 x TP1，16 NPU，7100-7115
node0 gateway: 38085
```

本地与 LWS 使用同一个环境变量契约。两台机器准备相同的镜像、仓库 commit、模型路径，
并按 `node0...nodeN` 顺序设置相同的 cluster IP 列表。node0 示例：

```bash
export RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/plan.yaml
export RECIPE_CI_MODEL_PATH=/models/DeepSeek-V4-Flash-w8a8-mtp
export RECIPE_CI_CLUSTER_IPS="<node0_ip>,<node1_ip>"
export RECIPE_CI_INTERFACE="<local_interface>"
export LWS_WORKER_INDEX=0
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
export RECIPE_CI_EVALUATION=none
scripts/recipe_ci/run.sh
```

另一台只把 `LWS_WORKER_INDEX` 改为 `1`，并按本机空闲卡设置
`ASCEND_RT_VISIBLE_DEVICES`。本地显式提供 `RECIPE_CI_CLUSTER_IPS`；CI 未提供时，同一个
`run.sh` 从 `LWS_LEADER_ADDRESS` 解析所有 Pod IP。其余 NPU 检查、临时 hosts、AISBench、
artifact/plog、信号和 Runner 生命周期完全相同。

## 手动 GitHub Actions workflow

workflow 分成选择用例和执行机制两层：

- `.github/workflows/recipe_verify_multi_node.yaml` 只提供 `workflow_dispatch` 输入并调用
  reusable workflow；当前默认选择 DeepSeek V4 双节点 plan，但 `plan` 是普通字符串，
  后续可以直接传入其他双节点或多节点 plan。
- `.github/workflows/_recipe_verify_multi_node.yaml` 是 `workflow_call` 执行层，负责解析
  `plan.nodes` 数量、创建和管理 LWS，不包含 DeepSeek、P/D 或固定双节点语义。

执行层沿用 vLLM Ascend 的多节点 K8s 结构：

```text
单个无 NPU Actions controller
  -> checkout 指定 ref（留空则使用 workflow commit），并把同一份源码暂存到共享 PVC
  -> 严格解析 plan，取 node_count = len(plan.nodes)
  -> 渲染、创建 size=node_count 的 LeaderWorkerSet
  -> 所有 Pod 直接运行 scripts/recipe_ci/run.sh
  -> LWS_WORKER_INDEX 映射 node0...nodeN
  -> LWS DNS 生成临时 hosts.yaml
  -> 所有 Runner 继续通过 HTTP coordinator 协调
  -> controller 枚举全部 Pod，流式输出日志、检查退出码并删除 LWS
  -> 从 PVC 收集 Runner artifact、Pod 日志和 Ascend plog 后上传
```

LWS 的 leader 和每个 worker 各申请 `16` 个 `huawei.com/ascend-1980`，使用同一 vLLM Ascend
A3 镜像和同一份暂存源码。K8s 决定节点地址和设备分配，因此 workflow 不再保存逐节点
runner label、物理 IP、网卡或 `ASCEND_RT_VISIBLE_DEVICES`。Pod 入口脚本默认把容器可见
设备表示为逻辑编号 `0..15`，交给 plan-local launcher 使用。

CI 管理员需要配置：

```text
Variable: RECIPE_CI_K8S_CONTROLLER_RUNNER   # 可选，默认 linux-aarch64-a3-0
Variable: RECIPE_CI_A3_RESOURCE_GROUP       # 可选，并发锁对应的 A3 资源组
Variable: RECIPE_CI_A3_PVC_NAME             # 可选，默认沿用 vllm-ascend A3 PVC
Variable: RECIPE_CI_AISBENCH_DATASET_DIR    # 可选，共享卷内数据集路径
Secret:   KUBECONFIG_B64
Secret:   RECIPE_CI_MODEL_PATH
```

`RECIPE_CI_MODEL_PATH` 必须是所有 Pod 均可见的路径，通常位于已挂载的共享 PVC。基础镜像
必须包含 `/vllm-workspace/vllm-ascend`；recipes 源码由 controller 暂存，不在 Pod 中联网
clone。`evaluation != none` 且镜像没有 AISBench 时，仅 node0 调用固定版本安装脚本。

在真实 runner、secret 和容器取消语义验证稳定之前，不接入 PR 或 nightly 自动触发。

## 当前不做

- Recipe YAML 到 plan 的正式转换器；
- 共享文件协调和独立 coordinator 服务；
- Runner 自动推导 P/D、DP rank、KV Connector 或 gateway backend；
- 复制 vLLM Ascend examples 或维护 AISBench fork；
- 三节点、四节点 fixture 和真实回归；
- PR/nightly 自动多节点触发；
- 自动下载大型模型和完整数据集。
