# What changed in apps/swarm-ui/src (lane C4F: issues #138, #98, #117; owner rulings 2026-10-07)

Rulings:
- #138: follow design-system §6.12 -- page head is TITLE LEFT, ACTIONS RIGHT, NO SUBTITLE LINE on any Screen. The screen's count moves to a note over its first card. Refresh becomes a quiet control in the head's actions carrying its own ticking read age ('⟳ 12 s'). Admin's two head shapes become one (AH-25: Platform counts' run button is no longer beside the title, it is in the right-hand actions). Overview's lead title ('Needs a look', `.ov-lh h2`) steps down one rank: now --t-body / --lh-body / 600 (was --t-lead).
- #98: each screen's own ticking age lives in that refresh control (one age per screen); a `Screen` (and Overview, and both Timeline pages) claims the page age (`useClaimPageAge(true)`), so the frame head's `.ctl-head-age` is NOT drawn on those routes any more; it is still drawn (inside `.c-acts`) by a PageHead with no age of its own (Help, API reads, Submit chooser, Repositories pages). The dock keeps the tab-wide age. Panels and Overview's tile/card feet state freshness ONLY when stale, worded 'from 6 min ago' (Shell.tsx `staleFoot`: older than AGED_AFTER_MS=5min, or a failed refresh) and are SILENT when fresh. So the Screen empty panel's old `Checked just now.` foot is gone while fresh. Overview's lead count `.ov-cnt` now reads `checks ${ran}/${total} · ${found} found something` (or `N of M checks still reading`).
- #117: cadences: POOLS/HOLDERS/ACCOUNTS/QUOTA_POLL_MS = 30_000 (capacityPoll.ts) with `capacityPoll(ms)` giving CAPACITY_PHONE_POLL_MS=60_000 below the phone breakpoint; TIMELINE_POLL_MS=60_000 (TimelineLanes.tsx; both Timeline pages poll); OVERVIEW_POLL_MS=20_000 (Overview.tsx, was a private POLL_MS). IDLE_STOP_MS = 15 min: a poller stops after 15 min without input and its head control becomes a button 'Paused · resume'.

New DOM of `PageHead` (Shell.tsx):
```
<div class="c-phead">
  <div class="head"><h1 title=…>Title</h1>{? help}</div>
  <div class="c-acts">{frame head age span.ctl-head-age, only on pages without their own age}{children}</div>
</div>
```
There is NO `p.sub`, NO `.c-meta` chip, NO `.c-age`, NO `.c-age-say` any more. `PageHead` lost its `meta` and `action` props.

`Screen` (Shell.tsx) renders `<PageHead>` with ONE child, `RefreshControl`, a `<button class="c-refresh">` (no underline/link styling; quiet). Its text:
- loading: `⟳ reading…` (disabled), aria-label 'Reading'
- read once (no pollMs): `⟳ 12 s`  (age via `spacedAge`: `0 s`, `12 s`, `6 min`, `2 h`, `3 d`, floored)
- polling: `⟳ every 30 s · read 4 s ago`; backing off: `⟳ every 1 min, backing off · read 4 s ago`
- stale (failed refresh) or aged (> 5 min): prefixed `not refreshed · `, e.g. `⟳ not refreshed · read 6 min ago` / `⟳ not refreshed · every 30 s · read 6 min ago`, class `c-refresh is-stale`
- 429: `paused 12 s` (disabled)
- error with no data: `⟳ refresh`, aria-label 'Refresh · not read'. admin_required error: NO control at all.
- idle-stopped: `Paused · resume`
- aria-label for a read: `Refresh · read 12 s ago[ · re-reads every 30 s][ · not refreshed]`; `title` = the visible words without the glyph.
The old words 'Reading…', 'Could not read.', 'Admin only.' (the sub-line) are gone; the FailedPanel / admin-gate panel below are unchanged and still say what they said. The `summary` prop of Screen now renders as `<p class="c-count-note">` (component `CountNote`, title attr = its text) as the FIRST child inside the body wrapper, before `children(data)`, only once data is in (not on empty/error/loading). The old 'Nothing to show' meta for an empty read is gone (the empty panel says it).

Other PageHead callers: Overview: head has only the RefreshControl (`⟳ every 20 s · read N s ago`); the tenant + `8/8 reads` tally (`.ov-tally`) moved into a `.c-count-note` right after the head. Timeline (Lanes and Outcomes): RefreshControl in head with `every 1 min`; range + lane count (`N lanes`) / range + cache in a `.c-count-note` after the TimelinePages tabs. Platform counts: `.c-acts` holds `<span class="counts-prov">` (provenance: 'not counted yet' / 'last run failed' / '1 run · read 4s ago') then the `Run the count · 24 reads` button; nothing beside the h1. Git tokens: scope radio moved into actions; `N tokens` → count note; Permissions page: order chip in actions, `N tokens × M repositories` → count note. Repositories list: meta → count note. RepositoriesDetail: fresh pill moved into actions. Help: the `N on this page · … topics in … groups · showing X` line → count note. API reads (Reference): 'this tab only · not the API surface' → count note.

CSS: `.sub` (16px summary line) rules deleted from styles.css, and every `.sub button` selector removed or replaced by `.c-refresh` (focus ring, 44px hit area on phone, disabled opacity). `.c-refresh` is NOT in the `.ctl-link` list any more (it is quiet, not an underlined accent link). New rules: `.c-phead > .c-acts`, `.c-refresh`, `.c-count-note` in styles.css "17c". `.c-meta`, `.c-age`, `.c-age-say` rules are gone. agents.css: `.app.has-inspector > .work .c-phead > .c-acts { flex: 1 1 0; min-width:0; overflow:hidden }` and `.c-acts > .c-refresh { flex: 0 1 auto }`. overview.css phone: `.ov-page > .c-phead > .c-acts`. intake.css: `.rn-run .c-phead > .c-acts`. admin.css: `.c-phead .counts-prov`.
