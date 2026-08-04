# Multi-node Recipe CI 开发交接

更新日期：2026-08-04

当前分支：`add_mul_ci`

当前基线提交：`c0dfce5`

> 重要：本文所述修改目前仍在本机工作区，尚未提交或推送。换设备前必须执行
> `git add -A`、提交并推送，或者生成并保存补丁；仅在新设备 clone `add_mul_ci`
> 分支无法获得这些未提交修改。目录重命名在 `git status` 中会暂时表现为旧文件删除和
> 新目录未跟踪，`git add -A` 后 Git 才会识别实际 rename。

## 1. 本阶段目标和范围

本阶段从手工编写的 `configs/recipe_ci/plans/**/plan.yaml` 中间态开始，验证一个清晰、
可维护的多节点执行框架。暂时不实现 Recipe 文档到中间态的转换，因为 Recipe 文档结构
尚未最终确定。

已经确定的边界：

- 不依赖 Kubernetes；本地和 CI 都通过 `hosts.yaml` 显式提供节点 IP 和网卡。
- 不依赖共享文件系统；当前控制面使用 HTTP 协调，后续可在不改变 plan 和节点脚本的
  前提下替换协调实现。
- 使用 vLLM Ascend 镜像作为运行环境。
- recipes 仓库位于 `/vllm-workspace/vllm-ascend-recipes`，从该目录执行 Runner。
- 镜像内 `/vllm-workspace/vllm-ascend` 提供与安装版本一致的上游源码和 example 工具。
- 不把 `launch_online_dp.py`、P/D proxy 等上游脚本复制进 recipes 仓库。
- Runner 不复刻 vLLM 的 rank/进程展开逻辑；每个节点只启动自己的独立 `run.sh`。
- 不在通用流程中执行 `unset http_proxy`、`https_proxy`、`ftp_proxy`。Runner 只把集群
  地址加入 `NO_PROXY`，内部 HTTP 客户端显式绕过代理。

当前阶段只追求主链路清晰可执行，没有加入大量防御性逻辑。幂等安装、复杂 schema
校验、重试策略等属于后续阶段。

## 2. 当前目录和职责

```text
scripts/recipe_ci/
├── run.sh                  # 加载 Ascend 环境并进入 Python Runner
├── runner.py               # 节点生命周期、环境注入、日志、清理
├── coordinator.py          # 多节点 HTTP 协调协议
├── plan.py                 # 中间态和 hosts 数据模型/解析
└── install_aisbench.sh     # 对齐 nightly Dockerfile 的 AISBench 安装步骤

configs/recipe_ci/plans/<case>/
├── plan.yaml               # 手工中间态；未来由 Recipe 转换生成
├── hosts.example.yaml      # 本地运行示例，不保存真实集群地址
├── nodes/
│   ├── node0/
│   │   ├── run.sh
│   │   └── run_dp_template.sh  # 仅使用上游 launcher 的节点需要
│   └── node1/
├── gateway/                # 可选统一入口，例如 P/D proxy
├── checks/                 # curl/smoke 检查
├── evaluations/            # accuracy/performance 执行脚本
└── aisbench/
    └── models/             # AISBench --config-dir 强制要求的 models 分类
        ├── vllm_api_general_chat.py
        └── vllm_api_stream_chat.py
```

`aisbench/models` 两层不是重复目录：evaluation 脚本把
`$RECIPE_PLAN_DIR/aisbench` 传给 `--config-dir`，AISBench 固定从该目录下的 `models/`
查找模型请求配置。保留 `aisbench/` 也为后续的 `datasets/`、`summarizers/` 留出独立
命名空间，避免污染 plan 根目录。

## 3. 中间态约定

节点身份统一按执行实例编号，不使用角色作为主键：

```yaml
nodes:
  - id: node0
    role: prefill
    launch: nodes/node0/run.sh
  - id: node1
    role: decode
    launch: nodes/node1/run.sh
```

三、四节点继续使用 `node2`、`node3`。多个节点可以拥有相同角色：

```yaml
nodes:
  - id: node0
    role: prefill
  - id: node1
    role: decode
  - id: node2
    role: decode
  - id: node3
    role: decode
```

相关规则：

- `id` 是稳定执行实例编号，同时对应 `nodes/<id>/`、`hosts.yaml` key 和 `--node-id`。
- `role` 描述 `prefill`、`decode`、`api`、`headless` 等服务职责。
- Runner 注入 `RECIPE_NODE_ID`、`RECIPE_NODE_INDEX` 和 `RECIPE_NODE_ROLE`。
- Runner 同时注入 `RECIPE_NODE_0_IP`、`RECIPE_NODE_1_IP` 等地址。
- plan 列表中的第一个节点默认是 HTTP 控制 leader；这不等同于 DP master 或服务角色。
- gateway 默认由第一个节点启动。
- headless 节点可以没有 `readiness`；无 gateway 时第一个节点必须有 HTTP readiness，
  因为它定义 checks/evaluations 使用的 endpoint。
- 每个节点必须拥有独立 launch 脚本。服务参数、HCCL/vLLM 环境变量和 rank 设置保留在
  节点脚本中，不进入 Runner。
- plan 不保存真实 IP、Runner label、Kubernetes 字段等基础设施内容。

## 4. Runner 已实现的生命周期

```text
各节点解析同一个 plan 和 hosts
             ↓
第一个节点启动 HTTP coordinator
             ↓
每个节点以 nodes/nodeN 为 cwd 启动自己的 run.sh
             ↓
有 readiness 的节点等待本机全部 /health
             ↓
所有节点向 coordinator 上报 ready
             ↓
leader 可选启动 gateway 并等待 /healthcheck
             ↓
leader 依次执行 checks
             ↓
leader 按 --evaluation 执行 accuracy/performance
             ↓
发布 done/failed，所有节点收集日志并清理各自进程组
```

以节点目录作为工作目录非常重要：上游 `launch_online_dp.py` 会从当前目录读取
`./run_dp_template.sh`。

Runner 当前 CLI：

```text
--plan
--hosts
--node-id
--model-path
--vllm-ascend-root
--control-port                 默认 29599
--startup-timeout-seconds      默认 1800
--run-timeout-seconds          默认 3600
--evaluation                  none/accuracy/performance/all
--artifact-root               默认 /tmp/recipe-ci
--validate-only
```

## 5. 当前两个用例

### 5.1 DeepSeek-V2-Lite P/D 双节点四卡

目录：`configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c`

拓扑：

```text
node0, role=prefill
  launch_online_dp.py 启动 2 个 TP1 Prefill 实例，端口 7100/7101

node1, role=decode
  launch_online_dp.py 启动 2 个 TP1 Decode 实例，端口 7100/7101

node0 gateway
  load_balance_proxy_server_example.py，端口 38085
```

运行镜像必须提供：

```text
/vllm-workspace/vllm-ascend/examples/external_online_dp/launch_online_dp.py
/vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

Prefill/Decode 的 vLLM 参数、KV connector 和环境变量分别位于：

```text
nodes/node0/run_dp_template.sh
nodes/node1/run_dp_template.sh
```

gateway 显式使用 `RECIPE_NODE_0_IP` 作为 Prefill、`RECIPE_NODE_1_IP` 作为 Decode。

### 5.2 Qwen3-30B-A3B 普通内部 DP 双节点四卡

目录：`configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c`

该用例不使用 P/D、不使用外部 DP launcher，也不需要 proxy：

```text
node0, role=api
  API server + DP rank 0/1，data-parallel-size-local=2

node1, role=headless
  headless DP rank 2/3，data-parallel-start-rank=2

全局 DP4、TP1，node0:7100 是唯一 endpoint
```

两个节点都直接执行 `vllm serve`。普通 external DP proxy 示例已经不再作为当前方案；
当前普通 DP 使用 vLLM 内置的多节点 DP 和 API 负载均衡能力。

Qwen MoE 在当前实机镜像中使用图模式 profile 时出现过本地 DP 设备混用，因此当前脚本
与上游 internal-DP 示例对齐，包含：

```text
--enforce-eager
--no-enable-prefix-caching
--max-num-batched-tokens 4096
--gpu-memory-utilization 0.9
```

普通 DP 用例会把整个 `ASCEND_RT_VISIBLE_DEVICES` 交给 vLLM，因此双卡测试时应只设置
两张卡，例如 `4,5`。P/D 用例的 launcher 模板会从候选卡列表中按逻辑 index 选择卡。

## 6. AISBench 方案

### 6.1 安装

`scripts/recipe_ci/install_aisbench.sh` 复现 vLLM Ascend
`.github/workflows/dockerfiles/Dockerfile.nightly.a3` 的主要安装过程：

```text
默认 tag: v3.1-20260609-master
默认源码: https://github.com/AISBench/benchmark.git
默认目录: /vllm-workspace/vllm-ascend/benchmark
安装方式: pip install -e . -r requirements/api.txt -r requirements/extra.txt
```

建议在干净容器中只安装一次固定 tag，不要先安装 moving master 再覆盖。脚本当前不是
幂等的：目标目录已存在时 `git clone` 会失败，这是第二阶段需要补充的防御性能力。

nightly Dockerfile 使用系统 Python 安装，因此本地默认也保持一致。脚本保留可选
`AIS_BENCH_VENV`，但它不是当前推荐主路径。

Dockerfile 构建成功不等于整个基础镜像能通过 `pip check`。当前容器中的 `te`、
`ms-service-profiler`、FastAPI/Starlette/OpenCV metadata 冲突不能全部归因于固定版
AISBench；功能验收以 `ais_bench -h` 和真实小数据集评测为准。

### 6.2 plan-local 配置

每个 plan 自带两个 AISBench 请求配置：

```text
vllm_api_general_chat.py  -> accuracy，stream=False
vllm_api_stream_chat.py   -> performance，stream=True
```

两个配置都读取 Runner 注入的 endpoint、模型路径和 served model。不要重新合并为一个
由 `RECIPE_AISBENCH_STREAM` 切换的文件；分开后更接近 AISBench/nightly 模板，也更适合
作为 Recipe 转换产物审查。

accuracy/performance 脚本通过 `--config-dir "$RECIPE_PLAN_DIR/aisbench"` 使用当前 plan，
不修改 AISBench 安装目录中的默认配置。

GSM8K 数据集需要位于：

```text
/vllm-workspace/vllm-ascend/benchmark/ais_bench/datasets/gsm8k
```

## 7. 已完成验证

### 7.1 本地测试

最后一次本地结果：

```text
.venv/bin/python -m unittest tests.recipe_ci.test_multi_node_ci -v
Ran 7 tests
OK
```

同时通过：

- `bash -n`：两个 plan 的节点、gateway、check、evaluation 脚本。
- `python -m py_compile`：Runner、plan parser 和 AISBench 配置。
- `git diff --check`。

项目约定要求所有本地 Python 测试都在仓库 `.venv` 中执行。

### 7.2 两台 NPU 机器

本轮使用过的环境：

```text
跳板: ssh a3-node0
机器: node1 / node2
容器: zsl_recipe
当时节点 IP: 172.22.0.155 / 172.22.0.188
当时网卡: enp23s0f3
当时使用物理卡: 两台机器各 4,5
```

这些地址和空闲卡只代表当时状态。再次执行前必须重新运行 `npu-smi info`、确认 IP 和
网卡，不能直接复用。

AISBench 在两台容器中安装并验证过：

```text
package version: 3.1.20260609
source commit: 0da56eadb2ac85c31c2540f4f5b69af3ec5717a5
ais_bench -h: passed
```

Qwen 普通内部 DP 已实际端到端通过：

- Gloo rank 0-3 跨两节点连接成功。
- node0 `/health` 返回 200。
- completion 请求成功。
- 两个 Runner 均退出 0 并输出 `plan completed`。
- AISBench accuracy 和 performance 阶段均完成。
- performance 两个样本：E2E 约 3751.8 ms、TTFT 约 251.6 ms、TPOT 约
  112.9 ms、输出吞吐约 8.529 token/s、请求吞吐约 0.2665 req/s。
- accuracy 当时为快速打通流程而设置 `max_out_len=32`，两个 GSM8K 样本得分为 0；
  该结果不能作为模型质量结论。

容器内曾保留的普通 DP 产物：

```text
/tmp/recipe-ci-internal-dp-retry2/qwen3-30b-a3b-dp-2n2c/
```

### 7.3 必须注意的验证边界

最后一次实机成功之后又发生了以下结构调整：

- 所有节点目录统一为 `node0/node1`。
- plan 增加必填 `role`。
- AISBench 从共享配置改为每个 plan 自带两个配置文件。
- evaluation 脚本改为 plan-local `--config-dir`。

这些调整通过了本地回归，但尚未使用最新工作区重新跑两节点 NPU。DeepSeek P/D 用例也
应以当前最终目录和 plan 重新完整验证。换设备后的第一项实机工作应是同时回归 P/D 和
普通 DP，而不能只依赖之前的成功结果。

## 8. 当前 Git 和 Workflow 状态

当前工作区包含大量未提交修改及新文件。高层次内容包括：

- `scripts/recipe_ci/plan.py`、`runner.py` 生命周期和 schema 调整。
- 两个 plan 的节点、gateway、checks、evaluations 和 README。
- 两个 plan 各自的 AISBench model 配置。
- AISBench 安装脚本。
- 测试和 `docs/MULTI_NODE_RECIPE_CI.md`。
- P/D 节点目录从 `prefill/decode` 重命名为 `node0/node1`。
- 普通 DP 删除旧 external launcher/proxy 模板，改为内部 DP。

仓库当前不存在 `.github/workflows/recipe_verify_multi_node.yaml`。现有：

```text
.github/workflows/_recipe_verify.yml
.github/workflows/pr-recipe-verify.yml
.github/workflows/nightly-recipe-verify.yml
```

仍然执行旧的单 Recipe `scripts/verify-recipe.sh` 路径，没有接入
`scripts/recipe_ci/run.sh` 和多节点 plan。不能把当前框架描述为已经接入 GitHub Actions。

## 9. 换设备前和换设备后的操作

### 9.1 当前设备上必须先做

```bash
cd /Users/user/work/MrZ20/vllm-ascend-recipes
git status --short
git diff --check
git add -A
git status --short
git commit -m "Add maintainable multi-node recipe CI runner"
git push origin add_mul_ci
```

提交信息由维护者最终决定。不要只复制本文而遗漏未提交源码。

如果暂时不希望提交，可至少生成补丁并另行保存：

```bash
git diff --binary > multi-node-recipe-ci.patch
```

注意：普通 `git diff` 不包含未跟踪文件；生成补丁前需要先 `git add -N` 或使用 Git 提交
方案。最稳妥的迁移方式仍是提交并推送分支。

### 9.2 新设备续接

```bash
git clone --branch add_mul_ci https://github.com/MrZ20/vllm-ascend-recipes.git
cd vllm-ascend-recipes
git status
```

按仓库开发约定创建/使用本地 `.venv`，安装 PyYAML 等测试依赖，然后运行：

```bash
.venv/bin/python -m unittest tests.recipe_ci.test_multi_node_ci -v
git diff --check
```

实机运行时，容器中的推荐布局为：

```text
/vllm-workspace/
├── vllm-ascend/          # 镜像自带
└── vllm-ascend-recipes/  # clone 当前分支
```

主流程始终从 recipes 仓库执行。每个用例的 hosts 和双节点命令以各自 README 为准。

## 10. 后续任务优先级

### P0：保存当前成果

- 审查 `git diff`。
- `git add -A`，确认目录 rename 和新 AISBench 文件全部纳入。
- 提交并推送 `add_mul_ci`。

### P0：最新代码双节点回归

- 使用最新的 `node0/node1 + role` plan 重新跑 DeepSeek P/D。
- 重新跑 Qwen 普通内部 DP。
- 两个用例都执行 completion、accuracy、performance。
- accuracy 使用足够的 `max_out_len`，把流程 smoke 与真实质量验证分开。
- 确认失败和成功后两台机器都无残留 vLLM/proxy/AISBench 进程。
- 保存完整 artifact 和所用镜像、vLLM、vllm-ascend、AISBench commit 信息。

### P1：接入 CI

- 新增或重构真正的多节点 workflow；不要与当前单节点 `_recipe_verify.yml` 混为一谈。
- 明确两个 self-hosted runner 如何获得相同 plan、hosts 和模型路径。
- CI 应提前 checkout 或挂载 recipes 源码；本地首次验证仍可使用容器启动后 clone。
- 定义手工 IP/hosts 输入、节点身份、卡选择和并发互斥方式。
- 收集两个节点日志和 leader 的 checks/evaluations artifacts。
- workflow 只处理基础设施编排，不复制 Runner 内部生命周期。

### P1：Recipe 到中间态转换

- Recipe 文档格式确定后实现转换器。
- 转换器生成 `plan.yaml`、每节点脚本、gateway/check/evaluation 和 AISBench 配置。
- 保持生成目录自包含，不依赖全局隐藏模板。
- 明确 Recipe 字段、运行时字段和基础设施字段的归属。

### P1：三节点和四节点设计验证

- 使用 `node0...nodeN` 和可重复 `role` 验证多个 Decode/headless 节点。
- gateway backend 列表应由转换产物明确展开，不让 Runner 理解 P/D 拓扑。
- 检查多节点 DP rank、local size、start rank 和端口分配。
- 当前 `role` 主要用于可读性和环境注入，尚未实现按 role 自动聚合 IP；不要在 Runner
  中过早加入 P/D 专属逻辑。

### P1：AISBench 上游边界

- recipes 只保留评测选择、数据集、样本数、baseline 策略和 artifact 收集。
- 可向 vLLM Ascend 的 `tools/aisbench.py` 推动：自定义 executable、work/config/output
  目录、served name 与模型路径分离、机器可读结果。
- 更通用的 endpoint CLI 和稳定配置接口应推动到 AISBench 上游。
- 在上游接口稳定前，plan-local 两个 Python 配置是临时但可读的适配层，不应发展为
  recipes 自己维护的 AISBench fork。

### P2：第二阶段防御性能力

- 让 `install_aisbench.sh` 支持已存在目录、版本校验和明确升级/重装行为。
- 补充 node id 连续编号、role 合法性、端口冲突等 schema 校验。
- 增加更明确的环境和上游工具 preflight。
- 改进超时、错误传播、失败 artifact 和残留进程诊断。
- 评估协调器存储接口，使 HTTP 与共享文件系统实现可替换；当前 HTTP 已支持本地无共享
  文件系统场景，不需要为第一阶段改写。

## 11. 继续开发时容易踩的点

- 不要在 `/vllm-workspace/vllm-ascend` 中运行 Recipe CI 主流程；该目录只是运行时依赖。
- 不要假设 `pip install vllm-ascend` 会包含 examples；官方镜像中的完整源码才提供这些
  launcher/proxy 脚本。
- 不要把 P/D 的 `launch_online_dp.py` 用于普通内部 DP。
- 不要恢复已经删除的普通 external DP proxy 方案。
- 不要让 Runner 展开 DP rank 或理解 KV connector；这些属于节点脚本。
- 不要把 HCCL/vLLM 服务环境变量重新塞回 plan 顶层。
- 不要把 node id 改回角色名；节点使用 `nodeN`，角色使用 `role`。
- 不要把 accuracy/performance 再合并为一个通过环境变量切换 stream 的 AISBench 配置。
- 不要把 `aisbench/models` 压平，`models/` 是 AISBench config-dir 的固定分类。
- 不要在通用流程中清除代理变量。
- 不要把两条、32 token 的 GSM8K smoke 结果当作准确率结论。
- 重新实机测试前必须检查 NPU 占用，不能默认物理卡 4、5 仍然空闲。

## 12. 完成标准

这项任务可以在满足以下条件后进入下一阶段：

- 当前工作区已提交并推送。
- 最新代码的 P/D 和普通内部 DP 均在两节点完成真实推理。
- completion、accuracy、performance 和 artifact 收集均通过。
- 三/四节点中间态能够用 `nodeN + role` 无歧义表达。
- 多节点 workflow 能在目标 self-hosted runner 环境启动两个节点并汇总结果。
- Recipe 文档稳定后，转换器能够生成与当前手工 plan 等价的自包含目录。

