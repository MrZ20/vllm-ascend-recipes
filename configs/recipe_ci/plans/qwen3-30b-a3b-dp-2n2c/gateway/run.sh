#!/usr/bin/env bash
set -euo pipefail

exec python3 \
    "$RECIPE_VLLM_ASCEND_ROOT/examples/external_online_dp/dp_load_balance_proxy_server.py" \
    --host "$RECIPE_LOCAL_IP" \
    --port "$RECIPE_GATEWAY_PORT" \
    --dp-hosts \
    "$RECIPE_NODE_0_IP" "$RECIPE_NODE_0_IP" \
    "$RECIPE_NODE_1_IP" "$RECIPE_NODE_1_IP" \
    --dp-ports 7100 7101 7100 7101
