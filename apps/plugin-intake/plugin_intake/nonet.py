"""No network, from inside the program.

The job's Terraform gives it no egress and no secrets (lane 3); this is the
second wall. The first thing `main` does is replace every way the standard
library opens a socket or resolves a name with a function that raises, so a
parser bug the archive reaches cannot call out even where a route exists.
"""

from __future__ import annotations

import socket


class NetworkForbidden(RuntimeError):
    """The intake job opened, or tried to open, a socket."""


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise NetworkForbidden("the plugin intake job never opens a socket")


_GUARDED = ("socket", "create_connection", "create_server", "socketpair", "fromfd", "getaddrinfo", "gethostbyname", "gethostbyname_ex")


def install() -> None:
    """From here on, every socket the process would open raises `NetworkForbidden`."""
    for name in _GUARDED:
        if hasattr(socket, name):
            setattr(socket, name, _refuse)
