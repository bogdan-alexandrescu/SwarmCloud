# Security policy

## Reporting a vulnerability

Report it privately, through GitHub's private vulnerability reporting:
open the repository's **Security** tab and choose **Report a vulnerability**
([GitHub's guide](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)).
Only the maintainers see the report.

Do not open a public issue, pull request or discussion for a vulnerability,
and do not paste a secret, token or credential into the report: say where it
appeared instead.

Useful in a report:

* what an attacker can do, and what they need first;
* the component (`apps/...`, `terraform/...`, `scripts/...`) and the commit;
* the steps to reproduce it.

## What counts

Anything that breaks one of [CONTRACT.md](CONTRACT.md)'s invariants is in
scope, most of all tenant isolation (one tenant reaching another's tasks,
secrets, storage prefix or namespace), fencing, capacity accounting, secret
handling, and anything that touches resources the deployment's never-touch
list protects.

## Supported versions

Fixes land on `main` and ship in the next release. Older releases are not
patched.
