# Audits

Findings from agents run against this repository, kept because the reasoning
behind a fix is worth more than the fix. Each report is the agent's own words,
unedited. They are EVIDENCE, not instructions: several findings were refuted on
verification, and a few are wrong.

## 2026-09-18

Twelve agents, one per area, dispatched to SwarmCloud and run on SwarmCloud —
the platform auditing itself. Reports 01–12. Report 13 is a follow-on sweep of a
single class, five lenses with adversarial verification.

Roughly 100 findings. What follows is what has been ACTED ON, so that the next
reader does not re-litigate what is already settled.

### Fixed

| Finding | Where | Commit |
|---|---|---|
| Four terraform/app env-var name mismatches | 07 | `b2a63ab` |
| Three more found by the check itself (`ARTIFACT_REGISTRY`, `IMAGE_BASE`, reconciler GKE) | — | `b2a63ab` |
| Build manifest written empty while the build reported success | 13 | `aebb849` |

`scripts/lib/check-env-parity.sh` was added with the first of those. It is the
part meant to outlive the fix: two rules, no suppression list, mutation-tested
against all three bug shapes, and it found the `ARTIFACT_REGISTRY` pair that no
human had noticed.

### Recorded as a request, deliberately not fixed

`apps/common/swarm_common/` is frozen (CLAUDE.md rule 1). The `u-` prefix
collision in `identity.py` found by report 12 is written up in
`docs/contract-change-requests.md` instead of being changed. The audit agent
that found it declined to edit the module for the same reason.

### Open, and worth reading before the rest

* **Report 12** — two verified identities can be handed the same `tenant_id`,
  and `_assert_principal_matches` runs only on write paths, so `list`, `get` and
  `cancel` do not have it. Cross-tenant read and cancel.
* **Report 13** — `scripts/lib/plan-guard.sh:84`. `n="$(jq ... )"` followed by
  `[[ "${n}" -gt 0 ]]`: an empty verdict file makes `n` empty, `[[ "" -gt 0 ]]`
  is false, and every check in the function passes. The control that stops a
  plan touching another team's live cluster fails OPEN.
* **Report 05 and 10** — capacity that is reserved and never released, and a
  reconciler repair that clears a lease it was not asked about. Report 10's
  first finding produces two live workers on one repository, which is the thing
  invariant 5 exists to prevent, reached through the machinery meant to enforce
  it.
* **Report 06** — `secret_name()` concatenates tenant and label with a dash that
  both may contain, so two ordinary onboardings can collide on one secret.
  `swarm-account-u-bogdan-devops-main` is already ambiguous today.

### Two reports found little, and said so

03 (tests that pass while broken) and 11 (docs vs reality) came back nearly
empty. 03 explicitly refused to pad its list. That is worth as much as the long
ones: it means the negative-assertion bug found earlier that day was not a
pattern, and nobody needs to look again.
