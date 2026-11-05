"""The machine's readings for the 5 s metrics frame
(docs/design/voice-it-own-speech.md §12.1, §12.2).

The server reports over the socket swarm-api already holds, and swarm-api
stores what it reports: no monitoring agent, no grant and no second store.
Each reading that cannot be taken is `None` rather than a guess (a machine with
no NVIDIA driver has no GPU reading; vLLM's gauges exist only once its engine
has run), and a reader that fails once is not retried for the life of the
process, so a missing library costs one exception, not one every 5 s.

vLLM runs in this process (`vllm.LLM`, not its HTTP server), so "vLLM's
/metrics read locally" (§12.1) is its Prometheus gauges read from this
process's registry: the waiting-request count and the KV cache's use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# vLLM's gauge names, old and new: the KV cache gauge was renamed in vLLM 0.10.
_VLLM_WAITING = ("vllm:num_requests_waiting",)
_VLLM_KV = ("vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc")


@dataclass
class Counters:
    """What the server counts between metrics frames' readers; every value only grows."""

    utterances: int = 0
    stt_errors: int = 0
    stt_audio_seconds: float = 0.0
    syntheses: int = 0
    tts_errors: int = 0
    tts_cancelled: int = 0
    tts_audio_seconds: float = 0.0
    protocol_errors: int = 0

    def as_dict(self) -> dict[str, float | int]:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in vars(self).items()}


def _read_cpu_times(proc: Path) -> tuple[int, int] | None:
    """(busy, total) jiffies from /proc/stat's first line."""
    try:
        first = (proc / "stat").read_text(encoding="ascii").splitlines()[0].split()
    except (OSError, IndexError):
        return None
    if first[0] != "cpu":
        return None
    values = [int(v) for v in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    total = sum(values[:8])  # guest time is already inside user and nice
    return total - idle, total


def _read_memory(proc: Path) -> dict[str, int] | None:
    try:
        lines = (proc / "meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    kib: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            kib[key] = int(parts[0])
    if "MemTotal" not in kib or "MemAvailable" not in kib:
        return None
    return {"used_mib": (kib["MemTotal"] - kib["MemAvailable"]) // 1024, "total_mib": kib["MemTotal"] // 1024}


def _read_disk(path: Path) -> dict[str, float] | None:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    return {"used_gb": round((total - free) / 1e9, 2), "total_gb": round(total / 1e9, 2)}


def nvml_reader() -> Callable[[], dict[str, float] | None]:
    """GPU utilisation and memory from NVML, for GPU 0 (the machine has one)."""
    state: dict[str, object] = {}

    def read() -> dict[str, float] | None:
        if state.get("broken"):
            return None
        try:
            import pynvml

            if "handle" not in state:
                pynvml.nvmlInit()
                state["handle"] = pynvml.nvmlDeviceGetHandleByIndex(0)
            handle = state["handle"]
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        except Exception:  # noqa: BLE001 -- no driver, no library: no reading, said once
            state["broken"] = True
            return None
        return {"util_pct": float(util.gpu), "mem_used_mib": mem.used // (1 << 20),
                "mem_total_mib": mem.total // (1 << 20)}

    return read


def vllm_reader() -> Callable[[], dict[str, float | None] | None]:
    """vLLM's waiting requests and KV cache use, from this process's registry."""
    state: dict[str, bool] = {}

    def read() -> dict[str, float | None] | None:
        if state.get("broken"):
            return None
        try:
            from prometheus_client import REGISTRY
        except ImportError:
            state["broken"] = True
            return None
        found: dict[str, float] = {}
        for metric in REGISTRY.collect():
            for sample in metric.samples:
                if sample.name in _VLLM_WAITING + _VLLM_KV:
                    found[sample.name] = found.get(sample.name, 0.0) + float(sample.value)
        if not found:
            return None
        kv = next((found[n] for n in _VLLM_KV if n in found), None)
        return {"queue": next((found[n] for n in _VLLM_WAITING if n in found), None),
                "kv_cache_pct": None if kv is None else round(kv * 100.0, 1)}

    return read


@dataclass
class Sampler:
    """One reading of the machine per call; CPU is the share busy since the last call."""

    disk_path: Path
    proc: Path = Path("/proc")
    gpu: Callable[[], dict | None] = field(default_factory=nvml_reader)
    vllm: Callable[[], dict | None] = field(default_factory=vllm_reader)
    _last_cpu: tuple[int, int] | None = None

    def sample(self) -> dict[str, object]:
        cpu_pct = None
        now = _read_cpu_times(self.proc)
        if now is not None and self._last_cpu is not None:
            busy, total = now[0] - self._last_cpu[0], now[1] - self._last_cpu[1]
            if total > 0:
                cpu_pct = round(100.0 * busy / total, 1)
        self._last_cpu = now
        return {"gpu": self.gpu(), "cpu_pct": cpu_pct, "memory": _read_memory(self.proc),
                "disk": _read_disk(self.disk_path), "vllm": self.vllm()}
