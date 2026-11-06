"""The real engines call their libraries as those libraries' source says
(swarm_speech/engines.py; vllm 0.33.0, kokoro 0.9.4), and the process
builds its app from the environment (swarm_speech/main.py).

The libraries are replaced in sys.modules by stand-ins that record their
calls: the root environment has no torch and no GPU. What is held is the
shape of each call: vLLM's constructor and the streaming loop's decodes; Kokoro's
model built from the fetched files, a pipeline per language, voices loaded
from their checked .pt files and speech split by sentence.
"""

from __future__ import annotations

import sys
import types

import pytest

import speech_fakes  # noqa: F401 -- puts apps/swarm-speech on sys.path
from swarm_speech import engines, main


class _Tokenizer:
    """One token per character, so a prefix's rollback is countable."""

    def encode(self, text):
        return list(text)

    def decode(self, ids):
        return "".join(ids)


class _LLM:
    calls: list = []
    answers: list = []

    def __init__(self, **kwargs):
        self.calls.append(("LLM", kwargs))

    def get_tokenizer(self):
        return _Tokenizer()

    def generate(self, requests, *, sampling_params, use_tqdm):
        (request,) = requests
        self.calls.append(("generate", request["prompt"], request["multi_modal_data"]["audio"][0], use_tqdm))
        text = self.answers.pop(0)
        return [types.SimpleNamespace(outputs=[types.SimpleNamespace(text=text)])]


def _vllm(monkeypatch):
    _LLM.calls = []
    monkeypatch.setitem(sys.modules, "vllm", types.SimpleNamespace(
        LLM=_LLM, SamplingParams=lambda **kwargs: ("SamplingParams", kwargs)))
    # The samples vLLM is given, as the number of 16-bit samples heard so far.
    monkeypatch.setattr(engines, "_float_samples", lambda pcm: len(pcm) // 2)


def test_qwen_loads_vllms_own_model_from_the_fetched_directory(monkeypatch, tmp_path):
    _vllm(monkeypatch)
    asr = engines.QwenAsr()
    with pytest.raises(RuntimeError):
        asr.begin()
    asr.load(tmp_path)
    assert _LLM.calls == [("LLM", {"model": str(tmp_path), "gpu_memory_utilization": engines.GPU_MEMORY_UTILIZATION})]
    assert asr._sampling == ("SamplingParams", {"temperature": 0.0, "max_tokens": engines.MAX_NEW_TOKENS})


def test_qwen_streams_as_the_sdk_did_redecoding_all_audio_from_the_settled_words(monkeypatch, tmp_path):
    _vllm(monkeypatch)
    asr = engines.QwenAsr()
    asr.load(tmp_path)
    second = int(engines.CHUNK_SECONDS * engines.STT_RATE) * 2
    # From the third decode the model continues the prompt's settled words.
    _LLM.answers = ["language English<asr_text>hello", "language English<asr_text>hello there",
                    "there friend", "riends"]
    stream = asr.begin()
    assert stream.feed(b"\0" * (second // 2)) == ""
    assert stream.feed(b"\0" * (second // 2)) == "hello"
    assert stream.feed(b"\0" * (second * 2 + second // 2)) == "hello there friend"
    assert stream.finish() == "hello there friends"
    assert stream.finish() == "hello there friends"
    decodes = [call[1:] for call in _LLM.calls if call[0] == "generate"]
    samples = second // 2
    raw = "language English<asr_text>hello there"
    assert decodes == [
        (engines.ASR_PROMPT, samples, False),
        (engines.ASR_PROMPT, 2 * samples, False),
        # From the third decode on, the words so far less their last 5 tokens.
        (engines.ASR_PROMPT + raw[:-5], 3 * samples, False),
        # The tail, at finish: less the last 5 tokens again.
        (engines.ASR_PROMPT + "language English<asr_text>hello there f", 3 * samples + samples // 2, False)]
    assert len(_LLM.calls) == 5


def test_qwen_finish_flushes_the_tail_without_a_whole_chunk(monkeypatch, tmp_path):
    _vllm(monkeypatch)
    asr = engines.QwenAsr()
    asr.load(tmp_path)
    _LLM.answers = ["language None<asr_text>"]
    stream = asr.begin()
    assert stream.feed(b"\0" * 100) == ""
    assert stream.finish() == ""
    assert [call[1:] for call in _LLM.calls if call[0] == "generate"] == [(engines.ASR_PROMPT, 50, False)]


def test_the_prompt_is_the_one_the_models_chat_template_rendered_for_the_sdk():
    # Qwen/Qwen3-ASR-0.6B's chat_template.json writes the system turn even when empty.
    assert engines.ASR_PROMPT == ("<|im_start|>system\n<|im_end|>\n<|im_start|>user\n"
                                  "<|audio_start|><|audio_pad|><|audio_end|><|im_end|>\n<|im_start|>assistant\n")


def test_the_transcript_is_what_follows_the_tag_with_decoding_loops_kept_once():
    assert engines.transcript("language English<asr_text> hi there ") == "hi there"
    assert engines.transcript("no tag at all") == "no tag at all"
    assert engines.transcript("language None<asr_text>") == ""
    assert engines.transcript("language English<asr_text>so" + "o" * 30) == "so"
    assert engines.transcript("language English<asr_text>ok " + "la " * 25 + "end") == "ok la end"


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
