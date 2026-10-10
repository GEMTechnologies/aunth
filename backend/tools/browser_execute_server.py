"""Host-side browser execution service — the bridge the container cannot be.

WHY THIS EXISTS, MEASURED

The container holds the worker FILE but none of its DEPENDENCIES, and cannot start a browser:

    /app/backend/tools/browser_worker.py   EXISTS in the image
    playwright / browser_use / stagehand   NOT importable in the container
    Chromium on disk in the container      absent
    user namespaces in the container       absent (CapEff 0000000000000000)
    host eval venv mounted into container  no - only postgres declares a volume

So the browser worker runs here, on the host. The container reaches it through
`tools/browser_execute_client.py`, which `browser_worker_command` points at. `SubprocessInvoker`
spawns the client, the client forwards the task, and this service returns the worker's report verbatim —
so `browser_invocation.py` needs no change at all.

WHAT THIS PROCESS HOLDS: NOTHING

**No database credentials. No tenant records. No package data at rest.** It receives a task, spawns one
worker process, returns what the worker said, and forgets it. That is strictly less privilege than the
alternative of giving a host process a database connection, and it is the strongest available answer to
§12's requirement that a browser worker not receive unrestricted tenant access.

It also plans nothing and decides nothing — every decision was already made and persisted by
`agent_workflows` in the container.

THE BIND ADDRESS IS THE SECURITY BOUNDARY

It listens on the Docker bridge gateway (`172.18.0.1` on this host) and on nothing else. That address is
reachable from the Granada containers and **not** from the public interface. Binding `0.0.0.0` would
publish an endpoint that launches browsers to the internet, and the default here refuses to.

CONCURRENCY IS ONE, ENFORCED BY A LOCK

Measured memory would allow ~3 sessions. The ceiling is credential blast radius: one organisation's
credentials in one browser at a time. The lock is what makes that true under concurrent requests rather
than merely intended.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(BACKEND, "tools", "browser_worker.py")

#: The hard concurrency bound. A lock rather than a comment: two simultaneous requests must not open
#: two browsers.
_SESSION_LOCK = threading.Lock()

DEFAULT_PORT = 8765
DEFAULT_TIMEOUT = 600
MAX_BODY_BYTES = 4 * 1024 * 1024


def execute(request: dict[str, Any], *, interpreter: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Run one worker for one request and return its report.

    Failure paths are UNCERTAIN for the same reason the container's invoker uses UNCERTAIN: a worker
    that died may have acted before it did, and a definite failure invites a retry that could file a
    second application.
    """
    command = [interpreter or sys.executable, WORKER]
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(request).encode(),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [
                {
                    "kind": "WORKER_TIMEOUT",
                    "detail": (
                        f"the worker exceeded {timeout}s; if it had reached the submission step the "
                        "outcome is unknown, so this must be reconciled rather than retried"
                    ),
                }
            ],
        }

    if completed.returncode != 0:
        return {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [
                {
                    "kind": "WORKER_EXIT",
                    "detail": f"worker exited {completed.returncode} without reporting an outcome",
                    "stderr_tail": completed.stderr.decode("utf-8", "replace")[-400:],
                }
            ],
        }

    try:
        return json.loads(completed.stdout.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {
            "status": "UNCERTAIN",
            "outcome_certain": False,
            "problems": [{"kind": "WORKER_UNPARSEABLE_OUTPUT"}],
        }


class Handler(BaseHTTPRequestHandler):
    interpreter: Optional[str] = None
    timeout: int = DEFAULT_TIMEOUT

    def _reply(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path == "/health":
            self._reply(200, {"ok": True, "worker": os.path.exists(WORKER), "concurrency": 1})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/execute":
            self._reply(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY_BYTES:
            # DRAIN THE BODY BEFORE REPLYING. Replying 413 without reading it leaves the client writing
            # into a closed socket and it sees ConnectionAbortedError instead of the status - so a
            # caller cannot tell "too large" from "the service died", which is exactly the distinction
            # this whole bridge is careful about elsewhere.
            if 0 < length <= MAX_BODY_BYTES * 4:
                try:
                    self.rfile.read(length)
                except OSError:
                    pass
            self._reply(413, {"error": f"body must be 1..{MAX_BODY_BYTES} bytes"})
            return

        try:
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._reply(400, {"error": "body is not JSON"})
            return
        if not isinstance(request, dict):
            self._reply(400, {"error": "body must be a JSON object"})
            return

        # THE CONCURRENCY BOUND. A second request arriving mid-run waits rather than opening a second
        # browser. `blocking=True` is deliberate: refusing outright would report a spurious failure for
        # work that is merely queued behind one session.
        with _SESSION_LOCK:
            report = execute(request, interpreter=self.interpreter, timeout=self.timeout)
        self._reply(200, report)

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log the body: it carries an organisation's form data.
        sys.stderr.write(f"[browser-execute] {fmt % args}\n")


def build_server(host: str, port: int, *, interpreter: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"interpreter": interpreter, "timeout": timeout})
    return ThreadingHTTPServer((host, port), handler)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Host-side browser execution service (one session at a time)")
    parser.add_argument(
        "--bind",
        default="172.18.0.1",
        help="bridge address to listen on. NEVER 0.0.0.0 - that publishes browser execution to the internet",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interpreter", default=None, help="python that has playwright (the eval venv)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    args = parser.parse_args(argv)

    if args.bind in ("0.0.0.0", "::", ""):
        raise SystemExit(
            "refusing to bind 0.0.0.0: that publishes an endpoint which launches browsers. "
            "Use the Docker bridge gateway address."
        )
    if not os.path.exists(WORKER):
        raise SystemExit(f"worker not found at {WORKER}")

    server = build_server(args.bind, args.port, interpreter=args.interpreter, timeout=args.timeout)
    print(f"browser execution service on http://{args.bind}:{args.port}  (concurrency 1)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
