"""What a project still holds, read-only, by kind and name (issue 344, gap 2).

`swarm doctor --project P --remains` prints this after `swarm uninstall`, so a
person can see from the product whether the project is empty again. It reads
the project with list calls only (HTTP GET through `preflight.Gcp`): it
creates, enables, patches, deletes and sets nothing, and runs no `kubectl`.

Rules the listing keeps:

* **Nothing is left out silently.** Every kind in `KINDS` is either read or
  reported under "not read" with the reason. A kind whose API is off reads as
  empty only where nothing of that kind can exist without its API
  (`off_means_empty`); buckets and service accounts can outlive theirs, so for
  them an API that is off is a kind not read (`preflight.neighbours` holds the
  same rule).
* **"Nothing remains" is said only after a complete read** that found nothing.
  A listing with any kind not read is never offered as the whole one.
* **A name on the install's never-touch list is never printed**, only counted
  (`uninstall.denied`, the engine's one matcher of that list).
* **The kinds an uninstall leaves on purpose** (`remains.KINDS`) are marked
  `expected`, so they read as the remains record says, not as a failure.

The table covers the kinds the install makes and the ones Google makes beside
them; a kind not in it is not seen, which is why docs/install.md names them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from swarm_redaction import redact

from . import remains as remains_mod
from .steps.preflight import COMPUTE, IAM, SECRET_MANAGER_API, USAGE, ApiError, Gcp, _api_off

RUN = "https://run.googleapis.com/v2"
SCHEDULER = "https://cloudscheduler.googleapis.com/v1"
PUBSUB = "https://pubsub.googleapis.com/v1"
REGISTRY = "https://artifactregistry.googleapis.com/v1"
FIRESTORE = "https://firestore.googleapis.com/v1"
KMS = "https://cloudkms.googleapis.com/v1"
DNS = "https://dns.googleapis.com/dns/v1"
LOGGING = "https://logging.googleapis.com/v2"
MONITORING = "https://monitoring.googleapis.com/v3"

#: Google's own log buckets every project has; not the install's, never removable.
BUILTIN_LOG_BUCKETS = ("_Default", "_Required")
#: How this listing's kinds map to the remains record's (`remains.KINDS`).
REMAINS_KIND = {"oauth-brand": "oauth-brand", "kms-key-ring": "kms-key-ring", "custom-role": "custom-roles",
                "firestore-database": "firestore-database"}
NOTHING_REMAINS = "Nothing remains in {project}."


@dataclass(frozen=True)
class Held:
    kind: str
    name: str
    note: str = ""


@dataclass(frozen=True)
class Unread:
    kind: str
    reason: str


@dataclass
class Inventory:
    project: str
    held: list[Held] = field(default_factory=list)
    unread: list[Unread] = field(default_factory=list)
    #: Resources found whose names are on the never-touch list: counted, never named.
    never_touch: int = 0
    #: What the project itself is: `ACTIVE`, `DELETE_REQUESTED`, or `not visible`.
    project_state: str = ""

    @property
    def complete(self) -> bool:
        return self.project_state in ("ACTIVE", "DELETE_REQUESTED") and not self.unread

    @property
    def nothing_remains(self) -> bool:
        return self.complete and self.project_state == "ACTIVE" and not self.held and not self.never_touch

    def to_json(self) -> dict[str, Any]:
        return {"project": self.project, "project_state": self.project_state, "complete": self.complete,
                "nothing_remains": self.nothing_remains, "held": [asdict(h) for h in self.held],
                "unread": [asdict(u) for u in self.unread], "never_touch_not_named": self.never_touch}


@dataclass(frozen=True)
class Kind:
    """One kind: `list_(gcp, project, region, number)` returns `(name, labels)`
    pairs, or None when the kind needs a region and none is recorded."""

    name: str
    list_: Callable[[Gcp, str, str, str], list[str] | None]
    off_means_empty: bool = True


def _last(value: Any) -> str:
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _paged(url: str, key: str, name: Callable[[Mapping[str, Any]], str] = lambda i: _last(i.get("name")),
           keep: Callable[[Mapping[str, Any]], bool] = lambda i: True, *, regional: bool = False,
           params: dict[str, Any] | None = None) -> Callable[[Gcp, str, str, str], list[str] | None]:
    """A lister over one GET-paged collection; `{project}`, `{region}` and
    `{number}` in `url` are filled in."""

    def lister(gcp: Gcp, project: str, region: str, number: str) -> list[str] | None:
        if regional and not region:
            return None
        items = gcp._pages(url.format(project=project, region=region, number=number), key, dict(params or {}))
        return [name(i) for i in items if keep(i)]

    return lister


def _aggregated(path: str, key: str) -> Callable[[Gcp, str, str, str], list[str] | None]:
    """Compute's aggregated lists: `{items: {scope: {key: [...]}}}`, paged."""

    def lister(gcp: Gcp, project: str, region: str, number: str) -> list[str] | None:
        found: list[str] = []
        params: dict[str, Any] = {}
        while True:
            page = gcp._call("GET", f"{COMPUTE}/projects/{project}/aggregated/{path}", params=dict(params)) or {}
            for scope, scoped in (page.get("items") or {}).items():
                for item in scoped.get(key, []) or []:
                    found.append(f"{_last(scope)}/{item.get('name')}")
            token = page.get("nextPageToken")
            if not token:
                return found
            params["pageToken"] = token

    return lister


def _clusters(gcp: Gcp, project: str, region: str, number: str) -> list[str]:
    return [str(c.get("name", "")) for c in gcp.clusters(project)]


def _service_accounts(gcp: Gcp, project: str, region: str, number: str) -> list[str]:
    # Only accounts made in the project: Google's service agents live under
    # other domains (preflight.neighbours keeps the same rule).
    own = f"@{project}.iam.gserviceaccount.com"
    return [str(a.get("email", "")) for a in gcp.project_service_accounts(project)
            if str(a.get("email", "")).endswith(own)]


def _kms_key_rings(gcp: Gcp, project: str, region: str, number: str) -> list[str]:
    found: list[str] = []
    for location in sorted({region, "global"} - {""}):
        items = gcp._pages(f"{KMS}/projects/{project}/locations/{location}/keyRings", "keyRings")
        found.extend(f"{location}/{_last(i.get('name'))}" for i in items)
    return found


def _brands(gcp: Gcp, project: str, region: str, number: str) -> list[str] | None:
    if not number:
        return None
    return [_last(b.get("name")) + (f" ({b['applicationTitle']})" if b.get("applicationTitle") else "")
            for b in gcp.iap_brands(number)]


def _log_bucket(item: Mapping[str, Any]) -> str:
    name = "/".join(str(item.get("name", "")).split("/")[-3:])  # <location>/buckets/<id>
    location, _, bucket = name.partition("/buckets/")
    pending = " (pending deletion)" if item.get("lifecycleState") == "DELETE_REQUESTED" else ""
    return f"{_last(location)}/{bucket}{pending}"


def _enabled(item: Mapping[str, Any]) -> str:
    return str((item.get("config") or {}).get("name") or _last(item.get("name")))


#: Every kind the listing reads, in the order it prints them.
KINDS: tuple[Kind, ...] = (
    Kind("gcp-api", _paged(f"{USAGE}/v1/projects/{{project}}/services", "services", _enabled,
                           params={"filter": "state:ENABLED"}), off_means_empty=False),
    Kind("cluster", _clusters),
    Kind("network", _paged(f"{COMPUTE}/projects/{{project}}/global/networks", "items")),
    Kind("subnet", _aggregated("subnetworks", "subnetworks")),
    Kind("firewall", _paged(f"{COMPUTE}/projects/{{project}}/global/firewalls", "items")),
    Kind("address", _aggregated("addresses", "addresses")),
    Kind("global-address", _paged(f"{COMPUTE}/projects/{{project}}/global/addresses", "items")),
    Kind("bucket", lambda gcp, project, region, number: [str(b.get("name", ""))
                                                         for b in gcp.project_buckets(project)],
         off_means_empty=False),
    Kind("service-account", _service_accounts, off_means_empty=False),
    Kind("custom-role", _paged(f"{IAM}/projects/{{project}}/roles", "roles"), off_means_empty=False),
    Kind("secret", _paged(f"{SECRET_MANAGER_API}/projects/{{project}}/secrets", "secrets")),
    Kind("cloud-run-service", _paged(f"{RUN}/projects/{{project}}/locations/{{region}}/services", "services",
                                     regional=True)),
    Kind("cloud-run-job", _paged(f"{RUN}/projects/{{project}}/locations/{{region}}/jobs", "jobs", regional=True)),
    Kind("scheduler-job", _paged(f"{SCHEDULER}/projects/{{project}}/locations/{{region}}/jobs", "jobs",
                                 regional=True)),
    Kind("pubsub-topic", _paged(f"{PUBSUB}/projects/{{project}}/topics", "topics")),
    Kind("pubsub-subscription", _paged(f"{PUBSUB}/projects/{{project}}/subscriptions", "subscriptions")),
    Kind("registry", _paged(f"{REGISTRY}/projects/{{project}}/locations/{{region}}/repositories", "repositories",
                            regional=True)),
    Kind("firestore-database", _paged(f"{FIRESTORE}/projects/{{project}}/databases", "databases")),
    Kind("firestore-backup", _paged(f"{FIRESTORE}/projects/{{project}}/locations/-/backups", "backups",
                                    lambda i: "/".join(str(i.get("name", "")).split("/")[-3::2]))),
    Kind("kms-key-ring", _kms_key_rings),
    Kind("dns-zone", _paged(f"{DNS}/projects/{{project}}/managedZones", "managedZones",
                            lambda i: str(i.get("name", "")))),
    Kind("logging-bucket", _paged(f"{LOGGING}/projects/{{project}}/locations/-/buckets", "buckets", _log_bucket,
                                  lambda i: _last(i.get("name")) not in BUILTIN_LOG_BUCKETS)),
    Kind("log-metric", _paged(f"{LOGGING}/projects/{{project}}/metrics", "metrics")),
    Kind("alert-policy", _paged(f"{MONITORING}/projects/{{project}}/alertPolicies", "alertPolicies",
                                lambda i: str(i.get("displayName") or _last(i.get("name"))))),
    Kind("notification-channel", _paged(f"{MONITORING}/projects/{{project}}/notificationChannels",
                                        "notificationChannels",
                                        lambda i: str(i.get("displayName") or _last(i.get("name"))))),
    Kind("oauth-brand", _brands),
)


def _denied(name: str, deny_list: Sequence[str]) -> bool:
    from .uninstall import denied

    bare = name.split(" (", 1)[0]
    return bool(denied([bare, _last(bare)], deny_list))


def _note(kind: str) -> str:
    remains_kind = REMAINS_KIND.get(kind)
    if remains_kind is not None and remains_kind in remains_mod.KINDS:
        return f"expected: swarm uninstall leaves it (remains record: {remains_kind})"
    return ""


def _reason(exc: Exception) -> str:
    return redact(f"{type(exc).__name__}: {exc}").text[:300]


def read(gcp: Gcp, project: str, *, region: str, deny_list: Iterable[str],
         kinds: Sequence[Kind] = KINDS) -> Inventory:
    """Every kind of `kinds` in `project`, read-only."""
    deny = [d for d in deny_list if d]
    inventory = Inventory(project=project)
    try:
        found = gcp.project(project)
    except ApiError as exc:
        inventory.project_state = "not visible"
        inventory.unread.append(Unread("project", _reason(exc)))
        return inventory
    if not found:
        inventory.project_state = "not visible"
        inventory.unread.append(Unread("project", "the project does not exist, or you cannot see it"))
        return inventory
    inventory.project_state = str(found.get("state") or found.get("lifecycleState") or "ACTIVE")
    if inventory.project_state != "ACTIVE":
        return inventory
    number = _last(found.get("name")) if str(found.get("name", "")).startswith("projects/") else ""
    held: list[Held] = []
    for kind in kinds:
        try:
            names = kind.list_(gcp, project, region, number)
        except ApiError as exc:
            if kind.off_means_empty and _api_off(exc):
                continue
            inventory.unread.append(Unread(kind.name, _reason(exc)))
            continue
        except Exception as exc:  # noqa: BLE001 -- any failed read is reported as not read, never as empty
            inventory.unread.append(Unread(kind.name, _reason(exc)))
            continue
        if names is None:
            inventory.unread.append(Unread(kind.name, "the install state records no region (or project "
                                                      "number) to read it in"))
            continue
        for name in names:
            if not name:
                continue
            if _denied(name, deny):
                inventory.never_touch += 1
                continue
            held.append(Held(kind.name, name, _note(kind.name)))
    order = {k.name: i for i, k in enumerate(kinds)}
    inventory.held = sorted(set(held), key=lambda h: (order.get(h.kind, len(order)), h.name))
    return inventory


def render(inventory: Inventory) -> list[str]:
    """The listing in words."""
    project = inventory.project
    lines: list[str] = []
    if inventory.project_state == "DELETE_REQUESTED":
        return [f"{project} is pending deletion: Google shuts it down now and deletes it, and everything in it, "
                "after 30 days. Nothing in it was listed."]
    if inventory.project_state != "ACTIVE":
        reason = inventory.unread[0].reason if inventory.unread else "unknown"
        return [f"{project} could not be read ({reason}), so nothing was listed and this is not proof that "
                "nothing remains."]
    if inventory.nothing_remains:
        return [NOTHING_REMAINS.format(project=project)]
    if inventory.held or inventory.never_touch:
        lines.append(f"What remains in {project}, by kind and name (read-only):")
        width = max([len(h.kind) for h in inventory.held] or [0])
        for item in inventory.held:
            lines.append(f"  {item.kind:<{width}}  {item.name}" + (f"  [{item.note}]" if item.note else ""))
    if inventory.never_touch:
        lines.append(f"  and {inventory.never_touch} resource(s) on the install's never-touch list, not named here")
    if inventory.unread:
        lines.append(f"Not read ({len(inventory.unread)} kind(s)); this listing is not complete:")
        for unread in inventory.unread:
            lines.append(f"  {unread.kind}: not read ({unread.reason})")
        if not inventory.held and not inventory.never_touch:
            lines.append(f"Nothing was found in the kinds that were read, but that is not proof that nothing "
                         f"remains in {project}.")
    return lines
