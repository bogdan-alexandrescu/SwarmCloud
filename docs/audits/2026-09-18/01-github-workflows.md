# task_26a501c25d824fb48806  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit .github/workflows/ for checks that appear to gate but do not - a step that cannot fail, a test that is skipped when a condition is not met, a job whose failure does not fail the workflow, a lint that runs on the wrong paths. For each, say what could ship past it. At most 5.

## Report

Audited all four workflows (`application.yml`, `release.yml`, `security.yml`, `terraform.yml`). Findings, worst first:

**1. `application.yml` → `integration` job silently no-ops (lines 242–247)**
```bash
if [ -d tests/integration ] && ls tests/integration/test_*.py >/dev/null 2>&1; then
  uv run pytest tests/integration -q
else
  echo "no integration tests present"
fi
```
This is the *only* place in the repo where the Firestore-emulator tests run — `make test` is explicitly offline (per CLAUDE.md) and never touches `tests/integration`. If the directory is renamed, the files stop matching `test_*.py`, or the last file is ever removed, this step prints a message and exits 0. **What ships:** a broken last-slot-race admission transaction (the one thing CLAUDE.md calls out as needing real Firestore transaction semantics, not a mock) merges with CI green.

**2. `release.yml` is not gated by `application.yml` or `security.yml` at all**
`release.yml` triggers independently on the same `push: branches: [main]` event as the other two workflows, but its own `verify` job (lines 65–83) only runs `pytest tests/unit`, `shellcheck`, and the destroy-guard self-test. It never reruns the frozen-contract diff check, the Kubernetes `runAsNonRoot`/Spot security-posture assertions, checkov, or trivy — and nothing in `release.yml` waits on `application.yml`/`security.yml`'s conclusion (there's no cross-workflow dependency, only GitHub Environment approval for prod). **What ships:** a commit that fails security.yml's secret scan or application.yml's pod-security-context check can still be built, promoted, applied to `dev`, and — after one human clicks approve on an environment that has no idea those other workflows are red — applied to `prod`.

**3. `application.yml` step literally named "format check" doesn't check formatting (lines 146–165)**
It only runs `compileall` and a hand-rolled `ast.parse` + tab-character scan. No black/ruff/isort/style check of any kind (the comment even says so: "without imposing a formatter this repository has not adopted"). **What ships:** arbitrarily inconsistent, unreadable Python — as long as it's syntactically valid ASCII-indented code — past a check whose name tells reviewers style is enforced.

**4. `application.yml` → `build` job doesn't depend on `integration` (line 331: `needs: [python, shell, manifests]`)**
The build job authenticates via WIF as a service account with `secretmanager.admin` / `datastore.owner` / `storage.admin` / `resourcemanager.projectIamAdmin`, and runs the PR author's own checked-in `cloudbuild.yaml`. It's gated by a human approval on the `build-pr` environment, but that approval has no signal about whether the emulator-backed concurrency tests passed — a reviewer can approve (or the job can complete) while `integration` is still running or has failed.

**5. (lower confidence) `terraform.yml` → `plan` job only needs `[validate]` (line 154)**, not `test` (the 86 `terraform test` assertions) or `checkov`. The plan job posts a plan to the PR and is likely the check branch protection points at; if so, a plan can be posted and a PR merged on infrastructure that fails the mock-provider test suite or checkov, since those are sibling jobs whose failure doesn't block `plan` from completing. This depends on branch-protection settings not visible in the repo, so it's the softest finding of the five.
