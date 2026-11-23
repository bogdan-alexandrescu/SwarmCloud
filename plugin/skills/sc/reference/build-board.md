### `build-board`

This project's build board (archived issue 1773): one local HTML page of every lane of
the work the developer submitted against this project's repository -- its
state, pull request and what it waits on, grouped by feature -- and a
"Waiting on someone" list -- an owner question counts there only while its
lane is open (not merged, not finished) and unanswered on the task, once per
lane (archived issue 1812). It lives at `<project>/.claude/build-board/`,
beside a `.gitignore` of `*`, so it is never committed, and it belongs to no
Claude account. The bridge already refreshes it after every dispatch, check
and attach, and as rows see work move; this verb refreshes it now and shows
it.

1. Make ONE `swarm_build_board` call (no arguments). It reads, renders and
   writes the page and answers `{path, written_at, counts, waiting}`.
2. Open the returned `path` -- the absolute path, exactly as answered -- with
   `open <path>` on macOS or `xdg-open <path>` elsewhere (the session's
   platform says which). Those two commands, for that one file, are all this
   verb runs; if the open fails, give the path for the developer to open.
3. Report in ONE line: the path, `written_at`, and the `counts` (lanes,
   merged/done, in flight, queued, needs you) with `waiting`.

A project with no work yet gets an empty board that says "No lanes yet": it
is a board, not an error -- open it and report zero lanes. An `error` in the
answer is a failed refresh: say it in one line and, when `path` is not null,
that the previous page is still there to open. Nothing here is written to
SwarmCloud; no other command is run.
