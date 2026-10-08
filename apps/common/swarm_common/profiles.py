"""Resource classes and runner profiles -- the admin-defined execution catalogue.

API callers choose a `runner_profile` by NAME and nothing else. They never supply
an image, a command, a resource spec or a backend. That is the whole point: it is
what stops an authenticated caller from turning the swarm into arbitrary compute.

Sizing note. These numbers are not guesses. They were measured on the reference
workstation on 2026-09-15: one working Claude Code lane was `claude` 1,532 MB +
`pytest` 769 MB + node/tsx guards 207 MB = ~2.5 GiB resident. The classes below
are roughly 2x that, and the worker exports peak RSS and peak disk per profile so
the numbers get corrected from production rather than from arithmetic.

Two hard rules follow from the stability requirement:
  * requests == limits. Bursting past a request is precisely what gets a
    container OOM-killed under node pressure, so we never do it.
  * Spot is disabled. Verified against GKE docs: Spot Pods cannot use Autopilot
    extended run time, so Spot and "no preemption" are mutually exclusive.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit


class Backend(str, Enum):
    CLOUD_RUN_JOB = "CLOUD_RUN_JOB"
    GKE_AUTOPILOT = "GKE_AUTOPILOT"
    AUTO = "AUTO"


class SpotStrategy(str, Enum):
    ON_DEMAND_ONLY = "on_demand_only"
    SPOT_PREFERRED = "spot_preferred"
    SPOT_ONLY = "spot_only"


class WorkerAction(str, Enum):
    """A platform action the WORKER performs instead of starting a runner.

    Contract request 33 (MERGE) and 35 (POST_VERDICT), ACCEPTED by the owner on
    2026-10-01 for #295. A profile that names one runs no agent, so a forge
    App key read by its Job's own service account is never in a container an
    agent shares (docs/merge-step.md §0, §1.3).
    """

    MERGE = "merge"
    POST_VERDICT = "post_verdict"


@dataclass(frozen=True)
class ResourceClass:
    """A sizing envelope. `cpu` and `memory_gib` are BOTH request and limit."""

    name: str
    cpu: float
    memory_gib: int
    disk_gib: int
    #: Capacity units consumed from the weighted global budget. Heavier classes
    #: cost more scheduling budget than light ones.
    units: int
    #: Cloud Run ephemeral disk is a Preview feature and, per Google's docs,
    #: disables live migration -- which is why mandatory checkpointing exists.
    requires_preview_disk: bool = False

    def __post_init__(self) -> None:
        if self.cpu <= 0 or self.memory_gib <= 0:
            raise ValueError(f"resource class {self.name}: cpu and memory must be positive")
        if self.cpu > 8 or self.memory_gib > 32:
            # Cloud Run's hard ceiling. Anything above must be GKE-only.
            raise ValueError(f"resource class {self.name} exceeds the Cloud Run Jobs ceiling")


# WORKSPACE SIZES ARE MEMORY, NOT DISK.
#
# These originally claimed 20/40/100 GiB of disk-backed ephemeral storage. That
# is a Cloud Run Preview feature the Terraform google provider cannot express:
# `empty_dir.medium` accepts only "MEMORY". The workspace is therefore a tmpfs
# carved out of the container's memory, so `disk_gib` is a slice OF `memory_gib`
# and not additional capacity.
#
# The measured reference workload -- Claude Code plus a Node/Python toolchain
# running a test suite -- peaks near 2.5 GiB, which leaves standard roughly
# 1.5 GiB of real workspace headroom. A large monorepo with node_modules will
# not fit on `standard`; use `browser` or `large`, or route to GKE.
#
# Upside: this path is fully GA and supports live migration, which the Preview
# disk explicitly does not -- so the reliability requirement that drove the
# Cloud Run choice is better served here than by the feature we set out to use.
RESOURCE_CLASSES: dict[str, ResourceClass] = {
    "standard": ResourceClass("standard", cpu=4, memory_gib=8, disk_gib=4, units=1),
    "browser": ResourceClass("browser", cpu=8, memory_gib=16, disk_gib=8, units=2),
    "large": ResourceClass("large", cpu=8, memory_gib=32, disk_gib=16, units=4),
}


# ---------------------------------------------------------------------------
# The inputs a caller may send a runner (contract request 25)
# ---------------------------------------------------------------------------
#
# Accepted by the owner on #142, 2026-09-25: "RunnerProfile.inputs goes in the
# frozen catalogue and the API enforces it for every caller, not only the
# bridge." Invariant 10 is unchanged -- a caller names a profile and supplies
# DATA, never an image, a command, a resource spec or a backend -- but an input
# means something only to the runner that reads it, and to another runner it
# can mean something else: `input.model` is read by the CLI runners and would
# choose the model a `claude-code` agent runs. So what may be sent is declared
# per profile, here, once. swarm-api refuses anything else at submission and
# the plugin's bridge refuses it sooner; both read this, and neither keeps a
# table of its own. Every profile declares: `browser` and `generic` last, by
# contract request 32 (#218), which added the kinds their inputs needed.

#: The kinds an input can be. `filename` is a bare file name with no
#: directory: the worker keeps only the last path segment of an artifact name
#: (`RunnerContext.artifact_path`), so `../x` would quietly become `x`.
#:
#: Added by contract request 32 (#218), for the two runners whose work IS
#: their input:
#:
#: * `url`: an http or https URL a browser may open. See `url_refusal`.
#: * `argument`: a value the generic runner appends to a catalogue argv, or
#:   runs in -- `_ARGUMENT_SAFE` and the `..` refusal in
#:   `agent_worker/runners/generic.py`, restated because the catalogue cannot
#:   import the worker. Existence and "inside the workspace" stay the
#:   runner's: only it has the workspace.
#: * `list`: a JSON array. Its BOUNDS ARE ITS LENGTH, both required, as a
#:   number's are; every element is `items`, and `items` may not itself be
#:   `required` -- an element of a list is always present, so that flag on an
#:   element has nothing to say.
#: * `object`: a JSON object in one of the fixed shapes `variants` names,
#:   chosen by its `type` key. A key its shape does not name is refused, as a
#:   key a profile does not declare is.
#: * `header`: a string that is, or could become, an HTTP header value:
#:   printable ASCII only (0x20-0x7E), which by construction rules out CR and
#:   LF and every other control character, so a caller cannot use it to
#:   inject a second header. `maximum` is required, as it is for a list: the
#:   bound is the string's length in characters.
INPUT_KINDS = ("number", "integer", "boolean", "string", "filename", "url", "argument", "list", "object", "header")

#: The signed 64-bit range, which is what Firestore stores an integer in.
#: Python reads a JSON integer of any length, so an integer input whose bounds
#: reached past this would accept a value the task write then fails to encode:
#: a 500 at the store instead of a 422 at the door (the review of #213).
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

#: `argument`: what `generic._ARGUMENT_SAFE` accepts, restated because the
#: catalogue cannot import the worker. No leading dash, so no value becomes a
#: flag; no leading slash; 256 characters at most.
_ARGUMENT = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._\-/]{0,255}")

#: The longest URL a browser task may name. Chromium's own limit is far
#: larger; a task's URL is stored with the task and shown with it, and past
#: this it is not a page address but a payload.
_URL_MAX_CHARS = 2048

#: A host, once `urlsplit` has extracted it and this module has lower-cased
#: it, must be exactly this: letters, digits, `.` and `-`. Nothing else is
#: plain ASCII host syntax, so this single check is what closes a
#: percent-encoded host (`interna%6cl`), a host carrying a literal backslash,
#: and a raw non-ASCII host at once -- see `url_refusal` and the entry's own
#: prose for the three bypasses a security review found here on 2026-09-29.
_HOST_CHARS = re.compile(r"[a-z0-9.-]+")

#: Every network `url_refusal` refuses a *global* IPv4 address in, besides
#: RFC 1918 and the rest of the ranges `ipaddress` itself would already
#: refuse as non-global. Chosen as an explicit, named list instead of relying
#: on `ipaddress.IPv4Address.is_global`, because that property's own
#: membership is not pinned across the versions this platform runs: the
#: CGNAT block (100.64.0.0/10) and 192.0.0.0/24 have both changed category
#: in `ipaddress` between Python 3.11 and 3.13. An explicit list is what a
#: reviewer can diff against RFC 1918, 5735 and 6890 directly, and it does
#: not move under this module on a Python upgrade the platform did not make
#: for this reason.
_URL_REFUSED_V4_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this network" (RFC 791)
    ipaddress.ip_network("10.0.0.0/8"),  # RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT, RFC 6598
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, and the metadata server
    ipaddress.ip_network("172.16.0.0/12"),  # RFC 1918
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # documentation (TEST-NET-1)
    ipaddress.ip_network("192.168.0.0/16"),  # RFC 1918
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # documentation (TEST-NET-2)
    ipaddress.ip_network("203.0.113.0/24"),  # documentation (TEST-NET-3)
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved
    ipaddress.ip_network("255.255.255.255/32"),  # limited broadcast
)

#: The IPv6 equivalent of `_URL_REFUSED_V4_NETWORKS`, for a literal IPv6 host
#: that is neither IPv4-mapped nor a NAT64/6to4 embedding (both unwrapped to
#: an IPv4 address and checked against the list above instead; see
#: `_embedded_v4`).
_URL_REFUSED_V6_NETWORKS = (
    ipaddress.ip_network("::1/128"),  # loopback
    ipaddress.ip_network("::/128"),  # unspecified
    ipaddress.ip_network("100::/64"),  # discard-only, RFC 6666
    ipaddress.ip_network("2001:db8::/32"),  # documentation
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("ff00::/8"),  # multicast
)

#: Private Google Access, which the worker's NetworkPolicy opens
#: (kubernetes/network-policies/allow-egress.yaml, rule 3). These are global
#: addresses by any definition, so they are refused explicitly rather than by
#: `is_global`: the pod can reach them, but they are authenticated Google
#: APIs, useless to a browser task without a token, and not what "the public
#: internet" is meant to include.
_URL_REFUSED_NETWORKS = (
    ipaddress.ip_network("199.36.153.4/30"),
    ipaddress.ip_network("199.36.153.8/30"),
)

#: Every IPv6 range that EMBEDS an IPv4 address, so a refused v4 address
#: reachable through any of them would otherwise pass
#: `_URL_REFUSED_V6_NETWORKS` unseen. IPv4-mapped (`::ffff:a.b.c.d`) is
#: handled separately by `ipaddress.IPv6Address.ipv4_mapped`; the rest are
#: unwrapped by `_embedded_v4`:
#: * `64:ff9b::/96` -- NAT64, RFC 6052 (well-known prefix).
#: * `64:ff9b:1::/48` -- NAT64, RFC 8215 (local-use prefix; the embedding
#:   follows RFC 6052 section 2.2's PL48 layout: 48-bit prefix, 16 bits of
#:   v4, an 8-bit zero field, 16 more bits of v4, 40-bit suffix).
#: * `2002::/16` -- 6to4, RFC 3056.
#: * `::ffff:0:0:0/96` -- "IPv4-translated", RFC 6052's SIIT form
#:   (`::ffff:0:a.b.c.d`; note the extra `:0:` before the address, which is
#:   what distinguishes it from IPv4-mapped).
#: * `::/96` -- IPv4-compatible, deprecated (RFC 4291 says so; RFC 6540 says
#:   not to originate or accept it) but still a parseable literal
#:   (`::a9fe:a9fe`), so still refused here explicitly rather than assumed
#:   gone.
_URL_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")
_URL_NAT64_LOCAL_PREFIX = ipaddress.ip_network("64:ff9b:1::/48")
_URL_6TO4_PREFIX = ipaddress.ip_network("2002::/16")
_URL_SIIT_PREFIX = ipaddress.ip_network("::ffff:0:0:0/96")
_URL_IPV4_COMPATIBLE_PREFIX = ipaddress.ip_network("::/96")

#: Names that are never a public page: GKE's metadata server is
#: `metadata.google.internal`; `.local`/`.localhost` never leave the host or
#: the cluster (`cluster.local`); and `.svc` is the short, no-FQDN-suffix
#: form Kubernetes' own DNS resolves for a Service
#: (`<service>.<namespace>.svc`, e.g. `kubernetes.default.svc`), which the
#: pod's search path completes to `.svc.cluster.local` for any name under
#: five dots (`ndots:5`) -- added 2026-09-29, after the owner found the
#: dot-count rule this replaced still admitted it. See `url_refusal`'s
#: docstring for what actually closes the general search-path gap; this
#: suffix closes only the specific, well-known `.svc` shorthand.
_URL_REFUSED_SUFFIXES = (".internal", ".local", ".localhost", ".svc")

#: How much of a refused value a refusal repeats. A caller who sent three
#: hundred digits needs the bound, not the digits back.
_SHOWN_VALUE_CHARS = 40


def _bound(value: float) -> str:
    """`33554432`, not `3.35544e+07`: a caller copies a bound, and `:g` rounds it."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _embedded_v4(address: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The IPv4 address a NAT64, 6to4, SIIT or IPv4-compatible address
    embeds, or None. IPv4-mapped is handled by the caller
    (`address.ipv4_mapped`) and never reaches here."""
    packed = address.packed
    if address in _URL_NAT64_PREFIX:
        return ipaddress.IPv4Address(packed[12:16])
    if address in _URL_NAT64_LOCAL_PREFIX:
        # RFC 6052 section 2.2, PL48: prefix(6 bytes) + v4-hi(2) + u(1, zero)
        # + v4-lo(2) + suffix(5). The `u` byte at packed[8] is skipped.
        return ipaddress.IPv4Address(bytes([packed[6], packed[7], packed[9], packed[10]]))
    if address in _URL_6TO4_PREFIX:
        return ipaddress.IPv4Address(packed[2:6])
    if address in _URL_SIIT_PREFIX:
        return ipaddress.IPv4Address(packed[12:16])
    if address in _URL_IPV4_COMPATIBLE_PREFIX:
        return ipaddress.IPv4Address(packed[12:16])
    return None


def url_refusal(value: str) -> str:
    """Why `value` is not a URL a browser task may open, or "" when it is.

    One home for the rule, so swarm-api, the plugin's bridge and the browser
    runner (`_check_url`, which today checks the scheme and that there is a
    host) give one answer. Not one of #218's three questions -- declaring
    `url` as a kind at all raises it; see the entry's prose in
    docs/contract-change-requests.md for why, and for four bypasses a
    security review found and closed here on 2026-09-29.

    THIS IS NOT THE SSRF CONTROL, AND CANNOT BE. It sees the URL a caller
    typed, never the page's redirects, its subresources, its script's
    requests, or what a name resolves to when the pod asks. The control is the
    network: the worker's NetworkPolicy drops every private range but the
    metadata server, and the metadata server answers only a request carrying
    `Metadata-Flavor: Google`, which a navigation does not send. What this
    buys is a 422 naming the reason, instead of a task that waits out
    `timeout_ms` against an address the network silently drops (Dataplane V2
    drops; the sender sees a timeout), and a URL with a password in it that
    is never stored with the task, served by the API or shown in the UI.

    Refuses rather than normalises. Every check below runs on the raw string
    or on `urlsplit`'s own view of it: a host that is not already plain ASCII
    (`[a-z0-9.-]+` once lower-cased) is refused outright rather than
    IDNA/UTS46-normalised and re-checked, because a normalising rule has to
    track whatever WHATWG host-parsing Chromium does, forever, while a
    refusing rule only has to be a subset of what Chromium accepts. A caller
    whose real target is an internationalised domain sends its ASCII
    (punycode) form, which the site already answers to -- the owner accepted
    this as the rule on 2026-09-29 (no separate IDN allowance).

    An underscore is refused by the same `[a-z0-9.-]+` check as any other
    character outside that set, with no separate rule: RFC 952/1035 do not
    allow one in a hostname label at all (some internal DNS -- SRV records,
    `_service._proto.name` -- uses one anyway, which is one more reason a
    browser task should not be handed a host carrying one).

    WHAT THIS DOES NOT DO, as of the owner's decision on 2026-09-29: it does
    not refuse a host by dot count. An earlier revision refused any non-IP
    host with fewer than two dots, which caught `kubernetes.default` and
    `swarm-api.swarm-system` but also every bare apex domain a browser task
    might legitimately target (`github.com`, `example.com` both have exactly
    one dot) -- for a browser profile, refusing those is refusing the
    profile's main use. It was also incomplete on its own terms: a three-label
    name ending in `.svc` (`kubernetes.default.svc`) has two dots and was
    never caught by it either. The general gap -- GKE's `ndots:5` pod
    resolver tries every search domain before the absolute name for anything
    under five dots -- is NOT closed by any check in this function, and
    cannot be from here: it sees the string the caller sent, never what the
    pod's resolver does with it. The real controls are the worker's
    NetworkPolicy (which does not depend on what a name resolves to), the
    pod's own DNS config (tracked as #341: set `ndots:1` and drop search
    domains, which removes the search-path trial entirely rather than
    guessing at every name shape it could produce), and the planned
    `context.route` guard (see the entry's *Preconditions*). What this
    function still refuses is the single-label case (`kubernetes`,
    `metadata`), which resolves only through the cluster's search path and
    is never a real page address, and the specific `.svc` shorthand below,
    which is the one case named in the review that is also a fixed, known
    string rather than an open-ended shape.
    """
    if len(value) > _URL_MAX_CHARS:
        return f"it is longer than {_URL_MAX_CHARS} characters"
    if any(ch < " " or ch == "\x7f" or ord(ch) > 0x7E for ch in value):
        return "it contains a space, a control character or a non-ASCII character"
    if "\\" in value:
        return "it contains a backslash, which a browser treats as a host or path separator"
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 -- raises ValueError on a port out of range
    except ValueError:
        return "it is not a URL"
    if parsed.scheme not in ("http", "https"):
        return "only http and https are opened"
    if parsed.username is not None or parsed.password is not None:
        return "it carries credentials, which would be stored with the task and shown with it"
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        return "it has no host"
    # An IP LITERAL IS CHECKED BEFORE THE HOST-CHARACTER RULE, not after: an
    # IPv6 literal's `hostname` is unbracketed and colon-bearing
    # (`64:ff9b::808:808`), which `_HOST_CHARS` never matches, and `ipaddress`
    # itself already rejects a backslash, a percent sign or a non-ASCII
    # character in an address -- there is nothing left for a second charset
    # check to catch there.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None:
        if not _HOST_CHARS.fullmatch(host):
            return (
                "its host is not letters, digits, '.' and '-' once lower-cased -- a "
                "browser's own host parsing accepts more than this, including a "
                "percent-encoded or backslash-bearing host, and this module refuses "
                "rather than reproduces it"
            )
        labels = host.split(".")
        if any(label == "" for label in labels):
            return "its host has an empty label ('..' in it, or it starts or ends with '.')"
        if any(label.strip("-") == "" for label in labels):
            return "its host has a label made only of hyphens, which is not a valid domain label"
        # A browser reads a host whose last label is a number (`2852039166`,
        # `0xa9.254.169.254`) as an IPv4 address in another notation, which
        # `ip_address` does not parse. No public suffix starts with a digit.
        if labels[-1][:1].isdigit():
            return "its host ends in a number, which a browser reads as an address"
        if not labels[-1][:1].isalpha():
            return f"its host's last label starts with {labels[-1][:1]!r}, not a letter"
        # SINGLE LABEL ONLY, not a general dot-count rule: see the docstring
        # above for why the broader rule this replaced was both too costly
        # (it refused bare apex domains) and still incomplete.
        if len(labels) < 2:
            return "its host is a single label, which only the cluster's search path resolves"
        if host.endswith(_URL_REFUSED_SUFFIXES):
            return "its host is a cluster or node-local name"
        return ""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        else:
            embedded = _embedded_v4(address)
            if embedded is not None:
                address = embedded
    if isinstance(address, ipaddress.IPv4Address):
        if any(address in net for net in _URL_REFUSED_V4_NETWORKS) or any(
            address in net for net in _URL_REFUSED_NETWORKS
        ):
            return "its host is not a public address"
    elif any(address in net for net in _URL_REFUSED_V6_NETWORKS):
        return "its host is not a public address"
    return ""


class InputRefused(ValueError):
    """An input a profile does not accept, and why.

    `key` is the first key refused and `keys` every one; `expected` is the
    declared bound a value broke, or None when the key is not declared at all
    or is required and was not sent.
    The message names the key and the bound, and never repeats a value longer
    than it has to.
    """

    def __init__(
        self,
        message: str,
        *,
        key: str,
        keys: tuple[str, ...] = (),
        expected: str | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.keys = keys or (key,)
        self.expected = expected


@dataclass(frozen=True)
class RunnerInput:
    """One key of a task's `input` that a runner reads, and what it must be.

    The bounds are what the runner can do anything useful with. A value outside
    them is refused at submission rather than coerced, or crashed on, after
    admission has spent a lease on it.

    A NUMBER HAS BOTH BOUNDS, and an integer's bounds lie inside the signed
    64-bit range. A declaration without them is refused here, not left to
    review: the mock's `steps`, `sleep_seconds` and `cpu_burn_seconds` were
    declared with a floor only, so `steps: 10**30` passed every check and
    failed at the Firestore write (the review of #213).
    """

    kind: str
    minimum: float | None = None
    maximum: float | None = None
    #: What the runner does with it, in the runner's own terms.
    means: str = ""
    #: Values inside the bounds that are refused all the same, each with what
    #: the platform would read it as instead -- the mock's exit codes 77, 78
    #: and 143. Pairs, not a dict, so the declaration stays hashable.
    refused: tuple[tuple[Any, str], ...] = ()
    #: A `string` must be one of these, when any are named: a name from a
    #: catalogue the runner owns, such as the generic runner's commands.
    choices: tuple[str, ...] = ()
    #: The caller must send this key. Read for a profile's keys and for an
    #: object's fields. Refused on a list's `items`: an element of a list is
    #: always present, so `required` on it has nothing to say.
    required: bool = False
    #: What each element of a `list` must be.
    items: RunnerInput | None = None
    #: An `object`'s shapes: the value of its `type` key, to the fields that
    #: shape takes besides `type`. Excluded from the hash, as
    #: `RunnerProfile.inputs` is; frozen read-only below.
    variants: Mapping[str, Mapping[str, RunnerInput]] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.kind not in INPUT_KINDS:
            raise ValueError(f"input kind {self.kind!r} is not one of {INPUT_KINDS}")
        numeric = self.kind in ("number", "integer")
        #: `list` and `header` MUST give both bounds, like a number: without
        #: them a list or a header string is unbounded until the task write
        #: fails. `string` MAY give a `maximum` -- a length bound in
        #: characters, for keys such as `selector`, `text` and `key`, whose
        #: content the runner does not otherwise constrain -- and when it
        #: does, an omitted `minimum` defaults to 0 rather than being
        #: required, because most bounded strings have no meaningful floor.
        strictly_bounded = numeric or self.kind in ("list", "header")
        length_boundable = strictly_bounded or self.kind == "string"
        if not length_boundable and (self.minimum is not None or self.maximum is not None):
            raise ValueError(
                f"a {self.kind} input has no bounds; only a number, a list, a header or a "
                "string does"
            )
        if not numeric and self.refused:
            raise ValueError(f"a {self.kind} input refuses no values; only a number does")
        if strictly_bounded and (self.minimum is None or self.maximum is None):
            raise ValueError(
                f"a {self.kind} input declares both a minimum and a maximum; without "
                "one, a JSON number -- or a string, for a header -- of any size passes "
                "and fails at the store instead"
            )
        if self.kind == "string" and self.maximum is not None and self.minimum is None:
            object.__setattr__(self, "minimum", 0.0)
        if self.kind in ("list", "header") and not (
            float(self.minimum).is_integer() and float(self.maximum).is_integer() and self.minimum >= 0
        ):
            raise ValueError(f"a {self.kind}'s bounds are its length: whole numbers, from 0")
        if self.choices and self.kind != "string":
            raise ValueError(f"a {self.kind} input has no choices; only a string does")
        if (self.kind == "list") != isinstance(self.items, RunnerInput):
            raise ValueError("a list input names what its elements are, and nothing else does")
        if self.kind == "list" and self.items is not None and self.items.required:
            raise ValueError(
                "a list's items are always present; `required` on them is refused"
            )
        if (self.kind == "object") != bool(self.variants):
            raise ValueError("an object input names its shapes, and nothing else does")
        if self.variants:
            frozen: dict[str, Mapping[str, RunnerInput]] = {}
            for shape, fields in self.variants.items():
                for name, declared in fields.items():
                    if name == "type" or not isinstance(declared, RunnerInput):
                        raise ValueError(
                            f"shape {shape!r}: field {name!r} must be a RunnerInput, "
                            "and `type` is the shape's name, not a field"
                        )
                frozen[shape] = MappingProxyType(dict(fields))
            object.__setattr__(self, "variants", MappingProxyType(frozen))
        for bound in (self.minimum, self.maximum):
            if bound is not None and not math.isfinite(bound):
                raise ValueError("an input's bounds must be finite numbers")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(f"input bounds {self.minimum}..{self.maximum} admit nothing")
        # Both bounds are set for an integer by now: the check above refused it.
        if self.kind == "integer" and not (
            self.minimum >= INT64_MIN and self.maximum <= INT64_MAX
        ):
            raise ValueError(
                f"integer input bounds {self.minimum}..{self.maximum} leave the signed "
                "64-bit range Firestore stores"
            )
        for _, reads_as in self.refused:
            if not reads_as:
                raise ValueError("a refused value must say what the platform would read it as")

    def describe(self) -> str:
        """`integer 1..255 except 77, 78, 143`: the kind and the bound, as a caller reads it."""
        # `__post_init__` gives a number both bounds and anything else neither.
        if self.kind == "list" and self.items is not None:
            return f"list of {_bound(self.minimum)}..{_bound(self.maximum)}, each {self.items.describe()}"
        if self.variants:
            return f"object, `type` one of {' | '.join(self.variants)}"
        if self.minimum is not None and self.maximum is not None:
            text = f"{self.kind} {_bound(self.minimum)}..{_bound(self.maximum)}"
        else:
            text = self.kind
        if self.choices:
            text += f", one of {' | '.join(self.choices)}"
        if self.refused:
            text += " except " + ", ".join(str(value) for value, _ in self.refused)
        return text

    def check(self, key: str, value: Any) -> Any:
        """`value`, normalised (an integral float becomes an int), or InputRefused."""
        expected = self.describe()
        article = "an" if expected[0] in "aeio" else "a"  # "a url"
        wanted = f"input {key!r} must be {article} {expected}"

        def refuse(detail: str = "") -> InputRefused:
            shown = repr(value)
            if len(shown) > _SHOWN_VALUE_CHARS:
                shown = f"{shown[:_SHOWN_VALUE_CHARS]}... ({len(shown)} characters)"
            return InputRefused(
                f"{wanted}, not {shown}{detail}", key=key, expected=expected
            )

        if self.kind in ("number", "integer"):
            # A bool is an int to Python and is refused: `sleep_seconds: true`
            # is a caller who meant something else.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise refuse()
            # json.loads reads NaN and Infinity, and every comparison with NaN
            # is False, so the bounds below would let it straight through.
            if isinstance(value, float) and not math.isfinite(value):
                raise refuse(" -- NaN and Infinity are not numbers a runner can use")
            if self.kind == "integer":
                if isinstance(value, float):
                    if not value.is_integer():
                        raise refuse()
                    value = int(value)
            if self.minimum is not None and value < self.minimum:
                raise refuse()
            if self.maximum is not None and value > self.maximum:
                raise refuse()
            for refused, reads_as in self.refused:
                if value == refused:
                    raise refuse(f": {reads_as}")
            return value
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise refuse(" (true or false)")
            return value
        if self.kind == "list":
            if not isinstance(value, list):
                raise refuse()
            if not self.minimum <= len(value) <= self.maximum:
                raise refuse(f" -- it has {len(value)} entries")
            # Named by position, so a refusal says WHICH element: `actions[3].url`.
            return [self.items.check(f"{key}[{index}]", item) for index, item in enumerate(value)]
        if self.kind == "object":
            if not isinstance(value, Mapping):
                raise refuse()
            shape = value.get("type")
            if not isinstance(shape, str) or shape not in self.variants:
                raise refuse()
            fields = self.variants[shape]
            unknown = sorted(set(value) - set(fields) - {"type"})
            if unknown:
                raise refuse(f" -- a {shape!r} takes {sorted(fields) or 'no field'}, not {unknown}")
            missing = sorted(name for name, spec in fields.items() if spec.required and name not in value)
            if missing:
                raise refuse(f" -- a {shape!r} needs {missing}")
            checked = {
                name: fields[name].check(f"{key}.{name}", value[name])
                for name in sorted(value)
                if name != "type"
            }
            return {"type": shape, **checked}
        if not isinstance(value, str):
            raise refuse()
        if self.kind == "filename" and (
            not value or value in (".", "..") or "/" in value or "\\" in value or "\x00" in value
        ):
            raise refuse(" -- a bare file name, with no directory")
        if self.kind == "argument" and (not _ARGUMENT.fullmatch(value) or ".." in value):
            raise refuse(
                " -- letters, digits, '.', '_', '-' and '/', starting with none of '-' or "
                "'/', at most 256 characters, and no '..'"
            )
        if self.kind == "url":
            reason = url_refusal(value)
            if reason:
                raise refuse(f" -- {reason}")
        if self.kind == "header":
            if not (self.minimum <= len(value) <= self.maximum):
                raise refuse(f" -- it has {len(value)} characters")
            if any(ch < " " or ch == "\x7f" or ord(ch) > 0x7E for ch in value):
                raise refuse(" -- printable ASCII only (0x20-0x7E), which rules out CR and LF")
        if self.kind == "string" and self.maximum is not None and not (
            self.minimum <= len(value) <= self.maximum
        ):
            raise refuse(f" -- it has {len(value)} characters")
        if self.choices and value not in self.choices:
            raise refuse()
        return value


def _frozen_inputs(declared: Mapping[str, RunnerInput]) -> Mapping[str, RunnerInput]:
    """A read-only copy, so no caller can widen a profile's declaration in place."""
    return MappingProxyType(dict(declared))


@dataclass(frozen=True)
class RunnerProfile:
    name: str
    image: str
    resource_class: str
    backend: Backend
    #: The argv of the RUNNER, which the worker lifecycle starts as a supervised
    #: CHILD (`agent_worker.lifecycle._runner_argv`). NEVER a container command:
    #: both worker images' ENTRYPOINT is the lifecycle (`tini -- python -m
    #: agent_worker`), and a container `command` replaces it, switching off
    #: fencing, cancel, heartbeat, checkpointing and lease release at once. A
    #: dispatcher names the profile in RUNNER_PROFILE and sets no command.
    #:
    #: Called `command` until 2026-09-24. Under that name both dispatchers put
    #: it on the container, and every GKE pod ran the bare runner (incident
    #: wf_ebb3ab2d65664707a559). Renamed by contract request 18, accepted by
    #: the owner, so `list(profile.command)` can no longer be written.
    runner_argv: tuple[str, ...]
    #: Provider whose quota and credentials this runner consumes. None means the
    #: runner needs no external provider, so it works before a tenant registers
    #: any key -- which is what keeps the mock smoke path always available.
    provider: str | None = None
    secrets: tuple[str, ...] = ()
    #: True when `secrets` lists INTERCHANGEABLE credentials rather than a set
    #: that must all be present. claude-code takes either metered API access or
    #: a subscription token, never both, so requiring all of them would refuse a
    #: tenant who supplied exactly the one they pay for.
    secrets_any_of: bool = False
    #: True when this runner DECLARES what an attempt cost rather than measuring
    #: it from a provider's bill. The mock reports $0.00 on purpose; a reader
    #: has to be able to tell that deliberate zero from a real one, so every
    #: cost figure over a declared profile is marked as declared
    #: (`swarm_api.outcomes`, "Reported cost · not a bill").
    #:
    #: Contract request 24, ACCEPTED by the owner on 2026-09-25 (#185, the
    #: decisions comment, item 9). Until then the outcome ledger named `mock`
    #: in a set of its own, a restatement of the catalogue held to it only by
    #: a test.
    cost_declared: bool = False
    #: Whether this profile may be dispatched AT ALL.
    #:
    #: A profile is disabled, not deleted, when its provider stops working.
    #: Deleting the entry would refuse new work (which is the point) but also
    #: strand anything already queued against it, and break the catalogue
    #: lookup for every task document that still names it -- 4 exist for
    #: `codex` today. The flag keeps the profile readable and makes re-enabling
    #: one word.
    #:
    #: `disabled_reason` is required when this is False and is served to the
    #: caller. "unknown runner_profile" would be a lie: the profile is known,
    #: it is refused, and a caller who cannot tell those apart goes looking for
    #: a typo that is not there.
    available: bool = True
    disabled_reason: str = ""
    supports_checkpoint: bool = True
    spot: SpotStrategy = SpotStrategy.ON_DEMAND_ONLY
    timeout_seconds: int = 3600
    #: Seconds between mandatory workspace checkpoints. This is what replaces
    #: live migration on the Cloud Run ephemeral-disk path.
    checkpoint_interval_seconds: int = 120
    #: The keys of `input`, besides `prompt`, a caller may set for this
    #: profile, each with its kind and bounds (contract request 25). For a
    #: profile that declares, swarm-api refuses any other key, from every
    #: caller, with 422 `invalid_input`; the plugin's bridge reads the same
    #: declaration to refuse it before it travels. `check_inputs` below is the
    #: rule both call, and the one place each of the three meanings is read.
    #:
    #: EMPTY IS A DECLARATION: the profile takes its prompt and nothing else.
    #: That is what closes `input.model` on `claude-code`, which its runner
    #: would pass as `--model`.
    #:
    #: THERE IS NO "NOT DECLARED YET". Until contract request 32 (#218) this
    #: field could be None, for `browser` and `generic`, and then only the
    #: input's size was bounded -- an amendment to request 25 the owner
    #: confirmed on 2026-09-26 as the state until #218 was decided. Both
    #: declare now, so the type is the one request 25 was accepted with.
    #:
    #: Excluded from the hash: a mapping is not hashable, and a profile's
    #: identity is its name.
    inputs: Mapping[str, RunnerInput] = field(default_factory=dict, hash=False)
    #: When set, the lifecycle performs this action itself and starts no
    #: runner child, so no agent ever runs under this profile's Job identity.
    #: `runner_argv` must then be empty, and it must be non-empty otherwise.
    #: Contract request 33, ACCEPTED by the owner on 2026-10-01.
    worker_action: WorkerAction | None = None
    #: When True, the lifecycle never restores a checkpoint for this
    #: profile, on any attempt, not only the first. False (the default)
    #: preserves today's behaviour for every existing profile. `review`
    #: must never resume an agent inside a workspace a previous attempt
    #: left behind -- its whole judgement depends on seeing the checked-out
    #: head honestly (merge-step.md's own threat model, S0).
    #:
    #: Contract request 36, ACCEPTED by the owner on 2026-10-01. The name is
    #: the owner's choice of 2026-09-30 over `restore_on_retry`, which read as
    #: "restore on attempt 1, skip only on retries". Checkpointing itself
    #: stays on (invariant 8); only the restore is skipped.
    never_restore_checkpoint: bool = False

    def __post_init__(self) -> None:
        if self.worker_action is not None and self.runner_argv:
            raise ValueError(
                f"runner {self.name}: a worker_action profile starts no runner, so its "
                "runner_argv must be empty (contract request 33)"
            )
        if self.worker_action is None and not self.runner_argv:
            raise ValueError(
                f"runner {self.name}: an empty runner_argv starts nothing; only a "
                "worker_action profile may have one (contract request 33)"
            )
        for key, declared in self.inputs.items():
            if not isinstance(declared, RunnerInput):
                raise ValueError(f"runner {self.name}: input {key!r} is not a RunnerInput")
            if key == "prompt":
                raise ValueError(
                    f"runner {self.name}: `prompt` is every profile's input and is not declared"
                )
            if key == "command" and self.name != "generic":
                raise ValueError(
                    f"runner {self.name}: `command` is invariant 10's own guard "
                    "(_NEVER, FORBIDDEN_CALLER_FIELDS); only `generic` is exempted, "
                    "and only for its closed catalogue -- see contract request 32"
                )
            if key == "command" and self.name == "generic" and (
                declared.kind != "string" or set(declared.choices) != set(_GENERIC_COMMANDS)
            ):
                raise ValueError(
                    f"runner {self.name}: `command` may only be a string whose choices "
                    "are exactly GENERIC_COMMANDS -- the one exemption contract request "
                    "32 asks _NEVER to carry, not a general licence to declare it"
                )
        object.__setattr__(self, "inputs", _frozen_inputs(self.inputs))
        if not self.available and not self.disabled_reason:
            raise ValueError(
                f"runner {self.name}: a disabled profile must say why. A caller "
                "told only that a known profile was refused has nothing to act on."
            )
        if self.resource_class not in RESOURCE_CLASSES:
            raise ValueError(f"runner {self.name}: unknown resource class {self.resource_class}")
        if self.spot is not SpotStrategy.ON_DEMAND_ONLY:
            raise ValueError(
                f"runner {self.name}: Spot is disabled platform-wide; Spot Pods cannot "
                "use extended run time and would violate the no-preemption requirement"
            )


#: The mock's test knobs (agent_worker/runners/mock.py), as the owner decided
#: them on #142 on 2026-09-25. Left out on purpose, because they write platform
#: records rather than shape a run: `spend`, which the worker books as the
#: attempt's cost; `provider`, which names whose quota document a park writes;
#: `credential_revoked_times` and `credential_detail`, which simulate a refused
#: credential; `quota_detail` and `reset_at`, which dress a simulated rate
#: limit. tests/unit/mcp/test_runner_inputs.py holds every key here to a
#: `payload` read in the mock's source.
_MOCK_INPUTS: dict[str, RunnerInput] = {
    # ONE HOUR, for both. Deliberately past the mock's own 600 s timeout
    # (`RUNNER_PROFILES["mock"].timeout_seconds`, which a caller may lower and
    # never raise): a sleep, or a burn, longer than the timeout is how the
    # timeout path is exercised, and #142's 120 s cancel check sits well inside
    # either. Six timeouts' worth is room for any such test; past it a value is
    # a typo, and without a ceiling at all `10**30` failed at the task write.
    "sleep_seconds": RunnerInput(
        "number", minimum=0, maximum=3600, means="how long the run sleeps, in total"
    ),
    "cpu_burn_seconds": RunnerInput(
        "number", minimum=0, maximum=3600, means="how long it burns CPU, in total"
    ),
    # A THOUSAND. Each step writes a small progress file into `work/`, and
    # every later checkpoint carries every one, so this bounds the files a
    # mock checkpoint can hold to a thousand. The largest any test or script
    # sends is 60 (tests/unit/worker/startup_harness.py); a checkpoint that
    # holds partial work needs two.
    "steps": RunnerInput(
        "integer",
        minimum=1,
        maximum=1000,
        means="how many progress files, and checkpoints, it writes",
    ),
    "fail": RunnerInput("boolean", means="fail on purpose, after the steps"),
    "fail_message": RunnerInput("string", means="the error a failure reports"),
    # THE WORKER DECIDES WHAT AN ATTEMPT WAS FROM ITS EXIT CODE, so a code it
    # reads as something else turns a failure on purpose into that thing
    # (`agent_worker/runners/base.py`, `lifecycle._finalise`). Minimum 1: a 0
    # beside the result.json the mock writes is recorded SUCCEEDED. The three
    # refused are the EXIT_* codes that are not a plain failure, restated
    # because the catalogue cannot import the worker; test_runner_inputs.py
    # reads base.py's EXIT_* constants and fails when one is missing here
    # (contract request 21 asks for the codes to have a shared home).
    "exit_code": RunnerInput(
        "integer",
        minimum=1,
        maximum=255,
        means="the exit code a failure uses",
        refused=(
            (77, "77 is a provider rate limit to the worker, which parks the task "
                 "instead of failing it -- and with no quota.json, again on every attempt"),
            (78, "78 is the code a runner exits with when its credential is refused, "
                 "so a failure recorded with it reads as one"),
            (143, "143 is a runner stopped by SIGTERM, which the worker records CANCELLED"),
        ),
    ),
    "artifact_text": RunnerInput("string", means="what the output artifact holds"),
    "artifact_name": RunnerInput("filename", means="the output artifact's file name"),
    # THE BOUNDED PARK. Withheld by the review of PR #201, because the mock
    # raised its rate limit on every attempt and a park does not spend one, so
    # a task sent this parked and resumed until someone cancelled it. The mock
    # now parks the task's FIRST attempt only, by the task's `attempt_count`,
    # which admission increments in the lease's own transaction and the
    # lifecycle hands every runner. Not by a count in its own `work/`: that
    # reached the next attempt only through the park's checkpoint, and a park
    # whose checkpoint failed lost it (the review of #213).
    "quota_exhausted": RunnerInput(
        "boolean",
        means="park the first attempt on a simulated provider rate limit; the next one runs",
    ),
    # One second to one hour. A wait under the worker's in-place threshold is
    # retried in place three times and then parked, a longer one is parked at
    # once; the mock refuses every retry of the same attempt, so either way one
    # attempt parks. The task waits parked, holding no capacity, and an hour is
    # longer than any test of the park path needs to be held.
    "retry_after_seconds": RunnerInput(
        "integer",
        minimum=1,
        maximum=3600,
        means="the retry-after that simulated rate limit reports",
    ),
}


#: A wait the browser runner hands Playwright, in milliseconds. FROM 1, NOT 0:
#: Playwright reads a timeout of 0 as "no timeout", so a 0 here would let one
#: action wait out the whole 5400 s attempt. FIVE MINUTES at most: a selector
#: that has not appeared in five minutes is not coming, and the attempt's own
#: timeout is the ceiling above that.
_BROWSER_WAIT_MS = {"minimum": 1, "maximum": 300_000}

#: `selector`, `text` and `key` may carry arbitrary Unicode (a CSS selector, a
#: page's own text, a key combination), so they are `string` with a length
#: bound rather than `header`, which is ASCII-only. 4 KiB: far past any real
#: selector or typed text, and small enough that a caller who sent this much
#: sent a payload, not a selector.
_BROWSER_TEXT_MAX = 4096

#: The browser runner's actions (`agent_worker/runners/browser.py`, `body`),
#: one shape per `type`, each field as the runner reads it. `type` is matched
#: exactly: the runner lower-cases it, so `"Goto"` runs today and is refused
#: here. The restatement is held to the runner's source by a test, as the
#: mock's keys are (tests/unit/mcp/test_runner_inputs.py).
_BROWSER_ACTION = RunnerInput(
    "object",
    means="one step of the run, in the shape its `type` names",
    variants={
        "goto": {
            "url": RunnerInput("url", required=True, means="the page to open"),
            "wait_until": RunnerInput(
                "string",
                choices=("load", "domcontentloaded", "networkidle", "commit"),
                means="when the load counts as done; default load",
            ),
        },
        "click": {
            "selector": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
                means="the element to click",
            )
        },
        "fill": {
            "selector": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
                means="the field to fill",
            ),
            "text": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
                means="what to type into it; default empty",
            ),
        },
        "press": {
            "selector": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
                means="the element to press a key in",
            ),
            "key": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
                means="the key; default Enter",
            ),
        },
        "wait_for": {
            "selector": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX, required=True,
                means="the element to wait for",
            ),
            "timeout_ms": RunnerInput(
                "integer", **_BROWSER_WAIT_MS, means="how long to wait; default the task's timeout_ms"
            ),
        },
        # 0..60: the runner clamps above 60 with `min(..., 60.0)`, so a larger
        # value is refused rather than quietly shortened.
        "wait": {"seconds": RunnerInput("number", minimum=0, maximum=60, means="how long to pause; default 1")},
        "screenshot": {
            "name": RunnerInput("filename", means="the artifact's file name; default by position"),
            "full_page": RunnerInput("boolean", means="the whole page, not the viewport; default true"),
        },
        "extract": {
            "selector": RunnerInput(
                "string", minimum=0, maximum=_BROWSER_TEXT_MAX,
                means="the element whose text is kept; default body",
            ),
            "name": RunnerInput("filename", means="the artifact's file name; default by position"),
        },
    },
)

#: The browser runner's inputs. NEITHER `url` NOR `actions` IS REQUIRED ALONE:
#: the runner needs one or the other, which `required` cannot say, so that
#: refusal stays the runner's (contract request 32, *Owner's decisions*).
_BROWSER_INPUTS: dict[str, RunnerInput] = {
    "url": RunnerInput("url", means="opened first, before any action"),
    # 200: the runner's MAX_ACTIONS.
    "actions": RunnerInput(
        "list", minimum=0, maximum=200, items=_BROWSER_ACTION, means="run in order, after `url`"
    ),
    "timeout_ms": RunnerInput(
        "integer", **_BROWSER_WAIT_MS, means="how long any one action may take; default 30000"
    ),
    # Three minutes: Chromium starts in seconds, and a launch still waiting at
    # three minutes is a pod short of /dev/shm, not a slow start.
    "launch_timeout_ms": RunnerInput(
        "integer", minimum=1, maximum=180_000, means="how long Chromium may take to start; default 60000"
    ),
    # Up to 4K. The viewport is rendered in the pod's memory, and a full-page
    # screenshot of it is written to the workspace, which is memory too.
    "viewport_width": RunnerInput("integer", minimum=320, maximum=3840, means="pixels; default 1280"),
    "viewport_height": RunnerInput("integer", minimum=240, maximum=2160, means="pixels; default 900"),
    # `header`, not `string`: this value is sent as the User-Agent HTTP
    # header verbatim, so it must be printable ASCII -- a caller could
    # otherwise inject a second header through it. 512: real User-Agent
    # strings run under 300 characters; past 512 it is not a browser
    # signature.
    "user_agent": RunnerInput(
        "header", minimum=0, maximum=512, means="the User-Agent sent; default Chromium's"
    ),
    "extract_text": RunnerInput("boolean", means="keep the final page's text as page.txt; default true"),
    "screenshot": RunnerInput("boolean", means="keep a final full-page screenshot; default true"),
}

#: The generic runner's catalogue (`agent_worker/runners/generic.py`,
#: `GENERIC_COMMANDS`), restated because the catalogue cannot import the
#: worker, and held to it by a test, as `_ARGUMENT` is.
_GENERIC_COMMANDS = ("make", "npm-build", "npm-ci", "npm-test", "pytest", "uv-sync")

#: Built without `inputs` first, so `_GENERIC_INPUTS` below can read this
#: profile's OWN `timeout_seconds` for its `timeout_seconds` input's ceiling
#: instead of restating the number as a second literal. `RunnerProfile` sets
#: no `timeout_seconds` for `generic`, so this is the class default (3600) --
#: reading it here, rather than writing `3600` again, is what keeps the two
#: from drifting if a future change gives `generic` its own value.
_GENERIC_PROFILE = RunnerProfile(
    name="generic",
    image="agent-runtime-base",
    resource_class="standard",
    backend=Backend.CLOUD_RUN_JOB,
    runner_argv=("python", "-m", "agent_worker.runners.generic"),
    provider=None,
)

#: The generic runner's inputs (`agent_worker/runners/generic.py`).
#:
#: `command` IS A NAME, NOT AN ARGV: `choices` is `_GENERIC_COMMANDS`, the
#: runner's own `GENERIC_COMMANDS` restated, whose argv are constants in the
#: runner. Declaring it reads as invariant 10 relaxed and is not -- see
#: contract request 32, Question 2 (#218) -- and `RunnerProfile.__post_init__`
#: enforces the one exemption `_NEVER` (tests/unit/mcp/test_runner_inputs.py)
#: is asked to carry: `command` declared as a string whose `choices` are
#: exactly `_GENERIC_COMMANDS`, for `generic` only.
#:
#: THE FOUR LIMITS MAY ONLY LOWER THE PLATFORM'S (`runners/limits.py`). Each
#: ceiling here is the value the worker exports by default -- the profile's
#: `timeout_seconds`, and `WorkerConfig`'s grace and output caps -- so a
#: request above it is refused at the door instead of accepted and clamped.
#: The runner still clamps to what the attempt's worker exports, which an
#: operator may have set lower. From 1: the runner reads 0 or less as "not
#: asked", so a 0 was accepted and meant nothing.
_GENERIC_INPUTS: dict[str, RunnerInput] = {
    "command": RunnerInput(
        "string",
        required=True,
        choices=_GENERIC_COMMANDS,
        means="the platform catalogue entry to run; the platform owns its argv",
    ),
    # 32: `GenericCommand.max_arguments`. Read for `pytest` only.
    "paths": RunnerInput(
        "list",
        minimum=0,
        maximum=32,
        items=RunnerInput("argument", means="an existing path inside the workspace"),
        means="pytest only: what to run; default everything",
    ),
    "target": RunnerInput("argument", means="make only: the target; default all"),
    "working_directory": RunnerInput(
        "argument", means="a directory inside the workspace to run in; default the workspace"
    ),
    "timeout_seconds": RunnerInput(
        "number", minimum=1, maximum=_GENERIC_PROFILE.timeout_seconds,
        means="lowers the command's wall clock",
    ),
    "grace_seconds": RunnerInput(
        "number", minimum=1, maximum=20, means="lowers the wait between SIGTERM and SIGKILL"
    ),
    "max_stdout_bytes": RunnerInput(
        "integer", minimum=1, maximum=32 * 1024 * 1024, means="lowers the stdout kept"
    ),
    "max_stderr_bytes": RunnerInput(
        "integer", minimum=1, maximum=8 * 1024 * 1024, means="lowers the stderr kept"
    ),
}


#: What claude-code and codex take beside the prompt: contract request 28,
#: accepted by the owner on 2026-09-28 for #265. `issue` names an issue in the
#: task's OWN repository; the worker fetches its title, body and comments
#: read-only with the tenant's forge credential, writes them to the workspace
#: as `issue.md` and names that file in the prompt (`agent_worker.issue`). The
#: number is data, and so is the text it fetches (invariant 10).
#:
#: THE CEILING IS 999999 BECAUSE `describe()` PRINTS SIX SIGNIFICANT DIGITS. A
#: bound past that is shown rounded -- 2**31 - 1 reads "2.14748e+09" -- which
#: tells a caller a limit the check does not apply. An issue number past it is
#: out of reach of any repository this platform works on.
_CLI_AGENT_INPUTS: dict[str, RunnerInput] = {
    "issue": RunnerInput(
        "integer",
        minimum=1,
        maximum=999_999,
        means=(
            "an issue in the task's repository: its title, body and comments are "
            "written to issue.md in the workspace and named in the prompt"
        ),
    ),
}


#: Why the three #295 profiles are refused, served to a caller who names one.
_DISABLED_UNTIL_342 = (
    "the merge chain (#295) is disabled for every tenant until signed step "
    "specs (#342) are enforced and the review and merge GitHub Apps exist."
)


RUNNER_PROFILES: dict[str, RunnerProfile] = {
    "mock": RunnerProfile(
        name="mock",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.mock"),
        provider=None,          # no key required -- smoke tests must always work
        cost_declared=True,     # its $0.00 is declared, not measured (request 24)
        timeout_seconds=600,
        checkpoint_interval_seconds=30,
        inputs=_MOCK_INPUTS,
    ),
    # Built from `_GENERIC_PROFILE` (declared above, alongside `_GENERIC_INPUTS`,
    # so the input's own `timeout_seconds` ceiling can read this profile's
    # `timeout_seconds` instead of restating it).
    "generic": replace(_GENERIC_PROFILE, inputs=_GENERIC_INPUTS),
    "claude-code": RunnerProfile(
        name="claude-code",
        image="agent-runtime-base",
        resource_class="standard",
        # GKE Autopilot since contract request 53 (applied 2026-10-08, after
        # request 55's canary): DISPATCHED -> RUNNING p50 ~23 s / max 44 s on
        # five real steps, against Cloud Run's DISPATCHED -> STARTING p50
        # 128 s / p90 212 s, 90-95 % of it in Cloud Run's own provisioning
        # (#363, #625, #667). Extended run time is what keeps a two-hour agent
        # from being consolidated away, and it is on-demand only: no Spot
        # (invariant 6). The tenants' Cloud Run Jobs for this profile are kept
        # until 2026-10-15 as the rollback (terraform/infra/locals.tf
        # `cloud_run_fallback_profiles`): rolling back is this one line.
        backend=Backend.GKE_AUTOPILOT,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        # BOTH are accepted, and a tenant supplies exactly one. Claude Code runs
        # on either metered API access (ANTHROPIC_API_KEY) or a Claude
        # subscription token (CLAUDE_CODE_OAUTH_TOKEN, from
        # `claude setup-token`). Requiring the API key would make a tenant who
        # already pays for a subscription buy metered access on top of it.
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
        inputs=_CLI_AGENT_INPUTS,
    ),
    "codex": RunnerProfile(
        name="codex",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.codex"),
        provider="openai",
        secrets=("OPENAI_API_KEY",),
        timeout_seconds=7200,
        inputs=_CLI_AGENT_INPUTS,
        # DISABLED 2026-09-23 by the owner's decision: the platform is focusing
        # on Claude, and codex does not currently work here anyway. A twenty-step
        # load test that day dispatched four codex steps and all four failed
        # with "openai refused the credential" -- the tenant's
        # swarm-tenant-eng-openai holds a single version written 2026-09-16 that
        # the provider rejects.
        #
        # Left in the catalogue rather than deleted, so the four task documents
        # that name it stay readable and re-enabling is one word. Nothing was
        # queued or running against it when this landed.
        available=False,
        disabled_reason=(
            "codex is disabled on this platform. The provider refused the "
            "registered credential and the platform is focused on Claude. Use "
            "claude-code."
        ),
    ),
    "browser": RunnerProfile(
        name="browser",
        image="agent-runtime-browser",
        resource_class="browser",
        # Chromium under Cloud Run needs a large /dev/shm; GKE gives us direct
        # control over that, so browser work stays on Autopilot.
        backend=Backend.GKE_AUTOPILOT,
        runner_argv=("python", "-m", "agent_worker.runners.browser"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY",),
        timeout_seconds=5400,
        inputs=_BROWSER_INPUTS,
    ),
    # --- #295, the merge chain: contract requests 33, 35 and 36 -------------
    #
    # ALL THREE ARE DISABLED. The owner accepted the requests on 2026-10-01 and
    # decided in the same breath that #295 stays disabled for every tenant
    # until signed step specs (#342) are enforced and the owner has created the
    # review and merge GitHub Apps. `available=False` is what swarm-api refuses
    # on submission (`validation.validate_runner_profile`), so an entry here
    # is readable and dispatchable by nobody. Terraform creates no Job for any
    # of them either (terraform/infra/locals.tf, `profiles_without_a_job`):
    # until each runs as its own service account, a Job would run it as the
    # tenant's worker account, which is the hole these profiles exist to close.
    #
    # EXCEPT `merge`, ENABLED BY CONTRACT REQUEST 47 (owner, 2026-10-04, #295):
    # merging moved off the GitHub-side App into this step, which uses the
    # tenant's EXISTING `-git` token. The owner accepted, on #476, that an
    # agent holding `-git` can therefore also merge: the token that pushes the
    # branch is the token that lands it, so a separate merge App and account
    # bought nothing an agent with `-git` could not already do. #342 (signed
    # step specs) is closed, which lifted the other half of the gate.
    "merge": RunnerProfile(
        name="merge",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=(),
        worker_action=WorkerAction.MERGE,
        # The credential is never mounted: the worker reads the tenant's `-git`
        # secret at merge time only, after every check that needs none, and
        # never puts it in the workspace, a file, an environment or a log
        # (#219). `provider` is what parks the step CREDENTIAL_MISSING, at no
        # cost, for a tenant that has not registered one.
        provider="git",
        secrets=(),
        timeout_seconds=600,
        inputs={},
    ),
    "post-verdict": RunnerProfile(
        name="post-verdict",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=(),
        worker_action=WorkerAction.POST_VERDICT,
        # The credential is never mounted: its secret is read by the worker at
        # post-time, as `swarm-<tenant>-post-verdict`, the Job's own service
        # account. `provider` is what parks the step CREDENTIAL_MISSING, at no
        # cost, for a tenant that has not registered one, and keeps Terraform
        # from creating a post-verdict Job for that tenant.
        #
        # Listing `git-review` here is not by itself sufficient to keep the
        # ordinary worker account off this secret (contract request 35, "What
        # it would break if accepted", and its #364 amendment).
        provider="git-review",
        secrets=(),
        timeout_seconds=300,
        inputs={},
        available=False,
        disabled_reason=_DISABLED_UNTIL_342,
    ),
    # Identical to claude-code except its name -- and so its Job and service
    # account, `swarm-<tenant>-review` -- and never_restore_checkpoint, and,
    # since contract request 53 moved claude-code to GKE Autopilot, its
    # backend: this profile is retired (no Job for any tenant,
    # `profiles_without_a_job`), so request 53 did not move it.
    "claude-code-review": RunnerProfile(
        name="claude-code-review",
        image="agent-runtime-base",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
        inputs=_CLI_AGENT_INPUTS,
        never_restore_checkpoint=True,
        available=False,
        disabled_reason=_DISABLED_UNTIL_342,
    ),
    # CONTRACT REQUEST 48, accepted by the owner 2026-10-05 (#625): claude-code
    # on agent-runtime-indexer, the image that carries the repository index's
    # toolchain once it left agent-runtime-base. Identical to claude-code in
    # every field but its name and its image, by that decision. swarm-api names
    # it for its own index runs (`swarm_api.repoindex.INDEXER_PROFILE`); a
    # caller picks it by name like any other and sends no image (invariant 10).
    "indexer": RunnerProfile(
        name="indexer",
        image="agent-runtime-indexer",
        resource_class="standard",
        backend=Backend.CLOUD_RUN_JOB,
        runner_argv=("python", "-m", "agent_worker.runners.claude_code"),
        provider="anthropic",
        secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
        secrets_any_of=True,
        timeout_seconds=7200,
        inputs=_CLI_AGENT_INPUTS,
    ),
}


def resolve_backend(profile: RunnerProfile) -> Backend:
    """Resolve AUTO to a concrete backend.

    Cloud Run Jobs is preferred for everything it can hold, because it has no
    nodes, no autoscaler and no node upgrades -- far fewer ways for the platform
    to kill a task. GKE Autopilot takes what does not fit, and claude-code,
    which names it for its start latency (contract request 53).
    """
    if profile.backend is not Backend.AUTO:
        return profile.backend
    rc = RESOURCE_CLASSES[profile.resource_class]
    if rc.cpu <= 8 and rc.memory_gib <= 32:
        return Backend.CLOUD_RUN_JOB
    return Backend.GKE_AUTOPILOT


def check_inputs(profile: RunnerProfile, raw: Mapping[str, Any]) -> dict[str, Any]:
    """The inputs `raw` sets, each checked against what `profile` declares.

    `raw` holds the keys BESIDES the prompt. Returns them normalised (an
    integral float for an integer input becomes an int), or raises
    InputRefused: for every key the profile does not declare, naming them all;
    for every required key `raw` does not send, naming them all; or for the
    first declared key whose value is out of its bounds, naming the bound.
    """
    declared = profile.inputs
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        if declared:
            offered = ", ".join(
                f"{key} ({spec.describe()})" for key, spec in sorted(declared.items())
            )
            message = (
                f"runner profile {profile.name!r} does not declare {unknown} as an input; "
                f"it declares {offered}"
            )
        else:
            message = (
                f"runner profile {profile.name!r} takes its prompt and no other input, "
                f"so {unknown} cannot be sent"
            )
        raise InputRefused(message, key=unknown[0], keys=tuple(unknown))
    missing = sorted(key for key, spec in declared.items() if spec.required and key not in raw)
    if missing:
        raise InputRefused(
            f"runner profile {profile.name!r} needs {missing} in its input",
            key=missing[0],
            keys=tuple(missing),
        )
    return {key: declared[key].check(key, raw[key]) for key in sorted(raw)}
