"""The artifact bucket is `swarm-artifacts-<project>`, and only that.

Terraform composes it in one place:

    terraform/modules/storage/main.tf
      artifact_bucket_name = "${var.name_prefix}-artifacts-${var.bucket_suffix}"
    terraform/infra/main.tf
      module "storage" { name_prefix = var.name_prefix  bucket_suffix = var.project_id }

so the real name is `swarm-artifacts-saga-agents-staging`. Reversed --
`saga-agents-staging-swarm-artifacts` -- it is the same four words naming a
bucket that has never been created, and every failure it produces points
somewhere else:

  * `scripts/lib/common.sh` carried the reversed default until it was fixed, and
    `register-tenant.sh` reported "artifact bucket does not exist yet; run 'make
    infra'" -- advice that cannot work, because `make infra` creates the bucket
    under its real name and the next run says the same thing;
  * `docs/disaster-recovery.md` put it in a `terraform import` under "Recovery
    from total loss", where the failure reads as the bucket having been lost too;
  * `.env.example` put it in the line an operator copies into their own `.env`,
    which then overrides the corrected default in common.sh.

This test is the check that stops the fifth copy. It is offline: it reads the
Terraform source rather than any state or API.

NOT COVERED HERE, deliberately, and both still reversed at the time of writing:

    apps/common/swarm_common/config.py:110
        artifact_bucket=os.environ.get("ARTIFACT_BUCKET", f"{project}-swarm-artifacts")
    kubernetes/render.py:308
        "ARTIFACT_BUCKET": args.bucket or f"{args.project}-swarm-artifacts"

The first is in the FROZEN contract (`apps/common/swarm_common/`), which this
repository's rules say to request a change to rather than make. The second is
Track C. Asserting over them would put a failing test in the tree for code this
track may not edit, so they are named here instead -- a record, not a hiding
place. Both are defaults: terraform/infra/locals.tf sets ARTIFACT_BUCKET
explicitly on the deployed services, so they bite when the variable is unset,
which is exactly what `kubernetes/render.py job` does without `--bucket`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Everything an operator reads, copies or runs. Not `apps/` or `kubernetes/`.
SEARCHED = (
    sorted((REPO / "docs").rglob("*.md"))
    + sorted((REPO / "scripts").rglob("*.sh"))
    + [REPO / "README.md", REPO / ".env.example", REPO / "Makefile"]
)

#: `<anything>-swarm-artifacts` -- the words in the wrong order -- where it is
#: being USED rather than merely named: assigned (`=`), quoted as a value, or
#: written as a `gs://` URI.
#:
#: A backtick-quoted mention is prose. Both `scripts/lib/common.sh` and
#: `docs/disaster-recovery.md` have to be able to write the reversed name down
#: in order to forbid it, and a check that cannot tell an explanation from an
#: instruction would delete its own documentation.
REVERSED = re.compile(r"""(?:=|gs://|["'])\s*\$?\{?[\w$.-]*-swarm-artifacts\b""")


def test_terraform_still_composes_the_name_the_way_this_test_assumes() -> None:
    """If the composition moves, this file must be re-read, not silently pass."""
    storage = (REPO / "terraform" / "modules" / "storage" / "main.tf").read_text()
    assert 'artifact_bucket_name = "${var.name_prefix}-artifacts-${var.bucket_suffix}"' in storage

    infra = (REPO / "terraform" / "infra" / "main.tf").read_text()
    module_storage = infra.split('module "storage" {', 1)[1].split("\n}", 1)[0]
    assert "bucket_suffix = var.project_id" in module_storage, module_storage
    assert "name_prefix   = var.name_prefix" in module_storage, module_storage

    variables = (REPO / "terraform" / "modules" / "storage" / "variables.tf").read_text()
    assert 'default = "swarm"' in variables.split('variable "name_prefix"', 1)[1]


def test_no_operator_facing_file_names_the_reversed_bucket() -> None:
    offenders = []
    for path in SEARCHED:
        if not path.exists():
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = REVERSED.search(line)
            if match:
                offenders.append(
                    f"{path.relative_to(REPO)}:{number}: {match.group(0)}  ({line.strip()})"
                )

    assert not offenders, (
        "the artifact bucket is `swarm-artifacts-<project>`; these name a bucket "
        "that has never existed:\n  " + "\n  ".join(offenders)
    )


def test_the_shell_default_matches_terraform() -> None:
    """`scripts/lib/common.sh` is where every script gets the name."""
    common = (REPO / "scripts" / "lib" / "common.sh").read_text()
    assert 'ARTIFACT_BUCKET="${ARTIFACT_BUCKET:-swarm-artifacts-${PROJECT_ID}}"' in common
