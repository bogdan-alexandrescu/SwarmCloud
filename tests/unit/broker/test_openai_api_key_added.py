"""Who added Voice it's OpenAI API key and when (docs/design/voice-it-own-speech.md §10.3).

The broker's key account carries the person the `voice-api-key` link was
bound to and the time it stored the key, so Voice it › Settings can say "set
by <person>, <date>" without the key. No cloud credential; nothing here
holds a key.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quota_broker import openai_api_key as oak
from quota_broker.oauth import CredentialError

AT = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)


def test_the_adder_and_the_time_are_two_fields_on_the_account():
    fields = oak.added_fields("  Root@Example.com ", AT)

    assert fields == {oak.ADDED_BY: "root@example.com", oak.ADDED_AT: AT}
    assert oak.added_from(fields) == {"by": "root@example.com", "at": AT}


def test_a_replacement_overwrites_both_fields():
    doc = {"registered_at": AT - timedelta(days=9), **oak.added_fields("dana@example.com", AT)}
    later = AT + timedelta(days=2)

    doc.update(oak.added_fields("root@example.com", later))

    assert oak.added_from(doc) == {"by": "root@example.com", "at": later}


@pytest.mark.parametrize("person", ["", "root", "root@", "@example.com", "a b@example.com",
                                    "a@b@example.com", "root@localhost", "x" * 250 + "@ex.com"])
def test_an_adder_that_is_not_one_email_address_is_refused(person):
    with pytest.raises(CredentialError):
        oak.added_fields(person, AT)


def test_a_time_without_its_zone_is_refused():
    with pytest.raises(CredentialError):
        oak.added_fields("root@example.com", AT.replace(tzinfo=None))


def test_a_document_written_before_the_fields_reads_as_unknown():
    assert oak.added_from({"registered_at": AT}) == {"by": None, "at": None}
    assert oak.added_from({oak.ADDED_BY: "", oak.ADDED_AT: "2026-11-05"}) == {"by": None,
                                                                            "at": None}
