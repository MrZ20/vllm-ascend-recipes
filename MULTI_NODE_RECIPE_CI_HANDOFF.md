# Multi-node Recipe CI 开发交接

更新日期：2026-08-05

分支：`add_mul_ci`

第二阶段开发基线：`c7d5fa448d680d80ae0a88da1a0ce79ae0e849ec`

> 本文描述当前工作区状态。修改尚未提交时，换设备前必须提交并 push，或保存完整 patch；
> 仅 clone 远端分支不会带走未提交内容。

## 当前目标与边界

框架仍从手工 `configs/recipe_ci/plans/**/plan.yaml` 中间态开始，不实现 Recipe 文档转换。
Runner 不理解 P/D、DP rank、KV Connector 或 gateway backend；每个节点执行自己的
plan-local 脚本。控制面使用 HTTP，不依赖 Kubernetes 或共享文件系统。

按本轮要求，第二阶段不增加或回归三、四节点用例。最终真实 CI 目标仅为：

```text
configs/recipe_ci/plans/deepseek-v4-flash-a3-pd
node0: A3 Prefill DP4 x TP4，16 卡
node1: A3 Decode  DP16 x TP1，16 卡
gateway: node0:38085
```

现有 DeepSeek V2 两节点 P/D 和 Qwen 两节点普通内部 DP 只保留兼容，不作为本轮新增真实
回归目标。

## 已落实内容

### Plan/schema

- `recipe-ci/v1` 严格 unknown-field 校验。
- 节点必须连续为 `node0...nodeN`，`role` 必填且允许重复。
- plan 引用必须是目录内普通文件，拒绝 `..` 和 symlink 逃逸。
- readiness、gateway 端口、health path、slug、step id 和 hosts IPv4 完整校验。
- `--validate-only` 可选校验 hosts，并输出展开后的拓扑、地址、网卡和 endpoint。

### Runner/process/coordinator

- `process.py` 集中处理独立 process group、日志尾部、SIGINT/SIGTERM cancellation、
  TERM/KILL 和存活验证。
- check/evaluation 改为受监管子进程；运行期间持续检查本机服务、gateway、远端失败、
  timeout 和 cancellation。
- Coordinator 状态统一为 `running/passed/failed/cancelled`，节点状态为
  `pending/ready/failed/cleaned`；重复请求幂等，terminal 不可修改，客户端只有限重试
  连接错误、408、429 和 5xx。
- terminal 先发布；每节点真实清理、关闭日志并写完 `node-result.json` 后才上报 cleaned。
- leader 硬退出时，worker 在 coordinator unreachable grace 后自行失败并清理。
- 不使用全局 `pkill/killall`，不删除用户代理变量。

### Result/artifact

- `result.py` 提供单一 `RunFailure`、UTC 时间、原子 JSON 和结果合并。
- 每节点写 `environment.json`、`node-result.json`；leader 写 `result.json`。
- 第一个执行错误保留为 primary failure，cleanup error 单独记录。
- coordinator 不传日志；本地节点各自保留 artifact，K8s CI 的 Pod 把 artifact 写到共享
  PVC，控制器 Job 负责打包上传。PVC 不参与状态协调。

### AISBench

- `install_aisbench.sh` 固定：

  ```text
  tag    v3.1-20260609-master
  commit 0da56eadb2ac85c31c2540f4f5b69af3ec5717a5
  ```

- 正确版本重复执行直接复用；错误版本默认失败，只有 `--force-reinstall` 才替换。
- 默认尊重已有 pip 配置，可通过 `AIS_BENCH_VENV` 隔离依赖。
- `aisbench.py` 完成命令/config/dataset/artifact preflight，并把 accuracy/performance
  产物转换为通用 `RECIPE_STEP_RESULT_FILE`。
- 默认少量样本是 smoke；配置 baseline/allowed-drop 才进行 accuracy gate。
- performance 提取 TTFT、TPOT、E2E、output token/s 和 request/s。

### DeepSeek V4 与 workflow

- 新增 `configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/`，参数手工对齐
  `models/en/DeepSeek/DeepSeek-V4-Flash.yaml` A3 1P1D 段。
- node0/node1 使用上游 `launch_online_dp.py` 和各自 `run_dp_template.sh`。
- gateway 显式列出 4 个 Prefill 和 16 个 Decode backend。
- completion、accuracy、performance 均为 plan-local 内容。
- `.github/workflows/recipe_verify_multi_node.yaml` 只负责手动输入，并调用
  `_recipe_verify_multi_node.yaml` reusable workflow；当前默认用例是 DeepSeek V4 双节点，
  但通用执行层不包含用例或双节点语义。
- reusable workflow 复用 vLLM Ascend 的单 controller Job + `LeaderWorkerSet` 方案，严格
  解析 `len(plan.nodes)` 作为 LWS size，并动态枚举任意 `node0...nodeN` Pod。
- controller 把指定 ref（留空则为 workflow commit）的同一份 recipes 源码暂存到 PVC，
  所有 16 卡 A3 Pod 直接运行 `scripts/recipe_ci/run.sh`；入口根据
  `LWS_WORKER_INDEX` 选择 node0...nodeN，并由 LWS DNS 动态生成 `hosts.yaml`。
- 本地与 LWS 只有一个公开入口。CI 由 LWS/Workflow 注入环境变量；本地手动
  设置相同的 `LWS_WORKER_INDEX`、`RECIPE_CI_CLUSTER_IPS`、网卡和可见卡。
- Runner 仍用 HTTP coordinator；共享 PVC 只传源码、artifact 和 Ascend plog。控制器
  动态流式输出所有 Pod 日志、检查全部 exit code、删除 LWS 并始终上传 bundle。

## 本地执行

镜像内推荐目录：

```text
/vllm-workspace/vllm-ascend
/vllm-workspace/vllm-ascend-recipes
```

本地手动 clone recipes 后，在所有机器准备相同 commit、镜像、模型，
按 `node0...nodeN` 顺序设置同一份 IP 列表，然后分别前台运行。node0 示例：

```bash
export RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/plan.yaml
export RECIPE_CI_MODEL_PATH=/path/to/DeepSeek-V4-Flash-w8a8-mtp
export RECIPE_CI_CLUSTER_IPS="<node0_ip>,<node1_ip>"
export RECIPE_CI_INTERFACE="<local_interface>"
export LWS_WORKER_INDEX=0
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
export RECIPE_CI_EVALUATION=none
scripts/recipe_ci/run.sh
```

另一台只把 `LWS_WORKER_INDEX` 改为 `1`，并按本机资源设置网卡和空闲卡。
这与 CI 执行同一个 shell 入口和 Runner/HTTP 流程，不需要
本地 Actions 模拟器、Kubernetes、共享文件系统或新建 SSH orchestration 层。

AISBench 不在基础镜像时：

```bash
scripts/recipe_ci/install_aisbench.sh
```

GSM8K 数据集目录默认是：

```text
/vllm-workspace/vllm-ascend/benchmark/ais_bench/datasets/gsm8k
```

## CI 管理员需要配置

```text
Variable: RECIPE_CI_K8S_CONTROLLER_RUNNER   # 可选
Variable: RECIPE_CI_A3_RESOURCE_GROUP       # 可选
Variable: RECIPE_CI_A3_PVC_NAME             # 可选
Variable: RECIPE_CI_AISBENCH_DATASET_DIR    # 可选
Secret:   KUBECONFIG_B64
Secret:   RECIPE_CI_MODEL_PATH
```

模型路径必须能被所有 LWS Pod 访问，通常放在相同 PVC 中。CI 不需要配置节点 IP、网卡、
两套 runner label 或空闲卡列表；Pod 调度和 16 卡分配由 K8s 完成。

## 已执行的本地验证

- `.venv/bin/python -m unittest discover -s tests/recipe_ci -v`
- Python `py_compile`：Plan、Runner、Coordinator、Process、Result、AISBench。
- 所有 Recipe CI shell 脚本 `bash -n`。
- 新增 DeepSeek V4 shell 脚本 `shellcheck`。
- `actionlint`：手动入口和 `_recipe_verify_multi_node.yaml` reusable workflow。
- workflow YAML/BaseLoader 静态结构检查。
- `git diff --check`。
- DeepSeek V4 plan 的统一入口 `RECIPE_CI_VALIDATE_ONLY=true` 校验。

最终交接前必须重新执行完整检查并以最后一次输出为准。

## 2026-08-05 双节点轻量实机回归

使用两台专用 A3 容器对当前未提交工作区快照重新执行
`deepseek-v2-lite-pd-2n2c`，没有验证 GitHub Actions/LWS：

```text
node1 -> Recipe node0 / Prefill / 172.22.0.155 / enp23s0f3 / 物理卡 4,5
node2 -> Recipe node1 / Decode  / 172.22.0.188 / enp23s0f3 / 物理卡 0,1
model -> /root/.cache/modelscope/hub/models/vllm-ascend/DeepSeek-V2-Lite-W8A8
```

上述 IP、网卡和空闲卡只代表本次运行，未写入 plan，下一次执行必须重新检查。两边使用
同一份工作区归档，SHA256 为
`726ad7bca3dddd592d22b5cd4dda699607cd9830ae98751e9a268d02a6e7f764`，解压到独立目录
`/vllm-workspace/vllm-ascend-recipes-validation-20260805`，没有覆盖原 clone。

验收结果：

- 每节点两个 TP1/DP2 vLLM 实例在 7100/7101 启动并通过 `/health`。
- Decode 与 Prefill 的 Mooncake 链路建立，gateway completion 返回 HTTP 200。
- 两节点 `node-result.json` 均为 `passed`，completion return code 为 0，无
  `primary_failure`、`cleanup_errors` 或 warning。
- leader 聚合 `result.json` 为 `passed`、`failure: null`，node0/node1 均为 `cleaned`，
  `checks.completion.status` 为 `passed`。
- Runner 完成后，目标端口全部关闭，Runner、vLLM、launcher、proxy 无残留，四张 NPU
  均释放到系统基线。
- vLLM 子进程收到正常 TERM 后打印了 multiprocessing resource tracker 警告及
  `corrupted size vs. prev_size`。服务和 gateway 的 return code `-15` 是 Runner 的预期
  生命周期清理，未导致框架失败；该底层退出告警仍值得在后续镜像/vLLM 版本回归中观察。

远端 artifact 保留在：

```text
/tmp/recipe-ci-phase2-rework-20260805/deepseek-v2-lite-pd-2n2c/
```

统一入口改造后又在同两个专用容器中重跑了最新工作区快照：

```text
node1 -> LWS_WORKER_INDEX=0 / 172.22.0.155 / 物理卡 0,1
node2 -> LWS_WORKER_INDEX=1 / 172.22.0.188 / 物理卡 0,1
snapshot -> /vllm-workspace/vllm-ascend-recipes-unified-20260805
artifact -> /tmp/recipe-ci-unified-20260805/deepseek-v2-lite-pd-2n2c
```

两边都只设置同一套环境变量并执行 `scripts/recipe_ci/run.sh`。入口正确完成
`LWS_WORKER_INDEX -> node id`、临时 hosts 和可见卡映射；两个 node result 及 leader
aggregate 都是 `passed`，completion 通过，node0/node1 都是 `cleaned`，没有
primary failure、cleanup error、warning 或相关残留进程。

## 尚未完成或仍需外部资源

- 尚未在两台 16 卡 A3 节点真实启动 DeepSeek V4 1P1D；这是最重要的剩余验收。
- 尚未在真实 K8s controller runner 上执行 workflow_dispatch；kubeconfig、PVC、LWS
  controller、模型路径、Pod 取消和 plog 收集仍需管理员环境实测。
- AISBench wrapper 已按固定 tag 源码格式和既有真实产物编写，但仍需用 DeepSeek V4 的
  一次 accuracy/performance 实跑确认产物与 score baseline。
- 当前不做三/四节点、Recipe converter、共享文件 coordinator、PR/nightly 自动触发。
- leader SIGKILL/机器掉电只能由 worker 检测 coordinator 不可达，无法保证收到 leader
  主动发布的具体失败原因。

详细设计与契约见 `docs/MULTI_NODE_RECIPE_CI.md`；原始第二阶段任务书保留在
`MULTI_NODE_RECIPE_CI_PHASE2_AGENT_TASK.md`，但其中三/四节点要求已被本轮用户指令覆盖。
