#!/usr/bin/env bash
set -euo pipefail

source_directory=$RECIPE_PLAN_DIR/aisbench/datasets/gsm8k
dataset_directory=$RECIPE_STEP_ARTIFACT_DIR/ais_bench/datasets/gsm8k

mkdir -p "$(dirname "$dataset_directory")"
ln -s "$source_directory" "$dataset_directory"
export AIS_BENCH_DATASETS_CACHE=$RECIPE_STEP_ARTIFACT_DIR
