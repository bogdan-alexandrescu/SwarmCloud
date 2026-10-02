"""The canonical form of a step spec (contract request 34, section 1).

`swarm_common.specsign` is the ONE definition of which fields swarm-api's
signature covers and how they become bytes. It implements RFC 8785 (JCS) with
no dependency, because `swarm_common` declares none. So it is held to:

  * RFC 8785's own vectors: Appendix B's numbers, section 3.2.2's example and
    section 3.2.3's sorting example -- the vector that caught the first draft
    sorting members by their ESCAPED names ("\\r" after "1");
  * the `rfc8785` package, as a test-only oracle, over generated values;
  * a pinned golden vector: one fixed task dict to fixed bytes and a fixed
    SHA-256, so any change to the projection is a visible diff that demands a
    format bump (`SPEC_FORMAT`), never a silent reinterpretation;
  * the three refusals: an integer past 2^53 - 1, a non-finite number and a
    lone surrogate have no canonical form.
"""

from __future__ import annotations

import math
import random
import struct

import pytest

from swarm_common import specsign
from swarm_common.specsign import SpecNotCanonical, canonical_step_spec, jcs, spec_digest

# ---------------------------------------------------------------------------
# RFC 8785, Appendix B: IEEE 754 bit patterns and their serialisations
# ---------------------------------------------------------------------------

APPENDIX_B = [
    ("0000000000000000", "0"),
    ("8000000000000000", "0"),
    ("0000000000000001", "5e-324"),
    ("8000000000000001", "-5e-324"),
    ("7fefffffffffffff", "1.7976931348623157e+308"),
    ("ffefffffffffffff", "-1.7976931348623157e+308"),
    ("4340000000000000", "9007199254740992"),
    ("c340000000000000", "-9007199254740992"),
    ("4430000000000000", "295147905179352830000"),
    ("44b52d02c7e14af5", "9.999999999999997e+22"),
    ("44b52d02c7e14af6", "1e+23"),
    ("44b52d02c7e14af7", "1.0000000000000001e+23"),
    ("444b1ae4d6e2ef4e", "999999999999999700000"),
    ("444b1ae4d6e2ef4f", "999999999999999900000"),
    ("444b1ae4d6e2ef50", "1e+21"),
    ("3eb0c6f7a0b5ed8c", "9.999999999999997e-7"),
    ("3eb0c6f7a0b5ed8d", "0.000001"),
    ("41b3de4355555553", "333333333.3333332"),
    ("41b3de4355555554", "333333333.33333325"),
    ("41b3de4355555555", "333333333.3333333"),
    ("41b3de4355555556", "333333333.3333334"),
    ("41b3de4355555557", "333333333.33333343"),
    ("becbf647612f3696", "-0.0000033333333333333333"),
    ("43143ff3c1cb0959", "1424953923781206.2"),
]


def _double(bits: str) -> float:
    return struct.unpack(">d", bytes.fromhex(bits))[0]


@pytest.mark.parametrize("bits,expected", APPENDIX_B, ids=[b for b, _ in APPENDIX_B])
def test_appendix_b_numbers(bits, expected):
    assert jcs(_double(bits)) == expected.encode()


@pytest.mark.parametrize("bits", ["7fffffffffffffff", "7ff0000000000000", "fff0000000000000"])
def test_appendix_b_non_finite_numbers_are_refused(bits):
    with pytest.raises(SpecNotCanonical):
        jcs(_double(bits))


def test_section_3_2_2_example():
    value = {
        "numbers": [333333333.33333329, 1e30, 4.50, 2e-3, 0.000000000000000000000000001],
        "string": "\u20ac$\u000f\u000aA'\u0042\u0022\u005c\\\"/",
        "literals": [None, True, False],
    }
    expected = (
        '{"literals":[null,true,false],'
        '"numbers":[333333333.3333333,1e+30,4.5,0.002,1e-27],'
        '"string":"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\\"/"}'
    )
    assert jcs(value) == expected.encode("utf-8")


def test_section_3_2_3_sorting_is_by_utf16_code_units_of_the_raw_names():
    value = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "דּ": "Hebrew Letter Dalet With Dagesh",
        "1": "One",
        "\U0001f600": "Emoji: Grinning Face",
        "\u0080": "Control",
        "ö": "Latin Small Letter O With Diaeresis",
    }
    out = jcs(value).decode("utf-8")
    names = ["\\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    positions = [out.index(f'"{name}":') for name in names]
    assert positions == sorted(positions), out


# ---------------------------------------------------------------------------
# The rfc8785 package as an oracle
# ---------------------------------------------------------------------------


def _random_string(rng: random.Random) -> str:
    pools = [
        (0x20, 0x7E), (0x00, 0x1F), (0x80, 0x7FF), (0x800, 0xD7FF),
        (0xE000, 0xFFFF), (0x10000, 0x10FFFF),
    ]
    chars = []
    for _ in range(rng.randint(0, 8)):
        lo, hi = rng.choice(pools)
        chars.append(chr(rng.randint(lo, hi)))
    return "".join(chars)


def _random_number(rng: random.Random) -> float | int:
    kind = rng.randint(0, 3)
    if kind == 0:
        return rng.randint(-specsign.MAX_SAFE_INTEGER, specsign.MAX_SAFE_INTEGER)
    if kind == 1:
        return rng.randint(-1000, 1000)
    while True:
        x = struct.unpack(">d", rng.getrandbits(64).to_bytes(8, "big"))[0]
        if math.isfinite(x):
            return x if kind == 2 else rng.uniform(-1e6, 1e6)


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.randint(0, 6 if depth < 3 else 3)
    if kind == 0:
        return rng.choice([None, True, False])
    if kind in (1, 2):
        return _random_number(rng)
    if kind == 3:
        return _random_string(rng)
    if kind == 4:
        return [_random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {_random_string(rng): _random_value(rng, depth + 1) for _ in range(rng.randint(0, 4))}


def test_agrees_with_the_rfc8785_package_on_generated_values():
    rfc8785 = pytest.importorskip("rfc8785")
    rng = random.Random(342)
    for i in range(20_000):
        value = _random_value(rng)
        assert jcs(value) == rfc8785.dumps(value), (i, value)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        2**53,
        -(2**53),
        2**63 - 1,
        float("nan"),
        float("inf"),
        float("-inf"),
        "\ud800",
        "a\udfffb",
        {"\ud800": 1},
        [1, {"x": [float("nan")]}],
    ],
    ids=["2^53", "-2^53", "2^63-1", "nan", "inf", "-inf", "lone-high", "lone-low",
         "surrogate-key", "nested-nan"],
)
def test_values_with_no_canonical_form_are_refused(value):
    with pytest.raises(SpecNotCanonical):
        jcs(value)


def test_the_largest_safe_integer_is_accepted():
    assert jcs(2**53 - 1) == b"9007199254740991"
    assert jcs(-(2**53 - 1)) == b"-9007199254740991"


@pytest.mark.parametrize("value", [b"bytes", {1: "int key"}, object(), {1.5}])
def test_a_value_json_has_no_form_for_is_refused(value):
    with pytest.raises(SpecNotCanonical):
        jcs(value)


def test_a_metadata_that_is_not_a_map_is_refused():
    with pytest.raises(SpecNotCanonical):
        canonical_step_spec({"metadata": ["x"]}, task_id="task_1")


# ---------------------------------------------------------------------------
# The projection, pinned
# ---------------------------------------------------------------------------

GOLDEN_DOC = {
    "id": "task_ignored",
    "tenant_id": "eng",
    "workflow_id": "wf_1",
    "step_id": "review",
    "submitted_by": "alice@saga.xyz",
    "runner_profile": "claude-code",
    "resource_class": "standard",
    "timeout_seconds": 3600,
    "max_attempts": 3,
    "provider": "anthropic",
    "model": None,
    "input": {"prompt": "review the change", "issue": 265},
    "depends_on": ["task_impl"],
    "repository_url": "https://github.com/acme/widgets.git",
    "repository_ref": "main",
    "metadata": {
        "dispatch": {"strategy": "direct-pr", "carrier": "checkpoints", "role": "reader"},
        "input_from": {"task_impl": "summary.md"},
        "workflow_step": "review",
        "startup_refunds": 2,
    },
    "state": "PARKED",
    "priority": 5,
    "attempt_count": 0,
}

GOLDEN_BYTES = (
    b'{"depends_on":["task_impl"],"format":1,'
    b'"input":{"issue":265,"prompt":"review the change"},"max_attempts":3,'
    b'"metadata":{"dispatch":{"carrier":"checkpoints","role":"reader","strategy":"direct-pr"},'
    b'"expected_outputs":null,"input_from":{"task_impl":"summary.md"}},'
    b'"model":null,"provider":"anthropic","purpose":"swarm.step-spec",'
    b'"repository_ref":"main","repository_url":"https://github.com/acme/widgets.git",'
    b'"resource_class":"standard","runner_profile":"claude-code","step_id":"review",'
    b'"submitted_by":"alice@saga.xyz","task_id":"task_0123456789abcdef","tenant_id":"eng",'
    b'"timeout_seconds":3600,"workflow_id":"wf_1"}'
)
GOLDEN_SHA256 = "86d2a8f146e6f700c19d6ed76ca53fd0e861c43b2793eb76f5301d688f785f4e"


def test_the_golden_vector():
    """A change here is a new FORMAT, not an edit: bump SPEC_FORMAT with it.

    Format 1's vector is unchanged by contract request 42: a document signed
    at format 1 keeps verifying under format 1's projection.
    """
    canonical = canonical_step_spec(GOLDEN_DOC, task_id="task_0123456789abcdef", spec_format=1)
    assert canonical == GOLDEN_BYTES
    assert spec_digest(canonical).hex() == GOLDEN_SHA256
    assert specsign.SPEC_FORMAT == 2
    assert specsign.SPEC_FORMATS == (1, 2)
    assert specsign.SPEC_PURPOSE == "swarm.step-spec"


GOLDEN_BYTES_V2 = (
    GOLDEN_BYTES.replace(b'"format":1,', b'"format":2,')
    .replace(
        b'"model":null,',
        b'"model":null,"parent_attempt_id":"att_parent","parent_task_id":"task_parent",',
    )
)


def test_format_2_covers_the_parent_fields():
    """Contract request 42: a child's parent is inside the signed bytes."""
    doc = {**GOLDEN_DOC, "parent_task_id": "task_parent", "parent_attempt_id": "att_parent"}
    canonical = canonical_step_spec(doc, task_id="task_0123456789abcdef")
    assert canonical == GOLDEN_BYTES_V2
    rewritten = canonical_step_spec(
        {**doc, "parent_task_id": "task_other"}, task_id="task_0123456789abcdef"
    )
    assert spec_digest(rewritten) != spec_digest(canonical)
    # Format 1 does not see them, so a format-1 document's digest is unchanged.
    assert canonical_step_spec(doc, task_id="task_0123456789abcdef", spec_format=1) == GOLDEN_BYTES


def test_a_task_that_names_no_parent_is_signed_at_format_1():
    """Contract request 42's rollout: a worker built before format 2 knows only
    format 1, so every non-child task is signed at it; only a child is format 2."""
    assert specsign.signing_format(GOLDEN_DOC) == 1
    assert specsign.signing_format({**GOLDEN_DOC, "parent_task_id": None}) == 1
    assert specsign.signing_format({**GOLDEN_DOC, "parent_task_id": "task_p"}) == 2
    assert specsign.signing_format({**GOLDEN_DOC, "parent_attempt_id": "att_p"}) == 2


def test_an_unknown_format_has_no_projection():
    with pytest.raises(specsign.SpecNotCanonical):
        canonical_step_spec(GOLDEN_DOC, task_id="task_x", spec_format=3)


def test_the_task_id_is_the_one_read_by_never_the_documents_own():
    a = canonical_step_spec(GOLDEN_DOC, task_id="task_a")
    b = canonical_step_spec({**GOLDEN_DOC, "id": "task_a"}, task_id="task_b")
    assert a != b
    assert b"task_ignored" not in a


def test_uncovered_fields_do_not_reach_the_bytes():
    base = canonical_step_spec(GOLDEN_DOC, task_id="t")
    for key, value in {
        "state": "RUNNING", "priority": 1, "attempt_count": 3, "current_generation": 9,
        "result_summary": {"x": 1}, "latest_checkpoint": "gs://x", "created_at": "now",
        "end_cause": "timeout", "cancel_requested": True,
    }.items():
        assert canonical_step_spec({**GOLDEN_DOC, key: value}, task_id="t") == base, key
    for key in ("workflow_step", "startup_refunds", "label"):
        metadata = {**GOLDEN_DOC["metadata"], key: "rewritten"}
        assert canonical_step_spec({**GOLDEN_DOC, "metadata": metadata}, task_id="t") == base


def test_a_missing_key_reads_as_null_and_a_missing_metadata_as_empty():
    got = canonical_step_spec({}, task_id="t").decode()
    assert '"metadata":{"dispatch":null,"expected_outputs":null,"input_from":null}' in got
    assert '"input":null' in got and '"depends_on":null' in got
