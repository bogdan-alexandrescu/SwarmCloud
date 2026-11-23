"""One source for model pins: every class-pin literal under apps/ equals terraform's `runner_models`.

terraform/infra/locals.tf's `runner_models` is "the one place a model id is
stated", keyed by model class, and a model release is an edit to that map and
a release (docs/design/model-per-step.md §4.2). Code that restates a pin -- the
voice planner's default `claudeturn.MODEL`, which a deployment overrides with
VOICE_PLANNER_MODEL, or the UI's offline fixture -- would otherwise keep the
old id after the map moves, and run it wherever the override is absent (the
owner's re-pin of fable to claude-fable-5-2, 2026-11-23).

So this reads the map and every source file under apps/ (outside tests and
docs), finds each literal `claude-<family>-<n>-<n>`, and for a family that is
a model class asserts it is that class's pin. An id that is deliberately NOT
the pin is listed below with its reason; an entry that no longer matches
anything, or that is the pin after all, fails too, so the list cannot rot.

Offline: reads files, no import of the apps.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCALS = ROOT / "terraform" / "infra" / "locals.tf"
APPS = ROOT / "apps"
CLAUDETURN = APPS / "swarm-voice" / "swarm_voice" / "claudeturn.py"

#: A model id as code spells one; `claude-haiku-4-5-20251001` and
#: `claude-haiku-4-5@20251001` read as `claude-haiku-4-5`.
MODEL_ID = re.compile(r"(?<![\w-])claude-([a-z]+)-(\d+)-(\d+)(?!\d)")
#: The files that hold code or configuration; docs (`.md`) are not code.
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".json", ".toml", ".sh", ".yaml", ".yml"}
#: Directories that are tests, dependencies or build output.
SKIP_DIRS = {"node_modules", "tests", "test", "__tests__", "dist", "build", ".venv", "__pycache__"}

#: Files whose every model id is data about many models, not a pin.
FILE_EXCEPTIONS = {
    "apps/swarm-api/swarm_api/modelprices.py":
        "the price seed lists every model the platform prices, current and retired: "
        "a former pin (claude-fable-5-1) stays so tasks recorded on it are still priced",
}

#: (file, id) -> why that file names a class's model that is not its pin.
EXCEPTIONS = {
    ("apps/swarm-api/swarm_api/wfdescription.py", "claude-haiku-4-5"):
        "DEFAULT_MODEL is Vertex AI's id (`claude-haiku-4-5@20251001`) of the workflow-description "
        "summary model: another provider's id, outside the class map, overridable per deployment",
    ("apps/quota-broker/quota_broker/api_key.py", "claude-haiku-4-5"):
        "DEFAULT_PROBE_MODEL: the vendor's smallest model a key probe asks, not a step's class",
    ("apps/swarm-voice/swarm_voice/claudefront.py", "claude-sonnet-4-6"):
        "FRONT_MODELS: a curated conversation model an admin may pick, not the sonnet class's pin",
    ("apps/swarm-api/swarm_api/voice/speech_catalogue.py", "claude-sonnet-4-6"):
        "CONVERSATION_MODELS: the same curated pick, held equal to claudefront.FRONT_MODELS",
    ("apps/swarm-api/swarm_api/voice/speech_catalogue.py", "claude-haiku-4-5"):
        "the docstring names the retired conversation model a stored pick may still hold",
    ("apps/swarm-api/swarm_api/schedtypes/model_prices_check.py", "claude-haiku-4-5"):
        "a docstring example of a dated id's priced name",
    ("apps/swarm-ui/src/AdminSettings.tsx", "claude-haiku-4-5"):
        "a comment's example of a priced name's dated suffix",
    ("apps/swarm-ui/src/AdminSettings.tsx", "claude-opus-4-8"):
        "a comment's example of a priced name's context-window suffix",
}


def runner_models() -> dict[str, str]:
    """`local.runner_models` as locals.tf states it: class -> id."""
    text = LOCALS.read_text(encoding="utf-8")
    block = re.search(r"^\s*runner_models\s*=\s*\{(.*?)^\s*\}", text, re.M | re.S)
    assert block, "terraform/infra/locals.tf has no runner_models map"
    pins = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]+)"', block.group(1), re.M))
    assert pins, "runner_models states no pin"
    return pins


def _is_test_file(name: str) -> bool:
    return (name.startswith("test_") or name == "conftest.py" or name.endswith("_test.py")
            or ".test." in name or ".spec." in name)


def source_files() -> list[Path]:
    found: list[Path] = []
    for here, dirs, files in os.walk(APPS):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(here) / name
            if path.suffix in SOURCE_SUFFIXES and not _is_test_file(name):
                found.append(path)
    return found


def literal_ids() -> dict[tuple[str, str], list[int]]:
    """(repo-relative file, id) -> the lines it is on, for every model id under apps/."""
    seen: dict[tuple[str, str], list[int]] = {}
    for path in source_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for match in MODEL_ID.finditer(line):
                seen.setdefault((rel, match.group(0)), []).append(number)
    return seen


def test_the_map_names_one_pin_per_class_and_fable_is_5_2():
    pins = runner_models()

    assert set(pins) == {"opus", "fable", "sonnet", "haiku"}
    # The owner's re-pin of 2026-11-23.
    assert pins["fable"] == "claude-fable-5-2"
    for cls, model in pins.items():
        assert MODEL_ID.fullmatch(model) and MODEL_ID.fullmatch(model).group(1) == cls, (cls, model)


def test_the_scan_reads_the_apps():
    """An empty scan would pass every check below: it must have read the tree."""
    files = source_files()
    ids = literal_ids()

    assert len(files) > 500, f"scanned only {len(files)} files under apps/"
    assert ("apps/swarm-voice/swarm_voice/claudeturn.py", runner_models()["fable"]) in ids
    assert not any("/tests/" in f or "__tests__" in f for f, _ in ids)


def test_every_class_literal_in_apps_is_its_class_pin():
    pins = runner_models()
    drift = []
    for (rel, model), lines in sorted(literal_ids().items()):
        family = MODEL_ID.fullmatch(model).group(1)
        if family not in pins or model == pins[family]:
            continue
        if rel in FILE_EXCEPTIONS or (rel, model) in EXCEPTIONS:
            continue
        drift.append(f"{rel}:{lines[0]} names {model}, but the {family} class's pin is {pins[family]}")

    assert not drift, (
        "a model id under apps/ restates a class but not its pin in terraform/infra/locals.tf "
        "runner_models: change it to the pin, or list it in EXCEPTIONS with the reason:\n"
        + "\n".join(drift)
    )


def test_every_exception_still_names_a_non_pin_literal():
    pins = runner_models()
    ids = literal_ids()

    for (rel, model), reason in EXCEPTIONS.items():
        assert reason
        assert (rel, model) in ids, f"stale exception: {rel} no longer names {model}"
        family = MODEL_ID.fullmatch(model).group(1)
        assert family in pins and pins[family] != model, (
            f"needless exception: {model} in {rel} is the {family} pin, or no class's model"
        )
    for rel, reason in FILE_EXCEPTIONS.items():
        assert reason and (ROOT / rel).is_file(), rel


def _module_str(path: Path, name: str) -> str:
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name):
            assert isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            return node.value.value
    raise AssertionError(f"{path.name} assigns no {name}")


def test_the_voice_defaults_are_their_classes_pins():
    """claudeturn.MODEL is the fable pin and FRONT_MODEL the haiku pin: the
    defaults a run with no VOICE_PLANNER_MODEL / VOICE_FRONT_MODEL uses."""
    pins = runner_models()

    assert _module_str(CLAUDETURN, "MODEL") == pins["fable"]
    assert _module_str(CLAUDETURN, "FRONT_MODEL") == pins["haiku"]
    assert _module_str(CLAUDETURN, "PLANNER_MODEL_ENV") == "VOICE_PLANNER_MODEL"
    assert _module_str(CLAUDETURN, "FRONT_MODEL_ENV") == "VOICE_FRONT_MODEL"
