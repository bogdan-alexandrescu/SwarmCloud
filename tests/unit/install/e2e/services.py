"""The fake services: commands (gcloud, kubectl, terraform and the pinned
tools) and Google's REST APIs, registered on a `World`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from swarm_install import tools

from subprocess import run as _real_run  # bound before the walk patches subprocess.run

from .world import World


def commands(world: World) -> None:
    versions = {
        "terraform": f"Terraform v{tools.TERRAFORM_VERSION}\non linux_amd64\n",
        "kubectl": f"Client Version: v{tools.KUBECTL_VERSION}\nKustomize Version: v5.7.1\n",
        "jq": f"jq-{tools.JQ_VERSION}\n",
        "uv": "uv 0.9.5\n",
        "cosign": f"GitVersion:    v{tools.COSIGN_VERSION}\n",
        "gcrane": f"{tools.GCRANE_VERSION}\n",
        "gke-gcloud-auth-plugin": f"Kubernetes v1.30.0+{tools.GKE_AUTH_PLUGIN_VERSION}\n",
        "python3": "Python 3.11.9\n",
        "git": "git version 2.45.0\n",
    }
    for program, said in versions.items():
        if program in ("terraform", "kubectl", "cosign", "gcrane"):
            continue

        def version(argv: list[str], stdin: Any, env: Any, cwd: Any, said: str = said) -> tuple[int, str, str]:
            return 0, said, ""

        world.on_command(program)(version)

    @world.on_command("gcloud")
    def gcloud(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        if words[:1] == ["version"]:
            return 0, f"{tools.GCLOUD_VERSION}\n", ""
        if words[:3] == ["config", "get-value", "account"]:
            return 0, (world.installer + "\n") if world.signed_in else "\n", ""
        if words[:2] == ["auth", "login"]:
            world.signed_in = True
            return 0, "", ""
        if words[:3] == ["auth", "application-default", "login"]:
            world.signed_in = True
            for word in words:
                if word.startswith("--scopes="):
                    world.cloud.adc_scopes = tuple(word.removeprefix("--scopes=").split(","))  # type: ignore[attr-defined]
            return 0, "", ""
        world.unhandled.append("gcloud " + " ".join(words))
        return 1, "", f"ERROR: (gcloud) the fake world has no `gcloud {' '.join(words[:3])}`"

    @world.on_command("kubectl")
    def kubectl(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        if words[:2] == ["version", "--client"]:
            return 0, versions["kubectl"], ""
        return world.kube(words, stdin, env)

    @world.on_command("terraform")
    def terraform(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        if words[:1] == ["version"]:
            if "-json" in words:
                return 0, json.dumps({"terraform_version": tools.TERRAFORM_VERSION}), ""
            return 0, versions["terraform"], ""
        return world.terraform(words, dict(env or {}), Path(cwd) if cwd else None)


def renderers(world: World) -> None:
    """The release bundle's `render.py` runs for real: it is pure, and what it
    renders is what the fake cluster is asked to hold."""
    import sys

    def python(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        if not argv[1:2] or not argv[1].endswith("render.py"):
            world.unhandled.append("python " + " ".join(argv[1:3]))
            return 2, "", "python: the fake world runs only the bundle's render.py"
        done = _real_run([sys.executable, *argv[1:]], input=stdin, capture_output=True, text=True, cwd=cwd,
                        timeout=60)
        return done.returncode, done.stdout, done.stderr

    for name in ("python", "python3", Path(sys.executable).name):
        world.on_command(name)(python)


def unhandled_kube(world: World) -> None:
    def kube(words: list[str], stdin: Any, env: Any) -> tuple[int, str, str]:
        world.unhandled.append("kubectl " + " ".join(words[:4]))
        return 1, "", "error: the fake world has no such kubectl call"

    world.kube = kube  # type: ignore[attr-defined]


def swarm_cli(world: World) -> None:
    """The swarm CLI's contexts and credential store (secrets on stdin only)."""
    world.contexts = {}  # type: ignore[attr-defined]

    @world.on_command("swarm")
    def swarm(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        contexts: dict[str, dict[str, Any]] = world.contexts  # type: ignore[attr-defined]
        if words[:2] == ["context", "list"]:
            rows = [{"name": n, "url": c["url"], "client_id": c.get("client_id"),
                     "signed_in": c.get("signed_in", False)} for n, c in contexts.items()]
            return 0, json.dumps({"contexts": rows}), ""
        if words[:2] == ["context", "add"]:
            if any("secret=" in w for w in words):
                return 2, "", "a secret is never accepted in argv"
            flags = dict(w.lstrip("-").split("=", 1) for w in words[3:] if "=" in w)
            contexts[words[2]] = {"url": flags.get("url"), "client_id": flags.get("client-id"),
                                  "has_secret": bool(stdin and stdin.strip())}
            return 0, "", ""
        world.unhandled.append("swarm " + " ".join(words[:4]))
        return 1, "", f"swarm: the fake world has no `{' '.join(words[:2])}`"


def release_tools(world: World) -> None:
    """cosign verifies with the release key; gcrane copies into the project's registry."""
    from .google import signature

    @world.on_command("cosign")
    def cosign(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        if words[:1] == ["version"]:
            return 0, f"GitVersion:    v{tools.COSIGN_VERSION}\n", ""
        if words[:1] == ["verify-blob"]:
            sig = Path(words[words.index("--signature") + 1]).read_bytes()
            blob = Path(words[-1]).read_bytes()
            if sig != signature(blob):
                return 1, "", "Error: invalid signature when validating ASN.1 encoded signature"
            return 0, "Verified OK\n", ""
        if words[:1] == ["verify"]:
            return 0, "[]", ""
        world.unhandled.append("cosign " + " ".join(words[:2]))
        return 1, "", "cosign: no such fake"

    @world.on_command("gcrane")
    def gcrane(argv: list[str], stdin: Any, env: Any, cwd: Any) -> tuple[int, str, str]:
        words = argv[1:]
        if words[:1] == ["version"]:
            return 0, f"{tools.GCRANE_VERSION}\n", ""
        if words[:1] == ["cp"]:
            target = words[2]
            repo = target.split("@")[0].rsplit("/", 1)[0]
            region, project, name = repo.split("/")[0].removesuffix("-docker.pkg.dev"), repo.split("/")[1], repo.split("/")[2]
            if f"projects/{project}/locations/{region}/repositories/{name}" not in world.registries:
                return 1, "", f"NAME_UNKNOWN: Repository {name} not found"
            world.images.add(target)
            return 0, "", ""
        world.unhandled.append("gcrane " + " ".join(words[:2]))
        return 1, "", "gcrane: no such fake"
