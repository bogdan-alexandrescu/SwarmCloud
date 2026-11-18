"""Tenant-scoped control-plane API for the agent swarm.

`create_app` is exported lazily: importing this package must not construct a
Firestore client, because the unit tests import it with no credentials and no
PROJECT_ID set.
"""

from __future__ import annotations

__all__ = ["create_app"]

__version__ = "0.1.0"


def create_app(ctx=None):
    from .main import create_app as _create_app

    return _create_app(ctx)
