# DeepSeek-V4-Flash A3 双节点 P/D CI plan

该目录是 `models/en/DeepSeek/DeepSeek-V4-Flash.yaml` 中 Atlas 800 A3 1P1D
部署段的手工中间态，用于第二阶段多节点 CI 验证。Runner 不计算拓扑，DP/TP、端口、
KV Connector 和 gateway backend 均由本目录显式保存。

## 拓扑

```text
node0 role=prefill: DP4 x TP4，16 张 NPU，端口 7100-7103
node1 role=decode:  DP16 x TP1，16 张 NPU，端口 7100-7115
node0 gateway:     端口 38085
```

每个节点必须提供 16 个可见设备，例如：

```bash
export ASCEND_RT_VISIBLE_DEVICES=0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15
```

镜像必须保留：

```text
/vllm-workspace/vllm-ascend/examples/external_online_dp/launch_online_dp.py
/vllm-workspace/vllm-ascend/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py
```

node0 运行：

```bash
export RECIPE_CI_PLAN=configs/recipe_ci/plans/deepseek-v4-flash-a3-pd/plan.yaml
export RECIPE_CI_MODEL_PATH=/models/DeepSeek-V4-Flash-w8a8-mtp
export RECIPE_CI_CLUSTER_IPS="<node0_ip>,<node1_ip>"
export RECIPE_CI_INTERFACE="<local_interface>"
export RECIPE_CI_EVALUATION=none
export LWS_WORKER_INDEX=0
scripts/recipe_ci/run.sh
```

node1 使用相同环境并将 `LWS_WORKER_INDEX` 改为 `1`。两个节点应使用相同仓库 commit、
镜像、模型路径和 `RECIPE_CI_CLUSTER_IPS`。CI 基础设施参数不写入该 plan。

需要本地运行 AISBench 时，先执行 `scripts/recipe_ci/install_aisbench.sh`，或在
node0/node1 的共同环境中设置 `RECIPE_CI_INSTALL_AISBENCH=true`，再把
`RECIPE_CI_EVALUATION` 改为 `accuracy`、`performance` 或 `all`。只有 node0 会执行
安装和评测。
