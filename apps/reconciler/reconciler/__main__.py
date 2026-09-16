"""Reconciler entrypoint.

    python -m reconciler            serve on $PORT (Cloud Run)
    python -m reconciler --once     run exactly one pass and print the report

`--once` is what an operator debugging an incident uses: the same code path the
scheduler triggers, with the report on stdout. There is deliberately no wrapper
around it -- run it against a deployed environment with

    uv run python -m reconciler --once

after `set -a; . ./.env; set +a`, which is what supplies PROJECT_ID and the rest
of `swarm_common.config.Settings`. The exit code is 0 when the pass completed
with no errors and 1 when any repair failed, so it is usable from a shell
condition.
"""

from __future__ import annotations

import json
import os
import sys

from swarm_common.config import Settings

from .config import ReconcilerConfig
from .logs import build_logger
from .service import build_reconciler, create_app


def main() -> int:
    logger = build_logger()
    if "--once" in sys.argv:
        settings = Settings.from_env()
        config = ReconcilerConfig.from_env(settings)
        report = build_reconciler(config, settings, logger).run_once()
        print(json.dumps(report.as_dict(), indent=2, default=str))
        return 0 if not report.errors else 1

    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(create_app(logger=logger), host="0.0.0.0", port=port, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
