"""What a resumed live-browser attempt gets back of the parked one's browser (issue #1362).

docs/design/live-browser.md §5.5.2. The park wipes the browser (§5.5) and the
resumed attempt is a new pod, so before this module a resumed agent found only
about:blank (QA batch full-1015, S5-resume, 6/6). The worker's half:

AT PARK (`capture`), called by `HandoffPath.end` while the hold's browser is
still up and BEFORE the wipe and the checkpoint:

* the TABS: the gate's `tabs_export` (livegate/tabs.py) -- URLs in order and
  which one was in front, already filtered there and filtered again here --
  written to `$SWARM_HANDOFF/browser-tabs.json`, which travels in the
  checkpoint with the rest of `work/`. A URL is the agent's own working
  state: it navigated there, and nothing goes back to anyone but the same
  task's next attempt;
* the SITE STORAGE (cookies and the open origins' localStorage), ONLY under
  the saved-logins rules (§5.5.1): in a personal tenant, the gate's
  `session_export` with `scope: park` is stored as one Secret Manager secret,
  `swarm-tenant-u-<user>-browser-park-<task>`, which swarm-api provisions
  through the same slot route as a workflow carry (24-hour TTL, granted to
  that tenant's worker alone). NEVER INTO THE CHECKPOINT: §5.5 says cookies
  and storage are never checkpointed, and that holds. A group tenant keeps
  its tabs and no storage.

ON RESUME (`restore`), called by `HandoffPath.prepare` -- after the gate's
registration and before the runner starts -- on an attempt whose spool holds
an EARLIER attempt's `browser-tabs.json`: the park secret's cookies go to the
gate's `session_import` (refused by the gate once an agent has connected),
then `tabs_restore` reopens the tabs with each origin's localStorage written
before that page's own scripts run. The file is removed once read, so a later
retry does not reopen a park it did not make. What came back is returned for
the resume summary the agent's prompt carries (`handoff.resume_summary`).

EVERYTHING HERE FAILS OPEN. A tab list or a jar that cannot be had costs the
resumed agent its pages, never its attempt.

NO VALUE IS LOGGED. Log lines carry counts and exception types. Every carried
cookie and storage value is registered with the logger's redaction set, as
`browser_login.SessionCarrier` does.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from swarm_common.models import utcnow

from .browser_login import (
    ENVELOPE_VERSION,
    SlotRefused,
    Slots,
    Vault,
    _envelope,
    _values,
    is_personal,
    park_secret_name,
)
from .livegate.session import EXPORT_OP, IMPORT_OP, JAR_VERSION, PARK_SCOPE, valid_jar
from .livegate.tabs import EXPORT_OP as TABS_EXPORT_OP
from .livegate.tabs import RESTORE_OP as TABS_RESTORE_OP
from .livegate.tabs import clean_tabs

#: The tab list's file in the hand-off spool.
TABS_NAME = "browser-tabs.json"
TABS_VERSION = 1
#: A larger file is not read: it is not one this module wrote.
MAX_TABS_BYTES = 64 * 1024
#: The kind a park's session envelope carries, beside "carry" and "login".
PARK_KIND = "park"

GateCall = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


@dataclass
class Restored:
    """What a resumed attempt's browser got back: URLs in order, the one in front, storage or not."""

    tabs: list[str] = field(default_factory=list)
    active: str | None = None
    storage: bool = False


def _write_atomic(path: Path, document: Mapping[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def load_tabs(root: Path) -> dict[str, Any] | None:
    """The spool's tab list, shape-checked, or None. It is in the agent's reach: read as untrusted."""
    path = root / TABS_NAME
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_TABS_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("v") != TABS_VERSION or not isinstance(data.get("attempt_id"), str):
        return None
    tabs, _ = clean_tabs(data.get("tabs"))
    return {"attempt_id": data["attempt_id"], "tabs": tabs, "storage": data.get("storage") is True}


class BrowserResume:
    """One attempt's park capture and resume restore, over its gate channel."""

    def __init__(
        self,
        cfg: Any,
        *,
        logger: Any,
        call: GateCall,
        vault: Callable[[], Vault] | None = None,
        slots: Callable[[], Slots] | None = None,
        register_secret: Callable[[str], None] | None = None,
        now: Callable[[], Any] = utcnow,
    ) -> None:
        self.cfg = cfg
        self.log = logger
        self._call = call
        self._vault = vault
        self._slots = slots
        self._register = register_secret
        self._now = now

    @property
    def _secret(self) -> str | None:
        """This task's park secret, in a personal tenant; None in a group tenant."""
        tenant = str(getattr(self.cfg, "tenant_id", ""))
        if not is_personal(tenant) or self._vault is None:
            return None
        try:
            return park_secret_name(tenant, str(self.cfg.task_id))
        except ValueError:
            return None

    def _protect(self, jar: Mapping[str, Any]) -> None:
        if self._register is None:
            return
        for value in _values(jar):
            self._register(value)

    # ------------------------------------------------------------- at park

    def capture(self, root: Path) -> None:
        """Keep the tabs in the spool and, in a personal tenant, the storage in its secret. Never raises."""
        try:
            tabs, _ = clean_tabs(self._call(TABS_EXPORT_OP, {}).get("tabs"))
        except Exception as exc:
            self.log.warning("could not read the browser's tabs at park", error=type(exc).__name__)
            tabs = []
        stored = self._store_session()
        try:
            path = root / TABS_NAME
            if not tabs and not stored:
                path.unlink(missing_ok=True)
                return
            _write_atomic(path, {
                "v": TABS_VERSION,
                "attempt_id": str(self.cfg.attempt_id),
                "generation": self.cfg.generation,
                "tabs": tabs,
                "storage": stored,
            })
            self.log.info("browser kept for the resumed attempt", tabs=len(tabs), storage=stored)
        except OSError as exc:
            self.log.warning("could not keep the browser's tabs", error=type(exc).__name__)

    def _store_session(self) -> bool:
        secret = self._secret
        if secret is None:
            return False
        try:
            jar = self._call(EXPORT_OP, {"scope": PARK_SCOPE}).get("jar")
        except Exception as exc:
            self.log.warning("could not read the browser's session at park", error=type(exc).__name__)
            return False
        if jar is None or not valid_jar(jar):
            return False
        self._protect(jar)
        envelope = {
            "v": ENVELOPE_VERSION,
            "kind": PARK_KIND,
            "saved_at": self._now().isoformat(),
            "attempt_id": str(self.cfg.attempt_id),
            "jar": jar,
        }
        try:
            assert self._slots is not None and self._vault is not None
            self._slots().provision(secret)
            self._vault().write(secret, json.dumps(envelope, separators=(",", ":"), ensure_ascii=True))
        except SlotRefused as exc:
            self.log.warning("the park's browser session was not stored", cause=exc.code)
            return False
        except Exception as exc:
            self.log.warning("the park's browser session was not stored", error=type(exc).__name__)
            return False
        return True

    # ----------------------------------------------------------- on resume

    def restore(self, root: Path) -> Restored | None:
        """Reopen an earlier attempt's tabs, before the runner starts. None when there was nothing to reopen."""
        document = load_tabs(root)
        if document is None or document["attempt_id"] == str(self.cfg.attempt_id):
            return None
        (root / TABS_NAME).unlink(missing_ok=True)
        restored = Restored()
        origins: list[Any] = []
        if document["storage"]:
            jar = self._read_session()
            if jar is not None:
                origins = list(jar.get("origins") or [])
                restored.storage = self._import_cookies(jar)
        tabs = document["tabs"]
        if tabs:
            try:
                answer = self._call(TABS_RESTORE_OP, {"tabs": tabs, "origins": origins})
            except Exception as exc:
                self.log.warning("the earlier attempt's tabs were not reopened", error=type(exc).__name__)
                answer = {}
            opened, _ = clean_tabs([{"url": u} for u in answer.get("opened") or [] if isinstance(u, str)])
            restored.tabs = [tab["url"] for tab in opened]
            front = next((tab["url"] for tab in tabs if tab["active"]), None)
            restored.active = front if front in restored.tabs else None
            restored.storage = restored.storage or bool(answer.get("storage_origins"))
        if not restored.tabs and not restored.storage:
            return None
        self.log.info("the earlier attempt's browser was put back", tabs=len(restored.tabs), storage=restored.storage)
        return restored

    def _read_session(self) -> dict[str, Any] | None:
        secret = self._secret
        if secret is None:
            return None
        try:
            assert self._vault is not None
            envelope = _envelope(self._vault().read(secret))
        except Exception as exc:
            self.log.warning("the park's browser session could not be read", error=type(exc).__name__)
            return None
        if envelope is None or envelope.get("kind") != PARK_KIND:
            return None
        jar = envelope["jar"]
        self._protect(jar)
        return jar

    def _import_cookies(self, jar: Mapping[str, Any]) -> bool:
        if not jar.get("cookies"):
            return False
        # Origin storage goes with its tab (`tabs_restore`): a blank page cannot take it.
        try:
            self._call(IMPORT_OP, {"jar": {"v": JAR_VERSION, "cookies": jar["cookies"], "origins": []}})
        except Exception as exc:
            self.log.warning("the park's cookies were not put back", error=type(exc).__name__)
            return False
        return True

