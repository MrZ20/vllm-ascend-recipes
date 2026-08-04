# DeepSeek-V2-Lite P/D 双节点四卡验证

这个手工中间态用例用于打通第一阶段主链路：两台机器分别承担 Prefill 和 Decode，
每台机器由 vLLM Ascend 的 `launch_online_dp.py` 启动两个 TP1 实例，因此每节点使用
2 张 NPU，总计 4 卡。所有后端就绪后，Prefill 节点启动上游 P/D Proxy，再依次执行
completion 检查和可选的 AISBench 评测。

该用例不依赖 Recipe 文档转换、Kubernetes 或共享文件系统。

## 运行镜像契约

使用 vLLM Ascend 官方运行镜像。镜像除已安装的 vLLM 和 vLLM Ascend 外，还必须保留
完整源码目录：

```text
/vllm-workspace/vllm-ascend/
├── examples/external_online_dp/launch_online_dp.py
├── examples/external_online_dp/dp_load_balance_proxy_server.py
└── examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

这些工具由镜像提供，本仓库不复制它们，也不会在执行时下载 vLLM Ascend 源码。

## 第一次本地验证：启动镜像后 clone

下面的动作需要在两台装有 NPU 的物理机上各执行一次。先把本次修改提交并推送到可
访问的分支；容器内 `git clone` 无法读取宿主机尚未推送的工作区修改。

在两台机器的宿主机上启动相同版本的镜像，并将模型挂载到相同的容器路径。镜像名、
宿主机模型路径按实际环境替换：

```bash
docker run --rm -it \
  --privileged \
  --network host \
  --ipc host \
  --shm-size 64g \
  -v /path/on/host/DeepSeek-V2-Lite-W8A8:/models/DeepSeek-V2-Lite-W8A8:ro \
  quay.io/ascend/vllm-ascend:v0.22.1rc1 \
  bash
```

进入容器后，先确认镜像契约，再 clone 当前分支。使用 HTTPS 可以避免容器内缺少宿主机
SSH 凭据：

```bash
test -f /vllm-workspace/vllm-ascend/examples/external_online_dp/launch_online_dp.py
test -f /vllm-workspace/vllm-ascend/examples/external_online_dp/dp_load_balance_proxy_server.py
test -f /vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py

cd /vllm-workspace
git clone --branch <your-branch> --depth 1 \
  https://github.com/MrZ20/vllm-ascend-recipes.git \
  vllm-ascend-recipes
cd /vllm-workspace/vllm-ascend-recipes
```

两台机器都复制并填写同一份 hosts 文件。`prefill` 是控制面 leader；这里的默认值来自
节点顺序，不需要在 plan 中重复声明：

```bash
cp configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/hosts.example.yaml \
  /tmp/deepseek-v2-lite-hosts.yaml
vi /tmp/deepseek-v2-lite-hosts.yaml
```

先检查中间态结构，不会检查 NPU 或启动服务：

```bash
scripts/recipe_ci/run.sh \
  --plan configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/plan.yaml \
  --validate-only
```

Prefill 机器执行：

```bash
scripts/recipe_ci/run.sh \
  --plan configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/plan.yaml \
  --hosts /tmp/deepseek-v2-lite-hosts.yaml \
  --node-id prefill \
  --model-path /models/DeepSeek-V2-Lite-W8A8
```

Decode 机器执行：

```bash
scripts/recipe_ci/run.sh \
  --plan configs/recipe_ci/plans/deepseek-v2-lite-pd-2n2c/plan.yaml \
  --hosts /tmp/deepseek-v2-lite-hosts.yaml \
  --node-id decode \
  --model-path /models/DeepSeek-V2-Lite-W8A8
```

两边的启动先后没有要求。框架默认使用
`/vllm-workspace/vllm-ascend`；非标准镜像可通过 `--vllm-ascend-root` 覆盖。
框架不会清理或改写任何 `http_proxy`、`https_proxy`、`ftp_proxy` 环境变量，只为集群
IP 补充 `NO_PROXY`。

需要放通节点间通信，至少包括协调端口 `29599`、服务端口 `7100-7101`、DP RPC 端口
`12321`、Mooncake 端口 `30000/30200` 和 Proxy 端口 `38085`。

成功或失败后，各节点都会清理自己启动的进程。日志默认位于：

```text
/tmp/recipe-ci/deepseek-v2-lite-pd-2n2c/
├── prefill/
│   ├── service.log
│   ├── gateway.log
│   └── checks/completion.log
└── decode/service.log
```

## 可选 AISBench 阶段

默认只运行 completion smoke check。确认 AISBench 已安装，并让选用的 AISBench 模型
配置指向 `<prefill_ip>:38085`、模型名为 `deepseek-v2-lite` 后，可在 Prefill 命令增加：

```bash
--evaluation accuracy
--evaluation performance
--evaluation all
```

模型配置名可分别通过 `RECIPE_AISBENCH_ACCURACY_MODEL_CONFIG` 和
`RECIPE_AISBENCH_PERFORMANCE_MODEL_CONFIG` 覆盖。评测命令输出和 AISBench 产物都会
写到该节点的 `accuracy/` 或 `performance/` artifact 目录。

若评测可能超过默认的一小时运行超时，需要在 Decode 命令上增加足够大的
`--run-timeout-seconds`；这个值只约束等待 leader 最终结果的节点，不写入 plan。
