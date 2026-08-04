#!/usr/bin/env python3
"""Tiny HTTP coordination protocol used by the local multi-node runner."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


class CoordinatorError(RuntimeError):
    """The coordinated run failed or timed out."""


class RunState:
    def __init__(self, node_ids: list[str]) -> None:
        self.node_ids = set(node_ids)
        self.ready: set[str] = set()
        self.completed: set[str] = set()
        self.status = "running"
        self.message = ""
        self.condition = threading.Condition()

    def mark_ready(self, node_id: str) -> None:
        with self.condition:
            self._check_node(node_id)
            self.ready.add(node_id)
            self.condition.notify_all()

    def mark_failed(self, node_id: str, message: str) -> None:
        with self.condition:
            self._check_node(node_id)
            if self.status != "failed":
                self.status = "failed"
                self.message = f"{node_id}: {message}"
            self.condition.notify_all()

    def mark_completed(self, node_id: str) -> None:
        with self.condition:
            self._check_node(node_id)
            self.completed.add(node_id)
            self.condition.notify_all()

    def finish(self, status: str, message: str = "") -> None:
        with self.condition:
            if self.status == "running":
                self.status = status
                self.message = message
            self.condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self.condition:
            return {
                "status": self.status,
                "message": self.message,
                "ready": sorted(self.ready),
                "completed": sorted(self.completed),
                "expected": sorted(self.node_ids),
            }

    def _check_node(self, node_id: str) -> None:
        if node_id not in self.node_ids:
            raise CoordinatorError(f"unknown node: {node_id}")


def _handler(state: RunState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path != "/state":
                self.send_error(404)
                return
            self._send_json(200, state.snapshot())

        def do_POST(self) -> None:  # noqa: N802
            parts = self.path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "nodes":
                self.send_error(404)
                return

            node_id, action = parts[1], parts[2]
            try:
                if action == "ready":
                    state.mark_ready(node_id)
                elif action == "failed":
                    state.mark_failed(
                        node_id, self._read_json().get("message", "failed")
                    )
                elif action == "complete":
                    state.mark_completed(node_id)
                else:
                    self.send_error(404)
                    return
            except CoordinatorError as error:
                self._send_json(400, {"error": str(error)})
                return
            self._send_json(200, state.snapshot())

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", "0"))
            if length == 0:
                return {}
            value = json.loads(self.rfile.read(length))
            return value if isinstance(value, dict) else {}

        def _send_json(self, status: int, value: object) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


class LeaderCoordinator:
    def __init__(self, node_ids: list[str], port: int) -> None:
        self.state = RunState(node_ids)
        self.server = ThreadingHTTPServer(("0.0.0.0", port), _handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def wait_ready(self, timeout: int, check_processes: Callable[[], None]) -> None:
        self._wait_for(
            lambda: self.state.ready == self.state.node_ids,
            timeout,
            "waiting for all nodes to become ready",
            check_processes,
        )

    def wait_completed(self, timeout: int) -> None:
        self._wait_for(
            lambda: self.state.completed == self.state.node_ids,
            timeout,
            "waiting for nodes to acknowledge completion",
            lambda: None,
            fail_on_terminal=False,
        )

    def raise_if_failed(self) -> None:
        snapshot = self.state.snapshot()
        if snapshot["status"] == "failed":
            raise CoordinatorError(str(snapshot["message"]))

    def _wait_for(
        self,
        complete: Callable[[], bool],
        timeout: int,
        description: str,
        check_processes: Callable[[], None],
        fail_on_terminal: bool = True,
    ) -> None:
        deadline = time.monotonic() + timeout
        with self.state.condition:
            while not complete():
                check_processes()
                if fail_on_terminal and self.state.status == "failed":
                    raise CoordinatorError(self.state.message)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoordinatorError(f"timed out {description}")
                self.state.condition.wait(min(1, remaining))


class CoordinatorClient:
    def __init__(self, host: str, port: int) -> None:
        self.base_url = f"http://{host}:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def mark_ready(self, node_id: str, timeout: int) -> None:
        self._post(f"/nodes/{node_id}/ready", {}, timeout)

    def mark_failed(self, node_id: str, message: str, timeout: int = 5) -> None:
        self._post(f"/nodes/{node_id}/failed", {"message": message}, timeout)

    def mark_completed(self, node_id: str, timeout: int = 5) -> None:
        self._post(f"/nodes/{node_id}/complete", {}, timeout)

    def wait_terminal(
        self,
        timeout: int,
        check_processes: Callable[[], None],
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            check_processes()
            state = self._get_state(deadline)
            if state["status"] != "running":
                return state
            time.sleep(1)
        raise CoordinatorError("timed out waiting for the leader result")

    def _get_state(self, deadline: float) -> dict[str, object]:
        while True:
            try:
                with self.opener.open(f"{self.base_url}/state", timeout=2) as response:
                    return json.loads(response.read())
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise CoordinatorError("cannot reach the leader coordinator")
                time.sleep(1)

    def _post(self, path: str, value: object, timeout: int) -> None:
        body = json.dumps(value).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                with self.opener.open(request, timeout=2):
                    return
            except (OSError, urllib.error.URLError):
                if time.monotonic() >= deadline:
                    raise CoordinatorError(f"cannot call coordinator endpoint {path}")
                time.sleep(1)
