"""The Jev outbound filter: what the gate hooks and the Jev browser may send.

Lane jev-outbound-redaction (owner decision 2026-11-02, CLD's Jev report): the
worker's judgments went through `judge/sanitize.py`, but jev-belay and
jev-enforce (run by `swarm-jev-gate.mjs`) got only the key and registered-
secret scrub, and the jev-browser MCP server sent page text and element labels
with no SwarmCloud-side redaction at all. Both launchers now run every request
body through one filter in `swarm-jev-gate.mjs` before it leaves:

1. path deny for an object with a `path`/`file` key (`sanitize.py`'s globs
   and directories, and the repository's hard-stop paths when the config names
   them): the object keeps its identity keys only;
2. the redaction rule families, asked of the image's own `swarm_redaction`
   (so there is one definition, not a copy that drifts);
3. the registered-secret and key scrub the launcher already did;
4. the never-sent keys, at any depth.

A matching string is DROPPED, never masked, as in the worker. Counts, never
text, go to the hooks' counts file and the browser's audit line.

MUTATION: remove `filterBody` from `guardedFetch`, the `--import` from the
browser wrapper, or any one step above, and a test here goes red.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_worker.judge import sanitize

REPO = Path(__file__).resolve().parents[3]
LAUNCHER = REPO / "images" / "agent-runtime-base" / "jev-gates" / "bin" / "swarm-jev-gate.mjs"
WRAPPER = REPO / "images" / "agent-runtime-browser" / "jev-browser" / "bin" / "jev-browser-mcp"
SERVER = "/opt/swarm/jev-browser/node_modules/jev-browser/bin/jev-browser-mcp.mjs"
GATE = "/opt/swarm/jev-gates/bin/swarm-jev-gate.mjs"
#: The interpreter that holds swarm_redaction here: the test's own.
PYTHON = sys.executable


def _github_token() -> str:
    return "gh" + "p_" + secrets.token_hex(18)


@pytest.fixture(scope="module")
def node() -> str:
    found = shutil.which("node")
    if not found:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH in CI, so the launcher cannot run; it must not pass by skipping")
        pytest.skip("node is not installed here; CI runs these")
    return found


def _node_eval(node: str, script: str) -> Any:
    preamble = f"import * as gate from {json.dumps(LAUNCHER.as_uri())};\nconst PY = {json.dumps(PYTHON)};\n"
    done = subprocess.run([node, "--input-type=module", "-e", preamble + script],
                          capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


class _Jev(BaseHTTPRequestHandler):
    seen: list[str] = []

    def do_POST(self) -> None:  # noqa: N802 -- http.server's spelling
        type(self).seen.append(self.rfile.read(int(self.headers["Content-Length"])).decode())
        out = b'{"answers": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def fake_jev():
    server = HTTPServer(("127.0.0.1", 0), _Jev)
    _Jev.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1/systemone"
    finally:
        server.shutdown()


# --- the gate hooks ----------------------------------------------------------------

#: A stand-in for jev-enforce's Stop hook: it sends what the real one does
#: (dist/src/check.js `findViolations`) for the final reply and each edit.
FAKE_ENFORCE = """
const edits = JSON.parse(process.env.FAKE_EDITS);
for (const [file, text] of Object.entries(edits)) {
  const res = await fetch("https://api.typesafe.ai/v1/systemone", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${process.env.TYPESAFE_API_KEY}` },
    body: JSON.stringify({ state: { kind: "Text an AI coding assistant just wrote into a file", file, text },
                           model: "jev-latest", questions: { r1: { type: "noul", instructions: "a rule" } } }),
  });
  await res.json();
}
process.exit(0);
"""


def _run_enforce(node: str, tmp_path: Path, shim_url: str, edits: dict[str, str], *,
                 env: dict[str, str] | None = None, config: dict[str, Any] | None = None) -> list[dict]:
    root = tmp_path / "jev-gates"
    (root / "bin").mkdir(parents=True)
    shutil.copy(LAUNCHER, root / "bin" / LAUNCHER.name)
    cli = root / "jev-enforce" / "dist" / "src" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text(FAKE_ENFORCE)
    (root / "jev-enforce" / "package.json").write_text('{"type": "module"}')
    shim = tmp_path / "shim"
    shim.write_text(json.dumps({"shim_url": shim_url, "token": secrets.token_hex(16)}))
    counts = tmp_path / "counts"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"key_file": str(shim), "counts_file": str(counts), "data_dir": str(tmp_path),
                                "secret_env_names": ["MY_PASSPHRASE"], "redaction_python": PYTHON,
                                **(config or {})}))
    done = subprocess.run([node, str(root / "bin" / LAUNCHER.name), str(path), "enforce-stop"],
                          input="{}", cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
                          env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path),
                               "FAKE_EDITS": json.dumps(edits), **(env or {})})
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in counts.read_text().splitlines()]


def test_an_edited_env_files_text_never_reaches_the_fake_jev_from_jev_enforce(node, tmp_path, fake_jev):
    # MUTATION: send the body without the path deny and the .env text reaches Jev.
    marker, kept = "marker-" + secrets.token_hex(8), "kept-" + secrets.token_hex(8)
    lines = _run_enforce(node, tmp_path, fake_jev, {
        str(tmp_path / ".env.local"): f"REGION=us-central1\nFEATURE={marker}\n",
        str(tmp_path / "deploy" / "id.pem"): f"body {marker}",
        str(tmp_path / "secrets" / "notes.txt"): f"notes {marker}",
        str(tmp_path / "src" / "app.py"): f"print('{kept}')\n",
    })
    assert len(_Jev.seen) == 4
    assert all(marker not in body for body in _Jev.seen)
    bodies = [json.loads(b) for b in _Jev.seen]
    # The name still goes, as the worker's identity keys do; the content never.
    assert [b["state"].get("text") for b in bodies[:3]] == [None, None, None]
    assert bodies[0]["state"]["file"].endswith(".env.local")
    assert bodies[3]["state"]["text"] == f"print('{kept}')\n"
    assert bodies[3]["questions"] == {"r1": {"type": "noul", "instructions": "a rule"}}
    assert sum(line["denied"] for line in lines) == 3
    assert {k for line in lines for k in line} == {"hook", "calls", "blocked", "rules", "dropped", "denied", "withheld"}


def test_the_repositorys_hard_stop_paths_are_denied_when_the_config_names_them(node, tmp_path, fake_jev):
    marker = "marker-" + secrets.token_hex(8)
    _run_enforce(node, tmp_path, fake_jev, {str(tmp_path / "infra" / "prod.yaml"): f"x: {marker}"},
                 config={"hard_stop_paths": ["infra/"]})
    (body,) = _Jev.seen
    assert marker not in body and json.loads(body)["state"]["file"].endswith("infra/prod.yaml")


def test_a_credential_in_an_edit_is_dropped_and_a_registered_secret_is_still_scrubbed(node, tmp_path, fake_jev):
    token, passphrase = _github_token(), "pp-" + secrets.token_hex(8)
    lines = _run_enforce(node, tmp_path, fake_jev, {
        str(tmp_path / "src" / "a.py"): f"GH = '{token}'",
        str(tmp_path / "src" / "b.py"): f"use {passphrase} here",
    }, env={"MY_PASSPHRASE": passphrase})
    first, second = (json.loads(b) for b in _Jev.seen)
    # Dropped, never masked: no prefix of it, and no `text` key, survives.
    assert "text" not in first["state"] and token[:8] not in _Jev.seen[0]
    assert second["state"]["text"] == "use <redacted> here"
    assert sum(line["dropped"] for line in lines) == 1


def test_the_filter_drops_never_sent_keys_and_credentials_at_every_depth(node):
    token = _github_token()
    out = _node_eval(node, f"""
const counts = {{ dropped: 0, denied: 0, withheld: 0 }};
const body = JSON.stringify({{ state: {{
  Author: "a", nested: [{{ email: "e@example.com", env: {{ X: "1" }}, transcript: "t", ok: "fine" }}],
  list: ["keep", {json.dumps(token)}], [{json.dumps(token)}]: "a key that is a credential",
  user: "u", login: "l", environment: "e", n: 3, b: true, z: null,
}} }});
const sent = gate.filterBody(body, {{ secrets: [], hardStop: [], cwd: "/w", python: PY, counts }});
console.log(JSON.stringify({{ sent: JSON.parse(sent), counts }}));
""")
    assert out["sent"] == {"state": {"nested": [{"ok": "fine"}], "list": ["keep"], "n": 3, "b": True, "z": None}}
    assert out["counts"] == {"dropped": 2, "denied": 0, "withheld": 7}


def test_a_state_sent_as_json_text_is_filtered_as_its_structure(node):
    # The browser's internal backend sends `state` as stable JSON text.
    token = _github_token()
    out = _node_eval(node, f"""
const state = JSON.stringify({{ page: {{ text: "hello " + {json.dumps(token)}, elements: [{{ i: 1, label: "Sign in" }}] }} }});
const sent = gate.filterBody(JSON.stringify({{ state, questions: {{}} }}), {{ python: PY }});
console.log(sent);
""")
    assert token not in json.dumps(out)
    assert json.loads(out["state"]) == {"page": {"elements": [{"i": 1, "label": "Sign in"}]}}


def test_without_its_redaction_python_the_filter_sends_nothing(node):
    out = _node_eval(node, """
let refused = "";
try { gate.filterBody(JSON.stringify({ state: { text: "hello" } }), { python: "/nonexistent/python" }); }
catch (e) { refused = e.message; }
console.log(JSON.stringify(refused));
""")
    assert "refused" in out


def test_the_path_deny_and_never_sent_keys_are_the_workers():
    out = subprocess.run(["node", "--input-type=module", "-e",
                          f"import * as g from {json.dumps(LAUNCHER.as_uri())};"
                          "console.log(JSON.stringify({globs: g.DENIED_NAME_GLOBS, dirs: g.DENIED_DIRECTORIES,"
                          " identity: g.IDENTITY_KEYS, never: g.NEVER_SENT_KEYS, paths: g.PATH_KEYS}))"],
                         capture_output=True, text=True, timeout=60, check=False) if shutil.which("node") else None
    if out is None:
        pytest.skip("node is not installed here; CI runs these")
    got = json.loads(out.stdout)
    assert tuple(got["globs"]) == sanitize.DENIED_NAME_GLOBS
    assert set(got["dirs"]) == sanitize.DENIED_DIRECTORIES
    assert set(got["identity"]) == sanitize.IDENTITY_KEYS
    assert set(got["never"]) == sanitize.NEVER_SENT_KEYS
    assert tuple(got["paths"]) == sanitize._PATH_KEYS


@pytest.mark.parametrize("path, denied", [
    (".env", True), ("app/.env.production", True), ("k/server.key", True), ("c.p12", True),
    ("prod.tfvars", True), ("terraform.tfstate.backup", True), ("secrets/a.txt", True),
    ("x/credentials/y.json", True), ("./.env", True), ("src/env.py", False), ("README.md", False),
    ("docs/secrets.md", False),
])
def test_the_launchers_path_deny_agrees_with_the_workers(node, path, denied):
    assert sanitize.is_denied_path(path) is denied
    assert _node_eval(node, f"console.log(JSON.stringify(gate.isDeniedPath({json.dumps(path)}, [], '/w')))") is denied


# --- the browser -------------------------------------------------------------------

#: A stand-in for jev-browser's server: one Jev call shaped as upstream
#: src/jev.mjs sends it (`fetch(API_URL, {method, headers, body})`), with a
#: page's text and element labels in `state`.
FAKE_SERVER = """
const page = JSON.parse(process.env.FAKE_PAGE);
const res = await fetch(process.env.JEV_API_URL, {
  method: "POST",
  headers: { "Content-Type": "application/json", Authorization: `Bearer ${process.env.TYPESAFE_API_KEY}` },
  body: JSON.stringify({ state: { page }, model: "jev-latest", questions: { ok: { type: "noul", instructions: "done?" } } }),
});
await res.json();
"""


def test_a_page_holding_a_github_token_never_reaches_the_fake_jev_from_the_browser(node, tmp_path, fake_jev):
    # MUTATION: exec the server without the wrapper's `--import` and the token reaches Jev.
    server = tmp_path / "server.mjs"
    server.write_text(FAKE_SERVER)
    text = WRAPPER.read_text()
    for old in ('NODE="/usr/local/bin/node"', f'SERVER="{SERVER}"', f'GATE="{GATE}"',
                'REDACTION_PYTHON="/opt/venv/bin/python"'):
        assert old in text, old
    copy = tmp_path / "jev-browser-mcp"
    copy.write_text(text.replace('NODE="/usr/local/bin/node"', f'NODE="{node}"')
                    .replace(f'SERVER="{SERVER}"', f'SERVER="{server}"')
                    .replace(f'GATE="{GATE}"', f'GATE="{LAUNCHER}"')
                    .replace('REDACTION_PYTHON="/opt/venv/bin/python"', f'REDACTION_PYTHON="{PYTHON}"'))
    token, key = _github_token(), "ts" + "-" + secrets.token_hex(24)
    page = {"title": "Settings", "text": f"Your new token is {token}",
            "elements": [{"i": 1, "role": "button", "label": "Copy"}, {"i": 2, "label": token}]}
    counts = tmp_path / "browser.counts"
    done = subprocess.run(["bash", str(copy)], capture_output=True, text=True, timeout=60, check=False, env={
        "PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "SWARM_CDP_ENDPOINT": "http://127.0.0.1:9222",
        "SWARM_JEV_BACKEND": "typesafe", "JEV_API_URL": fake_jev, "TYPESAFE_API_KEY": key,
        "FAKE_PAGE": json.dumps(page), "SWARM_JEV_BROWSER_COUNTS": str(counts),
        "SWARM_JEV_REDACTION_PYTHON": "/nonexistent/python",  # the wrapper's own value wins
    })
    assert done.returncode == 0, done.stderr
    (body,) = _Jev.seen
    assert token not in body and token[:8] not in body and key not in body
    sent = json.loads(body)
    assert sent["state"]["page"] == {"title": "Settings", "elements": [{"i": 1, "role": "button", "label": "Copy"}, {"i": 2}]}
    assert sent["questions"] == {"ok": {"type": "noul", "instructions": "done?"}}
    (line,) = counts.read_text().splitlines()
    assert json.loads(line) == {"hook": "browser", "calls": 1, "dropped": 2, "denied": 0, "withheld": 0}
    assert token not in done.stderr and '"dropped":2' in done.stderr
