"""A fake Google project and swarm cluster for `register-tenant.sh --workspace`.

tests/unit/scripts/test_register_tenant_workspace.py links this file as
`gcloud`, `kubectl` and `curl` into a directory that sits on PATH right behind
scripts/lib/guard-bin/, so every call the script (and kubernetes/apply.sh, and
the network parity check it runs) makes is judged by the REAL call guard first
and only then reaches this file, which plays the "real" binary. The world --
accounts, IAM policies, the artifact bucket, secrets, Kubernetes objects and
Firestore documents -- is one JSON file named by $FAKE_WORLD, read and written
on every call. Every call is appended to its `calls` list.

`fail` holds injected failures: {"tool", "words": [...], "times": n, "stderr",
"rc"}. A call whose positional words start with `words` fails `times` times.

Nothing here touches a network or a credential; the token it prints is made
up at runtime.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

import yaml

WORLD = Path(os.environ["FAKE_WORLD"])

GCLOUD_VALUE_FLAGS = {
    "--project", "--format", "--filter", "--role", "--member", "--condition-from-file", "--condition",
    "--display-name", "--description", "--iam-account", "--managed-by", "--replication-policy", "--labels",
    "--location", "--region", "--zone", "--flatten", "--impersonate-service-account",
}
KUBECTL_VALUE_FLAGS = {
    "--context", "-n", "--namespace", "-o", "--output", "-f", "--filename", "--dry-run", "--raw",
    "--field-selector", "--validate", "--request-timeout", "--type", "-p", "--patch",
}
CURL_VALUE_FLAGS = {"-m", "--max-time", "-o", "--output", "-w", "--write-out", "-X", "--request", "-H",
                    "--header", "-K", "--config", "--data-binary"}


def load() -> dict:
    return json.loads(WORLD.read_text())


def save(world: dict) -> None:
    tmp = WORLD.with_suffix(".tmp")
    tmp.write_text(json.dumps(world, indent=1, sort_keys=True))
    tmp.replace(WORLD)


def parse(argv: list[str], value_flags: set[str]) -> tuple[list[str], dict[str, list[str]]]:
    pos: list[str] = []
    flags: dict[str, list[str]] = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith("-") and arg != "-":
            name, eq, value = arg.partition("=")
            if eq:
                flags.setdefault(name, []).append(value)
            elif name in value_flags and i + 1 < len(argv):
                flags.setdefault(name, []).append(argv[i + 1])
                i += 1
            else:
                flags.setdefault(name, []).append("")
        else:
            pos.append(arg)
        i += 1
    return pos, flags


def flag(flags: dict[str, list[str]], *names: str, default: str = "") -> str:
    for name in names:
        if name in flags:
            return flags[name][-1]
    return default


def injected(world: dict, tool: str, pos: list[str]) -> dict | None:
    for rule in world.get("fail", []):
        if rule["tool"] == tool and pos[: len(rule["words"])] == rule["words"] and rule.get("times", 0) > 0:
            rule["times"] -= 1
            return rule
    return None


def bind(policy: dict, role: str, member: str, condition: dict | None = None) -> None:
    for binding in policy.setdefault("bindings", []):
        if binding["role"] == role and binding.get("condition") == condition:
            if member not in binding["members"]:
                binding["members"].append(member)
            return
    entry = {"role": role, "members": [member]}
    if condition is not None:
        entry["condition"] = condition
        policy["version"] = 3
    policy["bindings"].append(entry)


def not_found(what: str) -> int:
    sys.stderr.write(f"ERROR: (gcloud) NOT_FOUND: {what} not found\n")
    return 1


# ---------------------------------------------------------------------------
# gcloud
# ---------------------------------------------------------------------------

def gcloud(world: dict, argv: list[str]) -> int:
    pos, flags = parse(argv, GCLOUD_VALUE_FLAGS)
    project = flag(flags, "--project", default=world["project"])
    fmt = flag(flags, "--format")
    words = pos

    def out(value: object) -> None:
        sys.stdout.write(json.dumps(value) + "\n")

    if words[:2] == ["auth", "print-access-token"]:
        sys.stdout.write("fake-" + secrets.token_hex(12) + "\n")
        return 0
    if words[:2] == ["auth", "list"]:
        sys.stdout.write("swarm-workspace-deployer@" + project + ".iam.gserviceaccount.com\n")
        return 0
    if words[:2] == ["config", "get-value"]:
        sys.stdout.write(project + "\n")
        return 0

    accounts = world["accounts"]
    if words[:2] == ["iam", "service-accounts"]:
        verb = words[2]
        if verb == "create":
            email = f"{words[3]}@{project}.iam.gserviceaccount.com"
            if email in accounts:
                sys.stderr.write("ERROR: ALREADY_EXISTS\n")
                return 1
            accounts[email] = {"policy": {"version": 1, "etag": "BwX="}, "keys": [],
                               "uid": str(100000000000000000000 + len(accounts)),
                               "display_name": flag(flags, "--display-name"),
                               "description": flag(flags, "--description")}
            return 0
        if verb == "keys":
            email = flag(flags, "--iam-account")
            if email not in accounts:
                return not_found(email)
            for key in accounts[email].get("keys", []):
                sys.stdout.write(key + "\n")
            return 0
        email = words[3]
        if email not in accounts:
            return not_found(email)
        account = accounts[email]
        if verb == "describe":
            if "uniqueId" in fmt:
                sys.stdout.write(account["uid"] + "\n")
            elif fmt.startswith("value"):
                sys.stdout.write(email + "\n")
            else:
                out({"email": email, "uniqueId": account["uid"]})
            return 0
        if verb == "get-iam-policy":
            out(account["policy"])
            return 0
        if verb == "add-iam-policy-binding":
            bind(account["policy"], flag(flags, "--role"), flag(flags, "--member"))
            return 0

    if words[:3] == ["storage", "buckets", "get-iam-policy"]:
        out(world["bucket_policy"])
        return 0
    if words[:3] == ["storage", "buckets", "add-iam-policy-binding"]:
        condition = None
        if "--condition-from-file" in flags:
            condition = json.loads(Path(flag(flags, "--condition-from-file")).read_text())
        bind(world["bucket_policy"], flag(flags, "--role"), flag(flags, "--member"), condition)
        return 0
    if words[:2] == ["storage", "cp"]:
        world["objects"][words[3]] = sys.stdin.read()
        return 0

    secrets_ = world["secrets"]
    if words[0] == "secrets":
        verb, name = words[1], words[2]
        if verb == "create":
            if name in secrets_:
                sys.stderr.write("ERROR: ALREADY_EXISTS\n")
                return 1
            labels = dict(pair.split("=", 1) for pair in flag(flags, "--labels").split(",") if pair)
            secrets_[name] = {"labels": labels, "policy": {"version": 1, "etag": "BwX="}}
            return 0
        if name not in secrets_:
            return not_found(name)
        if verb == "describe":
            out({"name": f"projects/{project}/secrets/{name}", "labels": secrets_[name]["labels"]})
            return 0
        if verb == "get-iam-policy":
            out(secrets_[name]["policy"])
            return 0
        if verb == "add-iam-policy-binding":
            bind(secrets_[name]["policy"], flag(flags, "--role"), flag(flags, "--member"))
            return 0

    if words[:3] == ["container", "clusters", "get-credentials"]:
        location = flag(flags, "--location", "--region", "--zone")
        context = f"gke_{project}_{location}_{words[3]}"
        current = world.get("kube_current_context") or context
        Path(os.environ["KUBECONFIG"]).write_text(yaml.safe_dump({
            "apiVersion": "v1", "kind": "Config", "current-context": current,
            "contexts": [{"name": context, "context": {"cluster": context, "user": context}}],
            "clusters": [{"name": context, "cluster": {"server": "https://192.0.2.1"}}],
            "users": [{"name": context, "user": {}}],
        }))
        return 0
    if words[:3] == ["container", "clusters", "describe"]:
        out({"clusterIpv4Cidr": "10.8.0.0/14", "servicesIpv4Cidr": "34.118.224.0/20",
             "addonsConfig": {"dnsCacheConfig": {"enabled": True}}})
        return 0
    if words[:2] == ["projects", "get-iam-policy"]:
        out(world["project_policy"])
        return 0
    if words[:2] == ["projects", "add-iam-policy-binding"]:
        bind(world["project_policy"], flag(flags, "--role"), flag(flags, "--member"))
        return 0
    if words[:3] == ["iam", "roles", "describe"]:
        sys.stdout.write(f"projects/{project}/roles/{words[3]}\n")
        return 0
    sys.stderr.write("fake gcloud: unhandled call " + " ".join(argv) + "\n")
    return 2


# ---------------------------------------------------------------------------
# kubectl
# ---------------------------------------------------------------------------

KIND_WORDS = {
    "namespace": "Namespace", "namespaces": "Namespace", "ns": "Namespace",
    "serviceaccount": "ServiceAccount", "serviceaccounts": "ServiceAccount", "sa": "ServiceAccount",
    "resourcequota": "ResourceQuota",
    "resourcequotas": "ResourceQuota", "limitrange": "LimitRange", "limitranges": "LimitRange",
    "networkpolicy": "NetworkPolicy", "networkpolicies": "NetworkPolicy", "netpol": "NetworkPolicy",
    "role": "Role", "roles": "Role", "rolebinding": "RoleBinding", "rolebindings": "RoleBinding",
}


def key(kind: str, namespace: str, name: str) -> str:
    return f"{kind}/{namespace}/{name}"


def kubectl(world: dict, argv: list[str], stdin: str) -> int:
    pos, flags = parse(argv, KUBECTL_VALUE_FLAGS)
    output = flag(flags, "-o", "--output")
    namespace = flag(flags, "-n", "--namespace")
    objects = world["k8s"]
    if pos[:1] == ["version"]:
        sys.stdout.write("Client Version: v1.35.0\n")
        return 0
    if pos[:2] == ["config", "current-context"]:
        config = yaml.safe_load(Path(os.environ["KUBECONFIG"]).read_text())
        sys.stdout.write(config["current-context"] + "\n")
        return 0
    if pos[:2] == ["config", "view"]:
        config = yaml.safe_load(Path(os.environ["KUBECONFIG"]).read_text())
        sys.stdout.write(config["contexts"][0]["context"]["cluster"])
        return 0
    if pos[:1] == ["get"] and "--raw" in flags:
        if world.get("cluster_down"):
            sys.stderr.write("Unable to connect to the server: dial tcp: i/o timeout\n")
            return 1
        sys.stdout.write("ok")
        return 0
    if pos[:3] == ["get", "service", "kube-dns"]:
        sys.stdout.write("34.118.224.10")
        return 0
    if pos[:3] == ["get", "daemonset", "node-local-dns"]:
        sys.stdout.write(json.dumps({"spec": {"template": {"spec": {"containers": [
            {"name": "node-cache", "args": ["-localip", "169.254.20.10,34.118.224.10"]}]}}}}))
        return 0
    if pos[:1] == ["get"]:
        kind = KIND_WORDS[pos[1]]
        # By name only: the workspace deployer holds `get` and no `list`
        # (kubernetes/rbac/provisioner-rbac.yaml), so a nameless or
        # field-selected get is a call the real cluster refuses.
        if len(pos) < 3 or {"--field-selector", "-l", "--selector", "-A", "--all-namespaces"} & set(flags):
            sys.stderr.write("Error from server (Forbidden): cannot list resource " + pos[1] + "\n")
            return 1
        ns = "" if kind == "Namespace" else namespace
        found = objects.get(key(kind, ns, pos[2]))
        if found is None:
            sys.stderr.write(f'Error from server (NotFound): {pos[1]} "{pos[2]}" not found\n')
            return 1
        sys.stdout.write((f"{pos[1]}/{pos[2]}\n" if output == "name" else json.dumps(found)))
        return 0
    if pos[:1] == ["apply"]:
        dry = flag(flags, "--dry-run")
        docs = [d for d in yaml.safe_load_all(stdin) if d]
        for doc in docs:
            meta = doc.get("metadata") or {}
            kind = doc["kind"]
            ns = "" if kind == "Namespace" else (meta.get("namespace") or namespace)
            if dry == "server" and kind != "Namespace" and key("Namespace", "", ns) not in objects:
                sys.stderr.write(f'Error from server (NotFound): namespaces "{ns}" not found\n')
                return 1
            k = key(kind, ns, meta["name"])
            verdict = "configured" if k in objects else "created"
            if not dry:
                objects[k] = doc
            suffix = {"client": " (dry run)", "server": " (server dry run)"}.get(dry, "")
            sys.stdout.write(f"{kind.lower()}/{meta['name']} {verdict}{suffix}\n")
        return 0
    sys.stderr.write("fake kubectl: unhandled call " + " ".join(argv) + "\n")
    return 2


# ---------------------------------------------------------------------------
# curl: the Firestore REST API
# ---------------------------------------------------------------------------

def set_path(fields: dict, path: list[str], value: dict | None) -> None:
    head = path[0]
    if len(path) == 1:
        if value is None:
            fields.pop(head, None)
        else:
            fields[head] = value
        return
    node = fields.setdefault(head, {"mapValue": {"fields": {}}})
    inner = node.setdefault("mapValue", {}).setdefault("fields", {})
    set_path(inner, path[1:], value)


def get_path(fields: dict, path: list[str]) -> dict | None:
    node = fields.get(path[0])
    if node is None or len(path) == 1:
        return node
    return get_path((node.get("mapValue") or {}).get("fields") or {}, path[1:])


def update(world: dict, path: str, fields: dict, mask: list[str] | None) -> None:
    docs = world["firestore"]
    world["clock"] = world.get("clock", 0) + 1
    doc = docs.setdefault(path, {"fields": {}})
    if mask is None:
        doc["fields"] = fields
    else:
        for field_path in mask:
            parts = field_path.split(".")
            set_path(doc["fields"], parts, get_path(fields, parts))
    doc["updateTime"] = f"2026-10-08T09:00:00.{world['clock']:06d}Z"
    world.setdefault("writes", []).append(path)


def curl(world: dict, argv: list[str]) -> int:
    pos, flags = parse(argv, CURL_VALUE_FLAGS)
    method = flag(flags, "-X", "--request", default="POST" if "--data-binary" in flags else "GET")
    body = json.loads(flag(flags, "--data-binary") or "null")
    url = urlsplit(pos[0])
    base = f"/v1/projects/{world['project']}/databases/{world['database']}/documents"
    docs = world["firestore"]
    status, answer = 200, {}
    full = url.path
    if not full.startswith(base):
        status, answer = 404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "wrong database"}}
    else:
        rest = unquote(full[len(base):])
        query = parse_qsl(url.query)
        rule = injected(world, "curl", [method, rest])
        if rule:
            status, answer = rule.get("status", 503), {"error": {"code": rule.get("status", 503),
                                                                  "status": "UNAVAILABLE", "message": "injected"}}
        elif rest == ":runQuery":
            sq = body["structuredQuery"]
            collection = sq["from"][0]["collectionId"]
            where = (sq.get("where") or {}).get("fieldFilter")
            rows = []
            for path, doc in sorted(docs.items()):
                if path.rsplit("/", 1)[0] != collection:
                    continue
                if where and (doc["fields"].get(where["field"]["fieldPath"]) or {}) != where["value"]:
                    continue
                rows.append({"document": {"name": f"projects/{world['project']}/databases/{world['database']}"
                                                  f"/documents/{path}", **doc}})
            answer = rows or [{"readTime": "2026-10-08T09:00:00Z"}]
        elif rest == ":beginTransaction":
            answer = {"transaction": "dHgx+/=="}
        elif rest == ":rollback":
            world.setdefault("rollbacks", 0)
            world["rollbacks"] += 1
        elif rest == ":commit":
            for write in body["writes"]:
                path = write["update"]["name"].split("/documents/", 1)[1]
                if write.get("currentDocument", {}).get("exists") and path not in docs:
                    status, answer = 404, {"error": {"code": 404, "status": "NOT_FOUND"}}
                    break
                update(world, path, write["update"].get("fields", {}),
                       (write.get("updateMask") or {}).get("fieldPaths"))
        elif rest.startswith("/"):
            path = rest[1:]
            if method == "GET":
                if path in docs:
                    answer = {"name": f"projects/{world['project']}/databases/{world['database']}/documents/{path}",
                              **docs[path]}
                else:
                    status, answer = 404, {"error": {"code": 404, "status": "NOT_FOUND"}}
            elif method == "PATCH":
                mask = [v for k, v in query if k == "updateMask.fieldPaths"] or None
                expected = dict(query).get("currentDocument.updateTime")
                if expected is not None and docs.get(path, {}).get("updateTime") != expected:
                    status, answer = 400, {"error": {"code": 400, "status": "FAILED_PRECONDITION"}}
                else:
                    update(world, path, body.get("fields", {}), mask)
                    answer = docs[path]
            else:
                status, answer = 400, {"error": {"code": 400, "status": "INVALID_ARGUMENT"}}
    out = flag(flags, "-o", "--output")
    text = json.dumps(answer)
    if out:
        Path(out).write_text(text)
    else:
        sys.stdout.write(text)
    if flag(flags, "-w", "--write-out") == "%{http_code}":
        sys.stdout.write(str(status))
    return 0


def main() -> int:
    tool = os.environ.get("FAKE_TOOL") or Path(sys.argv[0]).name
    argv = sys.argv[1:]
    stdin = ""
    if tool == "kubectl" and ("-" in argv or "--filename=-" in argv):
        stdin = sys.stdin.read()
    if tool == "curl" and ("-K" in argv or "--config" in argv):
        sys.stdin.read()
    world = load()
    world.setdefault("calls", []).append({"tool": tool, "argv": argv})
    pos, _ = parse(argv, {"gcloud": GCLOUD_VALUE_FLAGS, "kubectl": KUBECTL_VALUE_FLAGS}.get(tool, CURL_VALUE_FLAGS))
    rule = injected(world, tool, pos) if tool != "curl" else None
    if rule:
        save(world)
        sys.stderr.write(rule.get("stderr", "ERROR: injected failure") + "\n")
        return int(rule.get("rc", 1))
    if tool == "gcloud":
        rc = gcloud(world, argv)
    elif tool == "kubectl":
        rc = kubectl(world, argv, stdin)
    else:
        rc = curl(world, argv)
    save(world)
    return rc


if __name__ == "__main__":
    sys.exit(main())
