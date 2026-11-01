"""The claude-code lanes' quality gates: jev-belay and jev-enforce, keyed with the workspace's TypeSafe key (JEV-LANES).

BGD decided 2026-10-31: 25% of lane PRs were red on their first CI run
(#642), and a review's NOT_YET round costs a fix step and a re-review. So a
claude-code lane in a workspace that turned `quality_gates.jev` on runs two
Claude Code hooks, both checked by TypeSafe's Jev:

* **jev-belay** (v0.2.0), a Stop hook: when files changed and no test, build
  or lint run passed since, one Jev call on whether the closing message is
  an unverified "done"; if so the stop is blocked with the reason.
* **jev-enforce** (v1.1.1) with `JEV_ENFORCE_MODE=end`: every Edit/Write is
  recorded while the agent works, and the Stop hook checks the final reply
  and the edits against the repository's CLAUDE.md rule files in one request.

Both are inert without TYPESAFE_API_KEY and let the turn end on every error.
This module is the worker's half: whether, which key, and the counts after.

WHETHER. `workspace_settings/{tenant}.quality_gates.jev is True`
(`swarm_api.qualitygates`, owner/admin-only and audited, default off), AND
the scheduler vouched at dispatch that swarm-api signed that block (contract
request 128). Off, or anything but `True`: nothing more is read and the
runner gets nothing, so no hook is registered and nothing leaves the pod.

WHY THE SCHEDULER VOUCHES. Any tenant's worker can write any
`workspace_settings` document, so a stored "on" proves nothing about who
chose it. swarm-api stores `quality_gates.mac`, an HMAC under the one
`swarm-settings-key`, with every write; a worker cannot check it, because a
worker that held that key could sign any tenant's block. The scheduler holds
it: at dispatch it verifies this attempt's tenant's block and writes the
verdict into the job's environment (`scheduler.judgment.quality_gates_env`;
the owner's pick on PR 2955), `VERDICT_ENV` "true" only for a block that
says on and verifies, with `REFUSED_ENV` saying why not. The worker acts on
"true" alone, and only while the stored block still says on, so turning the
switch off takes effect at once. Unsigned, a MAC that does not verify, no
settings key, SETTINGS_MAC_REQUIRED off or no verdict at all (a scheduler
older than this): off, and `refused` says which, for the attempt's summary.

WHICH KEY. The WORKSPACE's TypeSafe key, `swarm-tenant-<tenant>-typesafe`,
the secret the jev browser tools read (`jevkey.resolve`), resolved with no
submitter so a person's own key is never spent on a workspace's switch.
Read as this worker's own service account; no route serves it.

HANDED OVER HOW. Exactly as `jevkey` hands the browser tools theirs: the key
goes into ONE 0600 file in `private/` (never checkpointed, never uploaded),
registered with the logger for redaction the moment it is read. The runner
gets `CONFIG_ENV`, the PATH of a 0600 config naming that file, the counts
file, the hooks' data directory and the profile's registered secret NAMES --
never a value. The image's launcher (`/opt/swarm/jev-gates/bin/
swarm-jev-gate.mjs`), which each hook command names, reads the key into its
own process alone and scrubs every registered secret's value from the
request before it is sent (docs/jev-data-flow.md, "The lane hooks"). With
the TypeSafe forwarder on (`typesafe_shim.switched_on`) no key is read: the
config names the worker's shim file instead, and the launcher sends to the
loopback shim with its local token.

THE COUNTS. Each hook run appends one line of counts -- which hook, how many
Jev calls, whether it blocked, how many rules it flagged; never text -- to
the counts file. After the agent, `finish` sums them for the attempt's
summary, removes the key file, and the lifecycle records one RUNNING mark,
cause `quality_gate`, when belay blocked a stop or enforce flagged a rule.

THE WORKFLOW SWITCH (the owner's experiment, 2026-11-01). A spec's signed
dispatch block may carry `quality_gates: on|off` (`SWITCH`), which every
step of the workflow obeys, and the stored `jev` gains a third value for it,
`"on_request"` (`ON_REQUEST`): only a step whose signed switch says `on`
runs the gates. The switch only narrows: `off` runs none, whatever is
stored; `on` runs them only where the stored, signed block says `true` or
`"on_request"`, because the gates send code to TypeSafe and only a workspace
admin decides that. The scheduler vouches for `"on_request"` as it does for
`true` (its verdict is then the word `on_request`), and the worker acts on a
verdict only when it names the value stored now. `decide` says what ran and
why -- the switch, its value and the deciding source (`workflow switch`,
`workspace` or `default`) -- and the attempt's summary records it.

NEVER FATAL. A switch that cannot be read is off; a key that cannot be read
costs the attempt its gates, not the attempt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import indexrun
from . import jevkey

SETTINGS_COLLECTION = "workspace_settings"
FIELD = "quality_gates"
JEV = "jev"
#: The stored value that runs the gates only under a step's switch `on`
#: (`swarm_api.qualitygates.ON_REQUEST`, 2026-11-01).
ON_REQUEST = "on_request"
#: The workflow switch in the signed dispatch block: `on` | `off`.
SWITCH = "quality_gates"
ON, OFF = "on", "off"
SOURCE_SWITCH = "workflow switch"

#: The runner's variable: the PATH of the gates' config, never the key.
CONFIG_ENV = "SWARM_JEV_GATES_CONFIG"
CONFIG_FILE_NAME = "jev-gates.json"
KEY_FILE_NAME = "jev-gates.key"
COUNTS_FILE_NAME = "jev-gates.counts"
#: jev-enforce's CLAUDE_PLUGIN_DATA: its rule cache and, in `end` mode, the
#: edits it holds until the Stop hook. In `private/`, so never checkpointed.
DATA_DIR_NAME = "jev-gates-data"

#: The config's fields (the launcher reads exactly these).
KEY_FILE_FIELD = "key_file"
COUNTS_FILE_FIELD = "counts_file"
DATA_DIR_FIELD = "data_dir"
SECRET_NAMES_FIELD = "secret_env_names"

#: The hooks the counts name (the launcher's modes).
BELAY = "belay"
ENFORCE = "enforce"
#: The summary's key, and the RUNNING mark's `cause`.
SUMMARY_KEY = "quality_gates"
EVENT_CAUSE = "quality_gate"
#: A counts file larger than this is not the launcher's: read as nothing.
MAX_COUNTS_BYTES = 256 * 1024

#: The scheduler's verdict on this attempt's stored block, in the job's
#: environment (restated from `scheduler.judgment`, which the worker does not
#: import; tests/unit/worker/test_quality_gates.py holds them equal).
VERDICT_ENV = "SWARM_QUALITY_GATES_JEV"
REFUSED_ENV = "SWARM_QUALITY_GATES_REFUSED"
#: Why a stored "on" is off when the scheduler gave no "true". The
#: scheduler's own reasons (`unsigned`, `mac_mismatch`, `no_settings_key`,
#: `settings_mac_off`, `settings_unreadable`) are passed on as it gives them.
REFUSED_NO_VERDICT = "no_verdict"
REFUSED_OFF_AT_DISPATCH = "off_at_dispatch"
#: A reason longer than this, or not a word, is not the scheduler's.
_MAX_REASON = 64


def switched_on(workspace: Mapping[str, Any] | None) -> bool:
    """`quality_gates.jev is True`; anything else, malformed or absent, is off."""
    return stored_value(workspace) is True


def stored_value(workspace: Mapping[str, Any] | None) -> bool | str | None:
    """The stored `quality_gates.jev`: True, False, `ON_REQUEST`, or None when
    absent; anything malformed is False."""
    block = workspace.get(FIELD) if isinstance(workspace, Mapping) else None
    if not isinstance(block, Mapping) or JEV not in block:
        return None
    value = block.get(JEV)
    return value if value is True or value == ON_REQUEST else False


def decide(workspace: Mapping[str, Any] | None, switch: str | None
           ) -> tuple[bool | str, str, str]:
    """`(wanted, source, reason)` for this step: `wanted` is the stored value
    the gates would run under (True or `ON_REQUEST`), or False. The
    scheduler's verdict still has to vouch for it (`prepare`)."""
    switch = switch if switch in (ON, OFF) else None
    stored = stored_value(workspace)
    if switch == OFF:
        return False, SOURCE_SWITCH, "quality gates are off (workflow switch)"
    if stored is None:
        return False, "default", "quality gates are off (default)"
    if stored is False:
        return False, "workspace", "quality gates are off (workspace setting)"
    if stored == ON_REQUEST and switch != ON:
        return False, "workspace", "the workspace runs quality gates only on request"
    if switch == ON:
        return stored, SOURCE_SWITCH, "quality gates are on (workflow switch; workspace allows it)"
    return stored, "workspace", "quality gates are on (workspace setting)"


class GatesVerdict:
    """The scheduler's verdict on this attempt's `quality_gates` block, read
    from the job's environment. `refused` is why it is not "true", after
    `verdict`."""

    def __init__(self, environ: Mapping[str, str]) -> None:
        self._value = environ.get(VERDICT_ENV)
        self._reason = environ.get(REFUSED_ENV)
        self.refused: str | None = None
        #: What `prepare` decided for a step whose signed block set the switch,
        #: or whose workspace stored `ON_REQUEST`: `{switch, value, source,
        #: on, reason}`, for the attempt's summary. Empty otherwise.
        self.record: dict[str, Any] = {}

    def verdict(self, stored: bool | str = True) -> bool:
        """True only when the scheduler wrote exactly the word for `stored`:
        "true" for True, "on_request" for `ON_REQUEST`."""
        if self._value == ("true" if stored is True else ON_REQUEST):
            self.refused = None
            return True
        if self._value is None:
            self.refused = REFUSED_NO_VERDICT
            return False
        reason = self._reason
        if isinstance(reason, str) and reason and len(reason) <= _MAX_REASON \
                and reason.replace("_", "").isalnum():
            self.refused = reason
        else:
            # The block said off when the scheduler read it and on now.
            self.refused = REFUSED_OFF_AT_DISPATCH
        return False


def read_settings(db: Any, tenant_id: str, *,
                  call_options: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """`workspace_settings/{tenant}`: from `state/context` for a tenant on the
    state routes, else its Firestore document; {} when there is none."""
    state = indexrun.state_routes(db)
    if state is not None:
        served = indexrun.route_context(state).get("workspace_settings")
        return served if isinstance(served, Mapping) else {}
    snap = db.collection(SETTINGS_COLLECTION).document(tenant_id).get(**dict(call_options or {}))
    return (snap.to_dict() or {}) if snap.exists else {}


def _write_private(path: Path, text: str) -> None:
    if path.is_symlink() or path.exists():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def prepare(*, db: Any, client: Any, tenant_id: str, private_dir: Path, logger: Any,
            call_options: Mapping[str, Any] | None = None,
            secret_env_names: Iterable[str] = (), inject: bool = False,
            shim_file: Path | None = None,
            verdict: GatesVerdict | None = None,
            switch: str | None = None) -> dict[str, str]:
    """`{CONFIG_ENV: path}` when this workspace turned the gates on, the
    scheduler vouched at dispatch that swarm-api signed that "on" (`verdict`),
    and a key (or, with the forwarder, the shim) is available; else {}.
    Without a `verdict` a stored "on" is off: nothing else checked its MAC.

    `switch` is the signed dispatch block's `quality_gates` (`SWITCH`), None
    when unset (`decide`). A stored `ON_REQUEST` runs the gates only under
    `on`; `off` runs none. What was decided is left in `verdict.record` when
    the switch is set or the workspace stored `ON_REQUEST`. Never raises."""
    if db is None:
        return {}
    switch = switch if switch in (ON, OFF) else None
    note: dict[str, Any] = {}
    try:
        workspace = read_settings(db, tenant_id, call_options=call_options)
        wanted, source, reason = decide(workspace, switch)
        if switch is not None or stored_value(workspace) == ON_REQUEST:
            note = {"switch": SWITCH, "value": switch, "source": source, "on": False,
                    "reason": reason}
            if verdict is not None:
                verdict.record = note
        if not wanted:
            return {}
        if verdict is None:
            logger.warning("jev_gates_refused", reason=REFUSED_NO_VERDICT)
            return {}
        if not verdict.verdict(wanted):
            logger.warning("jev_gates_refused", reason=verdict.refused)
            note["reason"] = f"refused ({verdict.refused})"
            return {}
        private = Path(private_dir)
        if inject:
            if shim_file is None or not Path(shim_file).is_file():
                logger.info("jev_gates_unavailable", reason="no_shim")
                note["reason"] = "unavailable (no_shim)"
                return {}
            key_file = Path(shim_file)
        else:
            if client is None:
                note["reason"] = "unavailable (no_client)"
                return {}
            found = jevkey.resolve(db=db, client=client, tenant_id=tenant_id,
                                   submitted_by=None, logger=logger, call_options=call_options)
            if found is None:
                logger.info("jev_gates_unavailable", reason="no_key")
                note["reason"] = "unavailable (no_key)"
                return {}
            key, _scope, secret = found
            key_file = private / KEY_FILE_NAME
            _write_private(key_file, key)
            logger.info("jev_gates_key_resolved", secret=secret)
        counts = private / COUNTS_FILE_NAME
        if counts.is_symlink() or counts.exists():
            counts.unlink()
        data_dir = private / DATA_DIR_NAME
        data_dir.mkdir(mode=0o700, exist_ok=True)
        names = sorted({n for n in secret_env_names if isinstance(n, str) and n})
        config = private / CONFIG_FILE_NAME
        _write_private(config, json.dumps({
            KEY_FILE_FIELD: str(key_file),
            COUNTS_FILE_FIELD: str(counts),
            DATA_DIR_FIELD: str(data_dir),
            SECRET_NAMES_FIELD: names,
        }, indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001 -- never fatal, said by type
        logger.warning("jev_gates_unavailable", reason="error", error=type(exc).__name__)
        return {}
    note["on"] = True
    return {CONFIG_ENV: str(config)}


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10_000 else 0


def tally(counts_file: Path) -> dict[str, int] | None:
    """The hooks' counts summed, or None when no hook ran. Lines that are not
    the launcher's are skipped; only integers are read, never text."""
    path = Path(counts_file)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_COUNTS_BYTES:
            return None
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    out = {"belay_runs": 0, "belay_calls": 0, "belay_blocks": 0,
           "enforce_runs": 0, "enforce_calls": 0, "enforce_flags": 0, "enforce_rules_flagged": 0}
    seen = False
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("hook") not in (BELAY, ENFORCE):
            continue
        seen = True
        hook = row["hook"]
        out[f"{hook}_runs"] += 1
        out[f"{hook}_calls"] += _count(row.get("calls"))
        if row.get("blocked") is True:
            out["belay_blocks" if hook == BELAY else "enforce_flags"] += 1
        if hook == ENFORCE:
            out["enforce_rules_flagged"] += _count(row.get("rules"))
    return out if seen else None


def finish(env: Mapping[str, str]) -> dict[str, int] | None:
    """After the agent: the counts, and the key file removed. `env` is the
    runner's environment as built; without the gates' config it is None."""
    config_path = env.get(CONFIG_ENV)
    if not config_path:
        return None
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    key_file = config.get(KEY_FILE_FIELD)
    if isinstance(key_file, str) and Path(key_file).name == KEY_FILE_NAME:
        # Only the file this module wrote: the shim file is the worker's.
        try:
            Path(key_file).unlink()
        except OSError:
            pass
    counts_file = config.get(COUNTS_FILE_FIELD)
    return tally(Path(counts_file)) if isinstance(counts_file, str) else None


def worth_an_event(counts: Mapping[str, int] | None) -> bool:
    """A RUNNING mark only when belay blocked a stop or enforce flagged a rule."""
    return bool(counts) and (counts.get("belay_blocks", 0) > 0 or counts.get("enforce_flags", 0) > 0)


__all__ = [
    "BELAY",
    "CONFIG_ENV",
    "ENFORCE",
    "EVENT_CAUSE",
    "GatesVerdict",
    "REFUSED_ENV",
    "REFUSED_NO_VERDICT",
    "REFUSED_OFF_AT_DISPATCH",
    "VERDICT_ENV",
    "SUMMARY_KEY",
    "ON_REQUEST",
    "SWITCH",
    "decide",
    "finish",
    "prepare",
    "read_settings",
    "stored_value",
    "switched_on",
    "tally",
    "worth_an_event",
]
