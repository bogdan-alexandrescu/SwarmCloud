import { loadCapacity } from './api'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpCard, HelpLinks } from './HelpCard'
import { POOLS_POLL_MS } from './capacityPoll'
import { UtilTrack } from './primitives'
import { Screen } from './Shell'
import { blockerVerdict, classUnits, headroomFigure, useResourceClasses, verdictCopy, verdictNeedsAPerson } from './Blockers'
import {
  FAMILY_TITLE,
  POOL_FAMILY_ORDER,
  blockerCeiling,
  ceilingCopy,
  headroomFor,
  needsAPerson,
  overCeiling,
  poolKind,
  poolLabel,
  poolLabelAmong,
  poolScope,
  setBy,
  type Capacity,
  type Headroom,
  type Pool,
} from './types'

/**
 * Screen B -- the capacity board.
 *
 * Where an operator goes when something is not being admitted and they need to
 * know which ceiling is the binding one.
 *
 * NOTE ON THE SPEC: docs/web-ui/02-cluster-state.md defines this screen in
 * §4.1 and then stops -- the source text is cut off mid-sentence and §§4.2-9
 * were never written. So the grouping order and the scope declarations below
 * come from the spec; the columns come from the five data traps in §1.1 and
 * from what pool_to_api actually sends. Anything beyond that is not sourced
 * and is not pretended to be.
 *
 * ---------------------------------------------------------------------------
 * B4.4: WHERE THE THREE PARAGRAPHS WENT.
 *
 * This screen carried ~85 words of explanation above and below its figures.
 * All three claims survive; none of them is a sentence any more.
 *
 *   THE CONJUNCTION -- "a task must clear EVERY pool at once; capacity is the
 *   MINIMUM across them, never a sum" -- is now the COLUMN NAME:
 *   `Could start (min across pools)`. §8.4(3). This is strictly stronger than
 *   the paragraph was. The paragraph sat above a table and had to be carried
 *   back down to the column it was about; a reader who scrolled past it read
 *   the figures with no qualifier at all. The parenthetical cannot be
 *   separated from the number it qualifies, and cannot be applied to the wrong
 *   column.
 *
 *   THE SCOPE -- "these are YOUR tenant's pools, not the platform's" -- is the
 *   `.ctl-card-note` on the Headroom panel, and a `Scope` column on every row
 *   of every family, which is where two figures of different scope could
 *   otherwise be compared (Trap E). The families used to carry one note each,
 *   read off their FIRST row, so an admin's Tenants family said "this tenant"
 *   above four tenants' pools (CP-2, #85).
 *
 *   THE EM-DASH RULE -- "an em dash is not a zero: it means the number was not
 *   measured" -- is `.ctl-mark`, in the row that has one. `is-unread` when a
 *   required pool could not be read, and an `uncapped` fact chip when nothing
 *   caps the profile at all. Those two were one sentence and are two different
 *   situations; the marks tell them apart where the sentence could not.
 *
 * The arguments are at `#help/pools-all-at-once`, `#help/tenant-scope` and
 * `#help/absent-vs-zero`, which is where they were already written down.
 */
export function CapacityScreen() {
  return (
    <Screen
      // "Pools", not "Capacity". The tab that leads here says Pools, the
      // section says Pools, the table below is a list of pools -- and a
      // heading reading "Capacity" made the one screen look like two places,
      // so a runbook step saying "go to Pools" named nothing on the screen it
      // landed you on. The nav's own rule (App.tsx: sections are named after
      // OBJECTS, not the question they answer) decides which side gives way.
      title="Pools"
      load={loadCapacity}
      // Decided 2026-10-01 (#117): Pools re-reads every 30s, and `Screen`
      // pauses the timer while the tab is hidden.
      pollMs={POOLS_POLL_MS}
      summary={(d) => {
        const paused = d.pools.filter(isPaused).length
        const over = d.pools.filter(overCeiling).length
        return (
          <>
            {d.pools.length} pools
            {paused > 0 && ` · ${paused} paused`}
            {over > 0 && ` · ${over} over ceiling`}
          </>
        )
      }}
      /* A REAL ZERO. Pools are created at provisioning time, so an environment
         with none has not been fully applied -- which is an absence the mark
         names in two words instead of two clauses.

         THE MARK IS `Screen`'s, drawn in the heading by the shared empty state
         (#145). A hand-drawn `.ctl-mark is-zero` span here said `real zero` a
         second time, with no sentence behind it. */
      empty={{
        heading: 'No pools exist',
        body: <>none provisioned in this environment</>,
      }}
    >
      {(d) => (
        <>
          <Headroom capacity={d} />

          {/* CEILINGS, AS DECIDED ON 2026-10-01 (capacity.html, Variant 1, #125):
              one table per family, with a "Needs a look" group ahead of them
              holding every pool that is full, over its ceiling, at limit 0,
              paused or lowered below its configured limit. A pool appears
              once: the top group takes it out of its family. The Cards view
              was the alternative set aside and is gone. */}
          <CeilingTables pools={d.pools} viewer={viewerOf(d)} />

          <HelpLinks topics={CAPACITY_TOPICS} />
        </>
      )}
    </Screen>
  )
}

/**
 * Per-runner-profile headroom: how many more agents of each kind could start
 * right now, and which pool is the one stopping more.
 *
 * The number is the minimum across the profile's pools divided by its weight,
 * because a task must clear all of them at once and each is incremented by
 * `units` rather than by one. THAT FACT IS THE COLUMN NAME, not a footnote.
 *
 * The pool list comes from pool_names_for for the CALLING tenant -- including
 * for an admin -- so an admin reading these as the platform's capacity is the
 * single most plausible misreading here. A platform-wide figure does not exist
 * today and is not faked by substituting another tenant's pools. The card's
 * note says whose figures these are, on every render.
 */
/**
 * The tenant this read is scoped to: whose pool a row's `this tenant` means.
 *
 * `/v1/capacity` names the tenant itself -- service.capacity() has always
 * sent `tenant_id`, and this file used to guess it back out of the pool
 * names. The regex stays as the fallback for an API that predates the
 * field, because a blank here silently drops the scope from every figure.
 */
function viewerOf(capacity: Capacity): string | undefined {
  return (
    capacity.tenant_id ??
    capacity.pools
      .map((p) => /(?:^|:)tenant:([^:]+)/.exec(p.name)?.[1])
      .find((t): t is string => Boolean(t))
  )
}

/**
 * A pool's Scope cell, in words (CP-2, #85): `platform`, `this tenant`, or
 * `tenant X` for another tenant's pool.
 *
 * PER ROW, BECAUSE A FAMILY IS NOT ONE SCOPE. An admin sees every tenant's
 * pools (service.py filters them for non-admins only), so the Tenants family
 * holds several tenants and the Providers family holds both the shared
 * `provider:X` pool and every tenant's `provider:X:tenant:Y` slice. The note
 * each family used to carry was read off its first row and was wrong for the
 * rest. The owner's decision is this column, and the family note is gone.
 */
function scopeWord(name: string, viewer: string | undefined): string {
  if (poolScope(name) === 'platform') return 'platform'
  // `tenant:Y` or `provider:X:tenant:Y`: the tenant is the last segment.
  const owner = /(?:^|:)tenant:([^:]+)$/.exec(name)?.[1]
  if (owner === undefined || owner === viewer) return 'this tenant'
  return `tenant ${owner}`
}

function Headroom({ capacity }: { capacity: Capacity }) {
  // One task's weight per class, from `/v1/resource-classes` (#66), so a pool
  // too small for one task is not drawn as a full one. Null until it answers.
  // Above the early return: a hook is called on every render or on none.
  const classes = useResourceClasses()
  const profiles = Object.entries(capacity.runner_profiles)
  if (profiles.length === 0) return null

  const tenant = viewerOf(capacity)

  const rows = profiles
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, profile]) => ({ name, profile, h: headroomFor(profile) }))

  const unmeasured = rows.filter((r) => r.h.agents === null).length

  return (
    /* §B6.1: one box, and it is the table's. Headroom was the only panel on
       this screen inside a card -- the six family tables below it are bare
       `.ctl-table`s with their heading on the page -- so the same kind of
       content was drawn two ways on one screen. */
    <section className="section cap-headroom">
      <div className="ctl-toolbar">
        {/* NOT `tenant-scope` ON THE HEADING (B7.4). That topic was here to say
            these figures are the CALLING tenant's and not the platform's -- and
            the card already declares that on its own right-hand note, in words,
            on every render, which is the note the comment below this one is
            about. The topic is in the footer index.

            THE `?` THAT IS HERE IS THE SCREEN'S ONE IN-CONTENT GLYPH, and it
            MOVED HERE FROM THE `Could start` HEADER CELL (CP-11, visual QA
            2026-09-25). `(min across pools)` says WHAT the arithmetic is; it
            does not say that the reservation is all-or-nothing in a single
            transaction, which is invariant 2 and the reason the answer is a
            minimum rather than a sum. That is a platform rule a column name
            cannot carry, so it keeps its glyph -- but in the header cell it
            sat inside the `<thead>` that §B6.3 hides below 900px, so on a
            phone it was clipped away while staying in the tab order, focusable
            and invisible. The panel head is not hidden at any width.
            `honesty.capacity.test.tsx` pins it outside the table. */}
        <h2 className="ctl-card-title">
          Headroom
          <HelpCard topic="pools-all-at-once" />
        </h2>
        {/* TRAP D, AS AN ATTRIBUTE OF THE CARD. Every figure in this card is
            this tenant's; the note is what stops an admin reading them as the
            platform's. It is one line, mono and muted -- chrome, not copy. */}
        <span className="ctl-card-note is-end">
          {tenant ? `tenant ${tenant}` : 'your tenant'}
        </span>
      </div>

      <div className="ctl-table is-scroll">
        <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Runner profile</th>
                {/* THE CONJUNCTION LIVES HERE NOW. A task must clear every
                    pool in its list at the same moment, so the figure is the
                    minimum across them and never a sum -- and saying that in
                    the column name attaches it to the number it governs. The
                    stacked key below repeats the name IN FULL for the same
                    reason: a phone hides this header, and a bare `Could start`
                    there read the figure with no qualifier at all (CP-11). */}
                <th role="columnheader" scope="col" className="is-num">
                  {COULD_START}
                </th>
                {/* `(units)`, as every other column on this screen that counts
                    them says it; the cells keep the app-wide `2u` (CP-24). */}
                <th role="columnheader" scope="col" className="is-num">{WEIGHT}</th>
                {/* Plural on purpose. A task must clear EVERY pool at once, so
                    more than one can refuse at the same moment -- and while this
                    column named a single one, an operator would raise it and
                    nothing would move. */}
                <th role="columnheader" scope="col">Held back by</th>
                <th role="columnheader" scope="col">Backend</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map(({ name, profile, h }) => {
                const figure = headroomFigure(h)
                // A PROFILE THE PLATFORM REFUSES HAS NO HEADROOM TO OFFER,
                // whatever its pools say (CP-3). `/v1/capacity` did not send
                // `available` until visual QA 2026-09-25 found `codex` -- off
                // since its provider refused the tenant's credential -- drawn
                // here with 10 that could start. The pools are still true; the
                // figure is not an offer, so it is not drawn as one. Absent
                // means available: an older API does not say, and the submit
                // gate refuses a disabled profile regardless.
                const off = profile.available === false
                const reason = profile.disabled_reason || 'refused by the platform'
                return (
                  <tr
                    role="row"
                    key={name}
                    className={
                      off
                        ? undefined
                        : h.agents === 0
                          ? 'over'
                          : h.agents === null
                            ? 'unmeasured'
                            : undefined
                    }
                  >
                    <th role="rowheader" scope="row">{name}</th>
                    {/* THE CELL IS THE FIGURE AND NOTHING ELSE, so an absent
                        one is an em dash with no digit anywhere in it. WHICH
                        KIND of absence it is is drawn in `Held back by`, where
                        there is room for a mark; the sentence is the cell's
                        accessible name, which a `title=` was not. */}
                    <td
                      role="cell"
                      data-label={COULD_START}
                      className="is-num"
                      aria-label={off ? `Disabled: ${reason}` : figure.title}
                    >
                      {off ? (
                        <span className="ctl-chip is-bad">
                          <i aria-hidden="true" />
                          disabled
                        </span>
                      ) : figure.text === '—' ? (
                        <span className="ctl-em">—</span>
                      ) : (
                        figure.text
                      )}
                    </td>
                    <td role="cell" data-label={WEIGHT} className="is-num">{profile.units}u</td>
                    <td role="cell" data-label="Held back by">
                      {/* The reason is what holds a disabled profile back, and
                          the only part of it a reader can act on -- so it is
                          text on the row, not a tooltip. */}
                      {off ? <span>{reason}</span> : <HeldBackBy h={h} units={classUnits(classes, profile.resource_class)} />}
                    </td>
                    <td role="cell" data-label="Backend">{profile.backend}</td>
                  </tr>
                )
              })}
            </tbody>
          {/* §8.4(4): PROVENANCE, ONCE, FOR THE WHOLE PANEL. How many of these
              figures are measurements is a property of the read, not of each
              row, so it is said here rather than repeated per cell. It is a
              `<caption>` rather than a `.ctl-card-foot` now (§B6.1): the card
              it was the foot of is gone, and a caption is the slot the one
              box that is left already has for exactly this. */}
          <caption>
            {rows.length - unmeasured} of {rows.length} measured
            {unmeasured > 0 && ` · ${unmeasured} not counted`}
          </caption>
        </table>
      </div>
    </section>
  )
}

/**
 * EVERY pool refusing this profile, not the tightest one.
 *
 * The bug this replaces: `headroomFor` kept a running minimum and overwrote
 * `binding` on each new one, so with `resource:large` and `provider:anthropic`
 * both at their ceilings the cell named one of them. An operator raised it,
 * nothing changed, and the pool still refusing never appeared anywhere.
 *
 * A pause and a full pool are drawn differently because the remedies are
 * opposite -- resume it, versus wait or raise it -- and a paused pool can read
 * 0 of 8 units in use while admitting nothing at all, which is the case that
 * looks healthiest and is not.
 *
 * THE TWO ABSENCES ARE TWO MARKS. `not read` (dashed, warning-toned) is a
 * failure that is ours; `uncapped` is a measured fact about the platform and
 * is drawn as a fact, in `--info`, never as a verdict. One sentence used to
 * carry both and could not tell them apart.
 */
function HeldBackBy({ h, units }: { h: Headroom; units: number | null }) {
  const marks = (
    <>
      {!h.complete && (
        <span
          className="ctl-mark is-unread"
          title={`${h.unread.length} of this profile's pools could not be read.`}
        >
          not read
        </span>
      )}
      {h.missing.length > 0 && (
        <span className="ctl-chip is-info" title="No pool of this name is configured, so nothing caps it.">
          <i aria-hidden="true" />
          uncapped
        </span>
      )}
    </>
  )

  if (h.blockers.length === 0) {
    return (
      <span className="cap-marks">
        {/* A WORD, NOT THE DASH (CP-1). The read was complete and no pool is
            refusing: that is a measured fact, and the em dash is this screen's
            one mark for a figure nobody measured. It sat on every healthy row
            directly above `5 of 5 measured`, contradicting the caption. */}
        {h.complete && h.missing.length === 0 && 'none'}
        {marks}
      </span>
    )
  }
  // Named against each other, so two refusing pools never share a chip's name
  // (CP-15): `resource:browser` and `runner:browser` both printed `browser`.
  const among = h.blockers.map((b) => b.pool)
  const label = (pool: string) => poolLabelAmong(pool, among)
  return (
    <span className="cap-marks">
      {h.blockers.map((b) => {
        // A pool at ZERO refuses with the full pool's reason, and `0/0` in
        // the full pool's red reads as "all of nothing is in use" -- the
        // chip form of the live "busy platform-wide. (0/0)". The ceiling,
        // not the reason, tells them apart; see `blockerCeiling`. So does a
        // pool whose limit is below one task's weight (#66): `0/1` in red
        // for a browser task no wait will admit is the same misreading.
        const ceiling = blockerVerdict(b, units)
        return (
          <span
            key={b.pool}
            className={`ctl-chip ${verdictNeedsAPerson(ceiling) ? 'is-paused' : 'is-bad'}`}
            title={`${b.pool} — ${b.reason}, ${verdictCopy(b, units) ?? `${b.active} of ${b.limit} units in use`}`}
          >
            <i aria-hidden="true" />
            {label(b.pool)}
            {ceiling === 'paused'
              ? ' paused'
              : ceiling === 'full'
                ? ` ${b.active}/${b.limit}`
                : ceiling === 'below-units' || ceiling === 'below-units-quota'
                  ? ` limit ${b.limit} < ${units}u`
                  : ' limit 0'}
          </span>
        )
      })}
      {marks}
    </span>
  )
}

/**
 * True when a pool belongs in "Needs a look": anything the classification
 * marks (paused, over ceiling, limit 0, full), or a ceiling lowered below its
 * configured limit by AIMD or by provider quota (#125).
 */
export function needsALook(pool: Pool): boolean {
  return classifyPool(pool).row !== undefined || pool.effective_limit < pool.hard_limit
}

/**
 * The Ceilings tab: "Needs a look" first, then one table per family, each
 * holding only the pools the top group did not take.
 */
function CeilingTables({ pools, viewer }: { pools: Pool[]; viewer: string | undefined }) {
  const byName = (a: Pool, b: Pool) => a.name.localeCompare(b.name)
  const look = pools.filter(needsALook).sort(byName)
  const rest = pools.filter((p) => !needsALook(p))
  return (
    <div className="cap-families">
      {look.length > 0 && (
        <Family title={`Needs a look · ${look.length}`} className="cap-needs" pools={look} viewer={viewer} />
      )}
      {POOL_FAMILY_ORDER.map((kind) => {
        const family = rest.filter((p) => poolKind(p.name) === kind).sort(byName)
        if (family.length === 0) return null
        return <Family key={kind} title={FAMILY_TITLE[kind]} pools={family} viewer={viewer} />
      })}
    </div>
  )
}

function Family({
  title,
  className,
  pools,
  viewer,
}: {
  title: string
  className?: string
  pools: Pool[]
  /** The tenant this read is scoped to, for the rows' `this tenant`. */
  viewer: string | undefined
}) {
  // Trap E: a number may only sit beside another number of the same scope, so
  // the scope is declared rather than left to be inferred -- ON EVERY ROW
  // (CP-2, #85). This head used to carry one `.ctl-card-note` computed from
  // the family's first row, and a family is not one scope: Tenants holds
  // every tenant's pool for an admin, and Providers holds the shared pool
  // beside per-tenant slices. The note is removed, as the owner decided.
  return (
    <section className={className === undefined ? 'ctl-card' : `ctl-card ${className}`}>
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{title}</h2>
      </div>
      <div className="ctl-card-body is-flush">
        <PoolTable pools={pools} viewer={viewer} />
      </div>
    </section>
  )
}

const LEASED = 'Leased (units)'
const CEILING = 'Ceiling (units)'

function PoolTable({ pools, viewer }: { pools: Pool[]; viewer: string | undefined }) {
  return (
    <div className="ctl-table is-scroll">
      <table role="table">
        <thead role="rowgroup">
          <tr role="row">
            <th role="columnheader" scope="col">Pool</th>
            {/* THE SCOPE, PER ROW (CP-2). Beside the name it qualifies, and
                ahead of the figures, because it says which other figures on
                this screen a row's numbers may be compared with. */}
            <th role="columnheader" scope="col">Scope</th>
            {/* "units", never "agents". Trap A: admission increments by the
                resource class's units (1, 2 or 4), so 8 leased may be two
                large agents or eight standard ones. The caveat is in the
                column name, where it cannot be scrolled away from the
                figures it governs. "Leased" because concurrency counts from
                LEASED (invariant 3), not from RUNNING. */}
            <th role="columnheader" scope="col" className="is-num">{LEASED}</th>
            {/* THE CEILING IS UNITS TOO, and it says so (CP-24). */}
            <th role="columnheader" scope="col" className="is-num">{CEILING}</th>
            <th role="columnheader" scope="col">Use</th>
            <th role="columnheader" scope="col">State</th>
            <th role="columnheader" scope="col">Set by</th>
            <th role="columnheader" scope="col" aria-label="Links" />
          </tr>
        </thead>
        <tbody role="rowgroup">
          {pools.map((p) => (
            <PoolRow key={p.name} pool={p} scope={scopeWord(p.name, viewer)} />
          ))}
        </tbody>
      </table>
    </div>
  )
}

function PoolRow({ pool, scope }: { pool: Pool; scope: string }) {
  const marks = classifyPool(pool)
  const by = setBy(pool)
  const limit = pool.effective_limit
  // UNROUNDED for the track: 1 unit of 300 is 0.3%, and rounding it to 0
  // would hand the track a measured zero for a pool that holds something. A
  // ceiling of 0 leaves no room at all, so the track is full in the
  // classification's tone (#159 review of CP-14), never the not-measured hatch.
  const pct = limit > 0 ? Math.min(100, (pool.active / limit) * 100) : 100
  const shown = limit > 0 ? `${Math.round((pool.active / limit) * 100)}%` : 'no room'

  return (
    <tr role="row" className={marks.row}>
      <th role="rowheader" scope="row" title={pool.name}>
        {poolLabel(pool.name)}
        <span className="ctl-sub">{pool.name}</span>
      </th>
      <td role="cell" data-label="Scope">{scope}</td>
      <td role="cell" data-label={LEASED} className="is-num">{pool.active}</td>
      <td role="cell" data-label={CEILING} className="is-num">
        {limit}
        {limit < pool.hard_limit && (
          <span className="cap-was" title={`Configured hard limit is ${pool.hard_limit}`}>
            /{pool.hard_limit}
          </span>
        )}
      </td>
      <td role="cell" data-label="Use">
        <span className="cap-use">
          <UtilTrack
            pct={pct}
            tone={marks.track}
            meter={{
              label: `${poolLabel(pool.name)}: ${pool.active} of ${limit} units leased`,
              now: pool.active,
              max: limit,
            }}
          />
          <span className="cap-use-pct">{shown}</span>
        </span>
      </td>
      <td role="cell" data-label="State">
        <span className="cap-marks">
          <PoolMarks marks={marks} />
        </span>
      </td>
      {/* SET BY ONLY WHEN IT IS NOT "configured" (#125): the configured limit
          is what applies to almost every row, and a column repeating it hid
          the two rows where AIMD or quota had lowered it. */}
      <td role="cell" data-label="Set by" title={by.detail}>
        {by.term === 'configured' ? '' : by.term}
      </td>
      <td role="cell" data-label="Links" className="cap-links">
        <a className="ctl-link" href="#capacity/holders">holders</a>
        {' · '}
        <a className="ctl-link" href="#admin/limits">limit</a>
      </td>
    </tr>
  )
}

/** What a pool's state is drawn as, in the table and on its card alike. */
interface PoolClass {
  /** The marks, in the order they are drawn. Never empty: healthy is `ok`. */
  chips: { cls: string; word: string; title: string }[]
  /** The row's tone class (and its legacy word), or none. */
  row: string | undefined
  /** The card track's fill tone, from the same classification. */
  track: 'is-bad' | 'is-paused' | 'is-warn' | undefined
}

/**
 * ONE CLASSIFICATION FOR A POOL, READ BY THE TABLE AND THE CARDS ALIKE (CP-14,
 * #85): paused, over ceiling, limit 0, full, ok.
 *
 * The two views used to decide for themselves, and disagreed: the table drew
 * a healthy pool `ok` and the Cards view drew nothing; the card coloured a
 * full pool's track `is-bad`, and past 80% `is-warn`, while its chip and the
 * table's row said warn. Now the chip, the row and the track are one answer:
 * a full pool is warn in all three.
 *
 * `LIMIT 0` IS NEW ON POOLS, and it is the mark Held back by and Profile
 * headroom draw for the same pool: a pool that is not paused with a ceiling of
 * 0 admits nothing, and it was drawn `ok` because nothing was in use.
 * `blockerCeiling` decides whose zero it is -- a pool only a person writes is
 * `set-to-zero` (paused tone: a person has to act), a provider pool zeroed by
 * its quota is `zero` (bad tone) -- so the three screens cannot disagree.
 *
 * `paused` and `over ceiling` can both be true, and both are drawn: the drift
 * is not hidden by the pause. The row and the track take the worse of them.
 */
function classifyPool(pool: Pool): PoolClass {
  const paused = isPaused(pool)
  const over = overCeiling(pool)
  const limit = pool.effective_limit
  const zero = !paused && !over && limit === 0
  const full = !over && limit > 0 && pool.active >= limit
  const ceiling = zero ? blockerCeiling({ reason: '', pool: pool.name, limit: 0 }) : null
  const zeroTone = ceiling !== null && needsAPerson(ceiling) ? 'is-paused' : 'is-bad'

  const chips: PoolClass['chips'] = []
  if (paused) {
    chips.push({ cls: 'is-paused', word: 'paused', title: 'An operator paused this pool. It admits nothing until resumed.' })
  }
  if (over) {
    chips.push({
      cls: 'is-bad',
      word: 'over ceiling',
      title: `${pool.active} units are held against a ceiling of ${limit}. Admission cannot produce this, so it is drift: a limit lowered under running work, or a slot never released. 'make pool-check' finds these.`,
    })
  }
  if (zero) {
    chips.push({
      cls: zeroTone,
      word: 'limit 0',
      title: ceilingCopy({ reason: '', pool: pool.name, limit: 0, active: pool.active }, 'This pool') ?? 'This pool is at limit 0.',
    })
  }
  if (full) {
    chips.push({ cls: 'is-warn', word: 'full', title: `At its ceiling: ${pool.active} of ${limit} units in use.` })
  }
  // §6.6 AND THE CP-14 RULING: a healthy pool gets the ok mark -- the filled
  // disc and the word -- and the mark is a text grey, not a hue (`--text-faint`
  // since CH-17 set the chip primitive's grey). Healthy carries no hue; a quiet
  // grey screen is what a healthy platform looks like.
  if (chips.length === 0) chips.push({ cls: 'is-ok', word: 'ok', title: 'Read, capped, not paused, and not at its ceiling.' })

  const track = over ? 'is-bad' : paused ? 'is-paused' : zero ? zeroTone : full ? 'is-warn' : undefined
  const row =
    track === undefined
      ? undefined
      : over
        ? 'is-bad over'
        : paused
          ? 'is-paused paused'
          : zero
            ? `${zeroTone} limit-0`
            : 'is-warn full'
  return { chips, row, track }
}

/**
 * The marks `classifyPool` decided, drawn once for the table and the cards.
 * The caller supplies the `.cap-marks` strip, so a card can lead it with the
 * pool's scope.
 */
function PoolMarks({ marks }: { marks: PoolClass }) {
  return (
    <>
      {marks.chips.map((c) => (
        <span key={c.word} className={`ctl-chip ${c.cls}`} title={c.title}>
          <i aria-hidden="true" />
          {c.word}
        </span>
      ))}
    </>
  )
}

/**
 * The Headroom table's two column names, written once because each is said
 * twice: in the header a desktop shows, and as the `data-label` a phone shows
 * in its place (§B6.3). Two literals is how the stacked key came to read a
 * bare `Could start` under a header that said `(min across pools)` (CP-11).
 */
const COULD_START = 'Could start (min across pools)'
const WEIGHT = 'Weight (units)'

/**
 * The legend this screen used to end on, as links.
 *
 * The middle entry carried "standard 1, browser 2, large 4" -- three platform
 * figures typed into a paragraph, which §5 of the prose migration table lists
 * as a restatement nothing checks. `units-not-agents` states the RULE and
 * names no figure, and the weights themselves are on the sizing table under
 * Runtimes, where they are read from the catalogue.
 */
const CAPACITY_TOPICS: readonly TopicId[] = [
  'units-not-agents',
  'pools-all-at-once',
  'pool-freshness',
  'tenant-scope',
  'absent-vs-zero',
]
