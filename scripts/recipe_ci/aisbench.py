#!/usr/bin/env python3
"""Render AISBench inputs and translate artifacts to the step-result contract."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recipe_ci.result import write_json_atomic  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    accuracy = subparsers.add_parser("accuracy")
    accuracy.add_argument("--artifact-directory", type=Path, required=True)
    accuracy.add_argument("--result-file", type=Path, required=True)
    accuracy.add_argument("--baseline", type=float)
    accuracy.add_argument("--allowed-drop", type=float, default=0.0)

    performance = subparsers.add_parser("performance")
    performance.add_argument("--artifact-directory", type=Path, required=True)
    performance.add_argument("--result-file", type=Path, required=True)

    render = subparsers.add_parser("render-model-config")
    render.add_argument("--template", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def render_model_config(template: Path, output: Path) -> None:
    substitutions = {
        "__RECIPE_MODEL_PATH__": ("RECIPE_MODEL_PATH", None, False),
        "__RECIPE_SERVED_MODEL_NAME__": (
            "RECIPE_SERVED_MODEL_NAME",
            None,
            False,
        ),
        "__RECIPE_ENDPOINT_HOST__": ("RECIPE_ENDPOINT_HOST", None, False),
        "__RECIPE_ENDPOINT_PORT__": ("RECIPE_ENDPOINT_PORT", None, True),
    }
    content = template.read_text(encoding="utf-8")
    for placeholder, (name, default, numeric) in substitutions.items():
        value = os.environ.get(name, default)
        if not value:
            raise RuntimeError(f"required environment variable is missing: {name}")
        replacement = str(int(value)) if numeric else repr(value)
        content = content.replace(placeholder, replacement)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


def _latest_run(directory: Path) -> Path:
    output_root = directory / "outputs/default"
    runs = (
        [path for path in output_root.iterdir() if path.is_dir()]
        if output_root.is_dir()
        else []
    )
    if not runs:
        raise RuntimeError(f"AISBench run directory not found under {output_root}")
    return max(runs, key=lambda path: path.stat().st_mtime)


def _latest_file(directory: Path, pattern: str, label: str) -> Path:
    files = list(directory.glob(pattern))
    if not files:
        raise RuntimeError(f"AISBench {label} not found under {directory}")
    return max(files, key=lambda path: path.stat().st_mtime)


def _number_with_unit(value: object, unit: str, field: str) -> float:
    if not isinstance(value, str) or not value.endswith(unit):
        raise RuntimeError(f"invalid AISBench {field}: {value!r}")
    try:
        return float(value.removesuffix(unit))
    except ValueError as error:
        raise RuntimeError(f"invalid AISBench {field}: {value!r}") from error


def accuracy_score(directory: Path) -> tuple[float, Path]:
    """Read the fixed AISBench 3.1 summary CSV contract."""
    summary_directory = _latest_run(directory) / "summary"
    path = _latest_file(summary_directory, "summary_*.csv", "summary CSV")
    with path.open(newline="", encoding="utf-8-sig") as input_file:
        reader = csv.DictReader(input_file)
        fields = reader.fieldnames or []
        prefix = ["dataset", "version", "metric", "mode"]
        if fields[:4] != prefix:
            raise RuntimeError(f"invalid AISBench accuracy columns in {path}")
        score_fields = fields[4:]
        if score_fields and score_fields[0] == "total_count":
            score_fields = score_fields[1:]
        if len(score_fields) != 1:
            raise RuntimeError(f"expected one AISBench model column in {path}")
        rows = [row for row in reader if row["metric"] == "accuracy"]

    if len(rows) != 1:
        raise RuntimeError(f"expected one AISBench accuracy row in {path}")
    value = rows[0][score_fields[0]]
    match = (
        re.fullmatch(
            r"(-?(?:\d+(?:\.\d*)?|\.\d+))(?: \(\d+/\d+\))?", value
        )
        if value is not None
        else None
    )
    if match is None:
        raise RuntimeError(f"invalid AISBench accuracy score in {path}: {value!r}")
    return float(match.group(1)), path


def performance_metrics(directory: Path) -> tuple[dict[str, float], list[Path]]:
    """Read the fixed AISBench 3.1 default_perf JSON/CSV pair."""
    performance_root = _latest_run(directory) / "performances"
    json_files = list(performance_root.glob("*/*.json"))
    if len(json_files) != 1:
        raise RuntimeError(
            f"expected one AISBench performance JSON under {performance_root}"
        )
    json_path = json_files[0]
    csv_path = json_path.with_suffix(".csv")
    if not csv_path.is_file():
        raise RuntimeError(f"AISBench performance CSV not found: {csv_path}")

    try:
        value = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"invalid AISBench performance JSON: {json_path}") from error
    try:
        request_rate = value["Request Throughput"]["total"]
        output_rate = value["Output Token Throughput"]["total"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(f"invalid AISBench performance JSON: {json_path}") from error

    expected_columns = [
        "Performance Parameters",
        "Stage",
        "Average",
        "Min",
        "Max",
        "Median",
        "P75",
        "P90",
        "P99",
        "N",
    ]
    with csv_path.open(newline="", encoding="utf-8-sig") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames != expected_columns:
            raise RuntimeError(f"invalid AISBench performance columns in {csv_path}")
        rows = {
            row["Performance Parameters"]: row
            for row in reader
            if row["Stage"] == "total"
        }

    try:
        e2el = rows["E2EL"]["Average"]
        ttft = rows["TTFT"]["Average"]
        tpot = rows["TPOT"]["Average"]
    except KeyError as error:
        raise RuntimeError(f"invalid AISBench performance CSV: {csv_path}") from error

    metrics = {
        "request_per_second": _number_with_unit(
            request_rate, " req/s", "Request Throughput.total"
        ),
        "output_token_per_second": _number_with_unit(
            output_rate, " token/s", "Output Token Throughput.total"
        ),
        "e2e_latency_ms": _number_with_unit(e2el, " ms", "E2EL.Average"),
        "ttft_ms": _number_with_unit(ttft, " ms", "TTFT.Average"),
        "tpot_ms": _number_with_unit(tpot, " ms", "TPOT.Average"),
    }
    return metrics, [json_path, csv_path]


def relative_artifacts(paths: Iterable[Path], root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in paths]


def main() -> int:
    args = parse_args()
    try:
        if args.action == "render-model-config":
            render_model_config(args.template, args.output)
            return 0
        if args.action == "accuracy":
            score, source = accuracy_score(args.artifact_directory)
            metrics: dict[str, float] = {"accuracy": score}
            status = "passed"
            if args.baseline is not None:
                metrics.update(
                    baseline=args.baseline,
                    allowed_drop=args.allowed_drop,
                )
                if score < args.baseline - args.allowed_drop:
                    status = "failed"
            write_json_atomic(
                args.result_file,
                {
                    "status": status,
                    "type": "accuracy",
                    "mode": "gate" if args.baseline is not None else "smoke",
                    "metrics": metrics,
                    "artifacts": relative_artifacts(
                        [source], args.artifact_directory
                    ),
                },
            )
            if status == "failed":
                raise RuntimeError(
                    f"accuracy {score} is below {args.baseline - args.allowed_drop}"
                )
            return 0

        metrics, sources = performance_metrics(args.artifact_directory)
        write_json_atomic(
            args.result_file,
            {
                "status": "passed",
                "type": "performance",
                "metrics": metrics,
                "artifacts": relative_artifacts(sources, args.artifact_directory),
            },
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
