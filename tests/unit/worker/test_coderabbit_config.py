"""The repo-root .coderabbit.yaml turns off the hosted app's PR review (issue 140).

Lanes run the CodeRabbit CLI before they publish (test_coderabbit_cli.py pins
it in the image); the hosted app does not review pull requests here, because
lane PRs fold into merge trains and the train's CI is the gate. With no
`.coderabbit.yaml` the app runs on its defaults and reviews every PR, so these
cases keep the file from silently drifting back:

  * the file exists, starts with the yaml-language-server schema comment and
    parses as YAML;
  * reviews.auto_review.enabled and reviews.auto_review.drafts are both
    exactly False (compared explicitly, never by truthiness: a missing key or
    the string "false" is not off);
  * docs/worker-images.md's CodeRabbit section names the file and says the
    hosted app does not review pull requests.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
CONFIG = REPO / ".coderabbit.yaml"
DOC = REPO / "docs" / "worker-images.md"

SCHEMA_COMMENT = (
    "# yaml-language-server: $schema="
    "https://storage.googleapis.com/coderabbit_public_assets/schema.v2.json"
)


def _coderabbit_section() -> str:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"^## CodeRabbit\n(.*?)(?=^## )", text, re.M | re.S)
    assert match, "docs/worker-images.md has no ## CodeRabbit section"
    return match.group(1)


def test_config_turns_off_auto_review_and_drafts() -> None:
    assert CONFIG.is_file(), ".coderabbit.yaml is missing at the repo root"
    text = CONFIG.read_text(encoding="utf-8")
    assert text.splitlines()[0] == SCHEMA_COMMENT
    data = yaml.safe_load(text)
    assert isinstance(data, dict)
    auto_review = data["reviews"]["auto_review"]
    assert auto_review["enabled"] is False
    assert auto_review["drafts"] is False


def test_doc_says_hosted_app_does_not_review_prs() -> None:
    section = " ".join(_coderabbit_section().split())
    assert "`.coderabbit.yaml`" in section
    assert "does not review pull requests" in section
    assert "Review skipped" in section
