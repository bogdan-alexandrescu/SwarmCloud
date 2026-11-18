#!/usr/bin/env python3
"""The prefix of every label and annotation key this platform writes.

    kubernetes/label_prefix.py                 # prints the prefix
    kubernetes/label_prefix.py --key workspace # prints <prefix>/workspace
    kubernetes/label_prefix.py --fill FILE     # writes the prefix over FILE's __LABEL_PREFIX__

The keys used to embed one organisation's domain, written out in every
manifest, in the admission policies' CEL and in the scripts that read the keys
back. The owner made the prefix configuration (8 November 2026, "configurable";
docs/design/generic-repository.md section 3.3):

  * `SWARM_LABEL_PREFIX`, when set, is the prefix;
  * else `swarm.<SWARM_ORG_DOMAIN>`, so a deployment whose configuration names
    its organisation's domain keeps exactly the keys it has today: renaming a
    key would orphan every live object and policy that matches it;
  * else NOTHING: the render refuses with `UNSET`. A guessed key is worse
    than none, because the workspace deployer's scope policy admits a
    namespace only by its key, and a key the live policy does not match makes
    every workspace namespace a stranger's.

Everything that writes or reads a key asks here: kubernetes/render.py (the
`__LABEL_PREFIX__` token of every manifest), kubernetes/apply.sh (the token in
terraform's swarm-system renders), kubernetes/network_parity.py (the live
annotations), scripts/register-tenant.sh and scripts/lib/workspace-guard.sh
(the workspace label). The standard library only: the workspace job's Debian
python3 and the call guard run this before anything is installed.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

PREFIX_ENV = "SWARM_LABEL_PREFIX"
ORG_DOMAIN_ENV = "SWARM_ORG_DOMAIN"

#: The refusal, one fixed sentence: a caller may match it, and it names no value.
UNSET = (
    "the label-key prefix is not configured: set SWARM_LABEL_PREFIX, or SWARM_ORG_DOMAIN "
    "for the default swarm.<org_domain>; no key is guessed"
)

#: A Kubernetes key prefix is a DNS subdomain (at most 253 characters,
#: lower-case labels); this one must also hold a dot, as a domain does. A value
#: that reaches YAML and CEL as text can hold nothing else.
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
PREFIX = re.compile(rf"(?=.{{1,253}}$){_LABEL}(\.{_LABEL})+")
#: The name after the slash: a Kubernetes key's name part, as this platform writes them.
NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?")


class LabelPrefixError(ValueError):
    """No prefix is configured, or the configured one is not a key prefix."""


def label_prefix(environ: Mapping[str, str] | None = None) -> str:
    """The configured prefix, or LabelPrefixError; never a default of its own."""
    env = os.environ if environ is None else environ
    prefix = str(env.get(PREFIX_ENV) or "").strip()
    if prefix:
        if not PREFIX.fullmatch(prefix):
            raise LabelPrefixError(f"{PREFIX_ENV} is not a key prefix (a lower-case domain name like swarm.example.com)")
        return prefix
    domain = str(env.get(ORG_DOMAIN_ENV) or "").strip().lower().lstrip("@")
    if domain:
        prefix = f"swarm.{domain}"
        # The domain itself holds a dot: `swarm.<domain>` always would.
        if not PREFIX.fullmatch(domain) or not PREFIX.fullmatch(prefix):
            raise LabelPrefixError(f"{ORG_DOMAIN_ENV} is not a domain name like example.com")
        return prefix
    raise LabelPrefixError(UNSET)


def label_key(name: str, environ: Mapping[str, str] | None = None) -> str:
    """`<prefix>/<name>`."""
    if not NAME.fullmatch(name):
        raise LabelPrefixError(f"{name!r} is not a key name")
    return f"{label_prefix(environ)}/{name}"


#: The token every manifest under kubernetes/ carries where a key's prefix goes.
PLACEHOLDER = "__LABEL_PREFIX__"


def fill(text: str, environ: Mapping[str, str] | None = None) -> str:
    """TEXT with the prefix over every PLACEHOLDER; TEXT as it was when it holds none."""
    return text.replace(PLACEHOLDER, label_prefix(environ)) if PLACEHOLDER in text else text


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if not args:
            print(label_prefix())
        elif len(args) == 2 and args[0] == "--key":
            print(label_key(args[1]))
        elif len(args) == 2 and args[0] == "--fill":
            # In place, and only once the whole text is filled: a refusal
            # leaves the file as it was.
            path = Path(args[1])
            text = path.read_text()
            filled = fill(text)
            if filled != text:
                path.write_text(filled)
        else:
            print("usage: label_prefix.py [--key NAME | --fill FILE]", file=sys.stderr)
            return 2
    except LabelPrefixError as exc:
        print(f"label prefix: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
