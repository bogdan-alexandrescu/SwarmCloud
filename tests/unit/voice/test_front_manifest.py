"""swarm-voice's two model pins come from Terraform's `local.runner_models`
(docs/design/voice-it-own-speech.md §14.1): the fast front runs the haiku
class, the planner the planning class (fable), on both engines.

Reads the manifest template, voice.tf's render and locals.tf's map; checks
claudeturn's defaults are the same pins, so a pod started without the two
variables runs the same models.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
_APP = str(REPO / "apps" / "swarm-voice")
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from swarm_voice import claudeturn  # noqa: E402

MANIFEST = REPO / "kubernetes" / "swarm-system" / "swarm-voice.yaml"
VOICE_TF = REPO / "terraform" / "infra" / "voice.tf"
LOCALS_TF = REPO / "terraform" / "infra" / "locals.tf"


def _env() -> dict[str, str]:
    docs = [d for d in yaml.safe_load_all(MANIFEST.read_text()) if d]
    [sts] = [d for d in docs if d["kind"] == "StatefulSet"]
    [container] = sts["spec"]["template"]["spec"]["containers"]
    return {e["name"]: e.get("value") for e in container["env"]}


def _runner_models() -> dict[str, str]:
    block = re.search(r"runner_models = \{(.*?)\n  \}", LOCALS_TF.read_text(), re.S)
    assert block, "locals.tf has no runner_models map"
    return dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]+)"', block.group(1), re.M))


def test_the_manifest_takes_both_pins_from_placeholders():
    env = _env()
    assert env[claudeturn.FRONT_MODEL_ENV] == "__VOICE_FRONT_MODEL__"
    assert env[claudeturn.PLANNER_MODEL_ENV] == "__VOICE_PLANNER_MODEL__"


def test_voice_tf_renders_them_from_runner_models_haiku_and_fable():
    text = VOICE_TF.read_text()
    assert '"__VOICE_FRONT_MODEL__", local.runner_models["haiku"])' in text
    assert '"__VOICE_PLANNER_MODEL__", local.runner_models["fable"])' in text


def test_claudeturns_defaults_are_the_same_pins():
    models = _runner_models()
    assert claudeturn.FRONT_MODEL == models["haiku"]
    assert claudeturn.MODEL == models["fable"]
