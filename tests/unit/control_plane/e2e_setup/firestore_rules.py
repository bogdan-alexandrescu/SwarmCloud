"""A Firestore fake that refuses what real Firestore refuses, for the walk.

`StrictFirestore` is the shared fake (`..fakes.FakeFirestore`, through
`test_setup_routes.PathCheckingFirestore`) with google-cloud-firestore's own
rules in front of it: every collection and document path is split and checked
by the client library's `_path_helper` and `verify_path` (the code that
answered "A document must have an even number of path elements" on the live
deployment, audit 3's A2), and a reference built from another
(`collection(...).document(id)`, `document(...).collection(name)`) is checked
the same way. `stored_errors` holds every stored document to the server's
rules: a valid document path, a value the client can encode (`encode_dict`),
no array directly inside an array.
"""

from __future__ import annotations

import re
from typing import Any

from google.cloud.firestore_v1 import _helpers
from google.cloud.firestore_v1.base_client import _path_helper

from ..fakes import FakeCollectionRef, FakeDocumentRef
from ..test_setup_routes import PathCheckingFirestore

#: The server's rules on one path segment (an id): not empty, not `.` or
#: `..`, not `__.*__`, at most 1,500 bytes.
_RESERVED = re.compile(r"^__.*__$")
MAX_ID_BYTES = 1500


def path_errors(path: str, *, collection: bool) -> list[str]:
    """What the client library and the server refuse about `path`."""
    parts = tuple(_path_helper((path,)))
    errors: list[str] = []
    try:
        _helpers.verify_path(parts, is_collection=collection)
    except ValueError as exc:
        errors.append(f"{path!r}: {exc}")
    for part in parts:
        if part in ("", ".", "..") or _RESERVED.match(part) or len(part.encode()) > MAX_ID_BYTES:
            errors.append(f"{path!r}: the segment {part!r} is not a valid id")
    return errors


def _refuse(path: str, *, collection: bool) -> None:
    errors = path_errors(path, collection=collection)
    if errors:
        raise ValueError("; ".join(errors))


def _nested_arrays(value: Any, where: str, inside_array: bool = False) -> list[str]:
    if isinstance(value, list):
        if inside_array:
            return [f"{where}: an array directly inside an array"]
        return [e for i, item in enumerate(value)
                for e in _nested_arrays(item, f"{where}[{i}]", True)]
    if isinstance(value, dict):
        return [e for k, v in value.items() for e in _nested_arrays(v, f"{where}.{k}")]
    return []


def value_errors(path: str, data: dict[str, Any]) -> list[str]:
    """What the client library and the server refuse about a document's data."""
    errors = _nested_arrays(data, path)
    try:
        _helpers.encode_dict(data)
    except (TypeError, ValueError) as exc:
        errors.append(f"{path}: the client cannot encode it: {exc}")
    return errors


class StrictDocumentRef(FakeDocumentRef):
    def collection(self, name: str) -> FakeCollectionRef:
        _refuse(f"{self.path}/{name}", collection=True)
        return StrictCollectionRef(self._db, f"{self.path}/{name}")


class StrictCollectionRef(FakeCollectionRef):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        ref = super().document(doc_id)
        _refuse(ref.path, collection=False)
        return StrictDocumentRef(self._db, ref.path)


class StrictFirestore(PathCheckingFirestore):
    """The shared fake behind google-cloud-firestore's own path rules."""

    def collection(self, path: str) -> FakeCollectionRef:
        _refuse(path, collection=True)
        super().collection(path)  # the shared fake's own check as well
        return StrictCollectionRef(self, path)

    def document(self, path: str) -> FakeDocumentRef:
        _refuse(path, collection=False)
        super().document(path)
        return StrictDocumentRef(self, path)

    def stored_errors(self) -> list[str]:
        """Every stored document held to the server's rules."""
        errors: list[str] = []
        for path, data in sorted(self.docs.items()):
            errors += path_errors(path, collection=False)
            errors += value_errors(path, data)
        return errors
