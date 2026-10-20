"""The QA identity's IAP accessor rows wait for the QA account (#1649).

Measured 2026-10-20 at 02:20Z in the owner-approved bootstrap apply of PR 1666:
`frontend_accessors["swarm-ui-backend serviceAccount:swarm-qa@..."]` ran before
`google_service_account.qa_identity[0]` existed, and IAP answered 400 "Service
account swarm-qa@... does not exist". Four of the five rows were created and
the fifth needed a second apply. Terraform orders resources by reference, and
the accessor names the account only as a string in `var.frontend_iap_members`,
so nothing orders the two unless the resource says so. The ordering itself
cannot be exercised offline (the mock provider creates everything at once), so
this holds the declaration that produces it.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth = 0
    for at in range(text.index("{", start), len(text)):
        if text[at] == "{":
            depth += 1
        elif text[at] == "}":
            depth -= 1
            if depth == 0:
                return text[start : at + 1]
    raise AssertionError(f"unterminated block: {header}")


def test_the_iap_accessors_wait_for_the_qa_account() -> None:
    wif = (REPO / "terraform" / "bootstrap" / "wif.tf").read_text()
    block = _block(wif, 'resource "google_iap_web_backend_service_iam_member" "frontend_accessors"')
    deps = re.search(r"depends_on\s*=\s*\[([^\]]*)\]", block)
    assert deps, "frontend_accessors declares no depends_on, so swarm-qa's rows can run before swarm-qa exists"
    assert "google_service_account.qa_identity" in deps.group(1)
