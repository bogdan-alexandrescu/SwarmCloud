"""The speech image's closure holds no release the image scan of 2026-11-06
refused (apps/swarm-speech/pyproject.toml says why each moved).

The scan refused swarm-speech for pillow 12.3.0 (CVE-2026-112393, CRITICAL;
CVE-2026-112390), vllm 0.14.0 (CVE-2026-22778, CRITICAL; CVE-2026-114053),
jackson-core 2.18.8 inside ray's ray_dist.jar (CVE-2026-89407) and
follow-redirects 1.16.0 inside gradio's bundled node_modules (CVE-2026-111755).
The Dockerfile installs exactly apps/swarm-speech/uv.lock's export, so the lock
is what the image carries.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
LOCK = REPO / "apps" / "swarm-speech" / "uv.lock"


def _versions() -> dict[str, tuple[int, ...]]:
    packages = tomllib.loads(LOCK.read_text())["package"]
    return {p["name"]: tuple(int(part) for part in p["version"].split(".")[:3]) for p in packages
            if "version" in p and all(part.isdigit() for part in p["version"].split(".")[:3])}


def test_pillow_is_at_least_the_release_that_fixes_its_heap_overflow():
    assert _versions()["pillow"] >= (13, 0, 0)


def test_vllm_is_at_least_the_release_that_fixes_both_findings():
    assert _versions()["vllm"] >= (0, 31, 0)


def test_neither_ray_nor_gradio_nor_qwen_asr_is_in_the_image():
    # ray carried the old jackson-core; gradio the old follow-redirects; qwen-asr
    # pinned vllm 0.14.0 and brought gradio.
    names = {p["name"] for p in tomllib.loads(LOCK.read_text())["package"]}
    assert not names & {"ray", "gradio", "qwen-asr"}
