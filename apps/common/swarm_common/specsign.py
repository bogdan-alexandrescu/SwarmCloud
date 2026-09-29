"""The canonical form of a step's spec, signed by swarm-api and verified by the worker.

Contract request 34 (#342). A tenant's agents can write any task document of
their tenant (docs/multi-tenancy.md, "The Firestore row, in full"), so a
worker must not run a spec swarm-api did not sign. This module is the ONE
definition of which fields that signature covers and how they become bytes:
swarm-api signs `spec_digest(canonical_step_spec(task.to_firestore(),
task_id=task.id))`, and the worker verifies the same digest over the document
it fetched. Two copies of this projection would be two opinions about what
is signed. No cryptography lives here: signing is swarm-api's (Cloud KMS) and
verification is the worker's.

The encoding is RFC 8785 (JCS), implemented for exactly the values a
Firestore document written from JSON can hold, with no dependency: this
package is installed from a git tag by the plugin and declares none.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from typing import Any, Mapping

#: The canonical form's version. A change to the covered fields or the encoding
#: is a NEW format, never an edit to this one: a signature is over a format.
SPEC_FORMAT = 1

#: Domain separation. The bytes of a step spec cannot be read as another message.
SPEC_PURPOSE = "swarm.step-spec"

#: The keys of `Task.metadata` that swarm-api writes and the worker acts on.
#: Every other metadata key is the caller's label or another writer's record.
SIGNED_METADATA_KEYS = ("dispatch", "input_from", "expected_outputs")

#: JCS numbers are IEEE doubles, so an integer is exact only within this bound.
MAX_SAFE_INTEGER = 2**53 - 1


class SpecNotCanonical(ValueError):
    """A value in the spec has no canonical form (see `jcs`)."""


def canonical_step_spec(doc: Mapping[str, Any], *, task_id: str) -> bytes:
    """The bytes the signature covers, from a task document as stored.

    `task_id` is the id the document was READ BY (the worker's TASK_ID, the
    id swarm-api minted), never the document's own `id` field.
    """
    metadata = doc.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise SpecNotCanonical("metadata is not a map")
    spec = {
        "purpose": SPEC_PURPOSE,
        "format": SPEC_FORMAT,
        "task_id": task_id,
        "tenant_id": doc.get("tenant_id"),
        "workflow_id": doc.get("workflow_id"),
        "step_id": doc.get("step_id"),
        "submitted_by": doc.get("submitted_by"),
        "runner_profile": doc.get("runner_profile"),
        "resource_class": doc.get("resource_class"),
        "timeout_seconds": doc.get("timeout_seconds"),
        "max_attempts": doc.get("max_attempts"),
        "provider": doc.get("provider"),
        "model": doc.get("model"),
        "input": doc.get("input"),
        "depends_on": doc.get("depends_on"),
        "repository_url": doc.get("repository_url"),
        "repository_ref": doc.get("repository_ref"),
        "metadata": {key: metadata.get(key) for key in SIGNED_METADATA_KEYS},
    }
    return jcs(spec)


def spec_digest(canonical: bytes) -> bytes:
    """SHA-256 of the canonical bytes: what the KMS key signs."""
    return hashlib.sha256(canonical).digest()


def jcs(value: Any) -> bytes:
    """RFC 8785 serialisation of `value`, as UTF-8 bytes."""
    return _encode(value, "$").encode("utf-8")


def _encode(value: Any, path: str) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise SpecNotCanonical(f"{path} is an integer outside +/-(2**53 - 1)")
        return str(value)
    if isinstance(value, float):
        return _number(value, path)
    if isinstance(value, str):
        return _string(value, path)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    if isinstance(value, Mapping):
        items = []
        for key in value:
            if not isinstance(key, str):
                raise SpecNotCanonical(f"{path} has a key that is not a string")
            _string(key, path)  # refuses a lone surrogate before it is sorted
            items.append(key)
        # RFC 8785 3.2.3: members sorted by the UTF-16 code units of their RAW
        # names -- not their escaped form, which would put "\r" after "1".
        items.sort(key=lambda k: k.encode("utf-16-be"))
        return "{" + ",".join(
            _string(k, path) + ":" + _encode(value[k], f"{path}.{k}") for k in items
        ) + "}"
    raise SpecNotCanonical(f"{path} is a {type(value).__name__}, which JSON has no form for")


def _string(text: str, path: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SpecNotCanonical(f"{path} holds a lone surrogate") from exc
    # json.dumps with ensure_ascii=False escapes exactly what RFC 8785 3.2.2.2
    # does: '"', '\\', the five short forms, and \u00xx (lowercase) for the
    # rest of U+0000..U+001F. Everything else is written as itself.
    return json.dumps(text, ensure_ascii=False)


def _number(x: float, path: str) -> str:
    # RFC 8785 3.2.2.3: ECMAScript's Number.prototype.toString.
    if not math.isfinite(x):
        raise SpecNotCanonical(f"{path} is not a finite number")
    if x == 0:
        return "0"
    if x < 0:
        return "-" + _number(-x, path)
    # repr() is the shortest round-tripping decimal, as ECMAScript requires.
    _, digits_t, exponent = Decimal(repr(x)).as_tuple()
    digits = "".join(map(str, digits_t))
    stripped = digits.rstrip("0")
    exponent += len(digits) - len(stripped)
    digits = stripped
    k = len(digits)
    n = exponent + k
    if k <= n <= 21:
        return digits + "0" * (n - k)
    if 0 < n <= 21:
        return digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return "0." + "0" * (-n) + digits
    e = n - 1
    mantissa = digits if k == 1 else digits[0] + "." + digits[1:]
    return mantissa + "e" + ("+" if e >= 0 else "-") + str(abs(e))
