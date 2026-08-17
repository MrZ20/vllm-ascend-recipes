#!/usr/bin/env bash
set -euo pipefail

exec python3 \
    "$MULTI_NODE_VLLM_ASCEND_ROOT/examples/disaggregated_prefill_v1/load_balance_proxy_server_example.py" \
    --host "$MULTI_NODE_LOCAL_IP" \
    --port "$MULTI_NODE_GATEWAY_PORT" \
    --prefiller-hosts \
    "$MULTI_NODE_NODE_0_IP" "$MULTI_NODE_NODE_0_IP" \
    --prefiller-ports 7100 7101 \
    --decoder-hosts \
    "$MULTI_NODE_NODE_1_IP" "$MULTI_NODE_NODE_1_IP" \
    --decoder-ports 7100 7101
