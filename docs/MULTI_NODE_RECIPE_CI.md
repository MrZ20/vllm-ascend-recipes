# Multi-node Recipe CI

该框架从手工维护的 `plan.yaml` 中间态开始。Recipe 文档到中间态的转换和自动拓扑生成
不在当前范围内。第二阶段最终目标用例是 `deepseek-v4-flash-a2-pd-reduced` 双节点 P/D
分离；资源调度验证阶段先运行 `deepseek-v2-lite-pd-2n2c` 双节点四卡用例。GitHub Actions
的基础设施层复用 vLLM Ascend 已有的 Kubernetes
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
├── plan.py          # v1 中间态与 hosts 校验
├── coordinator.py   # 本地/LWS 共用的轻量 HTTP 协调
├── process.py       # 进程组、信号与清理
├── result.py        # 最小结果合同与原子 JSON
├── runner.py        # 线性节点生命周期
├── aisbench.py      # 固定 AISBench 版本的 preflight 与指标转换
├── install_aisbench.sh  # CI 启动 LWS 前准备共享缓存
├── k8s/lws.yaml.jinja2  # N 个 plan 指定卡数的 A2 Pod 和共享卷
└── run.sh           # 本地与 LWS 共用的唯一节点入口
```

- `plan.yaml` 串联节点脚本、readiness、gateway、check 和 evaluation，并通过
  `resources.npu_per_node` 声明每个节点需要的 NPU 数量。
- `model.cache_path` 保存模型缓存根目录下的相对路径；Runner 将它与固定根目录
  `/root/.cache/modelscope/hub/models` 拼接，不从 Workflow 接收模型绝对路径。
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

v1 使用严格 schema，未知字段直接报错。当前格式仍在测试阶段，合并为稳定契约后再执行
版本兼容规则。

- 至少两个节点，按列表位置连续命名为 `node0...nodeN`。
- `resources.npu_per_node` 是正整数，由 K8s 执行层转换为每个 Pod 的 NPU request/limit。
- `role` 必填但可重复；每个节点必须引用不同的 plan 内普通文件。
- 所有 launch/check/evaluation 路径及 symlink 最终目标都必须留在 plan 目录。
- metadata 和 step id 使用安全 slug，同一 stage 的 step id 不得重复。
- v1 只接受 IPv4 hosts。
- `node0` 永远是控制 leader；gateway 若存在，只在 leader 启动。
- 无 gateway 时，leader 必须有 readiness，第一个 readiness 端口就是统一 endpoint。
- gateway 端口不得与 leader 本机 readiness 端口范围冲突。

验证 plan 不会检查模型、NPU 或启动进程：

```bash
RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v4-flash-a2-pd-reduced/plan.yaml \
RECIPE_CI_VALIDATE_ONLY=true scripts/recipe_ci/run.sh
```

`validate-only` 不检查 NPU、模型或 cluster IP，也不启动进程。

## 运行时契约

本地与 LWS 都只执行 `scripts/recipe_ci/run.sh`，Runner 的 Python CLI 是该脚本的
内部实现，不作为第二套公开入口。

共同必填输入：

```text
RECIPE_CI_PLAN          plan.yaml 路径
LWS_WORKER_INDEX        当前节点序号，0...N
```

本地执行额外手动设置：

```text
RECIPE_CI_CLUSTER_IPS   按 node0...nodeN 排列的逗号分隔 IP
RECIPE_CI_INTERFACE     当前机器用于节点通信的网卡（可选）
ASCEND_RT_VISIBLE_DEVICES  当前机器的可用卡
```

LWS 自动注入 `LWS_WORKER_INDEX` 和 `LWS_LEADER_ADDRESS`；Device Plugin 注入实际分配的
NPU 列表。`run.sh` 在未提供
`RECIPE_CI_CLUSTER_IPS` 时等待所有 LWS Pod 注册 DNS，再生成相同的 IP 列表，避免
leader 先于 worker 调度完成时提前退出。
Workflow 在启动 LWS 前准备 AISBench，并向 Pod 注入 `RECIPE_AISBENCH_ROOT` 和
`RECIPE_AISBENCH_BIN`。本地模式使用已有安装时显式设置这两个变量；节点运行期间不会
clone 或安装 AISBench。Runner 不接受外部 evaluation 选择，plan 中声明的 accuracy 和
performance 步骤都会按顺序执行。

本地镜像中可将 recipes 仓库与 vLLM Ascend 源码放在同级目录：

```text
/vllm-workspace/
├── vllm-ascend/          # 镜像自带，与安装包版本一致
└── vllm-ascend-recipes/  # CI checkout、挂载或本地 clone
```

主流程从 recipes 根目录执行。`VLLM_ASCEND_ROOT` 默认是镜像中的
`/vllm-workspace/vllm-ascend`，非标准镜像可显式覆盖。该路径注入为
`RECIPE_VLLM_ASCEND_ROOT`。只有实际引用上游
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

Coordinator 的运行状态是 `running/passed/failed/cancelled`，节点执行状态是
`pending/ready/passed/failed/cancelled`；`cleaned` 是独立集合，不会覆盖执行结果。
相同请求幂等，terminal 不可改变。客户端只在等待 Leader coordinator 启动时轮询；普通
状态请求失败立即终止，避免通用重试掩盖节点网络故障。

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
│   ├── checks/
│   ├── accuracy/
│   ├── performance/
│   └── node-result.json
├── node1/
└── result.json            # 仅 leader
```

JSON 通过同目录临时文件和原子 replace 写入。leader 的 `result.json` 只包含执行结论、
首个失败、清理错误、coordinator 可见节点状态以及 leader step 结果。本地运行时各
节点自行保留日志；K8s 模式中所有 Pod 直接写入共享 artifact 根目录，控制器再组成 bundle。

## AISBench

固定运行镜像不包含 AISBench。CI controller 在启动 LWS 前显式执行：

```bash
scripts/recipe_ci/install_aisbench.sh
```

脚本固定版本，并按 commit、Python 主次版本、架构和 constraints 摘要生成共享 PVC
缓存键：

```text
tag:    v3.1-20260609-master
commit: 0da56eadb2ac85c31c2540f4f5b69af3ec5717a5
```

```text
/root/.cache/recipe-ci/tools/aisbench/
└── <commit>-py<major.minor>-<arch>-<constraints-hash>/
    ├── source/
    ├── venv/
    └── READY
```

缓存命中时只执行 `ais_bench -h` 校验；未命中时在临时目录安装，验证后在 per-key lock
保护下原子发布。K8s 使用集群 PyPI cache。`run.sh` 只消费最终路径，不承担安装。

轻量双节点 plan 自带 8 条 GSM8K 格式的离线 smoke 数据，并在每个 evaluation 的
step artifact 下构造 AISBench 约定的相对路径：

```text
<step-artifact>/ais_bench/datasets/gsm8k
```

因此这条 CI 不依赖共享 PVC 预置或在线下载完整 GSM8K，也不会修改共享的 AISBench
source/cache。每个 plan 分别携带 accuracy 的
`vllm_api_general_chat.py` 和 performance 的
`vllm_api_stream_chat.py`。evaluation 将其中少量运行时占位符渲染到当前 step 的 artifact
目录，再交给 AISBench，避免修改上游安装目录或依赖 MMEngine 的 lazy-import 细节。
controller prepare step 统一执行一次 `ais_bench -h`；evaluation 只检查当前命令、模型
配置和数据集路径并创建 step artifact。AISBench wrapper 从固定的 3.1 产物结构提取指标并写
`RECIPE_STEP_RESULT_FILE`；Runner 不解析 AISBench 私有日志。

当前手工中间态固定少量样本，属于流程 smoke。未来 Recipe 若声明 accuracy gate，转换器应
把 baseline 和 tolerance 直接生成到 evaluation 命令中，不在最终中间态通过兼容环境变量
覆盖。

performance 结果至少包含 TTFT、TPOT、E2E latency、output token/s 和 request/s。

## DeepSeek V4 双节点 A2 缩减用例

`configs/recipe_ci/plans/deepseek-v4-flash-a2-pd-reduced/` 是 A2 双节点 CI 中间态。
它使用一个 Prefill 和一个 Decode 节点验证 P/D 主链路，不代表 Recipe 的
完整 4P4D 性能拓扑：

```text
node0 prefill: DP8 x TP1，8 A2 NPU，7100-7107
node1 decode:  DP8 x TP1，8 A2 NPU，7100-7107
node0 gateway: 38085
```

本地与 LWS 使用同一个环境变量契约。两台机器准备相同的镜像、仓库 commit、模型路径，
并按 `node0...nodeN` 顺序设置相同的 cluster IP 列表。node0 示例：

```bash
export RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v4-flash-a2-pd-reduced/plan.yaml
export VLLM_ASCEND_ROOT=/vllm-workspace/vllm-ascend
export RECIPE_CI_CLUSTER_IPS="<node0_ip>,<node1_ip>"
export RECIPE_CI_INTERFACE="<local_interface>"
export LWS_WORKER_INDEX=0
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7
scripts/recipe_ci/run.sh
```

另一台只把 `LWS_WORKER_INDEX` 改为 `1`，并按本机空闲卡设置
`ASCEND_RT_VISIBLE_DEVICES`。本地显式提供 `RECIPE_CI_CLUSTER_IPS`；CI 未提供时，同一个
`run.sh` 从 `LWS_LEADER_ADDRESS` 解析所有 Pod IP。其余 NPU 检查、临时 hosts、AISBench、
artifact/plog、信号和 Runner 生命周期完全相同。

## Qwen3.5-27B 双节点 A2 缩减用例

`configs/recipe_ci/plans/qwen3.5-27b-a2-pd-reduced/` 参考官方 Qwen3.5-27B
多节点 P/D 方案。官方 A3 拓扑为每节点 `DP8 x TP2 = 16 NPU`；当前用例保持 TP2，按
A2 每节点 8 卡缩减为 `DP4 x TP2`。它尚未加入 PR 矩阵，需先确认共享 PVC 中存在
`Eco-Tech/Qwen3.5-27B-w8a8-mtp` 并完成真实双节点验证。

## Pull request 与手动 GitHub Actions workflow

workflow 分成选择用例和执行机制两层：

- `.github/workflows/recipe_verify_multi_node.yaml` 同时响应 `pull_request` 和
  `workflow_dispatch`，矩阵中只保存需要验证的 plan 路径。资源调度验证期间矩阵只有
  `configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/plan.yaml`；新增用例只需追加一行。
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
  -> 每个节点把退出码写入共享 PVC，controller 收齐后删除 LWS
  -> Pod 写完退出码后保持运行，避免 CCE LWS 的容器重启策略吞掉完成状态
  -> 从 PVC 收集 Runner artifact、Pod 日志和 Ascend plog 后上传
```

LWS 的 leader 和每个 worker 按 `plan.resources.npu_per_node` 申请集群实际注册的
`huawei.com/ascend-1980`，并沿用 vLLM Ascend nightly 的 `dedicated=night` toleration。
当前 a2b4 CI 执行层通过 `node.kubernetes.io/npu.chip.name=910B4` 选择同构节点；
该基础设施约束不进入 recipe plan。相同 run 的 Pod 通过 hostname 反亲和强制分散到
不同物理机。Pod 使用同一份
Mooncake-enabled A2 镜像和 PVC 暂存源码。K8s 决定节点地址和设备分配，因此 workflow 不再保存逐节点
runner label、物理 IP、网卡或 `ASCEND_RT_VISIBLE_DEVICES`。Pod 入口脚本读取 Device
Plugin 注入的 `ASCEND_VISIBLE_DEVICES`，再交给 plan-local launcher 使用。
由于 Pod 使用 `hostNetwork`，模板同时设置 `dnsPolicy: ClusterFirstWithHostNet`，确保
`LWS_LEADER_ADDRESS` 和同组 worker DNS 能通过集群 DNS 解析。

当前测试集群的 controller runner、并发资源组、namespace、PVC 名称、镜像以及启动和运行
超时都固定在 reusable workflow 中，不要求额外创建 GitHub Repository Variables。模型来自
挂载到 `/root/.cache` 的 PVC。Runner 按下面的规则获得路径：

```text
/root/.cache/modelscope/hub/models + plan.model.cache_path
```

例如当前调度验证 plan 的 `model.cache_path` 是
`vllm-ascend/DeepSeek-V2-Lite-W8A8`。模型路径不是凭据，因此不使用 Workflow input、
Variable 或 Secret。CI 管理员只需配置真正敏感的
`KUBECONFIG_B64` Secret。基础镜像必须包含 `/opt/vllm-ascend` 及 Mooncake runtime；recipes
源码由 controller 暂存，不在 Pod 中联网 clone。AISBench 由 controller 在 LWS 启动前
准备到共享 PVC，所有 Pod 使用同一固定路径；当前轻量 plan 的 GSM8K smoke 数据随源码暂存。

artifact bundle 同时通过 `ascend-gha-runners/artifact/upload@v0.3` 上传 OBS，并通过
`actions/upload-artifact` 上传 GitHub。self-hosted runner 到 GitHub Artifact Storage 的
连通性并不稳定，因此保留两个后端；上传失败只写 warning，不覆盖模型测试结论。

PR 使用集群 Kubeconfig 并执行 PR 中的脚本，因此只运行同仓库分支创建的 PR；fork PR 会跳过
集群 job，避免向不受信任的 fork 暴露凭据。当前不接入 nightly 自动触发。

## 当前不做

- Recipe YAML 到 plan 的正式转换器；
- 共享文件协调和独立 coordinator 服务；
- Runner 自动推导 P/D、DP rank、KV Connector 或 gateway backend；
- 复制 vLLM Ascend examples 或维护 AISBench fork；
- 三节点、四节点 fixture 和真实回归；
- nightly 自动多节点触发；
- 自动下载大型模型和完整数据集。
