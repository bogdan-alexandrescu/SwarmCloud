"""The dependency manifest: its typed model and loader (design §3.1, §3.3).

`install/manifest.yaml` lists every dependency a from-scratch install needs. It
is the base: each Day One lane that adds, changes or removes a collectable item
writes its own fragment, `install/manifest.d/<lane>.yaml`, and this loader
merges them (docs/design/day-one-plan.md §1 conventions):

* a fragment's entry with a new id is added;
* a fragment may restate a base entry by id only with `replace: true`;
* two fragments naming one id (added or replaced), an entry id the base already
  holds without `replace: true`, a `replace: true` with nothing to replace, or
  one `env:` name set twice, is a `ManifestError`.

The file's `counts:` header describes the base file alone; fragments carry no
counts, so a lane's fragment never edits the base.

`refs:` are the machine-matchable references `tests/unit/install/
test_manifest_complete.py` collects from the tree. Each list holds exact keys or
`fnmatch` patterns (`*` matches any run of characters):

* `terraform`: `<root>/<type>.<name>`, root `bootstrap`, `infra` or
  `modules/<module>`, for every `resource` and `data` block;
* `api`: `<name>.googleapis.com`;
* `secret`: a Secret Manager id; an interpolated id is collected with each
  `${...}` as `*` (`swarm-tenant-*-qa`), so a pattern entry matches it;
* `secret_flag`: a `scripts/create-secrets.sh` option, `--live-key`;
* `scheduler_job`: a `google_cloud_scheduler_job` resource name;
* `image`: an image name (images.json, `scripts/build-images.sh`'s targets) or
  `dockerfile:<path>` for a tracked Dockerfile;
* `kubernetes`: a path under `kubernetes/`;
* `variable`: `<root>/<name>` for a `bool` (or `enable_*`) Terraform variable.

Nothing here touches the network or builds a client.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: The repository root, three levels up from this file.
REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST_PATH = REPO_ROOT / "install" / "manifest.yaml"
FRAGMENTS_DIR = REPO_ROOT / "install" / "manifest.d"

#: The collectors' names, in design §3.3's order; `refs:` and `ignored:` key on
#: these (`env` has its own table).
REF_KINDS: tuple[str, ...] = (
    "terraform",
    "api",
    "env",
    "secret",
    "secret_flag",
    "scheduler_job",
    "image",
    "kubernetes",
    "variable",
)

PROVIDERS = (
    "engine",
    "human-in-console",
    "human-in-terminal",
    "org-admin",
    "company-external",
)


class ManifestError(ValueError):
    """The manifest or a fragment is malformed, or the fragments conflict."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Probe(BaseModel):
    """A typed read-only check (design §2.2), e.g. `{type: service_enabled,
    name: run.googleapis.com}`. Its other keys are the probe's arguments."""

    model_config = ConfigDict(extra="allow")
    type: str = Field(min_length=1)


class Refs(_Strict):
    terraform: list[str] = []
    api: list[str] = []
    secret: list[str] = []
    secret_flag: list[str] = []
    scheduler_job: list[str] = []
    image: list[str] = []
    kubernetes: list[str] = []
    variable: list[str] = []

    def items(self) -> Iterable[tuple[str, str]]:
        """Every (collector, pattern) pair this entry names."""
        for kind in REF_KINDS:
            for pattern in getattr(self, kind, ()) or ():
                yield kind, pattern


class Entry(_Strict):
    id: str = Field(min_length=3)
    kind: str
    scope: str
    stage: int = Field(ge=0, le=15)
    provider: Literal[
        "engine", "human-in-console", "human-in-terminal", "org-admin", "company-external"
    ]
    check: str | None = None
    apply: str | None = None
    verify: str | None = None
    explain: str | None = None
    guards: str | None = None
    secret_entry: str | None = None
    depends_on: list[str] = []
    evidence: list[str] = []
    notes: str | None = None
    # P3's four fields (design §3.1).
    probe: Probe | None = None
    step: str | None = None
    refs: Refs = Refs()
    expand: Literal["per_tenant", "per_profile", "per_account"] | None = None
    # An `api.*` entry the install enables that no list holds yet (design §3.3
    # collector 2).
    new: bool = False
    # A fragment's restatement of a base entry (plan §1 conventions).
    replace: bool = False

    @model_validator(mode="after")
    def _id_has_kind_prefix(self) -> Entry:
        if "." not in self.id:
            raise ValueError(f"entry id {self.id!r} has no '<kind prefix>.' part")
        if not (self.scope == "core" or self.scope.startswith("addon:")):
            raise ValueError(f"{self.id}: scope {self.scope!r} is neither core nor addon:<name>")
        return self


class Ignored(_Strict):
    """A collected reference no entry lists, and why that is right."""

    match: str = Field(min_length=1)
    reason: str = Field(min_length=10)


class EnvVar(_Strict):
    """Where an environment variable a service reads gets its value: a
    `path:line` setter, or `internal: <reason>`."""

    setter: str = Field(min_length=3)


class ManifestFile(_Strict):
    """One file: the base (`install/manifest.yaml`) or a fragment."""

    version: int = 1
    generated_from: str | None = None
    counts: dict[str, dict[str, int]] | None = None
    entries: list[Entry] = []
    ignored: dict[str, list[Ignored]] = {}
    env: dict[str, EnvVar] = {}

    @model_validator(mode="after")
    def _known_ignored_kinds(self) -> ManifestFile:
        unknown = sorted(set(self.ignored) - set(REF_KINDS))
        if unknown:
            raise ValueError(f"ignored: has unknown collectors {unknown}; known: {REF_KINDS}")
        return self


class Manifest(BaseModel):
    """The merged manifest: the base plus every fragment."""

    entries: list[Entry]
    ignored: dict[str, list[Ignored]]
    env: dict[str, EnvVar]
    #: Which file each entry id came from (the base or a fragment's name).
    sources: dict[str, str]

    def by_id(self) -> dict[str, Entry]:
        return {e.id: e for e in self.entries}

    def matches(self, kind: str, reference: str) -> list[str]:
        """The ids of the entries whose `refs.<kind>` match `reference`."""
        return [
            e.id
            for e in self.entries
            for k, pattern in e.refs.items()
            if k == kind and fnmatch.fnmatchcase(reference, pattern)
        ]

    def ignored_reason(self, kind: str, reference: str) -> str | None:
        for item in self.ignored.get(kind, ()):
            if fnmatch.fnmatchcase(reference, item.match):
                return item.reason
        return None


def parse_file(path: Path) -> ManifestFile:
    """Read and validate one manifest file. An empty file is an empty manifest."""
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path}: not YAML: {exc}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ManifestError(f"{path}: the top level must be a mapping")
    try:
        return ManifestFile.model_validate(raw)
    except ValidationError as exc:
        raise ManifestError(f"{path}: {exc}") from None


def merge(base: ManifestFile, fragments: dict[str, ManifestFile], base_name: str) -> Manifest:
    """Merge fragments (name -> file) into the base, refusing every conflict."""
    entries: dict[str, Entry] = {}
    sources: dict[str, str] = {}
    for entry in base.entries:
        if entry.id in entries:
            raise ManifestError(f"{base_name}: entry id {entry.id!r} is listed twice")
        if entry.replace:
            raise ManifestError(f"{base_name}: {entry.id}: replace: true is for fragments only")
        entries[entry.id] = entry
        sources[entry.id] = base_name
    ignored = {k: list(v) for k, v in base.ignored.items()}
    env = dict(base.env)
    env_sources = {name: base_name for name in env}
    touched: dict[str, str] = {}
    for name in sorted(fragments):
        fragment = fragments[name]
        if fragment.counts is not None:
            raise ManifestError(f"{name}: a fragment carries no counts: header")
        for entry in fragment.entries:
            if entry.id in touched:
                raise ManifestError(
                    f"{name}: entry id {entry.id!r} is also restated by {touched[entry.id]}; "
                    "one id belongs to one fragment"
                )
            if entry.replace and entry.id not in entries:
                raise ManifestError(f"{name}: {entry.id}: replace: true but no such entry")
            if not entry.replace and entry.id in entries:
                raise ManifestError(
                    f"{name}: entry id {entry.id!r} is already in {sources[entry.id]}; "
                    "restate it with replace: true"
                )
            touched[entry.id] = name
            entries[entry.id] = entry
            sources[entry.id] = name
        for kind, items in fragment.ignored.items():
            ignored.setdefault(kind, []).extend(items)
        for var, setter in fragment.env.items():
            if var in env:
                raise ManifestError(f"{name}: env {var} is already set by {env_sources[var]}")
            env[var] = setter
            env_sources[var] = name
    _check_depends_on(entries)
    return Manifest(entries=list(entries.values()), ignored=ignored, env=env, sources=sources)


def _check_depends_on(entries: dict[str, Entry]) -> None:
    missing = sorted(
        f"{e.id} -> {dep}" for e in entries.values() for dep in e.depends_on if dep not in entries
    )
    if missing:
        raise ManifestError(f"depends_on names ids no entry has: {missing}")


def load(path: Path = MANIFEST_PATH, fragments_dir: Path = FRAGMENTS_DIR) -> Manifest:
    """The base manifest merged with every `*.yaml` fragment in `fragments_dir`."""
    base = parse_file(path)
    fragments = (
        {p.name: parse_file(p) for p in sorted(fragments_dir.glob("*.yaml"))}
        if fragments_dir.is_dir()
        else {}
    )
    return merge(base, fragments, path.name)


def count(entries: Iterable[Entry]) -> dict[str, dict[str, int]]:
    """The header's counts for these entries: by kind, scope, provider, stage."""
    table: dict[str, dict[str, int]] = {"kind": {}, "scope": {}, "provider": {}, "stage": {}}
    total = 0
    for e in entries:
        total += 1
        for axis, value in (
            ("kind", e.kind),
            ("scope", e.scope),
            ("provider", e.provider),
            ("stage", str(e.stage)),
        ):
            table[axis][value] = table[axis].get(value, 0) + 1
    table["total"] = {"entries": total}
    return table
