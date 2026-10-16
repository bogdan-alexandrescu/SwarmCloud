"""The workspace call guard gives every call it has ever been fed the same verdict.

scripts/lib/workspace-guard.sh is the only thing between the workspace job's
project-wide account-IAM identity and the other team's resources in the shared
project (docs/workspaces.md §2.5). Lane GUARD-SPEED (part of #1334) rewrote how
it gathers a call's facts -- fewer jq, dirname and subshell starts per judged
call -- and a rewrite of a guard is exactly where a verdict changes without
anyone meaning it to.

fixtures/workspace_guard_corpus.json is every distinct call the guard's tests
fed it on 2026-10-16, before that rewrite: the self-test's cases, this
directory's test_workspace_guard.py, test_register_tenant_workspace.py (the
real register-tenant.sh, kubernetes/apply.sh and the network parity check
behind the real shims) and test_workspace_deployer_walk.py, 394 calls across
every rule C0-C9, allow and refuse. Each record carries what the judge reads --
the arguments, the files they name, stdin, the expectation file, whether a stop
file existed, the kubeconfig's context, the two CLOUDSDK_ variables and the WD9
switch -- and the verdict the guard gave it then: allow or refuse, the rule,
the command words and the reason, byte for byte.

Temporary paths are written `{dir}` (one directory per call here) and the one
line of a curl config `{auth_config}`, filled at run time from common.sh's own
auth_config around a token made here: nothing in the corpus is a path on the
machine that recorded it, or looks like a credential.

To re-record after a deliberate change to the rules or the judge: capture the
calls again the way PR "(GUARD-SPEED; part of #1334)" describes, and say in the
pull request which verdicts moved and why. A verdict that moves without that is
the defect this file exists to catch.
"""

from __future__ import annotations

import functools
import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUARD = REPO / "scripts" / "lib" / "workspace-guard.sh"
CORPUS = Path(__file__).resolve().parent / "fixtures" / "workspace_guard_corpus.json"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required")

DOC = json.loads(CORPUS.read_text())
RECORDS: list[dict] = DOC["records"]
BLOBS: dict[str, str] = DOC["blobs"]


@functools.cache
def _auth_shape() -> tuple[str, str]:
    """The config line common.sh's auth_config writes, split around its token."""
    mark = "@" + secrets.token_hex(8) + "@"
    line = subprocess.run(["bash", "-c", f'source "{REPO / "scripts" / "lib" / "common.sh"}"; auth_config "$1"',
                           "_", mark], capture_output=True, text=True, timeout=60, check=True).stdout
    head, _, tail = line.rstrip("\n").partition(mark)
    return head, tail


def _fill(text: str | None, where: Path, token: str) -> str | None:
    if text is None:
        return None
    if "{auth_config}" in text:
        head, tail = _auth_shape()
        text = text.replace("{auth_config}", head + token + tail)
    return text.replace("{dir}", str(where))


#: The replay is split this many ways so `pytest -n auto` judges the shards side
#: by side: one guard process judges ~400 calls in about 90 s on a lane container.
SHARDS = 8


def _indices(shard: int | None) -> list[int]:
    return [i for i in range(len(RECORDS)) if shard is None or i % SHARDS == shard]


def materialise(tmp: Path, indices: list[int]) -> Path:
    """Write the world of each record named under tmp; return the replay plan."""
    plan = tmp / "plan.jsonl"
    lines = []
    for i in indices:
        rec = RECORDS[i]
        where = tmp / f"r{i}"
        where.mkdir()
        token = secrets.token_hex(24)
        for path, blob in rec["files"].items():
            target = Path(_fill(path, where, token))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_fill(BLOBS[blob], where, token))
        expect = _fill(rec["expect_path"], where, token) or ""
        if rec["expect_state"] in ("ok", "loose"):
            path = Path(expect)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(BLOBS[rec["expect"]] if rec["expect"] else "{}")
            path.chmod(0o622 if rec["expect_state"] == "loose" else 0o600)
            if rec["stopped"]:
                Path(expect + ".stop").write_text('{"state":"needs_owner"}\n')
        kubeconfig = where / "kubeconfig.recorded"
        context = f"current-context: {rec['context']}\n" if rec["context"] else ""
        kubeconfig.write_text("apiVersion: v1\nkind: Config\n" + context)
        stdin = BLOBS[rec["stdin"]] if rec["stdin"] else None
        lines.append(json.dumps({
            "tool": rec["tool"],
            "argv": [_fill(a, where, token) for a in rec["argv"]],
            "stdin": _fill(stdin, where, token),
            "expect": expect,
            "kubeconfig": str(kubeconfig),
            "wd9_fallback": rec["wd9_fallback"],
            "env_impersonate": rec["env_impersonate"],
            "env_project": rec["env_project"],
        }))
    plan.write_text("\n".join(lines) + "\n")
    return plan


def replay(tmp: Path, shard: int | None = None) -> list[list[str]]:
    plan = materialise(tmp, _indices(shard))
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SWARM_CALL_GUARD", "CLOUDSDK_", "GOOGLE_")) and k != "KUBECONFIG"}
    env.update(SWARM_ENV_FILE=str(tmp / "no.env"), NO_COLOR="1", TMPDIR=str(tmp))
    out = subprocess.run([str(GUARD), "self-test", "--replay", str(plan)], capture_output=True,
                         text=True, env=env, timeout=900, check=False)
    assert out.returncode == 0, out.stderr[-3000:]
    return [json.loads(line) for line in out.stdout.splitlines()]


def test_the_corpus_covers_every_rule_both_ways() -> None:
    seen = {(rec["want"][1], rec["want"][0]) for rec in RECORDS}
    for rule in [f"C{n}" for n in range(10)]:
        assert (rule, "refuse") in seen, f"the corpus holds no call {rule} refuses"
    for rule in [f"C{n}" for n in range(1, 9)]:
        assert (rule, "allow") in seen, f"the corpus holds no call {rule} allows"
    assert {rec["tool"] for rec in RECORDS} >= {"gcloud", "kubectl", "curl"}


def test_the_shards_cover_every_record() -> None:
    assert sorted(i for shard in range(SHARDS) for i in _indices(shard)) == list(range(len(RECORDS)))


@pytest.mark.parametrize("shard", range(SHARDS))
def test_every_recorded_call_gets_the_same_verdict_byte_for_byte(tmp_path: Path, shard: int) -> None:
    indices = _indices(shard)
    got = replay(tmp_path, shard)
    assert len(got) == len(indices), f"the replay judged {len(got)} of {len(indices)} calls"
    moved = []
    for i, verdict in zip(indices, got):
        rec = RECORDS[i]
        want = [_fill(field, tmp_path / f"r{i}", "") for field in rec["want"]]
        if verdict != want:
            moved.append(f"#{i} {rec['tool']} {' '.join(rec['argv'])[:160]}\n  was {want}\n  now {verdict}")
    assert not moved, f"{len(moved)} of {len(indices)} verdicts moved:\n" + "\n".join(moved[:20])


# --------------------------------------------------------------------------
# what one judged call costs, in process starts
# --------------------------------------------------------------------------
#
# Measured by lane FIX-TESTS (PR 1415): each judged call started about 7 jq, 7
# dirname and 2 bash, and one workspace create makes ~129 of them -- most of
# test_register_tenant_workspace.py's ~320 s, and every real workspace job pays
# it too. Every command a call starts is counted here by a wrapper on PATH,
# through the real shim, for the four shapes of call the job makes.

COUNTED = ("jq", "dirname", "basename", "grep", "sed", "tr", "python3", "bash", "cat", "ls", "awk")
SHIMS = REPO / "scripts" / "lib" / "guard-bin"


def _pick(tool: str, rule: str, *, stdin: bool, files: bool) -> int:
    for i, rec in enumerate(RECORDS):
        if (rec["tool"], rec["want"][0], rec["want"][1]) == (tool, "allow", rule) \
           and (rec["stdin"] is not None) == stdin and bool(rec["files"]) == files \
           and rec["expect_state"] == "ok" and not rec["stopped"]:
            return i
    raise AssertionError(f"the corpus holds no {tool} call {rule} allows (stdin={stdin}, files={files})")


CALLS = {
    "gcloud": _pick("gcloud", "C1", stdin=False, files=False),
    "gcloud with a condition file": _pick("gcloud", "C4", stdin=False, files=True),
    "kubectl with a manifest on stdin": _pick("kubectl", "C7", stdin=True, files=False),
    "curl with its config on stdin": _pick("curl", "C8", stdin=True, files=False),
}


@pytest.mark.parametrize("call", sorted(CALLS))
def test_a_judged_call_starts_one_jq_and_no_dirname_of_its_own(tmp_path: Path, call: str) -> None:
    index = CALLS[call]
    p = json.loads(materialise(tmp_path, [index]).read_text())
    log = tmp_path / "started"
    wrap = tmp_path / "wrap"
    wrap.mkdir()
    for name in COUNTED:
        real = shutil.which(name)
        if real:
            (wrap / name).write_text(f'#!/bin/sh\necho {name} >> "{log}"\nexec "{real}" "$@"\n')
            (wrap / name).chmod(0o755)
    real_bin = tmp_path / "realbin"
    real_bin.mkdir()
    (real_bin / p["tool"]).write_text("#!/bin/sh\nexit 0\n")
    (real_bin / p["tool"]).chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SWARM_", "CLOUDSDK_", "GOOGLE_")) and k != "KUBECONFIG"}
    env.update(PATH=f"{SHIMS}:{wrap}:{real_bin}:{os.environ['PATH']}", SWARM_CALL_GUARD=p["expect"],
               KUBECONFIG=p["kubeconfig"], SWARM_ENV_FILE=str(tmp_path / "no.env"), NO_COLOR="1")
    if p["env_project"]:
        env["CLOUDSDK_CORE_PROJECT"] = p["env_project"]
    out = subprocess.run([str(SHIMS / p["tool"]), *p["argv"]], input=p["stdin"] or "", env=env,
                         capture_output=True, text=True, timeout=120, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    started = log.read_text().split() if log.exists() else []
    count = {name: started.count(name) for name in COUNTED}
    assert count["bash"] >= 1 and count["jq"] >= 1, f"the wrappers saw nothing start: {count}"
    # One jq: the judge, which also builds the argument list, the condition
    # files and the deny-lists it used to ask separate jq calls for.
    assert count["jq"] == 1, count
    # No dirname at all: the guard, the shim and common.sh resolve paths by
    # parameter expansion (GUARD-SPEED-2). The one cat a call can start is the
    # guard copying a stdin it was handed; common.sh no longer starts one when
    # it is sourced.
    assert count["dirname"] == 0 and count["basename"] == 0, count
    assert count["cat"] <= (1 if p["stdin"] is not None else 0), count
    # The kubeconfig's context is read by the shell, not sed | sed | tr; the
    # deny-list is filtered in the judge, not by grep.
    assert count["sed"] == 0 and count["tr"] == 0 and count["grep"] == 0, count
    # The manifest reader is the one python start, and only for a manifest.
    assert count["python3"] == (1 if p["tool"] == "kubectl" else 0), count
    assert count["bash"] <= 2, count


# --------------------------------------------------------------------------
# the two things the guard used to ask other processes, held to them
# --------------------------------------------------------------------------

COMMON = REPO / "scripts" / "lib" / "common.sh"


def _bash(script: str, *args: str) -> str:
    return subprocess.run(["bash", "-c", script, "_", *args], capture_output=True, text=True,
                          timeout=60, check=True).stdout


def test_the_judge_reads_the_deny_list_as_guard_deny_json_prints_it(tmp_path: Path) -> None:
    """deny_scan in the judge restates guard_deny_json; every entry it prints is refused by name."""
    whole = _bash(f'source "{COMMON}"; printf "%s\\n" "${{SHARED_DENY_LIST[@]}}"').splitlines()
    scan = json.loads(_bash(f'source "{COMMON}"; guard_deny_json'))
    assert whole and scan and set(scan) != set(whole), "common.sh no longer drops `default` or adds `(default)`"
    base = _pick("kubectl", "C1", stdin=False, files=False)
    rec = RECORDS[base]
    expect = tmp_path / "expect.json"
    expect.write_text(BLOBS[rec["expect"]])
    expect.chmod(0o600)
    lines, wants = [], []
    for name in sorted(set(whole) | set(scan)):
        lines.append({"tool": "gcloud", "argv": ["projects", "describe", name], "kubeconfig": "",
                      "stdin": None, "expect": str(expect), "wd9_fallback": True,
                      "env_impersonate": "", "env_project": ""})
        wants.append(("argument", name, name in scan))
    for i, name in enumerate(sorted(whole)):
        kubeconfig = tmp_path / f"kubeconfig-{i}"
        kubeconfig.write_text(f"current-context: gke_p_l_{name}\n")
        argv = [a for a in rec["argv"] if not a.startswith("--context")]
        lines.append({"tool": "kubectl", "argv": argv, "kubeconfig": str(kubeconfig), "stdin": None,
                      "expect": str(expect), "wd9_fallback": True, "env_impersonate": "", "env_project": ""})
        wants.append(("context", name, True))
    plan = tmp_path / "plan.jsonl"
    plan.write_text("".join(json.dumps(line) + "\n" for line in lines))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SWARM_CALL_GUARD", "CLOUDSDK_"))}
    env.update(SWARM_ENV_FILE=str(tmp_path / "no.env"), TMPDIR=str(tmp_path))
    out = subprocess.run([str(GUARD), "self-test", "--replay", str(plan)], capture_output=True, text=True,
                         env=env, timeout=300, check=False)
    assert out.returncode == 0, out.stderr[-2000:]
    got = [json.loads(line) for line in out.stdout.splitlines()]
    assert len(got) == len(wants)
    for (kind, name, denied), (verdict, rule, _, reason) in zip(wants, got):
        if kind == "argument":
            named = f"an argument names {name}, which belongs to another team" in reason
            assert named == denied, (name, verdict, rule, reason)
        else:
            assert verdict == "refuse", (name, verdict, rule, reason)
            assert f"is the cluster {name}, which belongs to another team" in reason, (name, reason)


PATHS = ["/a/b/c", "/a", "/a/", "//", "/", "a", "a/", "a//b", "//a", "///a/b//", "..", "../x", "",
         "./x", "/usr/bin//gcloud", "/x/foo/", "a/b/", "/a//b//c//", ".", "x/./y"]


def test_the_guards_dirname_and_basename_answer_as_coreutils_does() -> None:
    if shutil.which("dirname") is None or shutil.which("basename") is None:
        pytest.skip("coreutils dirname/basename are not on PATH")
    script = (
        f'eval "$(sed -n \'/^guard_dirname() {{/,/^}}/p;/^guard_basename() {{/,/^}}/p\' "{GUARD}")"\n'
        'for p in "$@"; do\n'
        '  guard_dirname "$p"; guard_basename "$p"\n'
        '  printf "%s\\t%s\\t%s\\t%s\\n" "$GUARD_DIRNAME" "$(dirname -- "$p")" "$GUARD_BASENAME" "$(basename -- "$p")"\n'
        'done\n'
    )
    rows = _bash(script, *PATHS).split("\n")[:-1]
    assert len(rows) == len(PATHS)
    for path, row in zip(PATHS, rows):
        ours_d, theirs_d, ours_b, theirs_b = row.split("\t")
        assert (ours_d, ours_b) == (theirs_d, theirs_b), path


#: kubeconfigs as they come, and as they could be made to come: quoted, CRLF,
#: tabbed, no space after the colon, an empty first value, indented look-alikes,
#: no trailing newline, a second current-context line.
KUBECONFIGS = [
    "apiVersion: v1\ncurrent-context: swarm-dev\n",
    "current-context: \"gke_p_us-central1_swarm-autopilot\"\n",
    "current-context: 'swarm-dev'\r\n",
    "current-context:\t \tswarm dev \t\n",
    "current-context:swarm-dev",
    "  current-context: indented\nkind: Config\n",
    "current-context: \"\"\ncurrent-context: second\n",
    "current-context: first\ncurrent-context: second\n",
    "contexts: []\n",
    "",
    "current-context: a\"b'c d\x0be\x0cf\n",
    "# current-context: comment\ncurrent-context-x: no\ncurrent-context: yes\n",
]
OLD_CONTEXT_READER = (
    "sed -n 's/^current-context:[[:space:]]*//p' \"$1\" | sed -n '1p' | tr -d '\"\\047[:space:]'"
)


def test_the_guard_reads_a_kubeconfigs_context_as_sed_and_tr_did(tmp_path: Path) -> None:
    """guard_current_context replaced `sed | sed | tr`; for one file it gives what they gave."""
    for i, text in enumerate(KUBECONFIGS):
        path = tmp_path / f"kubeconfig-{i}"
        path.write_bytes(text.encode())
        old = _bash(OLD_CONTEXT_READER, str(path))
        script = (
            f'eval "$(sed -n \'/^guard_current_context() {{/,/^}}/p\' "{GUARD}")"\n'
            'KUBECONFIG="$1" guard_current_context; printf "%s" "$GUARD_CONTEXT"\n'
        )
        new = _bash(script, str(path))
        assert new == old.rstrip("\n"), (text, old, new)
