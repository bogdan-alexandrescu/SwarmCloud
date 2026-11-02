"""A step's additional repositories at submission: gated, granted, pinned, sized, signed.

docs/design/multi-repository-steps.md §4.1-§4.3 and §4.7 (MULTI-REPO, #1434),
as the owner decided it on PR 1461 (2026-10-17). A caller names repositories
by `owner/repo` (`schemas.AdditionalRepositoryCreate`); this module turns each
name into the entry `Task.additional_repositories` holds (contract request 95)
or refuses the submission, before anything is stored:

  1. **Profile.** Only an agent profile whose backend is GKE Autopilot takes
     them: the worker clones them onto the pod's cache volume, which a Cloud
     Run attempt does not have (owner's Q4). A worker action, an
     `await-input` step and the indexer run no agent to read them.
  2. **Count.** At most the platform setting `max_additional_repositories`
     (default 5), read at submission from the settings document an admin
     edits at run time (`store.PLATFORM_SETTINGS_DEFAULTS`), never a
     constant: a change applies to the next submission, with no deploy.
  3. **Names.** Each entry is one repository and one directory: two entries
     naming either, or an entry naming the task's own repository, are
     refused rather than merged.
  4. **Gate.** The tenant's registration must exist, not be archived, and
     be `readable_by_tasks` -- the admin-set gate `read_private` uses
     (owner's Q1). Registered alone is not enough.
  5. **Grant and slot.** A person's entry needs their grant on THAT
     repository (`REPOSITORY_NOT_GRANTED`, one refusal naming every one, as
     `SubmissionService._resolve_forge`), and is cloned with their slot for
     its owner. A service submission, and a person while grants are not
     enforced, use `forge.service_slot`. Never a repository (`git-r-`) slot,
     which only swarm-api may read (#1225): a repository only that slot can
     read is refused `additional_repository_unreadable` (owner's Q2).
  6. **Pin.** With that same slot's token, GitHub's `repos/<o>/<r>` (its
     `size`, and its default branch when the registration has none) and
     `commits?sha=<ref>&per_page=1` (the 40-hex commit). Because swarm-api
     reads with the slot the worker will clone with, a ref that resolves
     here can be cloned there. Each (repository, ref, slot) is resolved ONCE
     per submission, so every step of one workflow reads the same commit
     even if the branch moves while it is submitted (owner's Q3).
  7. **Size.** The forge's estimates must total no more than the platform
     setting `additional_repositories_budget_gib` (default 6). An estimate:
     the worker measures the checkouts and enforces the same setting.

The token lives in a local for the two reads and is in no document, no
response, no log line and no exception message.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

from swarm_common.models import (
    ADDITIONAL_FORGE_CREDENTIAL,
    ADDITIONAL_REPOSITORY_NAME,
    Task,
    Tenant,
    check_additional_repositories,
)
from swarm_common.profiles import RUNNER_PROFILES, Backend, RunnerProfile, resolve_backend

from .errors import ValidationFailed
from .forge import ForgeSlot, NoForgeCredential
from .forgewrite import ForgeWriteForbidden, ForgeWriteNotFound, ForgeWriteUnauthorized
from .repositories import (
    Repositories,
    check_branch,
    parse_repository,
    readable_by_tasks_of,
    repo_id_for,
    repository_url,
)
from .validation import (
    AdditionalRepositoriesDisabled,
    AdditionalRepositoriesTooLarge,
    AdditionalRepositoriesUnsupportedProfile,
    AdditionalRepositoryDuplicate,
    AdditionalRepositoryUnreadable,
    IssueRef,
    RepositoryNotReadableByTasks,
    RepositoryNotRegistered,
    TooManyAdditionalRepositories,
    check_repository_ref,
    github_repository,
    repository_not_granted,
)

log = logging.getLogger(__name__)

#: The platform settings this module reads at every submission that names
#: additional repositories (`Store.get_platform_settings`, which answers each
#: at its default when unset or malformed). The worker reads the budget from
#: the same key (`agent_worker.extrarepos.BUDGET_SETTING`).
MAX_COUNT_SETTING = "max_additional_repositories"
BUDGET_SETTING = "additional_repositories_budget_gib"

#: `swarm_api.repoindex.INDEXER_PROFILE`, restated: `repoindex` imports the
#: submission service, which imports this module. A test holds the two equal.
INDEXER_PROFILE = "indexer"

_KIB_PER_GIB = 1024 * 1024

#: The one sentence for "this slot cannot read it" and "this ref names no
#: commit" (design §4.1): which one it was is not the caller's to learn from
#: the answer, as `read_private`'s `repo_not_allowlisted`.
UNREADABLE = ("{repository} cannot be read at {ref!r} with the GitHub credential its "
              "clone would use (yours for a person's step, the tenant's for a service's; "
              "never the repository's own token), or that ref names no commit")


@dataclass(frozen=True)
class Limits:
    """The two platform settings, as read for one submission."""

    max_count: int
    budget_gib: int

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> "Limits":
        return cls(max_count=int(settings[MAX_COUNT_SETTING]),
                   budget_gib=int(settings[BUDGET_SETTING]))


def runs_an_agent(profile: RunnerProfile) -> bool:
    """Whether a step of this profile runs an agent, so a workflow's list is
    its default: a worker action (merge, post-verdict) and an `await-input`
    step read no repository, and inherit none."""
    return profile.worker_action is None and not profile.control_plane


def takes_additional_repositories(profile: RunnerProfile) -> bool:
    """Design §4.3, owner's Q4: an agent profile on GKE Autopilot, not the indexer.

    Read from the catalogue (`resolve_backend`), never from the caller. On
    2026-10-18 that admits generic, claude-code, codex, browser and
    claude-code-browser, and refuses mock and claude-code-review (Cloud Run),
    merge and post-verdict (worker actions), await-input and indexer.
    """
    return (runs_an_agent(profile) and profile.name != INDEXER_PROFILE
            and resolve_backend(profile) is Backend.GKE_AUTOPILOT)


def requested_any(lists: Sequence[Sequence[Any] | None]) -> bool:
    return any(bool(items) for items in lists)


def refuse_unless_enabled(enabled: bool, lists: Sequence[Sequence[Any] | None]) -> None:
    """The feature flag: off, a submission naming any entry is refused. An
    empty list asks for nothing and is accepted either way."""
    if not enabled and requested_any(lists):
        raise AdditionalRepositoriesDisabled(
            "additional_repositories is not enabled on this deployment yet; submit "
            "without it (ADDITIONAL_REPOSITORIES_ENABLED is off)",
            detail={"field": "additional_repositories"},
        )


@dataclass(frozen=True)
class _Asked:
    """One entry as asked, checked for shape: the caller's words only."""

    owner: str
    repo: str
    repo_id: str
    ref: str | None
    name: str

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class Resolver:
    """One submission's resolution. Built per submission, so its caches --
    registrations, the service slot, and every pin -- answer every step of a
    workflow alike and outlive nothing."""

    db: Any
    now: Callable[[], Any]
    tenant: Tenant
    submitter: str
    service: bool
    grants_enforced: bool
    limits: Limits
    tokens: Any
    writer: Any
    #: `SubmissionService._grant_mode` and `_person_slot`, and the service
    #: slot's rule: the same functions the primary repository's slot uses.
    grant_mode: Callable[[str, str, str], str | None]
    person_slot: Callable[[str, str, str], str]
    service_slot: Callable[[], ForgeSlot]
    _registrations: dict[str, dict[str, Any] | None] = field(default_factory=dict)
    _service: ForgeSlot | None = None
    _pins: dict[tuple[str, str, str], tuple[str, str, int]] = field(default_factory=dict)

    # -- the whole list --------------------------------------------------------

    def resolve(self, task: Task, requested: Sequence[Any] | None, *,
                step_id: str | None = None) -> list[dict[str, Any]] | None:
        """`requested` as `Task.additional_repositories`, or None for none.

        Raises the refusals in `validation`; nothing is read from GitHub
        before every check that needs no read has passed.
        """
        if not requested:
            return None
        where = _detail(step_id)
        profile = RUNNER_PROFILES[task.runner_profile]
        if not takes_additional_repositories(profile):
            raise AdditionalRepositoriesUnsupportedProfile(
                f"{_where(step_id)}runner profile {profile.name!r} does not take additional "
                "repositories: only an agent profile on GKE does (generic, claude-code, claude-code-large, "
                "claude-code-lightpanda, codex, browser, claude-code-browser). On a workflow, give this step "
                "`additional_repositories: []`",
                detail={**where, "runner_profile": profile.name},
            )
        if len(requested) > self.limits.max_count:
            raise TooManyAdditionalRepositories(
                f"{_where(step_id)}a step may read at most {self.limits.max_count} additional "
                f"repositories (the platform setting {MAX_COUNT_SETTING}); this one names "
                f"{len(requested)}",
                detail={**where, "limit": self.limits.max_count, "count": len(requested),
                        "setting": MAX_COUNT_SETTING},
            )
        asked = self._asked(task, requested, step_id)
        slots = self._slots(asked, step_id)
        entries: list[dict[str, Any]] = []
        for item, (provider, source) in zip(asked, slots):
            record = self._registrations[item.repo_id] or {}
            # GitHub's spelling, as registered: read and stored one way
            # whatever case the caller typed.
            item = replace(item, **dict(zip(("owner", "repo"), _registered_name(record, item))))
            ref, commit, size_kib = self._pin(item, record, provider, step_id)
            entries.append({
                "name": item.name,
                "repo_id": item.repo_id,
                "repository": item.repository,
                "repository_url": repository_url(item.owner, item.repo),
                "ref": ref,
                "commit": commit,
                "forge_credential": provider,
                "forge_credential_source": source,
                "size_kib": size_kib,
            })
        total = sum(entry["size_kib"] for entry in entries)
        if total > self.limits.budget_gib * _KIB_PER_GIB:
            raise AdditionalRepositoriesTooLarge(
                f"{_where(step_id)}the additional repositories total about "
                f"{_gib(total)} GiB by GitHub's estimate, over the {self.limits.budget_gib} GiB "
                f"a step may read (the platform setting {BUDGET_SETTING})",
                detail={**where, "budget_gib": self.limits.budget_gib, "size_kib": total,
                        "setting": BUDGET_SETTING},
            )
        try:
            check_additional_repositories(entries)
        except ValueError as exc:
            # Every value above was checked or built here; this is the
            # contract's own statement of the shape, run on what is stored.
            raise ValidationFailed(f"{_where(step_id)}{exc}", detail=where) from None
        return entries

    # -- shape, names, duplicates ---------------------------------------------

    def _asked(self, task: Task, requested: Sequence[Any],
               step_id: str | None) -> list[_Asked]:
        tenant_id = self.tenant.tenant_id
        primary = github_repository(task.repository_url)
        primary_id = repo_id_for(tenant_id, *primary) if primary else None
        out: list[_Asked] = []
        names: set[str] = set()
        repo_ids: set[str] = set()
        for item in requested:
            owner, repo = item.repository.split("/", 1)
            name = item.name or repo.lower()
            if not ADDITIONAL_REPOSITORY_NAME.fullmatch(name):
                raise ValidationFailed(
                    f"{_where(step_id)}{owner}/{repo} has no usable directory name of its "
                    "own; give the entry a `name` of lower-case letters, digits, '.', '_' "
                    "and '-', starting with a letter or digit",
                    detail={**_detail(step_id), "field": "additional_repositories.name",
                            "repository": f"{owner}/{repo}"},
                )
            ref = item.ref
            if ref is not None:
                check_repository_ref(ref)
                try:
                    check_branch(ref)
                except ValueError:
                    raise ValidationFailed(
                        f"{_where(step_id)}the ref for {owner}/{repo} must be a branch, a tag "
                        "or a full 40-character commit sha",
                        detail={**_detail(step_id), "field": "additional_repositories.ref",
                                "repository": f"{owner}/{repo}"},
                    ) from None
            repo_id = repo_id_for(tenant_id, owner, repo)
            why = ("is the task's own repository" if repo_id == primary_id
                   else "is named twice" if repo_id in repo_ids
                   else f"would share the directory {name!r}" if name in names
                   else None)
            if why is not None:
                raise AdditionalRepositoryDuplicate(
                    f"{_where(step_id)}{owner}/{repo} {why}: each additional repository is "
                    "a different repository, in a directory of its own",
                    detail={**_detail(step_id), "repository": f"{owner}/{repo}", "name": name},
                )
            names.add(name)
            repo_ids.add(repo_id)
            out.append(_Asked(owner=owner, repo=repo, repo_id=repo_id, ref=ref, name=name))
        return out

    # -- gate, grant, slot ----------------------------------------------------

    def _registration(self, repo_id: str) -> dict[str, Any] | None:
        if repo_id not in self._registrations:
            self._registrations[repo_id] = Repositories(self.db, now=self.now).find(
                self.tenant.tenant_id, repo_id)
        return self._registrations[repo_id]

    def _slots(self, asked: Sequence[_Asked],
               step_id: str | None) -> list[tuple[str, str]]:
        """(slot, source) per entry, after every entry's gate; every ungranted
        entry named in one REPOSITORY_NOT_GRANTED."""
        for item in asked:
            record = self._registration(item.repo_id)
            if record is None:
                raise RepositoryNotRegistered(
                    f"{_where(step_id)}{item.repository} is not registered in this "
                    "workspace; register it under Repositories before a step can read it",
                    detail={**_detail(step_id), "repository": item.repository},
                )
            if record.get("archived") is True or not readable_by_tasks_of(record):
                raise RepositoryNotReadableByTasks(
                    f"{_where(step_id)}{item.repository} is registered, but tasks may not "
                    "read it: an admin turns on `readable_by_tasks` for it under "
                    "Repositories (an archived repository is never read)",
                    detail={**_detail(step_id), "repository": item.repository},
                )
        slots: list[tuple[str, str]] = []
        refused: list[tuple[str | None, str]] = []
        for item in asked:
            if not self.service:
                mode = self.grant_mode(self.tenant.tenant_id, self.submitter, item.repo_id)
                if mode is not None:
                    # Either mode: the clone only reads.
                    slots.append((self.person_slot(self.tenant.tenant_id, self.submitter,
                                                   item.owner), "person"))
                    continue
                if self.grants_enforced:
                    refused.append((step_id, item.repository))
                    continue
            slot = self._service_slot()
            if not slot.known:
                raise AdditionalRepositoryUnreadable(
                    _where(step_id) + UNREADABLE.format(repository=item.repository,
                                                        ref=item.ref or "its default branch"),
                    detail={**_detail(step_id), "repository": item.repository},
                )
            slots.append((slot.provider, slot.source))
        if refused:
            raise repository_not_granted(refused)
        return slots

    def _service_slot(self) -> ForgeSlot:
        if self._service is None:
            self._service = self.service_slot()
        return self._service

    # -- pin and size --------------------------------------------------------

    def _pin(self, item: _Asked, record: Mapping[str, Any], provider: str,
             step_id: str | None) -> tuple[str, str, int]:
        """(ref, commit, size_kib), read once per (repository, ref, slot)."""
        key = (item.repo_id, item.ref or "", provider)
        if key in self._pins:
            return self._pins[key]
        asked_ref = item.ref or "its default branch"
        unreadable = AdditionalRepositoryUnreadable(
            _where(step_id) + UNREADABLE.format(repository=item.repository, ref=asked_ref),
            detail={**_detail(step_id), "repository": item.repository},
        )
        if not ADDITIONAL_FORGE_CREDENTIAL.fullmatch(provider):
            # A repository slot: only swarm-api reads those (#1225, Q2).
            raise unreadable
        try:
            token = self.tokens.read_slot(self.tenant, provider).value
        except NoForgeCredential:
            raise unreadable from None
        if not token:
            raise unreadable
        target = IssueRef(owner=item.owner, repo=item.repo, number=0)
        try:
            about = self._get(target, "", token)
            ref = item.ref or _branch(record.get("default_branch")) or _branch(
                (about or {}).get("default_branch") if isinstance(about, dict) else None)
            if ref is None:
                raise unreadable
            listed = self._get(target, f"commits?sha={quote(ref, safe='')}&per_page=1", token)
        except (ForgeWriteNotFound, ForgeWriteForbidden, ForgeWriteUnauthorized):
            raise unreadable from None
        finally:
            del token
        first = listed[0] if isinstance(listed, list) and listed else None
        commit = first.get("sha") if isinstance(first, dict) else None
        if not isinstance(commit, str) or not _is_sha(commit):
            raise unreadable
        size = about.get("size") if isinstance(about, dict) else None
        size_kib = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
        pinned = (ref, commit, size_kib)
        self._pins[key] = pinned
        log.info("additional repository pinned tenant=%s repository=%s ref=%s commit=%s "
                 "size_kib=%d slot=%s", self.tenant.tenant_id, item.repository, ref,
                 commit[:12], size_kib, provider)
        return pinned

    def _get(self, target: IssueRef, path: str, token: str) -> Any:
        """One GET on api.github.com through `GitHubWriter`'s wire, as
        `GitTools._get`: the pinned host, no redirect, the token in the
        header only, constant errors."""
        url = self.writer._url(target, path).rstrip("/")
        return self.writer._call("GET", url, token,
                                 f"{target.repository} {path.split('?', 1)[0][:80] or 'repo'}",
                                 needs="contents: read")


def _registered_name(record: Mapping[str, Any], item: _Asked) -> tuple[str, str]:
    """GitHub's spelling of the name, as the registration stored it, so one
    repository is stored one way whatever case a caller typed; the caller's
    when the registration's is missing or names another repository."""
    owner, repo = record.get("owner"), record.get("repo")
    if isinstance(owner, str) and isinstance(repo, str):
        try:
            owner, repo = parse_repository(f"{owner}/{repo}")
        except ValueError:
            return item.owner, item.repo
        if f"{owner}/{repo}".lower() == item.repository.lower():
            return owner, repo
    return item.owner, item.repo


def _branch(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return check_branch(value)
    except ValueError:
        return None


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value)


def _gib(kib: int) -> str:
    return f"{kib / _KIB_PER_GIB:.1f}"


def _where(step_id: str | None) -> str:
    return f"step {step_id!r}: " if step_id is not None else ""


def _detail(step_id: str | None) -> dict[str, Any]:
    return {"field": "additional_repositories",
            **({"step_id": step_id} if step_id is not None else {})}

