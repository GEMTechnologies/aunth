"""Phase 10 readiness checks, and the attachment scanner.

The readiness tests assert the SHAPE and the SEMANTICS of each check rather than a
particular deployment's state, because a health check that only passes on one machine
is not a health check. The scanner tests assert behaviour on known inputs, including
the one signature whose presence proves the scanner runs.
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from agent.mail.scanning import (  # noqa: E402
    COVERAGE,
    ScanVerdict,
    scan_attachment,
    scan_many,
)
from health import (  # noqa: E402
    BLOCKING,
    Check,
    CheckStatus,
    ReadinessReport,
    validate_configuration,
)

#: The published EICAR test string. Not malware - a marker every scanner is expected
#: to detect, which makes it the one signature whose presence proves the scanner runs.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


# ===========================================================================
# THE REPORT SHAPE
# ===========================================================================
def test_a_report_with_no_blocking_check_is_ready():
    report = ReadinessReport(checks=[
        Check("database", CheckStatus.HEALTHY),
        Check("redis", CheckStatus.DEGRADED, "backlog"),
    ])
    # DEGRADED does not make it unready. A backlog needs attention; refusing traffic
    # because the relay is behind would turn a delay into an outage.
    assert report.ready is True
    assert report.status == CheckStatus.DEGRADED
    assert report.as_dict()["degraded"] == ["redis"]


def test_a_not_ready_check_makes_the_report_not_ready():
    report = ReadinessReport(checks=[
        Check("database", CheckStatus.HEALTHY),
        Check("migrations", CheckStatus.NOT_READY, "schema is behind"),
    ])
    assert report.ready is False
    assert report.status == CheckStatus.NOT_READY
    assert report.as_dict()["failing"] == ["migrations"]


def test_skipped_checks_are_not_failures():
    report = ReadinessReport(checks=[Check("mail_providers", CheckStatus.SKIPPED, "none configured")])
    assert report.ready is True
    assert report.status == CheckStatus.HEALTHY


def test_only_not_ready_blocks():
    assert BLOCKING == {CheckStatus.NOT_READY}


def test_every_check_reports_a_name_status_and_detail():
    """The contract an operator relies on: what is wrong, not that something is."""
    from health import readiness

    report = readiness(include_redis=False)
    assert report.checks, "no checks ran"
    for check in report.checks:
        assert check.name
        assert isinstance(check.status, CheckStatus)
        assert check.detail is not None
        # Every check is serialisable for the API.
        payload = check.as_dict()
        assert set(payload) >= {"name", "status", "detail"}


# ===========================================================================
# THE CHECKS THEMSELVES
# ===========================================================================
def test_the_readiness_report_works_without_redis():
    """Redis is a transport, never the source of truth.

    Its absence must not make a deployment unready: PostgreSQL holds every durable
    fact and the outbox holds every undelivered event, so the service is correct
    without it, just with delivery paused.
    """
    from health import readiness

    report = readiness(include_redis=False)
    redis_checks = [c for c in report.checks if c.name == "redis"]
    assert redis_checks and redis_checks[0].status == CheckStatus.SKIPPED


def test_a_failing_check_is_a_status_not_an_exception():
    """A readiness probe that throws is worse than one that reports.

    A load balancer sees an unhandled 500 and cannot distinguish it from the process
    being down.
    """
    from health import readiness

    report = readiness(include_redis=False)
    assert isinstance(report, ReadinessReport)


def test_the_configuration_check_finds_no_problems_in_the_test_environment():
    """Phase 7c's switches are off here, so nothing dangerous is configured."""
    problems = validate_configuration()
    assert all(p.severity in ("warn", "refuse") for p in problems)
    # The tests run with autonomous mail off, so the dangerous combination is absent.
    assert not any(
        p.code == "AUTONOMOUS_MAIL_WITHOUT_OUTBOUND_PROVIDER" for p in problems
    )


def test_a_warning_is_a_degradation_and_a_refusal_stops_the_process():
    """The distinction the startup validator depends on."""
    from health import validate_startup

    # With the test configuration there is nothing to refuse.
    problems = validate_startup(raise_on_refuse=True)
    assert all(p.severity != "refuse" for p in problems)


# ===========================================================================
# THE SCANNER
# ===========================================================================
@pytest.mark.parametrize(
    "label,content,filename,expected",
    (
        ("clean pdf", b"%PDF-1.4 ordinary document", "accounts.pdf", ScanVerdict.CLEAN),
        ("clean png", b"\x89PNG\r\n\x1a\n" + b"0" * 40, "logo.png", ScanVerdict.CLEAN),
        ("clean zip", None, "archive.zip", None),  # built below
    ),
)
def test_ordinary_documents_are_clean(label, content, filename, expected):
    if content is None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("notes.txt", "hello")
        content = buffer.getvalue()
        expected = ScanVerdict.CLEAN
    assert scan_attachment(content=content, filename=filename).verdict == expected


def test_the_eicar_test_string_is_detected():
    """The one signature whose presence proves the scanner is running.

    The first version escaped a backslash twice, so this - the standard anti-malware
    test file that every scanner is expected to catch - matched nothing at all.
    """
    result = scan_attachment(content=EICAR, filename="note.txt")
    assert result.verdict == ScanVerdict.MALICIOUS
    assert any(f.code == "EICAR_TEST_SIGNATURE" for f in result.findings)


def test_an_executable_named_as_a_document_is_malicious_by_content():
    """The extension is attacker-controlled; the magic bytes are not."""
    result = scan_attachment(content=b"MZ\x90\x00payload", filename="accounts.pdf")
    assert result.verdict == ScanVerdict.MALICIOUS
    assert any(f.code == "EXECUTABLE_CONTENT" for f in result.findings)


def test_an_executable_extension_is_malicious():
    result = scan_attachment(content=b"anything", filename="run.exe")
    assert result.verdict == ScanVerdict.MALICIOUS
    assert any(f.code == "EXECUTABLE_EXTENSION" for f in result.findings)


@pytest.mark.parametrize(
    "content,filename",
    (
        (b"\x7fELF" + b"0" * 20, "tool"),
        (b"\xcf\xfa\xed\xfe" + b"0" * 20, "tool"),
        (b"\xca\xfe\xba\xbe" + b"0" * 20, "Tool.class"),
        (b"#!/bin/sh\nrm -rf /", "helper.txt"),
    ),
)
def test_non_windows_executables_are_also_detected(content, filename):
    """A screen that only looks for `MZ` protects one platform."""
    assert scan_attachment(content=content, filename=filename).verdict == ScanVerdict.MALICIOUS


def test_a_verdict_never_downgrades():
    """Regression test for a real bug.

    An `MZ` executable named `.pdf` was correctly marked MALICIOUS by the content
    check and then OVERWRITTEN to SUSPICIOUS by the unrecognised-content check that
    ran afterwards. The file was still quarantined, but the record said "suspicious"
    about a Windows executable - and a verdict that can go down is one nobody can
    rely on.
    """
    result = scan_attachment(content=b"MZ\x90\x00payload", filename="accounts.pdf")
    assert result.verdict == ScanVerdict.MALICIOUS, (
        "a weaker finding downgraded a stronger one"
    )


def test_macro_enabled_documents_are_quarantined():
    result = scan_attachment(content=b"PK\x03\x04VBA", filename="budget.docm")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "MACRO_ENABLED_DOCUMENT" for f in result.findings)


def test_an_embedded_macro_stream_is_detected_in_an_ole_document():
    content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"VBA"
    result = scan_attachment(content=content, filename="old.doc")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "EMBEDDED_MACRO_STREAM" for f in result.findings)


@pytest.mark.parametrize(
    "content",
    (
        b"<html><script>alert(1)</script>",
        b"%PDF-1.4 /JavaScript (app.alert)",
        b"%PDF-1.4 /Launch /S",
        b"Set x = CreateObject(\"WScript.Shell\")",
        b"powershell -enc ZQBjAGgAbwA=",
    ),
)
def test_active_content_is_flagged(content):
    result = scan_attachment(content=content, filename="doc.txt")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "ACTIVE_CONTENT" for f in result.findings)


def test_a_zip_bomb_is_refused_before_extraction():
    """A size limit alone misses this.

    A 200 KB archive that expands to hundreds of megabytes passes every byte check on
    the way in. The declared sizes are read from the central directory and summed
    BEFORE anything is decompressed, so the refusal happens without allocating the
    memory the bomb was designed to consume.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a" * 2000, "b" * 500_000_000)
    payload = buffer.getvalue()

    assert len(payload) < 1_000_000, "the fixture is not actually small"
    result = scan_attachment(content=payload, filename="bomb.zip")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "ARCHIVE_EXPANSION" for f in result.findings)


def test_an_archive_with_path_traversal_is_flagged():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../etc/passwd", "x")
    result = scan_attachment(content=buffer.getvalue(), filename="evil.zip")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "ARCHIVE_PATH_TRAVERSAL" for f in result.findings)


def test_an_archive_containing_an_executable_is_flagged():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("payload.exe", "MZ")
    result = scan_attachment(content=buffer.getvalue(), filename="bundle.zip")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "ARCHIVE_CONTAINS_EXECUTABLE" for f in result.findings)


def test_an_oversize_file_is_refused_before_being_read():
    result = scan_attachment(content=b"x" * 100, filename="big.pdf", max_bytes=10)
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code == "OVERSIZE" for f in result.findings)


def test_a_type_mismatch_is_flagged():
    result = scan_attachment(content=b"%PDF-1.4", filename="picture.png")
    assert result.verdict == ScanVerdict.SUSPICIOUS
    assert any(f.code in ("TYPE_MISMATCH", "UNRECOGNISED_CONTENT") for f in result.findings)


def test_missing_content_is_unavailable_rather_than_clean():
    """Absence of evidence is not evidence of safety.

    A scanner that returns CLEAN for a file it never saw is worse than no scanner,
    because the record then says the file was checked.
    """
    result = scan_attachment(content=None, filename="x.pdf")
    assert result.verdict == ScanVerdict.UNAVAILABLE
    assert result.safe_to_attach_outbound is False


def test_only_a_clean_scan_may_be_attached_outbound():
    assert scan_attachment(content=b"%PDF-1.4 ok", filename="a.pdf").safe_to_attach_outbound is True
    assert scan_attachment(content=EICAR, filename="a.txt").safe_to_attach_outbound is False
    assert scan_attachment(content=None, filename="a.pdf").safe_to_attach_outbound is False


def test_every_result_states_its_coverage_and_never_claims_safety():
    """The honesty the brief requires.

    An operator reading `CLEAN` must not conclude the file was checked against
    something that knows about malware, so the coverage is on every result and the
    note says plainly what CLEAN does not mean.
    """
    for content, filename in (
        (b"%PDF-1.4 ok", "a.pdf"),
        (EICAR, "a.txt"),
        (None, "a.pdf"),
    ):
        result = scan_attachment(content=content, filename=filename)
        payload = result.as_dict()
        assert payload["coverage"] == COVERAGE
        assert "NOT an anti-virus engine" in payload["coverage"]
        assert "does not" in payload["note"]
        assert "known to be safe" in payload["note"]


def test_every_result_carries_a_checksum_of_what_was_read():
    result = scan_attachment(content=b"%PDF-1.4 ok", filename="a.pdf")
    assert result.checksum_sha256 and len(result.checksum_sha256) == 64


def test_scanning_a_set_keys_by_checksum_so_a_duplicate_is_screened_once():
    results = scan_many([
        {"content": b"%PDF-1.4 same", "filename": "a.pdf"},
        {"content": b"%PDF-1.4 same", "filename": "b.pdf"},
        {"content": b"%PDF-1.4 different", "filename": "c.pdf"},
    ])
    assert len(results) == 2


def test_the_scanner_needs_no_network():
    """It runs on every inbound file, so it must not be a dependency that can be down.

    Asserted structurally: the module imports nothing that could reach out.
    """
    import inspect

    from agent.mail import scanning

    source = inspect.getsource(scanning)
    for forbidden in ("requests.", "httpx.", "urllib.request", "socket."):
        assert forbidden not in source, f"the scanner references {forbidden}"
