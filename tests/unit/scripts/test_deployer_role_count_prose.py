"""The deployer's grantable-role list is parsed, never typed (archived issue 68).

WHY THIS EXISTS. The owner removes the deployer's unconditioned
`projectIamAdmin` by hand, following the comments in
terraform/bootstrap/terraform.tfvars, docs/ci.md and the refusal-probe
runbook. archived pull request 150 took `swarmSecretLister` off
`local.deployer_grantable_project_roles` (15 roles became 14), and the prose
those steps send the owner to went on saying "fifteen" in nine places -- a
runbook that disagrees with the code is the one followed at 3am.

So this reads the list's real length out of
terraform/bootstrap/deployer_conditions.tf -- the literal predefined roles
plus the keys of the custom_role_ids loop, parsed, never typed in here -- and
holds the list to the shape the rest of terraform relies on.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CONDITIONS = REPO / "terraform" / "bootstrap" / "deployer_conditions.tf"

# A role a bootstrap variable adds to the list only when on, the shape
# `var.<flag> ? [module.custom_role_ids.names["<key>"]] : []`. The counts the
# prose states are of the list as committed, with every such flag at its
# default, false (archived issue 1384, HOOKS-ROLE).
GATED = re.compile(
    r'var\.(\w+)\s*\?\s*\[\s*module\.custom_role_ids\.names\["([a-z_]+)"\]\s*\]\s*:\s*\[\s*\]'
)
BOOTSTRAP = REPO / "terraform" / "bootstrap"


def _concat_body() -> str:
    """concat()'s arguments, comments dropped."""
    text = CONDITIONS.read_text()
    m = re.search(
        r"^\s*deployer_grantable_project_roles\s*=\s*concat\((.*?)^\s*\)\s*$",
        text,
        re.M | re.S,
    )
    assert m, "deployer_grantable_project_roles = concat(...) not found"
    return "\n".join(
        line for line in m.group(1).splitlines() if not line.lstrip().startswith("#")
    )


def gated_roles() -> dict[str, str]:
    """custom_role_ids key -> the bootstrap variable that puts it on the list."""
    return {key: flag for flag, key in GATED.findall(_concat_body())}


def flags_on() -> set[str]:
    """The gated flags terraform/bootstrap/terraform.tfvars records as true: what
    an apply of the committed tfvars turns on."""
    text = (BOOTSTRAP / "terraform.tfvars.example").read_text()
    return set(re.findall(r"^\s*(\w+)\s*=\s*true\s*$", text, re.M))


def static_roles() -> list[str]:
    """The list with every gated role left off: the literal roles and the
    custom_role_ids loop."""
    body = _concat_body()
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


def grantable_roles() -> list[str]:
    """local.deployer_grantable_project_roles as an apply of the committed
    terraform/bootstrap/terraform.tfvars builds it: the static list plus each
    gated role whose flag the tfvars records as true. The counts the prose
    states are of this list (archived pull request 1430 turned swarmGithubHooksDedup on)."""
    on = flags_on()
    return static_roles() + [
        f"custom_role_ids.{key}" for key, flag in gated_roles().items() if flag in on
    ]


def test_the_list_is_parsed_not_typed():
    roles = grantable_roles()
    assert len(roles) == len(set(roles))
    assert "roles/run.viewer" in roles
    assert "custom_role_ids.worker_firestore" in roles
    # swarmGithubHooksDedup is on the list because the committed tfvars turns it on.
    assert "custom_role_ids.github_hooks_dedup" in roles
    # swarmSecretLister came off in archived pull request 150; the parser must not find it.
    assert not any("secret_lister" in r for r in roles)


def test_ci_may_grant_no_container_role():
    """swarm-verify's conditioned roles/container.clusterViewer is granted from
    terraform/bootstrap (verify_cluster_read.tf), which the owner applies, and
    never from terraform/infra (VERIFY-GKE-READ-2, part of archived issue 1754; owner
    decision 2026-10-21). hasOnly() limits which roles CI grants, never to whom
    or under what condition, so a container role on this list is one CI could
    grant itself unconditioned, reaching the other team's peer-cluster
    cluster. The prose counts are therefore of a list without it: 15.
    MUTATION: put "roles/container.clusterViewer" back on
    deployer_grantable_project_roles and this is red."""
    roles = grantable_roles()
    assert not [r for r in roles if r.startswith("roles/container.")], roles
    assert len(roles) == 15, roles


def test_a_gated_role_is_off_by_default_and_read_by_nothing_else():
    """swarmGithubHooksDedup (terraform/bootstrap/github_hooks_role.tf) joins
    the list only when enable_github_hooks_dedup_role is on. The variable
    defaults to false (so any root that does not set it is unchanged), is not in
    the static list, and terraform/bootstrap/terraform.tfvars records it as true
    (archived pull request 1430, the owner's apply): a -var that is not committed there makes the next
    plan from main read it as false and destroy the role. Every count in prose
    is therefore of the ON list, which grantable_roles() reads.
    MUTATION: add it to the custom_role_ids loop, default its variable to true,
    or drop the tfvars line."""
    gated = gated_roles()
    assert gated == {"github_hooks_dedup": "enable_github_hooks_dedup_role"}, gated
    for key, flag in gated.items():
        assert f"custom_role_ids.{key}" not in static_roles(), key
        assert f"custom_role_ids.{key}" in grantable_roles(), key
        declared = [
            p
            for p in sorted(BOOTSTRAP.glob("*.tf"))
            if re.search(rf'^variable\s+"{flag}"\s*\{{', p.read_text(), re.M)
        ]
        assert len(declared) == 1, f"{flag} is declared in {declared}"
        block = re.search(
            rf'^variable\s+"{flag}"\s*\{{(.*?)^\}}', declared[0].read_text(), re.M | re.S
        )
        assert block and re.search(r"^\s*default\s*=\s*false\s*$", block.group(1), re.M), (
            f"{flag} must default to false: on, it changes every projectIamAdmin chunk"
        )
        assert re.search(
            rf"^\s*{flag}\s*=\s*true\s*$",
            (BOOTSTRAP / "terraform.tfvars.example").read_text(),
            re.M,
        ), f"terraform/bootstrap/terraform.tfvars must record {flag} = true, the value the owner applied"


def test_every_role_on_the_list_is_one_the_parser_reads():
    """concat()'s arguments are the literal roles, the custom_role_ids loop and
    gated roles, and nothing else: a role added in a fourth shape would be on
    the list but missing from every count this file checks."""
    body = _concat_body()
    rest = re.sub(r"\s*\[(.*?)\]", "", body, count=1, flags=re.S)
    rest = re.sub(
        r"\[\s*for\s+key\s+in\s+\[.*?\]\s*:\s*module\.custom_role_ids\.names\[key\]\s*\]",
        "",
        rest,
        flags=re.S,
    )
    rest = GATED.sub("", rest)
    assert re.fullmatch(r"[\s,]*", rest), f"an argument the parser does not read: {rest!r}"


def test_the_header_counts_the_grants_conditioned_from_the_start():
    """deployer_conditions.tf's header counts the deployer's unconditioned
    roles ("Fifteen predefined roles and one custom role"). A grant written
    there conditioned from the start -- not behind deployer_scoped_roles, so not
    one of those -- is the deployer's too, and the header names each one, so
    the prose the owner reads before a bootstrap apply says what that apply
    adds. Today one: swarmSpeechDeployer, the speech machine's instance and disk
    verbs (owner's decided follow-up, 2026-11-05). None of them is a role CI may
    grant: hasOnly() limits which roles CI grants, never under what condition.
    MUTATION: drop the header's paragraph, or add a second such grant without
    naming it there, and this is red."""
    text = CONDITIONS.read_text()
    m = re.search(
        r"ONE MORE GRANT HERE IS CONDITIONED FROM THE START(.*?)(?:\n#\n)", text, re.S
    )
    assert m, "the header has no paragraph for the grants conditioned from the start"
    paragraph = m.group(1)

    # Every project binding here that is not switched by deployer_scoped and
    # names a custom role this file defines.
    always = re.findall(
        r'^resource\s+"google_project_iam_member"\s+"(\w+)"\s*\{\n\s*count\s*=\s*local\.deployer_enabled\n',
        text,
        re.M,
    )
    assert always == ["deployer_speech_machine"], always
    ids = re.findall(
        r'^resource\s+"google_project_iam_custom_role"\s+"\w+"\s*\{.*?role_id\s*=\s*"(\w+)"',
        text,
        re.M | re.S,
    )
    assert ids == ["swarmSpeechDeployer"], ids
    for role_id in ids:
        assert role_id in paragraph, f"the header does not name {role_id}"
        assert not any(role_id in r for r in grantable_roles())
