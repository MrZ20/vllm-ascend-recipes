#!/usr/bin/env bash
set -euo pipefail

AIS_BENCH_TAG=${AIS_BENCH_TAG:-v3.1-20260609-master}
AIS_BENCH_URL=${AIS_BENCH_URL:-https://github.com/AISBench/benchmark.git}
VLLM_ASCEND_ROOT=${VLLM_ASCEND_ROOT:-/vllm-workspace/vllm-ascend}
AIS_BENCH_ROOT=${AIS_BENCH_ROOT:-$VLLM_ASCEND_ROOT/benchmark}
PIP_INDEX_URL=${PIP_INDEX_URL:-https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple}
AIS_BENCH_PYTHON=python3
AIS_BENCH_COMMAND=ais_bench

if [[ -n "${AIS_BENCH_VENV:-}" ]]; then
    python3 -m venv "$AIS_BENCH_VENV"
    AIS_BENCH_PYTHON="$AIS_BENCH_VENV/bin/python"
    AIS_BENCH_COMMAND="$AIS_BENCH_VENV/bin/ais_bench"
fi

git clone --branch "$AIS_BENCH_TAG" --depth 1 \
    "$AIS_BENCH_URL" "$AIS_BENCH_ROOT"

"$AIS_BENCH_PYTHON" -m pip install \
    --index-url "$PIP_INDEX_URL" \
    --editable "$AIS_BENCH_ROOT" \
    --requirement "$AIS_BENCH_ROOT/requirements/api.txt" \
    --requirement "$AIS_BENCH_ROOT/requirements/extra.txt"

"$AIS_BENCH_COMMAND" -h >/dev/null
echo "AISBench $AIS_BENCH_TAG installed from $AIS_BENCH_ROOT"
if [[ -n "${AIS_BENCH_VENV:-}" ]]; then
    echo "Set RECIPE_AISBENCH_BIN=$AIS_BENCH_COMMAND before running evaluations"
fi
