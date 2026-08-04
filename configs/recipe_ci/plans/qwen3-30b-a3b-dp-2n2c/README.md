# Qwen3-30B-A3B 普通 DP 双节点四卡验证

这个用例验证普通 external data parallel，不包含 Prefill/Decode 拆分：

```text
node0: DP rank 0、1，TP1，共 2 卡
node1: DP rank 2、3，TP1，共 2 卡
                 ↓
全局 DP4，DP master 为 node0:12321
                 ↓
dp_load_balance_proxy_server.py 汇总四个 API 后端
                 ↓
completion curl 和可选 AISBench
```

它直接消费手工 `plan.yaml` 中间态，不依赖 Recipe 转换、Kubernetes 或共享文件系统。
每个节点都通过镜像内的 `launch_online_dp.py` 启动自己的两个 rank，Runner 不展开 vLLM
实例。

## 准备镜像和源码

两台 NPU 机器使用相同的 vLLM Ascend 镜像、代码分支和模型。当前分支需要先提交并
推送，容器内 `git clone` 才能获取修改。

宿主机启动容器的示例：

```bash
docker run --rm -it \
  --privileged \
  --network host \
  --ipc host \
  --shm-size 64g \
  -v /path/on/host/Qwen3-30B-A3B:/models/Qwen3-30B-A3B:ro \
  quay.io/ascend/vllm-ascend:v0.22.1rc1 \
  bash
```

容器内确认三个上游工具存在，然后 clone recipes 仓库：

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

两台机器填写完全相同的 hosts 文件：

```bash
cp configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/hosts.example.yaml \
  /tmp/qwen3-dp-hosts.yaml
vi /tmp/qwen3-dp-hosts.yaml
```

## 启动

先做静态校验：

```bash
scripts/recipe_ci/run.sh \
  --plan configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/plan.yaml \
  --validate-only
```

node0 执行：

```bash
scripts/recipe_ci/run.sh \
  --plan configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/plan.yaml \
  --hosts /tmp/qwen3-dp-hosts.yaml \
  --node-id node0 \
  --model-path /models/Qwen3-30B-A3B
```

node1 执行：

```bash
scripts/recipe_ci/run.sh \
  --plan configs/recipe_ci/plans/qwen3-30b-a3b-dp-2n2c/plan.yaml \
  --hosts /tmp/qwen3-dp-hosts.yaml \
  --node-id node1 \
  --model-path /models/Qwen3-30B-A3B
```

启动顺序没有要求。node0 同时是默认控制 leader 和这个用例显式选择的 DP master；两种
职责概念上仍然独立。两个节点的四个后端全部通过 `/health` 后，node0 才会启动普通
DP Proxy 并执行 completion 检查。

至少需要放通协调端口 `29599`、服务端口 `7100-7101`、DP RPC 端口 `12321` 和 Proxy
端口 `38085`。默认日志位于：

```text
/tmp/recipe-ci/qwen3-30b-a3b-dp-2n2c/
├── node0/
│   ├── service.log
│   ├── gateway.log
│   └── checks/completion.log
└── node1/service.log
```

默认不运行评测。AISBench 模型配置指向 `<node0_ip>:38085` 且 served model 为 `qwen3`
后，可在 node0 命令增加 `--evaluation accuracy`、`performance` 或 `all`。Runner 不会
清理任何代理环境变量，只会把节点 IP 加入 `NO_PROXY`。
