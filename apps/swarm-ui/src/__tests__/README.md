# The component test layer, and what it replaced

Run with `npm test` from `apps/swarm-ui`, or `make ui-component-test` from the
repository root. `make test` runs it. CI runs it as the `ui` job in
`.github/workflows/application.yml`.

Offline by construction: `setup.ts` replaces the global `fetch` with one that
throws, so a test that forgets to stub a response fails by name instead of
reaching a machine. No credentials, no emulator, no network at test time.
`npm ci` reaches the npm registry the first time, exactly as `uv run` reaches
PyPI the first time `make test` is run on a fresh clone.

## Why this exists

20,445 lines of TypeScript had no test runner. What covered the UI instead was
a set of Python files under `tests/unit/control_plane/` that read `.tsx` files
**as text** and assert on source strings. A source grep:

* cannot catch a render error;
* cannot catch a runtime exception;
* passes when the string it looks for appears in a **comment** — and these
  components are written in this repository's house style, which means the copy
  a grep searches for is usually also quoted in the explanation above it;
* cannot tell two cases apart when both of their literals are in the file. The
  em-dash rule is exactly that: `—` and `0` are both in `Blockers.tsx` and
  `Capacity.tsx` whichever branch actually runs.

That last one was demonstrated rather than argued. Changing one JSX expression
in `Capacity.tsx` from `{figure.text}` to `{h.agents ?? 0}` makes an unmeasured
headroom render as `0` — the exact bug this product exists to prevent — while
`grep 'h.agents === null'`, `grep '—'` and `grep uncapped` all still match and
every Python UI-surface suite still passes. The component test fails with
`expected '0' to be '—'`.

## What moved, and to what

| Removed from Python | Replaced by |
|---|---|
| `test_blocker_ui_surface.py::test_the_capacity_board_renders_every_blocker_not_the_binding_one` | `honesty.capacity.test.tsx` — "names EVERY refusing pool"; counts rendered chips |
| `…::test_a_paused_blocker_is_drawn_differently_from_a_full_one` | `honesty.capacity.test.tsx` — "draws a paused pool differently from a full one" |
| `…::test_an_incomplete_list_says_so` | `honesty.capacity.test.tsx` — "an incomplete read" (4 cases), including that an empty blocker list under a partial read is **not** reported as "no pool is refusing this profile" |
| `…::test_a_missing_headroom_renders_an_em_dash_and_never_a_zero` | `honesty.capacity.test.tsx` — the em dash **and** the measured `0`; one without the other keeps the rule by accident |
| `…::test_the_counterfactual_is_worded_as_a_snapshot_and_not_as_a_promise` | `honesty.capacity.test.tsx` — past conditional, plus the rendered `<time dateTime>` |
| `…::test_a_zero_delta_names_what_still_binds` | `honesty.capacity.test.tsx` — "a zero delta names what still binds" |
| `test_dispatch_ui_surface.py::test_an_api_that_reports_no_dispatch_is_not_rendered_as_collect` | `dispatch.test.ts` — calls `dispatchOf` with a missing block, null, an array, a scalar, the worker's `patches` spelling and an unknown strategy |
| `…::test_the_default_option_never_promises_a_pull_request` | `dispatch.test.ts` — calls `consequenceOf` across every strategy and 0/1/2/7 steps |

## What stays in Python, and why it is not a grep in disguise

These are checks on the **source's declarations or its absences**. No
behavioural test can make them, because a component that does the wrong thing
correctly produces identical pixels.

* `test_blocker_ui_surface.py` §1 — `headroomFor` contains no `Infinity`,
  `Math.floor`, `.available` or `effective_limit`. This asserts that the client
  does not **re-derive** admission. A re-derivation that happens to agree with
  the server renders exactly the same DOM; only the source shows it. This is
  the property whose violation collapsed a list of blockers to one pool.
* `test_blocker_ui_surface.py` §3 and `test_runtimes_screen.py`'s fixture
  tests — the development fixture is byte-for-byte what `analyse_profile` and
  the frozen catalogue produce. A Python-side comparison; it could not move.
* `test_account_api.py::test_every_path_the_ui_calls_exists_on_this_api` and
  `test_runtimes_screen.py::test_every_v1_path_the_ui_calls_is_served_by_this_api`
  — the route-level seam. Legitimate static checks, explicitly kept.
  `tests/unit/control_plane/test_ui_api_field_contract.py` is the FIELD-level
  version of the same idea and follows their shape.
* `test_runtimes_screen.py::test_the_screen_restates_no_value_from_the_frozen_catalogue`
  and `::test_the_screen_never_coalesces_a_failed_read_to_a_number` — both
  assert an **absence** (`?? 0` must not appear; no frozen name may be
  special-cased). A render test can only sample inputs; it cannot show that no
  input reaches a coalescing branch that is not there.
* `test_runtimes_screen.py::test_the_runtimes_route_is_read_by_a_screen_the_router_reaches`
  and `::test_the_screen_is_reachable_by_a_hash_that_resolves_to_it` — wiring,
  across three files. A render test of a screen nothing routes to passes.
* `test_dispatch_ui_surface.py` §1–2 and the fixture section — the submit forms'
  vocabulary and request bodies, asserted against the real `swarm-api`. Seam
  tests, not render claims.

## The files

| File | What it holds |
|---|---|
| `setup.ts` | The offline guard, RTL cleanup, and `expectNoFigures` |
| `fetch.test.ts` | `Result` — 14 failure paths × (no data / data in hand), the probe registry, `isPaused`, `num`, writes |
| `types.test.ts` | `headroomFor`, `poolKind`, `poolLabel`, `poolScope`, `setBy`, `limitedBy`, `overCeiling`, `reasonCopy`, `timeAgo` |
| `dispatch.test.ts` | `dispatchOf`, `needsRepository`, `consequenceOf`, the defaults and the carrier note |
| `honesty.screen.test.tsx` | `Screen` across loading / ok / empty / stale / error / admin_required |
| `honesty.capacity.test.tsx` | The em-dash rule, the full blocker list, partial reads, counterfactual wording |
| `honesty.counts.test.tsx` | No total over a partial response, both directions |
| `keyboard.ts` | The keyboard probe: what a tab stop is, which sheet rules promise a control, which draw a focus ring |
| `keyboard.test.tsx` | The keyboard sweep — every route's tab stops pressed, every `?` opened and dismissed, the drawer's trap and the help card's tab bridge |

## The two sweeps, and why neither can replace the other

`spacing.test.tsx` and `keyboard.test.tsx` both render all fifteen routes and
both hold a floor on what they examined, for the same reason: this repository
keeps producing checks that run over nothing and report zero problems. They
measure different things and have different blind spots, and both blind spots
are written down rather than implied.

| | `spacing.test.tsx` | `keyboard.test.tsx` |
|---|---|---|
| reads | the computed box of every shape with geometry | every tab stop, and the sheet's own cursor and focus rules |
| floor | `> 800` shapes examined (1,295 measured) | `> 480` tab stops (576 measured), `> 60` help cards opened (84 measured), every route ≥ 20 |
| cannot see | overlap — jsdom has no layout engine | a React handler, layout, or Tab actually moving focus |

The keyboard sweep's blind spot needs the most care when reading a green run.
jsdom implements no sequential focus navigation, so a Tab keydown moves
nothing. What the sweep measures instead is whether a handler **swallowed**
that keydown — in this app the only way to change where Tab goes — and it holds
the set of elements that do to a named list. "Tab order follows VISUAL order"
is therefore NOT asserted anywhere; what is asserted is the two ways this app
can break it, a positive `tabindex` and a portal. Overlap, reading order within
a row, and `order` on a flex child need a real browser.
