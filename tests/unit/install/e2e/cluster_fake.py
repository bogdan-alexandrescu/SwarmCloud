"""The install cluster, as gcloud and kubectl reach it.

* `gcloud container clusters get-credentials` answers only for the cluster an
  infra pass made, and writes the context the real command names;
* the control plane answers kubectl only from an address in the master
  authorised networks the last infra apply rendered (decision 2): from
  anywhere else kubectl times out, as it does against a private endpoint;
* `diff`, server-side `apply`, `get` and `rollout status` act on the objects
  the cluster holds, so a render nothing applied stays a difference.
"""

from __future__ import annotations

import json
from typing import Any

import yaml

from .google import INSTALLER_IP
from .world import World

TIMEOUT = "Unable to connect to the server: dial tcp 10.0.0.2:443: i/o timeout"


def _key(doc: dict[str, Any]) -> tuple[str, str, str]:
    meta = doc.get("metadata") or {}
    return str(doc.get("kind")).lower(), str(meta.get("namespace") or ""), str(meta.get("name"))


def attach(world: World) -> None:
    objects: dict[tuple[str, str, str], dict[str, Any]] = {}
    applied: dict[tuple[str, str, str], dict[str, Any]] = {}  # what each object was last applied as
    world.kube_objects = objects  # type: ignore[attr-defined]
    world.kube_context = ""  # type: ignore[attr-defined]

    def cluster() -> str:
        return str(world.tf_outputs["infra"].get("gke_cluster_name") or "")  # type: ignore[attr-defined]

    def authorised() -> bool:
        rendered = [inputs for root, inputs, targets in world.tf_applies  # type: ignore[attr-defined]
                    if root == "infra" and not targets]
        networks = (rendered[-1] if rendered else {}).get("gke_master_authorized_cidrs") or []
        return any(n.get("cidr_block") == f"{INSTALLER_IP}/32" for n in networks)

    gcloud = next(fn for want, fn in world.command_handlers if want == "gcloud")

    def gcloud_with_clusters(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        if words[:3] != ["container", "clusters", "get-credentials"]:
            return gcloud(argv, stdin, env, cwd)
        flags = dict(w.lstrip("-").split("=", 1) for w in words if "=" in w)
        if words[3] != cluster():
            return 1, "", f"ERROR: (gcloud.container.clusters.get-credentials) 404 cluster {words[3]} not found"
        world.kube_context = f"gke_{flags['project']}_{flags['region']}_{words[3]}"  # type: ignore[attr-defined]
        return 0, f"kubeconfig entry generated for {words[3]}.\n", ""

    world.command_handlers = [(want, gcloud_with_clusters if want == "gcloud" else fn)
                              for want, fn in world.command_handlers]

    def kube(words: list[str], stdin: Any, env: Any) -> tuple[int, str, str]:
        words = [w for w in words if not w.startswith(("--kubeconfig=", "--context="))]
        if words[:2] == ["config", "current-context"]:
            context = world.kube_context  # type: ignore[attr-defined]
            return (0, context + "\n", "") if context else (1, "", "error: current-context is not set")
        if not authorised():
            return 1, "", TIMEOUT
        flags = dict(w.lstrip("-").split("=", 1) for w in words if w.startswith("--") and "=" in w)
        namespace = flags.get("namespace", "")
        if words[:2] == ["get", "namespaces"]:
            return 0, "".join(f"namespace/{n}\n" for k, _, n in sorted(objects) if k == "namespace"), ""
        if words[:3] == ["auth", "can-i", "list"]:
            who = flags.get("as", "")
            bound = any(k in ("rolebinding", "clusterrolebinding") and (k == "clusterrolebinding" or ns == namespace)
                        and any(str(s.get("name")) == who for s in o.get("subjects") or [])
                        for (k, ns, _), o in objects.items())
            return (0, "yes\n", "") if bound else (1, "no\n", "")
        if words[:1] == ["get"] and words[1:2] == ["namespace"]:
            found = ("namespace", "", words[2]) in objects
            return (0, f"namespace/{words[2]}\n", "") if found else (
                1, "", f'Error from server (NotFound): namespaces "{words[2]}" not found')
        if words[:1] == ["get"] and "/" in words[1]:
            kind, name = words[1].split("/", 1)
            found = objects.get((kind, namespace, name))
            if found is None:
                return 1, "", f'Error from server (NotFound): {kind} "{name}" not found'
            return 0, json.dumps(found), ""
        if words[:1] == ["get"] and words[1] == "jobs":
            items = [o for (k, ns, _), o in objects.items() if k == "job" and ns == namespace]
            return 0, json.dumps({"kind": "List", "items": items}), ""
        if words[:1] in (["diff"], ["apply"]):
            docs = [d for d in yaml.safe_load_all(stdin or "") if isinstance(d, dict)]
            if words[0] == "diff":
                changed = [d for d in docs if applied.get(_key(d)) != d]
                return (1, "".join(f"diff -u -N /tmp/LIVE/{k}.{n} /tmp/MERGED/{k}.{n}\n"
                                   for k, _, n in map(_key, changed)), "") if changed else (0, "", "")
            for d in docs:
                applied[_key(d)] = d
                held = dict(d)
                if _key(d)[0] in ("deployment", "statefulset"):
                    held["status"] = {"readyReplicas": int((d.get("spec") or {}).get("replicas") or 1)}
                objects[_key(d)] = held
            return 0, "".join(f"{k}/{n} serverside-applied\n" for k, _, n in map(_key, docs)), ""
        if words[:2] == ["rollout", "status"]:
            kind, name = words[2].split("/", 1)
            if (kind, namespace, name) not in objects:
                return 1, "", f'Error from server (NotFound): {kind} "{name}" not found'
            return 0, f'{words[2]} successfully rolled out\n', ""
        world.unhandled.append("kubectl " + " ".join(words[:4]))
        return 1, "", "error: the fake cluster has no such kubectl call"

    world.kube = kube  # type: ignore[attr-defined]

    def make_namespace(name: str) -> None:
        objects[("namespace", "", name)] = {"kind": "Namespace", "metadata": {"name": name}}

    world.make_namespace = make_namespace  # type: ignore[attr-defined]

    def apply_in_cluster(rendered: str) -> None:
        """What a job inside the cluster's network applies (swarm-workspace-apply):
        no authorised-networks check, the objects as rendered."""
        for d in yaml.safe_load_all(rendered):
            if isinstance(d, dict):
                objects[_key(d)] = dict(d)
                applied[_key(d)] = d

    world.apply_in_cluster = apply_in_cluster  # type: ignore[attr-defined]
