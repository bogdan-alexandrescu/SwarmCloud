"""The real engines call their libraries as those libraries' source says
(swarm_speech/engines.py; qwen-asr 0.0.6, kokoro 0.9.4), and the process
builds its app from the environment (swarm_speech/main.py).

The libraries are replaced in sys.modules by stand-ins that record their
calls: the root environment has no torch and no GPU. What is held is the
shape of each call: Qwen3-ASR's vLLM constructor and streaming state; Kokoro's
model built from the fetched files, a pipeline per language, voices loaded
from their checked .pt files and speech split by sentence.
"""

from __future__ import annotations

import sys
import types

import pytest

import speech_fakes  # noqa: F401 -- puts apps/swarm-speech on sys.path
from swarm_speech import engines, main


class _State:
    text = ""


class _Qwen:
    calls: list = []

    @classmethod
    def LLM(cls, **kwargs):  # noqa: N802 -- the library's name
        cls.calls.append(("LLM", kwargs))
        return cls()

    def init_streaming_state(self, **kwargs):
        self.calls.append(("init_streaming_state", kwargs))
        return _State()

    def finish_streaming_transcribe(self, state):
        state.text = "the final words"
        return state


def test_qwen_loads_through_vllm_from_the_fetched_directory_and_streams(monkeypatch, tmp_path):
    _Qwen.calls = []
    monkeypatch.setitem(sys.modules, "qwen_asr", types.SimpleNamespace(Qwen3ASRModel=_Qwen))
    asr = engines.QwenAsr()
    with pytest.raises(RuntimeError):
        asr.begin()
    asr.load(tmp_path)
    stream = asr.begin()
    assert stream.finish() == "the final words"
    assert _Qwen.calls == [
        ("LLM", {"model": str(tmp_path), "gpu_memory_utilization": engines.GPU_MEMORY_UTILIZATION,
                 "max_new_tokens": engines.MAX_NEW_TOKENS}),
        ("init_streaming_state", {"chunk_size_sec": engines.CHUNK_SECONDS})]


class _Audio:
    def __init__(self, n: int) -> None:
        self.n = n

    def numel(self):
        return self.n

    def detach(self):
        return self

    def clamp(self, lo, hi):
        return self

    def __mul__(self, k):
        return self

    def to(self, dtype):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self

    def astype(self, dtype):
        assert dtype == "<i2"
        return self

    def tobytes(self):
        return b"\x00\x00" * self.n


def _kokoro(record: list):
    class KModel:
        def __init__(self, **kwargs):
            record.append(("KModel", kwargs))

        def to(self, device):
            record.append(("to", device))
            return self

        def eval(self):
            return self

    class KPipeline:
        def __init__(self, **kwargs):
            record.append(("KPipeline", kwargs["lang_code"], kwargs["repo_id"], kwargs["device"]))

        def load_voice(self, path):
            record.append(("load_voice", path))

        def __call__(self, text, voice, split_pattern):
            record.append(("say", text, voice, split_pattern))
            yield types.SimpleNamespace(audio=_Audio(3))
            yield types.SimpleNamespace(audio=None)
            yield types.SimpleNamespace(audio=_Audio(2))

    return types.SimpleNamespace(KModel=KModel, KPipeline=KPipeline)


def test_kokoro_builds_from_the_checked_files_and_speaks_by_sentence(monkeypatch, tmp_path):
    record: list = []
    torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True), int16="int16")
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "kokoro", _kokoro(record))
    (tmp_path / "kokoro-v1_0.pth").write_bytes(b"w")
    tts = engines.KokoroTts()
    tts.load(tmp_path, repo="hexgrad/Kokoro-82M", voices=("af_heart", "am_michael", "bf_emma"))
    chunks = list(tts.synthesize("Hello there. Bye.", "bf_emma"))
    assert chunks == [b"\x00\x00" * 3, b"\x00\x00" * 2]
    assert record[0] == ("KModel", {"repo_id": "hexgrad/Kokoro-82M", "config": str(tmp_path / "config.json"),
                                    "model": str(tmp_path / "kokoro-v1_0.pth")})
    assert [r for r in record if r[0] == "KPipeline"] == [
        ("KPipeline", "a", "hexgrad/Kokoro-82M", "cuda"), ("KPipeline", "b", "hexgrad/Kokoro-82M", "cuda")]
    assert [r[1] for r in record if r[0] == "load_voice"] == [
        str(tmp_path / "voices" / f"{v}.pt") for v in ("af_heart", "am_michael", "bf_emma")]
    assert record[-1] == ("say", "Hello there. Bye.", str(tmp_path / "voices" / "bf_emma.pt"), engines.SENTENCE_SPLIT)
    with pytest.raises(KeyError):
        list(tts.synthesize("hi", "bm_george"))


def test_kokoro_refuses_a_directory_without_exactly_one_weights_file(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setitem(sys.modules, "kokoro", _kokoro([]))
    with pytest.raises(RuntimeError, match="one .pth"):
        engines.KokoroTts().load(tmp_path, repo="hexgrad/Kokoro-82M", voices=("af_heart",))


def test_the_process_will_not_start_without_its_one_caller(monkeypatch):
    monkeypatch.delenv("SWARM_SPEECH_CALLER", raising=False)
    with pytest.raises(RuntimeError, match="SWARM_SPEECH_CALLER"):
        main.create_app_from_env()


def test_a_refused_models_value_still_starts_the_server_to_say_why(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SPEECH_CALLER", "swarm-api@example.iam.gserviceaccount.com")
    monkeypatch.setenv("SWARM_SPEECH_MODELS", '{"stt": {"name": "x", "revision": "main"}}')
    monkeypatch.setattr(main, "MODEL_ROOT", tmp_path / "models")
    monkeypatch.setattr(main, "STATE_DIR", tmp_path / "state")
    app = main.create_app_from_env()
    assert app.state.engine.models is None
    assert isinstance(app.state.engine.stt, engines.QwenAsr) and isinstance(app.state.engine.tts, engines.KokoroTts)
