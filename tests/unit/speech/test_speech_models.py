"""Which weights the speech server fetches, and how it checks them
(docs/design/voice-it-own-speech.md §5.2, §11.2; swarm_speech/models.py).

Models are named by the catalogue, pinned to a commit, and every file fetched
is one the SHA-256 list names and matches. Anything else in
SWARM_SPEECH_MODELS (a branch, a path, a missing checksum, a field the
catalogue does not define) refuses the boot: the metadata is set by swarm-api
from catalogue names only (invariant 10's rule), and this is the server's own
check of it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets

import pytest

import speech_fakes as fk
from swarm_speech import models as m
from swarm_speech.engines import resample_24k_to_16k


def raw() -> dict:
    voice = {"voices/af_heart.pt": hashlib.sha256(b"v").hexdigest()}
    return {
        "stt": {"name": "qwen3-asr-0.6b", "hf": "Qwen/Qwen3-ASR-0.6B", "revision": secrets.token_hex(20),
                "sha256": {"model.safetensors": hashlib.sha256(b"w").hexdigest()}},
        "tts": {"name": "kokoro-82m", "hf": "hexgrad/Kokoro-82M", "revision": secrets.token_hex(20),
                "sha256": {"kokoro-v1_0.pth": hashlib.sha256(b"k").hexdigest(), **voice},
                "voices": ["af_heart"]},
    }


def test_the_catalogues_shape_parses():
    parsed = m.parse_models(json.dumps(raw()))
    assert parsed.stt.name == "qwen3-asr-0.6b" and parsed.tts.voices == ("af_heart",)
    assert parsed.stt.dirname == f"qwen3-asr-0.6b@{parsed.stt.revision}"


def _set(path: str, value):
    def edit(doc: dict) -> None:
        *head, last = path.split(".")
        target = doc
        for key in head:
            target = target[key]
        if value is _DELETE:
            del target[last]
        else:
            target[last] = value
    return edit


_DELETE = object()


@pytest.mark.parametrize("edit", [
    _set("stt.revision", "main"),
    _set("stt.name", "../../etc"),
    _set("stt.hf", "https://example.com/weights"),
    _set("stt.sha256", {}),
    _set("stt.sha256", {"../escape.bin": "0" * 64}),
    _set("stt.sha256", {"/abs/model.bin": "0" * 64}),
    _set("stt.sha256", {"model.safetensors": "not-a-digest"}),
    _set("stt.image", "evil:latest"),
    _set("tts.voices", []),
    _set("tts.voices", ["af_heart", "/tmp/voice.pt"]),
    _set("tts.voices", ["bm_george"]),  # a voice whose file has no checksum
    _set("tts", _DELETE),
], ids=["branch", "name-path", "url", "no-sums", "dotdot", "absolute", "bad-digest", "extra-field",
        "no-voices", "voice-path", "voice-unsummed", "no-tts"])
def test_anything_outside_the_catalogues_shape_is_refused(edit):
    doc = copy.deepcopy(raw())
    edit(doc)
    with pytest.raises(m.ModelConfigError):
        m.parse_models(json.dumps(doc))


@pytest.mark.parametrize("text", [None, "", "not json", "[]", '{"stt": {}}'])
def test_a_missing_or_malformed_value_is_refused(text):
    with pytest.raises(m.ModelConfigError):
        m.parse_models(text)


def test_fetch_takes_only_the_listed_files_at_the_pinned_commit_and_marks_it_complete(tmp_path):
    spec = fk.models().tts
    download = fk.FakeDownload()
    where = m.fetch(tmp_path, spec, download)
    assert where == tmp_path / spec.dirname
    assert sorted(f for _, f, _ in download.calls) == sorted(spec.sha256)
    assert {r for _, _, r in download.calls} == {spec.revision}
    assert (where / "voices" / "af_heart.pt").read_bytes() == fk.blob("voices/af_heart.pt")
    assert m.is_present(tmp_path, spec)
    again = fk.FakeDownload()
    m.fetch(tmp_path, spec, again)
    assert again.calls == []


def test_a_file_that_does_not_match_its_checksum_leaves_nothing_behind(tmp_path):
    spec = fk.models().stt
    with pytest.raises(m.ChecksumMismatch) as refused:
        m.fetch(tmp_path, spec, fk.FakeDownload(corrupt="model.safetensors"))
    assert "model.safetensors" in str(refused.value)
    assert list(tmp_path.iterdir()) == []
    assert not m.is_present(tmp_path, spec)


def test_the_warm_up_resampler_keeps_two_samples_in_three():
    pcm = b"".join(int(v).to_bytes(2, "little", signed=True) for v in (0, 300, 600, 900, 1200, 1500))
    out = resample_24k_to_16k(pcm)
    samples = [int.from_bytes(out[i:i + 2], "little", signed=True) for i in range(0, len(out), 2)]
    assert samples == [0, 450, 900, 1350]
    assert len(resample_24k_to_16k(b"\x00\x00" * 72_000)) == 2 * 48_000
