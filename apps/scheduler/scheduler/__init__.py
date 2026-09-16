"""Admission controller: a bounded drain loop woken by Pub/Sub.

Importing this package must not construct a Firestore client -- the unit tests
import it with no credentials -- so `create_app` and `build_scheduler` are
resolved lazily.
"""

from __future__ import annotations

__all__ = ["build_scheduler", "create_app"]

__version__ = "0.1.0"


def create_app(scheduler=None):
    from .main import create_app as _create_app

    return _create_app(scheduler)


def build_scheduler(**kwargs):
    from .main import build_scheduler as _build

    return _build(**kwargs)
