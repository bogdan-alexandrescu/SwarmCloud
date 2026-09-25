import { loadCapacity } from './api'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { Screen, timeAgo } from './Shell'
import { BlockerList, IncompleteNote, ceilingFigure, ceilingMark, ceilingTitle, headroomFigure, liftFigure } from './Blockers'
import { AGE_TICK_MS, useNow } from './useNow'
import {
  blockerCeiling,
  headroomFor,
  needsAPerson,
  poolLabelAmong,
  poolScope,
  type Capacity,
  type Counterfactual,
  type Pool,
  type ProfileBlocker,
  type RunnerProfile,
} from './types'

/**
 * The counterfactual's column (CP-6, #85), named once because it is said
 * twice: in the header, and as the stacked key on the cells that carry it.
 */
const LIFTED = '+N if lifted'

/**
 * The runner-profile catalogue — what kinds of agent this platform can run.
 *
 * Capacity answers "which ceiling is binding right now", pool by pool. This
 * answers the question before it: what may I ask for, what does one of them
 * weigh, and which pools does asking for it touch.
 *
 * Three things it must not get wrong.
 *
 * 1. A profile's `pools` list is the CALLING TENANT'S, including for an admin:
 *    service.capacity() builds it with pool_names_for(tenant_id=ctx.tenant_id)
 *    unconditionally (service.py:313-329). Every count here answers "how many
 *    more could I submit", so the tenant is named in words, repeatedly.
 * 2. A task must clear EVERY pool in its list at the same moment — admission is
 *    all-or-nothing across it (models.py:90-94). Capacity is the MINIMUM across
 *    them, never a sum; the per-pool "fits" column makes that minimum visible
 *    rather than asserted.
 * 3. The catalogue is frozen contract data that can GAIN entries, so everything
 *    below renders whatever arrived: no list of five, no per-name special case,
 *    no client-side weight table.
 */
export function ProfilesScreen() {
  return (
    <Screen
      /* "Profile headroom", NOT "Runner profiles", AND THE HEADING MOVED
         BECAUSE THE TAB DID -- test_nav_headings_agree.py asserts the two are
         one name and would have failed the build otherwise.
         The rename is what pays for the three-section nav putting this screen
         and `Runtimes.tsx` in one section. Those two were kept in SEPARATE
         sections for exactly this reason: "Runtimes" and "Runner profiles" are
         near-synonyms answering different questions, and two adjacent tabs
         with near-synonymous labels is how a reader takes the per-tenant
         figures on THIS screen for the platform-wide ones on that one. Every
         count here is built from `pool_names_for(tenant_id=ctx.tenant_id)`
         unconditionally, admin included (service.py:313-329); nothing on the
         runtime topology reads a tenant document at all. "Headroom" is the
         word this product already uses for a measurement of one tenant against
         a ceiling, so the label now says which of the two questions this
         screen answers instead of leaving it to be inferred from the section.
         The route is still `#capacity/profiles`: `runner_profile` is the
         contract's field name and invariant 10 is why this screen exists, so
         the address keeps the contract's noun. */
      title="Profile headroom"
      load={loadCapacity}
      summary={(d) => {
        const profiles = Object.values(d.runner_profiles)
        const backends = new Set(profiles.map((p) => p.backend)).size
        return `${profiles.length} profiles · ${backends} backend${backends === 1 ? '' : 's'}`
      }}
      /* loadCapacity calls a response empty when `pools` is empty, not when the
         catalogue is — so this copy names THAT query. A response with pools and
         no profiles never reaches here; Catalogue below renders that case. */
      empty={{
        heading: 'The capacity read returned no pools',
        body: 'The read succeeded and its pool list was empty. Pools are created at provisioning time, so profiles cannot be priced against anything yet — this is a real absence, not a failed lookup.',
      }}
    >
      {(d) => <Catalogue capacity={d} />}
    </Screen>
  )
}

/**
 * Whose view this is, taken from the PROFILES' pool lists rather than the
 * top-level pool list.
 *
 * It matters which. An admin sees OTHER tenants' pools in `capacity.pools`
 * (service.py:299-306 filters them for non-admins only) and they sort ahead of the
 * caller's — `provider:anthropic:tenant:eng` precedes `tenant:u-bogdan` — so reading
 * the first tenant-ish name out of that list names the wrong tenant for exactly the
 * reader most likely to be misled. A profile's list is built for ctx.tenant_id alone
 * and always carries `tenant:<id>` (models.py:95-101). `slice`, not a `[^:]+` pattern:
 * an id truncated at a colon is a different id.
 */
function tenantOf(capacity: Capacity): string | null {
  for (const profile of Object.values(capacity.runner_profiles)) {
    for (const name of profile.pools) {
      if (name.startsWith('tenant:')) return name.slice('tenant:'.length)
    }
  }
  return null
}

function Catalogue({ capacity }: { capacity: Capacity }) {
  const entries = Object.entries(capacity.runner_profiles).sort(([a], [b]) => a.localeCompare(b))
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const tenant = tenantOf(capacity)

  // A read that succeeded with an empty catalogue. Not the Screen's `empty`,
  // which can only mean "no pools", and not a failure — so nothing here is red.
  if (entries.length === 0) {
    return (
      <section className="section panel">
        <h2>The catalogue came back with no entries</h2>
        <p className="muted">
          The capacity read succeeded and returned {capacity.pools.length} pools, so
          this is not a failed query — <code>runner_profiles</code> itself arrived
          empty, and nothing can be submitted until a profile is registered.
        </p>
      </section>
    )
  }

  return (
    <>
      {/* THE BANNER AND THE SCOPE PARAGRAPH ARE GONE (CP-5, visual QA
          2026-09-25), as prose-migration-table §3.14 had already decided and
          this screen had not followed. Neither claim was lost:

            "a task must clear EVERY pool ... the MINIMUM, never a sum" is
            `#help/pools-all-at-once`, linked from this screen's footer, and is
            drawn on every card as the pool list headed `Pool it must clear`
            with the figure above it equal to the smallest `Fits`;

            "these pool lists are for tenant X, including if you are an admin"
            is the `tenant X` note on EVERY card's head -- the card-note slot
            Pools uses for the same Trap D -- and `#help/tenant-scope` in the
            footer. A qualifier on the card cannot be scrolled away from the
            figures it scopes; a paragraph above the first card could. */}
      {entries.map(([name, profile]) => (
        <ProfileCard
          key={name}
          name={name}
          profile={profile}
          byName={byName}
          tenant={tenant}
          groups={capacity.blocked_reason_groups}
        />
      ))}
      {/* §8.4(4): PROVENANCE, ONCE, FOR THE WHOLE PAGE. Every `+N if lifted`
          figure above is a prediction worked out from pool counts read at one
          instant, and the instant is the server's `generated_at`. It was a
          sentence at the foot of every card ("From pool counts read 2m ago,
          one pool at a time."); the owner's CP-6 decision leaves each card one
          fact sentence, so the instant is said here, once, for all of them. */}
      <p className="provenance">
        {entries.length} profiles · the whole catalogue this response carried, not a page of it ·{' '}
        <CountsAge generatedAt={capacity.generated_at} />
      </p>
      <HelpLinks topics={PROFILE_TOPICS} />
    </>
  )
}

/**
 * `counts read 2m ago, one pool at a time`, on the age tick, so it moves while
 * the page is open. The `<time>` carries the server's own instant.
 */
function CountsAge({ generatedAt }: { generatedAt: string }) {
  const now = useNow(AGE_TICK_MS)
  return (
    <>
      counts read{' '}
      <time dateTime={generatedAt} title={generatedAt}>
        {timeAgo(generatedAt, now)}
      </time>
      , one pool at a time
    </>
  )
}

function ProfileCard({ name, profile, byName, tenant, groups }: {
  name: string
  profile: RunnerProfile
  /** Every pool this caller may see, by name. A name absent from it is uncapped. */
  byName: ReadonlyMap<string, Pool>
  tenant: string | null
  /** The server's remedy groups, `blocked_reason_groups`, for the blocker list. */
  groups: Record<string, string[]> | undefined
}) {
  // Read, not computed. `profile.admission` is what swarm_api/headroom.py got
  // out of `evaluate_capacity`; this card used to re-derive it and kept only
  // the running minimum, which is how two pools refusing at once became one
  // name on the screen.
  const head = headroomFor(profile)
  const figure = headroomFigure(head)
  const weight = profile.units > 0 ? profile.units : 1
  const rows = profile.pools.map((pool) => ({ pool, row: byName.get(pool) ?? null }))
  // EVERY pool refusing this profile, and every pool capping it. Two sets,
  // because "refusing now" and "would run out first" are different facts and
  // the row tags have to say which one a pool is.
  const refusing = new Map(head.blockers.map((b) => [b.pool, b]))
  const unread = new Set(head.unread)
  // What lifting each ceiling would have bought, by pool (CP-6).
  const lifts = new Map(head.counterfactual.map((c) => [c.pool, c]))
  // EVERY BINDING POOL, NOT THE FIRST (CP-4, visual QA 2026-09-25). `head.
  // binding` is `admission.binding[0]` -- one name, for the columns that have
  // room for one -- and this card used it to tag rows, so when two pools tied
  // it tagged one of them `binding` while this screen's own counterfactual
  // said lifting that one alone bought nothing. 3 of 5 cards, live. The list
  // is the server's (`headroom.py`); nothing here re-derives it.
  const binding = bindingPools(profile, head.binding)
  // One name per pool across the whole card, qualified where two would read
  // alike (CP-15): the `browser` profile clears `resource:browser` AND
  // `runner:browser`, and both printed `browser`.
  const among = [...profile.pools, ...binding]
  const label = (pool: string) => poolLabelAmong(pool, among)
  // A PROFILE THE PLATFORM REFUSES IS NOT PRICED (CP-3). Its pools are still
  // listed -- they are true, and they are what re-enabling it would face --
  // but "N could start", the run-out line and the counterfactuals are offers,
  // and there is nothing on offer. Absent means available: an older API does
  // not send the field, and the submit gate refuses a disabled profile anyway.
  const off = profile.available === false
  const reason = profile.disabled_reason || 'refused by the platform'

  return (
    <section className="section panel">
      <h2>
        {/* `.section > h2` uppercases, and this is an identifier: `claude-code`
            is the exact string a caller sends as runner_profile, so CLAUDE-CODE
            would be a name nobody can copy. Overridden here rather than in
            styles.css, where it would change every other screen's headings. */}
        <span className="mono" style={{ textTransform: 'none' }}>{name}</span>
        {/* Three cases, not two. A measured count, a profile nothing caps (no
            number exists), and a profile with an unread pool (a number exists
            and we do not have it). The last two both render an em dash and
            must not be worded the same. A fourth, above all three: the
            platform refuses the profile, and no count is drawn at all. */}
        {off ? (
          <span className="ctl-chip is-bad" aria-label={`Disabled: ${reason}`}>
            <i aria-hidden="true" />
            disabled
          </span>
        ) : (
          <span className="count-chip" title={figure.title}>
            {head.agents !== null
              ? `${head.agents} could start`
              : head.basis === 'uncapped'
                ? 'nothing here caps this'
                : 'not measured — a pool could not be read'}
          </span>
        )}
        {/* TRAP D, ON THE CARD (CP-5). Every figure here is the CALLING
            tenant's, admin included -- `pool_names_for(tenant_id=ctx.
            tenant_id)`. It is the note Pools' Headroom carries, in the same
            slot, and it replaces both the paragraph above the first card and
            the `for <tenant>` this chip used to end on. */}
        <span className="ctl-card-note is-end">{tenant ? `tenant ${tenant}` : 'your tenant'}</span>
      </h2>

      {off && <p className="muted">{reason}</p>}

      <dl className="kv">
        <dt>Backend</dt>
        <dd className="mono">{profile.backend}</dd>
        <dt>Provider</dt>
        {/* null is not "unknown": the profile needs no provider key at all, so
            no provider pool exists and no provider quota can block it. That
            is a measured answer, so it is WORDS -- Runtimes' words for the
            same fact -- and not the em dash, which on this screen means a
            figure nobody measured (CP-1). */}
        <dd className="mono">{profile.provider ?? 'none needed'}</dd>
        <dt>Resource class</dt>
        <dd className="mono">{profile.resource_class}</dd>
        {/* Weight straight from the payload. RESOURCE_UNITS in types.ts is a
            bundled copy of the same table and can drift; the response has
            already resolved the class to units, so that is what is shown.
            In the app-wide `2u`, as Pools and Runtimes print it (CP-24): the
            clause that followed it -- "per agent, added to every pool below
            on admission" -- was the rationale `#help/units-not-agents` holds,
            linked from this screen's footer. */}
        <dt>Weight</dt>
        <dd>{profile.units}u</dd>
      </dl>

      {/* `is-stacked` — F6 OF `docs/audits/2026-09-23/overflow-inventory.md`.
          At 390pt this table measured `clientWidth: 358` against a
          `scrollWidth` of 543-615 across its five instances: 34-42% of the
          columns were behind an `overflow-x: auto` that paints no scrollbar on
          this platform, so `Units free`, `Fits` and `Status` were simply not
          there and nothing said they existed. Below 900px each row becomes a
          stacked record with its own key column (§B6.3 in `styles.css`), which
          is what `data-label` supplies — as an attribute rather than a second
          element per cell, so that the rendered-word budgets count the same
          screen they always did.

          The explicit `role`s are not decoration either: changing `display` on
          a table element drops its implicit ARIA role in every browser, so
          without them a stacked table is a pile of anonymous blocks to a screen
          reader. They are the roles these elements already have. */}
      <div className="table-wrap is-stacked">
        <table className="pools" role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Pool it must clear</th>
              <th role="columnheader" scope="col">Scope</th>
              {/* "units", never "agents": admission increments by the class's
                  weight, so 8 in use may be four browser agents. */}
              <th role="columnheader" scope="col" className="n">Units free</th>
              <th role="columnheader" scope="col" className="n">Fits</th>
              {/* THE COUNTERFACTUAL AS A COLUMN (CP-6, #85). It was a list of
                  sentences under the card -- "4 more would have started",
                  and 5-7 rows of "nothing would have changed" -- which made
                  the page 9,882px tall at 390. The figure a sentence carried
                  is now a cell on the row of the pool it is about, filled for
                  the pools that cap this card's figure; a pool whose lifting
                  buys nothing gets no cell value, which is what those rows
                  said. The sentence is the cell's accessible name. */}
              <th role="columnheader" scope="col" className="n">{LIFTED}</th>
              <th role="columnheader" scope="col">Status</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {rows.map(({ pool, row }) => {
              const paused = row !== null && isPaused(row)
              // Every refusing pool is marked, not just the tightest one.
              const blocker = refusing.get(pool) ?? null
              const capping = binding.includes(pool) || blocker !== null
              const scope = poolScope(pool)
              const mark = statusMark(row, blocker, unread.has(pool))
              return (
                <tr role="row" key={pool} className={mark.row}>
                  <th role="rowheader" scope="row" className="pool-name" title={pool}>
                    {label(pool)}
                    <span className="raw">{pool}</span>
                  </th>
                  <td role="cell" data-label="Scope">
                    <span className={`scope ${scope}`}>{scope === 'platform' ? 'platform-wide' : 'this tenant'}</span>
                  </td>
                  <td role="cell" data-label="Units free" className="n">{row === null ? '—' : row.available}</td>
                  {/* A paused pool fits 0 however much headroom it reports &mdash; that is
                      a fact admission enforces, not a missing value coalesced to zero. */}
                  <td role="cell" data-label="Fits" className="n">{row === null ? '—' : paused ? 0 : Math.floor(Math.max(0, row.available) / weight)}</td>
                  <LiftCell
                    // A disabled profile is not priced (CP-3): nothing it
                    // could buy is on offer, so no row carries a figure.
                    show={capping && !off}
                    complete={head.complete}
                    lift={lifts.get(pool)}
                    pool={pool}
                    label={label}
                  />
                  <td role="cell" data-label="Status">
                    {/* EXACTLY ONE STATUS MARK, NEVER A BLANK CELL (CP-12,
                        #85). A healthy pool used to draw nothing here while
                        Pools drew `ok` for the same pool, and at 390 the
                        stacked row read a bare `Status` key. The marks are
                        Pools' vocabulary, and the first that applies wins. */}
                    <span className="cap-marks">
                      <StatusMarkView mark={mark} />
                      {/* A FACT ABOUT A HEALTHY POOL, NOT A WARNING (CP-13).
                          `binding` was the retired bordered `.tag` in the warn
                          hue, on a pool with room left: it says which ceiling
                          the figure above is the minimum of. The info chip is
                          §6.6's word for a fact that is neither good nor bad.
                          It FOLLOWS the status mark rather than replacing it. */}
                      {capping && blocker === null && (
                        <span className="ctl-chip is-info">
                          <i aria-hidden="true" />
                          binding
                        </span>
                      )}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {/* ONE FACT SENTENCE PER CARD, AND IT IS WHAT RUNS OUT FIRST (CP-6).
          "No pool is refusing this profile.", the counterfactual list and the
          per-card provenance line are gone: what lifting a pool would buy is
          its `+N if lifted` cell, and the instant the counts were read is the
          page's foot. A disabled profile gets no sentence at all: every one
          of these is an offer the platform will refuse, and why it refuses is
          under the heading. */}
      {!off && (
        <p className="muted small">
          {head.agents === null
            ? head.basis === 'uncapped'
              ? 'No pool in this list is configured, so nothing here limits this profile and there is no number to report.'
              : 'A pool in this list could not be read, so there is no number to report.'
            : head.agents === 0
              ? `Nothing more of this profile can start: ${
                  head.blockers.length > 0 ? head.blockers.map((b) => label(b.pool)).join(' and ') : 'a pool'
                } ${head.blockers.length > 1 ? 'are' : 'is'} refusing it.`
              : `${binding.length > 0 ? binding.map(label).join(' and ') : 'One of these pools'} would run out first.`}
        </p>
      )}

      {/* THE REMEDY, AS TEXT A PHONE SHOWS (#159 review of CP-6). The first
          cut of CP-6 removed the grouped blocker list and the incomplete-read
          banner along with the counterfactual list, which the owner's
          decision did not ask for, and left each refusal's reason, figures
          and remedy in a status mark's `title` -- which a touch screen never
          shows. They are back under the sentence: every refusing pool, filed
          by the SERVER'S remedy group -- somebody has to act, or waiting
          clears it -- in the card's own marks (CP-12: no `.tag` here), with
          the banner above a list that a missed read made partial. Both draw
          nothing when nothing refuses, so a healthy card is still the table
          and its one sentence. A disabled profile gets neither, for the
          reason it gets no sentence. */}
      {!off && (
        <>
          <IncompleteNote h={head} label={label} />
          <BlockerList h={head} groups={groups} label={label} />
        </>
      )}
    </section>
  )
}

/**
 * The one status mark a Profile headroom row carries (CP-12, #85): the first of
 * these that applies, in Pools' vocabulary.
 *
 *   not read   `.ctl-mark.is-unread` -- a MARK, dashed, as Pools' Held back by
 *              draws it and as design-system §8.6's "read failed" row says
 *   uncapped   `.ctl-chip.is-info`, the flat bar: a fact, not a verdict (it
 *              was the green `.tag.ok`)
 *   paused     `.ctl-chip.is-paused`
 *   limit 0    the chip Held back by draws for a blocker at a ceiling of zero:
 *              `is-paused` when a person set it (`set-to-zero`), `is-bad` for
 *              a provider pool zeroed by quota (`zero`). Never `full`.
 *   full       a blocker at a positive ceiling: `.ctl-chip.is-warn`, as Pools
 *              draws full (it was `.tag.full` in `--bad`)
 *   ok         read, capped, not paused, not refusing: `.ctl-chip.is-ok`, the
 *              grey disc (CP-14)
 *
 * Each old tag's explanation is the mark's `title` and `aria-label`. The row's
 * class follows the mark, so a limit-0 row is not classed `full`. The tone and
 * word of paused, limit 0 and full are `ceilingMark`'s (Blockers.tsx), the
 * same table the blocker list under the card reads, so the two places on one
 * card cannot draw one pool two ways.
 */
interface StatusMark {
  kind: 'unread' | 'uncapped' | 'paused' | 'limit-0' | 'full' | 'ok'
  cls: string
  word: string
  say: string
  row: string | undefined
}

function statusMark(row: Pool | null, blocker: ProfileBlocker | null, notRead: boolean): StatusMark {
  if (notRead) {
    return {
      kind: 'unread',
      cls: 'ctl-mark is-unread',
      word: 'not read',
      say: 'This pool could not be read. It is not uncapped and it is not empty: nothing is known about it, and it may be the one refusing.',
      row: 'unmeasured',
    }
  }
  if (row === null) {
    return {
      kind: 'uncapped',
      cls: 'ctl-chip is-info',
      word: 'uncapped',
      say: 'No pool of this name is configured, so nothing caps it. The global and tenant pools always exist, so this is a narrow named pool that was never given a limit.',
      row: undefined,
    }
  }
  const ceiling = blocker !== null ? blockerCeiling(blocker) : null
  if (isPaused(row) || ceiling === 'paused') {
    const m = ceilingMark('paused')
    return {
      kind: 'paused',
      cls: `ctl-chip ${m.cls}`,
      word: m.word,
      say: `${blocker !== null ? `${blocker.reason}, ${ceilingFigure(blocker)}. ` : ''}${ceilingTitle('paused')}`,
      row: 'paused',
    }
  }
  // A ceiling of zero, from the blocker the server sent -- or, on a read that
  // listed no blocker for it, from the pool's own figure, decided the same way.
  const zero =
    ceiling === 'set-to-zero' || ceiling === 'zero'
      ? ceiling
      : row.effective_limit === 0
        ? blockerCeiling({ reason: '', pool: row.name, limit: 0 })
        : null
  if (zero === 'set-to-zero' || zero === 'zero') {
    const person = needsAPerson(zero)
    const m = ceilingMark(zero)
    return {
      kind: 'limit-0',
      cls: `ctl-chip ${m.cls}`,
      word: m.word,
      say: `${blocker !== null ? `${blocker.reason}, ${ceilingFigure(blocker)}. ` : ''}${ceilingTitle(zero)}`,
      row: person ? 'paused' : 'over',
    }
  }
  if (ceiling === 'full' && blocker !== null) {
    const m = ceilingMark('full')
    return {
      kind: 'full',
      cls: `ctl-chip ${m.cls}`,
      word: m.word,
      say: `${blocker.reason}, ${ceilingFigure(blocker)}. ${ceilingTitle('full')}`,
      row: 'full',
    }
  }
  return {
    kind: 'ok',
    cls: 'ctl-chip is-ok',
    word: 'ok',
    say: 'Read, capped, not paused, and not refusing this profile.',
    row: undefined,
  }
}

function StatusMarkView({ mark }: { mark: StatusMark }) {
  if (mark.kind === 'unread') {
    return (
      <span className={mark.cls} title={mark.say} aria-label={mark.say}>
        {mark.word}
      </span>
    )
  }
  return (
    <span className={mark.cls} title={mark.say} aria-label={mark.say}>
      <i aria-hidden="true" />
      {mark.word}
    </span>
  )
}

/**
 * One `+N if lifted` cell (CP-6, #85): what relaxing THIS pool's ceiling alone
 * would have bought, at the counts the page foot dates.
 *
 * Filled for the pools that cap the card's figure -- the binding ones, and
 * every one refusing now. A pool whose lifting buys nothing gets an empty
 * cell with no stacked key: those were the "nothing would have changed" rows,
 * and the owner's decision removes them. A +0 on a pool that DOES bind stays,
 * because it is the valuable zero -- two pools tie, and lifting one alone buys
 * nothing -- and its accessible name says what still binds.
 *
 * A PAUSED pool's figure is what RESUMING it buys, not lifting it (the
 * server's `action: 'resume'`), and its visible text says so -- `+4 if
 * resumed` -- because the column's header cannot.
 */
function LiftCell({
  show,
  complete,
  lift,
  pool,
  label,
}: {
  show: boolean
  complete: boolean
  lift: Counterfactual | undefined
  pool: string
  label: (pool: string) => string
}) {
  if (!show) return <td role="cell" className="n cf-lift" />
  const f = liftFigure(lift, complete, pool, label)
  return (
    <td
      role="cell"
      data-label={LIFTED}
      className={`n cf-lift${f.kind === 'measured' ? '' : ` is-${f.kind}`}`}
      aria-label={f.say}
      title={f.say}
    >
      {f.text === '—' ? <span className="ctl-em">—</span> : <span className="cf-effect">{f.text}</span>}
    </td>
  )
}

/**
 * Every pool the server says the next task of this profile would run out on.
 *
 * `admission.binding` is the list (`headroom.py`). `fallback` is `headroomFor`'s
 * single name, which is the first blocker when the list is empty and nothing
 * when the API predates the block -- so a card never tags FEWER pools than it
 * did before this read the list.
 */
function bindingPools(profile: RunnerProfile, fallback: string | null): string[] {
  const listed = profile.admission?.binding ?? []
  if (listed.length > 0) return listed
  return fallback === null ? [] : [fallback]
}

/**
 * The two `<dt>`/`<dd>` pairs this screen used to end on, as links.
 *
 * Both were arguments made on other screens too -- `units-not-agents` on six
 * files and `runner-profile-by-name` on three -- so collapsing them into one
 * topic each is where the page actually gets shorter rather than merely
 * tidier.
 */
const PROFILE_TOPICS: readonly TopicId[] = [
  'units-not-agents',
  'runner-profile-by-name',
  'pools-all-at-once',
  'tenant-scope',
]
