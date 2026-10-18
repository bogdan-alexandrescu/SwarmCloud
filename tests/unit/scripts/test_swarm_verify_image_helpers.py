"""Every Python helper an acceptance group runs is in the swarm-verify image.

The acceptance suites run INSIDE the VPC as the swarm-verify Cloud Run job,
whose image carries only what images/swarm-verify/Dockerfile copies (and
cloudbuild.yaml stages). The git-tools group (G7-ACCEPTANCE, PR 1269) runs
`python3 tests/acceptance/test_runner_git_tools.py` for every prompt and every
verdict, but the image never carried it. Measured on accept runs 38660723288,
38669300566 and 38673253443 (2026-10-15): "python3: can't open file
'/swarm/tests/acceptance/test_runner_git_tools.py'", every prompt went out
empty ("claude-code requires a non-empty string input.prompt"), and all five
git-tools checks failed with "the judge wrote no verdict".

MUTATION: drop the test_runner_git_tools.py COPY line from the Dockerfile, or
its cp line from cloudbuild.yaml.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HELPER = re.compile(r"tests/acceptance/[A-Za-z0-9_./-]+\.py")


def _helpers() -> set[str]:
    found: set[str] = set()
    for script in sorted((ROOT / "scripts" / "acceptance").rglob("*.sh")):
        found.update(HELPER.findall(script.read_text()))
    return found


def _carried(path: str, lines: list[str]) -> bool:
    """`path`, or a directory above it, is copied by one of `lines`."""
    parts = path.split("/")
    candidates = {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
    return any(any(f" {c} " in f" {l} " or f" {c}/ " in f" {l} " for c in candidates) for l in lines)


def test_the_acceptance_scripts_name_python_helpers():
    # A control: the scan must find the helpers it is about, or it proves nothing.
    assert {"tests/acceptance/test_live_browser.py",
            "tests/acceptance/test_runner_git_tools.py"} <= _helpers()


def test_every_acceptance_helper_is_copied_into_the_swarm_verify_image():
    dockerfile = (ROOT / "images" / "swarm-verify" / "Dockerfile").read_text().splitlines()
    copies = [l for l in dockerfile if l.startswith("COPY ")]
    missing = sorted(h for h in _helpers() if not _carried(h, copies))
    assert missing == [], f"not COPYed into images/swarm-verify: {missing}"


def test_every_acceptance_helper_is_staged_and_checked_by_the_image_build():
    build = (ROOT / "images" / "swarm-verify" / "cloudbuild.yaml").read_text().splitlines()
    staged = [l.strip() for l in build if l.strip().startswith("cp ")]
    checked = [l.strip() for l in build if l.strip().startswith("test -f ")]
    for helper in sorted(_helpers()):
        assert _carried(helper, staged), f"cloudbuild.yaml never stages {helper}"
        if helper.endswith(".py") and "/fixtures/" not in helper:
            assert any(helper in l for l in checked), f"cloudbuild.yaml never checks {helper} exists"
