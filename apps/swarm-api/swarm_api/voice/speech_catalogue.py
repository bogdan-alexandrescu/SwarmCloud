"""What Voice it's Settings may choose, by name (docs/design/voice-it-own-speech.md §11.2).

`GET /v1/voice/settings/catalogue` serves this; `voice/settings.py` refuses
any value that is not one of its names. A choice is a NAME, never a machine
type, image, command, URL or model path as free text: the design's reading
of CONTRACT.md invariant 10 for a machine that is not a runner profile.
Adding a model, a voice or a machine is a new entry here, reviewed like any
code, never a value typed into a form.

THE ENTRIES, and where each figure came from:

* MACHINES are the single-L4 g2 types only (one NVIDIA L4 each; the speech
  machine holds Qwen3-ASR and Kokoro on one GPU). g2-standard-4 at $0.71 an
  hour is BGD's figure (2026-11-05, pick 1) and the default. The others are
  Compute Engine's on-demand us-central1 list price, machine plus its L4,
  priced from the G2 components (vCPU $0.024988, memory $0.002927 per GB,
  one L4 $0.5599, per hour) that reproduce BGD's $0.71 for g2-standard-4;
  `price_read_at` says when. Not yet read through the Cloud Billing Catalog
  API, which needs a billing credential this service does not hold.
* MODELS are pinned to one Hugging Face revision each, with the SHA-256 of
  every weight file the speech machine fetches (§5.2: the boot checks them
  and refuses a mismatch). Read from the Hugging Face API on 2026-11-05.
* THE CONVERSATION MODEL (BGD's answer 3, 2026-11-05): on the own-speech
  engine a small, fast Claude model from the pool answers each spoken
  sentence at once and hands planning and actions to the Claude planner,
  acting as the signed-in person. No OpenAI model is ever listed here.
  WHICH ONE is an admin's pick from a curated list of the Claude models the
  platform already has (the owner's answer, 2026-11-05: "selected from a
  curated list that we have available"): each entry is a model the platform
  prices (`modelprices.SEED`), so every pick runs on the pool's accounts and
  is costed like any other turn. Haiku 4.5, the smallest and fastest, is the
  default; the two Sonnets trade a slower first word for a stronger reply.
* THE PLANNER MODEL (BGD, 2026-11-05: the planner runs the planning class,
  "make it configurable as well"): the model the planner turns of BOTH
  engines run, picked by CLASS, never by id. The names are the frozen
  `swarm_common.profiles.MODEL_CLASSES`, the classes Terraform's
  `local.runner_models` pins one id each (`WORKER_CLASS_MODELS`, which
  swarm-api's settings refuse unless it names exactly those classes), so
  every pick runs on the pool's Claude accounts. `fable`, the planning class,
  is the default: what voice.tf pins as VOICE_PLANNER_MODEL. A session reads
  it at its start (`settings.VoiceSettings.session_planner_model`); a change
  reaches the next session, never one already running.
* ZONES are the deployment's provisioned speech machines, one each
  (`SWARM_SPEECH_ZONES`, from Terraform's `voice_speech.zones`). One zone
  by default (BGD's answer 1): a stock-out falls back to text mode, and no
  second machine is tried.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from swarm_common.profiles import MODEL_CLASSES

from ..settings import parse_worker_class_models

#: The day every price and pin below was read.
READ_AT = "2026-11-05"

ENGINE_OWN = "own"
ENGINE_GPT_LIVE = "gpt-live"


@dataclass(frozen=True)
class Engine:
    name: str
    label: str
    #: Whether the engine runs on the speech machine (and so reads the
    #: machine, model, voice, idle and zone settings).
    uses_machine: bool


@dataclass(frozen=True)
class Machine:
    name: str
    vcpu: int
    memory_gb: int
    gpus: int
    usd_per_hour: float
    price_source: str
    gpu: str = "nvidia-l4"
    price_read_at: str = READ_AT


@dataclass(frozen=True)
class WeightFile:
    path: str
    sha256: str


@dataclass(frozen=True)
class SttModel:
    name: str
    hf: str
    revision: str
    files: tuple[WeightFile, ...]
    disk_gb: int


@dataclass(frozen=True)
class TtsModel:
    name: str
    hf: str
    revision: str
    files: tuple[WeightFile, ...]
    voices: tuple[str, ...]
    default_voice: str


@dataclass(frozen=True)
class ConversationModel:
    name: str
    #: The Anthropic model id the pool account is asked for.
    model: str
    provider: str
    label: str


@dataclass(frozen=True)
class PlannerModel:
    #: A `MODEL_CLASSES` name; the id is the deployment's pin for it.
    name: str
    label: str


@dataclass(frozen=True)
class Idle:
    name: str
    seconds: int


ENGINES: tuple[Engine, ...] = (
    Engine(ENGINE_OWN, "Own speech", uses_machine=True),
    Engine(ENGINE_GPT_LIVE, "GPT-Live", uses_machine=False),
)

_BGD = "BGD's figure, 2026-11-05"
_LIST = "Compute Engine on-demand list price, us-central1, machine plus one L4"

MACHINES: tuple[Machine, ...] = (
    Machine("g2-standard-4", vcpu=4, memory_gb=16, gpus=1, usd_per_hour=0.71, price_source=_BGD),
    Machine("g2-standard-8", vcpu=8, memory_gb=32, gpus=1, usd_per_hour=0.85, price_source=_LIST),
    Machine("g2-standard-12", vcpu=12, memory_gb=48, gpus=1, usd_per_hour=1.00,
            price_source=_LIST),
    Machine("g2-standard-16", vcpu=16, memory_gb=64, gpus=1, usd_per_hour=1.15,
            price_source=_LIST),
)

STT_MODELS: tuple[SttModel, ...] = (
    SttModel(
        "qwen3-asr-0.6b", hf="Qwen/Qwen3-ASR-0.6B",
        revision="5eb144179a02acc5e5ba31e748d22b0cf3e303b0",
        files=(WeightFile("model.safetensors",
                          "79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea"),),
        disk_gb=2,
    ),
)

_KOKORO_VOICES = {
    "af_heart": "0ab5709b8ffab19bfd849cd11d98f75b60af7733253ad0d67b12382a102cb4ff",
    "am_michael": "9a443b79a4b22489a5b0ab7c651a0bcd1a30bef675c28333f06971abbd47bd37",
    "bf_emma": "d0a423deabf4a52b4f49318c51742c54e21bb89bbbe9a12141e7758ddb5da701",
    "bm_george": "f1bc812213dc59774769e5c80004b13eeb79bd78130b11b2d7f934542dab811b",
}

TTS_MODELS: tuple[TtsModel, ...] = (
    TtsModel(
        "kokoro-82m", hf="hexgrad/Kokoro-82M",
        revision="f3ff3571791e39611d31c381e3a41a3af07b4987",
        files=(WeightFile("kokoro-v1_0.pth",
                          "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4"),
               *(WeightFile(f"voices/{voice}.pt", sha) for voice, sha in _KOKORO_VOICES.items())),
        voices=tuple(_KOKORO_VOICES),
        default_voice="af_heart",
    ),
)

CONVERSATION_MODELS: tuple[ConversationModel, ...] = (
    ConversationModel("claude-haiku-4-5", model="claude-haiku-4-5-20251001", provider="anthropic",
                      label="Claude Haiku 4.5 (fastest)"),
    ConversationModel("claude-sonnet-5", model="claude-sonnet-5", provider="anthropic",
                      label="Claude Sonnet 5"),
    ConversationModel("claude-sonnet-4-6", model="claude-sonnet-4-6", provider="anthropic",
                      label="Claude Sonnet 4.6"),
)

#: The planning class (MODEL_CLASSES' docstring: "`fable` is for planning").
PLANNING_CLASS = "fable"
_PLANNER_LABELS = {
    "opus": "Opus class (coding)",
    "fable": "Fable class (planning)",
    "sonnet": "Sonnet class",
    "haiku": "Haiku class (fastest)",
}
#: Every class, in MODEL_CLASSES' order; a class added there is listed here
#: by its name until it is given a label.
PLANNER_MODELS: tuple[PlannerModel, ...] = tuple(
    PlannerModel(name, _PLANNER_LABELS.get(name, name)) for name in MODEL_CLASSES)
#: The variable `model_class_env` sets from `jsonencode(local.runner_models)`.
CLASS_MODELS_ENV = "WORKER_CLASS_MODELS"

IDLE: tuple[Idle, ...] = (
    Idle("2m", 120),
    #: The default: five minutes without speech or tool activity (pick 1).
    Idle("5m", 300),
    Idle("10m", 600),
    Idle("15m", 900),
)

#: The one zone a deployment has when Terraform names none (BGD's answer 1).
DEFAULT_ZONES: tuple[str, ...] = ("us-central1-a",)
ZONES_ENV = "SWARM_SPEECH_ZONES"

#: Every field Settings holds, and its default when no document exists. The
#: voice default follows the default text-to-speech model.
DEFAULTS: dict[str, str] = {
    "engine": ENGINE_OWN,
    "machine": MACHINES[0].name,
    "stt_model": STT_MODELS[0].name,
    "tts_model": TTS_MODELS[0].name,
    "tts_voice": TTS_MODELS[0].default_voice,
    "conversation_model": CONVERSATION_MODELS[0].name,
    "planner_model": PLANNING_CLASS,
    "idle": "5m",
}
#: `zone` is a field too; its default is the deployment's first zone.
FIELDS: tuple[str, ...] = (*DEFAULTS, "zone")


def configured_zones(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """The zones with a provisioned speech machine, in Terraform's order."""
    raw = (os.environ if environ is None else environ).get(ZONES_ENV, "")
    zones = tuple(dict.fromkeys(z.strip() for z in raw.split(",") if z.strip()))
    return zones or DEFAULT_ZONES


def configured_class_models(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """{model class: model id} from WORKER_CLASS_MODELS; empty when unset or
    refused (swarm-api's start refuses a bad map before this is read)."""
    raw = (os.environ if environ is None else environ).get(CLASS_MODELS_ENV, "")
    try:
        return parse_worker_class_models(raw)
    except ValueError:
        return {}


def planner_pin(name: str, class_models: Mapping[str, str]) -> str | None:
    """The model id class `name` runs on this deployment, or None when the
    class is not listed, has no pin, or its pin is not of the class."""
    model_class = MODEL_CLASSES.get(name) if name in _names(PLANNER_MODELS) else None
    model = class_models.get(name)
    if model_class is None or not isinstance(model, str) or not model_class.admits(model):
        return None
    return model


def _names(entries: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(e.name for e in entries)


def choices(field: str, zones: tuple[str, ...], *, tts_model: str | None = None) -> tuple[str, ...]:
    """The names `field` may take. `tts_voice` depends on the model chosen."""
    if field == "engine":
        return _names(ENGINES)
    if field == "machine":
        return _names(MACHINES)
    if field == "stt_model":
        return _names(STT_MODELS)
    if field == "tts_model":
        return _names(TTS_MODELS)
    if field == "tts_voice":
        model = tts(tts_model or DEFAULTS["tts_model"])
        return model.voices if model is not None else ()
    if field == "conversation_model":
        return _names(CONVERSATION_MODELS)
    if field == "planner_model":
        return _names(PLANNER_MODELS)
    if field == "idle":
        return _names(IDLE)
    if field == "zone":
        return zones
    return ()


def defaults(zones: tuple[str, ...]) -> dict[str, str]:
    return {**DEFAULTS, "zone": zones[0]}


def machine(name: str) -> Machine | None:
    return next((m for m in MACHINES if m.name == name), None)


def tts(name: str) -> TtsModel | None:
    return next((m for m in TTS_MODELS if m.name == name), None)


def idle_seconds(name: str) -> int | None:
    return next((i.seconds for i in IDLE if i.name == name), None)


def _files(files: tuple[WeightFile, ...]) -> list[dict[str, str]]:
    return [{"path": f.path, "sha256": f.sha256} for f in files]


def view(zones: tuple[str, ...],
         class_models: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The catalogue as `GET /v1/voice/settings/catalogue` serves it. Each
    planner class shows the id it runs (`class_models`, else
    WORKER_CLASS_MODELS), None where the deployment pins none."""
    pins = configured_class_models() if class_models is None else class_models
    return {
        "engines": [{"name": e.name, "label": e.label, "uses_machine": e.uses_machine}
                    for e in ENGINES],
        "machines": [{"name": m.name, "vcpu": m.vcpu, "memory_gb": m.memory_gb, "gpu": m.gpu,
                      "gpus": m.gpus, "usd_per_hour": m.usd_per_hour,
                      "price_read_at": m.price_read_at, "price_source": m.price_source}
                     for m in MACHINES],
        "stt_models": [{"name": m.name, "hf": m.hf, "revision": m.revision,
                        "files": _files(m.files), "disk_gb": m.disk_gb} for m in STT_MODELS],
        "tts_models": [{"name": m.name, "hf": m.hf, "revision": m.revision,
                        "files": _files(m.files), "voices": list(m.voices),
                        "default_voice": m.default_voice} for m in TTS_MODELS],
        "conversation_models": [{"name": m.name, "label": m.label, "model": m.model,
                                 "provider": m.provider} for m in CONVERSATION_MODELS],
        "planner_models": [{"name": m.name, "label": m.label, "model": planner_pin(m.name, pins)}
                           for m in PLANNER_MODELS],
        "idle": [{"name": i.name, "seconds": i.seconds} for i in IDLE],
        "zones": [{"name": z, "machine": True} for z in zones],
        "defaults": defaults(zones),
        "read_at": READ_AT,
    }


__all__ = [
    "CLASS_MODELS_ENV",
    "CONVERSATION_MODELS",
    "DEFAULTS",
    "DEFAULT_ZONES",
    "ENGINES",
    "ENGINE_GPT_LIVE",
    "ENGINE_OWN",
    "FIELDS",
    "IDLE",
    "MACHINES",
    "PLANNER_MODELS",
    "PLANNING_CLASS",
    "READ_AT",
    "STT_MODELS",
    "TTS_MODELS",
    "ZONES_ENV",
    "choices",
    "configured_class_models",
    "configured_zones",
    "defaults",
    "idle_seconds",
    "machine",
    "planner_pin",
    "tts",
    "view",
]
