"""Every stated count of the deployer's grantable roles matches the list (#68).

WHY THIS EXISTS. The owner removes the deployer's unconditioned
`projectIamAdmin` by hand, following the comments in
terraform/bootstrap/terraform.tfvars, docs/ci.md and the refusal-probe
runbook. #150 took `swarmSecretLister` off
`local.deployer_grantable_project_roles` (15 roles became 14), and the prose
those steps send the owner to went on saying "fifteen" in nine places -- a
runbook that disagrees with the code is the one followed at 3am.

So this reads the list's real length out of
terraform/bootstrap/deployer_conditions.tf -- the literal predefined roles
plus the keys of the custom_role_ids loop, parsed, never typed in here -- and
holds every count of it written in words or digits next to "grantable",
"roles terraform/infra grants" or "chunk" to that length, and every stated
number of chunks to ceil(length / 10) (hasOnly() refuses a list over 10, #275).

A clause that is explicitly history -- "15 before #150", "Sixteen until
2026-09-25", "measured 2026-09-24 ... 15" -- may keep its old number: it names
a date or an issue next to before/until/measured/was. A clause that bounds a
number rather than stating it ("at most 10 roles", "grows past 200 roles") is
not a count of this list.
"""

from __future__ import annotations

import bisect
import math
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CONDITIONS = REPO / "terraform" / "bootstrap" / "deployer_conditions.tf"

CHECKED = [
    REPO / "terraform" / "bootstrap" / "terraform.tfvars",
    CONDITIONS,
    REPO / "scripts" / "iam-refusal-probe.sh",
    REPO / "docs" / "runbooks" / "iam-refusal-probe.md",
    REPO / "docs" / "ci.md",
    REPO / "tests" / "terraform" / "deployer_iam.tftest.hcl",
]

CHUNK_SIZE = 10  # chunklist(local.deployer_grantable_project_roles, 10)

WORDS = {
    w: i
    for i, w in enumerate(
        "zero one two three four five six seven eight nine ten eleven twelve "
        "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
    )
}
NUM = r"(?<![#\w./-])(\d+|" + "|".join(WORDS) + r")(?![\w/-])"

# A sentence is about the list only if it says so.
TOPIC = re.compile(
    r"grantable|roles terraform[/ ]infra grants|chunk|any of the " + NUM, re.I
)

# Counts of the list itself.
ROLE_COUNT = [
    re.compile(NUM + r"\s+(?:(?:project|grantable|predefined)\s+)?roles\b", re.I),
    re.compile(NUM + r"\s+grantable\b", re.I),
    re.compile(r"\bthe\s+" + NUM + r"\s+deployer_grantable_project_roles\b", re.I),
    re.compile(r"\bany of the\s+" + NUM + r"(?=\s*[,.;()]|\s*$)", re.I),
]

# Counts of the chunks the list splits into.
CHUNK_COUNT = [
    re.compile(r"\bchunk\s+\d+\s+of\s+" + NUM, re.I),
    re.compile(NUM + r"\s+for\s+\S+\s+roles\b", re.I),
    re.compile(r"\binto\s+" + NUM + r"\s+(?:chunked\s+)?bindings\b", re.I),
    re.compile(r"\bthe\s+" + NUM + r"\s+chunk\s+(?:titles|bindings)\b", re.I),
    re.compile(NUM + r"\s+chunks\b", re.I),
    re.compile(r"\bper chunk is\s+" + NUM, re.I),
    re.compile(NUM + r"\s+to add\b", re.I),
]

HISTORY_WORD = re.compile(r"\b(?:before|until|measured|was|were)\b", re.I)
HISTORY_REF = re.compile(r"#\d+|\b\d{4}-\d{2}-\d{2}\b")
BOUND = re.compile(r"\b(?:at most|past|more than|up to|over|exceeds?|beyond)\b", re.I)

SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z*`(\[0-9])")
CLAUSE_SPLIT = re.compile(r"[,;:]|--|—")


def grantable_roles() -> list[str]:
    """local.deployer_grantable_project_roles, as deployer_conditions.tf builds it."""
    text = CONDITIONS.read_text()
    m = re.search(
        r"^\s*deployer_grantable_project_roles\s*=\s*concat\((.*?)^\s*\)\s*$",
        text,
        re.M | re.S,
    )
    assert m, "deployer_grantable_project_roles = concat(...) not found"
    body = "\n".join(
        line for line in m.group(1).splitlines() if not line.lstrip().startswith("#")
    )
    literal = re.match(r"\s*\[(.*?)\]", body, re.S)
    assert literal, "the list of literal roles is not concat()'s first argument"
    roles = re.findall(r'"(roles/[^"]+)"', literal.group(1))
    loop = re.search(
        r"for\s+key\s+in\s+\[(.*?)\]\s*:\s*module\.custom_role_ids\.names\[key\]",
        body,
        re.S,
    )
    assert loop, "the custom_role_ids loop is not concat()'s second argument"
    keys = re.findall(r'"([a-z_]+)"', loop.group(1))
    return roles + [f"custom_role_ids.{k}" for k in keys]


def prose_blocks(path: Path) -> list[tuple[str, list[int], int]]:
    """(joined text, line-start offsets, first line number) per paragraph.

    Code files contribute their comments only; Markdown, every paragraph.
    """
    blocks: list[tuple[str, list[int], int]] = []
    markdown = path.suffix == ".md"
    text, starts, first = "", [], 0

    def flush() -> None:
        nonlocal text, starts, first
        if text.strip():
            blocks.append((text, starts, first))
        text, starts, first = "", [], 0

    for n, raw in enumerate(path.read_text().splitlines(), 1):
        stripped = raw.strip()
        if markdown:
            line = stripped
        elif stripped.startswith("#") and not stripped.startswith("#!"):
            line = stripped.lstrip("#").strip()
        else:
            flush()
            continue
        if not line:
            flush()
            continue
        if not text:
            first = n
        starts.append(len(text))
        text += line + " "
    flush()
    return blocks


def value(token: str) -> int:
    return int(token) if token.isdigit() else WORDS[token.lower()]


def stale(expected_roles: int, expected_chunks: int) -> list[str]:
    found: list[str] = []
    for path in CHECKED:
        rel = path.relative_to(REPO)
        for text, starts, first in prose_blocks(path):
            pos = 0
            for sentence in SENTENCE_END.split(text):
                at = text.index(sentence, pos)
                pos = at + len(sentence)
                if not TOPIC.search(sentence):
                    continue
                clause_at = at
                for clause in CLAUSE_SPLIT.split(sentence):
                    c_at = text.index(clause, clause_at)
                    clause_at = c_at + len(clause)
                    if BOUND.search(clause):
                        continue
                    if HISTORY_WORD.search(clause) and HISTORY_REF.search(clause):
                        continue
                    for patterns, want, what in (
                        (ROLE_COUNT, expected_roles, "grantable roles"),
                        (CHUNK_COUNT, expected_chunks, "chunks"),
                    ):
                        for pattern in patterns:
                            for m in pattern.finditer(clause):
                                if value(m.group(1)) == want:
                                    continue
                                line = first + bisect.bisect_right(starts, c_at + m.start()) - 1
                                msg = (
                                    f"{rel}:{line}: says {m.group(1)!r} {what}, "
                                    f"the list makes {want}: {clause.strip()!r}"
                                )
                                if msg not in found:
                                    found.append(msg)
    return found


def test_the_list_is_parsed_not_typed():
    roles = grantable_roles()
    assert len(roles) == len(set(roles))
    assert "roles/run.viewer" in roles
    assert "custom_role_ids.worker_firestore" in roles
    # swarmSecretLister came off in #150; the parser must not find it.
    assert not any("secret_lister" in r for r in roles)


def test_every_stated_grantable_role_and_chunk_count_matches_the_list():
    n = len(grantable_roles())
    problems = stale(n, math.ceil(n / CHUNK_SIZE))
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "sentence, ok",
    [
        ("CI may still modify the 15 roles terraform/infra grants.", False),
        ("CI may modify the 14 roles terraform/infra grants (15 before #150 took it off).", True),
        ("whose lists together are the fifteen project roles terraform/infra grants.", False),
        ("Sixteen grantable until 2026-09-25, when roleAdmin came off.", True),
        ("one binding per chunk of at most 10 roles.", True),
        ("two for fourteen roles in each chunk.", True),
        ("three for fourteen roles in each chunk.", False),
        ("Expect (chunk 1 of 3) for the grantable list.", False),
        ("CI can still grant any of the fifteen, unconditioned, to anyone.", False),
    ],
)
def test_the_matcher_tells_a_stale_count_from_a_current_or_historical_one(
    tmp_path, monkeypatch, sentence, ok
):
    doc = tmp_path / "doc.md"
    doc.write_text(sentence + "\n")
    monkeypatch.setitem(globals(), "CHECKED", [doc])
    monkeypatch.setitem(globals(), "REPO", tmp_path)
    assert (not stale(14, 2)) is ok, stale(14, 2)
