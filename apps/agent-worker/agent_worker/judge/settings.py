"""Whether Jev may be asked about this task's code, and which backend answers.

Two stored settings decide it (owner decisions, 2026-10-14):

* **Code intelligence** (Settings > Workspace > Code intelligence, lane
  CTXQ-SET): Jev may see code only when the workspace opted in. Off by
  default, with a repository override `inherit` / `on` / `off`. `on` cannot
  opt a repository in when its workspace has not: the opt-in is the
  workspace admin's decision about what leaves the platform, and a
  repository setting is not that admin's.
* **The judgment backend** (lane JUDGE-SELECT): `internal-jev` (the Saga
  decision API over the tailnet, the default), `internal-laya`, or
  `typesafe`. The Internal backends take no credential at all; TypeSafe takes
  the workspace's key.

Nothing here reads Firestore. The caller passes the stored documents in, so
the decision is a pure function a unit test can enumerate. An unreadable or
unknown value is the safe one: not opted in, the default backend.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

#: Where the Internal decision API is reached. Deployment configuration, not a
#: caller input (invariant 10): the worker's environment names it, and an
#: unset URL means the Internal backends are unavailable, never guessed at.
INTERNAL_URL_ENV = "SWARM_JUDGE_INTERNAL_URL"
#: The model names the Internal API is asked for. Overridable for the same
#: reason the URL is: they are the decision API's names, not this repository's.
INTERNAL_JEV_MODEL_ENV = "SWARM_JUDGE_INTERNAL_JEV_MODEL"
INTERNAL_LAYA_MODEL_ENV = "SWARM_JUDGE_INTERNAL_LAYA_MODEL"

#: jev-browser 0.2.0's `src/jev.mjs` API_URL and MODEL, the only Jev client
#: the platform ships (context-quality.md §1.5).
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL = "jev-latest"
#: Where TypeSafe is reached on GKE with the forwarder switched on
#: (SEC-MIGRATE-TYPESAFE): the worker's loopback shim
#: (`agent_worker.typesafe_shim`), which forwards to the TypeSafe forwarder
#: with the attempt's ticket. Only this shape is believed. A judge on Cloud Run
#: has no shim (the forwarder is a cluster Service) and keeps TYPESAFE_URL
#: with the key file.
SHIM_URL_PATTERN = re.compile(r"http://127\.0\.0\.1:[0-9]{1,5}/v1/systemone")

INTERNAL_JEV = "internal-jev"
INTERNAL_LAYA = "internal-laya"
TYPESAFE = "typesafe"
BACKENDS = (INTERNAL_JEV, INTERNAL_LAYA, TYPESAFE)
#: The owner's default: no credential reaches anything on this backend.
DEFAULT_BACKEND = INTERNAL_JEV

OVERRIDES = ("inherit", "on", "off")


@dataclass(frozen=True)
class JudgeSettings:
    """The stored settings, as read for one task."""

    code_opt_in: bool = False
    repository_override: str = "inherit"
    backend: str = DEFAULT_BACKEND
    #: The per-workspace monthly cap on Jev input tokens; None means no cap
    #: was set (§8 Q4: set once TypeSafe's rate is known).
    monthly_cap_tokens: int | None = None

    @classmethod
    def from_mapping(cls, workspace: Mapping[str, Any] | None,
                     repository: Mapping[str, Any] | None = None) -> "JudgeSettings":
        """From the workspace's `code_intelligence` block and the repository's
        override, tolerating anything malformed as "off"."""
        block = (workspace or {}).get("code_intelligence") if isinstance(workspace, Mapping) else None
        block = block if isinstance(block, Mapping) else {}
        backend = block.get("judgment_backend")
        cap = block.get("jev_monthly_cap_tokens")
        override = (repository or {}).get("jev") if isinstance(repository, Mapping) else None
        return cls(
            code_opt_in=block.get("jev") is True,
            repository_override=override if override in OVERRIDES else "inherit",
            backend=backend if backend in BACKENDS else DEFAULT_BACKEND,
            monthly_cap_tokens=cap if isinstance(cap, int) and not isinstance(cap, bool)
            and cap >= 0 else None,
        )


@dataclass(frozen=True)
class Backend:
    """Where one call goes and how its body is shaped."""

    name: str
    url: str
    model: str
    #: The Internal API takes `state` as a STRING (measured): objects are
    #: JSON-stringified. TypeSafe takes the object, as jev.mjs sends it.
    stringify_state: bool
    #: The bearer credential, or None. Never logged, never in an audit row.
    key: str | None = None

    def __repr__(self) -> str:  # the key must not reach a log through repr()
        return f"Backend(name={self.name!r}, url={self.url!r}, model={self.model!r})"


@dataclass(frozen=True)
class Off:
    """No call will be made, and why (the pack's `jev: off (reason)`)."""

    reason: str


def opted_in(given: JudgeSettings) -> Off | None:
    if not given.code_opt_in:
        return Off("workspace_not_opted_in")
    if given.repository_override == "off":
        return Off("repository_off")
    return None


def typesafe_url(shim_url: str | None = None) -> str | None:
    """TypeSafe's address for this judge: the worker's shim when there is one
    (GKE, the forwarder on), else TYPESAFE_URL. None for a shim address that
    is not the shim's shape: nothing is sent then, never to TypeSafe direct
    with the shim's token."""
    if shim_url is None:
        return TYPESAFE_URL
    return shim_url if SHIM_URL_PATTERN.fullmatch(shim_url) else None


def resolve_backend(given: JudgeSettings, *, key: str | None,
                    environ: Mapping[str, str] | None = None,
                    shim_url: str | None = None) -> Backend | Off:
    """The backend this task's calls go to, or why there are none.

    `key` is the TypeSafe key the caller resolved (#1297's `jevkey` order);
    it is ignored by the Internal backends, which carry no credential. With
    `shim_url` (the worker's shim, `typesafe_url`), `key` is the shim's local
    token, never a TypeSafe key.
    """
    off = opted_in(given)
    if off is not None:
        return off
    env = os.environ if environ is None else environ
    if given.backend == TYPESAFE:
        url = typesafe_url(shim_url)
        if not key or url is None:
            return Off("no_key")
        return Backend(TYPESAFE, url, TYPESAFE_MODEL, stringify_state=False, key=key)
    url = (env.get(INTERNAL_URL_ENV) or "").strip()
    if not url:
        return Off("no_internal_url")
    if given.backend == INTERNAL_LAYA:
        model = (env.get(INTERNAL_LAYA_MODEL_ENV) or "").strip() or "laya-latest"
    else:
        model = (env.get(INTERNAL_JEV_MODEL_ENV) or "").strip() or "jev-latest"
    return Backend(given.backend, url, model, stringify_state=True)
