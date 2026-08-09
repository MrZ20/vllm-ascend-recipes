#!/usr/bin/env python3
"""HTTP coordination for Recipe CI nodes."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable


RUNNING = "running"


class CoordinatorError(RuntimeError):
    def __init__(self, message: str, *, code: str = "coordinator_error") -> None:
        super().__init__(message)
        self.code = code


class RunState:
    def __init__(self, node_ids: list[str]) -> None:
        self.nodes = {node_id: "pending" for node_id in node_ids}
        self.failures: dict[str, str] = {}
        self.cleaned: set[str] = set()
        self.status = RUNNING
        self.message = ""
        self.condition = threading.Condition()

    def mark_ready(self, node_id: str) -> None:
        with self.condition:
            status = self._node_status(node_id)
            if status == "ready":
                return
            if self.status != RUNNING or status != "pending":
                raise CoordinatorError(f"node {node_id} cannot become ready")
            self.nodes[node_id] = "ready"
            self.condition.notify_all()

    def mark_failed(self, node_id: str, message: str) -> None:
        with self.condition:
            self._node_status(node_id)
            if node_id in self.failures:
                return
            if self.status in {"passed", "cancelled"}:
                raise CoordinatorError(f"run is already {self.status}")
            self.nodes[node_id] = "failed"
            self.failures[node_id] = message
            if self.status == RUNNING:
                self.status = "failed"
                self.message = f"{node_id}: {message}"
            self.condition.notify_all()

    def mark_cleaned(self, node_id: str) -> None:
        with self.condition:
            self._node_status(node_id)
            self.cleaned.add(node_id)
            self.condition.notify_all()

    def finish(self, status: str, message: str = "") -> None:
        with self.condition:
            if status not in {"passed", "cancelled"}:
                raise CoordinatorError(f"invalid terminal status: {status}")
            if self.status == status:
                return
            if self.status != RUNNING:
                raise CoordinatorError(
                    f"run is already {self.status}; cannot finish as {status}"
                )
            self.status = status
            self.message = message
            for node_id, node_status in self.nodes.items():
                if status == "passed" and node_status == "ready":
                    self.nodes[node_id] = "passed"
                elif status == "cancelled" and node_status != "failed":
                    self.nodes[node_id] = "cancelled"
            self.condition.notify_all()

    def snapshot(self) -> dict[str, object]:
        with self.condition:
            return {
                "status": self.status,
                "message": self.message,
                "nodes": dict(sorted(self.nodes.items())),
                "failures": dict(sorted(self.failures.items())),
                "cleaned": sorted(self.cleaned),
            }

    def _node_status(self, node_id: str) -> str:
        try:
            return self.nodes[node_id]
        except KeyError as error:
            raise CoordinatorError(f"unknown node: {node_id}") from error


def _handler(state: RunState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/state":
                self._send(200, state.snapshot())
            else:
                self._send(404, {"error": "endpoint not found"})

        def do_POST(self) -> None:  # noqa: N802
            parts = self.path.strip("/").split("/")
            if len(parts) != 3 or parts[0] != "nodes":
                self._send(404, {"error": "endpoint not found"})
                return
            node_id, action = parts[1:]
            try:
                if action == "ready":
                    state.mark_ready(node_id)
                elif action == "failed":
                    state.mark_failed(node_id, self._failure_message())
                elif action == "cleaned":
                    state.mark_cleaned(node_id)
                else:
                    self._send(404, {"error": "endpoint not found"})
                    return
            except CoordinatorError as error:
                self._send(400, {"error": str(error)})
                return
            self._send(200, state.snapshot())

        def _failure_message(self) -> str:
            try:
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length))["message"]
                if not isinstance(message, str) or not message:
                    raise ValueError
                return message
            except (LookupError, TypeError, ValueError) as error:
                raise CoordinatorError("invalid failure message") from error

        def _send(self, status: int, value: object) -> None:
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


class LeaderCoordinator:
    def __init__(
        self, node_ids: list[str], port: int, *, host: str = "0.0.0.0"
    ) -> None:
        self.state = RunState(node_ids)
        self.server = ThreadingHTTPServer((host, port), _handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def wait_ready(self, timeout: int, check_processes: Callable[[], None]) -> None:
        self._wait(
            lambda: all(status == "ready" for status in self.state.nodes.values()),
            timeout,
            "nodes to become ready",
            check_processes,
            stop_on_terminal=True,
        )

    def wait_cleaned(self, timeout: int) -> None:
        self._wait(
            lambda: self.state.cleaned == self.state.nodes.keys(),
            timeout,
            "nodes to report cleanup",
            lambda: None,
        )

    def raise_if_failed(self) -> None:
        with self.state.condition:
            if self.state.status == "failed":
                raise CoordinatorError(self.state.message)

    def _wait(
        self,
        complete: Callable[[], bool],
        timeout: int,
        description: str,
        check_processes: Callable[[], None],
        *,
        stop_on_terminal: bool = False,
    ) -> None:
        deadline = time.monotonic() + timeout
        with self.state.condition:
            while not complete():
                check_processes()
                if stop_on_terminal and self.state.status != RUNNING:
                    raise CoordinatorError(self.state.message or self.state.status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CoordinatorError(f"timed out waiting for {description}")
                self.state.condition.wait(min(1, remaining))


class CoordinatorClient:
    def __init__(
        self, host: str, port: int, *, request_timeout: float = 5.0
    ) -> None:
        self.base_url = f"http://{host}:{port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.request_timeout = request_timeout

    def mark_ready(self, node_id: str, timeout: int) -> None:
        self._request(f"/nodes/{node_id}/ready", {}, timeout)

    def mark_failed(self, node_id: str, message: str, timeout: int = 5) -> None:
        self._request(f"/nodes/{node_id}/failed", {"message": message}, timeout)

    def mark_cleaned(self, node_id: str, timeout: int = 5) -> None:
        self._request(f"/nodes/{node_id}/cleaned", {}, timeout)

    def wait_available(
        self, timeout: int, check_processes: Callable[[], None]
    ) -> None:
        deadline = time.monotonic() + timeout
        while True:
            check_processes()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CoordinatorError(
                    "timed out waiting for the coordinator",
                    code="coordinator_unreachable",
                )
            try:
                self._request("/state", None, remaining)
                return
            except CoordinatorError as error:
                if error.code != "coordinator_unreachable":
                    raise
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def wait_terminal(
        self, timeout: int, check_processes: Callable[[], None]
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while True:
            check_processes()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CoordinatorError("timed out waiting for the leader result")
            state = self._request("/state", None, remaining)
            if state["status"] != RUNNING:
                return state
            time.sleep(min(1, max(0, deadline - time.monotonic())))

    def _request(
        self, path: str, value: object | None, timeout: float
    ) -> dict[str, object]:
        body = None if value is None else json.dumps(value).encode()
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with self.opener.open(
                request, timeout=min(timeout, self.request_timeout)
            ) as response:
                result = json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read()).get("error")
            except (AttributeError, ValueError):
                message = None
            error.close()
            raise CoordinatorError(
                str(message or f"coordinator returned HTTP {error.code}")
            ) from error
        except OSError as error:
            raise CoordinatorError(
                f"cannot reach coordinator at {self.base_url}",
                code="coordinator_unreachable",
            ) from error
        except ValueError as error:
            raise CoordinatorError("coordinator returned invalid JSON") from error
        if not isinstance(result, dict):
            raise CoordinatorError("coordinator returned invalid JSON")
        return result
