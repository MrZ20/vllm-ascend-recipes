#!/usr/bin/env python3
"""Temporarily make upstream external-DP worker failures visible to Recipe CI."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {Path(sys.argv[0]).name} LAUNCHER [ARG ...]", file=sys.stderr)
        return 2

    launcher = Path(sys.argv[1]).resolve()
    if not launcher.is_file():
        print(f"external-DP launcher not found: {launcher}", file=sys.stderr)
        return 2

    # TEMPORARY UPSTREAM WORKAROUND.
    # vLLM Ascend v0.23.0rc1 joins these workers without checking exit codes:
    # https://github.com/vllm-project/vllm-ascend/blob/f4a08bddd0cc65a0bd8c3d377b158ae5ca7527db/examples/external_online_dp/launch_online_dp.py
    # The launcher was introduced by https://github.com/vllm-project/vllm-ascend/pull/2685.
    # Delete this adapter once upstream propagates worker failures, and execute
    # launch_online_dp.py directly from each generated node script.
    sys.argv = [str(launcher), *sys.argv[2:]]
    namespace = runpy.run_path(str(launcher), run_name="__main__")
    processes = namespace.get("processes")
    if not isinstance(processes, list) or any(
        not hasattr(process, "exitcode") for process in processes
    ):
        print(
            "external-DP launcher contract changed: expected a processes list; "
            "remove or update the temporary upstream workaround",
            file=sys.stderr,
        )
        return 2
    failures = [
        (index, process.exitcode)
        for index, process in enumerate(processes)
        if process.exitcode not in (None, 0)
    ]
    if not failures:
        return 0

    for index, exit_code in failures:
        print(
            f"external-DP worker {index} exited with {exit_code}",
            file=sys.stderr,
        )
    first_exit_code = failures[0][1]
    return first_exit_code if first_exit_code and 0 < first_exit_code < 256 else 1


if __name__ == "__main__":
    raise SystemExit(main())
