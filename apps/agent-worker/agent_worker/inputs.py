"""Staging an upstream step's artifacts into this attempt's workspace.

A workflow step may declare `input_from = {upstream_step: artifact_filename}`.
The API validates it against the DAG (an `input_from` source must also be a
`depends_on`, so the artifact cannot be staged from a step that may not have
run) and the service rewrites it onto the task as
`metadata.input_from = {upstream_TASK_id: filename}`. This module is the half
that was missing: the worker reading that declaration and putting the file
where the agent will find it.

    metadata.input_from: {"task_abc": "summary.md"}
      -> tenants/<tenant>/tasks/task_abc/attempts/<the one that SUCCEEDED>/artifacts/summary.md
      -> <workspace>/work/summary.md          (the agent's current directory)

Three properties are worth stating out loud, because each of them is a decision
that could reasonably have gone the other way.

**A declared input that cannot be staged FAILS THE ATTEMPT.** It does not warn
and continue. A step that declares an input has been promised that file, and its
prompt is written as though the file is there; starting the agent without it
produces a confident, wrong answer that nothing downstream can tell apart from a
right one. Every refusal below names the upstream task and the filename, because
those two strings are what a person needs in order to fix it.

**The artifacts that count belong to the attempt that SUCCEEDED.** An upstream
task may have been tried three times, and two of those attempts may have
uploaded a half-written file under the same name into their own prefixes. The
successful attempt's manifest is `task.result_summary.artifacts`, written by
`lifecycle._upload_outputs` at the moment that attempt finished, and it records
the exact URI of every artifact it uploaded. That manifest is the only thing
this module resolves against; it is authoritative about which attempt produced
the file, about whether it was uploaded at all, and about how large it is.

**Everything is resolved before anything is downloaded.** A workflow step with
four declared inputs, one of them missing, fails without moving the other three
across the network into a memory-backed workspace. The size cap is applied
twice for the same reason: once to the sizes the upstream manifests declare, so
an oversized set never starts, and again to the bytes that actually arrive,
because a manifest is a claim about the object and only the download measures
it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from swarm_common.states import TaskState

from .errors import InputUnavailable
from .objectstore import ObjectStore, validate_key

#: The key under `task.metadata`. Per-dispatch options live there because
#: `Task.metadata` is a free-form dict on a FROZEN type -- adding a field to
#: `Task` would be a contract change, and `metadata` is already used this way
#: (`metadata.origin`, `metadata.unit`). The contract types the field as a dict
#: but says nothing about what is inside it, so its contents are read
#: defensively here.
METADATA_KEY = "input_from"


@dataclass(frozen=True)
class DeclaredInput:
    """One `{upstream_task_id: filename}` entry, syntactically checked."""

    upstream_task_id: str
    filename: str


@dataclass(frozen=True)
class ArtifactReference:
    """Where a declared input actually lives, per the upstream's own manifest."""

    key: str
    uri: str
    size_bytes: int


@dataclass(frozen=True)
class StagedInput:
    """One input as it ended up on disk. Reported to the agent and the caller."""

    upstream_task_id: str
    filename: str
    #: Relative to the work directory, which is the agent's current directory.
    path: str
    size_bytes: int
    uri: str | None = None
    #: True when a restored checkpoint already contained the file, so this
    #: attempt did not fetch it again. See `stage_inputs`.
    from_checkpoint: bool = False

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "task_id": self.upstream_task_id,
            "filename": self.filename,
            "path": self.path,
            "bytes": self.size_bytes,
        }
        if self.uri:
            record["uri"] = self.uri
        if self.from_checkpoint:
            record["from_checkpoint"] = True
        return record


# ---------------------------------------------------------------------------
# reading the declaration
# ---------------------------------------------------------------------------


def declared_inputs(metadata: Any) -> list[DeclaredInput]:
    """Parse `metadata.input_from`, or return an empty list.

    An ABSENT declaration is the overwhelmingly common case -- no workflow, or a
    step that stages nothing -- and returns immediately, so a deployment that
    does not use the feature performs no extra read and behaves exactly as it
    did before this module existed.

    A declaration that is PRESENT but malformed raises. Skipping it would make
    the feature inert on precisely the dispatch that asked for it, which is the
    silent-degradation failure this platform keeps hitting.
    """
    if not isinstance(metadata, dict):
        # `Task.metadata` is typed `dict[str, Any]` in the frozen contract, so a
        # value of any other type cannot be carrying an `input_from` mapping:
        # there is nothing here to honour and nothing being dropped. Failing a
        # task over the shape of a field this feature is not used by would be a
        # regression for a deployment that never opted in.
        return []
    raw = metadata.get(METADATA_KEY)
    if raw is None or raw == {}:
        return []
    if not isinstance(raw, dict):
        raise InputUnavailable(
            f"metadata.{METADATA_KEY} must be a mapping of upstream task id to "
            f"artifact filename, got {type(raw).__name__}"
        )

    declared: list[DeclaredInput] = []
    # Sorted by upstream id so the order of staging, the order of the manifest
    # and the order of any failure message are the same on every run.
    for upstream, filename in sorted(raw.items(), key=lambda item: str(item[0])):
        if not isinstance(upstream, str) or not upstream.strip():
            raise InputUnavailable(
                f"metadata.{METADATA_KEY} contains a key that is not an upstream "
                f"task id: {upstream!r}"
            )
        if not isinstance(filename, str) or not filename.strip():
            raise InputUnavailable(
                f"metadata.{METADATA_KEY}[{upstream!r}] must name an artifact "
                f"filename, got {filename!r}"
            )
        declared.append(DeclaredInput(upstream.strip(), filename.strip()))

    _assert_distinct_destinations(declared)
    return declared


def _assert_distinct_destinations(declared: list[DeclaredInput]) -> None:
    """Refuse two upstream tasks that stage the same filename.

    `input_from` names the artifact in the UPSTREAM's prefix and that same name
    is where it lands here, so there is no way to say "take both patches". Two
    entries with one name would mean one file overwriting the other and an agent
    running on whichever was written last -- a wrong answer, arrived at quietly.
    Refusing is the honest outcome, and it is a submission-time mistake that a
    person can fix, unlike a silently dropped input.
    """
    by_name: dict[str, list[str]] = {}
    for item in declared:
        by_name.setdefault(item.filename, []).append(item.upstream_task_id)
    for filename, sources in sorted(by_name.items()):
        if len(sources) > 1:
            raise InputUnavailable(
                f"upstream tasks {', '.join(sorted(sources))} all stage {filename!r} "
                "into this workspace; one would silently overwrite the other, so "
                "the attempt is refused rather than run on whichever arrived last"
            )


def destination_for(work: Path, filename: str, *, reserved: frozenset[str]) -> Path:
    """The path a declared input is staged to, or a refusal.

    `filename` reaches here from a caller's workflow submission and is NOT
    pattern-checked by the API, so every unsafe shape is rejected here:
    an absolute path, a traversal, and any name whose first segment is one the
    worker itself owns inside `work/` (the clone directory, the worker's own
    state directory, and the four control files the runner protocol uses). A
    file staged over `input.json` would be destroyed by the worker moments
    later; one staged over `result.json` would be read back as the agent's
    own result.
    """
    if filename.startswith("/"):
        raise InputUnavailable(
            f"input artifact name must be relative to the workspace, got {filename!r}"
        )
    parts = PurePosixPath(filename).parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise InputUnavailable(
            f"input artifact name contains an unsafe path segment: {filename!r}"
        )
    if "\\" in filename or "\x00" in filename:
        raise InputUnavailable(
            f"input artifact name contains an unsafe character: {filename!r}"
        )
    if parts[0] in reserved:
        raise InputUnavailable(
            f"input artifact {filename!r} would be staged over {parts[0]!r}, which "
            "the worker owns inside the work directory"
        )
    destination = work.joinpath(*parts)
    # Belt and braces against a shape the segment check did not anticipate: the
    # resolved path must still be inside the work directory.
    try:
        destination.resolve().relative_to(work.resolve())
    except ValueError as exc:
        raise InputUnavailable(
            f"input artifact {filename!r} resolves outside the work directory"
        ) from exc
    return destination


# ---------------------------------------------------------------------------
# resolving the upstream artifact
# ---------------------------------------------------------------------------


def fetch_upstream_task(db: Any, *, upstream_task_id: str, tenant_id: str) -> dict[str, Any]:
    """Read the upstream task document, refusing one belonging to another tenant.

    Deliberately NOT `TenantMismatchError`. That exception means "a document
    THIS attempt owns names another tenant", and the worker's response to it is
    to exit having written nothing at all, because every write would land on
    someone else's data. This is a different situation: our own documents are
    fine, and a task id in our own metadata pointed somewhere it must not. The
    correct response is to fail THIS attempt, loudly, in the ordinary way.

    Invariant 9 is not resting on this check alone -- `artifact_key` builds the
    object key from THIS attempt's tenant, so a cross-tenant artifact is
    unreachable by construction. This check exists so the operator gets "that
    task belongs to another tenant" rather than "file not found".
    """
    snapshot = db.collection("tasks").document(upstream_task_id).get()
    if not snapshot.exists:
        raise InputUnavailable(
            f"upstream task {upstream_task_id} has no task document, so the "
            "artifact it was to supply cannot be located"
        )
    document = snapshot.to_dict() or {}
    actual = document.get("tenant_id")
    if actual != tenant_id:
        raise InputUnavailable(
            f"upstream task {upstream_task_id} belongs to tenant {actual!r}, not "
            f"{tenant_id!r}; refusing to stage another tenant's artifact"
        )
    return document


def artifact_reference(
    task_document: dict[str, Any],
    *,
    tenant_id: str,
    upstream_task_id: str,
    filename: str,
) -> ArtifactReference:
    """Locate one artifact in the upstream's successful-attempt manifest."""
    state = task_document.get("state")
    if state != TaskState.SUCCEEDED.value:
        raise InputUnavailable(
            f"upstream task {upstream_task_id} is {state!r}, not "
            f"{TaskState.SUCCEEDED.value!r}; {filename!r} would have to come from "
            "the attempt that succeeded, and there is no such attempt"
        )

    summary = task_document.get("result_summary")
    if not isinstance(summary, dict):
        raise InputUnavailable(
            f"upstream task {upstream_task_id} succeeded without recording what it "
            f"uploaded, so {filename!r} cannot be located"
        )

    entries = summary.get("artifacts")
    entries = entries if isinstance(entries, list) else []
    names: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str):
            continue
        names.append(name)
        if name != filename:
            continue
        uri = entry.get("uri")
        if not isinstance(uri, str) or not uri:
            raise InputUnavailable(
                f"upstream task {upstream_task_id} recorded {filename!r} without a "
                "location, so it cannot be fetched"
            )
        # An entry with no `bytes` is NOT an entry of size zero, and the
        # difference is the whole of the cap in `stage_inputs`: a zero lets any
        # number of artifacts of any size through a check that is a sum. The
        # two honest readings are "refuse it" and "measure it", and measuring
        # is not available here -- the `ObjectStore` protocol exposes no size,
        # so the only way to learn one is to download the object, which is the
        # decision the cap exists to make BEFORE anything is pulled into a
        # memory-backed workspace. So: refuse. It costs nothing against any
        # manifest this platform has written, because the single writer
        # (`lifecycle._upload_outputs`) records `path.stat().st_size` on every
        # entry it appends; an entry reaching here without a usable size came
        # from no writer of ours. A negative is refused for the same reason a
        # missing one is -- it would SUBTRACT from the total and let a genuinely
        # oversized sibling through.
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            recorded = "no size" if size is None else f"a size of {size!r}"
            raise InputUnavailable(
                f"upstream task {upstream_task_id} recorded {filename!r} with "
                f"{recorded}, where a whole number of bytes was required; that "
                "number is what bounds how much one attempt may stage into a "
                "memory-backed workspace, so the input is refused rather than "
                "fetched uncounted"
            )
        return ArtifactReference(
            key=artifact_key(
                uri,
                tenant_id=tenant_id,
                upstream_task_id=upstream_task_id,
                filename=filename,
            ),
            uri=uri,
            size_bytes=size,
        )

    # A skipped artifact is a different fault with a different remedy: the file
    # was produced and deliberately not uploaded because the attempt was over
    # its artifact cap. Saying "not found" would send somebody looking at the
    # agent's prompt instead of at `MAX_ARTIFACT_BYTES`.
    skipped = summary.get("artifacts_skipped")
    if isinstance(skipped, list) and filename in skipped:
        raise InputUnavailable(
            f"upstream task {upstream_task_id} produced {filename!r} but did not "
            "upload it: the attempt exceeded its artifact size cap"
        )
    raise InputUnavailable(
        f"upstream task {upstream_task_id} did not produce an artifact named "
        f"{filename!r}; it uploaded "
        + (", ".join(repr(n) for n in sorted(names)) if names else "nothing")
    )


def artifact_key(
    uri: str, *, tenant_id: str, upstream_task_id: str, filename: str
) -> str:
    """The object key for a recorded artifact URI, checked against its prefix.

    The URI is whatever the store rendered at upload time -- `gs://<bucket>/<key>`
    in production, `file://<root>/<key>` for a local run -- so rather than
    parsing either scheme, the key is recovered as the TAIL of the URI starting
    at this tenant's prefix for this upstream task. That does two jobs at once:
    it gets the attempt id (which is the part nothing else here knows) and it
    proves the recorded location is inside the prefix it is supposed to be in.
    """
    prefix = f"tenants/{tenant_id}/tasks/{upstream_task_id}/attempts/"
    index = uri.find(prefix)
    if index < 0:
        raise InputUnavailable(
            f"the recorded location of {filename!r} ({uri}) is not inside tenant "
            f"{tenant_id!r}'s prefix for task {upstream_task_id}; refusing to read it"
        )
    attempt_id, separator, tail = uri[index + len(prefix):].partition("/")
    if not attempt_id or not separator or tail != f"artifacts/{filename}":
        raise InputUnavailable(
            f"the recorded location of {filename!r} ({uri}) is not an artifact of a "
            f"single attempt of task {upstream_task_id}; refusing to read it"
        )
    return validate_key(f"{prefix}{attempt_id}/artifacts/{filename}")


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------


def stage_inputs(
    declared: list[DeclaredInput],
    *,
    work: Path,
    store: ObjectStore,
    db: Any,
    tenant_id: str,
    logger: Any,
    resumed: bool,
    max_total_bytes: int,
    reserved: frozenset[str],
) -> list[StagedInput]:
    """Put every declared input on disk, or fail the attempt trying."""
    if not declared:
        return []

    # Pass 1: destinations. Every unsafe name is refused before a single
    # control-plane read, so a hostile filename costs nothing.
    destinations = {
        item: destination_for(work, item.filename, reserved=reserved)
        for item in declared
    }

    # Pass 2: what a restored checkpoint already holds.
    #
    # Same rule as the clone in `lifecycle._maybe_clone`, for the same two
    # reasons. Re-fetching would overwrite a file the agent may have EDITED
    # before it was interrupted, and a resumed attempt would otherwise start
    # failing the moment a retention rule expired an upstream artifact it no
    # longer needs to read.
    staged: list[StagedInput] = []
    pending: list[DeclaredInput] = []
    for item in declared:
        destination = destinations[item]
        if resumed and destination.is_file():
            staged.append(
                StagedInput(
                    upstream_task_id=item.upstream_task_id,
                    filename=item.filename,
                    path=destination.relative_to(work).as_posix(),
                    size_bytes=destination.stat().st_size,
                    from_checkpoint=True,
                )
            )
            logger.info(
                "declared input already present from the checkpoint; not re-fetching",
                upstream_task=item.upstream_task_id,
                filename=item.filename,
            )
            continue
        pending.append(item)

    # Pass 3: resolve every remaining input BEFORE downloading any of them.
    references: dict[DeclaredInput, ArtifactReference] = {}
    documents: dict[str, dict[str, Any]] = {}
    for item in pending:
        if item.upstream_task_id not in documents:
            documents[item.upstream_task_id] = fetch_upstream_task(
                db, upstream_task_id=item.upstream_task_id, tenant_id=tenant_id
            )
        references[item] = artifact_reference(
            documents[item.upstream_task_id],
            tenant_id=tenant_id,
            upstream_task_id=item.upstream_task_id,
            filename=item.filename,
        )

    # The workspace is memory-backed tmpfs, so inputs are charged against the
    # attempt's RAM. One artifact cannot exceed `max_artifact_bytes` -- the cap
    # was applied when it was uploaded -- but ten of them together can, and an
    # OOM kill several minutes into an agent run is a far worse diagnosis than a
    # refusal at setup. The same cap bounds what one attempt may move either way.
    total = sum(reference.size_bytes for reference in references.values())
    if total > max_total_bytes:
        raise InputUnavailable(
            f"the {len(references)} declared inputs total {total} bytes, over the "
            f"{max_total_bytes} byte cap for one attempt; refusing to stage them"
        )

    # Pass 4: fetch, re-checking the cap against what actually arrives.
    #
    # The check above was made against sizes the UPSTREAM reported about
    # itself. This one is made against bytes that landed, because the two can
    # differ -- a truncated upload, a hand-edited task document, a future
    # writer that rounds -- and only this one is measured on the RAM being
    # spent. A perfect type on the manifest entry would not close this; the
    # manifest is a claim, and the claim is checkable only here.
    downloaded = 0
    for item in pending:
        reference = references[item]
        destination = destinations[item]
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = store.download_file(reference.key, destination)
        except Exception as exc:
            raise InputUnavailable(
                f"could not stage {item.filename!r} from upstream task "
                f"{item.upstream_task_id} ({reference.uri}): {exc}"
            ) from exc
        downloaded += size
        if downloaded > max_total_bytes:
            # Unlinked before raising, not left for the workspace teardown: the
            # work directory is memory-backed tmpfs, so until this file is gone
            # it still holds the RAM this refusal exists to protect, and the
            # failure path above us has a checkpoint and an upload left to do.
            try:
                destination.unlink()
            except OSError:  # pragma: no cover - best effort on a doomed attempt
                pass
            raise InputUnavailable(
                f"staging {item.filename!r} from upstream task "
                f"{item.upstream_task_id} brought the declared inputs to "
                f"{downloaded} bytes actually downloaded, over the "
                f"{max_total_bytes} byte cap for one attempt; the upstream "
                f"manifests declared {total} bytes in total, so at least one of "
                "them under-reported what it had stored"
            )
        staged.append(
            StagedInput(
                upstream_task_id=item.upstream_task_id,
                filename=item.filename,
                path=destination.relative_to(work).as_posix(),
                size_bytes=size,
                uri=reference.uri,
            )
        )
        logger.info(
            "staged a declared input",
            upstream_task=item.upstream_task_id,
            filename=item.filename,
            bytes=size,
        )

    staged.sort(key=lambda item: (item.upstream_task_id, item.filename))
    return staged
