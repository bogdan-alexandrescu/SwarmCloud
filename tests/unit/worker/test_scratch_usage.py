"""What fills a scratch volume, read before kubelet evicts the pod for it (#2436).

kubelet evicts a pod whose emptyDir passes its `sizeLimit` with no grace
period, so the worker reads its volumes on every heartbeat instead
(`agent_worker.scratchusage`), and the reconciler reports the newest reading.
These pin the reading against a volume of known contents: allocated bytes, the
largest paths first, a descent through a single wrapper directory, a bounded
walk that says so, and paths that are cut and redacted.

Imports are inside the tests so each one fails on its own.
"""

from __future__ import annotations

import secrets
from pathlib import Path

MIB = 1024**2
GIB = 1024**3
#: Directory blocks and file tails: what block rounding may add to a size.
ROUNDING = 256 * 1024


def _file(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written, not truncated: a sparse file allocates nothing, and the
    # reading counts allocated blocks as kubelet does.
    path.write_bytes(b"x" * size)


def _shown(path: Path) -> str:
    """A path as a reading reports it: cut at `PATH_CHARS`, however long the test's tmp path is."""
    from agent_worker.scratchusage import PATH_CHARS

    return str(path)[:PATH_CHARS]


def _fill(root: Path) -> None:
    for i in range(3):
        _file(root / "pgdata" / "base" / f"rel{i}", 10 * MIB)
    _file(root / "tr" / "run.log", 10 * MIB)
    _file(root / "ms-playwright" / "chromium" / "chrome", 5 * MIB)
    for i in range(4):
        _file(root / f"note{i}.txt", 100)


def test_sample_names_the_largest_paths_of_a_volume_with_known_contents(tmp_path: Path):
    # MUTATION: sort `top` ascending in `scratchusage.sample` -- the small
    # notes come first and the order assertion fails.
    from agent_worker.scratchusage import sample

    volume = tmp_path / "tmp"
    _fill(volume)

    reading = sample([("tmp", volume, 3 * GIB)])

    entry = reading["tmp"]
    assert entry["limit_bytes"] == 3 * GIB
    assert entry["partial"] is False
    paths = [path for path, _ in entry["top"]]
    assert paths[:3] == [_shown(volume / "pgdata"), _shown(volume / "tr"), _shown(volume / "ms-playwright")]
    assert len(entry["top"]) == 5
    sizes = dict((path, size) for path, size in entry["top"])
    for name, expected in (("pgdata", 30 * MIB), ("tr", 10 * MIB), ("ms-playwright", 5 * MIB)):
        assert expected <= sizes[_shown(volume / name)] < expected + ROUNDING, name
    assert 45 * MIB <= entry["used_bytes"] < 45 * MIB + ROUNDING


def test_sample_descends_through_a_single_wrapper_directory(tmp_path: Path):
    # MUTATION: set `MAX_DESCENT` to 0 -- the report names the wrapper alone.
    from agent_worker.scratchusage import sample

    volume = tmp_path / "workspace"
    wrapper = volume / "att_1" / "work"
    _fill(wrapper)
    _file(volume / "stray.txt", 100)

    entry = sample([("workspace", volume, 5 * GIB)])["workspace"]

    assert [path for path, _ in entry["top"]][:3] == [
        _shown(wrapper / "pgdata"), _shown(wrapper / "tr"), _shown(wrapper / "ms-playwright"),
    ]


def test_a_bounded_walk_says_it_is_partial(tmp_path: Path):
    # MUTATION: return `False` for partial at the entry bound -- the floor
    # reads as the whole volume.
    from agent_worker.scratchusage import sample

    volume = tmp_path / "tmp"
    for i in range(50):
        _file(volume / f"f{i:02d}", 100)

    assert sample([("tmp", volume, GIB)], max_entries=10)["tmp"]["partial"] is True
    assert sample([("tmp", volume, GIB)])["tmp"]["partial"] is False


def test_paths_are_cut_and_redacted(tmp_path: Path, monkeypatch):
    # MUTATION: cut before redacting in `scratchusage._shown` -- the secret's
    # head survives the cut, unredacted.
    from agent_worker import scratchusage
    from agent_worker.redact import REDACTED, scrub_text
    from agent_worker.scratchusage import sample

    value = secrets.token_hex(24)
    volume = tmp_path / "tmp"
    # The cut is set 60 characters into the directory's name, whatever the
    # tmp path's length, and the secret starts 20 before it: a path cut first
    # would keep the secret's head and leave nothing for the redactor to match.
    cut = len(str(volume) + "/") + 60
    monkeypatch.setattr(scratchusage, "PATH_CHARS", cut)
    _file(volume / ("a" * 40 + value + "b" * 100) / "blob", MIB)

    entry = sample([("tmp", volume, GIB)], scrub=lambda text: scrub_text(text, [value]))["tmp"]

    path = entry["top"][0][0]
    assert value not in path and value[:16] not in path
    assert REDACTED in path
    assert len(path) == cut


def test_format_top_reads_as_path_and_size_pairs():
    from agent_worker.scratchusage import format_top

    assert format_top({"top": [["/tmp/pgdata", 30 * MIB], ["/tmp/tr", 10 * MIB]]}) == (
        "/tmp/pgdata 30.0 MiB, /tmp/tr 10.0 MiB"
    )
    assert format_top({"top": [["/tmp/pgdata", int(2.1 * GIB)], ["/tmp/ms-playwright", int(0.7 * GIB)]]}) == (
        "/tmp/pgdata 2.1 GiB, /tmp/ms-playwright 716.8 MiB"
    )
    assert format_top({"top": []}) == ""


def test_parse_volumes_reads_the_dispatchers_value_and_refuses_a_malformed_one():
    from agent_worker.scratchusage import parse_volumes, warn_threshold

    assert parse_volumes("") == []
    assert parse_volumes(None) == []
    assert parse_volumes("workspace=/workspace:5,tmp=/tmp:3,home=/home/swarm:2") == [
        ("workspace", Path("/workspace"), 5 * GIB),
        ("tmp", Path("/tmp"), 3 * GIB),
        ("home", Path("/home/swarm"), 2 * GIB),
    ]
    for bad in ("tmp=/tmp", "tmp=tmp:3", "tmp=/tmp:x", "tmp=/tmp:0", "=/tmp:3", "tmp=/tmp:3,junk"):
        assert parse_volumes(bad) == [], bad
    assert warn_threshold(3 * GIB) == 2 * GIB


def test_a_reading_stays_within_its_share_of_the_event():
    # MUTATION: return `reading` unchanged from `bounded` -- three volumes of
    # long paths outgrow the event's size cap.
    import json

    from agent_worker.scratchusage import SCRATCH_DETAIL_MAX_BYTES, bounded

    long = "/" + "p" * 119
    reading = {
        name: {"used_bytes": used, "limit_bytes": 3 * GIB, "top": [[long, 1] for _ in range(5)], "partial": False}
        for name, used in (("workspace", GIB), ("tmp", int(2.5 * GIB)), ("home", 0))
    }

    out = bounded(reading)

    assert len(json.dumps(out).encode()) <= SCRATCH_DETAIL_MAX_BYTES
    assert len(out["tmp"]["top"]) == 5, "the fullest volume keeps its paths"
