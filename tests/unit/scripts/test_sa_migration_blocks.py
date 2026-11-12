"""The platform service accounts live in terraform/bootstrap (DAYONE-BOOT-2, Q7).

docs/design/day-one-install.md §5.3 and docs/design/day-one-plan.md §8:
bootstrap's grants name accounts that terraform/infra used to create, so a
fresh project's first bootstrap apply needed "later" switches. Every platform
account now lives in bootstrap; the live deployment moves with zero destroys:

* infra forgot each account with `removed { ... destroy = false }`, and the
  blocks were deleted on 2026-11-11 once neither state held their addresses;
* bootstrap adopts each one with an `import {}` gated on
  `adopt_from_infra_states`, as platform_roles.tf does for the custom roles;
* infra reads the accounts as data sources and declares none of them.

Read as text: unit tests run with no Terraform, no credentials and no cloud.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TF = ROOT / "terraform"

# The five infra addresses that held the platform accounts before BOOT-2, and
# the bootstrap resource each one becomes.
MIGRATED = {
    "module.iam.google_service_account.platform": "google_service_account.platform",
    "module.iam.google_service_account.tick": "google_service_account.tick",
    "google_service_account.verify": "google_service_account.verify",
    "module.scheduler.google_service_account.schedule_tick": "google_service_account.schedule_tick",
    "module.scheduler.google_service_account.rollup_sweeper": "google_service_account.rollup_sweeper",
}

# Accounts no root had before BOOT-2: created by bootstrap, nothing to import.
NEW_IN_BOOTSTRAP = ("discovery", "doctor")

# Service accounts infra (or a module it calls) may still declare: the
# per-tenant workers (no install has Terraform tenants; the live ones are not
# platform accounts) and the addon:ci MCP test server (plan §8).
INFRA_ALLOWED = {
    ("modules/tenancy", "worker"),
    ("infra", "mcp_test_server"),
}

_SA_HEADER = re.compile(r'^resource\s+"google_service_account"\s+"([A-Za-z0-9_]+)"', re.M)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _blocks(text: str, keyword: str) -> list[str]:
    """Every top-level `<keyword> { ... }` block, by brace counting."""
    out: list[str] = []
    for match in re.finditer(rf"^{keyword}\s*\{{", text, re.M):
        depth = 0
        for i in range(match.end() - 1, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    out.append(text[match.start() : i + 1])
                    break
    return out


def _root_blocks(root: str, keyword: str) -> list[str]:
    return [b for p in sorted((TF / root).glob("*.tf")) for b in _blocks(_text(p), keyword)]


def _bootstrap_accounts() -> set[str]:
    return {
        name for p in sorted((TF / "bootstrap").glob("*.tf")) for name in _SA_HEADER.findall(_text(p))
    }


def test_no_root_carries_the_applied_forget_blocks() -> None:
    """The `moved`/`removed` blocks retired after both states applied them.

    On 2026-11-11 the dev infra state held none of the addresses
    terraform/infra/removed.tf named (BOOT-2's five accounts and W9's personal
    tenant), the bootstrap state none of terraform/bootstrap/removed.tf's, and
    no prod state existed, so each block was a no-op and both files went. A
    block whose `from` is gone from state changes no plan; one left behind is
    dead text that names a tenant's old addresses.
    """
    gone = [f"terraform/{r}/removed.tf" for r in ("infra", "bootstrap") if (TF / r / "removed.tf").exists()]
    assert not gone, f"retired after apply (2026-11-11); delete: {gone}"
    stale = []
    for root in ("infra", "bootstrap"):
        for block in _root_blocks(root, "removed") + _root_blocks(root, "moved"):
            for address in re.findall(r"^\s*(?:from|to)\s*=\s*(\S+)\s*$", block, re.M):
                if address in MIGRATED or "_w9_" in address:
                    stale.append(f"{root}: {address}")
    assert not stale, (
        "terraform/infra forgot the platform accounts and W9's tenant once; no "
        f"state holds those addresses any more, so no block may name them: {stale}"
    )


def test_every_bootstrap_sa_has_gated_import() -> None:
    accounts = _bootstrap_accounts()
    wanted = {target.rsplit(".", 1)[1] for target in MIGRATED.values()} | set(NEW_IN_BOOTSTRAP)
    assert wanted <= accounts, f"terraform/bootstrap declares no account for: {sorted(wanted - accounts)}"

    imports = _root_blocks("bootstrap", "import")
    bad = []
    for target in MIGRATED.values():
        hits = [b for b in imports if re.search(rf"^\s*to\s*=\s*{re.escape(target)}(\[|\s*$)", b, re.M)]
        gated = [
            b
            for b in hits
            if re.search(r"for_each\s*=\s*length\(var\.adopt_from_infra_states\)\s*>\s*0\s*\?", b)
        ]
        if len(gated) != 1:
            bad.append(target)
    assert not bad, (
        "each adopted account needs one `import {}` whose for_each is "
        "`length(var.adopt_from_infra_states) > 0 ? ... : ...` "
        f"(platform_roles.tf's gate); missing or ungated: {bad}"
    )
    for name in NEW_IN_BOOTSTRAP:
        assert not any(
            re.search(rf"to\s*=\s*google_service_account\.{name}\b", b) for b in imports
        ), f"google_service_account.{name} is new: no root ever held it, so nothing imports it"


def test_infra_declares_no_platform_sa() -> None:
    found = []
    for p in sorted((TF / "infra").glob("*.tf")):
        found += [("infra", n, p) for n in _SA_HEADER.findall(_text(p))]
    for p in sorted(TF.glob("modules/*/*.tf")):
        found += [(f"modules/{p.parent.name}", n, p) for n in _SA_HEADER.findall(_text(p))]
    extra = [
        f"{p.relative_to(ROOT)}: {name}" for root, name, p in found if (root, name) not in INFRA_ALLOWED
    ]
    assert not extra, (
        "a platform service account is declared outside terraform/bootstrap; infra "
        f"reads them as data sources (plan §8): {extra}"
    )


def test_no_root_declares_an_account_twice() -> None:
    """The ids are spelled once (modules/service_account_ids); the account is made once."""
    ids = _text(TF / "modules" / "service_account_ids" / "main.tf")
    for key in ("discovery_id", "doctor_id", "voice_id"):
        assert re.search(rf"^\s*{key}\s*=\s*\"swarm-", ids, re.M), f"{key} is not spelled in service_account_ids"
    bootstrap = _bootstrap_accounts()
    infra = {n for p in (TF / "infra").glob("*.tf") for n in _SA_HEADER.findall(_text(p))}
    assert not (bootstrap & infra), f"declared in both roots: {sorted(bootstrap & infra)}"

