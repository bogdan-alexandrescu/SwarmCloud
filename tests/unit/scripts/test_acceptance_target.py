"""Release acceptance runs in the `smoke` tenant against the private sandbox (#628).

Owner decision 2026-10-05 (history I5). Since 2026-09-30 the release's
acceptance suite opened and closed 58 fixture pull requests ("Fix add()...")
on the public repository, each one running full CI there, and its tasks --
submitted as swarm-verify, which resolves to `eng` -- were 62% of eng's
tasks, so eng's success rates and failure classes could not be read. The
`smoke` tenant had none.

What these hold, none of it needing a deployment, a credential or a network:

* the acceptance configuration (scripts/acceptance/config.sh) resolves to the
  `smoke` tenant and the private sandbox repository, and refuses a target
  that is the repository this checkout or this CI run is for;
* nothing the acceptance suite runs, and nothing in the release's acceptance
  job, names the public repository -- the guard;
* every API call the suite makes carries `X-Swarm-Tenant: smoke`
  (common.sh api_request honours SWARM_API_TENANT, and lib.sh exports it), and
  the suite refuses to run when the API resolves it to any other tenant;
* the suite refuses a repository that is publicly readable;
* dev.tfvars gives `smoke` a principal that slugs to `smoke` and registers it
  as a directory group -- the only way X-Swarm-Tenant can select it -- and
  does NOT list swarm-verify under `service_accounts`, which would make it a
  continuation-scoped account that can submit nothing but a continuation.
"""

from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from swarm_common.identity import tenant_id_for_group

from .test_dev_tfvars_capacity import DEV_TFVARS, _block, _entries, _strip_comments

ROOT = Path(__file__).resolve().parents[3]
ACCEPTANCE = ROOT / "scripts" / "acceptance"
CONFIG = ACCEPTANCE / "config.sh"
LIB = ACCEPTANCE / "lib.sh"
COMMON = ROOT / "scripts" / "lib" / "common.sh"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

#: The repository the owner created for acceptance on 2026-10-05: private,
#: default branch main.
SANDBOX = "bogdan-alexandrescu/swarmcloud-sandbox"
#: The public repository acceptance must never name. Named HERE, in the
#: guard, and nowhere the suite runs. Spelled from two pieces so a grep of the
#: suite's own tree for the name never matches this file by accident.
PUBLIC = "bogdan-alexandrescu/" + "SwarmCloud"
#: The public name as it appears anywhere: any case, with or without `.git`,
#: but not as the prefix of a longer name (the sandbox's own name starts with
#: it, in lower case).
PUBLIC_RE = re.compile(re.escape(PUBLIC) + r"(?![\w-])", re.IGNORECASE)

SWARM_VERIFY = "swarm-verify@saga-agents-staging.iam.gserviceaccount.com"


def _clean_env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("API_", "SWARM_", "GITHUB_", "GH_"))
    }
    env["NO_COLOR"] = "1"
    env.update(extra)
    return env


def _bash(script: str, env: dict[str, str], cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _config(**env: str) -> subprocess.CompletedProcess:
    script = (
        f"source {shlex.quote(str(CONFIG))}\n"
        'acc_config_problem || exit 3\n'
        'printf "tenant=%s\\nurl=%s\\nrepo=%s\\nref=%s\\n" '
        '"${ACC_TENANT}" "${ACC_REPOSITORY_URL}" "${ACC_GITHUB_REPO}" "${ACC_REF}"\n'
    )
    return _bash(script, _clean_env(**env))


def _values(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------


def test_the_acceptance_config_resolves_to_the_smoke_tenant_and_the_sandbox():
    result = _config()
    assert result.returncode == 0, result.stderr
    got = _values(result.stdout)
    assert got["tenant"] == "smoke"
    assert got["repo"] == SANDBOX
    assert got["url"] == f"https://github.com/{SANDBOX}.git"
    assert got["ref"] == "main"


def test_the_config_refuses_the_repository_this_ci_run_is_for():
    result = _config(
        SWARM_ACCEPTANCE_REPOSITORY_URL=f"https://github.com/{PUBLIC}.git",
        GITHUB_REPOSITORY=PUBLIC,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "refus" in result.stderr.lower(), result.stderr


def test_the_config_refuses_the_repository_this_checkout_was_cloned_from(tmp_path):
    # A laptop or agent run has no GITHUB_REPOSITORY, but its checkout knows
    # where it came from. A copy of config.sh in a fresh repository whose
    # origin is the target stands in for this checkout.
    repo = tmp_path / "checkout"
    (repo / "scripts" / "acceptance").mkdir(parents=True)
    (repo / "scripts" / "acceptance" / "config.sh").write_text(CONFIG.read_text())
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", f"git@github.com:{PUBLIC.lower()}.git"],
        check=True,
    )
    script = (
        f"source {shlex.quote(str(repo / 'scripts' / 'acceptance' / 'config.sh'))}\n"
        "acc_config_problem || exit 3\n"
    )
    result = _bash(
        script,
        _clean_env(SWARM_ACCEPTANCE_REPOSITORY_URL=f"https://github.com/{PUBLIC}"),
        cwd=repo,
    )
    assert result.returncode == 3, result.stdout + result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/someone/sandbox.git",
        "https://gitlab.com/someone/sandbox.git",
        "https://github.com/someone",
        "",
    ],
)
def test_the_config_refuses_a_repository_url_it_cannot_read_as_github(url):
    result = _config(SWARM_ACCEPTANCE_REPOSITORY_URL=url) if url else _config(
        SWARM_ACCEPTANCE_REPOSITORY_URL=" "
    )
    assert result.returncode == 3, result.stdout + result.stderr


@pytest.mark.parametrize("tenant", ["Smoke", "smoke eng", "../eng", "-x"])
def test_the_config_refuses_a_tenant_that_is_not_a_tenant_id(tenant):
    result = _config(SWARM_ACCEPTANCE_TENANT=tenant)
    assert result.returncode == 3, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# The guard: nothing acceptance runs can name the public repository
# ---------------------------------------------------------------------------


def _acceptance_files() -> list[Path]:
    return sorted(p for p in ACCEPTANCE.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def _acceptance_job_text() -> str:
    """The `acceptance:` job of release.yml as written, comments included."""
    text = RELEASE.read_text()
    match = re.search(r"(?m)^  acceptance:\n", text)
    assert match, "release.yml has no `acceptance` job"
    rest = text[match.end():]
    end = re.search(r"(?m)^  [A-Za-z0-9_-]+:\n|^  # -{10,}", rest)
    return rest[: end.start()] if end else rest


def test_the_guard_pattern_catches_every_spelling_and_not_the_sandbox():
    for spelling in (
        PUBLIC,
        PUBLIC.lower(),
        f"https://github.com/{PUBLIC}.git",
        f"https://raw.githubusercontent.com/{PUBLIC}/main/x",
        f"repos/{PUBLIC.upper()}/pulls",
    ):
        assert PUBLIC_RE.search(spelling), spelling
    assert not PUBLIC_RE.search(f"https://github.com/{SANDBOX}.git")


def test_the_guard_visits_every_acceptance_file():
    files = _acceptance_files()
    # Fewer than this means the walk lost its way, not that the suite shrank:
    # the entry scripts, the five wrappers and the five group files alone are
    # more.
    assert len(files) >= 15, [str(p) for p in files]
    assert CONFIG in files and LIB in files


@pytest.mark.parametrize("path", _acceptance_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_acceptance_file_names_the_public_repository(path):
    text = path.read_text(errors="replace")
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(text.splitlines(), 1)
        if PUBLIC_RE.search(line)
    ]
    assert not hits, f"{path.relative_to(ROOT)} names the public repository:\n" + "\n".join(hits)


def test_the_release_acceptance_job_does_not_name_the_public_repository():
    job = _acceptance_job_text()
    assert "verify-remote.sh" in job, "the slice is not the acceptance job"
    assert not PUBLIC_RE.search(job)
    # github.repository is the public repository in every run of this
    # workflow; the job must not hand it to the suite or the sweep either.
    assert "github.repository" not in job


# ---------------------------------------------------------------------------
# The tenant: every API call carries X-Swarm-Tenant
# ---------------------------------------------------------------------------

FAKE_CURL = r"""#!/usr/bin/env bash
printf '%s\n' "$@" >>"${FAKE_DIR}/curl.args"
printf '{}\n200'
"""


def _api_request(tmp_path: Path, tenant: str | None) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "curl"
    fake.write_text(FAKE_CURL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = _clean_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        FAKE_DIR=str(tmp_path),
        SWARM_ENV_FILE=str(tmp_path / "no.env"),
        PROJECT_ID="swarm-test-project",
        API_URL="http://api.invalid",
    )
    if tenant is not None:
        env["SWARM_API_TENANT"] = tenant
    script = (
        f"source {shlex.quote(str(COMMON))}\n"
        "api_credential() { printf 'fake'; }\n"
        'api_request GET "/v1/tenants/me" >/dev/null\n'
        'api_request POST "/v1/tasks" "{}" >/dev/null\n'
    )
    result = _bash(script, env)
    assert result.returncode == 0, result.stderr
    return (tmp_path / "curl.args").read_text().splitlines()


def test_api_request_sends_x_swarm_tenant_when_swarm_api_tenant_is_set(tmp_path):
    args = _api_request(tmp_path, "smoke")
    assert args.count("X-Swarm-Tenant: smoke") == 2, args


def test_api_request_sends_no_tenant_header_by_default(tmp_path):
    args = _api_request(tmp_path, None)
    assert not any("X-Swarm-Tenant" in a for a in args), args


def _lib(body: str, **env: str) -> subprocess.CompletedProcess:
    script = f"""set -euo pipefail
REPO_ROOT={shlex.quote(str(ROOT))}
die() {{ printf 'DIE %s\\n' "$*" >&2; exit 9; }}
t_pass() {{ :; }}; t_fail() {{ :; }}; t_case() {{ :; }}; t_skip() {{ :; }}; t_info() {{ :; }}; info() {{ :; }}
source {shlex.quote(str(LIB))}
{body}
"""
    return _bash(script, _clean_env(**env))


def test_the_suite_exports_its_tenant_to_every_api_call():
    result = _lib('printf "%s" "${SWARM_API_TENANT}"; bash -c \'printf "|%s" "${SWARM_API_TENANT}"\'')
    assert result.returncode == 0, result.stderr
    assert result.stdout == "smoke|smoke"


@pytest.mark.parametrize(
    ("status", "body", "ok"),
    [
        ("200", '{"tenant": {"tenant_id": "smoke"}, "principal": {}}', True),
        ("200", '{"tenant": {"tenant_id": "eng"}, "principal": {}}', False),
        ("403", '{"code": "tenant_not_member", "message": "no"}', False),
        ("200", "{}", False),
    ],
    ids=["smoke", "eng", "not-a-member", "no-tenant"],
)
def test_the_suite_refuses_to_run_in_any_tenant_but_its_own(status, body, ok):
    stub = f"""
API_STATUS=0
api_get() {{ API_STATUS={status}; printf '%s' {shlex.quote(body)}; [[ "${{API_STATUS}}" == 200 ]]; }}
acc_require_tenant
echo REACHED
"""
    result = _lib(stub)
    if ok:
        assert result.returncode == 0, result.stderr
        assert "REACHED" in result.stdout
    else:
        assert result.returncode == 9, result.stdout + result.stderr
        assert "REACHED" not in result.stdout
        assert "smoke" in result.stderr


@pytest.mark.parametrize(
    ("codes", "ok"),
    [(["404"], True), (["200"], False), (["503", "404"], True), (["503", "503"], False), (["000", "000"], False)],
    ids=["private", "public", "flake-then-private", "unanswered", "unreachable"],
)
def test_the_suite_refuses_a_repository_anyone_can_read(tmp_path, codes, ok):
    (tmp_path / "codes").write_text("\n".join(codes) + "\n")
    # The counter is a file: the suite calls curl inside $(...), whose
    # subshell would drop a shell variable's increment.
    count = tmp_path / "count"
    count.write_text("0\n")
    stub = f"""
curl() {{
  local n
  n=$(( $(cat {shlex.quote(str(count))}) + 1 ))
  printf '%s\\n' "$n" >{shlex.quote(str(count))}
  for a in "$@"; do [[ "$a" != *Authorization* ]] || {{ echo AUTH >&2; return 2; }}; done
  sed -n "${{n}}p" {shlex.quote(str(tmp_path / 'codes'))}
}}
sleep() {{ :; }}
acc_require_private_repository
echo REACHED
"""
    result = _lib(stub)
    assert "AUTH" not in result.stderr, "the publicness probe must be anonymous: a token reads a private repository too"
    if ok:
        assert result.returncode == 0, result.stderr
        assert "REACHED" in result.stdout
    else:
        assert result.returncode == 9, result.stdout + result.stderr


def test_run_sh_checks_the_repository_and_the_tenant_before_any_group_runs():
    text = (ACCEPTANCE / "run.sh").read_text()
    platform = text.index("\n  require_platform\n")
    private = text.index("\n  acc_require_private_repository\n")
    tenant = text.index("\n  acc_require_tenant\n")
    groups = text.rindex('"run_$(fn_name "${g}")"')
    assert platform < tenant < groups
    assert private < groups


# ---------------------------------------------------------------------------
# GitHub: without a token the suite says it could not look, never FAILs or
# PASSes a read it could not make
# ---------------------------------------------------------------------------


def test_github_reads_of_the_private_sandbox_need_a_token():
    result = _lib('acc_github_can_read && echo YES || echo NO')
    assert result.stdout.strip() == "NO"
    token = "t" + "k" * 12
    result = _lib('acc_github_can_read && echo YES || echo NO', SWARM_ACCEPTANCE_GITHUB_TOKEN=token)
    assert result.stdout.strip() == "YES"


@pytest.mark.parametrize("group", ["claude-code.sh", "workflow.sh"])
def test_every_pull_request_read_back_is_gated_on_a_token(group):
    text = (ACCEPTANCE / "groups" / group).read_text()
    reads = [m.start() for m in re.finditer(r"acc_github GET|acc_pr_files_vs_ref \"|acc_github_raw ", text)]
    assert reads, f"{group} reads nothing back from GitHub"
    gates = [m.start() for m in re.finditer(r"acc_github_can_read", text)]
    assert gates, f"{group} reads GitHub without asking whether it can"
    # Every read lies in a function that asked first.
    for at in reads:
        fn_start = text.rfind("\n_", 0, at)
        assert any(fn_start < g < at for g in gates), f"{group}: a GitHub read at offset {at} is not gated"


def test_no_acceptance_group_fetches_from_raw_githubusercontent():
    # raw.githubusercontent.com serves a private repository's files to nobody
    # without a token; the contents API with the token does.
    for path in (ACCEPTANCE / "groups").glob("*.sh"):
        code = "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))
        assert "raw.githubusercontent.com" not in code, path.name


# ---------------------------------------------------------------------------
# The release's wiring
# ---------------------------------------------------------------------------


def _job() -> dict:
    return yaml.safe_load(RELEASE.read_text())["jobs"]["acceptance"]


def _steps_running(script: str) -> list[dict]:
    return [s for s in _job()["steps"] if script in str(s.get("run", ""))]


def test_the_release_sweeps_the_sandbox_with_the_sandbox_token_not_the_github_token():
    (sweep,) = _steps_running("github-cleanup.sh")
    env = sweep.get("env", {})
    assert "secrets.SWARM_SANDBOX_GITHUB_TOKEN" in str(env.get("SWARM_ACCEPTANCE_GITHUB_TOKEN", ""))
    assert "github.token" not in str(sweep), "the job's own token reaches only the public repository"


def test_the_release_syncs_the_fixtures_into_the_sandbox_before_the_suite():
    steps = _job()["steps"]
    sync = next(i for i, s in enumerate(steps) if "sandbox-sync.sh" in str(s.get("run", "")))
    suite = next(i for i, s in enumerate(steps) if "verify-remote.sh" in str(s.get("run", "")))
    assert sync < suite
    env = steps[sync].get("env", {})
    assert "secrets.SWARM_SANDBOX_GITHUB_TOKEN" in str(env.get("SWARM_ACCEPTANCE_GITHUB_TOKEN", ""))


def test_the_acceptance_job_cannot_write_to_the_public_repository():
    perms = _job().get("permissions", {})
    assert perms.get("contents") in (None, "read"), perms
    assert perms.get("pull-requests") in (None, "read", "none"), perms
    assert perms.get("id-token") == "write", "the suite still runs through verify-remote.sh"


def test_the_sandbox_sync_script_is_wired_as_an_entry_script():
    sync = ACCEPTANCE / "sandbox-sync.sh"
    assert sync.is_file()
    assert os.access(sync, os.X_OK)
    text = sync.read_text()
    assert "config.sh" in text, "the sync must read its target from the acceptance config"


# ---------------------------------------------------------------------------
# dev.tfvars: how swarm-verify reaches `smoke`
# ---------------------------------------------------------------------------


def _tenants() -> dict[str, dict[str, str]]:
    text = _strip_comments(DEV_TFVARS.read_text())
    return {name: _entries(body) for name, body in _entries(_block(text, "tenants")).items()}


def _unquote(value: str) -> str:
    return value.strip().strip('"')


def test_the_smoke_tenant_is_a_directory_group_whose_principal_slugs_to_smoke():
    smoke = _tenants()["smoke"]
    assert _unquote(smoke["kind"]) == "group"
    # X-Swarm-Tenant can only select a tenant in tenant_choices, which only
    # registered directory groups the caller is a confirmed member of fill.
    assert smoke["directory_group"].strip() == "true"
    # The header selects by the id swarm_common.identity derives from the
    # principal. `swarm-smoke@saga.xyz` derived `swarm-smoke`, which is not
    # this tenant.
    assert tenant_id_for_group(_unquote(smoke["principal"])) == "smoke"


def test_the_smoke_tenant_holds_the_provider_the_claude_code_checks_need():
    # Terraform makes a tenant's claude-code Cloud Run Job only for a declared
    # provider (terraform/infra/locals.tf job_matrix): without it the
    # direct-pr check that opens the sandbox's pull request has nowhere to run.
    providers = re.findall(r'"([^"]+)"', _tenants()["smoke"].get("providers", "[]"))
    assert "anthropic" in providers


def test_swarm_verify_is_not_a_listed_service_account_anywhere():
    # A listed account is continuation-scoped (swarm_api.auth
    # CONTINUATION_ROUTES): it could not POST /v1/tasks at all, and every suite
    # swarm-verify runs would stop.
    for name, cfg in _tenants().items():
        assert SWARM_VERIFY not in cfg.get("service_accounts", ""), name


# ---------------------------------------------------------------------------
# The read-back the suite cannot make: github-verify.sh on the GitHub runner
# ---------------------------------------------------------------------------
#
# The suite runs in swarm-verify, which holds no GitHub token, so against the
# private sandbox it SKIPs every pull-request read-back. Without the step
# below, a release went green with the direct-pr title/body/diff and the
# integrate merged-branch list never checked (review of #628). These pin that
# the release makes them with the sandbox token before the sweep closes the
# pull requests, and that the script fails on each defect it exists to catch.

VERIFY = ACCEPTANCE / "github-verify.sh"
CALC = "tests/acceptance/fixtures/claude-code/calc.py"
BUG_PATCH = "@@ -12,4 +12,4 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
FAKE_GH_API = "http://gh.invalid"

#: A curl that answers GitHub's API from files: the URL's path, every
#: non-alphanumeric character turned to `_`, names the file under FAKE_DIR.
#: Anything else is a 404. It reads the token header from stdin and records
#: whether one arrived, never what it was.
FAKE_GH_CURL = r"""#!/usr/bin/env bash
out=""; url=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -K|-m|-H|-w|-X|--data-binary) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
grep -q 'Authorization: Bearer' && echo yes >>"${FAKE_DIR}/auth" || echo no >>"${FAKE_DIR}/auth"
key="$(printf '%s' "${url#*://*/}" | sed 's#[^A-Za-z0-9]#_#g')"
if [[ -f "${FAKE_DIR}/gh/${key}" ]]; then cp "${FAKE_DIR}/gh/${key}" "${out}"; printf 200
else printf '{"message":"Not Found"}' >"${out}"; printf 404; fi
"""


def _gh_key(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", path.lstrip("/"))


def _pr(number: int, task: str, *, title: str = "add() returns the sum of its arguments",
        body: str | None = None, created: str = "2026-10-05T12:00:00Z", fork: bool = False) -> dict:
    repo = {"full_name": SANDBOX}
    return {
        "number": number,
        "title": title,
        "body": f"Opened by swarm task {task}.\n" if body is None else body,
        "created_at": created,
        "head": {"ref": f"swarm/{task}", "repo": {"full_name": "someone/fork"} if fork else repo},
        "base": {"repo": repo},
    }


def _calc_file(patch: str = BUG_PATCH) -> dict:
    return {"filename": CALC, "patch": patch}


def _verify(tmp_path: Path, pulls: list[dict], files: dict[int, list[dict]], *args: str,
            token: bool = True) -> subprocess.CompletedProcess:
    import json

    gh = tmp_path / "gh"
    gh.mkdir()
    repo_path = f"repos/{SANDBOX}"
    (gh / _gh_key(f"{repo_path}/pulls?state=open&per_page=100")).write_text(json.dumps(pulls))
    for number, listed in files.items():
        (gh / _gh_key(f"{repo_path}/pulls/{number}/files?per_page=100")).write_text(json.dumps(listed))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "curl"
    fake.write_text(FAKE_GH_CURL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    env = _clean_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        HOME=str(tmp_path),
        TMPDIR=str(tmp_path),
        FAKE_DIR=str(tmp_path),
        SWARM_ENV_FILE=str(tmp_path / "no.env"),
        SWARM_ACCEPTANCE_GITHUB_API=FAKE_GH_API,
    )
    if token:
        env["SWARM_ACCEPTANCE_GITHUB_TOKEN"] = "t" + "k" * 12
    return subprocess.run(
        ["bash", str(VERIFY), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _integrate_body(task: str, merged: list[str], extra: str = "") -> str:
    lines = [f"Opened by swarm task {task}.", "", f"Integrates {len(merged)} contributor branch(es):"]
    lines += [f"- merged: `{b}`" for b in merged]
    return "\n".join(lines) + "\n" + extra


def _good_pulls() -> tuple[list[dict], dict[int, list[dict]]]:
    direct, fix, impl = "task_" + "a1" * 8, "task_" + "b2" * 8, "task_" + "c3" * 8
    pulls = [
        _pr(7, direct),
        _pr(8, fix, title="Integrate the add() fix", body=_integrate_body(fix, [f"swarm/{impl}"])),
    ]
    return pulls, {7: [_calc_file()], 8: [_calc_file()]}


def test_verify_passes_the_pull_requests_the_platform_should_open(tmp_path):
    pulls, files = _good_pulls()
    result = _verify(tmp_path, pulls, files)
    assert result.returncode == 0, result.stderr
    assert "verified 2 pull request(s): 1 direct-pr, 1 integrate" in result.stderr
    # Every read carried the sandbox token: the repository is private.
    assert set((tmp_path / "auth").read_text().split()) == {"yes"}


def test_verify_refuses_to_run_without_the_sandbox_token(tmp_path):
    pulls, files = _good_pulls()
    result = _verify(tmp_path, pulls, files, token=False)
    assert result.returncode != 0
    assert "SWARM_ACCEPTANCE_GITHUB_TOKEN" in result.stderr


def _broken(case: str) -> tuple[list[dict], dict[int, list[dict]]]:
    pulls, files = _good_pulls()
    direct_task = pulls[0]["head"]["ref"].removeprefix("swarm/")
    if case == "title-is-task-id":
        pulls[0]["title"] = f"[swarm] {direct_task}"
    elif case == "empty-title":
        pulls[0]["title"] = "  "
    elif case == "body-lacks-task-id":
        pulls[0]["body"] = "acceptance-run:x\n"
    elif case == "file-outside-fixtures":
        files[7] = [_calc_file(), {"filename": "README.md", "patch": "+x\n"}]
    elif case == "direct-pr-changes-another-fixture":
        files[7] = [_calc_file(), {"filename": "tests/acceptance/fixtures/claude-code/test_calc.py", "patch": "+x\n"}]
    elif case == "bug-line-not-removed":
        files[7] = [_calc_file("@@ -1 +1 @@\n-x\n+y\n")]
    elif case == "integrate-merged-nothing":
        task = pulls[1]["head"]["ref"].removeprefix("swarm/")
        pulls[1]["body"] = _integrate_body(task, [])
    elif case == "integrate-left-a-branch-out":
        task = pulls[1]["head"]["ref"].removeprefix("swarm/")
        pulls[1]["body"] = _integrate_body(task, ["swarm/task_" + "d4" * 8], "- conflicted: `swarm/task_" + "e5" * 8 + "`\n")
    elif case == "files-unreadable":
        del files[8]
    else:
        raise AssertionError(case)
    return pulls, files


@pytest.mark.parametrize(
    "case",
    [
        "title-is-task-id",
        "empty-title",
        "body-lacks-task-id",
        "file-outside-fixtures",
        "direct-pr-changes-another-fixture",
        "bug-line-not-removed",
        "integrate-merged-nothing",
        "integrate-left-a-branch-out",
        "files-unreadable",
    ],
)
def test_verify_fails_the_job_on_each_defect_it_exists_to_catch(tmp_path, case):
    pulls, files = _broken(case)
    result = _verify(tmp_path, pulls, files)
    assert result.returncode != 0, f"{case} passed:\n{result.stderr}"
    assert "assertion(s) failed" in result.stderr


def test_verify_judges_only_this_runs_same_repository_task_pull_requests(tmp_path):
    pulls, files = _good_pulls()
    stale_task = "task_" + "f6" * 8
    stale = _pr(3, stale_task, title=f"[swarm] {stale_task}", created="2026-10-04T09:00:00Z")
    fork = _pr(4, "task_" + "a7" * 8, title="[swarm] fork", fork=True)
    human = dict(_pr(5, "x", title=""), head={"ref": "feature/x", "repo": {"full_name": SANDBOX}})
    result = _verify(tmp_path, [stale, fork, human, *pulls], files, "--since", "2026-10-05T11:00:00Z")
    assert result.returncode == 0, result.stderr
    assert "verified 2 pull request(s)" in result.stderr
    # Without --since the stale one, opened by an earlier unswept run, is judged.
    other = tmp_path / "again"
    other.mkdir()
    result = _verify(other, [stale, *pulls], {**files, 3: [_calc_file()]})
    assert result.returncode != 0


def test_verify_finding_nothing_says_so_and_does_not_fail(tmp_path):
    result = _verify(tmp_path, [], {})
    assert result.returncode == 0, result.stderr
    assert "verified 0 pull request(s)" in result.stderr
    assert "no direct-pr pull request" in result.stderr
    assert "no integrate pull request" in result.stderr


def test_verify_refuses_a_since_that_is_not_utc_iso8601(tmp_path):
    result = _verify(tmp_path, [], {}, "--since", "yesterday")
    assert result.returncode != 0
    assert "--since" in result.stderr


def test_verify_writes_nothing_to_github():
    code = "\n".join(l for l in VERIFY.read_text().splitlines() if not l.lstrip().startswith("#"))
    for verb in ("PATCH", "DELETE", "POST", "PUT", "--data-binary"):
        assert verb not in code, f"github-verify.sh must only read; it mentions {verb}"


def test_the_release_reads_the_pull_requests_back_with_the_sandbox_token_before_the_sweep():
    steps = _job()["steps"]

    def at(script: str) -> int:
        return next(i for i, s in enumerate(steps) if script in str(s.get("run", "")))

    suite, verify, sweep = at("verify-remote.sh"), at("github-verify.sh"), at("github-cleanup.sh")
    assert suite < verify < sweep, "the read-back runs after the suite and before anything is closed"
    step = steps[verify]
    assert "!cancelled()" in str(step.get("if", "")), "a failed suite's pull requests are read back too"
    env = step.get("env", {})
    assert "secrets.SWARM_SANDBOX_GITHUB_TOKEN" in str(env.get("SWARM_ACCEPTANCE_GITHUB_TOKEN", ""))
    assert "github.token" not in str(step)
    # It judges only what this run opened: SINCE comes from a step that ran
    # before the suite started.
    started = next(i for i, s in enumerate(steps) if s.get("id") == "started")
    assert started < suite
    assert "steps.started.outputs.at" in str(env.get("SWARM_ACCEPTANCE_SINCE", ""))
    # The sweep is not skipped when the read-back fails.
    assert "!cancelled()" in str(steps[sweep].get("if", ""))


# ---------------------------------------------------------------------------
# The collect check applies the patch without reading the private sandbox
# ---------------------------------------------------------------------------


def test_the_swarm_verify_image_carries_the_acceptance_fixtures():
    dockerfile = (ROOT / "images" / "swarm-verify" / "Dockerfile").read_text()
    assert "COPY tests/acceptance/fixtures/ /swarm/tests/acceptance/fixtures/" in dockerfile
    build = yaml.safe_load((ROOT / "images" / "swarm-verify" / "cloudbuild.yaml").read_text())
    assemble = next(s for s in build["steps"] if s.get("id") == "assemble-context")
    script = "\n".join(assemble["args"])
    assert "cp -R tests/acceptance/fixtures /workspace/verify-ctx/tests/acceptance/fixtures" in script
    assert "test -f /workspace/verify-ctx/tests/acceptance/fixtures/claude-code/calc.py" in script


def test_collect_applies_the_patch_to_the_builds_own_fixture_when_it_holds_no_token():
    text = (ACCEPTANCE / "groups" / "claude-code.sh").read_text()
    fn = text[text.index("_cc_check_collect() {"):text.index("_cc_check_direct_pr() {")]
    local = fn.index('cp "${REPO_ROOT}/${CC_FIXTURE}" "${original}"')
    gate = fn.index("if acc_github_can_read; then")
    skip = fn.index("acc_github_skip_reason")
    # Token first (reads the sandbox itself), then this build's copy at main,
    # and only then a SKIP.
    assert gate < local < skip
    assert '"${ACC_REF}" == "main"' in fn


def test_the_privacy_probe_names_githubs_rate_limit_rather_than_a_platform_failure(tmp_path):
    stub = """
curl() { printf 403; }
sleep() { :; }
acc_require_private_repository
echo REACHED
"""
    result = _lib(stub)
    assert result.returncode == 9
    assert "REACHED" not in result.stdout
    assert "rate limit" in result.stderr
