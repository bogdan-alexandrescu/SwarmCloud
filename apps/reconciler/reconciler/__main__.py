"""Reconciler entrypoint.

    python -m reconciler            serve on $PORT (Cloud Run)
    python -m reconciler --once     run exactly one pass and print the report

`--once` is what `make reconcile` and an operator debugging an incident use: the
same code path the scheduler triggers, with the report on stdout.
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
