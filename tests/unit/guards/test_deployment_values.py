"""The deployment-values guard (docs/design/generic-repository.md sections 1, 3 and 4).

scripts/guards/deployment-values.sh finds what ties the repository to a
company, a person or a live deployment, by shape (rules in the tree) and by
name (the pattern list, kept outside the tree). Section 4.5's tests:

* a planted value of every category is found, by rule id, and the value
  never appears in the output;
* every placeholder in scripts/guards/placeholders.json passes;
* an unset list is exit 2 under CI=true and exit 3 locally;
* a digest mismatch is exit 2;

plus what the brief adds: report mode prints counts per directory only and
exits 0; `--diff` reads added lines only; `--text` reads one file.

Every planted value is built at run time from `secrets`, so this file holds
nothing the guard would find. Offline: a temporary git repository, no cloud.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GUARD = ROOT / "scripts" / "guards" / "deployment-values.sh"
FINDING = re.compile(r"^(?P<path>\S+):(?P<line>\d+) (?P<cat>C\d+) (?P<rule>\S+)$")

_spec = importlib.util.spec_from_file_location("placeholders", ROOT / "tests" / "placeholders.py")
assert _spec and _spec.loader
ph = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ph)
placeholder_deployment = ph.deployment()


@pytest.fixture(scope="module", autouse=True)
def _placeholder_deployment(tmp_path_factory):
    yield from placeholder_deployment.activate(tmp_path_factory)


def _word(prefix: str = "w") -> str:
    return prefix + secrets.token_hex(4)


def _number(digits: int = 12) -> str:
    return str(10 ** (digits - 1) + secrets.randbelow(9 * 10 ** (digits - 1)))


def pattern_list(entries: list[tuple[str, str, str]], *, digest: str | None = None) -> str:
    """The list's text: the `# sha256 <digest of the rest>` line, then the entries."""
    body = "".join(f"{c}\t{k}\t{v}\n" for c, k, v in entries)
    return f"# sha256 {digest or hashlib.sha256(body.encode()).hexdigest()}\n{body}"


def _clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in {"CI", "GITHUB_ACTIONS", "DEPLOYMENT_PATTERNS", "DEPLOYMENT_PATTERNS_FILE", "GITHUB_REPOSITORY"}}
    env.update(extra)
    return env


def run(cwd: Path, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(GUARD), *args], cwd=cwd, env=_clean_env(**env),
                          capture_output=True, text=True, timeout=120)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", f"user.email={ph.OWNER}", *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


def make_tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "tree")
    return root


def findings(out: str) -> list[tuple[str, int, str, str]]:
    found = []
    for line in out.splitlines():
        m = FINDING.match(line)
        if m:
            found.append((m["path"], int(m["line"]), m["cat"], m["rule"]))
    return found


# ---------------------------------------------------------------------------
# A planted value of every category
# ---------------------------------------------------------------------------


@pytest.fixture
def planted(tmp_path: Path):
    project, other = _word("proj-"), _word("proj-")
    number, client_number = _number(12), _number(12)
    host, mail_domain = _word("h") + ".com", _word("d") + ".com"
    tailnet = _word("tail") + ".ts.net"
    company, peer_cluster = _word("Co"), _word("cl")
    tenant, bucket = _word("t"), _word("bkt-")
    app_id = _number(6)
    values = {
        "C1": [project],
        "C2": [number],
        "C3": [other],
        "C4": [host, tailnet],
        "C5": [mail_domain],
        "C6": [client_number, app_id],
        "C7": [bucket, tenant],
        "C8": [peer_cluster],
        "C9": [company],
    }
    lines = [
        f"gcloud run deploy api --project={project}",  # 1 C1
        f"name = projects/{number}/secrets/x",  # 2 C2
        f"runner@{other}.iam.gserviceaccount.com",  # 3 C3
        f"see https://{host}/page",  # 4 C4
        f"mail someone@{mail_domain}",  # 5 C5
        f"client {client_number}-{secrets.token_hex(16)}.apps.googleusercontent.com",  # 6 C6
        f"cp x gs://{bucket}/y",  # 7 C7
        f"never touch {peer_cluster}",  # 8 C8
        f"built by {company.upper()} for us",  # 9 C9
        f"reach {tailnet}",  # 10 C4
        f'app_id = "{app_id}"',  # 11 C6
        f"secret swarm-tenant-{tenant}-git",  # 12 C7
        f"old ref part of #{_number(4)}",  # 13 C11 (only with --archive-refs)
    ]
    entries = [("C8", "literal", peer_cluster), ("C9", "literal", company)]
    tree = make_tree(tmp_path / "repo", {
        "docs/notes.md": "\n".join(lines) + "\n",
        "terraform/environments/prod/prod.tfvars": "x = 1\n",
        "terraform/environments/prod/prod.tfvars.example": "x = 1\n",
        "tests/terraform/fixture.tfvars": "x = 1\n",
        ".env": "A=1\n",
    })
    return tree, pattern_list(entries), values


def test_a_planted_value_of_every_category_is_found_by_rule_without_its_value(planted):
    tree, listed, values = planted
    result = run(tree, "--tree", "--mode", "enforce", "--archive-refs", DEPLOYMENT_PATTERNS=listed)
    out = result.stdout + result.stderr
    assert result.returncode == 1, out
    got = findings(result.stdout)
    by_line = {(p, n): c for p, n, c, _ in got}
    expected = {1: "C1", 2: "C2", 3: "C3", 4: "C4", 5: "C5", 6: "C6", 7: "C7", 8: "C8", 9: "C9",
                10: "C4", 11: "C6", 12: "C7", 13: "C11"}
    for line, cat in expected.items():
        assert by_line.get(("docs/notes.md", line)) == cat, (line, cat, got)
    # C10: the tracked deployment files, and not their examples or the Terraform test fixtures.
    tracked = {p for p, _, c, _ in got if c == "C10"}
    assert tracked == {"terraform/environments/prod/prod.tfvars", ".env"}
    # A name entry's rule id is its line in the list (the digest is line 1), never its value.
    rules = {n: r for p, n, _, r in got if p == "docs/notes.md"}
    assert rules[8] == "list:2" and rules[9] == "list:3"
    # MUTATION: print the matched text and this lists it.
    leaked = [v for vs in values.values() for v in vs if v.lower() in out.lower()]
    assert leaked == []


def test_old_references_are_found_only_when_asked(planted):
    tree, listed, _ = planted
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=listed)
    assert "C11" not in {c for _, _, c, _ in findings(result.stdout)}


def test_a_literal_matches_any_separator_and_case_but_only_as_a_word(tmp_path: Path):
    a, b = _word("a"), _word("b")
    tree = make_tree(tmp_path / "repo", {"x.txt": "\n".join([
        f"{a}-{b}", f"{a}_{b}".upper(), f"{a}.{b}", f"x{a}-{b}", f"{a}-{b}y", "nothing"]) + "\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([("C9", "literal", f"{a}-{b}")]))
    assert sorted(n for _, n, _, _ in findings(result.stdout)) == [1, 2, 3]


def test_a_regex_entry_is_found(tmp_path: Path):
    stem = _word("r")
    tree = make_tree(tmp_path / "repo", {"x.txt": f"ok\n{stem}-42\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([("C9", "regex", stem + r"-\d+")]))
    assert findings(result.stdout) == [("x.txt", 2, "C9", "list:2")]


# ---------------------------------------------------------------------------
# Every placeholder passes
# ---------------------------------------------------------------------------


def _placeholder_lines() -> list[str]:
    return [
        f"gcloud run deploy --project={ph.PROJECT} && gcloud x --project {ph.SHARED_PROJECT}",
        f"projects/{ph.PROJECT}/secrets/x projects/{ph.PROJECT_NUMBER}/y projects/{ph.SHARED_PROJECT_NUMBER}",
        f"{ph.PROJECT_NUMBER}-compute@developer.gserviceaccount.com service-{ph.PROJECT_NUMBER}@gcp-sa-iap.iam.gserviceaccount.com",
        f"{ph.service_account('swarm-api')} {ph.service_account('swarm-ci-build')} {ph.PEER['service_account']}",
        f"{ph.PROJECT}.svc.id.goog[ns/sa]",
        f"https://{ph.CONSOLE_HOST}/x https://{ph.API_HOST}/v1 https://{ph.ORG_DOMAIN} http://{ph.OUTSIDE_DOMAIN}",
        f"{ph.OWNER} {ph.ADMIN} {ph.MEMBER} {ph.OUTSIDER} {ph.ADMINS_GROUP} {ph.TEAM_GROUP}",
        f"client {ph.OAUTH_CLIENT_ID}",
        f'app_id = "{ph.GITHUB_APP_ID}" installation_id: {ph.GITHUB_INSTALLATION_ID} repository {ph.GITHUB_REPOSITORY}',
        f"gs://{ph.STATE_BUCKET}/config gs://{ph.ARTIFACT_BUCKET}/x gs://{ph.PEER['bucket']}",
        f"{ph.TENANT_GIT_SECRET} swarm-tenant-{ph.TEAM_TENANT}-anthropic",
        f"{ph.PEER['cluster']} {ph.PEER['network']} {ph.PEER['subnet']} {ph.PRODUCT}",
        f"https://{ph.TAILNET}/x {ph.TAILNET} {ph.INSTALLER_CIDR} {ph.REGION} {ph.ZONE}",
        "https://github.com/x/y https://storage.googleapis.com/b/o https://pypi.org/simple git@github.com:x/y",
        "gs://swarm-tfstate-${PROJECT_ID}/x projects/${PROJECT_ID} --project=\"$P\" --project <id>",
    ]


def test_every_placeholder_passes(tmp_path: Path):
    tree = make_tree(tmp_path / "repo", {
        "docs/placeholders.md": "\n".join(_placeholder_lines()) + "\n",
        "terraform/environments/dev/dev.tfvars.example": "x = 1\n",
    })
    listed = pattern_list([("C9", "literal", _word("Co"))])
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=listed)
    assert (result.returncode, findings(result.stdout)) == (0, []), result.stdout + result.stderr


def test_the_placeholder_file_itself_passes(tmp_path: Path):
    text = (ROOT / "scripts" / "guards" / "placeholders.json").read_text(encoding="utf-8")
    tree = make_tree(tmp_path / "repo", {"scripts/guards/placeholders.json": text})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert (result.returncode, findings(result.stdout)) == (0, []), result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Failing closed (section 4.2)
# ---------------------------------------------------------------------------


@pytest.fixture
def small(tmp_path: Path) -> Path:
    return make_tree(tmp_path / "repo", {"a/x.md": f"mail someone@{_word('d')}.com\n"})


def test_an_unset_list_is_exit_2_under_ci(small: Path):
    result = run(small, "--tree", CI="true")
    assert result.returncode == 2
    assert "pattern list unavailable" in result.stderr
    # The shape layer still ran and reported.
    assert "a 1" in result.stdout


def test_an_empty_list_variable_is_exit_2_under_ci(small: Path):
    assert run(small, "--tree", CI="true", DEPLOYMENT_PATTERNS="").returncode == 2


def test_an_unset_list_is_exit_3_locally(small: Path):
    result = run(small, "--tree")
    assert result.returncode == 3
    assert "skipped: no pattern list" in result.stderr


def test_a_digest_mismatch_is_exit_2(small: Path):
    listed = pattern_list([("C9", "literal", _word())], digest=hashlib.sha256(b"other").hexdigest())
    result = run(small, "--tree", DEPLOYMENT_PATTERNS=listed)
    assert result.returncode == 2
    assert "digest" in result.stderr


def test_a_list_without_its_digest_line_is_exit_2(small: Path):
    result = run(small, "--tree", DEPLOYMENT_PATTERNS=f"C9\tliteral\t{_word()}\n")
    assert result.returncode == 2


@pytest.mark.parametrize("line", ["C9\tliteral", "C99\tliteral\tx", "C9\tglob\tx", "C9\tregex\t(", "C9\tliteral\t"])
def test_a_malformed_entry_is_exit_2_and_names_its_line_not_its_value(small: Path, line: str):
    body = f"{line}\n"
    listed = f"# sha256 {hashlib.sha256(body.encode()).hexdigest()}\n{body}"
    result = run(small, "--tree", DEPLOYMENT_PATTERNS=listed)
    assert result.returncode == 2
    assert "line 2" in result.stderr


def test_the_digest_is_printed_on_every_run(small: Path):
    listed = pattern_list([("C9", "literal", _word())])
    digest = listed.splitlines()[0].split()[2]
    assert digest in run(small, "--tree", DEPLOYMENT_PATTERNS=listed).stderr


def test_a_list_file_is_read_like_the_variable(small: Path, tmp_path: Path):
    company = _word("Co")
    (small / "b.md").write_text(f"by {company}\n", encoding="utf-8")
    git(small, "add", "b.md")
    git(small, "commit", "-q", "-m", "b")
    listed = tmp_path / "list.tsv"
    listed.write_text(pattern_list([("C9", "literal", company)]), encoding="utf-8")
    result = run(small, "--tree", "--mode", "enforce", "--patterns-file", str(listed))
    assert ("b.md", 1, "C9", "list:2") in findings(result.stdout)


def test_an_unreadable_secret_fails_and_names_the_slot(small: Path, tmp_path: Path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "gcloud").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    (fake / "gcloud").chmod(0o755)
    result = run(small, "--tree", "--patterns-secret", PATH=f"{fake}:{os.environ['PATH']}",
                 PROJECT_ID=ph.PROJECT)
    assert result.returncode == 2
    assert "swarm-deployment-patterns" in result.stderr


# ---------------------------------------------------------------------------
# Report mode, --diff and --text
# ---------------------------------------------------------------------------


def test_report_mode_prints_counts_per_directory_only_and_exits_0(planted):
    tree, listed, values = planted
    result = run(tree, "--tree", DEPLOYMENT_PATTERNS=listed)
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert findings(result.stdout) == []
    assert not re.search(r"\S+:\d+ C\d+", out), out
    assert "docs 12" in result.stdout.splitlines()
    assert "total 14" in result.stdout.splitlines()
    assert [v for vs in values.values() for v in vs if v.lower() in out.lower()] == []


def test_diff_reads_only_added_lines(tmp_path: Path):
    old, new = _word("d") + ".com", _word("d") + ".com"
    tree = make_tree(tmp_path / "repo", {"a.md": f"one@{old}\nkeep\n"})
    base = git(tree, "rev-parse", "HEAD").strip()
    (tree / "a.md").write_text(f"one@{old}\nkeep\ntwo@{new}\n", encoding="utf-8")
    (tree / "dev.tfvars").write_text("x = 1\n", encoding="utf-8")
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", "more")
    result = run(tree, "--diff", base, "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert sorted(findings(result.stdout)) == [("a.md", 3, "C5", "email"), ("dev.tfvars", 1, "C10", "tracked-file")]


def test_text_reads_one_file(tmp_path: Path):
    issue = tmp_path / "issue.md"
    issue.write_text(f"hello\nfrom someone@{_word('d')}.com\n", encoding="utf-8")
    result = run(tmp_path, "--text", str(issue), "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [(str(issue), 2, "C5", "email")]


def test_summary_mode_prints_only_the_verdict_and_the_count(planted):
    tree, listed, _ = planted
    result = run(tree, "--tree", "--mode", "summary", DEPLOYMENT_PATTERNS=listed)
    assert result.returncode == 1
    assert result.stdout.strip().splitlines() == ["fail: 14 findings"]


def test_the_repository_address_is_allowed_only_where_it_must_appear(tmp_path: Path):
    owner = _word("o")
    repo = f"{owner}/{_word('r')}"
    tree = make_tree(tmp_path / "repo", {
        "README.md": f"install from github.com/{repo}\n",
        "docs/x.md": f"install from github.com/{repo}\n",
    })
    listed = pattern_list([("C5", "literal", owner)])
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=listed, GITHUB_REPOSITORY=repo)
    assert findings(result.stdout) == [("docs/x.md", 1, "C5", "list:2")]


def test_the_default_mode_is_report():
    # Lane 12 flips this once --tree reports zero (section 4.4).
    text = (ROOT / "scripts" / "guards" / "deployment_values.py").read_text(encoding="utf-8")
    assert re.search(r'^DEFAULT_MODE = "report"$', text, re.M)


# ---------------------------------------------------------------------------
# The helpers: candidates.sh and sync-patterns.sh
# ---------------------------------------------------------------------------


def test_candidates_go_to_tmpdir_and_never_to_the_output(tmp_path: Path):
    domain, host = _word("d") + ".com", _word("h") + ".io"
    proper = "Zq" + secrets.token_hex(3)
    prose = " ".join(f"{proper} said so." for _ in range(7))
    tree = make_tree(tmp_path / "repo", {"docs/a.md": f"x@{domain} https://{host}/p\n{prose}\n"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = subprocess.run(["bash", str(ROOT / "scripts" / "guards" / "candidates.sh")], cwd=tree,
                            env=_clean_env(TMPDIR=str(scratch)), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    assert domain not in out and host not in out and proper not in out
    written = [p for p in scratch.rglob("*") if p.is_file()]
    assert len(written) == 1
    text = written[0].read_text(encoding="utf-8")
    assert domain in text and host in text and proper in text
    assert oct(written[0].stat().st_mode & 0o777) == "0o600"
    assert not [p for p in tree.rglob("*") if p.is_file() and ".git" not in p.parts and p.name != "a.md"]


def test_candidates_refuse_a_tmpdir_inside_the_tree(tmp_path: Path):
    tree = make_tree(tmp_path / "repo", {"a.md": "x\n"})
    (tree / "tmp").mkdir()
    result = subprocess.run(["bash", str(ROOT / "scripts" / "guards" / "candidates.sh")], cwd=tree,
                            env=_clean_env(TMPDIR=str(tree / "tmp")), capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert list((tree / "tmp").iterdir()) == []


def _fake_tools(bindir: Path, record: Path, source: str) -> None:
    """gcloud and gh stand-ins: they record their arguments and what they were
    given to store, and `gcloud storage cat` serves the source object."""
    bindir.mkdir()
    (bindir / "gcloud").write_text(f"""#!/usr/bin/env bash
set -euo pipefail
printf 'gcloud %s\\n' "$*" >>"{record}/calls"
case "$*" in
  "storage cat "*) cat "{source}" ;;
  "secrets describe "*) exit 0 ;;
  "secrets versions add "*)
    for a in "$@"; do case "$a" in --data-file=*) cp "${{a#--data-file=}}" "{record}/sm" ;; esac; done ;;
  *) exit 0 ;;
esac
""", encoding="utf-8")
    (bindir / "gh").write_text(f"""#!/usr/bin/env bash
set -euo pipefail
printf 'gh %s\\n' "$*" >>"{record}/calls"
cat >"{record}/gh"
""", encoding="utf-8")
    for tool in ("gcloud", "gh"):
        (bindir / tool).chmod(0o755)


def _sync(tmp_path: Path, source_text: str) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    source = tmp_path / "source.tsv"
    source.write_text(source_text, encoding="utf-8")
    record, scratch, home = tmp_path / "record", tmp_path / "scratch", tmp_path / "home"
    for d in (record, scratch, home):
        d.mkdir()
    _fake_tools(tmp_path / "bin", record, str(source))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "guards" / "sync-patterns.sh")], cwd=ROOT,
        env=_clean_env(PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", HOME=str(home), PROJECT_ID=ph.PROJECT,
                       GITHUB_REPOSITORY=ph.GITHUB_REPOSITORY, TMPDIR=str(scratch),
                       SWARM_ENV_FILE=str(tmp_path / "no.env"), TF_STATE_BUCKET=ph.STATE_BUCKET),
        capture_output=True, text=True, timeout=120)
    return result, record, scratch


def test_sync_writes_both_stores_from_the_source_with_the_digest_line(tmp_path: Path):
    company = _word("Co")
    body = f"# the company\nC9\tliteral\t{company}\n"
    result, record, scratch = _sync(tmp_path, body)
    assert result.returncode == 0, result.stderr
    expected = f"# sha256 {hashlib.sha256(body.encode()).hexdigest()}\n{body}"
    assert (record / "gh").read_text(encoding="utf-8") == expected
    assert (record / "sm").read_text(encoding="utf-8") == expected
    calls = (record / "calls").read_text(encoding="utf-8")
    assert f"storage cat gs://{ph.STATE_BUCKET}/config/deployment-patterns.tsv" in calls
    assert f"secret set DEPLOYMENT_PATTERNS --repo {ph.GITHUB_REPOSITORY}" in calls
    assert f"secrets versions add swarm-deployment-patterns --project={ph.PROJECT}" in calls
    assert company not in result.stdout + result.stderr
    assert hashlib.sha256(body.encode()).hexdigest() in result.stderr
    # Nothing is left behind in TMPDIR.
    assert list(scratch.iterdir()) == []


def test_sync_restamps_a_source_that_already_carries_a_digest_line(tmp_path: Path):
    body = f"C9\tliteral\t{_word('Co')}\n"
    result, record, _ = _sync(tmp_path, f"# sha256 {'0' * 64}\n{body}")
    assert result.returncode == 0, result.stderr
    assert (record / "gh").read_text(encoding="utf-8") == f"# sha256 {hashlib.sha256(body.encode()).hexdigest()}\n{body}"


def test_sync_refuses_a_malformed_source_and_writes_nothing(tmp_path: Path):
    result, record, scratch = _sync(tmp_path, "C9\tglob\tx\n")
    assert result.returncode != 0
    assert not (record / "gh").exists() and not (record / "sm").exists()
    assert list(scratch.iterdir()) == []


# ---------------------------------------------------------------------------
# The owner's rulings of 7 November 2026 (part of #2688): public hosts, generic
# workspace slugs, the email rule's false positives and LICENSE line 3 are not
# deployment values. Every allowed value is read from placeholders.json, so this
# file restates none of them; every listed value is built at run time.
# ---------------------------------------------------------------------------

ALLOWLIST = ph.ALL["allowlist"]


def _listed_neighbour(tmp_path: Path, allowed_lines: list[str]) -> tuple[list[tuple[str, int, str, str]], str]:
    """Each allowed line followed by a line that carries a listed value; the findings."""
    listed_value = _word("Co")
    lines = [part for line in allowed_lines for part in (line, f"{line} {listed_value}")]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": "\n".join(lines) + "\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([("C9", "literal", listed_value)]))
    return findings(result.stdout), result.stdout + result.stderr


def _only_the_listed_lines_are_reported(found: list[tuple[str, int, str, str]], count: int) -> None:
    assert found == [("docs/x.md", 2 * i + 2, "C9", "list:2") for i in range(count)], found


def test_every_public_product_host_is_not_reported_but_a_listed_value_beside_it_is(tmp_path: Path):
    hosts = [entry["host"] for entry in ALLOWLIST["public_product_hosts"]]
    assert hosts and all(entry["why"] for entry in ALLOWLIST["public_product_hosts"])
    lines = [f"call https://{host}/v1 and https://sub.{host}/x" for host in hosts]
    found, out = _listed_neighbour(tmp_path, lines)
    _only_the_listed_lines_are_reported(found, len(lines))


def test_a_host_that_only_ends_like_a_product_host_is_still_reported(tmp_path: Path):
    host = ALLOWLIST["public_product_hosts"][0]["host"]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": f"https://{_word('x')}{host}/v1\nhttps://{host}.{_word('e')}.com/\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("docs/x.md", 1, "C4", "url-host"), ("docs/x.md", 2, "C4", "url-host")]


def test_every_public_service_address_is_not_reported(tmp_path: Path):
    addresses = [entry["address"] for entry in ALLOWLIST["public_service_addresses"]]
    assert addresses and all(entry["why"] for entry in ALLOWLIST["public_service_addresses"])
    found, _ = _listed_neighbour(tmp_path, [f"grant {a} write" for a in addresses])
    _only_the_listed_lines_are_reported(found, len(addresses))


def test_another_address_at_a_public_service_domain_is_still_reported(tmp_path: Path):
    domain = ALLOWLIST["public_service_addresses"][0]["address"].split("@", 1)[1]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": f"mail {_word('p')}@{domain}\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("docs/x.md", 1, "C5", "email")]


def test_a_public_documentation_path_is_not_a_project_but_the_same_word_elsewhere_is(tmp_path: Path):
    paths = [entry["path"] for entry in ALLOWLIST["public_url_paths"]]
    assert paths and all(entry["why"] for entry in ALLOWLIST["public_url_paths"])
    word = "a" + _word("p")
    lines = [f"see https://{p}{word}/x" for p in paths] + [f"see projects/{word}/x"]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": "\n".join(lines) + "\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("docs/x.md", len(lines), "C1", "project-path")]


def test_cluster_dns_names_and_the_cloud_run_url_shape_are_not_reported(tmp_path: Path):
    lines = [
        f"http://{_word('s')}.{_word('ns')}.svc:8080/x http://kubernetes.default.svc/api",
        f"https://{_word('svc-')}-abcdef-uc.a.run.app https://{_word('svc-')}-xxxxxxxxxx-uc.a.run.app",
        f"https://{_word('svc-')}.example.run.app https://{_word('svc-')}-{ph.PROJECT_NUMBER}.{ph.REGION}.run.app",
        "http://localhost.localdomain:8080/",
    ]
    found, _ = _listed_neighbour(tmp_path, lines)
    _only_the_listed_lines_are_reported(found, len(lines))


def test_a_real_shaped_cloud_run_hash_or_project_number_is_still_reported(tmp_path: Path):
    real_hash = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(9)) + "q"
    tree = make_tree(tmp_path / "repo", {"docs/x.md": "\n".join([
        f"https://{_word('svc-')}-{real_hash}-uc.a.run.app/x",
        f"https://{_word('svc-')}-{_number(12)}.{ph.REGION}.run.app/x",
        f"http://{_word('s')}.svc.{_word('h')}.com/x",
    ]) + "\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("docs/x.md", n, "C4", "url-host") for n in (1, 2, 3)]


def test_every_generic_workspace_slug_is_not_reported_but_a_listed_value_beside_it_is(tmp_path: Path):
    slugs = [entry["slug"] for entry in ALLOWLIST["tenant_slugs"]]
    assert slugs and all(entry["why"] for entry in ALLOWLIST["tenant_slugs"])
    found, _ = _listed_neighbour(tmp_path, [f"secret swarm-tenant-{s}-git swarm-tenant-{s}-anthropic" for s in slugs])
    _only_the_listed_lines_are_reported(found, len(slugs))


def test_the_two_placeholder_tenants_are_unchanged_and_another_slug_is_still_reported(tmp_path: Path):
    assert ph.ALL["tenants"] == ["example-tenant", "team-a"]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": f"secret swarm-tenant-{_word('t')}-git\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("docs/x.md", 1, "C7", "tenant-secret")]


def test_a_generic_slug_on_the_private_pattern_list_is_not_allowed(tmp_path: Path):
    slug = ALLOWLIST["tenant_slugs"][0]["slug"]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": f"secret swarm-tenant-{slug}-git\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([("C7", "literal", slug)]))
    assert ("docs/x.md", 1, "C7", "tenant-secret") in findings(result.stdout)


def test_a_decorator_or_a_git_config_key_is_not_an_address_but_an_address_after_an_escape_is(tmp_path: Path):
    domain = _word("d") + ".com"
    tree = make_tree(tmp_path / "repo", {"tests/x.py": "\n".join([
        'BODY = "```python\\n@pytest.fixture\\ndef widget():\\n```\\n"',
        'BODY = "\\n\\n@app.route(\\"/ready\\")\\n"',
        'KEYS = ("credential.https://github.com.helper", "http.https://github.com.proxy")',
        f'BODY = "hello\\n{_word("p")}@{domain}"',
    ]) + "\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("tests/x.py", 4, "C5", "email")]


def test_license_line_3_is_exempt_and_line_4_is_not(tmp_path: Path):
    exempt = ALLOWLIST["exempt_lines"]
    assert [(e["path"], e["line"]) for e in exempt] == [("LICENSE", 3)] and exempt[0]["why"]
    holder = _word("Co")
    tree = make_tree(tmp_path / "repo", {
        "LICENSE": f"MIT License\n\nCopyright (c) 2026 {holder} {_word('p')}@{_word('d')}.com\n{holder}\n",
        "docs/x.md": f"x\n\n{holder}\n",
    })
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([("C9", "literal", holder)]))
    assert findings(result.stdout) == [("LICENSE", 4, "C9", "list:2"), ("docs/x.md", 3, "C9", "list:2")]


# ---------------------------------------------------------------------------
# The owner's rulings of 8 November 2026 (part of #2688): GitHub Actions' public
# App id and the release home as a product address. (The third, the owner's
# code-owner handle in .github/CODEOWNERS, is moot since main's CODEOWNERS
# names a placeholder team.) As above, every allowed value is read from
# placeholders.json and every listed value is built at run time.
# ---------------------------------------------------------------------------

_guard_spec = importlib.util.spec_from_file_location("deployment_values", ROOT / "scripts" / "guards" / "deployment_values.py")
assert _guard_spec and _guard_spec.loader
dv = importlib.util.module_from_spec(_guard_spec)
sys.modules["deployment_values"] = dv
_guard_spec.loader.exec_module(dv)


def test_the_actions_app_id_and_the_release_home_are_not_reported_but_a_listed_value_beside_them_is(tmp_path: Path):
    app_ids = [entry["id"] for entry in ALLOWLIST["public_app_ids"]]
    home = ALLOWLIST["release_home"]
    buckets = [entry["bucket"] for entry in home["buckets"]]
    projects = [entry["project"] for entry in home["projects"]]
    assert app_ids and buckets and projects and home["_why"]
    assert all(e["why"] for e in [*ALLOWLIST["public_app_ids"], *home["buckets"], *home["projects"]])
    lines = ([f'{{"context": "{_word("c")}", "app_id": {a}}}' for a in app_ids]
             + [f"gsutil ls gs://{b}/releases/V/" for b in buckets]
             + [f"publish --project {p} to projects/{p}/locations/us" for p in projects])
    found, _ = _listed_neighbour(tmp_path, lines)
    _only_the_listed_lines_are_reported(found, len(lines))


def test_another_app_id_bucket_or_project_is_still_reported(tmp_path: Path):
    bucket = ALLOWLIST["release_home"]["buckets"][0]["bucket"]
    project = ALLOWLIST["release_home"]["projects"][0]["project"]
    tree = make_tree(tmp_path / "repo", {"docs/x.md": "\n".join([
        f'"app_id": {_number(6)}',
        f"gs://{bucket}-{_word('b')}/x",
        f"--project {project}-{_word('p')}",
    ]) + "\n"})
    result = run(tree, "--tree", "--mode", "enforce", DEPLOYMENT_PATTERNS=pattern_list([]))
    assert findings(result.stdout) == [("docs/x.md", 1, "C6", "github-app-id"), ("docs/x.md", 2, "C7", "bucket"),
                                       ("docs/x.md", 3, "C1", "project-flag")]
