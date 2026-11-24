# Day One snag log (template)

Copy this file once per proof install. Name the copy after the run, for
example `snag-log-proof-1.md`, and keep it outside the repository or attach it
to the epic. Fill one row for every step of
[the proof install runbook](day-one-proof-install.md), clean or not: "no snag"
is a result.

**Never write a secret here.** No token, no client secret, no Claude sign-in
code, no environment dump, and no log line that could hold one. Where a value
was typed, write *typed at the product's prompt*. Where a secret showed up
somewhere it should not have, write where it appeared, never the value, and
file it as S0.

Use placeholders for anything that identifies a company, a person or a real
deployment: `<proof-project-1>`, `<host-1>`, `<example.com>`,
`<example-org>`, `<owner>`, `<teammate>`.

## Header

| Field | Value |
|---|---|
| Run | proof install 1 / proof install 2 / reinstall into project 1 / upgrade |
| Release | `<V>-rc.<n>` (as `swarm install --status` prints it) |
| Project | `<proof-project-N>` |
| Hostname | `<host-N>` |
| Install type | team / one person (stage 0's answer) |
| Machine | macOS or Linux, shell, Claude Code version (for the teammate) |
| Started (UTC) | |
| Ended (UTC) | |
| Run by | `<owner>` (and `<teammate>` for section 5) |

## Before starting

One row per item in the runbook's section 3.

| Item | How it was checked | Result | Note |
|---|---|---|---|
| Google organisation and admin | | ok / missing | |
| Project Creator on the parent | | | |
| Billing User on a full billing account | | | |
| New project id (installer creates it) | | | |
| Hostname and DNS access | | | |
| GitHub organisation, App rights, target repository | | | |
| Claude subscriptions (count) | | | |
| Teammate ready (account, GitHub, Claude Code version) | | | |
| Terminal tools | | | |
| Release-home rights | | | |

## Steps

One row per runbook step (A1, B1, B2, C1 to C16, D1 to D6, E1 to E3, T1 to
T5, R1, R2). A step with more than one snag gets one row per snag.

| Step | Start (UTC) | End (UTC) | Who | Result | What happened (quote the product's exact words) | Snag? | Severity | Issue | Disposition |
|---|---|---|---|---|---|---|---|---|---|
| A1 | | | | pass / fail / skipped | | no | | | |
| B1 | | | | | | | | | |
| B2 | | | | exit code: | | | | | |
| C1 | | | | | | | | | |

**Result** is `pass`, `fail` or `skipped (why)`. For a `swarm` command, add its
exit code.

**Snag?** is `yes` when any of these happened:

- the step needed anything outside the product (a command not printed by the
  product, a file edited, a person from the build team);
- the product's words were wrong, missing or misleading, including a known
  disagreement (runbook K1 to K9) met on the way;
- a wait ran past its usual time by more than half again;
- the step failed and needed a resume, a doctor or a repair.

**Severity** follows the issue forms' scale. S0 is only for tenant isolation,
the shared project's resources, capacity accounting, fencing or secrets.

**Issue** is the number of the issue filed for this snag, one issue per snag,
titled as a fact.

**Disposition** is `fixed in #<pull request>` or
`accepted by <owner initials>, <date>, because ...`. A row with an empty
disposition blocks the sign-off.

## Timings

Fill from the terminal's `Stage N` lines and the console's times. The
repository gives no figure for stages 4 to 7; these rows are how the next
release gets one.

| Stage | Started | Ended | Minutes | Usual (runbook 2.6) |
|---|---|---|---|---|
| 0 | | | | waits only |
| 1 | | | | a few minutes |
| 2 | | | | DNS: minutes after the record |
| 3 | | | | up to 10 |
| 4 | | | | not stated |
| 5 | | | | not stated |
| 6 | | | | not stated |
| 7 | | | | not stated |
| 8 | | | | up to 1 h |
| 10 | | | | a few minutes |
| 13 (console) | | | | 20 to 60 |
| 14 | | | | 15 to 25 |
| Total | | | | |

## What remains after the uninstall

Paste the uninstall's `What remains of the SwarmCloud install in <project>:`
output. It holds names, never secrets. Then tick each kind.

| Kind | Listed? | Expected? | Note |
|---|---|---|---|
| oauth-brand | | yes | |
| kms-key-ring | | yes | |
| custom-roles | | yes | undelete deadline: uninstall date + 7 days |
| firestore-database | | only if not deleted | |
| desktop-client | | yes | |
| github-app | | yes | |
| dns-record | | only at a registrar | |
| workspace-groups | | yes | |
| Also not removed | | only unlabelled resources, the budget if refused, the project if kept | |

## Sign-off

The runbook's section 7, one row per line, with the evidence (a link, a
pasted line, or the step row above).

| Checklist line | Evidence | Owner's tick and date |
|---|---|---|
| | | |
