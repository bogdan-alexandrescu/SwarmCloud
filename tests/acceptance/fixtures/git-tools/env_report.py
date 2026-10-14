#!/usr/bin/env python3
"""What credential an agent's environment carries, reported by NAME, never by value.

The git-tools acceptance group (scripts/acceptance/groups/git-tools.sh,
docs/acceptance.md "git-tools") has its rebase lane run this inside the
runner and save the output as an artifact. #1225's acceptance asks that
`env | grep -i token` be empty in the runner; the owner narrowed it on
2026-10-13: no forge-slot value and no GH_TOKEN, GITHUB_TOKEN or GIT_*
credential variable, while CLAUDE_CODE_OAUTH_TOKEN (the Claude CLI's own
login) is expected. The JUDGING is tests/acceptance/test_runner_git_tools.py's
`judge_env`; this only reports, because it runs on the sandbox's copy and
the judge must be this platform's.

NO VALUE IS EVER PRINTED. The runner's environment holds the Claude login, so
a report that echoed values would publish it as an artifact. Printed: every
variable's name; the KEY of each GIT_CONFIG_KEY_<n> (a git config key such as
`credential.helper` -- a name, not a secret); and the names of the variables
whose value has the shape of a forge credential. The shapes are built from
parts so this file is not itself credential-shaped.
"""

from __future__ import annotations

import json
import os
import re
import sys

#: The shapes a GitHub credential takes in a value: a classic or fine-grained
#: personal access token, an App installation or user token, a token in a URL,
#: and a PEM private key (a GitHub App's).
FORGE_SHAPES = (
    re.compile(r"\bgh[pousr]" + r"_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub" + r"_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"x-access" + r"-token:"),
    re.compile(r"-----BEGIN [A-Z ]*" + r"PRIVATE" + r" KEY-----"),
)


def report(environ: dict[str, str]) -> dict[str, object]:
    names = sorted(environ)
    return {
        "names": names,
        "count": len(names),
        "git_config_keys": {
            name: environ[name] for name in names if re.fullmatch(r"GIT_CONFIG_KEY_\d+", name)
        },
        "forge_shaped": [
            name for name in names if any(shape.search(environ[name]) for shape in FORGE_SHAPES)
        ],
    }


if __name__ == "__main__":
    json.dump(report(dict(os.environ)), sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
