"""`scripts/offboard-tenant.sh` deletes everything a tenant has, and nothing else.

OWNER DECISION, 2026-09-24: when a tenant is offboarded, its artifacts and
checkpoints under `gs://<artifact bucket>/tenants/<id>/`, its Firestore
records, its pools and its tenant document are all deleted at offboarding, so
that a later registration deriving the same tenant id can read nothing of the
old tenant. The script is the one place that deletion happens, and these are
the properties it must keep, each asserted against the REAL script with a fake
`curl` (which IS Firestore here) and a fake `gcloud` (which IS the bucket and
Secret Manager) on PATH, sharing one state file:

  * it refuses a tenant that holds capacity -- a task in LEASED, DISPATCHED,
    STARTING or RUNNING, an unreleased lease, a pool with `active > 0`, an
    account with an agent assigned -- and deletes NOTHING. CONTRACT.md
    invariants 1 and 3: nothing may be deleted under a running worker, and a
    deleted unreleased lease is capacity no pool ever gets back;
  * a dry run, the default, inventories and deletes nothing;
  * the typed confirmation is required even with SWARM_ASSUME_YES set
    (CLAUDE.md: anything destructive must ignore it);
  * a deletion that did not land is found by the proof and exits non-zero,
    with the tenant document kept so the id stays held and the run repeats;
  * a real run removes every record and object version of `eng` and leaves
    every one of `eng-x` alone -- the tenant whose id `eng` is a prefix of;
  * events are found by their own `tenant_id`, across every task, and a task
    is never deleted while an event is left under it -- so a run after a
    failed proof finds what the first one left instead of printing 0 over
    events no task listing can reach any more;
  * the publication ledger is listed from the ledger itself, so an entry
    whose secret is already gone is still found.

The two refusal guards are also MUTATED in-test: the same assertions run
against a copy of the script with the guard removed, and must then see the
deletion happen. A refusal test that would also pass against a script that
never deletes anything proves nothing; these prove the harness can see a
deletion, and that the guard is what prevents it.

WHAT THIS CANNOT PROVE: that the real Firestore REST API and `gcloud` answer
in the shapes the fakes serve. The `gcloud storage ls --long` TOTAL line and
its "matched no objects" error were read from the live artifact bucket on
2026-09-24 (read-only). The Firestore request and response shapes are the ones
`scripts/lib/common.sh` already speaks. The collection-group queries on
`events` were run read-only against the live `swarm` database on 2026-09-24,
for a tenant id that does not exist: without `orderBy at DESC` a runQuery and
a count both answer 400 FAILED_PRECONDITION, with it both answer 200, and a
`startAt` cursor of `[at, __name__]` is accepted. The fake refuses the same
shapes. That no result came back means the cursor's PAGING over real rows was
not observed; and `gcloud secrets list --filter='labels.tenant:*'
--format='value(name.basename(),labels.tenant)'` was read the same day and
prints TAB-separated columns, as the fake does.
"""

from __future__ import annotations

import json
import os
import pty
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT_REL = Path("scripts") / "offboard-tenant.sh"
PROJECT = "swarm-test-project"
DATABASE = "swarm"
BUCKET = "swarm-artifacts-swarm-test-project"
TENANT = "eng"
#: `eng` is a string prefix of this id, so every prefix-shaped mistake --
#: `tenants/eng` without its slash, `startswith("tenant:eng")`, a secret name
#: prefix -- reaches this tenant's data.
NEIGHBOUR = "eng-x"
DOCS = f"projects/{PROJECT}/databases/{DATABASE}/documents"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="offboard-tenant.sh needs bash and jq",
)


# ---------------------------------------------------------------------------
# The fakes
# ---------------------------------------------------------------------------

#: Firestore, over the four curl shapes common.sh uses:
#:   fs_request          -K - -X M [-H ...] [--data-binary B] -o FILE -w '%{http_code}' URL
#:   fs_database_exists  -H "Authorization: ..." URL        (body on stdout)
#: FAKE_DROP_DELETES=<collection,...>: `commit` answers 200 but those deletes
#: do not land -- the failure the proof exists to catch.
FAKE_CURL = r'''#!{python} -S
import json, os, sys
from functools import cmp_to_key
from urllib.parse import urlsplit

args = sys.argv[1:]
VALUED = {"-m", "--max-time", "-K", "--config", "-X", "--request", "-H", "--header",
          "--data-binary", "-d", "--data", "-o", "--output", "-w", "--write-out",
          "--data-urlencode"}
opts, url, i = {}, None, 0
while i < len(args):
    a = args[i]
    if a in VALUED:
        opts[a] = args[i + 1]
        i += 2
        continue
    if a.startswith("http"):
        url = a
    i += 1
if opts.get("-K") == "-":
    sys.stdin.read()
method = opts.get("-X", "GET")
body = opts.get("--data-binary")

STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    state = json.load(fh)
docs = state["docs"]
ROOT = "https://firestore.googleapis.com/v1/"
path = urlsplit(url).path[len("/v1/"):]
db = "projects/%s/databases/%s" % (os.environ["FAKE_PROJECT"], os.environ["FAKE_DATABASE"])
base = db + "/documents"
drop = set(filter(None, os.environ.get("FAKE_DROP_DELETES", "").split(",")))


def log(kind, **kw):
    with open(os.environ["FAKE_FS_LOG"], "a") as fh:
        fh.write(json.dumps(dict(kind=kind, **kw)) + "\n")


def save():
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


def reply(status, payload):
    text = json.dumps(payload)
    if "-o" in opts:
        with open(opts["-o"], "w") as fh:
            fh.write(text)
        if "http_code" in opts.get("-w", ""):
            sys.stdout.write(str(status))
    else:
        sys.stdout.write(text)
    sys.exit(0)


def children(parent, collection):
    prefix = (parent + "/" if parent else "") + collection + "/"
    return [p for p in sorted(docs) if p.startswith(prefix) and "/" not in p[len(prefix):]]


def matches(fields, flt):
    if not flt:
        return True
    if "compositeFilter" in flt:
        assert flt["compositeFilter"]["op"] == "AND", flt
        return all(matches(fields, f) for f in flt["compositeFilter"]["filters"])
    if "fieldFilter" in flt:
        f = flt["fieldFilter"]
        cur = fields.get(f["field"]["fieldPath"])
        if f["op"] == "EQUAL":
            return cur == f["value"]
        if f["op"] == "ARRAY_CONTAINS":
            return cur is not None and f["value"] in cur.get("arrayValue", {}).get("values", [])
        raise SystemExit("fake firestore: unsupported op " + f["op"])
    if "unaryFilter" in flt:
        f = flt["unaryFilter"]
        name = f["field"]["fieldPath"]
        assert f["op"] == "IS_NULL", f
        return name in fields and fields[name] == {"nullValue": None}
    raise SystemExit("fake firestore: unsupported filter " + json.dumps(flt))


#: Firestore's cross-type order, for the value types these tests store.
TYPE_ORDER = ["nullValue", "booleanValue", "integerValue", "doubleValue", "timestampValue",
              "stringValue", "referenceValue"]


def value_key(v):
    for rank, kind in enumerate(TYPE_ORDER):
        if kind in v:
            x = v[kind]
            if kind == "nullValue":
                x = 0
            elif kind == "integerValue":
                x = int(x)
            return (rank, x)
    raise SystemExit("fake firestore: cannot order on " + json.dumps(v))


def field_value(p, path):
    if path == "__name__":
        return {"referenceValue": base + "/" + p}
    return docs[p].get("fields", {})[path]


def compare(a, b, dirs):
    for x, y, d in zip(a, b, dirs):
        kx, ky = value_key(x), value_key(y)
        if kx != ky:
            c = -1 if kx < ky else 1
            return -c if d == "DESCENDING" else c
    return 0


#: Measured read-only against the live `swarm` database on 2026-09-24: a
#: collection-group query on `events` filtered on tenant_id -- a runQuery or a
#: count -- is refused with FAILED_PRECONDITION ("The query requires a
#: COLLECTION_GROUP_ASC index for collection events and field tenant_id")
#: unless it orders by `at` DESCENDING. Single-field indexes are kept for
#: COLLECTION scope only, so the one index that serves it is terraform's
#: `events-tenant-at` (tenant_id ASC, at DESC, __name__ DESC). The fake refuses
#: the same queries the database does.
def cg_index_ok(q):
    if not q.get("where") and not q.get("orderBy"):
        return True
    f = (q.get("where") or {}).get("fieldFilter", {})
    if f.get("field", {}).get("fieldPath") != "tenant_id" or f.get("op") != "EQUAL":
        return False
    order = [(o["field"]["fieldPath"], o.get("direction", "ASCENDING")) for o in q.get("orderBy", [])]
    return order in ([("at", "DESCENDING")], [("at", "DESCENDING"), ("__name__", "DESCENDING")])


def run_query(parent, q):
    (frm,) = q["from"]
    coll = frm["collectionId"]
    if frm.get("allDescendants"):
        if not cg_index_ok(q):
            reply(400, [{"error": {"code": 400, "status": "FAILED_PRECONDITION",
                                   "message": "The query requires a COLLECTION_GROUP_ASC index for "
                                              "collection %s and field tenant_id." % coll}}])
        prefix = parent + "/" if parent else ""
        paths = [p for p in docs if p.startswith(prefix) and p.split("/")[-2] == coll]
    else:
        paths = children(parent, coll)
    order = [(o["field"]["fieldPath"], o.get("direction", "ASCENDING")) for o in q.get("orderBy", [])]
    # A document that lacks a field the query orders by is not in the result.
    found = [p for p in paths
             if matches(docs[p].get("fields", {}), q.get("where"))
             and all(f == "__name__" or f in docs[p].get("fields", {}) for f, _ in order)]
    if not any(f == "__name__" for f, _ in order):
        order.append(("__name__", order[-1][1] if order else "ASCENDING"))
    dirs = [d for _, d in order]

    def key(p):
        return [field_value(p, f) for f, _ in order]

    rows = sorted(found, key=cmp_to_key(lambda a, b: compare(key(a), key(b), dirs)))
    start = q.get("startAt")
    if start:
        cur = start["values"]
        if start.get("before"):
            rows = [p for p in rows if compare(key(p)[: len(cur)], cur, dirs) >= 0]
        else:
            rows = [p for p in rows if compare(key(p)[: len(cur)], cur, dirs) > 0]
    if "limit" in q:
        rows = rows[: q["limit"]]
    return [(base + "/" + p, p) for p in rows]


def project(fields, q):
    sel = q.get("select")
    if not sel:
        return fields
    keep = {f["fieldPath"] for f in sel["fields"]} - {"__name__"}
    return {k: v for k, v in fields.items() if k in keep}


if path == db:
    log("database")
    # As `gcloud firestore databases describe --database=swarm` read it on
    # 2026-09-24: point-in-time recovery on, seven days of versions.
    reply(200, {"name": db, "pointInTimeRecoveryEnablement": "POINT_IN_TIME_RECOVERY_ENABLED",
                "versionRetentionPeriod": "604800s"})

if not path.startswith(base):
    reply(404, {"error": {"code": 404, "message": "fake: unknown path " + path}})
rest = path[len(base):].lstrip("/")

for verb in (":runQuery", ":runAggregationQuery", ":commit"):
    if rest.endswith(verb) or (rest == "" and path.endswith(verb)):
        parent = rest[: -len(verb)] if rest.endswith(verb) else ""
        payload = json.loads(body)
        if verb == ":runQuery":
            q = payload["structuredQuery"]
            rows = run_query(parent, q)
            log("query", parent=parent, collection=q["from"][0]["collectionId"], n=len(rows),
                group=bool(q["from"][0].get("allDescendants")))
            out = [{"document": {"name": n, "fields": project(docs[p].get("fields", {}), q)},
                    "readTime": "2026-09-24T00:00:00Z"} for n, p in rows]
            reply(200, out or [{"readTime": "2026-09-24T00:00:00Z"}])
        if verb == ":runAggregationQuery":
            q = dict(payload["structuredAggregationQuery"]["structuredQuery"])
            q.pop("limit", None)
            n = len(run_query(parent, q))
            log("count", parent=parent, collection=q["from"][0]["collectionId"], n=n,
                group=bool(q["from"][0].get("allDescendants")))
            reply(200, [{"result": {"aggregateFields": {"n": {"integerValue": str(n)}}},
                         "readTime": "2026-09-24T00:00:00Z"}])
        if verb == ":commit":
            names = []
            for w in payload["writes"]:
                assert set(w) == {"delete"}, w
                name = w["delete"]
                assert name.startswith(base + "/"), name
                rel = name[len(base) + 1:]
                names.append(rel)
                segs = rel.split("/")
                collection = segs[-2]
                if collection not in drop:
                    docs.pop(rel, None)
            save()
            log("commit", deletes=names)
            reply(200, {"writeResults": [{} for _ in names], "commitTime": "2026-09-24T00:00:00Z"})

segs = rest.split("/") if rest else []
if len(segs) % 2 == 0 and segs:
    if method == "GET":
        log("get", doc=rest)
        if rest in docs:
            reply(200, {"name": base + "/" + rest, "fields": docs[rest].get("fields", {})})
        reply(404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "no " + rest}})
    if method == "DELETE":
        log("delete", doc=rest)
        segs2 = rest.split("/")
        if segs2[-2] not in drop:
            docs.pop(rest, None)
        save()
        reply(200, {})
    if method == "PATCH":
        log("patch", doc=rest)
        reply(500, {"error": {"code": 500, "message": "fake: offboarding must not PATCH"}})
if len(segs) % 2 == 1 and method == "GET":
    log("list", collection=rest)
    out = [{"name": base + "/" + p, "fields": docs[p].get("fields", {})} for p in children("", rest)]
    reply(200, {"documents": out} if out else {})

reply(400, {"error": {"code": 400, "message": "fake firestore: unhandled %s %s" % (method, path)}})
'''

#: `gcloud storage ls|rm` over a versioned, soft-deleting bucket, and
#: `gcloud secrets list`. The rm follows `gcloud storage rm --help`: a
#: wildcard alone removes LIVE versions only; --recursive or --all-versions
#: removes every version; and --recursive on a bare bucket URL deletes the
#: bucket, which the fake records so a test can refuse it.
FAKE_GCLOUD = r'''#!{python} -S
import json, os, sys

args = sys.argv[1:]
STATE = os.environ["FAKE_STATE"]
with open(STATE) as fh:
    state = json.load(fh)


def log(**kw):
    with open(os.environ["FAKE_GCLOUD_LOG"], "a") as fh:
        fh.write(json.dumps(kw) + "\n")


def save():
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


def url_of():
    for a in args:
        if a.startswith("gs://"):
            return a
    raise SystemExit("fake gcloud: no gs:// url in " + " ".join(args))


def split(url):
    bucket, _, rest = url[len("gs://"):].partition("/")
    return bucket, rest


if "print-access-token" in args:
    print("ya29.fake-token-for-offboarding-tests")
    sys.exit(0)

if args[:2] == ["storage", "ls"]:
    url = url_of()
    bucket, pattern = split(url)
    log(cmd="ls", url=url, soft="--soft-deleted" in args)
    if os.environ.get("FAKE_GCS_LS_ERROR"):
        print("ERROR: (gcloud.storage.ls) HTTPError 403: caller does not have "
              "storage.objects.list access to the Google Cloud Storage bucket.", file=sys.stderr)
        sys.exit(1)
    assert pattern.endswith("**"), pattern
    prefix = pattern[:-2]
    pool = state["gcs_soft"] if "--soft-deleted" in args else state["gcs"]
    objs = [o for o in pool.get(bucket, []) if o["name"].startswith(prefix)
            and ("--all-versions" in args or "--soft-deleted" in args or o["live"])]
    if not objs:
        print("ERROR: (gcloud.storage.ls) One or more URLs matched no objects.", file=sys.stderr)
        sys.exit(1)
    for o in objs:
        print("%10d  2026-09-23T20:13:30Z  gs://%s/%s#%s  metageneration=1"
              % (o["size"], bucket, o["name"], o["generation"]))
    total = sum(o["size"] for o in objs)
    print("TOTAL: %d objects, %d bytes (%.2fkiB)" % (len(objs), total, total / 1024))
    sys.exit(0)

if args[:2] == ["storage", "rm"]:
    url = url_of()
    bucket, prefix = split(url)
    all_versions = bool({"--recursive", "-r", "-R", "--all-versions", "-a"} & set(args))
    log(cmd="rm", url=url, all_versions=all_versions)
    if prefix == "" and all_versions:
        state["bucket_deleted"] = bucket
        state["gcs"][bucket] = []
        save()
        sys.exit(0)
    if prefix.endswith("**"):
        prefix = prefix[:-2]
    hit = [o for o in state["gcs"].get(bucket, []) if o["name"].startswith(prefix)
           and (all_versions or o["live"])]
    if not hit:
        print("ERROR: (gcloud.storage.rm) The following URLs matched no objects or files:\n-" + url,
              file=sys.stderr)
        sys.exit(1)
    print("Removing objects:", file=sys.stderr)
    for o in hit:
        print("Removing gs://%s/%s#%s..." % (bucket, o["name"], o["generation"]), file=sys.stderr)
    if not os.environ.get("FAKE_RM_KEEPS"):
        state["gcs"][bucket] = [o for o in state["gcs"][bucket] if o not in hit]
        state["gcs_soft"].setdefault(bucket, []).extend(hit)
        save()
    sys.exit(0)

if args[:2] == ["secrets", "list"]:
    # `--format=value(a,b)` prints the columns TAB-separated, as the real one
    # did for `value(name.basename(),labels.tenant)` on 2026-09-24.
    flt, fmt = "", "value(name.basename())"
    for i, a in enumerate(args):
        if a.startswith("--filter="):
            flt = a.split("=", 1)[1]
        elif a == "--filter":
            flt = args[i + 1]
        elif a.startswith("--format="):
            fmt = a.split("=", 1)[1]
        elif a == "--format":
            fmt = args[i + 1]
    log(cmd="secrets-list", filter=flt)
    if flt == "labels.tenant:*":
        chosen = [s for s in state["secrets"] if "tenant" in s["labels"]]
    elif flt.startswith("labels.tenant="):
        chosen = [s for s in state["secrets"] if s["labels"].get("tenant") == flt.split("=", 1)[1]]
    else:
        raise SystemExit("fake gcloud: unsupported secrets filter " + flt)
    assert fmt.startswith("value(") and fmt.endswith(")"), fmt
    cols = [c.strip() for c in fmt[len("value("):-1].split(",")]
    for s in chosen:
        out = []
        for c in cols:
            if c == "name.basename()":
                out.append(s["name"])
            elif c.startswith("labels."):
                out.append(s["labels"].get(c[len("labels."):], ""))
            else:
                raise SystemExit("fake gcloud: unsupported secrets column " + c)
        print("\t".join(out))
    sys.exit(0)

print("fake gcloud: unhandled " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


# ---------------------------------------------------------------------------
# The world: tenant `eng`, and its neighbour `eng-x`
# ---------------------------------------------------------------------------

def s(v):
    return {"stringValue": v}


def i(n):
    return {"integerValue": str(n)}


def b(v):
    return {"booleanValue": v}


def ts(v="2026-09-23T20:13:30Z"):
    return {"timestampValue": v}


NULL = {"nullValue": None}


def arr(*vals):
    return {"arrayValue": {"values": [s(v) for v in vals]}} if vals else {"arrayValue": {}}


def _obj(name, gen, size, live=True):
    return {"name": name, "generation": str(gen), "size": size, "live": live}


def world(**over) -> dict:
    """Two tenants whose ids share a prefix, each with one of everything.

    `eng` is drained and disabled -- every task terminal or pending, every lease
    released, every pool idle -- which is the state the runbook brings a tenant
    to before this script. `eng-x` is busy, on purpose: a RUNNING task, an
    unreleased lease and a pool with active capacity, none of which may stop
    `eng`'s offboarding or be touched by it."""
    docs = {
        f"tenants/{TENANT}": {"fields": {"tenant_id": s(TENANT), "kind": s("group"),
                                         "enabled": b(False), "principal": s("eng@saga.xyz")}},
        f"tenants/{NEIGHBOUR}": {"fields": {"tenant_id": s(NEIGHBOUR), "enabled": b(True)}},
        "tasks/task_e1": {"fields": {"tenant_id": s(TENANT), "state": s("SUCCEEDED")}},
        "tasks/task_e2": {"fields": {"tenant_id": s(TENANT), "state": s("PARKED")}},
        "tasks/task_x1": {"fields": {"tenant_id": s(NEIGHBOUR), "state": s("RUNNING")}},
        # Every writer of an event sets `at` (scheduler, API, worker and
        # reconciler stores). ev1 and ev2 share one, so a listing paginated on
        # `at` alone would skip one of them at a page boundary.
        "tasks/task_e1/events/ev1": {"fields": {"tenant_id": s(TENANT), "at": ts("2026-09-23T20:13:30Z")}},
        "tasks/task_e1/events/ev2": {"fields": {"tenant_id": s(TENANT), "at": ts("2026-09-23T20:13:30Z")}},
        "tasks/task_e2/events/ev3": {"fields": {"tenant_id": s(TENANT), "at": ts("2026-09-23T21:00:00Z")}},
        "tasks/task_x1/events/evx": {"fields": {"tenant_id": s(NEIGHBOUR), "at": ts("2026-09-23T20:13:30Z")}},
        "attempts/att_e1": {"fields": {"tenant_id": s(TENANT)}},
        "attempts/att_x1": {"fields": {"tenant_id": s(NEIGHBOUR)}},
        "leases/lease_e1": {"fields": {"tenant_id": s(TENANT), "released_at": ts()}},
        "leases/lease_x1": {"fields": {"tenant_id": s(NEIGHBOUR), "released_at": NULL}},
        "workflows/wf_e1": {"fields": {"tenant_id": s(TENANT)}},
        "workflows/wf_x1": {"fields": {"tenant_id": s(NEIGHBOUR)}},
        f"quota/anthropic:{TENANT}": {"fields": {"tenant_id": s(TENANT)}},
        f"quota/anthropic:{NEIGHBOUR}": {"fields": {"tenant_id": s(NEIGHBOUR)}},
        # The GET /v1/outcomes rollup (#185). The neighbour's id shares the
        # prefix, so a delete by document-id prefix would take eng-x's too.
        f"outcome_days/{TENANT}_2026-09-24": {"fields": {"tenant_id": s(TENANT), "day": s("2026-09-24")}},
        f"outcome_days/{NEIGHBOUR}_2026-09-24": {"fields": {"tenant_id": s(NEIGHBOUR),
                                                            "day": s("2026-09-24")}},
        f"accounts/{TENANT}:main": {"fields": {"owner_tenant": s(TENANT), "assigned": i(0), "lend_to": arr()}},
        f"accounts/{NEIGHBOUR}:main": {"fields": {"owner_tenant": s(NEIGHBOUR), "assigned": i(1),
                                                  "lend_to": arr()}},
        "account_auth/state_e1": {"fields": {"owner_tenant": s(TENANT)}},
        "account_auth/state_x1": {"fields": {"owner_tenant": s(NEIGHBOUR)}},
        f"credential_publications/swarm-tenant-{TENANT}-anthropic": {"fields": {"digest": s("d1")}},
        f"credential_publications/swarm-tenant-{NEIGHBOUR}-anthropic": {"fields": {"digest": s("d2")}},
        "pools/global": {"fields": {"name": s("global"), "active": i(3)}},
        f"pools/tenant:{TENANT}": {"fields": {"name": s(f"tenant:{TENANT}"), "active": i(0)}},
        f"pools/provider:anthropic:tenant:{TENANT}": {"fields": {"active": i(0)}},
        f"pools/tenant:{NEIGHBOUR}": {"fields": {"active": i(1)}},
        f"pools/provider:anthropic:tenant:{NEIGHBOUR}": {"fields": {"active": i(1)}},
        "control/dispatch": {"fields": {"paused": b(False)}},
    }
    gcs = [
        _obj(f"tenants/{TENANT}/.tenant", 1, 12),
        _obj(f"tenants/{TENANT}/tasks/task_e1/attempts/att_e1/checkpoints/ckpt-00001/archive.tar.gz", 2, 4000),
        # A NONCURRENT version: as readable by the tenant's IAM grant as a live
        # one, and invisible to an rm that is not told --all-versions.
        _obj(f"tenants/{TENANT}/tasks/task_e1/attempts/att_e1/checkpoints/ckpt-00001/archive.tar.gz", 1, 3000,
             live=False),
        _obj(f"tenants/{NEIGHBOUR}/tasks/task_x1/attempts/att_x1/logs/stdout.log", 5, 70),
        _obj("backups/purge-20260901T000000Z/all.export", 9, 100),
    ]
    secrets = [
        {"name": f"swarm-tenant-{TENANT}-anthropic", "labels": {"tenant": TENANT}},
        {"name": f"swarm-tenant-{TENANT}-anthropic-refresh", "labels": {"tenant": TENANT}},
        {"name": f"swarm-tenant-{NEIGHBOUR}-anthropic", "labels": {"tenant": NEIGHBOUR}},
    ]
    state = {"docs": docs, "gcs": {BUCKET: gcs}, "gcs_soft": {}, "secrets": secrets}
    for key, value in over.items():
        if value is None:
            state["docs"].pop(key, None)
        else:
            state["docs"][key] = value
    return state


def _eng_docs(state: dict) -> list[str]:
    """Every document path that belongs to `eng`, by the rule the script is
    meant to follow -- written out here, not derived from the script."""
    out = []
    for path, doc in state["docs"].items():
        f = doc.get("fields", {})
        if f.get("tenant_id") == s(TENANT) or f.get("owner_tenant") == s(TENANT):
            out.append(path)
        elif path in (f"tenants/{TENANT}", f"pools/tenant:{TENANT}",
                      f"pools/provider:anthropic:tenant:{TENANT}",
                      f"credential_publications/swarm-tenant-{TENANT}-anthropic"):
            out.append(path)
        elif path.startswith(("tasks/task_e1/", "tasks/task_e2/")):
            out.append(path)
    return sorted(out)


def _not_eng(state: dict) -> dict:
    keep = set(_eng_docs(state))
    return {p: d for p, d in state["docs"].items() if p not in keep}


def _eng_objects(state: dict) -> list[dict]:
    return [o for o in state["gcs"][BUCKET] if o["name"].startswith(f"tenants/{TENANT}/")]


# ---------------------------------------------------------------------------
# The harness
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, proc_rc, stderr, state, fs_log, gcloud_log):
        self.rc = proc_rc
        self.stderr = stderr
        self.state = state
        self.fs_log = fs_log
        self.gcloud_log = gcloud_log

    @property
    def writes(self) -> list[dict]:
        """Everything that changed the database or the bucket."""
        fs = [e for e in self.fs_log if e["kind"] in ("commit", "delete", "patch")]
        gc = [e for e in self.gcloud_log if e.get("cmd") == "rm"]
        return fs + gc

    def deleted_docs(self) -> list[str]:
        out = []
        for e in self.fs_log:
            if e["kind"] == "commit":
                out.extend(e["deletes"])
            elif e["kind"] == "delete":
                out.append(e["doc"])
        return out


def _run(tmp_path: Path, state: dict, *args: str, typed: str | None = None,
         mutate: tuple[str, str] | None = None, **env_extra: str) -> Run:
    # A subdirectory per run, so one test can run the script twice over the
    # state the first run left.
    tmp_path.mkdir(parents=True, exist_ok=True)
    root = tmp_path / "repo"
    shutil.copytree(REPO / "scripts", root / "scripts")
    script = root / SCRIPT_REL
    assert script.exists(), f"{SCRIPT_REL} does not exist"
    if mutate is not None:
        old, new = mutate
        text = script.read_text()
        assert text.count(old) == 1, (
            f"the mutation target {old!r} occurs {text.count(old)} time(s) in the script; "
            "the guard this test mutates has moved, so the mutant would prove nothing"
        )
        script.write_text(text.replace(old, new))

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("curl", FAKE_CURL), ("gcloud", FAKE_GCLOUD)):
        path = bindir / name
        path.write_text(body.replace("{python}", sys.executable))
        path.chmod(0o755)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps(state))
    fs_log = tmp_path / "fs.jsonl"
    gcloud_log = tmp_path / "gcloud.jsonl"
    home = tmp_path / "home"
    home.mkdir()

    env = {k: v for k, v in os.environ.items()
           if k not in ("SWARM_ASSUME_YES", "K_SERVICE", "CLOUD_RUN_JOB", "SWARM_IMPERSONATE_SA",
                        "ARTIFACT_BUCKET", "FIRESTORE_DATABASE", "ENVIRONMENT", "PROJECT_ID")}
    env.update({
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARM_ENV_FILE": str(tmp_path / "no-such.env"),
        "PROJECT_ID": PROJECT,
        "REGION": "us-central1",
        "ENVIRONMENT": "dev",
        "FIRESTORE_DATABASE": DATABASE,
        "ARTIFACT_BUCKET": BUCKET,
        "NO_COLOR": "1",
        "TMPDIR": str(tmp_path),
        "FAKE_STATE": str(state_file),
        "FAKE_PROJECT": PROJECT,
        "FAKE_DATABASE": DATABASE,
        "FAKE_FS_LOG": str(fs_log),
        "FAKE_GCLOUD_LOG": str(gcloud_log),
        **env_extra,
    })
    cmd = ["bash", str(script), *args]

    if typed is None:
        proc = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=180, check=False)
        rc, stderr = proc.returncode, proc.stderr
    else:
        # A real terminal on stdin, so `[[ -t 0 ]]` is true and the answer is
        # TYPED -- the one thing the confirmation accepts.
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(cmd, env=env, stdin=slave, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            os.close(slave)
            os.write(master, (typed + "\n").encode())
            _, stderr = proc.communicate(timeout=180)
            rc = proc.returncode
        finally:
            os.close(master)

    def lines(p: Path) -> list[dict]:
        return [json.loads(x) for x in p.read_text().splitlines() if x] if p.exists() else []

    return Run(rc, stderr, json.loads(state_file.read_text()), lines(fs_log), lines(gcloud_log))


def _row(text: str, label: str) -> str:
    """The value printed after LABEL in the first table row that has it, or ''.

    Spaces and tabs only between label and value, never a newline, so a label
    at the end of one line cannot borrow the next line's value."""
    m = re.search(r"^[ \t]+" + re.escape(label) + r"[ \t]+(\S.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def _section(text: str, anchor: str) -> str:
    """From ANCHOR (a table's own `== ...` header) to the next `== ` heading.

    The capacity table has `tasks RUNNING` rows and the inventory a `tasks`
    row; reading each inside its own table keeps one from answering for the
    other."""
    assert anchor in text, f"no {anchor!r} section in the output:\n{text[-3000:]}"
    start = text.index(anchor)
    end = text.find("\n== ", start + len(anchor))
    return text[start:] if end < 0 else text[start:end]


# ---------------------------------------------------------------------------
# 1. A tenant that holds capacity is refused, and nothing is deleted
# ---------------------------------------------------------------------------

#: Every way `eng` can hold capacity. Each is added to an otherwise drained
#: world, so the refusal can only be about that one thing.
HOLDS_CAPACITY = {
    "task-running": ("tasks/task_e3", {"fields": {"tenant_id": s(TENANT), "state": s("RUNNING")}},
                     "tasks RUNNING"),
    "task-leased": ("tasks/task_e3", {"fields": {"tenant_id": s(TENANT), "state": s("LEASED")}},
                    "tasks LEASED"),
    "lease-unreleased": ("leases/lease_e2", {"fields": {"tenant_id": s(TENANT), "released_at": NULL}},
                         "unreleased leases"),
    # A lease with NO released_at field: IS_NULL does not match it server side.
    "lease-without-release-field": ("leases/lease_e2", {"fields": {"tenant_id": s(TENANT)}},
                                    "unreleased leases"),
    "pool-active": (f"pools/tenant:{TENANT}", {"fields": {"active": i(2)}}, "pools with active > 0"),
    "account-assigned": (f"accounts/{TENANT}:main",
                         {"fields": {"owner_tenant": s(TENANT), "assigned": i(1), "lend_to": arr()}},
                         "accounts assigned"),
}


@pytest.mark.parametrize("case", sorted(HOLDS_CAPACITY))
def test_a_tenant_holding_capacity_is_refused_and_nothing_is_deleted(tmp_path, case):
    doc, value, label = HOLDS_CAPACITY[case]
    before = world(**{doc: value})
    # --apply, with the id typed at a terminal: every other gate is open, so
    # the capacity check is the only thing standing between this run and the
    # delete.
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed=TENANT)

    assert run.rc != 0, run.stderr[-3000:]
    assert not run.writes, f"deleted under a tenant holding capacity ({case}): {run.writes}"
    assert run.state == before, "the state changed although the run was refused"
    assert f"refusing to offboard {TENANT}" in run.stderr, run.stderr[-3000:]
    capacity = _section(run.stderr, "== Capacity held by")
    assert _row(capacity, label).startswith("1"), (
        f"the capacity table does not show the one {label!r} this case added:\n{capacity}"
    )
    assert "(7 capacity checks)" in capacity, "the capacity table did not visit every check"


def test_the_capacity_refusal_is_what_stops_the_delete(tmp_path):
    """The same run against a copy of the script with the refusal removed. It
    must now delete -- a running task's record included -- or the test above
    would pass against a script that simply never deletes anything."""
    doc, value, _ = HOLDS_CAPACITY["task-running"]
    run = _run(tmp_path, world(**{doc: value}), "--tenant", TENANT, "--apply", typed=TENANT,
               mutate=('  if [[ "${held}" -ne 0 ]]; then', "  if false; then"))
    assert "tasks/task_e3" in run.deleted_docs(), (
        "with the capacity refusal removed the running task's record was still not deleted, "
        f"so the refusal test cannot tell a guard from a script that deletes nothing:\n{run.stderr[-3000:]}"
    )


# ---------------------------------------------------------------------------
# 2. A dry run -- the default -- inventories and deletes nothing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--tenant", TENANT], ["--tenant", TENANT, "--dry-run"]],
                         ids=["default", "explicit"])
def test_a_dry_run_inventories_everything_and_deletes_nothing(tmp_path, argv):
    before = world()
    # A terminal with the id already typed: if the dry run ever reached the
    # confirmation, the answer would be waiting for it.
    run = _run(tmp_path, before, *argv, typed=TENANT)

    assert run.rc == 0, run.stderr[-3000:]
    assert not run.writes, f"a dry run wrote: {run.writes}"
    assert run.state == before, "a dry run changed the state"
    assert "dry run complete: nothing was deleted" in run.stderr, run.stderr[-3000:]

    # The inventory counted THIS tenant: not the neighbour, not the prefix, and
    # the noncurrent checkpoint version too.
    inv = _section(run.stderr, "== Inventory:")
    objects = _eng_objects(before)
    assert _row(inv, f"gs://{BUCKET}/tenants/{TENANT}/") == (
        f"{len(objects)} object version(s), {sum(o['size'] for o in objects)} byte(s)"
    ), inv
    assert _row(inv, "tasks") == "2", inv
    assert _row(inv, "task events").startswith("3 "), inv
    for label in ("attempts", "workflows", "quota", "outcome_days", "accounts (owned)", "account_auth"):
        assert _row(inv, label) == "1", f"{label}: {_row(inv, label)!r}\n{inv}"
    assert _row(inv, "leases").startswith("1 "), inv
    assert _row(inv, "credential_publications").startswith("1 "), inv
    assert _row(inv, "pools") == "2", inv


# ---------------------------------------------------------------------------
# 3. The typed confirmation is required, SWARM_ASSUME_YES or not
# ---------------------------------------------------------------------------

def test_swarm_assume_yes_does_not_replace_the_typed_confirmation(tmp_path):
    before = world()
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", SWARM_ASSUME_YES="1")

    assert run.rc != 0, run.stderr[-3000:]
    assert not run.writes, f"deleted on SWARM_ASSUME_YES with no one at the keyboard: {run.writes}"
    assert run.state == before
    assert "ignoring SWARM_ASSUME_YES" in run.stderr, run.stderr[-3000:]
    # It got as far as asking, and asking is where it stopped.
    assert "interactive confirmation" in run.stderr, run.stderr[-3000:]


def test_a_wrong_answer_at_the_terminal_deletes_nothing(tmp_path):
    before = world()
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed="yes", SWARM_ASSUME_YES="1")

    assert run.rc != 0, run.stderr[-3000:]
    assert not run.writes, f"deleted after the answer 'yes' instead of the tenant id: {run.writes}"
    assert run.state == before
    assert "confirmation did not match" in run.stderr, run.stderr[-3000:]


def test_the_unset_is_what_stops_swarm_assume_yes(tmp_path):
    """The mutant keeps SWARM_ASSUME_YES, which common.sh's confirm() honours,
    and must then delete with nobody at a terminal -- the defect CLAUDE.md
    forbids, shown to be reachable and shown to be caught."""
    run = _run(tmp_path, world(), "--tenant", TENANT, "--apply", SWARM_ASSUME_YES="1",
               mutate=("  unset SWARM_ASSUME_YES\n", "  :\n"))
    assert run.writes, (
        "with the unset removed, SWARM_ASSUME_YES still did not get past the confirmation, "
        f"so the test above cannot tell the guard from something else:\n{run.stderr[-3000:]}"
    )


# ---------------------------------------------------------------------------
# 4. A deletion that did not land fails the proof, and keeps the id held
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "fault, row, left",
    [
        # Firestore answered 200 to the commit, and the attempts are still there.
        ({"FAKE_DROP_DELETES": "attempts"}, "attempts", "1"),
        # A task's events survived: the check that has to look under each task.
        ({"FAKE_DROP_DELETES": "events"}, "task events", "3"),
        # gcloud storage rm exited 0 and removed nothing.
        ({"FAKE_RM_KEEPS": "1"}, f"gs://{BUCKET}/tenants/{TENANT}/", "3"),
    ],
    ids=["firestore-commit-dropped", "events-dropped", "gcs-rm-kept"],
)
def test_a_verification_failure_exits_non_zero_and_keeps_the_tenant(tmp_path, fault, row, left):
    run = _run(tmp_path, world(), "--tenant", TENANT, "--apply", typed=TENANT, **fault)

    assert run.writes, "the fault case never reached the delete; it tests nothing"
    assert run.rc != 0, f"residue was left and the run exited 0:\n{run.stderr[-3000:]}"
    assert "VERIFICATION FAILED" in run.stderr, run.stderr[-3000:]
    proof = _section(run.stderr, "== Proof:")
    assert _row(proof, row).startswith(left), (
        f"the proof row {row!r} reads {_row(proof, row)!r}, expected {left}:\n{proof}"
    )
    # The id stays held: releasing it with data left is the hazard itself.
    assert f"tenants/{TENANT}" in run.state["docs"], (
        "the tenant document was deleted although the proof failed; the id is free while data remains"
    )
    assert f"tenants/{TENANT}" not in run.deleted_docs()


# ---------------------------------------------------------------------------
# 5. A real run: everything of `eng`, nothing of `eng-x`
# ---------------------------------------------------------------------------

def test_apply_deletes_every_record_and_object_of_the_tenant_and_nothing_else(tmp_path):
    before = world()
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed=TENANT)

    assert run.rc == 0, run.stderr[-4000:]
    assert _eng_docs(run.state) == [], f"left behind: {_eng_docs(run.state)}"
    assert run.state["docs"] == _not_eng(before), "a document that is not eng's changed"
    assert _eng_objects(run.state) == [], f"objects left: {_eng_objects(run.state)}"
    assert [o for o in run.state["gcs"][BUCKET] if not o["name"].startswith(f"tenants/{TENANT}/")] == [
        o for o in before["gcs"][BUCKET] if not o["name"].startswith(f"tenants/{TENANT}/")
    ], "an object outside tenants/eng/ was touched"
    assert "bucket_deleted" not in run.state

    deleted = run.deleted_docs()
    # Events before their task: a run that dies in between leaves no orphan.
    order = {d: n for n, d in enumerate(deleted)}
    for task in ("task_e1", "task_e2"):
        events = [d for d in deleted if d.startswith(f"tasks/{task}/events/")]
        assert events and max(order[e] for e in events) < order[f"tasks/{task}"], (
            f"tasks/{task} was deleted before its events: {deleted}"
        )
    # The tenant document is the LAST write: the id is held until the rest is gone.
    assert deleted[-1] == f"tenants/{TENANT}", f"the tenant document was not deleted last: {deleted}"

    proof = _section(run.stderr, "== Proof:")
    assert "(13 checks)" in proof, proof
    for label in (f"gs://{BUCKET}/tenants/{TENANT}/", "tasks", "task events", "events by tenant_id",
                  "attempts", "leases", "workflows", "quota", "outcome_days", "accounts", "account_auth",
                  "credential_publications", "pools"):
        assert _row(proof, label).startswith("0"), f"proof row {label!r}: {_row(proof, label)!r}"
    assert _row(proof, f"tenants/{TENANT}") == "absent", proof
    # What survives anyway is said, not hidden: the soft-deleted object
    # versions, and the Firestore versions point-in-time recovery keeps.
    assert "3 object version(s) under" in run.stderr and "SOFT-deleted" in run.stderr, run.stderr[-3000:]
    assert "point-in-time recovery is ON" in run.stderr and "604800s" in run.stderr, run.stderr[-3000:]


def test_documents_terraform_owns_are_left_for_terraform(tmp_path):
    """terraform/modules/firestore creates the tenant document and its pools
    with enabled = true. Deleting them here would let the next release recreate
    them ENABLED before the tfvars removal merges. So they are left, named in
    the proof, and everything else still goes."""
    managed = {"managed_by": s("swarm-terraform")}
    before = world(**{
        f"tenants/{TENANT}": {"fields": {"tenant_id": s(TENANT), "enabled": b(False), **managed}},
        f"pools/tenant:{TENANT}": {"fields": {"active": i(0), **managed}},
    })
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed=TENANT)

    assert run.rc == 0, run.stderr[-4000:]
    assert _eng_docs(run.state) == [f"pools/tenant:{TENANT}", f"tenants/{TENANT}"], _eng_docs(run.state)
    # The pool terraform does NOT hold (an admin's per-provider pool) still goes.
    assert f"pools/provider:anthropic:tenant:{TENANT}" in run.deleted_docs()
    proof = _section(run.stderr, "== Proof:")
    assert "left for terraform" in _row(proof, f"tenants/{TENANT}"), proof
    assert "left for terraform" in _row(proof, f"pools/tenant:{TENANT}"), proof
    assert _row(proof, "pools").startswith("0"), proof


# ---------------------------------------------------------------------------
# 6. What is refused before anything is looked at
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tenant, over, says",
    [
        (TENANT, {f"tenants/{TENANT}": None}, "is not a registered tenant"),
        (TENANT, {f"tenants/{TENANT}": {"fields": {"tenant_id": s(TENANT), "enabled": b(True)}}},
         "still ENABLED"),
        # No `enabled` field at all is not "disabled".
        (TENANT, {f"tenants/{TENANT}": {"fields": {"tenant_id": s(TENANT)}}}, "still ENABLED"),
        (TENANT, {f"tenants/{TENANT}": {"fields": {"tenant_id": s("other"), "enabled": b(False)}}},
         "does not declare tenant_id"),
        (TENANT, {f"accounts/{NEIGHBOUR}:main": {"fields": {"owner_tenant": s(NEIGHBOUR), "assigned": i(0),
                                                            "lend_to": arr(TENANT)}}},
         "still lend to it"),
        ("", {}, "--tenant is required"),
        ("Eng", {}, "is not a tenant id"),
        ("eng/x", {}, "is not a tenant id"),
        ("eng*", {}, "is not a tenant id"),
        ("-eng", {}, "is not a tenant id"),
        ("abcdefghijkl", {}, "at most 11 characters"),
        ("default", {}, "shared deny-list"),
    ],
    ids=["unregistered", "enabled", "enabled-missing", "wrong-document", "still-lent", "empty", "upper",
         "slash", "wildcard", "dash", "too-long", "deny-listed"],
)
def test_refusals_delete_nothing(tmp_path, tenant, over, says):
    before = world(**over)
    run = _run(tmp_path, before, "--tenant", tenant, "--apply", typed=tenant or "x")
    assert run.rc != 0, run.stderr[-3000:]
    assert says in run.stderr, run.stderr[-3000:]
    assert not run.writes, run.writes
    assert run.state == before


def test_a_listing_that_fails_is_not_an_empty_prefix(tmp_path):
    """A denied `gcloud storage ls` must stop the run, not inventory as zero:
    a deletion script that reads a failed look as "nothing there" reports a
    tenant gone whose checkpoints are all still readable."""
    before = world()
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed=TENANT, FAKE_GCS_LS_ERROR="1")
    assert run.rc != 0, run.stderr[-3000:]
    assert "failure to LOOK" in run.stderr, run.stderr[-3000:]
    assert not run.writes
    assert run.state == before


# ---------------------------------------------------------------------------
# 7. Events are found by their tenant, not only under the tasks a run listed
# ---------------------------------------------------------------------------
#
# Firestore does not delete a document's subcollections with it. A task
# document deleted while an event is still under it leaves that event where no
# `tenant_id ==` listing of tasks can reach it again -- and a proof that
# counts events only under the tasks it listed then prints 0 over it. The
# fault that produces it is a delete that did not land (FAKE_DROP_DELETES),
# and in production an event appended between the events delete and the task
# delete: `store.cancel`'s cascade and the reconciler both append events as
# blind sets, outside any transaction the script could see.


def _eng_events(state: dict) -> list[str]:
    """Every stored event that carries eng's id, wherever it is."""
    out = []
    for path, doc in state["docs"].items():
        segs = path.split("/")
        if len(segs) == 4 and segs[0] == "tasks" and segs[2] == "events" \
                and doc.get("fields", {}).get("tenant_id") == s(TENANT):
            out.append(path)
    return sorted(out)


#: An event of `eng` whose task document is already gone -- what an earlier
#: run of the old script left after its proof failed -- and one of `eng-x`
#: in the same shape, which must be left alone.
ORPHANS = {
    "tasks/task_gone/events/ev_o1": {"fields": {"tenant_id": s(TENANT), "at": ts("2026-09-22T09:00:00Z")}},
    "tasks/task_gone_x/events/ev_ox": {"fields": {"tenant_id": s(NEIGHBOUR), "at": ts("2026-09-22T09:00:00Z")}},
}


def test_a_task_whose_events_survived_their_delete_is_not_deleted(tmp_path):
    run = _run(tmp_path, world(), "--tenant", TENANT, "--apply", typed=TENANT, FAKE_DROP_DELETES="events")

    assert run.writes, "the fault case never reached the delete; it tests nothing"
    for task in ("tasks/task_e1", "tasks/task_e2"):
        assert task not in run.deleted_docs(), (
            f"{task} was deleted with its events still under it; nothing that lists tasks by "
            f"tenant_id can reach those events again:\n{run.stderr[-3000:]}"
        )
        assert task in run.state["docs"]
    assert run.rc != 0, run.stderr[-3000:]
    assert "VERIFICATION FAILED" in run.stderr
    proof = _section(run.stderr, "== Proof:")
    assert _row(proof, "events by tenant_id").startswith("3"), proof


def test_running_again_after_an_events_fault_finishes_the_job(tmp_path):
    """The runbook's answer to a failed proof is to run the same command
    again. That rerun must find what the first run left, delete it and prove
    it -- not print 0 over events it can no longer see and release the id."""
    first = _run(tmp_path / "first", world(), "--tenant", TENANT, "--apply", typed=TENANT,
                 FAKE_DROP_DELETES="events")
    assert first.rc != 0 and "VERIFICATION FAILED" in first.stderr, first.stderr[-3000:]
    assert _eng_events(first.state), "the fault left no events behind; the rerun tests nothing"

    second = _run(tmp_path / "second", first.state, "--tenant", TENANT, "--apply", typed=TENANT)

    left = _eng_events(second.state)
    assert not (left and f"tenants/{TENANT}" not in second.state["docs"]), (
        f"the rerun released tenants/{TENANT} with {len(left)} of its events still stored: {left}\n"
        f"{second.stderr[-3000:]}"
    )
    assert second.rc == 0, second.stderr[-3000:]
    assert _eng_docs(second.state) == [], f"left behind: {_eng_docs(second.state)}"
    assert second.state["docs"] == _not_eng(world()), "a document that is not eng's changed"


def test_an_event_whose_task_is_already_gone_is_found_by_its_tenant(tmp_path):
    before = world(**ORPHANS)
    run = _run(tmp_path / "apply", before, "--tenant", TENANT, "--apply", typed=TENANT)

    assert "tasks/task_gone/events/ev_o1" not in run.state["docs"], (
        f"an event of {TENANT} under a task that no longer exists survived the offboarding:\n"
        f"{run.stderr[-3000:]}"
    )
    assert run.state["docs"]["tasks/task_gone_x/events/ev_ox"] == ORPHANS["tasks/task_gone_x/events/ev_ox"]
    assert run.rc == 0, run.stderr[-3000:]
    assert _eng_docs(run.state) == [], f"left behind: {_eng_docs(run.state)}"
    assert run.state["docs"] == _not_eng(before), "a document that is not eng's changed"
    proof = _section(run.stderr, "== Proof:")
    assert _row(proof, "events by tenant_id").startswith("0"), proof

    # The dry run counts it too: the inventory is what --apply deletes.
    dry = _run(tmp_path / "dry", before, "--tenant", TENANT, typed=TENANT)
    assert dry.rc == 0, dry.stderr[-3000:]
    inv = _section(dry.stderr, "== Inventory:")
    assert _row(inv, "events by tenant_id").startswith("4 "), inv
    assert _row(inv, "task events").startswith("3 "), inv


def test_every_listing_is_paginated_not_capped(tmp_path):
    """Pages of two, so every listing -- the collection-group one on its
    (at, __name__) cursor included -- crosses a page boundary, and two events
    share an `at`. A listing capped at its first page, or a cursor on `at`
    alone, leaves an event behind that this test finds."""
    before = world(**ORPHANS)
    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed=TENANT,
               mutate=("FS_PAGE=300", "FS_PAGE=2"))

    assert _eng_docs(run.state) == [], f"left behind: {_eng_docs(run.state)}\n{run.stderr[-3000:]}"
    assert run.rc == 0, run.stderr[-3000:]
    assert run.state["docs"] == _not_eng(before), "a document that is not eng's changed"


# ---------------------------------------------------------------------------
# 8. The publication ledger is read from the ledger, not only from labels
# ---------------------------------------------------------------------------

def test_a_ledger_entry_is_found_from_the_ledger_not_only_from_secret_labels(tmp_path):
    """`credential_publications/<secret name>` carries no tenant field. A
    listing of it that starts from the secrets labelled `tenant=<id>` finds
    nothing for a secret that is already gone, and a proof built on the same
    listing prints 0 over the entry. So the ledger itself is listed, an entry
    is this tenant's when its id is one of this tenant's secret-name forms,
    and it is left alone when a secret of that name is labelled for another
    tenant or a longer registered tenant id also names it."""
    ledger = "credential_publications/"
    before = world(**{
        # eng's, with no secret left to carry a label.
        f"{ledger}swarm-tenant-{TENANT}-openai": {"fields": {"digest": s("d3")}},
        f"{ledger}swarm-account-{TENANT}--main": {"fields": {"digest": s("d4")}},
        # A secret named for eng-z, labelled eng-z, whose tenant document is
        # gone: `swarm-tenant-eng-` starts it, and the label says whose it is.
        f"{ledger}swarm-tenant-eng-z-openai": {"fields": {"digest": s("d5")}},
    })
    before["secrets"] = [
        sec for sec in before["secrets"] if sec["labels"].get("tenant") != NEIGHBOUR
    ] + [{"name": "swarm-tenant-eng-z-openai", "labels": {"tenant": "eng-z"}}]
    # eng-x's own entry (in the world) now has no secret either; eng-x is
    # registered, and its id is the longer one that names the entry.
    assert f"{ledger}swarm-tenant-{NEIGHBOUR}-anthropic" in before["docs"]

    run = _run(tmp_path, before, "--tenant", TENANT, "--apply", typed=TENANT)

    for gone in (f"swarm-tenant-{TENANT}-anthropic", f"swarm-tenant-{TENANT}-openai",
                 f"swarm-account-{TENANT}--main"):
        assert ledger + gone not in run.state["docs"], (
            f"{ledger}{gone} survived the offboarding of {TENANT}:\n{run.stderr[-3000:]}"
        )
    for kept in (f"swarm-tenant-{NEIGHBOUR}-anthropic", "swarm-tenant-eng-z-openai"):
        assert run.state["docs"][ledger + kept] == before["docs"][ledger + kept], (
            f"{ledger}{kept} is not {TENANT}'s and was touched"
        )
    assert run.rc == 0, run.stderr[-3000:]
    inv = _section(run.stderr, "== Inventory:")
    assert _row(inv, "credential_publications").startswith("3 "), inv
    proof = _section(run.stderr, "== Proof:")
    assert _row(proof, "credential_publications").startswith("0"), proof
