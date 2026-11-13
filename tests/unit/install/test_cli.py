"""`swarm_install.cli`: the verbs, discovered; `swarm install`'s dry run, its
`--json-events` stream and `--status` (lane DAYONE-CLI-1, part of #2688;
design §2.1, §2.4, §2.5).

No credentials and no emulator: the steps are fakes, the state bucket is an
in-memory object store, and `HOME` is a temporary directory so the local state
(`~/.config/swarmcloud/install/<project>.json`) lands where the test can see it.
"""

from __future__ import annotations

import json
import sys
import textwrap
from dataclasses import dataclass, field

import pytest

from swarm_install import cli, verbs
from swarm_install.events import EVENT_VERSION, KINDS
from swarm_install.exitcodes import MET, UNMET, ExitCode
from swarm_install.state import STATE_OBJECT, InstallState, PreconditionFailed, StepRecord
from swarm_install.steps import Observation, Plan
from swarm_install.verbs import install


@dataclass
class FakeStep:
    id: str
    stage: int
    satisfies: tuple[str, ...]
    met: bool = False
    applied: list[str] = field(default_factory=list)

    def check(self, ctx) -> Observation:
        return Observation(MET if self.met else UNMET, "fake")

    def plan(self, ctx) -> Plan:
        return Plan(f"create {self.id}", (f"gcloud create {self.id}",))

    def apply(self, ctx, plan) -> None:
        self.applied.append(plan.summary)
        self.met = True

    def verify(self, ctx) -> Observation:
        return self.check(ctx)

    def explain(self) -> str:
        return f"{self.id} is a fake step"


class MemoryStore:
    """An `ObjectStore` held in memory; counts its writes."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.writes = 0

    def read(self, name):
        return self.objects.get(name)

    def write(self, name, data, *, if_generation_match):
        current = self.objects.get(name, (b"", 0))[1]
        if current != if_generation_match:
            raise PreconditionFailed(name)
        self.writes += 1
        self.objects[name] = (data, current + 1)
        return current + 1

    def delete(self, name, *, if_generation_match):
        self.writes += 1
        self.objects.pop(name, None)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(install, "holder_identity", lambda: "admin@example.test")
    return tmp_path


@pytest.fixture
def bucket(monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(install, "open_bucket", lambda project: store)
    return store


@pytest.fixture
def steps(monkeypatch):
    found = [
        FakeStep("preflight", 0, ("other.fake-preflight",), met=True),
        FakeStep("state-bucket", 1, ("other.fake-bucket",)),
    ]
    monkeypatch.setattr(install, "discover_steps", lambda: list(found))
    monkeypatch.setattr(install, "load_manifest", lambda path: None)
    return found


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_verbs_discovered_from_package(tmp_path, monkeypatch, capsys):
    assert "install" in verbs.discover()

    # A verb module dropped into a verbs package is a `swarm` subcommand, with
    # no registry edited: that is how the later lanes add theirs.
    package = tmp_path / "fake_verbs_cli1"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "hello.py").write_text(
        textwrap.dedent(
            """
            NAME = "hello"
            HELP = "say hello"

            def add_arguments(parser):
                parser.add_argument("--who", required=True)

            def run(args):
                print(f"hello {args.who}")
                return 4
            """
        )
    )
    (package / "helpers.py").write_text("VALUE = 1\n")  # no NAME: not a verb
    monkeypatch.syspath_prepend(str(tmp_path))

    assert cli.main(["hello", "--who", "there"], package="fake_verbs_cli1") == 4
    assert capsys.readouterr().out == "hello there\n"

    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"], package="fake_verbs_cli1")
    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert "hello" in out and "say hello" in out and "helpers" not in out

    with pytest.raises(SystemExit) as raised:
        cli.main(["install"], package="fake_verbs_cli1")
    assert raised.value.code == ExitCode.USAGE
    sys.modules.pop("fake_verbs_cli1", None)
    sys.modules.pop("fake_verbs_cli1.hello", None)
    sys.modules.pop("fake_verbs_cli1.helpers", None)


def test_dry_run_writes_nothing(home, bucket, steps, capsys):
    code = cli.main(["install", "--project", "proj-dry", "--env", "prod", "--dry-run",
                     "--version", "2026.10.1", "--domain", "swarm.example.test"])

    out = capsys.readouterr().out
    assert code == ExitCode.OK, out
    assert _files(home) == []
    assert bucket.writes == 0 and bucket.objects == {}
    assert all(not s.applied for s in steps)
    assert "create state-bucket" in out


def test_install_runs_and_records(home, bucket, steps, capsys):
    code = cli.main(["install", "--project", "proj-run", "--env", "prod",
                     "--version", "2026.10.1", "--domain", "swarm.example.test"])

    out = capsys.readouterr().out
    assert code == ExitCode.OK, out
    assert steps[1].applied == ["create state-bucket"]
    local = home / ".config" / "swarmcloud" / "install" / "proj-run.json"
    state = InstallState.from_bytes(local.read_bytes())
    assert state.release == "2026.10.1"
    assert state.hostname == "swarm.example.test"
    assert state.done("state-bucket") and state.done("preflight")


def test_conflicting_release_refused(home, bucket, steps, capsys):
    state = InstallState(project="proj-c", environment="prod", release="2026.09.1")
    bucket.objects[STATE_OBJECT] = (state.to_bytes(), 7)

    code = cli.main(["install", "--project", "proj-c", "--env", "prod", "--version", "2026.10.1"])

    err = capsys.readouterr().err
    assert code == ExitCode.REFUSED
    assert "2026.09.1" in err and "swarm upgrade" in err
    assert all(not s.applied for s in steps)


def test_json_events_schema(home, bucket, steps, capsys):
    code = cli.main(["install", "--project", "proj-json", "--env", "prod", "--dry-run", "--json-events"])

    lines = capsys.readouterr().out.splitlines()
    assert lines, "no events were written"
    events = [json.loads(line) for line in lines]  # every line is one JSON object
    for event in events:
        assert event["v"] == EVENT_VERSION
        assert event["event"] in KINDS
        assert isinstance(event["at"], (int, float))
    assert events[0]["event"] == "run_start" and events[0]["dry_run"] is True
    assert events[-1]["event"] == "run_end"
    assert events[-1]["exit_code"] == int(code)
    plans = [e for e in events if e["event"] == "step_plan"]
    assert [e["step"] for e in plans] == ["state-bucket"]
    assert plans[0]["actions"] == ["gcloud create state-bucket"]


def test_no_steps_is_never_success(home, bucket, monkeypatch, capsys):
    monkeypatch.setattr(install, "discover_steps", lambda: [])
    monkeypatch.setattr(install, "load_manifest", lambda path: None)

    code = cli.main(["install", "--project", "proj-empty", "--env", "prod", "--dry-run"])

    assert code == ExitCode.TROUBLE
    assert "no install steps" in capsys.readouterr().err


def test_missing_manifest_is_a_usage_error(home, bucket, steps, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(install, "load_manifest", install._load_manifest_file)

    code = cli.main(["install", "--project", "proj-m", "--env", "prod", "--dry-run",
                     "--manifest", str(tmp_path / "absent.yaml")])

    assert code == ExitCode.USAGE
    assert "absent.yaml" in capsys.readouterr().err


def _state_with(*done: str, ready: bool = False) -> InstallState:
    state = InstallState(project="example-project", environment="prod", hostname="swarm.example.test")
    for i, step_id in enumerate(done):
        state.steps[step_id] = StepRecord(state="done", at=100.0 + i, by="admin@example.test")
    if ready:
        state.ready_at = 200.0
    return state


def test_status_reads_the_bucket_and_writes_nothing(home, bucket, steps, capsys):
    bucket.objects[STATE_OBJECT] = (_state_with("preflight").to_bytes(), 3)

    code = cli.main(["install", "--status", "--json", "--project", "example-project", "--env", "prod"])

    report = json.loads(capsys.readouterr().out)
    assert code == ExitCode.TROUBLE  # stage 1 is not done
    assert report["source"] == "gs://swarm-tfstate-example-project/swarm-install/state.json"
    assert report["stages"] == [
        {"stage": 0, "state": "done", "at": 100.0, "by": "admin@example.test"},
        {"stage": 1, "state": None, "at": None, "by": None},
    ]
    assert report["ready"] is False
    assert _files(home) == [] and bucket.writes == 0


def test_status_ready_screen(home, bucket, steps, capsys):
    bucket.objects[STATE_OBJECT] = (_state_with("preflight", "state-bucket", ready=True).to_bytes(), 3)

    code = cli.main(["install", "--status", "--project", "example-project", "--env", "prod"])

    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert "https://swarm.example.test" in out
    assert "claude plugin marketplace add" in out
    assert "swarm doctor" in out


def test_status_without_any_state_is_not_fine(home, monkeypatch, steps, capsys):
    def unreadable(project):
        raise PermissionError("no credentials")

    monkeypatch.setattr(install, "open_bucket", unreadable)

    code = cli.main(["install", "--status", "--project", "proj-none", "--env", "prod"])

    captured = capsys.readouterr()
    assert code == ExitCode.TROUBLE
    assert "could not be read" in captured.err
    assert "no credentials" in captured.err


def test_status_ready_screen_for_one_person_names_no_teammates(home, bucket, steps, capsys):
    state = _state_with("preflight", "state-bucket", ready=True)
    state.outputs["sizing"] = {"mode": "individual", "members": 1}
    bucket.objects[STATE_OBJECT] = (state.to_bytes(), 3)

    code = cli.main(["install", "--status", "--project", "example-project", "--env", "prod"])

    out = capsys.readouterr().out
    assert code == ExitCode.OK
    assert "Ready." in out and "swarm doctor" in out
    assert "Teammates" not in out and "claude plugin marketplace add" not in out


def test_a_fresh_laptop_is_told_stage_0_signs_it_in_not_a_bucket_error(home, monkeypatch, steps, capsys):
    """No application-default credentials before stage 0's sign-in is the
    expected first run: one plain line, never the raw exception and its link."""
    from google.auth.exceptions import DefaultCredentialsError

    def no_credentials(project):
        raise DefaultCredentialsError(
            "Your default credentials were not found. To set up Application Default Credentials, "
            "see https://cloud.google.com/docs/authentication/external/set-up-adc for more information.")

    monkeypatch.setattr(install, "open_bucket", no_credentials)

    code = cli.main(["install", "--project", "example-project", "--env", "prod"])

    captured = capsys.readouterr()
    assert code == ExitCode.OK
    assert "could not be read" not in captured.err
    assert "DefaultCredentialsError" not in captured.out + captured.err
    assert "cloud.google.com" not in captured.out + captured.err
    assert "not signed in yet; stage 0 signs you in" in captured.out


def test_a_state_bucket_not_made_yet_is_not_a_read_failure(home, monkeypatch, steps, capsys):
    from google.api_core.exceptions import NotFound

    def no_bucket(project):
        raise NotFound("The specified bucket does not exist.")

    monkeypatch.setattr(install, "open_bucket", no_bucket)

    code = cli.main(["install", "--project", "example-project", "--env", "prod"])

    captured = capsys.readouterr()
    assert code == ExitCode.OK
    assert "could not be read" not in captured.err and "NotFound" not in captured.err


def test_a_signed_in_read_failure_is_still_said(home, monkeypatch, steps, capsys):
    def refused(project):
        raise PermissionError("403 storage.objects.get denied")

    monkeypatch.setattr(install, "open_bucket", refused)

    cli.main(["install", "--project", "example-project", "--env", "prod"])

    err = capsys.readouterr().err
    assert "could not be read" in err and "storage.objects.get denied" in err
