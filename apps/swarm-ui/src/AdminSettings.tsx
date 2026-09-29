import { useEffect, useId, useState, useSyncExternalStore } from 'react'
import { loadCapacity, setPoolLimit } from './api'
import { errorHeading, isPaused, type ApiError } from './fetch'
import { HelpCard } from './HelpCard'
import { Screen } from './Shell'
import {
  FAMILY_TITLE,
  POOL_FAMILY_ORDER,
  poolKind,
  poolLabel,
  poolLabelAmong,
  setBy,
  type Capacity,
  type Pool,
  type PoolKind,
} from './types'

/**
 * Pool limits: the concurrency ceilings, editable.
 *
 * WHY THIS SCREEN EXISTS RATHER THAN A TFVARS EDIT. `pool_limits` in an
 * environment's tfvars is the ceiling a NEW environment is born with, and
 * nothing else: terraform/modules/firestore/bootstrap.tf carries
 * `ignore_changes = [fields]` on every pool document, deliberately, because
 * `active` is mutated by the admission transaction on every lease and an
 * apply that rewrote those documents would reset live counters to zero and
 * instantly oversubscribe every pool.
 *
 * The consequence was found the hard way: pool_limits was raised from
 * 20/10/5 to 40/20/15, committed with a carefully argued comment beside the
 * values, applied -- and the running platform stayed at 20/10/5. The apply
 * moved a terraform output and nothing else.
 *
 * THE ARITHMETIC THIS SCREEN HAS TO MAKE OBVIOUS, AND HOW IT NOW DOES IT.
 * A task takes EVERY pool in its runner profile's list, so its capacity is the
 * MINIMUM across them. Raising one pool changes nothing if another still binds
 * -- which is exactly what happened when five pools went to 40 and
 * `provider:anthropic:tenant:u-bogdan` stayed at 5, holding the real ceiling
 * at five.
 *
 * That used to be a sentence: "a task must clear EVERY pool its profile lists,
 * so its ceiling is the MINIMUM across them". A sentence asserting an
 * arithmetic rule is the weakest way to show one, because the reader has to
 * carry it to the figures and apply it themselves. SO THE OPERANDS ARE DRAWN
 * INSTEAD: every profile card lists each of its pools with that pool's own
 * agent ceiling, and the smallest is marked as the one that binds. `min()` is
 * not explained; it is shown with its inputs beside its output, which is the
 * form in which nobody has to be told what `min` means.
 *
 * The sentence itself lives at `#help/pools-all-at-once`.
 */
export function AdminSettingsScreen() {
  /**
   * THE READ A SAVE TRIGGERS, AND WHAT THE SAVE SAID, BOTH HELD OUT HERE (AH-7,
   * visual QA 2026-09-25).
   *
   * A save used to bump a `key` on `Screen`, which re-reads by being REMOUNTED:
   * the skeletons flashed, every row was rebuilt, so the `saved` tag the row
   * had just set never painted, an unsaved edit in another row was thrown
   * away, and -- because `Screen`'s stale rule lives in a ref the remount
   * destroys -- a re-read that failed blanked the whole screen immediately
   * after a write that had SUCCEEDED. The operator was left not knowing
   * whether their ceiling had been applied, which is the one thing this
   * screen exists to tell them.
   *
   * So `Screen` is never remounted. A save re-reads `/v1/capacity` itself and
   * the result is drawn in place of the rows `Screen` last handed down, for as
   * long as those rows are the ones the re-read was taken over: a refresh from
   * `Screen`'s own control hands down a new object and wins, so the newer read
   * is always the one on screen. A re-read that fails leaves the rows exactly
   * as they were and says, on the row, that the figures were not re-read.
   * `Accounts.tsx` keeps its verdicts on this side of the reload for the same
   * reason (its `Persisted`); this is that rule without the remount.
   */
  const [fresh, setFresh] = useState<{ over: Capacity; data: Capacity } | null>(null)
  const [saved, setSaved] = useState<Readonly<Record<string, SaveMark>>>({})

  const mark = (pool: string, m: SaveMark | null) =>
    setSaved((all) => {
      const next = { ...all }
      if (m === null) delete next[pool]
      else next[pool] = m
      return next
    })

  const reread = async (over: Capacity, pool: string): Promise<boolean> => {
    mark(pool, 'rereading')
    const r = await loadCapacity()
    if (r.status === 'ok' || r.status === 'stale') {
      setFresh({ over, data: r.data })
      mark(pool, 'saved')
      return true
    }
    mark(pool, 'unread')
    return false
  }

  return (
    <Screen
      // "Pool limits", not "Admin settings". The tab says Pool limits and it
      // is the accurate one twice over: this screen edits concurrency ceilings
      // and nothing else, so "Admin settings" over-claimed a settings page
      // that does not exist, and it restated the section it already sits under
      // ("Admin") instead of naming the thing on the screen.
      title="Pool limits"
      load={loadCapacity}
      // A count, not a promise. "changes take effect immediately" was a
      // rationale in the one slot on this screen a reader cannot skip.
      summary={(d) =>
        `${d.pools.length} pools · ${Object.keys(d.runner_profiles).length} profiles`
      }
      empty={{
        heading: 'No pools exist',
        body: 'Pools are created at provisioning time.',
      }}
    >
      {(d) => (
        <Body
          capacity={fresh !== null && fresh.over === d ? fresh.data : d}
          saved={saved}
          onSaved={(pool) => reread(d, pool)}
          onEdit={(pool) => mark(pool, null)}
        />
      )}
    </Screen>
  )
}

/**
 * What the last save of one row said, kept OUTSIDE the row so nothing that
 * re-renders the rows can take it off screen.
 *
 *   rereading  written; the figures are being read back
 *   saved      written and read back -- the row shows the platform's value
 *   unread     written, but the read-back failed: the row's figures are the
 *              ones from before the write, and it says so
 */
type SaveMark = 'rereading' | 'saved' | 'unread'

/**
 * One profile's ceiling, with the numbers it was taken over.
 *
 * `agents` is the pool's own effective limit divided by the profile's weight,
 * because a pool counts weighted units and this column counts agents. `null`
 * means the pool the profile names is not in the response at all, which is an
 * absence rather than a zero and is drawn as one.
 */
interface Operand {
  pool: string
  agents: number | null
}

/**
 * The ceiling, and EVERY pool that sets it.
 *
 * AH-1 (S1, visual QA 2026-09-25): this kept a running minimum with a strict
 * `<` and so named the FIRST of two pools tied at it. On 3 of 5 live cards two
 * pools tied, the card bolded one and its foot said `binds on` that one -- and
 * an operator who raised it watched the ceiling stay exactly where it was,
 * which is the incident this screen's header describes, reintroduced by the
 * screen built to prevent it.
 *
 * NOT `profile.admission.binding`. That list is HEADROOM-binding -- which pool
 * the next task would run out on given what is in use now -- and this card is
 * about the CEILING, which is a property of the limits alone. The two name
 * different pools whenever one pool is busier than another with a higher
 * limit, so reading the served list here would mark the wrong operand.
 */
function arithmetic(
  pools: string[],
  units: number,
  byName: Map<string, Pool>,
): { operands: Operand[]; ceiling: number | null; binding: string[] } {
  const w = units > 0 ? units : 1
  const operands: Operand[] = pools.map((pool) => {
    const p = byName.get(pool)
    return { pool, agents: p ? Math.floor(p.effective_limit / w) : null }
  })

  const measured = operands.flatMap((o) => (o.agents === null ? [] : [o.agents]))
  const ceiling = measured.length === 0 ? null : Math.min(...measured)
  const binding = ceiling === null ? [] : operands.filter((o) => o.agents === ceiling).map((o) => o.pool)
  return { operands, ceiling, binding }
}

function Body({
  capacity,
  saved,
  onSaved,
  onEdit,
}: {
  capacity: Capacity
  saved: Readonly<Record<string, SaveMark>>
  onSaved: (pool: string) => Promise<boolean>
  onEdit: (pool: string) => void
}) {
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const profiles = Object.entries(capacity.runner_profiles).sort(([a], [b]) =>
    a.localeCompare(b),
  )

  return (
    <>
      {profiles.length > 0 && (
        <section className="section">
          {/* One word where "What actually binds, per profile" used to be, and
              now two: `Binding pool` says WHICH of the several ceilings each
              card names, which is what the `?` was opened for. The rule behind
              it -- that a task clears every pool in its list at one moment, so
              the binding one is the minimum and never a sum -- is stated where
              the figure it governs is, on the Capacity board's `Could start
              (min across pools)` column, and is in the rail's Help section.
              This screen keeps one glyph, on Ceilings below. */}
          {/* NO `has-q` EITHER. That class is `display: flex` with a baseline
              gap, and it exists to sit a `?` beside an eyebrow's text; on an
              eyebrow with no glyph it turns one text node into a flex item for
              nothing. The `Ceilings` eyebrow below keeps both, because it keeps
              the glyph. */}
          <span className="ctl-eyebrow">Binding pool</span>
          <div className="ctl-cards">
            {profiles.map(([name, prof]) => (
              <ProfileCard
                key={name}
                name={name}
                units={prof.units}
                {...arithmetic(prof.pools, prof.units, byName)}
              />
            ))}
          </div>
        </section>
      )}

      <PoolEditor pools={capacity.pools} saved={saved} onSaved={onSaved} onEdit={onEdit} />
    </>
  )
}

function ProfileCard({
  name,
  units,
  operands,
  ceiling,
  binding,
}: {
  name: string
  units: number
  operands: Operand[]
  ceiling: number | null
  binding: string[]
}) {
  // The unit that used to be a footnote -- "'Ceiling' is how many AGENTS of
  // that profile could run at once, not units" -- is now fused to the figure
  // (`4 agents`) and to the weight in the card note (`1 unit each`), which is
  // the other half of the same fact. §8.4.1: a well-chosen unit is the
  // explanation.
  const measured = ceiling !== null
  // One name per operand, qualified where two would read alike (CP-15): the
  // `browser` profile takes `resource:browser` AND `runner:browser`, and both
  // printed `browser` -- two operands with one name and different ceilings.
  const among = operands.map((o) => o.pool)
  const label = (pool: string) => poolLabelAmong(pool, among)
  const named = binding.map(label).join(' and ')

  return (
    <section className="ctl-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{name}</h2>
        <span className="ctl-card-note">
          {units} unit{units === 1 ? '' : 's'} each
        </span>
      </div>
      <div className="ctl-card-body">
        <b
          className={`ctl-figure${measured ? '' : ' is-absent'}`}
          aria-label={
            measured
              ? `${ceiling} agents of ${name} can run at once. That is the smallest ceiling across the ${operands.length} pools this profile takes, and ${binding.length === 1 ? `${named} is the pool that binds it` : `${named} bind it together: raising any one of them alone leaves it where it is`}.`
              : `No ceiling can be computed for ${name}: none of the pools it takes are in this response, so the figure is not measured rather than zero.`
          }
        >
          {/* A CEILING, AND SAID TO BE ONE (#132): `max 5 agents`. The bare
              `5 agents` read as a count of agents running, which is the other
              figure on this platform with that unit. */}
          {measured && <span className="ctl-figure-unit adm-figure-max">max</span>}
          {measured ? ceiling : <i className="ctl-em">—</i>}
          {measured && <span className="ctl-figure-unit">agents</span>}
        </b>

        {/* THE OPERANDS. Each pool's own ceiling in agents, the smallest one
            marked. This is the whole reason the paragraph could go: the
            reader sees three numbers and the marked one is the smallest, so
            "the minimum across them" is a thing they read off the card
            rather than a rule they were asked to remember. */}
        {/* §B6.4: a key COLUMN. Eight pool names and eight ceilings in a
            wrapping strip read as one run-on line of alternating word and
            digit; the smallest of them is the whole point of the card and it
            was the hardest thing on it to find. */}
        {/* EVERY OPERAND AT THE MINIMUM IS MARKED (AH-1), not the first one:
            two pools tied at the ceiling both bind it, and raising either
            alone moves nothing.

            THE FIGURE IS ITS OWN ELEMENT (AH-19). It was a bare text node after
            the key, so it started wherever the key's column ended and a `5`
            sat under the `2` of a `25`. `.adm-value` is the hook the
            stylesheet right-aligns in a fixed tabular-nums track, so the
            operands compare down the card the way a column of figures does. */}
        <ul className="ctl-facts is-rows adm-operands">
          {operands.map((o) => (
            <li
              key={o.pool}
              className={`ctl-fact${o.agents === null ? ' is-absent' : ''}${
                binding.includes(o.pool) ? ' is-binding' : ''
              }`}
              title={o.pool}
            >
              <b>{label(o.pool)}</b>
              <span className="adm-value">
                {o.agents === null ? <i className="ctl-em">—</i> : o.agents}
              </span>
            </li>
          ))}
        </ul>
      </div>
      <p className="ctl-card-foot">
        {binding.length === 0 ? (
          <>no pool in this response</>
        ) : (
          <>binds on {named}</>
        )}
      </p>
    </section>
  )
}

/**
 * The pool a link asked for: `#admin/limits?pool=tenant:eng`, as the Tenants
 * roster's Enforced figures write it (#134).
 *
 * READ FROM THE ADDRESS, AND FOLLOWED. The router keeps `?pool=` on Pool
 * limits' address (App.tsx `fromHash` / `canonical`) -- it did not at first,
 * and the normalise effect stripped it before this screen's async read had
 * drawn a row, so the link opened the page at no row. It is read through
 * `hashchange` too, so a second Enforced link followed while this screen is
 * already mounted moves the mark rather than leaving it on the first row.
 */
function subscribeHash(onChange: () => void): () => void {
  if (typeof window === 'undefined') return () => {}
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

function linkedPool(): string | null {
  if (typeof window === 'undefined') return null
  const hash = window.location.hash
  const at = hash.indexOf('?')
  if (at === -1) return null
  return new URLSearchParams(hash.slice(at + 1)).get('pool')
}

/** §7.2's phone width, where the filter is drawn. The Help card's own query. */
const PHONE_QUERY = '(max-width: 560px)'

function subscribePhone(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(PHONE_QUERY)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

/** False where there is no `matchMedia` (jsdom), which is the desktop's answer. */
function atPhoneWidth(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(PHONE_QUERY).matches
}

/**
 * The row being edited, and what has been typed into it. `draft` null means
 * the field shows the pool's own value off the latest read.
 *
 * HELD HERE, ABOVE THE ROWS (AH-7). One editor is open at a time (#132), and
 * what is typed into it must survive anything that rebuilds the rows -- a
 * re-read after another row's save, a filter that is changed and changed back.
 */
interface Editing {
  pool: string
  draft: string | null
}

function PoolEditor({
  pools,
  saved,
  onSaved,
  onEdit,
}: {
  pools: Pool[]
  saved: Readonly<Record<string, SaveMark>>
  onSaved: (pool: string) => Promise<boolean>
  onEdit: (pool: string) => void
}) {
  const [editing, setEditing] = useState<Editing | null>(null)
  const [writing, setWriting] = useState<readonly string[]>([])
  const onWriting = (pool: string, on: boolean) =>
    setWriting((w) => (on ? [...w.filter((p) => p !== pool), pool] : w.filter((p) => p !== pool)))
  const [filter, setFilter] = useState('')
  const target = useSyncExternalStore(subscribeHash, linkedPool, () => null)
  const phone = useSyncExternalStore(subscribePhone, atPhoneWidth, () => false)

  // THE FILTER IS A PHONE CONTROL (#132). At 390 the families run to several
  // screens of rows; above 560px they fit, and a box nobody needs is noise.
  // A filter typed on a phone that is then turned wide stops applying rather
  // than hiding rows behind a box that is no longer drawn.
  const needle = phone ? filter.trim().toLowerCase() : ''
  const matches = (p: Pool) =>
    needle === '' ||
    p.name.toLowerCase().includes(needle) ||
    poolLabel(p.name).toLowerCase().includes(needle) ||
    // The open editor is never filtered away from under what was typed in it.
    p.name === editing?.pool
  const families = POOL_FAMILY_ORDER.flatMap((kind) => {
    const rows = pools
      .filter((p) => poolKind(p.name) === kind && matches(p))
      .sort((a, b) => a.name.localeCompare(b.name))
    return rows.length === 0 ? [] : [{ kind, rows }]
  })

  // TO THE ROW A LINK NAMED, once it is on screen. jsdom implements no
  // `scrollIntoView`, hence the guard.
  const landed = target !== null && pools.some((p) => p.name === target)
  useEffect(() => {
    if (!landed) return
    const row = document.getElementById(`limit-${target}`)
    if (row !== null && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center' })
  }, [landed, target])

  const close = (pool: string) => setEditing((e) => (e?.pool === pool ? null : e))

  // AN UNSAVED VALUE IS NOT THROWN AWAY BY A CLICK ELSEWHERE. Opening another
  // row replaces the one editor, so while the open row holds a typed value
  // that differs from its pool's own, every other row's `edit` is disabled:
  // the reader saves or cancels first, rather than losing the number without
  // a word.
  // A value being WRITTEN is not held: from the click on `save` until the
  // read-back lands it is on its way to the server, and closing its editor
  // loses nothing.
  const heldPool = editing?.draft != null ? pools.find((p) => p.name === editing.pool) : undefined
  const held =
    heldPool !== undefined && !writing.includes(heldPool.name) && editing!.draft!.trim() !== String(heldPool.hard_limit)
      ? heldPool.name
      : null

  return (
    <section className="section">
      <span className="ctl-eyebrow has-q">
        Ceilings
        {/* The consequence -- "lowering a ceiling evicts nothing" -- sits
            beside the open editor (#132), where the control it qualifies is;
            it used to be the table's caption, under every row whether or not
            anything was being changed. The argument behind it is behind the ?.
            THIS IS THE ONE GLYPH B7.4 LEFT ON THIS SCREEN, and it is the only
            one on it that survives the test: what a control DOES NOT do when
            you use it cannot be written into the control's own label without
            the label arguing with the button. Everything on this screen is a
            write, so it is the place a reader is most likely to act on a wrong
            expectation. */}
        <HelpCard topic="ceiling-change-evicts-nothing" />
      </span>
      {phone && (
        <div className="ctl-toolbar adm-filter">
          <input
            type="search"
            className="mono"
            placeholder="filter pools"
            aria-label="Filter pools by name"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
      )}
      {families.length === 0 && <p className="ctl-panel-note">no pool matches</p>}
      {/* GROUPED AS CAPACITY › POOLS GROUPS THEM (#132): the same families, in
          the same order, under the same headings, so a pool is found on the
          screen that edits it where it was found on the screen that showed it
          binding. It was one table sorted by name, where `global` sat between
          `backend:*` and `provider:*`. `.cap-families` is that board's own
          container, so the headings, the tables' borders and the phone
          scroll rules are its rules and not a copy of them. */}
      <div className="cap-families">
        {families.map(({ kind, rows }) => (
          <Family
            key={kind}
            kind={kind}
            pools={rows}
            saved={saved}
            editing={editing}
            target={target}
            held={held}
            onWriting={onWriting}
            onOpen={(pool) => setEditing({ pool, draft: null })}
            onDraft={(pool, draft) => {
              setEditing({ pool, draft })
              onEdit(pool)
            }}
            onClose={close}
            onSaved={onSaved}
          />
        ))}
      </div>
    </section>
  )
}

function Family({
  kind,
  pools,
  saved,
  editing,
  target,
  held,
  onWriting,
  onOpen,
  onDraft,
  onClose,
  onSaved,
}: {
  kind: PoolKind
  pools: Pool[]
  saved: Readonly<Record<string, SaveMark>>
  editing: Editing | null
  target: string | null
  /** The pool whose open editor holds an unsaved value, if one does. */
  held: string | null
  onWriting: (pool: string, on: boolean) => void
  onOpen: (pool: string) => void
  onDraft: (pool: string, draft: string) => void
  onClose: (pool: string) => void
  onSaved: (pool: string) => Promise<boolean>
}) {
  // `Set by` ONLY WHERE IT SAYS SOMETHING (#132). On a pool whose ceiling is
  // its configured value the column read `configured` on every row, which is
  // the column restating the Ceiling beside it. A family where every pool is
  // as configured draws no column; one where a quota or AIMD holds a pool
  // down draws it, with the word on that row alone.
  const showSetBy = pools.some((p) => setBy(p).term !== 'configured')
  return (
    <section className="ctl-card adm-family">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{FAMILY_TITLE[kind]}</h2>
      </div>
      <div className="ctl-card-body is-flush">
        <div className="ctl-table is-scroll">
          <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Pool</th>
                {/* The unit rides on the column name (§8.4.3) rather than in a
                    footnote under the table -- on BOTH figures (CP-24). This
                    said `In use` bare beside `Ceiling (units)` while Pools said
                    the opposite, so each screen labelled the other one's half. */}
                <th role="columnheader" scope="col" className="is-num">In use (units)</th>
                <th role="columnheader" scope="col" className="is-num">Ceiling (units)</th>
                {showSetBy && <th role="columnheader" scope="col">Set by</th>}
                <th role="columnheader" scope="col">Change</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {pools.map((p) => (
                <PoolRow
                  key={p.name}
                  pool={p}
                  mark={saved[p.name] ?? null}
                  showSetBy={showSetBy}
                  editing={editing?.pool === p.name ? editing : null}
                  target={target === p.name}
                  locked={held !== null && held !== p.name}
                  onWriting={(on) => onWriting(p.name, on)}
                  onOpen={() => onOpen(p.name)}
                  onDraft={(draft) => onDraft(p.name, draft)}
                  onClose={() => onClose(p.name)}
                  onSaved={() => onSaved(p.name)}
                />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}

function PoolRow({
  pool,
  mark,
  showSetBy,
  editing,
  target,
  locked,
  onWriting,
  onOpen,
  onDraft,
  onClose,
  onSaved,
}: {
  pool: Pool
  /** What the last save of this row said. Held by the screen, not the row (AH-7). */
  mark: SaveMark | null
  showSetBy: boolean
  /** This row's editor, when it is the one open. Held above the rows (AH-7). */
  editing: Editing | null
  /** The row a link named (#134). */
  target: boolean
  /** Another row's editor holds an unsaved value: this row's `edit` waits. */
  locked: boolean
  /** From the click on `save` until the read-back settles, or the write fails. */
  onWriting: (on: boolean) => void
  onOpen: () => void
  onDraft: (draft: string) => void
  onClose: () => void
  /** Resolves true once the pools have been read back after the write. */
  onSaved: () => Promise<boolean>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const messageId = useId()

  const open = editing !== null
  // WHAT SOMEBODY TYPED, OR THE POOL'S OWN VALUE. A row nobody has typed in
  // follows a re-read -- including one that picked up another operator's
  // change -- instead of showing the old limit as an edit a stray `save`
  // would write back.
  const value = editing?.draft ?? String(pool.hard_limit)
  const dirty = value.trim() !== String(pool.hard_limit)
  const parsed = Number(value)
  const valid = Number.isInteger(parsed) && parsed >= 0 && parsed <= 100_000
  const invalid = open && dirty && !valid
  const by = setBy(pool)
  const editable = isEditable(pool)

  const save = () => {
    if (!valid || busy) return
    setBusy(true)
    setError(null)
    onWriting(true)
    setPoolLimit(pool.name, parsed).then(async (r) => {
      setBusy(false)
      if (r.status === 'ok' || r.status === 'stale') {
        // The editor closes only once the read-back carries the value, so the
        // field never flicks back to the old limit in between. If the
        // read-back fails the editor stays open on what was written, beside
        // `not re-read`.
        const reread = await onSaved()
        onWriting(false)
        if (reread) onClose()
      } else {
        onWriting(false)
        if (r.status === 'error') setError(r.error)
      }
    })
  }

  const classes = [isPaused(pool) ? 'is-paused' : '', target ? 'is-target' : ''].filter(Boolean).join(' ')

  return (
    // THE ID A LINK NAMES (#132, #134): `limit-<pool>`, the raw pool name,
    // so the Tenants roster can point at `limit-tenant:<id>`.
    <tr role="row" id={`limit-${pool.name}`} className={classes === '' ? undefined : classes}>
      <th role="rowheader" scope="row" className="pool-name">
        {poolLabel(pool.name)}
        {/* The raw name, because it is what you paste into pool-limit.sh and a
            prettified label is not. */}
        <span className="ctl-sub">{pool.name}</span>
      </th>
      <td role="cell" data-label="In use (units)" className="is-num">{pool.active}</td>
      <td role="cell" data-label="Ceiling (units)" className="is-num">
        <span className="adm-ceiling">{pool.effective_limit}</span>
      </td>
      {showSetBy && (
        <td role="cell" data-label="Set by" title={by.term === 'configured' ? undefined : by.detail}>
          {by.term === 'configured' ? null : by.term}
        </td>
      )}
      <td role="cell" data-label="Change">
        <span className="limit-edit">
          {open ? (
            <>
              <input
                type="number"
                min={0}
                max={100000}
                value={value}
                disabled={busy || !editable}
                onChange={(e) => onDraft(e.target.value)}
                aria-label={`Hard limit for ${pool.name}`}
                // THE INPUT SAYS IT IS WRONG, AND WHY (AH-8). The range message
                // used to appear beside the control with nothing tying the two
                // together, so a keyboard or screen-reader user typing `-1` was
                // told nothing; the stylesheet draws the warn border off
                // `aria-invalid`.
                aria-invalid={invalid ? true : undefined}
                aria-describedby={invalid ? messageId : undefined}
              />
              <button onClick={save} disabled={!dirty || !valid || busy || !editable}>
                {busy ? 'saving…' : 'save'}
              </button>
              <button
                onClick={() => {
                  setError(null)
                  onClose()
                }}
                disabled={busy}
              >
                cancel
              </button>
            </>
          ) : (
            // A pool nothing can write gets no editor to open: an enabled
            // control that silently does nothing is worse than none. The
            // sentence is on the button, where a keyboard reader reaching it
            // gets it and a sighted reader gets the two-word marker instead.
            <button
              onClick={onOpen}
              disabled={!editable || locked}
              aria-label={
                !editable
                  ? `Edit ceiling for ${pool.name}. This pool kind has no write route, so it is read-only.`
                  : locked
                    ? `Edit ceiling for ${pool.name}. Another row has an unsaved value: save or cancel it first.`
                    : `Edit ceiling for ${pool.name}`
              }
              title={locked ? 'another row has an unsaved value' : undefined}
            >
              edit
            </button>
          )}
          {!editable && <span className="client-side">read-only</span>}
          {mark !== null && <span className="tag ok">saved</span>}
          {mark === 'unread' && (
            <span className="warn-text" title="The write succeeded; reading the pools back afterwards did not, so the figures in this row are from before it.">
              not re-read
            </span>
          )}
          {error && (
            <span className="warn-text" title={error.message}>
              {errorHeading(error)}
            </span>
          )}
        </span>
        {/* BELOW THE CONTROL ROW, NOT IN IT (AH-8). Inside the wrapping flex
            strip it arrived as one more item and reflowed every column of the
            table the moment a character was typed. */}
        {invalid && (
          <div className="warn-text limit-message" id={messageId}>
            0–100000
          </div>
        )}
        {/* BESIDE THE ONE EDITOR THAT IS OPEN (#132), because it qualifies
            that control and no other. */}
        {open && <div className="limit-note">lowering a ceiling evicts nothing</div>}
      </td>
    </tr>
  )
}

/**
 * Whether a route exists for this pool kind.
 *
 * Rendering an enabled input for a pool nothing can write would be a control
 * that silently does nothing — worse than no control. Every kind below has a
 * PUT under /v1/admin/limits/, including the two added on 2026-09-20:
 * backend, and the per-tenant slice of a provider.
 */
function isEditable(pool: Pool): boolean {
  const kind = poolKind(pool.name)
  if (kind === 'global' || kind === 'tenant' || kind === 'resource') return true
  if (kind === 'runner' || kind === 'backend') return true
  if (kind === 'provider') return true
  return false
}
