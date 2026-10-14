"""The relay's counts, for scripts/roll-live-relay.sh (RELAY-ROLL, part of #1030).

    kubectl exec swarm-live-0 -c relay -n swarm-system -- python -m swarm_live.status

prints `/healthz` from the pod's own loopback as one JSON line:
`{"ok": true, "tunnels": N, "viewers": N, "controlled": N}`. Counts only --
never a session id, an email or a ticket. The roll script defers while
`viewers` or `controlled` is above zero.

WHY THROUGH `kubectl exec`. The relay's NetworkPolicy admits only swarm-api's
egress range and the tenant gates, and a release runner is neither. `exec` is
authenticated and authorised by the Kubernetes API (the release's identity,
the same one that applies the relay), and the read happens inside the pod, so
no policy and no route are widened for it. Standard library only: the image
needs nothing new.
"""

from __future__ import annotations

import json
import sys
import urllib.request

from .main import PORT

URL = f"http://127.0.0.1:{PORT}/healthz"
TIMEOUT_SECONDS = 5


def read(url: str = URL, *, timeout: float = TIMEOUT_SECONDS) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - a fixed loopback URL
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict):
        raise ValueError("the relay's /healthz is not an object")
    return {key: body.get(key) for key in ("ok", "tunnels", "viewers", "controlled")}


def main(argv: list[str] | None = None) -> int:
    try:
        counts = read()
    except Exception as exc:  # noqa: BLE001 - the class only; the roll script decides
        print(f"relay status unreadable: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(counts, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
