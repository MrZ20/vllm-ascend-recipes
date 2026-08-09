from __future__ import annotations

import errno
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.recipe_ci.coordinator import (  # noqa: E402
    CoordinatorClient,
    CoordinatorError,
    LeaderCoordinator,
    RunState,
)


class FakeResponse:
    def __init__(self, value: object) -> None:
        self.body = json.dumps(value).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class SequenceOpener:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    def open(self, request: object, timeout: float) -> FakeResponse:
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        assert isinstance(outcome, FakeResponse)
        return outcome


def refused() -> urllib.error.URLError:
    return urllib.error.URLError(
        ConnectionRefusedError(errno.ECONNREFUSED, "connection refused")
    )


class RunStateTests(unittest.TestCase):
    def test_cleanup_does_not_replace_execution_status(self) -> None:
        state = RunState(["node0", "node1"])
        state.mark_ready("node0")
        state.mark_failed("node0", "service stopped")
        state.mark_cleaned("node0")
        state.mark_cleaned("node0")

        snapshot = state.snapshot()
        self.assertEqual(snapshot["nodes"]["node0"], "failed")
        self.assertEqual(snapshot["failures"], {"node0": "service stopped"})
        self.assertEqual(snapshot["cleaned"], ["node0"])

    def test_passing_run_records_success_before_cleanup(self) -> None:
        state = RunState(["node0", "node1"])
        state.mark_ready("node0")
        state.mark_ready("node1")
        state.finish("passed")
        state.mark_cleaned("node0")

        self.assertEqual(
            state.snapshot()["nodes"], {"node0": "passed", "node1": "passed"}
        )

    def test_first_failure_and_terminal_state_are_preserved(self) -> None:
        state = RunState(["node0", "node1"])
        state.mark_failed("node0", "first failure")
        state.mark_failed("node1", "second failure")

        snapshot = state.snapshot()
        self.assertEqual(snapshot["message"], "node0: first failure")
        self.assertEqual(
            snapshot["failures"],
            {"node0": "first failure", "node1": "second failure"},
        )
        with self.assertRaises(CoordinatorError):
            state.finish("passed")

    def test_unknown_node_is_rejected(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "unknown node"):
            RunState(["node0"]).mark_cleaned("node9")


class CoordinatorHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = LeaderCoordinator(
            ["node0", "node1"], 0, host="127.0.0.1"
        )
        self.coordinator.start()
        self.addCleanup(self.coordinator.close)
        self.client = CoordinatorClient("127.0.0.1", self.coordinator.port)

    def test_ready_terminal_and_cleanup_round_trip(self) -> None:
        for node_id in ("node0", "node1"):
            self.client.mark_ready(node_id, 1)
        self.coordinator.wait_ready(1, lambda: None)
        self.coordinator.state.finish("passed")
        for node_id in ("node0", "node1"):
            self.client.mark_cleaned(node_id)
        self.coordinator.wait_cleaned(1)

        snapshot = self.client.wait_terminal(1, lambda: None)
        self.assertEqual(snapshot["status"], "passed")
        self.assertEqual(snapshot["cleaned"], ["node0", "node1"])
        self.assertEqual(
            snapshot["nodes"], {"node0": "passed", "node1": "passed"}
        )

    def test_invalid_request_returns_a_simple_error(self) -> None:
        with self.assertRaisesRegex(CoordinatorError, "unknown node"):
            self.client.mark_cleaned("node9")


class CoordinatorStartupTests(unittest.TestCase):
    def test_startup_wait_retries_connection_refusal_until_available(self) -> None:
        client = CoordinatorClient("coordinator.invalid", 1, request_timeout=0.01)
        opener = SequenceOpener(
            [refused(), refused(), FakeResponse({"status": "running"})]
        )
        client.opener = opener  # type: ignore[assignment]

        with patch("scripts.recipe_ci.coordinator.time.sleep"):
            client.wait_available(3, lambda: None)

        self.assertEqual(opener.calls, 3)

    def test_normal_operations_do_not_apply_generic_retries(self) -> None:
        client = CoordinatorClient("coordinator.invalid", 1, request_timeout=0.01)
        opener = SequenceOpener([refused(), FakeResponse({})])
        client.opener = opener  # type: ignore[assignment]

        with self.assertRaises(CoordinatorError) as raised:
            client.mark_ready("node0", 1)

        self.assertEqual(raised.exception.code, "coordinator_unreachable")
        self.assertEqual(opener.calls, 1)


if __name__ == "__main__":
    unittest.main()
