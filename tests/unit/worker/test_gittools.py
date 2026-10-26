"""The worker's half of the runner git tools (docs/runner-git-tools.md §1.2-§1.4; lane G3).

Against a local bare repository standing in for origin and a fake swarm-api
plugged into the real `ChildApi` as its opener, with no network. The fake
verifies every request's attempt proof with swarm-api's OWN verifier
(`swarm_api.childkey`), applies a push's bundle to the bare origin, and
answers a fetch with a bundle built from it -- the shape lane G4/G5 serve.

What each test holds, from the design's §7 row G3:

* a spool push naming `main`, another task's branch, or a name outside
  `swarm/` is refused before any request;
* a commit adding a credential-shaped line (built at runtime) is `leak_found`
  with `path:line rule` and no match text;
* the bundle holds exactly `<remote tip>..HEAD`;
* a rebase conflict leaves the rebase in progress and lists the paths;
* a request is signed and its proof verifies with `childkey.py`'s verifier;
* a 409 raises the fenced error.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import urllib.error
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from agent_worker import children as children_mod
from agent_worker import gittools
from agent_worker.gitmcp import server as shim
from agent_worker.logs import build_logger
from swarm_api import childkey as api_childkey

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

TASK = "task_" + "a" * 20
OTHER_TASK = "task_" + "b" * 20
OWN_BRANCH = f"swarm/{TASK}"
PERSON = ("Ada Person", "ada@example.invalid")


#: The test's own git, with no identity or config of whoever runs it: the
#: commits name exactly who each test says (`_commit`).
_ENV = {k: v for k, v in os.environ.items()
        if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_CONFIG"))}
_ENV.update({"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"})


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=_ENV
    ).stdout.strip()


def _commit(clone: Path, path: str, text: str, message: str,
            who: tuple[str, str] = PERSON) -> str:
    target = clone / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(clone, "add", "-A")
    _git(clone, "-c", f"user.name={who[0]}", "-c", f"user.email={who[1]}",
         "commit", "-q", "-m", message)
    return _git(clone, "rev-parse", "HEAD")


class FakeApi:
    """swarm-api's git route, enough of it: proof checked, then the tool."""

    def __init__(self, origin: Path, scratch: Path, public_key: str) -> None:
        self.origin = origin
        self.scratch = scratch
        self.public_key = public_key
        self.calls: list[dict[str, Any]] = []
        self.fenced = False

    def open(self, req, timeout=None):
        path = urlparse(req.full_url).path
        body = req.data or b""
        proof = req.get_header(children_mod.PROOF_HEADER.capitalize())
        stamp = req.get_header(children_mod.TIMESTAMP_HEADER.capitalize())
        message = api_childkey.request_message(req.get_method(), path, body, stamp or "")
        verified = bool(proof) and api_childkey.verify_proof(self.public_key, proof, message)
        payload = json.loads(body.decode())
        self.calls.append({"path": path, "body": payload, "raw": body, "proof": proof,
                           "timestamp": stamp, "verified": verified})
        if not verified:
            return self._answer(req, 403, {"code": "forbidden", "message": "no"})
        if self.fenced:
            return self._answer(req, 409, {"code": gittools.GIT_FENCED, "message": "superseded"})
        tool = path.rsplit("/", 1)[1]
        return self._answer(req, 200, getattr(self, tool)(payload))

    def _answer(self, req, status: int, payload: dict[str, Any]):
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
        return _Response(status, raw)

    def git_push_branch(self, body: dict[str, Any]) -> dict[str, Any]:
        bundle = self.scratch / f"{body['request_id']}.bundle"
        bundle.write_bytes(base64.b64decode(body["bundle"]))
        _git(self.origin, "fetch", "-q", str(bundle), "+refs/heads/swarm-publish:refs/swarm/in")
        new = _git(self.origin, "rev-parse", "refs/swarm/in")
        ref = f"refs/heads/{body['branch']}"
        old = subprocess.run(["git", "-C", str(self.origin), "rev-parse", "--verify", "--quiet",
                              ref], capture_output=True, text=True).stdout.strip() or None
        _git(self.origin, "update-ref", ref, new)
        return {"branch": body["branch"], "old_sha": old, "new_sha": new}

    def git_fetch(self, body: dict[str, Any]) -> dict[str, Any]:
        if "confirm" in body:
            # swarm-api's answer to "which of these does origin have" (#2413):
            # the shas that are commits in the bare origin, nothing else.
            return {"confirmed": [sha for sha in body["confirm"]
                                  if subprocess.run(["git", "-C", str(self.origin), "cat-file",
                                                     "-e", f"{sha}^{{commit}}"],
                                                    capture_output=True).returncode == 0]}
        ref = body["ref"]
        sha = _git(self.origin, "rev-parse", f"{ref}^{{commit}}")
        if sha in body["haves"]:
            return {"resolved_sha": sha}
        _git(self.origin, "update-ref", "refs/swarm/out", sha)
        bundle = self.scratch / f"{body['request_id']}-out.bundle"
        _git(self.origin, "bundle", "create", str(bundle), "refs/swarm/out",
             *(f"^{h}" for h in body["haves"]))
        raw = bundle.read_bytes()
        return {"resolved_sha": sha, "bundle": base64.b64encode(raw).decode(),
                "bundle_sha256": hashlib.sha256(raw).hexdigest()}

    def pr_checks(self, body: dict[str, Any]) -> dict[str, Any]:
        return {"pr": body.get("pr"), "head_sha": "0" * 40, "state": "green", "rows": []}


class _Response:
    def __init__(self, status: int, raw: bytes) -> None:
        self.status = status
        self._raw = raw

    def read(self) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class World:
    def __init__(self, tmp_path: Path, *, shallow: bool = False, **overrides: Any) -> None:
        self.origin = tmp_path / "origin.git"
        seed = tmp_path / "seed"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True,
                       env=_ENV)
        subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True, env=_ENV)
        _commit(seed, "README.md", "line one\nline two\n", "base")
        _git(seed, "push", "-q", str(self.origin), "main")
        self.seed = seed
        self.clone = tmp_path / "work" / "repo"
        # The worker's clone is `--depth 1` (`gitops.shallow_clone`); `shallow`
        # stands that in, over file:// so git honours the depth.
        depth = ["--depth", "1"] if shallow else []
        source = f"file://{self.origin}" if shallow else str(self.origin)
        subprocess.run(["git", "clone", "-q", *depth, source, str(self.clone)], check=True,
                       env=_ENV)
        self.base = _git(self.clone, "rev-parse", "HEAD")
        self.work = tmp_path / "work"
        scratch = tmp_path / "api"
        scratch.mkdir()
        self.key = children_mod.AttemptKey()
        self.api = FakeApi(self.origin, scratch, self.key.public)
        client = children_mod.ChildApi("https://api.invalid", token_fetcher=lambda _a: "id",
                                       opener=self.api.open)
        fields: dict[str, Any] = dict(
            tenant_id="acme", task_id=TASK, attempt_id="att_1", lease_id="lease_1",
            generation=3, repository="acme/widgets", clone_base=self.base,
            pushable=(OWN_BRANCH,), author=PERSON, protected=("main",),
        )
        fields.update(overrides)
        self.cfg = gittools.GitToolsConfig(**fields)
        self.tools = gittools.GitTools(
            self.cfg, key=self.key, api=client, clone=self.clone,
            private_dir=tmp_path / "private", logs_dir=_dir(tmp_path / "logs"),
            logger=build_logger(task_id=TASK, attempt_id="att_1", tenant_id="acme",
                                generation=3, runner_profile="mock", stream=io.StringIO()),
        )
        self.tools.prepare(self.work)

    def spool(self, tool: str, arguments: dict[str, Any], request_id: str = "r1") -> dict:
        shim.write_request(gittools.GitTools.spool(self.work), request_id, tool, arguments)
        self.tools.tick(self.work)
        response = gittools.GitTools.spool(self.work) / "responses" / f"{request_id}.json"
        return json.loads(response.read_text())

    def advance_main(self, path: str, text: str, message: str) -> str:
        _git(self.seed, "pull", "-q", str(self.origin), "main")
        sha = _commit(self.seed, path, text, message, who=("Someone Else", "else@example.invalid"))
        _git(self.seed, "push", "-q", str(self.origin), "main")
        return sha


def _dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture(params=[False, True], ids=["full-clone", "shallow-clone"])
def world(request, tmp_path: Path) -> World:
    return World(tmp_path, shallow=request.param)


# --------------------------------------------------------------------------
# Refused before any request (§1.2)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "code"),
    [
        ("main", "protected_branch"),
        (f"swarm/{OTHER_TASK}", "not_this_tasks_branch"),
        ("feature/mine", "not_this_tasks_branch"),
        ("release", "not_this_tasks_branch"),
    ],
)
def test_a_spool_push_to_a_branch_this_task_may_not_push_is_refused_before_any_request(
    world: World, branch: str, code: str
):
    _commit(world.clone, "src/app.py", "x = 1\n", "work")
    answer = world.spool("git_push_branch", {"branch": branch})
    assert answer["refused"]["code"] == code
    assert world.api.calls == []
    assert _git(world.origin, "for-each-ref", "--format=%(refname)") == "refs/heads/main"


def test_main_is_refused_even_when_the_worker_was_not_told_the_default_branch(tmp_path: Path):
    world = World(tmp_path, protected=())
    _commit(world.clone, "src/app.py", "x = 1\n", "work")
    assert world.spool("git_push_branch", {"branch": "main"})["refused"]["code"] == \
        "not_this_tasks_branch"
    assert world.api.calls == []


def test_a_strategy_that_pushes_nothing_is_refused_before_any_request(tmp_path: Path):
    world = World(tmp_path, pushes=False)
    _commit(world.clone, "src/app.py", "x = 1\n", "work")
    assert world.spool("git_push_branch", {})["refused"]["code"] == "strategy_pushes_nothing"
    assert world.api.calls == []


@pytest.mark.parametrize(
    ("tool", "arguments", "code"),
    [
        ("git_fetch", {"ref": "main", "repo": "acme/other"}, "other_repository"),
        ("git_push_branch", {"url": "https://example.invalid/x.git"}, "other_repository"),
        ("git_fetch", {"ref": "refs/heads/main"}, "bad_ref"),
        ("git_fetch", {"ref": "--upload-pack=/bin/sh"}, "bad_ref"),
        ("git_fetch", {"ref": "a..b"}, "bad_ref"),
        ("read_private", {"repo": "acme/lib", "path": "../etc", "ref": "main"}, "bad_path"),
        ("read_private", {"repo": "acme/lib", "path": "/etc/passwd", "ref": "main"}, "bad_path"),
        ("read_private", {"repo": "acme/lib", "path": "a\\b", "ref": "main"}, "bad_path"),
        ("read_private", {"repo": "acme", "path": "x", "ref": "main"}, "bad_ref"),
        ("git_fetch", {"ref": "main", "refspec": "+x:y"}, "request_invalid"),
        ("git_clone", {"url": "x"}, "unknown_tool"),
        ("pr_checks", {"pr": True}, "request_invalid"),
    ],
)
def test_a_malformed_request_is_refused_before_any_request(
    world: World, tool: str, arguments: dict, code: str
):
    assert world.spool(tool, arguments)["refused"]["code"] == code
    assert world.api.calls == []


def test_a_commit_the_task_did_not_write_is_foreign_author(world: World):
    _commit(world.clone, "src/app.py", "x = 1\n", "work",
            who=("Mallory", "mallory@example.invalid"))
    answer = world.spool("git_push_branch", {})
    assert answer["refused"]["code"] == "foreign_author"
    assert world.api.calls == []


# --------------------------------------------------------------------------
# The leak scan
# --------------------------------------------------------------------------


def test_a_commit_adding_a_credential_shaped_line_is_leak_found_with_path_line_rule(
    world: World,
):
    value = "ghp_" + secrets.token_hex(18)  # assembled at runtime, never a literal
    _commit(world.clone, "src/settings.py",
            f"import os\n\nGITHUB = \"{value}\"\nDEBUG = False\n", "settings")
    _commit(world.clone, "src/later.py", "y = 2\n", "a clean commit after it")
    answer = world.spool("git_push_branch", {})
    refused = answer["refused"]
    assert refused["code"] == "leak_found"
    assert len(refused["hits"]) == 1
    where, rule = refused["hits"][0].split(" ")
    assert where == "src/settings.py:3"
    assert rule and rule != "registered_secret"
    dumped = json.dumps(answer)
    assert value not in dumped and value[4:] not in dumped
    assert world.api.calls == []


def test_a_registered_secret_in_a_commit_is_leak_found_too(tmp_path: Path):
    registered = secrets.token_hex(12)
    world = World(tmp_path)
    world.tools._secrets = (registered,)
    _commit(world.clone, "notes.txt", f"first\nsecond {registered}\n", "notes")
    answer = world.spool("git_push_branch", {})
    assert answer["refused"]["code"] == "leak_found"
    assert answer["refused"]["hits"] == ["notes.txt:2 registered_secret"]
    assert registered not in json.dumps(answer)


def test_a_clean_commit_is_not_refused_by_the_scan(world: World):
    """The control: the scan is not what refuses every push."""
    _commit(world.clone, "src/settings.py", "import os\n\nGITHUB = os.environ['X']\n", "clean")
    answer = world.spool("git_push_branch", {})
    assert "result" in answer, answer


# --------------------------------------------------------------------------
# The bundle holds exactly <remote tip>..HEAD
# --------------------------------------------------------------------------


def _bundle_of(call: dict[str, Any], path: Path) -> Path:
    path.write_bytes(base64.b64decode(call["body"]["bundle"]))
    return path


def _prerequisites(bundle: Path, repo: Path) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "bundle", "verify", str(bundle)],
                         capture_output=True, text=True, check=True)
    text = out.stdout + out.stderr
    lines = text.splitlines()
    if "requires these" in text or "requires this ref" in text:
        start = next(i for i, line in enumerate(lines) if "requires" in line)
        return sorted(line.split()[0] for line in lines[start + 1:]
                      if line and len(line.split()[0]) == 40)
    return []


def _commits_in(bundle: Path, tmp: Path, tip_repo: Path, *, exclude: list[str]) -> list[str]:
    scratch = tmp / f"probe-{secrets.token_hex(3)}"
    subprocess.run(["git", "clone", "-q", "--bare", str(tip_repo), str(scratch)], check=True,
                   env=_ENV)
    _git(scratch, "fetch", "-q", str(bundle), "+refs/heads/swarm-publish:refs/probe")
    return _git(scratch, "rev-list", "refs/probe", *(f"^{s}" for s in exclude)).split()


def test_the_bundle_holds_exactly_the_remote_tip_to_head(world: World, tmp_path: Path):
    first = _commit(world.clone, "a.txt", "a\n", "one")
    answer = world.spool("git_push_branch", {}, request_id="p1")
    assert answer["result"] == {"branch": OWN_BRANCH, "old_sha": None, "new_sha": first}
    one = _bundle_of(world.api.calls[-1], tmp_path / "one.bundle")
    assert _prerequisites(one, world.origin) == [world.base]
    assert _git(world.origin, "rev-parse", f"refs/heads/{OWN_BRANCH}") == first

    second = _commit(world.clone, "b.txt", "b\n", "two")
    third = _commit(world.clone, "c.txt", "c\n", "three")
    answer = world.spool("git_push_branch", {}, request_id="p2")
    assert answer["result"] == {"branch": OWN_BRANCH, "old_sha": first, "new_sha": third}
    call = world.api.calls[-1]
    assert call["body"]["old_sha"] == first and call["body"]["new_sha"] == third
    two = _bundle_of(call, tmp_path / "two.bundle")
    assert _prerequisites(two, world.origin) == [first]
    heads = subprocess.run(["git", "bundle", "list-heads", str(two)], capture_output=True,
                           text=True, check=True).stdout.split()
    assert heads == [third, "refs/heads/swarm-publish"]
    assert _commits_in(two, tmp_path, world.origin, exclude=[first]) == [third, second]
    assert _git(world.origin, "rev-parse", f"refs/heads/{OWN_BRANCH}") == third


def test_a_push_after_rebasing_onto_a_newer_main_carries_only_the_tasks_commits(
    world: World, tmp_path: Path
):
    world.advance_main("other.txt", "theirs\n", "main moved")
    mine = _commit(world.clone, "mine.txt", "mine\n", "mine")
    rebased = world.spool("git_rebase_onto", {"ref": "main"}, request_id="rb")["result"]
    assert rebased["conflicts"] == [] and rebased["head"] != mine
    answer = world.spool("git_push_branch", {}, request_id="p1")
    assert answer["result"]["new_sha"] == rebased["head"]
    bundle = _bundle_of(world.api.calls[-1], tmp_path / "rb.bundle")
    assert _prerequisites(bundle, world.origin) == [rebased["onto"]]
    assert _commits_in(bundle, tmp_path, world.origin, exclude=[rebased["onto"]]) == \
        [rebased["head"]]


def test_nothing_new_is_refused_without_a_request(world: World):
    assert world.spool("git_push_branch", {})["refused"]["code"] == "nothing_to_push"
    assert world.api.calls == []


# --------------------------------------------------------------------------
# Fetch and rebase in the agent's clone
# --------------------------------------------------------------------------


def test_a_fetch_lands_on_origin_ref_and_a_sha_under_refs_swarm_fetched(world: World):
    moved = world.advance_main("other.txt", "theirs\n", "main moved")
    answer = world.spool("git_fetch", {"ref": "main"}, request_id="f1")
    assert answer["result"] == {"ref": "main", "resolved_sha": moved,
                                "local_ref": "refs/remotes/origin/main"}
    assert _git(world.clone, "rev-parse", "refs/remotes/origin/main") == moved
    assert world.api.calls[-1]["body"]["haves"] == [world.base]

    answer = world.spool("git_fetch", {"ref": world.base}, request_id="f2")
    assert answer["result"]["local_ref"] == f"refs/swarm/fetched/{world.base}"
    assert _git(world.clone, "rev-parse", f"refs/swarm/fetched/{world.base}") == world.base


def test_a_rebase_conflict_leaves_the_rebase_in_progress_and_lists_the_paths(world: World):
    world.advance_main("README.md", "line one\nTHEIR line two\n", "theirs")
    world.advance_main("docs/x.md", "docs\n", "theirs again")
    _commit(world.clone, "README.md", "line one\nMY line two\n", "mine")
    _commit(world.clone, "src/app.py", "x = 1\n", "unrelated")
    answer = world.spool("git_rebase_onto", {"ref": "main"})
    result = answer["result"]
    assert result["conflicts"] == ["README.md"]
    assert result["in_progress"] is True and result["head"] is None
    # Recorded before the rebase ran: the agent may still continue it.
    assert world.tools.rebased_onto == result["onto"]
    assert (world.clone / ".git" / "rebase-merge").is_dir()
    status = _git(world.clone, "status", "--porcelain")
    assert "UU README.md" in status

    again = world.spool("git_push_branch", {}, request_id="p-mid")
    assert again["refused"]["code"] == "rebase_in_progress"


def test_a_clean_rebase_ends_on_top_of_the_fetched_tip(world: World):
    onto = world.advance_main("docs/x.md", "docs\n", "theirs")
    _commit(world.clone, "src/app.py", "x = 1\n", "mine")
    result = world.spool("git_rebase_onto", {"ref": "main"})["result"]
    assert result["onto"] == onto and result["conflicts"] == []
    assert _git(world.clone, "rev-parse", "HEAD~1") == onto
    # The publish diffs from here (SPEED-D1, `Worker._adopt_rebase_base`).
    assert world.tools.rebased_onto == onto
    assert _git(world.clone, "log", "-1", "--format=%cn <%ce>") == f"{PERSON[0]} <{PERSON[1]}>"


# --------------------------------------------------------------------------
# The signed call and the fence
# --------------------------------------------------------------------------


def test_a_request_is_signed_and_its_proof_verifies_with_swarm_apis_verifier(world: World):
    answer = world.spool("pr_checks", {"pr": 12})
    assert answer["result"]["state"] == "green"
    call = world.api.calls[-1]
    assert call["path"] == "/v1/attempts/att_1/git/pr_checks"
    assert call["verified"] is True
    assert {k: call["body"][k] for k in ("tenant_id", "task_id", "attempt_id", "lease_id",
                                         "generation")} == {
        "tenant_id": "acme", "task_id": TASK, "attempt_id": "att_1", "lease_id": "lease_1",
        "generation": 3}
    # The control: the same proof does not verify over a changed body.
    tampered = call["raw"].replace(b'"generation": 3', b'"generation": 4')
    assert tampered != call["raw"]
    message = api_childkey.request_message("POST", call["path"], tampered, call["timestamp"])
    assert api_childkey.verify_proof(world.key.public, call["proof"], message) is False


def test_a_409_raises_the_fenced_error_and_tells_the_agent(world: World):
    world.api.fenced = True
    spool = gittools.GitTools.spool(world.work)
    shim.write_request(spool, "r1", "git_fetch", {"ref": "main"})
    with pytest.raises(gittools.GitToolFenced) as raised:
        world.tools.tick(world.work)
    assert isinstance(raised.value, children_mod.ChildSubmitFenced)
    answer = json.loads((spool / "responses" / "r1.json").read_text())
    assert answer["refused"]["code"] == "fenced"
    assert not (spool / "requests" / "r1.json").exists()


def test_the_route_is_called_once_per_request_and_a_push_is_rate_limited(tmp_path: Path):
    world = World(tmp_path, pushes_per_hour=1)
    _commit(world.clone, "a.txt", "a\n", "one")
    assert "result" in world.spool("git_push_branch", {}, request_id="p1")
    _commit(world.clone, "b.txt", "b\n", "two")
    assert world.spool("git_push_branch", {}, request_id="p2")["refused"]["code"] == "rate_limited"
    assert len(world.api.calls) == 1


# --------------------------------------------------------------------------
# The tip a push answered with, for the end-of-task publish (#1188)
# --------------------------------------------------------------------------


def test_the_relay_remembers_the_tip_its_own_push_answered_with(world: World):
    head = _commit(world.clone, "a.txt", "a\n", "one")
    # MUTATION: record fetched tips as pushed ones and this fetch of the
    # task's own branch-to-be would already answer a tip.
    world.spool("git_fetch", {"ref": "main"}, request_id="f1")
    assert world.tools.pushed_tip(OWN_BRANCH) is None
    answer = world.spool("git_push_branch", {}, request_id="p1")
    # MUTATION: drop `self.pushed_tips[chosen] = new_sha` and this is None.
    assert world.tools.pushed_tip(OWN_BRANCH) == answer["result"]["new_sha"] == head
    # MUTATION: answer from `origin.tips` and `main`, which the fetch learnt,
    # reads as a pushed tip the publish would lease on.
    assert world.tools.pushed_tip("main") is None


def test_a_pushed_tip_is_answered_only_for_a_branch_this_task_may_push(world: World):
    _commit(world.clone, "a.txt", "a\n", "one")
    world.spool("git_push_branch", {}, request_id="p1")
    # A tip recorded under another name -- however it got there -- is never
    # a lease the publish is handed.
    world.tools.pushed_tips[f"swarm/{OTHER_TASK}"] = world.tools.pushed_tips[OWN_BRANCH]
    # MUTATION: drop the `branch in self.cfg.pushable` test and this answers a sha.
    assert world.tools.pushed_tip(f"swarm/{OTHER_TASK}") is None


def _refusing_pr_checks(message: str):
    """swarm-api's 422 for a forge refusal, as `ChildApi` reads it off the wire."""
    def pr_checks(self, body: dict[str, Any]) -> dict[str, Any]:
        raw = json.dumps({"code": "forge_refused", "message": message}).encode()
        raise urllib.error.HTTPError("https://api.invalid", 422, "x", {}, io.BytesIO(raw))
    return pr_checks


def test_a_refused_tool_calls_log_line_carries_its_scrubbed_message(world: World, monkeypatch):
    registered = "reg-" + secrets.token_hex(16)
    shaped = "ghp_" + secrets.token_hex(18)
    stream = io.StringIO()
    world.tools.log = build_logger(task_id=TASK, attempt_id="att_1", tenant_id="acme",
                                   generation=3, runner_profile="mock", stream=stream)
    world.tools._secrets = (registered,)
    monkeypatch.setattr(FakeApi, "pr_checks", _refusing_pr_checks(
        "GitHub refused the tenant's forge credential for the pull request's check runs "
        f"(HTTP 403): {registered} {shaped}"))

    answer = world.spool("pr_checks", {}, request_id="c1")

    assert answer["refused"]["code"] == "forge_refused"
    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    (refused,) = [line for line in lines if "git tool refused" in json.dumps(line)]
    text = json.dumps(refused)
    # MUTATION: log the line without `message=` (as before #1188) and the
    # cause of the refusal is not in the worker's log at all.
    assert "check runs (HTTP 403)" in text, refused
    # MUTATION: log `refusal.message` unscrubbed and either value is in the log.
    assert registered not in stream.getvalue()
    assert shaped not in stream.getvalue()
