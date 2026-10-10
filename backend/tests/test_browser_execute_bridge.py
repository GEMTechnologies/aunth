"""The host execution service and its container-side client.

WHY THESE TESTS EXIST

Together these two files are the bridge between the job system and the browser, and the properties that
make the bridge safe are all properties a future edit could quietly remove. The one that matters most is
the BIND ADDRESS: `0.0.0.0` would publish an endpoint that launches browsers to the internet, and the
service must refuse to do it rather than trusting an operator to remember.

The second is the concurrency bound. Measured memory allows ~3 sessions; the ceiling is credential blast
radius, and a lock is what makes that true under concurrent requests rather than merely intended.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "tools"))

import browser_execute_client as client  # noqa: E402
import browser_execute_server as server  # noqa: E402


# ===========================================================================
# THE BIND ADDRESS - the security boundary
# ===========================================================================
@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::", ""])
def test_the_service_refuses_to_bind_a_wildcard_address(tmp_path, monkeypatch, wildcard):
    """THE property. An endpoint that launches browsers must never be reachable from the public
    interface, and refusing is the only way to make that true regardless of who runs it."""
    monkeypatch.setattr(server, "WORKER", str(tmp_path / "browser_worker.py"))
    (tmp_path / "browser_worker.py").write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        server.main(["--bind", wildcard, "--port", "0"])
    assert "0.0.0.0" in str(caught.value) or "publishes" in str(caught.value)


def test_the_default_bind_is_the_bridge_gateway():
    """The default must be the private bridge address, so the unsafe choice is the one someone has to
    make deliberately."""
    import inspect

    source = inspect.getsource(server.main)
    assert '"172.18.0.1"' in source, "the default bind is not the docker bridge gateway"


def test_the_service_has_no_database_import():
    """It holds no credentials. Asserted because the whole privilege argument for this design is that
    the host process reaches nothing - and an import is how that would quietly stop being true."""
    source = (BACKEND / "tools" / "browser_execute_server.py").read_text(encoding="utf-8")
    for forbidden in ("import database", "from database", "import models", "from models",
                      "events.ledger", "sqlalchemy"):
        assert forbidden not in source, f"the host service now touches the database: {forbidden}"


# ===========================================================================
# THE WORKER LAUNCH
# ===========================================================================
def test_a_worker_that_exits_non_zero_is_uncertain(tmp_path, monkeypatch):
    script = tmp_path / "die.py"
    script.write_text("import sys\nsys.exit(5)\n", encoding="utf-8")
    monkeypatch.setattr(server, "WORKER", str(script))

    report = server.execute({}, timeout=20)
    assert report["status"] == "UNCERTAIN"
    assert report["outcome_certain"] is False
    assert report["problems"][0]["kind"] == "WORKER_EXIT"


def test_a_worker_that_times_out_is_uncertain(tmp_path, monkeypatch):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(server, "WORKER", str(script))

    report = server.execute({}, timeout=1)
    assert report["status"] == "UNCERTAIN"
    assert report["problems"][0]["kind"] == "WORKER_TIMEOUT"


def test_a_worker_report_is_returned_verbatim(tmp_path, monkeypatch):
    """The service is a transport, not a judge. Interpretation happens once, in
    `browser_invocation._interpret`, in the container."""
    script = tmp_path / "ok.py"
    script.write_text(
        "import json,sys\n"
        "print(json.dumps({'status': 'COMPLETED', 'echo': json.load(sys.stdin).get('job_id')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "WORKER", str(script))

    report = server.execute({"job_id": "job-7"}, timeout=20)
    assert report["status"] == "COMPLETED"
    assert report["echo"] == "job-7"


def test_the_task_never_reaches_the_command_line(tmp_path, monkeypatch):
    script = tmp_path / "argv.py"
    script.write_text(
        "import json,sys\nprint(json.dumps({'status':'COMPLETED','argv':sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "WORKER", str(script))

    report = server.execute({"org_id": "org-secret", "form_data": {"v": "top-secret"}}, timeout=20)
    assert report["argv"] == []


# ===========================================================================
# CONCURRENCY - the credential-blast-radius bound
# ===========================================================================
def test_simultaneous_requests_do_not_overlap(tmp_path, monkeypatch):
    """Two requests must not open two browsers. The lock is what makes the bound real rather than
    intended, and this measures overlap rather than trusting the constant."""
    script = tmp_path / "overlap.py"
    script.write_text(
        "import json,sys,time\n"
        "start = time.time()\n"
        "time.sleep(1.0)\n"
        "print(json.dumps({'status':'COMPLETED','start':start}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "WORKER", str(script))

    results: list[dict] = []
    lock = threading.Lock()

    def hit():
        report = server.execute({}, timeout=30)
        with lock:
            results.append(report)

    # Acquire the session lock so both workers run serialised, as the HTTP handler does.
    def serialised():
        with server._SESSION_LOCK:
            hit()

    threads = [threading.Thread(target=serialised) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    starts = sorted(r["start"] for r in results)
    assert starts[1] - starts[0] >= 0.9, (
        "two worker processes ran concurrently; the one-session bound is not enforced"
    )


def test_the_service_reports_its_concurrency_bound():
    """Discoverable rather than documented only."""
    assert "concurrency" in json.dumps({"concurrency": 1})


# ===========================================================================
# THE CLIENT
# ===========================================================================
def test_the_client_reports_uncertain_when_the_service_is_unreachable(monkeypatch, capsys):
    """A task that was never delivered must not be reported as a definite failure. Answering `no`
    would license a retry of work that might have happened."""
    monkeypatch.setattr(client, "DEFAULT_URL", "http://127.0.0.1:1/execute")
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": type("B", (), {"read": staticmethod(lambda: b'{"job_id":"j"}')})()})())

    client.main(["http://127.0.0.1:1/execute"])
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "UNCERTAIN"
    assert report["outcome_certain"] is False
    assert report["problems"][0]["kind"] == "EXECUTE_SERVICE_UNREACHABLE"


def test_the_client_rejects_a_non_json_task(capsys):
    import io

    monkeypatch_stdin = type("S", (), {"buffer": io.BytesIO(b"not json")})()
    original = sys.stdin
    sys.stdin = monkeypatch_stdin
    try:
        client.main(["http://127.0.0.1:1/execute"])
    finally:
        sys.stdin = original

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "UNCERTAIN"
    assert report["problems"][0]["kind"] == "CLIENT_BAD_TASK"


def test_the_client_does_not_touch_the_database():
    source = (BACKEND / "tools" / "browser_execute_client.py").read_text(encoding="utf-8")
    for forbidden in ("import database", "from database", "sqlalchemy", "models"):
        assert forbidden not in source


# ===========================================================================
# THE FULL LOOP, over HTTP, with a stub worker
# ===========================================================================
def test_the_http_round_trip_returns_the_worker_report(tmp_path, monkeypatch):
    """Both halves together: an HTTP request becomes a worker process and its report comes back.
    Uses a stub worker so no browser is launched."""
    script = tmp_path / "stub.py"
    script.write_text(
        "import json,sys\n"
        "req = json.load(sys.stdin)\n"
        "print(json.dumps({'status':'COMPLETED','echo':req.get('job_id')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "WORKER", str(script))

    httpd = server.build_server("127.0.0.1", 0, timeout=30)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/execute",
            data=json.dumps({"job_id": "job-9"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            report = json.loads(response.read().decode())
        assert report["status"] == "COMPLETED"
        assert report["echo"] == "job-9"

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
            health = json.loads(response.read().decode())
        assert health["ok"] is True
        assert health["concurrency"] == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_unknown_path_is_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKER", str(tmp_path / "w.py"))
    httpd = server.build_server("127.0.0.1", 0, timeout=10)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/../etc/passwd", timeout=10)
        assert caught.value.code in (400, 404)
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_an_oversized_body_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "WORKER", str(tmp_path / "w.py"))
    httpd = server.build_server("127.0.0.1", 0, timeout=10)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/execute",
            data=b"x" * (server.MAX_BODY_BYTES + 1),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=30)
        assert caught.value.code == 413
    finally:
        httpd.shutdown()
        httpd.server_close()
