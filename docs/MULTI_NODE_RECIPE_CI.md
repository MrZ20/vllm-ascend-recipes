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
- node id 和 `nodes/<id>/` 统一按执行实例编号为 `node0`、`node1`、`node2`；`role`
  单独描述 `prefill`、`decode`、`api`、`headless`。列表第一项决定默认控制 leader，
  节点编号不等同于服务角色。
- 每个节点拥有独立的 `run.sh`；需要上游 launcher 的用例再在节点目录提供
  `run_dp_template.sh`。DP/TP、rank、KV Connector 和服务环境变量都留在对应节点目录。
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
  - id: node0
    role: prefill
    launch: nodes/node0/run.sh
    readiness:
      port_start: 7100
      count: 2
  - id: node1
    role: decode
    launch: nodes/node1/run.sh
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

不暴露 HTTP 服务的 headless 节点可以省略 `readiness`。没有 gateway 时，第一个节点
必须保留 HTTP readiness，因为它同时定义统一的检查和评测入口。

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
`RECIPE_VLLM_ASCEND_ROOT`。公共运行契约要求镜像提供：

- `vllm` 命令和 `vllm_ascend` Python 包；
- 与镜像安装版本对应的 vLLM Ascend 源码目录。

P/D 用例还要求 `examples/external_online_dp/launch_online_dp.py` 和对应的 proxy
example。Runner 只校验上游源码根目录，具体脚本依赖由消费它的节点或 gateway 脚本
负责，避免无关工具缺失时阻断其他用例。

这些 example 不随普通 Python 包安装稳定交付，因此当前仓库不复制、也不在运行时
下载它们；使用镜像内同版本源码可以避免工具与安装包版本错配。

### AISBench

`quay.io/ascend/vllm-ascend:nightly-<branch>-a3` 是运行时基础镜像，本身不包含
AISBench。vLLM Ascend 的 `Dockerfile.nightly.a3` 以它为基础继续构建
`nightly-ci-<branch>-a3` 测试镜像，并固定安装 `v3.1-20260609-master`。因此在运行时
nightly 镜像中找不到 `ais_bench` 不是漏打包。

当前仓库把官方 Dockerfile 中的安装过程整理为显式的一次性准备脚本。它同样使用固定
tag，并默认安装到上游源码目录的 `benchmark/`：

```bash
cd /vllm-workspace/vllm-ascend-recipes
scripts/recipe_ci/install_aisbench.sh
```

可以通过环境变量覆盖官方默认值，例如使用镜像已配置的 PyPI 源或镜像仓库：

```bash
export AIS_BENCH_TAG=v3.1-20260609-master
export AIS_BENCH_URL=https://github.com/AISBench/benchmark.git
export PIP_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
scripts/recipe_ci/install_aisbench.sh
```

安装脚本有意复现上游 CI 镜像的系统 Python 安装方式，而不是安装移动中的 `master`。
应在干净容器中只安装一次固定 tag；当前默认方式和 vLLM Ascend nightly CI 保持一致。
隔离虚拟环境仅作为本地可选方式：

```bash
export AIS_BENCH_VENV=/vllm-workspace/aisbench-venv
scripts/recipe_ci/install_aisbench.sh
export RECIPE_AISBENCH_BIN=$AIS_BENCH_VENV/bin/ais_bench
```

这仍使用相同的固定源码 tag 和依赖文件，只改变 Python 安装位置；不是当前 vLLM
Ascend nightly CI 的默认方式。

GSM8K 按 AISBench 官方数据集说明部署到工具源码根目录：

```bash
cd /vllm-workspace/vllm-ascend/benchmark/ais_bench/datasets
curl -LO http://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/gsm8k.zip
unzip gsm8k.zip
```

每个 plan 自带 `aisbench/models/vllm_api_general_chat.py` 和
`vllm_api_stream_chat.py`，分别供精度和性能评测使用。它们是未来 Recipe 转换生成的
请求配置，从 Runner 注入的 endpoint host/port、`RECIPE_MODEL_PATH` 和
`RECIPE_SERVED_MODEL_NAME` 读取当前服务，不修改 AISBench 安装目录中的默认模型配置。
精度和性能脚本分别用
`RECIPE_AISBENCH_ACCURACY_NUM_PROMPTS`、`RECIPE_AISBENCH_PERFORMANCE_NUM_PROMPTS`
控制截取的样本数。

## 执行流程

```text
各节点执行自己的 nodes/<node>/run.sh
                 ↓
配置了 readiness 的节点等待本机 HTTP /health
                 ↓
各节点向 leader 的 HTTP 协调器上报 ready
                 ↓
leader 等待所有节点 ready，按 plan 可选启动 gateway/run.sh
                 ↓
有 gateway 时等待其 /healthcheck
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

运行前设置的 `ASCEND_RT_VISIBLE_DEVICES` 是物理卡候选列表。使用上游 launcher 的 P/D
用例会把从 0 开始的逻辑设备索引映射到候选列表。例如设置 `4,5,6,7` 时，当前每节点
两卡 P/D 用例实际使用物理卡 4、5，而不会误用 0、1。内置 DP 用例直接把整个可见卡
列表交给 `vllm serve`。

## 当前用例

`configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/` 是双节点、每节点两卡的 P/D 用例。
它直接复用镜像内的上游 launcher 和 proxy，覆盖 curl completion，并保留可选 AISBench
accuracy/performance 阶段。镜像启动、clone、hosts 填写和双节点命令见该目录 README。

`configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/` 使用相同的物理规模验证普通内置 DP。
`api` 提供 HTTP 服务和 rank 0–1，`headless` 提供 rank 2–3，四个 rank 组成一个全局
DP group。社区已经删除未使用的普通 DP 示例 Proxy，因此该用例直接采用 vLLM 当前
文档中的多节点内置负载均衡方式。

## 当前明确不做

- Recipe YAML 到 `plan.yaml` 的转换和最终 Recipe 文档结构；
- `configs/recipe_ci/plans/deepseek-v4-flash-a3-pd` 等现有 Recipe 的迁移；
- Kubernetes、LeaderWorkerSet、PVC 和 GitHub 多节点工作流；
- 共享文件协调实现；
- 完整 schema、认证、高可用、断点恢复和大范围防御性校验。

第一阶段只保留主链路所需的字段校验、超时、失败传播和进程清理，优先确保代码架构
清晰、可读并可由小规模用例验证。
