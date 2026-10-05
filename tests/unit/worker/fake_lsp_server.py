"""A fake language server over stdio, for the LSP driver's protocol paths.

docs/repo-index.md §3.5 runs pyright, tsserver, gopls and terraform-ls as
child processes speaking LSP over stdio. The unit tests must not need any of
them installed, so this script stands in: it speaks the same framing
(`Content-Length` headers, JSON-RPC 2.0) and answers from a scenario file
the test writes, which says what each request returns and how the server
misbehaves.

    python fake_lsp_server.py <scenario.json>

The scenario's keys, all optional:

  mode          "ok" (default), "hang" (never answer the queries),
                "slow" (answer each query after `delay` seconds), "crash"
                (exit 3 on the first query), "memory" (hold `memory_mib`
                MiB once initialized and keep answering, each query after
                `delay` seconds), "init_error" (answer `initialize` with an
                error)
  definitions   {"<path>:<line>": {"path", "line", "character"}} -- a
                definition at 0-based <line> of <path> (relative to the
                workspace root) answers one LocationLink to the target
  incoming      {"<path>:<line>": [{"path", "line", "character", "name",
                "call_line"}]} -- a call-hierarchy item prepared at <line>
                answers these incoming callers
  references    {"<path>:<line>": [{"path", "line", "character"}]}
  server_info   initialize's serverInfo (default fake-lsp 0.0.1)
  log           a file each received message is appended to, as one JSON
                line, so a test can read the exact positions it was asked
  ask_config    if true, the server asks `workspace/configuration` after
                `initialized` and logs the client's answer
  log_env       if true, the first log line is {"env": <its environment>}

Nothing here is a real server's behaviour beyond the wire format; what a
real server returns for a real repository is the adapters' tests (RI10a-d).
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.parse import unquote, urlparse

QUERIES = {
    "textDocument/definition",
    "textDocument/prepareCallHierarchy",
    "callHierarchy/incomingCalls",
    "textDocument/references",
}


def read_message(stream) -> dict | None:
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    if length is None:
        return None
    return json.loads(stream.read(length))


def write_message(stream, message: dict) -> None:
    body = json.dumps(message).encode("utf-8")
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    stream.flush()


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as handle:
        scenario = json.load(handle)
    mode = scenario.get("mode", "ok")
    log_path = scenario.get("log")
    root = ""
    ballast: list[bytearray] = []
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    next_id = 1000

    def log(message: dict) -> None:
        if log_path:
            with open(log_path, "a", encoding="utf-8") as out:
                out.write(json.dumps(message) + "\n")

    def uri(path: str) -> str:
        return "file://" + root + "/" + path

    def rel(document_uri: str) -> str:
        path = unquote(urlparse(document_uri).path)
        return path[len(root) + 1:] if path.startswith(root + "/") else path

    def point(target: dict) -> dict:
        start = {"line": target["line"], "character": target["character"]}
        end = {"line": target["line"], "character": target["character"] + len(target.get("name", "x"))}
        return {"start": start, "end": end}

    if scenario.get("log_env"):
        log({"env": dict(os.environ)})
    while True:
        message = read_message(stdin)
        if message is None:
            return 0
        log(message)
        method = message.get("method")
        if "id" in message and method is None:
            continue  # the client's answer to our own request; logged above
        if method == "initialize":
            root = unquote(urlparse(message["params"]["rootUri"]).path).rstrip("/")
            if mode == "init_error":
                write_message(stdout, {"jsonrpc": "2.0", "id": message["id"],
                                       "error": {"code": -32603, "message": "cannot start"}})
                continue
            write_message(stdout, {"jsonrpc": "2.0", "id": message["id"], "result": {
                "capabilities": {"definitionProvider": True, "callHierarchyProvider": True,
                                 "referencesProvider": True},
                "serverInfo": scenario.get("server_info", {"name": "fake-lsp", "version": "0.0.1"}),
            }})
            continue
        if method == "initialized":
            if mode == "memory":
                ballast.append(bytearray(scenario.get("memory_mib", 256) * 1024 * 1024))
                for page in range(0, len(ballast[0]), 4096):
                    ballast[0][page] = 1
            if scenario.get("ask_config"):
                next_id += 1
                write_message(stdout, {"jsonrpc": "2.0", "id": next_id, "method": "workspace/configuration",
                                       "params": {"items": [{"section": "python.analysis"}]}})
            continue
        if method == "shutdown":
            write_message(stdout, {"jsonrpc": "2.0", "id": message["id"], "result": None})
            continue
        if method == "exit":
            return 0
        if method not in QUERIES:
            if "id" in message:
                write_message(stdout, {"jsonrpc": "2.0", "id": message["id"], "result": None})
            continue
        if mode == "hang":
            continue
        if mode == "crash":
            return 3
        if mode in ("slow", "memory") and scenario.get("delay"):
            time.sleep(scenario["delay"])
        params = message["params"]
        if method == "callHierarchy/incomingCalls":
            item = params["item"]
            key = f"{rel(item['uri'])}:{item['selectionRange']['start']['line']}"
            result = [{
                "from": {"name": c["name"], "kind": 12, "uri": uri(c["path"]),
                         "range": point(c), "selectionRange": point(c)},
                "fromRanges": [{"start": {"line": c["call_line"], "character": 4},
                                "end": {"line": c["call_line"], "character": 8}}],
            } for c in scenario.get("incoming", {}).get(key, [])]
        else:
            document = params["textDocument"]["uri"]
            key = f"{rel(document)}:{params['position']['line']}"
            if method == "textDocument/definition":
                target = scenario.get("definitions", {}).get(key)
                result = None if target is None else [{
                    "targetUri": uri(target["path"]),
                    "targetRange": point(target),
                    "targetSelectionRange": point(target),
                }]
            elif method == "textDocument/prepareCallHierarchy":
                result = None
                if key in scenario.get("incoming", {}):
                    here = {"line": params["position"]["line"], "character": params["position"]["character"]}
                    result = [{"name": "item", "kind": 12, "uri": document,
                               "range": point(here), "selectionRange": point(here)}]
            else:
                result = [{"uri": uri(r["path"]), "range": point(r)}
                          for r in scenario.get("references", {}).get(key, [])]
        write_message(stdout, {"jsonrpc": "2.0", "id": message["id"], "result": result})


if __name__ == "__main__":
    sys.exit(main())
