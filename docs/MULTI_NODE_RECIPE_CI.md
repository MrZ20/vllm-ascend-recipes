# Multi-node Recipe CI

第一阶段从手工维护的 YAML 中间态开始，不定义 Recipe 文档如何转换为该中间态，也不
接入 Kubernetes。当前目标是先固定职责边界，打通两台物理机上的服务、网关、检查和
可选评测生命周期。

## 目录与职责

```text
configs/recipe_ci/plans/<case>/
├── plan.yaml
├── hosts.example.yaml
├── nodes/
│   ├── prefill/
│   │   ├── run.sh
│   │   └── run_dp_template.sh
│   └── decode/
│       ├── run.sh
│       └── run_dp_template.sh
├── gateway/run.sh
├── checks/completion.sh
└── evaluations/
    ├── accuracy.sh
    └── performance.sh

scripts/recipe_ci/
├── plan.py
├── coordinator.py
├── runner.py
└── run.sh
```

- `plan.yaml` 是未来 Recipe 转换器的输出，目前手工维护。它只串联节点脚本、就绪条件、
  网关、检查和评测，不保存 Kubernetes、Runner 标签、本机 IP 或 vLLM 专属参数。
- `hosts.yaml` 是某次运行的基础设施输入，只保存节点 IP 和通信网卡，不属于 Recipe，
  不提交真实集群地址。
- 每个节点拥有独立的 `run.sh` 和 `run_dp_template.sh`。DP/TP、rank、KV Connector 和
  服务环境变量都留在对应节点目录。
- `gateway/` 负责 P/D 后端组装。没有显式指定部署节点时，Runner 在第一个节点上启动
  gateway。
- `checks/` 和 `evaluations/` 只消费 `RECIPE_ENDPOINT`，不参与服务进程展开。
- `runner.py` 只负责生命周期、环境注入、日志、协调和清理，不解释 Recipe，也不复刻
  vLLM Ascend 的进程启动器。

新增用例只需增加一个自包含目录。修改某类服务的启动参数只改相应节点模板；修改请求
只改 checks；修改评测只改 evaluations。

## 中间态示例

```yaml
api_version: recipe-ci/v1
kind: MultiNodePlan

metadata:
  name: example-pd-2n2c

model:
  id: example/model
  served_name: example

nodes:
  - id: prefill
    launch: nodes/prefill/run.sh
    readiness:
      port_start: 7100
      count: 2
  - id: decode
    launch: nodes/decode/run.sh
    readiness:
      port_start: 7100
      count: 2

gateway:
  launch: gateway/run.sh
  port: 38085

checks:
  - id: completion
    script: checks/completion.sh

evaluations:
  accuracy:
    - id: gsm8k
      script: evaluations/accuracy.sh
```

基础设施默认值不在每个 plan 重复：第一个节点是 HTTP 协调 leader，gateway 默认也在
该节点；协调端口默认 `29599`；健康路径分别默认 `/health` 和 `/healthcheck`；启动及
执行超时、artifact 根目录由 Runner 提供默认值并允许 CLI 覆盖。控制面 leader 只负责
协调，不等同于 vLLM 的 DP master；DP 地址仍由每个节点脚本明确设置。

## 运行时依赖边界

Recipe CI 从 recipes 仓库根目录执行。vLLM Ascend 镜像负责提供 vLLM、vllm_ascend 包
和与该版本匹配的完整源码树，默认位置是：

```text
/vllm-workspace/
├── vllm-ascend/
└── vllm-ascend-recipes/
```

Runner 按 `--vllm-ascend-root`、`VLLM_ASCEND_ROOT`、
`/vllm-workspace/vllm-ascend` 的顺序解析上游源码根目录，再向节点脚本注入
`RECIPE_VLLM_ASCEND_ROOT`。当前运行契约要求镜像提供：

- `vllm` 命令和 `vllm_ascend` Python 包；
- `examples/external_online_dp/launch_online_dp.py`；
- `examples/external_online_dp/dp_load_balance_proxy_server.py`；
- `examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py`。

这些 example 不随普通 Python 包安装稳定交付，因此当前仓库不复制、也不在运行时
下载它们；使用镜像内同版本源码可以避免工具与安装包版本错配。

## 执行流程

```text
各节点执行自己的 nodes/<node>/run.sh
                 ↓
各节点等待本机全部后端 /health
                 ↓
各节点向 leader 的 HTTP 协调器上报 ready
                 ↓
leader 等待所有节点 ready，启动 gateway/run.sh
                 ↓
leader 等待 gateway /healthcheck
                 ↓
leader 依次运行 checks
                 ↓
leader 按 --evaluation 运行 accuracy/performance
                 ↓
发布结果，各节点收集日志并清理自己的进程
```

HTTP 协调只依赖节点互通，既不要求共享文件系统，也不要求本地运行依赖 Kubernetes。
协调实现被隔离在 `coordinator.py`；未来若 CI 保证共享目录，可以新增同职责实现，不需
修改 plan、节点脚本或检查脚本。

Runner 不执行 `unset http_proxy` 等操作。内部协调请求显式绕过代理，并把集群 IP 加入
子进程的 `NO_PROXY`，但保留用户原有代理配置，避免把某个 Recipe 文档的局部步骤变成
全局行为。

## 当前用例

`configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/` 是双节点、每节点两卡的 P/D 用例。
它直接复用镜像内的上游 launcher 和 proxy，覆盖 curl completion，并保留可选 AISBench
accuracy/performance 阶段。镜像启动、clone、hosts 填写和双节点命令见该目录 README。

`configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/` 使用相同的物理规模验证普通 external
DP。node0 提供 rank 0–1，node1 提供 rank 2–3，四个 rank 组成一个全局 DP group，
并由普通 DP Proxy 暴露统一入口。新增该用例不需要修改 Runner 生命周期。

## 当前明确不做

- Recipe YAML 到 `plan.yaml` 的转换和最终 Recipe 文档结构；
- `configs/recipe_ci/plans/deepseek-v4-flash-a3-pd` 等现有 Recipe 的迁移；
- Kubernetes、LeaderWorkerSet、PVC 和 GitHub 多节点工作流；
- 共享文件协调实现；
- 完整 schema、认证、高可用、断点恢复和大范围防御性校验。

第一阶段只保留主链路所需的字段校验、超时、失败传播和进程清理，优先确保代码架构
清晰、可读并可由小规模用例验证。
