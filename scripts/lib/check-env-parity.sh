#!/usr/bin/env bash
# Assert that every environment variable the control plane READS is one the
# deployment actually SETS.
#
# WHY THIS EXISTS
# ---------------
# A Cloud Run environment variable is a string handed across a boundary that
# nothing type-checks. One side writes `WAKE_TOPIC`, the other reads
# `DISPATCH_TOPIC`, and there is no error anywhere -- the reader gets "" and
# takes its "not configured" branch, which is almost always a quiet fallback
# rather than a crash. Five had accumulated by 2026-09-18, found only because
# twelve audit agents were pointed at the repository at once:
#
#   PLATFORM_SERVICE_ACCOUNTS  never set -> the quota sweep 403'd on every tick
#                              for the life of the deployment, silently
#   WAKE_TOPIC/DISPATCH_TOPIC  name mismatch -> the Pub/Sub fast-wake path never
#                              fired; the 60s safety tick hid it as tail latency
#   TENANT_GROUPS/ADMIN_GROUPS never set -> group tenant resolution never
#                              engaged; every caller silently got a personal
#                              tenant, which is working code, just the wrong one
#   PUSH_AUDIENCE, PUSH_SERVICE_ACCOUNT, BROKER_AUDIENCE
#                              never set -> the second auth layer in front of
#                              the admission controller was inert
#   GKE_CA_CERT_PATH/_B64      name mismatch -> all GKE dispatch failed, and the
#                              scheduler's retry loop made it look transient
#
# Every one of them failed OPEN and SILENTLY. That is the shared property worth
# defending against: none of these would ever have produced a red test, an
# alert, or a stack trace. Only a reader comparing two files could see it, and
# nothing made anyone compare them. This is that comparison, run every build.
#
# WHAT IT DOES NOT COVER
# ----------------------
# `apps/agent-worker` is excluded on purpose. Its environment is written by the
# dispatcher at dispatch time (scheduler/dispatch.py, kubernetes/render.py), not
# by terraform, so it is a different contract with a different writer. Extending
# this check to cover that pairing is worth doing and is not what this is.
#
# Run by `make test` and by the `shell` job in .github/workflows/application.yml.
# It needs no cloud credentials and no emulator.
#
# Usage: scripts/lib/check-env-parity.sh

set -euo pipefail
# shellcheck source-path=SCRIPTDIR
# shellcheck source=common.sh
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_cmd python3

step "Environment parity (control plane reads vs deployment sets)"

python3 - "${REPO_ROOT}" <<'PY'
import ast
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])

# terraform's name for a service -> the app that runs in it. swarm_common is
# read by all four, so it counts as part of every one.
SERVICE_APP = {
    "swarm-api":          ["apps/swarm-api", "apps/common"],
    "swarm-scheduler":    ["apps/scheduler", "apps/common"],
    "swarm-quota-broker": ["apps/quota-broker", "apps/common"],
    "swarm-reconciler":   ["apps/reconciler", "apps/common"],
}

# RULE 2's list. A variable here is one whose absence does not fail, but
# disables something the deployment is supposed to have. Every entry is a bug
# that actually shipped -- this list is the scar tissue, not a style guide.
#
# It is curated, and that is a real weakness: nothing adds to it automatically,
# so a NEW silently-optional feature will not be caught until someone puts it
# here. RULE 1 needs no list and is the one doing most of the work.
REQUIRED = {
    "DISPATCH_TOPIC": "without it swarm_api.deps installs NullWaker() and every "
                      "submission waits for the 60s safety tick instead of the "
                      "Pub/Sub fast path",
    "PLATFORM_SERVICE_ACCOUNTS": "the broker derives 'platform' from this list; "
                                 "empty means the Cloud Scheduler tick gets 403 "
                                 "on /v1/quota/sweep, forever and silently",
    "TENANT_GROUPS": "empty means resolve_tenant() has no groups to check and "
                     "every caller silently falls back to a personal u-<email> "
                     "tenant -- working code, wrong tenant",
    "PUSH_SERVICE_ACCOUNT": "empty leaves PushVerifier disabled, so the second "
                            "auth layer in front of the admission controller is "
                            "inert while its comment claims it is on",
    "PUSH_AUDIENCE": "google-auth SKIPS the aud claim entirely when no audience "
                     "is passed, so a token minted for any other service is "
                     "accepted",
    "BROKER_AUDIENCE": "same as PUSH_AUDIENCE, for the quota broker",
    "WORKER_IMAGE_REFS": "without it scheduler.dispatch.image_uri falls back to "
                         "<image>:<WORKER_IMAGE_TAG>, so every GKE Job and every "
                         "job the dispatcher creates runs whatever a tag points "
                         "at when it is pulled -- the mutable-tag path the "
                         "digest map closes",
}

# ---------------------------------------------------------------------------
# What each app reads.
# ---------------------------------------------------------------------------


def env_reads(path):
    """Env keys read by this module, including through its own local helpers.

    A settings module almost always wraps os.environ in a `_csv`/`_int`/`_bool`
    helper, so matching only `os.environ.get` would miss exactly the modules
    most worth checking -- TENANT_GROUPS is read as `_csv("TENANT_GROUPS")`.
    So: any module-level function whose body touches os.environ is itself
    treated as an env reader, and its string-literal first argument is a key.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    helpers = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            dumped = ast.dump(node)
            if "'environ'" in dumped or "'getenv'" in dumped:
                helpers.add(node.name)

    keys = set()

    def literal(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr == "environ":
                key = literal(node.slice)
                if key:
                    keys.add(key)
        elif isinstance(node, ast.Call):
            fn = node.func
            matched = False
            if isinstance(fn, ast.Attribute):
                inner = fn.value
                if fn.attr == "get" and isinstance(inner, ast.Attribute) and inner.attr == "environ":
                    matched = True
                elif fn.attr == "getenv":
                    matched = True
            elif isinstance(fn, ast.Name) and fn.id in helpers:
                matched = True
            if matched and node.args:
                key = literal(node.args[0])
                if key:
                    keys.add(key)
    return keys


def reads_of(app_dirs):
    found = {}
    for rel in app_dirs:
        for py in sorted((root / rel).rglob("*.py")):
            if "/tests/" in str(py) or py.name.startswith("test_"):
                continue
            for key in env_reads(py):
                found.setdefault(key, str(py.relative_to(root)))
    return found


# ---------------------------------------------------------------------------
# What terraform sets, per service. Parsed out of the service_env / common_env
# blocks in infra/locals.tf by brace depth. A name that appears only in a
# comment must NOT count as set, or this check would pass on the very bugs that
# motivated it -- the comments naming WAKE_TOPIC and GKE_CA_CERT_PATH are still
# in the tree on purpose.
# ---------------------------------------------------------------------------
ASSIGN = re.compile(r'^\s*([A-Z][A-Z0-9_]*)\s*=')
BLOCK = re.compile(r'^\s*"(swarm-[a-z-]+)"\s*=\s*merge\(')

locals_tf = (root / "terraform/infra/locals.tf").read_text().splitlines()

per_service = {name: set() for name in SERVICE_APP}
common = set()
current, depth = None, 0
in_common = False

for line in locals_tf:
    stripped = line.lstrip()
    if stripped.startswith("#"):
        continue
    line = re.sub(r'\s+#.*$', '', line)

    if re.match(r'^\s*common_env\s*=\s*merge\(', line):
        in_common, depth = True, line.count("{") - line.count("}")
        continue
    block = BLOCK.match(line)
    if block:
        current = block.group(1)
        depth = line.count("{") - line.count("}")
        continue

    if in_common or current:
        depth += line.count("{") - line.count("}")
        match = ASSIGN.match(line)
        if match:
            (common if in_common else per_service.setdefault(current, set())).add(match.group(1))
        if depth <= 0:
            in_common, current = False, None

# ---------------------------------------------------------------------------
failed = []

# RULE 1: a variable placed in a service's environment must be read by that
# service. This is the sharp one: it needs no list, and dead configuration has
# no legitimate form.
for service, keys in sorted(per_service.items()):
    if service not in SERVICE_APP:
        failed.append(("UNKNOWN SERVICE", service,
                       "locals.tf sets an environment for a service this check "
                       "does not know; add it to SERVICE_APP"))
        continue
    seen = reads_of(SERVICE_APP[service])
    for key in sorted(keys):
        if key not in seen:
            failed.append(("DEAD", f"{service}.{key}",
                           f"terraform sets it, but nothing in "
                           f"{'/'.join(SERVICE_APP[service])} reads it"))

all_reads = {}
for dirs in SERVICE_APP.values():
    all_reads.update(reads_of(dirs))

for key in sorted(common):
    if key not in all_reads:
        failed.append(("DEAD", f"common_env.{key}",
                       "terraform sets it for every service, and no service reads it"))

# RULE 2: a variable whose absence silently disables a feature must be set.
everywhere = set(common)
for keys in per_service.values():
    everywhere |= keys

for key, why in sorted(REQUIRED.items()):
    if key not in everywhere:
        failed.append(("UNSET", key, why))

for kind, what, why in failed:
    print(f"  {kind:8} {what}", file=sys.stderr)
    print(f"           {why}", file=sys.stderr)

counted = len(everywhere)
if failed:
    print(f"\n  {len(failed)} problem(s) across {counted} configured environment "
          f"variables.", file=sys.stderr)
    sys.exit(1)

print(f"  ok   {counted} configured environment variables, each read by the "
      f"service it is set on", file=sys.stderr)
print(f"  ok   {len(REQUIRED)} variables known to fail silently when unset are "
      f"all set", file=sys.stderr)
PY

hr
ok "the deployment sets what the control plane reads, and sets nothing it does not"
