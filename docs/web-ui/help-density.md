# The `?` is rationed — B7.4

**Status:** implemented. The count below is enforced by `apps/swarm-ui/tests/help.test.ts`.
**Measured:** 2026-09-24, at `25f8bd0`.
**Relates to:** `docs/web-ui/ui-audit-and-build-prompt.md` §B7 (the `?` and the card
behind it), `docs/web-ui/prose-migration-table.md` (where each paragraph went).

---

## 1. The finding

Counted by reading `apps/swarm-ui/src/*.tsx` for every anchor that draws a help
glyph:

```
grep -oE '(topic|help)="[a-z0-9-]+"' apps/swarm-ui/src/*.tsx | wc -l     # 139
```

**139 help anchors in the source.** Opening every route and counting what is on
screen at once gives **82 `?` widgets across the fifteen routes**, nineteen of
them on Runtimes and fifty-four authored in `Accounts.tsx` alone — more than a
third of the console's total, on one route.

The two numbers differ because a screen's branches are mutually exclusive: the
account sign-in flow authors about thirty anchors and draws a handful of them at
a time. Both figures are in this document because they answer different
questions. 139 is what a person maintaining the code meets; 82 is what a reader
meets.

A console that needs eighty-two explanatory popovers is not a well-documented
console. It is a console whose labels are not carrying their weight. Every one
of those glyphs is something a reader has to notice, hover and read in order to
learn what the layout could have said outright — and several of them sat on a
column head that already printed the unit they were explaining.

## 2. Why this is a design problem and not a documentation one

The prose migration (§B7) was right that a paragraph under a table is worse than
a topic behind a `?`: a paragraph can be scrolled away from the figures it
qualifies, and a reader who lands on row nine has the caveat nowhere in view. It
moved several hundred words off the glass and the screens got better.

What it did not have was a budget. So each pass replaced paragraphs with glyphs,
and then new glyphs arrived one at a time in unrelated diffs — because a single
extra `?` is never worth objecting to, and nothing was counting. Eighty-two is
what that produces over four passes.

The failure is specific, and it is not aesthetic:

* **A rationed affordance is learnable; an ubiquitous one is not.** A reader who
  meets a `?` on every third element stops reading them, exactly as they stop
  reading a banner that is always there.
* **A glyph that repeats its neighbour costs more than it gives.** Three of
  Runtimes' ten sat on `In use (units)`, `Workspace (of memory)` and
  `ws of mem` — column names that already state the entire claim, in the one
  place a reader cannot scroll past.
* **The popover is the worst medium for what several of them held.** Most of the
  deleted ones sat beside a `.ctl-mark` whose own `aria-label` is a longer, more
  specific sentence than the topic — with *this* response's numbers and *this*
  failure's message in it, which a shared topic cannot have.

## 3. The rule

An explanation goes to the first of these that will hold it, and only reaches
the last if the first three genuinely will not:

1. **The label.** "Capacity holders" became "Holders" the moment its section was
   named Capacity; the same move was available nearly everywhere.
2. **The column head**, when what is being explained is a unit or a basis.
   `In use (units)`, `Backend (resolved)`, `Weight (units)`,
   `Could start (min across pools)`. A parenthetical in a column name cannot be
   separated from the figures under it and cannot be read as qualifying the
   column next door.
3. **The screen's footer index** — `<HelpLinks>`, or `.ctl-card-foot`. A
   platform concept (the 5-hour and 7-day quota windows, absent vs zero,
   all-or-nothing reservation, what a dispatch strategy publishes) is **one
   topic with one destination**, `#help/<id>`, linked once per screen rather
   than pinned to every figure that happens to obey it.
4. **A `?`.** At most one per screen, on the screen's own subject, in the same
   place every time so it is learnable rather than hunted for.

### What may not be traded away

This console draws **three marks** and they mean different things:

| mark | means |
|---|---|
| a digit | a measured value |
| `~12%` | a real reading, too old to trust |
| `—` | nobody measured this |

**No label may be shortened in a way that lets "nobody measured this" read as
"this is zero."** Where a shorter label would blur that, the `?` stays and the
label does not change. Two of the sixteen surviving glyphs are there for exactly
this reason: `projected-not-measured` on the accounts table's tilde, and
`absent-vs-zero` on the Runtimes unread-counters mark.

### The silent form

Deleting a `?` from a label takes something away from one reader and nobody
else. The sighted reader keeps the mark, the unit in the column head and the
footer link; the screen-reader user loses the sentence the label was publishing
through `aria-describedby`.

So the button goes and the description stays. `<HelpNote>`
(`apps/swarm-ui/src/HelpCard.tsx`) renders the topic's short form into a
visually hidden node at the id the label points at, and draws nothing. It is the
same node `<HelpCard>` renders, from one component, so the attribute every prose
gate strips by (`data-help-description`) is written in one place.

In the screens this reads as an `explain="<topic>"` prop where a `help="<topic>"`
prop used to be — `CardHead` and `Absent` on Overview, `Metric` and `Absent` on
the agent detail.

## 4. The result

16 glyph anchors, from 139. At most two in any one file, and a maximum of two
drawn at once on any route.

| screen file | before | after |
|---|---:|---:|
| `Accounts.tsx` | 54 | 2 |
| `AgentDetail.tsx` | 22 | 1 |
| `Overview.tsx` | 12 | 1 |
| `Runtimes.tsx` | 10 | 2 |
| `PlatformCounts.tsx` | 7 | 1 |
| `Submit.tsx` | 7 | 1 |
| `Dispatch.tsx` | 5 | 0 |
| `Workflows.tsx` | 5 | 1 |
| `Holders.tsx` | 4 | 1 |
| `Capacity.tsx` | 3 | 1 |
| `AdminSettings.tsx` | 2 | 1 |
| `Agents.tsx` | 2 | 1 |
| `AttemptTimeline.tsx` | 2 | 1 |
| `SubmitWorkflow.tsx` | 2 | 1 |
| `ArtifactViewer.tsx` | 1 | 1 |
| `Blockers.tsx` | 1 | 0 |
| **total** | **139** | **16** |

`Dispatch.tsx` and `Blockers.tsx` go to zero because neither is a screen: both
are drawn inside screens that keep one of their own, so their five and one
glyphs were arriving on three routes and two routes respectively.

### Reproducing the count

Before, over any commit at or before `25f8bd0`:

```sh
grep -oE '(topic|help)="[a-z0-9-]+"' apps/swarm-ui/src/*.tsx | wc -l
```

After, the same measurement, with comments stripped so that prose *about* a `?`
is not counted as one, and with `HelpCard.tsx` excluded because it is the
renderer rather than a screen. That is what
`apps/swarm-ui/tests/help.test.ts` does — `glyphAnchors()` — and it prints the
per-file breakdown before it asserts, so a run that fails says what to put.

It does not count `explain="..."`, because `<HelpNote>` draws nothing. That
exclusion is not assumed: the same file renders a `<HelpNote>` and asserts it
emits no `<button>` and no `aria-expanded`, so the day it grows a widget the
count stops being wrong quietly.

## 5. The ceilings, and what raising one costs

```
total        ≤ 20     (measured 16)
per screen   ≤ 2      (Accounts and Runtimes are the two at 2)
total        ≥ 10     a floor, so deleting every explanation does not pass
```

**The per-file ceiling is the one that bites**, and it is deliberately the
tighter of the two: the total could absorb one new glyph without moving, and two
per screen cannot. Runtimes is the worked example — it had ten, it has two, and
putting back the one that sat on `In use (units)` takes it to three and fails by
name. That is the mutation the assertion exists to catch, and it is the exact
mutation that produced the eighty-two.

Raising either number is a decision, not an outcome. Anyone raising one writes
in the diff which of the four routes in §3 the explanation could have taken
instead, and why none of them would hold it.

## 6. The two implementations of the help card

There are two, and this pass did not add a third.

* `HelpCard.tsx` — `<HelpCard>`, the `?` and the card behind it.
* `SectionQuestion` in `App.tsx` — the `?` in the frame head that holds the
  section's question.

They were separate implementations of one widget and the second missed every fix
the first received, most recently the edge-safe placement: at 390px all four of
`SectionQuestion`'s cards opened 299px past the right edge. `287c8b8` fixed that
by having `SectionQuestion` import `useHelpDisclosure` and `useEdgeSafePlacement`
rather than reimplement them, so the behaviour and the placement are now one
implementation with two renderings.

B7.4 changed neither. `<HelpNote>` is not a third implementation: it renders no
card, has no state machine and imports nothing from the disclosure. It renders
the hidden description node — and `HelpCardView` now renders the same component
rather than its own copy of that markup, so that node has one spelling.

`SectionQuestion` is outside the ration above. It is one glyph, in the frame,
identical on every route, and it is what the §3 rule asks a screen's own glyph to
be: the same affordance in the same place every time.
