# Multi-node Recipe CI

当前阶段只打通“最终中间态 -> 测试执行”。Recipe 文档到中间态的转换器尚未实现；
`configs/recipe_ci/plans/` 下的内容按生成产物对待，不承担历史兼容，也允许每个节点显式重复
完整命令。仓库暂时保留两个小模型 fixture，分别覆盖 P/D 分离与普通多节点 DP。

## 职责边界

```text
未来的 Recipe converter
  -> 校验 Recipe 语义并展开成最终中间态
  -> 生成 node-local launch、gateway 和通用 stages

Runtime core
  -> 解码可信中间态
  -> 启动进程、等待 readiness、执行 stages
  -> 协调 stop signal、清理和最终 outcome

Infrastructure adapter
  -> 把本地、裸机或 LWS 的运行环境映射为统一 runtime 输入
```

主要文件：

```text
configs/recipe_ci/plans/<case>/
├── plan.yaml
├── nodes/node0...nodeN/
├── gateway/                 # 可选
├── checks/
└── evaluations/

scripts/recipe_ci/
├── plan.py                  # 可信 plan 解码；hosts 做运行时一致性检查
├── runner.py                # 通用节点、stage 和 outcome 生命周期
├── coordinator.py           # 本地/裸机/LWS 共用的 HTTP 状态通道
├── process.py               # 进程组、信号、日志和清理
├── result.py                # outcome schema、序列化和原子写盘
├── aisbench.py              # 下载数据、复制固定模板、执行并转换 AISBench 结果
├── install_aisbench.sh      # 测试前准备共享缓存
├── run.sh                   # 与基础设施无关的统一节点入口
└── k8s/
    ├── run_lws.sh           # LWS 环境到通用 runtime 输入的适配器
    ├── lws.yaml.tmpl        # 简单占位模板
    └── render_lws.py        # 严格替换模板占位符
```

## `runner.py` 中文阅读导读（临时注释）

这一节按 `scripts/recipe_ci/runner.py` 的函数和调用顺序解释代码。它是阅读
辅助，不是新的运行时契约；真正的输入仍以 plan、hosts 和脚本实现为准。

### 函数地图

| 函数 | 作用 |
| --- | --- |
| `parse_args` | 解析 Runner 的内部命令行参数；`run.sh` 会调用它。 |
| `interface_addresses` | 获取本机网卡与 IPv4 的对应关系。 |
| `select_interface` | 选择 HCCL/Gloo 等跨节点通信网卡。 |
| `resolve_vllm_ascend_root` | 确定镜像内 vLLM Ascend 源码目录。 |
| `base_environment` | 把 plan、hosts、模型、节点和 artifact 信息转换成环境变量。 |
| `wait_http_ready` | 轮询一个 HTTP 健康地址。 |
| `wait_node_ready` | 按节点的 `readiness` 检查一个或多个服务端口。 |
| `run_stage` | 顺序执行 stage steps，检查退出码和 `result.json`。 |
| `aggregate_run_outcome` | 根据最终节点结果生成唯一的全局结果。 |
| `run_node` | 当前节点的完整生命周期，代码主体在这里。 |
| `main` | 读取 plan/hosts，选择 validate-only 或真实执行模式。 |

### 主流程

```text
main
  ├─ load_plan / load_hosts
  ├─ validate-only？打印拓扑后结束
  └─ run_node
       ├─ 选择本机网卡，构造环境变量
       ├─ node0 启动 LeaderCoordinator；worker 等待 coordinator
       ├─ 启动 plan.nodes[node].launch
       ├─ 等待本节点 readiness
       ├─ leader 等待所有节点 ready
       ├─ leader 启动可选 gateway 并检查 gateway health
       ├─ leader 顺序执行 plan.stages
       ├─ 任一失败/取消时发布 StopSignal
       ├─ 清理本节点所有进程组
       ├─ 写入并上报 node-result.json
       └─ leader 聚合并写入 result.json
```

### 几个容易混淆的边界

- `runner.py` 不会推导 P/D、DP/TP、rank、KV Connector 或 proxy backend；这些
  都在 `nodes/nodeN/run.sh`、`run_dp_template.sh` 和 `gateway/run.sh` 中显式定义。
- `Coordinator` 只传输 ready、stop signal 和 NodeOutcome，不传输 service.log。
- `start_process` 会把 service、gateway 和 stage 分别写入 artifact 日志，并为
  每个受管命令创建独立进程组；因此不是“新开终端”，而是同一 Pod 内的受管子进程。
- StopSignal 只是让其他节点尽快进入清理阶段；最终结论要等 NodeOutcome 收集后，
  由 `aggregate_run_outcome` 生成。
- worker 只启动自己的 service、上报 ready 并等待 stop；gateway 和 stages 由 leader
  执行一次。

### 失败归因顺序

`aggregate_run_outcome` 不把所有异常简单合并，而是按以下顺序选主因：

1. 首个失败节点发布的执行失败；
2. 其他节点自身的执行失败；
3. 进程清理失败；
4. coordinator 结果收集失败或节点缺失；
5. 取消；
6. 所有节点通过。

因此，观察到 leader 失败而退出的 worker 通常是 `aborted`，不会被重复记录成第二个
primary failure。

中间态显式提供节点脚本、readiness、可选 gateway 和任意命名的 `stages`。每个 stage 包含
`id`、`failure_category` 和顺序执行的 steps；Runtime 不内置 `checks`、`accuracy`、
`performance`、P/D、DP、TP、rank、KV Connector 或 gateway backend 等业务语义。

`model.cache_path` 是固定模型根目录
`/root/.cache/modelscope/hub/models` 下的相对路径。节点地址、网卡和设备分配属于运行环境，
不写入 plan。

## 中间态假设

Runtime 假设 plan 已由 converter 完成静态和语义校验，因此 `plan.py` 只把 YAML 解码成执行
对象。以下检查应由未来 converter 负责，而不是在一次性执行代码中重复实现：

- schema、未知字段、字段类型、节点命名和 step 唯一性；
- 脚本路径 containment、文件存在性、端口范围和冲突；
- DP/TP/rank、资源、KV Connector 和 gateway backend 的语义一致性；
- AISBench 配置、baseline、tolerance 和样本参数的展开。

Runtime 仍然处理真实执行故障，包括进程启动/异常退出、readiness 和 step 超时、HTTP 或
coordinator 不可达、信号、清理失败、artifact I/O 失败以及外部工具结果解析失败。

短期保留 `RECIPE_CI_VALIDATE_ONLY=true`，仅用于解码 plan 并打印拓扑，方便本地新增用例和
调试；它不是 converter validator，也不检查模型、NPU、网络或脚本语义：

```bash
RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/plan.yaml \
RECIPE_CI_VALIDATE_ONLY=true scripts/recipe_ci/run.sh
```

## 通用运行时契约

本地、裸机和 LWS 最终都执行 `scripts/recipe_ci/run.sh`。共同输入为：

```text
RECIPE_CI_PLAN          plan.yaml 路径
RECIPE_CI_NODE_INDEX    当前节点序号，0...N
RECIPE_CI_CLUSTER_IPS   按 node0...nodeN 排列的逗号分隔 IPv4 地址
RECIPE_CI_INTERFACE     当前节点通信网卡，可选；未设置时按本机地址选择
ASCEND_RT_VISIBLE_DEVICES 或 RECIPE_CI_VISIBLE_DEVICES
```

本地模式直接注入这些变量。LWS Pod 执行 `k8s/run_lws.sh`：它读取
`LWS_WORKER_INDEX` 和 `LWS_LEADER_ADDRESS`，等待同组 Pod DNS，生成
`RECIPE_CI_NODE_INDEX` 与 `RECIPE_CI_CLUSTER_IPS` 后调用同一个 `run.sh`。LWS 语义不会进入
Runtime core。

主流程从 recipes 根目录运行。`VLLM_ASCEND_ROOT` 默认是
`/vllm-workspace/vllm-ascend`，并作为 `RECIPE_VLLM_ASCEND_ROOT` 传给生成脚本。运行镜像必须
提供 plan 实际引用的 vLLM Ascend 源码与运行能力；例如 P/D fixture 需要：

```text
examples/external_online_dp/launch_online_dp.py
examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

Mooncake 是运行镜像契约，不在测试过程中动态安装。Runner 不删除代理变量；内部协调请求
绕过代理，并把集群 IP 加入 `NO_PROXY`。

Runner 向生成脚本注入的主要变量：

```text
RECIPE_NODE_ID / RECIPE_NODE_INDEX / RECIPE_NODE_ROLE
RECIPE_LOCAL_IP / RECIPE_LOCAL_INTERFACE / RECIPE_LEADER_IP
RECIPE_NODE_0_IP / RECIPE_NODE_1_IP / ...
RECIPE_MODEL_PATH / RECIPE_SERVED_MODEL_NAME
RECIPE_ENDPOINT / RECIPE_ENDPOINT_HOST / RECIPE_ENDPOINT_PORT
RECIPE_ARTIFACT_ROOT / RECIPE_NODE_ARTIFACT_DIR
RECIPE_STEP_ARTIFACT_DIR / RECIPE_STEP_INPUT_FILE / RECIPE_STEP_RESULT_FILE
```

Runner 会把 step 的通用 `inputs` 写入 `RECIPE_STEP_INPUT_FILE`。每个 stage step 必须在
退出前向 `RECIPE_STEP_RESULT_FILE` 写入 JSON，至少包含
`{"status": "passed"}`。Runtime 只检查公共 status，不解析工具私有日志或业务指标。

## 生命周期与结果协议

```text
启动本节点 service -> 本机 readiness -> 上报 ready
  -> leader 等待全部 ready，启动可选 gateway，顺序执行 stages
  -> 任一节点发布一次 StopSignal(completed/failed/cancelled)
  -> 其他节点停止等待；远端失败只记为 aborted，不伪造本地 failure
  -> 每个节点清理自己创建的全部进程组
  -> 清理后构造唯一且不可变的 NodeOutcome
  -> 同一 NodeOutcome 写 node-result.json 并上报 coordinator
  -> leader 等待所有可达节点 outcome，聚合 RunOutcome
  -> 写 result.json 并完成 final 状态
```

StopSignal 只用于尽快通知所有节点进入清理阶段，不是最终结果。Coordinator 在收齐
NodeOutcome 前保持 `stopping`；最终结论只从清理后的 outcome 聚合。执行失败优先于清理失败，
但清理失败仍会令全局结果失败。若节点只是观察到其他节点失败，其
`execution_status=aborted`，不会出现在本地 failure 归因中。

所有受管命令使用独立 process group。清理顺序为 SIGTERM、有限等待、SIGKILL、关闭日志和
存活验证，不使用 `pkill` 或 `killall`。Coordinator 只承载 readiness、stop signal 与
outcome，不传输日志，也不依赖共享 PVC。

节点结果使用 `recipe-ci-node-result/v2`，全局结果使用 `recipe-ci-result/v2`。典型 artifact：

```text
artifacts/<plan>/
├── node0/
│   ├── service.log
│   ├── gateway.log          # 可选
│   ├── <stage>/<step>.log
│   └── node-result.json
├── node1/
└── result.json              # 仅 leader
```

`node-result.json` 同时保留 `execution_status` 和受清理结果影响的最终 `status`；
`result.json` 包含 `nodes`、`stages`、`failure_node_id` 与 `missing_nodes`。JSON 使用同目录
临时文件和原子 replace 写入。

## AISBench 准备与缓存

固定运行镜像不包含 AISBench。LWS 的 node0 在各节点进入 `run.sh` 前执行：

```bash
AIS_BENCH_ENVIRONMENT_IDENTITY='runtime=<runtime image>' \
scripts/recipe_ci/install_aisbench.sh --env-file /tmp/aisbench.env
```

脚本固定 tag 和 commit，并把下列输入绑定到共享 PVC cache key：

- cache schema 与 AISBench commit；
- runtime image identity；
- Python 主次版本、CPU 架构和 constraints 摘要。

命中完整缓存时直接复用；冷 cache 安装到临时目录后原子发布。node0 负责准备 AISBench 并
把环境文件写入本次运行的共享目录，其他节点等待该文件。所有节点拿到同一个
`RECIPE_AISBENCH_BIN` 和 `RECIPE_AISBENCH_SOURCE` 后才进入 `run.sh` 并启动服务。
Runtime Pod 通过集群内部 PyPI cache 下载 AISBench 依赖。

plan 的 step inputs 声明 ModelScope dataset ID、AISBench dataset/request config 名称、
`num_prompts` 和推理参数。`aisbench.py` 把数据集下载到 PVC 上的 ModelScope cache，从固定
AISBench commit 复制模板到当前 step artifact，再补全 endpoint、model 和数据路径；不会修改
共享 AISBench source。两个 fixture 用 `num_prompts: 1` 做 smoke，仓库不再复制 GSM8K 数据或
Python 配置模板。

## 保留的两个 fixture

### DeepSeek-V2-Lite P/D 双节点四卡

`configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/` 使用两个逻辑节点，每节点两张 NPU：

```text
node0: 两个 Prefill TP1 实例 + P/D proxy
node1: 两个 Decode TP1 实例
endpoint: node0:38085
```

### Qwen3-30B-A3B 普通 DP 双节点四卡

`configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/` 不含 P/D 分离：

```text
node0: API + DP rank 0,1
node1: headless DP rank 2,3
global: DP4, TP1
endpoint: node0:7100
```

两者都可在两台机器上使用相同的输入形式：

```bash
export RECIPE_CI_PLAN=configs/recipe_ci/plans/<case>/plan.yaml
export RECIPE_CI_CLUSTER_IPS='<node0_ip>,<node1_ip>'
export RECIPE_CI_INTERFACE='<local_interface>'
export RECIPE_CI_NODE_INDEX=0  # 另一台为 1
export ASCEND_RT_VISIBLE_DEVICES=4,5
scripts/recipe_ci/run.sh
```

具体端口和镜像要求见各 fixture README。

## GitHub Actions / LWS

`.github/workflows/recipe_verify_multi_node.yaml` 的矩阵包含上述两个小模型 plan；
`_recipe_verify_multi_node.yaml` 是与模型语义无关的执行层：

```text
无 NPU controller checkout 源码
  -> 解析 node_count / npu_per_node
  -> render_lws.py 渲染并创建 LeaderWorkerSet
  -> 在 15 分钟总期限内一次等待全部 Pod 创建并 Ready
  -> Pod 通过 run_lws.sh 准备或复用共享 AISBench cache
  -> 所有 Pod 准备完成后进入通用 run.sh
  -> Pod 将退出码、artifact 和 plog 写入共享 PVC
  -> 第一个失败后最多等待 120 秒，让其他节点完成 stop/cleanup/outcome
  -> 固定采集 K8s 诊断，删除 LWS，打包 artifact
```

CI job 总超时为 180 分钟；Pod 内共享 30 分钟启动期限和 120 分钟执行期限。单个 step 的
实际 timeout 是其 plan 声明与剩余执行期限的较小值，避免多个阶段各自重新获得完整超时。

Pod 按 `plan.resources.npu_per_node` 申请 `huawei.com/ascend-1980`，并由当前 cluster profile
选择 A2B4 节点、night toleration、hostNetwork、PVC 和 runtime image。这些基础设施策略只在
workflow/template 中，不进入 plan 或 Runtime core。相同 run 的 Pod 通过反亲和分散到不同
物理机。

artifact bundle 优先上传 OBS。只有 OBS step 没有成功时才执行
`actions/upload-artifact` 作为 GitHub fallback；任一上传失败只产生 warning，不覆盖模型测试
结论。成功和失败使用相同日志策略：固定保留 `artifacts/`、`plogs/`、`pod-status/`，以及
渲染值、LWS/Pod 最终 YAML 和 namespace events；controller step 输出和 Pod stdout 只实时显示
在 GitHub，不再重复保存。共享 PVC 中的本次 run 目录只在 OBS 或 GitHub 至少一处上传成功后删除。

PR job 只运行同仓库分支，fork PR 不接触集群凭据。当前不包含 nightly 自动触发。

## 当前不做

- Recipe YAML parser、schema 重设计或 YAML -> intermediate converter；
- 历史 intermediate 兼容层或执行时配置补偿；
- Runner 自动推导 P/D、DP/TP/rank、KV Connector 或 gateway backend；
- 重写 vLLM Ascend launcher、P/D proxy 或 AISBench；
- 为消除生成脚本重复而抽取 runtime 模板系统；
- 三/四节点 fixture、完整数据集或大模型自动下载。
