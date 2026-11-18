"""What a Firestore document can hold, applied to the one value the worker
writes that it does not shape itself: `result_summary`.

WHY THIS EXISTS. A summary carries the runner's own `output` whole whenever
it encodes to fewer than 8,000 characters (`lifecycle._truncate_json`), and
that output is whatever the runner and its gate recorded. The pre-publish
gate records its checks as a list of rounds, each round a list of checks
(`prepublish.Gate._round`); a hunk range is a `(start, end)` pair. Firestore
refuses an array held directly in an array, and refuses the whole write with
it: `400 Property result_summary contains an invalid nested entity`. From
2026-10-16 19:51 UTC every claude-code and indexer step that reached
`ControlPlane.finish` with such a value crashed there -- 89 crashes in two
hours. The branch had been pushed, the terminal state never landed, the
reconciler reclaimed the attempt as a lost worker, and the retry's push was
refused because the first attempt's commit was already on the branch. Three
attempts, then FAILED, with the work sitting on GitHub unreported.

So the summary is made storable at the write, not at each of its sources:
the next field to carry a pair, from whichever writer, is caught here too.

WHAT IS REWRITTEN, and only this:

  * an array directly inside an array becomes `{"items": [...]}`, so the
    values stay, in order, one level down;
  * a tuple or a set is an array (a set in sorted order when it can be);
  * a map key that is not a string is its `str()`; an empty key, or one
    Firestore reserves (`__name__`-shaped), gets a visible prefix;
  * a value nested deeper than Firestore's 20 levels is its JSON text,
    cut to `DEEP_TEXT_CHARS`.

Everything else passes through untouched, the same object where nothing
under it changed. `storable` returns the paths it rewrote, so the worker can
say which fields were reshaped without logging a single value.
"""

from __future__ import annotations

import json
from typing import Any

#: The key a nested array is moved under.
ITEMS_KEY = "items"

#: Firestore allows maps and arrays nested 20 deep; the summary itself is one
#: level inside the task document, so its own values get 19.
MAX_DEPTH = 19

#: How much of a too-deep value's JSON is kept.
DEEP_TEXT_CHARS = 2_000

#: How many rewritten paths are reported; the count is always exact.
MAX_PATHS = 20

#: The prefix for a key Firestore would refuse (empty, or `__x__`).
KEY_PREFIX = "key:"


def storable(value: Any) -> tuple[Any, int, list[str]]:
    """`value` as Firestore can store it, how many places were rewritten, and
    up to `MAX_PATHS` of their paths (`$.output.prepublish.rounds[0]`)."""
    paths: list[str] = []
    count = [0]

    def note(path: str) -> None:
        count[0] += 1
        if len(paths) < MAX_PATHS:
            paths.append(path)

    def key_of(key: Any, path: str) -> str:
        text = key if isinstance(key, str) else str(key)
        if not isinstance(key, str):
            note(f"{path}.<{type(key).__name__} key>")
        if text == "" or (text.startswith("__") and text.endswith("__")):
            note(f"{path}.{text or '<empty key>'}")
            text = KEY_PREFIX + text
        return text

    def walk(item: Any, path: str, depth: int, in_array: bool) -> Any:
        if isinstance(item, (tuple, set, frozenset)):
            item = _as_list(item)
        if isinstance(item, (list, dict)) and depth >= MAX_DEPTH:
            note(path)
            return json.dumps(item, default=str)[:DEEP_TEXT_CHARS]
        if isinstance(item, list):
            changed = False
            out = []
            for index, element in enumerate(item):
                new = walk(element, f"{path}[{index}]", depth + 1, True)
                changed = changed or new is not element
                out.append(new)
            result: Any = out if changed else item
            if in_array:
                note(path)
                return {ITEMS_KEY: result}
            return result
        if isinstance(item, dict):
            changed = False
            out_map: dict[str, Any] = {}
            for key, element in item.items():
                name = key_of(key, path)
                new = walk(element, f"{path}.{name}", depth + 1, False)
                changed = changed or new is not element or name is not key
                out_map[name] = new
            return out_map if changed else item
        return item

    return walk(value, "$", 0, False), count[0], paths


def _as_list(item: tuple | set | frozenset) -> list[Any]:
    if isinstance(item, tuple):
        return list(item)
    try:
        return sorted(item)
    except TypeError:
        return list(item)
