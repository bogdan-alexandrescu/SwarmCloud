"""Per (provider, tenant) quota state with AIMD adaptive concurrency.

Importing this package must not construct a Firestore client, so the app and
broker factories resolve lazily. `aimd` is pure and safe to import anywhere.
"""

from __future__ import annotations

__all__ = ["build_broker", "create_app"]

__version__ = "0.1.0"


def create_app(broker=None, **kwargs):
    from .main import create_app as _create_app

    return _create_app(broker, **kwargs)


def build_broker(**kwargs):
    from .main import build_broker as _build

    return _build(**kwargs)
