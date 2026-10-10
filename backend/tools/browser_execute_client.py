"""Container-side client for the host browser execution service.

WHAT THIS IS FOR

`browser_invocation.SubprocessInvoker` spawns `browser_worker_command` with the task on stdin and reads
a JSON report from stdout. This script satisfies that contract while forwarding the work to the host,
where Chromium and Playwright actually are.

Point `browser_worker_command` at THIS file, not at `browser_worker.py`. That is the mistake an
operator will naturally make: `browser_worker.py` exists in the image, looks correct, and immediately
reports `WORKER_CRASH: No module named 'playwright'` because its dependencies are not.

So `browser_invocation.py` needs no change. The invoker's contract is met by a client instead of a
worker, and every lease, RLS binding and attempt record stays in the container where it already is.

WHAT IT NEVER DOES

It does not read the database, does not hold credentials, and does not decide anything. It moves bytes:
stdin -> HTTP -> stdout. If the service is unreachable it prints an UNCERTAIN report, because a task that
was never delivered and a task whose outcome is unknown must not be reported as a definite failure.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = os.environ.get("BROWSER_EXECUTE_URL", "http://172.18.0.1:8765/execute")
TIMEOUT_SECONDS = int(os.environ.get("BROWSER_EXECUTE_TIMEOUT", "600"))


def _uncertain(kind: str, detail: str) -> dict:
    # Not FAILED. A request that could not be delivered leaves the same question open as one whose
    # answer was lost: did anything happen? Answering "no" here would license a retry.
    return {
        "status": "UNCERTAIN",
        "outcome_certain": False,
        "problems": [{"kind": kind, "detail": detail}],
    }


def main(argv: list[str] | None = None) -> int:
    url = (argv or sys.argv[1:] or [DEFAULT_URL])[0]
    raw = sys.stdin.buffer.read()
    try:
        request = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        print(json.dumps(_uncertain("CLIENT_BAD_TASK", "stdin was not a JSON object")))
        return 0

    body = json.dumps(request).encode()
    http_request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(http_request, timeout=TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        print(json.dumps(_uncertain("EXECUTE_SERVICE_ERROR", f"service returned HTTP {exc.code}")))
        return 0
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(
            json.dumps(
                _uncertain(
                    "EXECUTE_SERVICE_UNREACHABLE",
                    f"the host browser service could not be reached at {url} ({type(exc).__name__}); "
                    "no browser was launched by this call",
                )
            )
        )
        return 0

    try:
        report = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        print(json.dumps(_uncertain("EXECUTE_SERVICE_BAD_REPLY", "service reply was not JSON")))
        return 0

    print(json.dumps(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry
    raise SystemExit(main())
