"""The speech server's process (docs/design/voice-it-own-speech.md §5.2).

uvicorn builds the app through `create_app_from_env` with `factory=True`, so
nothing reads the environment, loads a model or reaches Google at import.

The environment, set by the launcher (vm/swarm-speech-launch.sh) from the
instance's metadata (§5.1):

    SWARM_SPEECH_CALLER  swarm-api's service account email, the one caller admitted
    SWARM_SPEECH_MODELS  the models to run, models.py's JSON
    PORT                 8080, the port the firewall opens (§3.2)

The server refuses to start without SWARM_SPEECH_CALLER (it would admit no
one). A missing or refused SWARM_SPEECH_MODELS still starts it, in phase
`failed` with reason `models_config`, so swarm-api's socket learns why and
falls back to text instead of waiting out its four minutes.

Logs are lines on stdout, for the machine's logging agent (journald collects
the container's output); they carry codes and counts, never audio or words.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from fastapi import FastAPI

from .engines import KokoroTts, QwenAsr
from .models import ModelConfigError, parse_models
from .server import AUDIENCE, create_app, google_caller_check

# The launcher mounts the host's /var/lib/swarm-speech here: the weights
# survive the stopped machine (they are on its kept boot disk, §5.1) and the
# activity file is where the host's watchdog reads it.
MODEL_ROOT = Path("/var/lib/swarm-speech/models")
STATE_DIR = Path("/var/lib/swarm-speech/state")


def create_app_from_env() -> FastAPI:
    caller = os.environ.get("SWARM_SPEECH_CALLER", "").strip()
    check = google_caller_check(AUDIENCE, frozenset([caller]) if caller else frozenset())
    models, error = None, None
    try:
        models = parse_models(os.environ.get("SWARM_SPEECH_MODELS"))
    except ModelConfigError as exc:
        error = str(exc)
    return create_app(stt=QwenAsr(), tts=KokoroTts(), models=models, config_error=error,
                      model_root=MODEL_ROOT, state_dir=STATE_DIR, verify_caller=check)


def main() -> None:
    import uvicorn

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("swarm_speech.main:create_app_from_env", factory=True, host="0.0.0.0", port=port,
                log_level="info", ws_ping_interval=20.0, ws_ping_timeout=20.0)


if __name__ == "__main__":
    main()
