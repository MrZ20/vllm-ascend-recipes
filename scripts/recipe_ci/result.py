#!/usr/bin/env python3
"""Small JSON result helpers shared by the runner and evaluation scripts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


RESULT_SCHEMA_VERSION = "recipe-ci-result/v1"
NODE_RESULT_SCHEMA_VERSION = "recipe-ci-node-result/v1"
FAILURE_CATEGORIES = frozenset(
    {
        "launch_failed",
        "startup_timeout",
        "gateway_failed",
        "node_failed",
        "check_failed",
        "evaluation_failed",
        "coordinator_unreachable",
        "cancelled",
        "cleanup_failed",
        "internal_error",
    }
)
FINAL_STATUSES = frozenset({"passed", "failed", "cancelled"})


@dataclass(frozen=True)
class RunFailure:
    category: str
    message: str

    def __post_init__(self) -> None:
        if self.category not in FAILURE_CATEGORIES:
            raise ValueError(f"unknown failure category: {self.category}")
        if not self.message:
            raise ValueError("failure message must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return {"category": self.category, "message": self.message}


def _cleanup_failures(errors: Iterable[RunFailure]) -> list[RunFailure]:
    cleanup = list(errors)
    if any(error.category != "cleanup_failed" for error in cleanup):
        raise ValueError("cleanup_errors must use category cleanup_failed")
    return cleanup


def _outcome(
    status: str,
    failure: RunFailure | None,
    cleanup: list[RunFailure],
) -> tuple[str, RunFailure | None]:
    if status not in FINAL_STATUSES:
        raise ValueError(f"unknown result status: {status}")
    if failure is None and cleanup:
        status, failure = "failed", cleanup[0]
    if status == "passed" and failure is not None:
        raise ValueError("passed result must not contain a primary failure")
    if status != "passed" and failure is None:
        raise ValueError(f"{status} result requires a primary failure")
    return status, failure


def build_node_result(
    *,
    node_id: str,
    status: str,
    failure: RunFailure | None = None,
    cleanup_errors: Iterable[RunFailure] = (),
) -> dict[str, Any]:
    cleanup = _cleanup_failures(cleanup_errors)
    status, failure = _outcome(status, failure, cleanup)
    return {
        "schema_version": NODE_RESULT_SCHEMA_VERSION,
        "node_id": node_id,
        "status": status,
        "failure": failure.to_dict() if failure else None,
        "cleanup_errors": [error.to_dict() for error in cleanup],
    }


def build_final_result(
    *,
    plan: str,
    status: str,
    nodes: Mapping[str, Mapping[str, Any]] | None = None,
    checks: Mapping[str, Any] | None = None,
    evaluations: Mapping[str, Any] | None = None,
    failure: RunFailure | None = None,
    cleanup_errors: Iterable[RunFailure] = (),
) -> dict[str, Any]:
    """Build the execution result; cleanup errors never replace its failure."""
    cleanup = _cleanup_failures(cleanup_errors)
    status, failure = _outcome(status, failure, cleanup)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "plan": plan,
        "status": status,
        "failure": failure.to_dict() if failure else None,
        "cleanup_errors": [error.to_dict() for error in cleanup],
        "nodes": {node_id: dict(value) for node_id, value in (nodes or {}).items()},
        "checks": dict(checks or {}),
        "evaluations": dict(evaluations or {}),
    }


def write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            json.dump(value, output, ensure_ascii=False, allow_nan=False, indent=2)
            output.write("\n")
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value
