"""Two vocabularies bridged by code, and the drift a bridge can hide.

A phase-14 scan for raw string literals that duplicate a defined vocabulary produced
**mostly homonyms** - `INFO` is a log level and not `Severity.INFO`, `PERMANENT_REJECTION` in
the events layer is a dead-letter category and not `SubmissionFailure.PERMANENT_REJECTION`,
`SUSPICIOUS` in `risk.py` is a `MailAttachment.scan_status` and not a `ScanVerdict`. A guard
that flagged those would be switched off within a week, which is why this file checks the
bridges explicitly instead of scanning for literals.

It found two bridges worth pinning:

1. **`ScanVerdict` -> `MailAttachment.scan_status`** was an if/elif chain inside a 200-line
   method. The chain had an `else` meaning UNAVAILABLE, so a new verdict would have fallen
   into it silently - and it collapsed `MALICIOUS` onto `SUSPICIOUS`, so a file that matched a
   malware signature was recorded identically to one that merely looked unusual.
2. **`EMAIL_INTENTS` <-> `MailClassification`** are two closed sets that overlap by design.
   Nothing checked the overlap, so renaming a classification would leave the decision gateway
   answering with an intent the classifier never produces.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import models  # noqa: E402
from agent.decision.models import EMAIL_INTENTS  # noqa: E402
from agent.mail.scanning import (  # noqa: E402
    SCAN_STATUS_BY_VERDICT,
    ScanVerdict,
    scan_status_for,
)


# ===========================================================================
# BRIDGE 1: the scan verdict
# ===========================================================================
def test_the_bridge_is_TOTAL_over_the_verdict_enum():
    """Every verdict has a status.

    This is the property the if/elif chain could not have: a chain has an `else`, so a new
    verdict silently became UNAVAILABLE. A dict has no fallback, and `scan_status_for` raises
    `KeyError` naming the verdict it does not know.
    """
    for verdict in ScanVerdict:
        assert verdict in SCAN_STATUS_BY_VERDICT, (
            f"{verdict} has no scan_status. An unknown verdict must raise rather than "
            "silently become UNAVAILABLE, because UNAVAILABLE reads as 'we did not look' and "
            "that would be a lie about a file we did look at."
        )


def test_the_bridge_has_no_verdict_the_enum_does_not_define():
    """A stale entry is a mapping nobody can reach, and it hides a removal."""
    for verdict in SCAN_STATUS_BY_VERDICT:
        assert isinstance(verdict, ScanVerdict)


def test_the_bridge_produces_only_values_the_model_defines():
    """THE cross-module assertion.

    `scanning.py` returns plain strings because it must stay free of database and network
    dependencies - it runs on every inbound file. The cost of that choice is that nothing
    links its strings to the model's constants, so this test is the link.
    """
    defined = {
        models.MailAttachment.SCAN_PENDING,
        models.MailAttachment.SCAN_CLEAN,
        models.MailAttachment.SCAN_SUSPICIOUS,
        models.MailAttachment.SCAN_MALICIOUS,
        models.MailAttachment.SCAN_FAILED,
        models.MailAttachment.SCAN_UNAVAILABLE,
    }
    for verdict, status in SCAN_STATUS_BY_VERDICT.items():
        assert status in defined, (
            f"{verdict} maps to {status!r}, which is not a scan_status the model defines. "
            "The column would accept it - it is an unconstrained String - so nothing else "
            "would catch this."
        )


def test_a_MATCH_is_distinguishable_from_a_SUSPICION():
    """THE defect this bridge replaced.

    `MALICIOUS` was mapped onto `SUSPICIOUS`, so a file that matched a malware signature and
    one that merely looked unusual wrote the same column value. The finding survived only
    inside `scan_detail` as free text, which made "how many malicious attachments have we
    seen" unanswerable from the database.
    """
    assert scan_status_for(ScanVerdict.MALICIOUS, has_content=True) == "MALICIOUS"
    assert scan_status_for(ScanVerdict.SUSPICIOUS, has_content=True) == "SUSPICIOUS"
    assert (
        scan_status_for(ScanVerdict.MALICIOUS, has_content=True)
        != scan_status_for(ScanVerdict.SUSPICIOUS, has_content=True)
    )
    assert models.MailAttachment.SCAN_MALICIOUS != models.MailAttachment.SCAN_SUSPICIOUS


def test_a_clean_verdict_without_content_is_UNAVAILABLE_not_CLEAN():
    """A CLEAN verdict on a file nobody supplied is not a clean file.

    Recording it as CLEAN would be the exact misreading `CLEAN` must never invite, and this
    branch existed in the original chain - it is preserved deliberately rather than
    discovered to be missing later.
    """
    assert scan_status_for(ScanVerdict.CLEAN, has_content=True) == "CLEAN"
    assert scan_status_for(ScanVerdict.CLEAN, has_content=False) == "UNAVAILABLE"


def test_an_unknown_verdict_RAISES():
    """The reason for a dict over a chain, asserted rather than asserted-about.

    A chain answers its `else`; this raises. For a security verdict, silently degrading to
    "we did not look" is worse than an exception, because nothing surfaces it.
    """

    class Invented(str):
        pass

    with pytest.raises(KeyError):
        scan_status_for("A_VERDICT_THIS_BRIDGE_DOES_NOT_KNOW", has_content=True)


# ===========================================================================
# BRIDGE 2: the email intents
# ===========================================================================
#: The `EMAIL_INTENTS` that correspond 1:1 to a `MailClassification`.
#:
#: Declared here because the relationship is real and was previously implicit. The other
#: intents - CONTRACT, PAYMENT_OR_BANK_REQUEST, GENERAL_QUESTION, BOUNCE - are gateway
#: vocabulary with no classifier counterpart, which is why the two sets are not identical and
#: must not be forced to be.
SHARED_INTENTS = (
    "ACKNOWLEDGEMENT",
    "CLARIFICATION_REQUEST",
    "DOCUMENT_REQUEST",
    "DEADLINE_CHANGE",
    "INTERVIEW_INVITATION",
    "AWARD_NOTICE",
    "REJECTION_NOTICE",
    "UNKNOWN",
)


def test_the_intents_shared_with_the_classifier_still_exist_in_both():
    """A rename on either side would desynchronise them silently.

    The decision gateway would keep answering with an intent the mail classifier never
    produces, and nothing would raise: both are plain string sets, and a string that matches
    nothing is not an error anywhere.
    """
    from agent.mail.vocabulary import MailClassification

    classifier_values = {member.value for member in MailClassification}

    for intent in SHARED_INTENTS:
        assert intent in EMAIL_INTENTS, (
            f"{intent!r} is declared as shared with the classifier but is no longer an "
            "email intent"
        )
        assert intent in classifier_values, (
            f"{intent!r} is declared as shared with the classifier but MailClassification no "
            "longer defines it. Either the classification was renamed - in which case the "
            "intent must follow, or the gateway will answer with an intent nothing produces - "
            "or it was deliberately removed, in which case remove it from SHARED_INTENTS."
        )


def test_the_intents_that_are_gateway_only_are_declared_as_such():
    """So the two sets are not expected to be identical, and the difference is deliberate.

    Forcing equality would either invent classifications for gateway vocabulary or drop
    intents the gateway legitimately needs. What must be true is that every intent is either
    shared, or explicitly gateway-only.
    """
    gateway_only = set(EMAIL_INTENTS) - set(SHARED_INTENTS)
    assert gateway_only == {
        "CONTRACT",
        "PAYMENT_OR_BANK_REQUEST",
        "GENERAL_QUESTION",
        "BOUNCE",
    }, (
        f"the gateway-only intents changed: {sorted(gateway_only)}. If one of these now has a "
        "classifier counterpart, move it into SHARED_INTENTS so the link is checked."
    )


def test_the_intent_set_is_closed_and_free_of_duplicates():
    """It is a model-output allowlist: a duplicate or an empty value weakens it."""
    assert len(EMAIL_INTENTS) == len(set(EMAIL_INTENTS)), "EMAIL_INTENTS has duplicates"
    for intent in EMAIL_INTENTS:
        assert intent and intent.strip() == intent and intent.upper() == intent
