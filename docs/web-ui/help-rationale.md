# Why the console draws what Help describes (#131)

**Status:** implemented. `apps/swarm-ui/src/__tests__/help.rationale.test.tsx`
holds Help's long form free of these sentences and holds every section below
to its topic.
**Relates to:** `apps/swarm-ui/src/help.ts` (the topics), `docs/web-ui/help-density.md`
(the `?` budget), `docs/web-ui/prose-migration-table.md` (where each panel
paragraph went).

---

Help is read by someone looking at a state who wants to know what it means and
what to do about it: what they are seeing, what it means, what to do or where
to look. Issue #131 found the long forms mostly explaining something else —
why the console draws a state the way it does, usually by describing a
hypothetical screen that would have drawn it wrongly.

That reasoning is still worth keeping. It is what stops a later change
quietly reverting a decision: drawing an unread figure as a zero, keeping a
local copy of the catalogue, or summing a partial response. So it moved here,
word for word, under the topic it left. Each topic's long form now states the
fact the reader needs and ends in its action.

The test's pattern finds the shape this rationale took — a screen, panel,
chip, console or button "that drew", "that kept", "that fell through"; "this
UI's defining bug"; "the failure this whole app exists to prevent". It does
not catch a statement of what the console does now ("this app never claims
to"), or a platform rule's own reason ("a console that answered differently
would be an oracle" explains the API's refusal, not a drawing choice). Both
belong in Help.

## `absent-vs-zero`

> They are different facts, and drawing them alike was this UI's defining bug.

The four kinds of figure — measured, never measured, not read, stale — were
once drawn alike, and an unread figure read as a zero. Each now has its own
mark, and the mark carries the meaning, so it survives greyscale.

## `api-reads`

> A 403 on an admin-only route is counted apart from failures, and
> deliberately. … a console that reported that as a fault would be
> reporting itself broken every time a non-admin opened it.

## `checkpoints`

> A screen that drew a file tree here would be inventing it.

Nothing writes a manifest of an archive's contents. Any list of files would be
the console's invention, not a record.

## `all-clear-basis`

> A panel that draws them alike is at its least trustworthy exactly when it
> matters most.

## `read-failed`

> This app exists because of one bug: a failed probe rendered as an absence.

The 56 places the operations sweep found, where a read failure printed as
"nothing to report", are why every figure in the console carries its read's
outcome. Help keeps the example; this is the reason.

## `paused-vs-full`

> That is why the screens draw a paused pool in a treatment of its own rather
> than as a pool at its limit.

A full pool and a paused one both refuse admission, but the action differs:
raising a paused pool's ceiling changes nothing. Drawing them alike would
send an operator to the wrong control.

## `not-a-machine-inventory`

> A screen that drew machines would be inventing them, because nothing in this
> platform publishes them to a console.

## `catalogue-from-route`

> A console that kept its own copy would be correct until the day it mattered.

> The visible cost is that a failed read leaves the screen with nothing to
> show. That is the intended cost: an empty catalogue drawn from a cached copy
> is the failure this whole app exists to prevent.

The catalogue is contract data that gains entries. A fallback list in the
bundle would agree with the platform until the day a runtime was added or
changed, and would then disagree without any mark.

## `runtime-needs-no-provider`

> Neither is drawn as an absence, because an absence on the Runtimes screen
> would read as "we could not find out", and both were found out.

## `clipboard-secure-context`

> The button reports the refusal rather than claiming success, because a
> button that says "copied" when nothing was copied is the small version of
> the bug this whole app is about.

## `withheld-total`

> That is deliberately more annoying than showing a number. A total quietly
> computed over what happened to arrive is the failure this whole app exists
> to prevent.

## `provider-quota-states`

> A chip that fell through to "unknown" for a state it did not recognise would
> mislabel exactly the condition the Provider quota screen exists for, so every
> member is drawn by name.
