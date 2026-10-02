import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { loadCapacity, loadMe, setPoolLimit } from './api'
import { errorHeading, isPaused, type ApiError, type Result } from './fetch'
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
  type Me,
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
 * The per-profile cards that drew those operands here are gone (owner
 * decision, 2026-10-01): Pools > By runner profile draws every profile against
 * every pool, and the side editor states what a new limit does to each
 * profile's ceiling before it is saved.
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
  /**
   * WHO IS ASKING, from the session read the frame already makes (a `frame`
   * read, so it does not stand in for this screen's own newest read). A
   * non-admin reads every ceiling with Edit locked (decided 2026-10-01).
   * `null` is "not known yet, or the read failed": the write route is the
   * real gate, and it answers 403 with its own message.
   */
  const [admin, setAdmin] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    Promise.resolve(loadMe({ frame: true })).then(
      (r: Result<Me> | undefined) => {
        if (live && r !== undefined && (r.status === 'ok' || r.status === 'stale')) setAdmin(r.data.principal.is_admin)
      },
      () => {},
    )
    return () => {
      live = false
    }
  }, [])

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
          admin={admin}
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
  /**
   * The pool is in the response but has no `hard_limit` (#374). Its ceiling
   * is unknown, not 0 -- and admission refuses on it, so no figure the other
   * pools give is this profile's ceiling either.
   */
  unset: boolean
}

/** A pool's configured limit as the editor's text: empty when none was ever set (#374). */
function limitText(pool: Pool): string {
  return pool.hard_limit === null ? '' : String(pool.hard_limit)
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
): { operands: Operand[]; ceiling: number | null; binding: string[]; unset: boolean } {
  const w = units > 0 ? units : 1
  const operands: Operand[] = pools.map((pool) => {
    const p = byName.get(pool)
    if (p !== undefined && p.effective_limit === null) return { pool, agents: null, unset: true }
    return { pool, agents: p ? Math.floor(p.effective_limit as number / w) : null, unset: false }
  })

  // A POOL WITH NO LIMIT SET BINDS THE CARD (#374). Admission refuses on it,
  // so the smallest of the OTHER pools' ceilings would be a figure nothing
  // can reach; the card says "no limit set" and marks the pools to set.
  const unset = operands.filter((o) => o.unset).map((o) => o.pool)
  if (unset.length > 0) return { operands, ceiling: null, binding: unset, unset: true }

  const measured = operands.flatMap((o) => (o.agents === null ? [] : [o.agents]))
  const ceiling = measured.length === 0 ? null : Math.min(...measured)
  const binding = ceiling === null ? [] : operands.filter((o) => o.agents === ceiling).map((o) => o.pool)
  return { operands, ceiling, binding, unset: false }
}

function Body({
  capacity,
  admin,
  saved,
  onSaved,
  onEdit,
}: {
  capacity: Capacity
  admin: boolean | null
  saved: Readonly<Record<string, SaveMark>>
  onSaved: (pool: string) => Promise<boolean>
  onEdit: (pool: string) => void
}) {
  // THE PER-PROFILE "BINDING POOL" CARDS ARE GONE (owner decision,
  // 2026-10-01). Each drew one runner profile's ceiling in agents -- the
  // smallest of its pools' limits over its weight -- with every operand
  // listed. Pools > By runner profile (`ProfileMatrix`) draws every profile
  // against every pool with leased/ceiling per cell and outlines the pool that
  // runs out first, and the side editor here says what a new limit does to
  // each profile's ceiling (`impactOf`, which still reads `arithmetic`). The
  // cards held no control, so nothing a person could do went with them.
  return <PoolEditor capacity={capacity} admin={admin} saved={saved} onSaved={onSaved} onEdit={onEdit} />
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
  window.addEventListener('popstate', onChange)
  return () => {
    window.removeEventListener('hashchange', onChange)
    window.removeEventListener('popstate', onChange)
  }
}

function linkedPool(): string | null {
  if (typeof window === 'undefined') return null
  // The hash grammar first, then the real path's query (`/admin/limits?pool=`),
  // which is where the router leaves it once it has rewritten the hash.
  const hash = window.location.hash
  const at = hash.indexOf('?')
  if (at !== -1) return new URLSearchParams(hash.slice(at + 1)).get('pool')
  return new URLSearchParams(window.location.search).get('pool')
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
 * The pool being edited, and what has been typed for it. `draft` null means
 * the field shows the pool's own value off the latest read.
 *
 * HELD HERE, ABOVE THE ROWS AND THE SIDE EDITOR (AH-7). One editor is open at
 * a time (#132), and what is typed into it must survive anything that rebuilds
 * the rows -- a re-read after another pool's save, a filter that is changed
 * and changed back.
 */
interface Editing {
  pool: string
  draft: string | null
}

/**
 * A DRASTIC CHANGE: to 0, or a cut of half or more (admin-help.html, L2, the
 * owner's pick 2026-10-01). Those are the two changes that can silently stop a
 * whole class of work from starting, so the side editor asks for the pool's
 * name to be typed before it writes one. A raise, or a smaller cut, writes on
 * Save alone.
 */
export function isDrasticCut(from: number | null, to: number): boolean {
  // A pool with no limit set (#374) has nothing to be cut from: setting one is a raise.
  if (from === null || to >= from) return false
  return to === 0 || (from - to) * 2 >= from
}

/** The ceiling admission would apply with `hard` as the hard limit. */
function effectiveAt(p: Pool, hard: number): number {
  const caps = [hard, p.adaptive_target, p.quota_derived_limit].filter(
    (v): v is number => typeof v === 'number',
  )
  return Math.max(0, Math.min(...caps))
}

/**
 * WHAT THE CHANGE DOES, IN SENTENCES, FROM DATA THIS SCREEN ALREADY HAS: the
 * pool's own figures and every runner profile's ceiling before and after
 * (`arithmetic`, the minimum across a profile's pools). Nothing here is a
 * forecast of queue length -- the response carries no per-pool waiting count,
 * so the impact names ceilings and units in use, and nothing else.
 */
function impactOf(pool: Pool, next: number, capacity: Capacity): string[] {
  const after = effectiveAt(pool, next)
  const said: string[] = []

  if (after === pool.effective_limit) {
    said.push(`The ceiling admission applies stays ${after}: ${setBy(pool).term} holds it there.`)
  }

  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const changed = new Map(byName)
  changed.set(pool.name, { ...pool, effective_limit: after })
  const moved: string[] = []
  const held: string[] = []
  const profiles = Object.entries(capacity.runner_profiles)
    .filter(([, prof]) => prof.pools.includes(pool.name))
    .sort(([a], [b]) => a.localeCompare(b))
  for (const [name, prof] of profiles) {
    const before = arithmetic(prof.pools, prof.units, byName).ceiling
    const now = arithmetic(prof.pools, prof.units, changed)
    if (before !== now.ceiling) {
      moved.push(`${name} ${before ?? '—'} → ${now.ceiling ?? '—'}`)
    } else {
      const others = now.binding.filter((b) => b !== pool.name).map((b) => poolLabelAmong(b, prof.pools))
      held.push(others.length === 0 ? name : `${name} (${others.join(' and ')} bind${others.length === 1 ? 's' : ''})`)
    }
  }
  if (profiles.length === 0) said.push('No runner profile takes this pool.')
  if (moved.length > 0) said.push(`Max agents: ${moved.join(', ')}.`)
  if (held.length > 0) said.push(`Unchanged: ${held.join(', ')}.`)

  if (after === 0) {
    said.push(
      pool.active > 0
        ? `Nothing new is admitted to it. The ${pool.active} units in use are not stopped.`
        : 'Nothing new is admitted to it.',
    )
  } else if (after < pool.active) {
    said.push(
      `Nothing running is stopped: the ${pool.active} units in use finish, and new work waits until fewer than ${after} are in use.`,
    )
  } else if (pool.effective_limit !== null && after < pool.effective_limit) {
    said.push(`The ${pool.active} units in use fit under it; nothing is stopped.`)
  } else if (pool.effective_limit === null || after > pool.effective_limit) {
    said.push(`${after - pool.active} units free on this pool after the change.`)
  }
  return said
}

/** `20 → 14 · −30%`. No percentage off a zero. */
function deltaWords(from: number | null, to: number): string {
  if (from === null) return `no limit set → ${to}`
  if (from === 0) return `${from} → ${to}`
  const pct = Math.round(((to - from) / from) * 100)
  return `${from} → ${to} · ${pct < 0 ? '−' : '+'}${Math.abs(pct)}%`
}

/**
 * WHY "not recorded". Who changed a ceiling and when needs `admin_changed_by`
 * and `admin_changed_at` on the pool, which the API does not serve. The pool's
 * `updated_at` is NOT that: admission rewrites the pool document on every
 * lease, so it moves with traffic, and printing it as "last changed" would
 * name a time nobody changed anything.
 */
const NOT_RECORDED_WHY =
  'Not recorded: the API does not serve admin_changed_by or admin_changed_at yet, so who changed this ceiling, and when, is not known here.'

function PoolEditor({
  capacity,
  admin,
  saved,
  onSaved,
  onEdit,
}: {
  capacity: Capacity
  /** From the session read. null: not known (yet), and treated as allowed. */
  admin: boolean | null
  saved: Readonly<Record<string, SaveMark>>
  onSaved: (pool: string) => Promise<boolean>
  onEdit: (pool: string) => void
}) {
  const pools = capacity.pools
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
    // The open editor's row is never filtered away from under what was typed.
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

  // CLOSING THE EDITOR RETURNS FOCUS TO THE ROW'S EDIT CONTROL. The editor
  // unmounts with the focused control inside it, which would drop a keyboard
  // reader to the top of the document. The focus is moved after the commit, in
  // the effect below, because the Edit button is disabled while another pool's
  // unsaved value is held and is enabled only once the editor is gone.
  const refocus = useRef<string | null>(null)
  const close = (pool: string) => {
    refocus.current = pool
    setEditing((e) => (e?.pool === pool ? null : e))
  }
  useEffect(() => {
    if (editing !== null || refocus.current === null) return
    const row = document.getElementById(`limit-${refocus.current}`)
    refocus.current = null
    row?.querySelector<HTMLButtonElement>('.limit-edit button')?.focus()
  }, [editing])

  // AN UNSAVED VALUE IS NOT THROWN AWAY BY A CLICK ELSEWHERE. Opening another
  // pool replaces the one editor, so while the open editor holds a typed value
  // that differs from its pool's own, every other row's `edit` is disabled:
  // the reader saves or cancels first, rather than losing the number without
  // a word. A value being WRITTEN is not held: from the click on Save until
  // the read-back lands it is on its way to the server, and closing its
  // editor loses nothing.
  const open = editing !== null ? pools.find((p) => p.name === editing.pool) : undefined
  const draft = editing?.draft ?? null
  const held =
    open !== undefined && draft !== null && !writing.includes(open.name) && draft.trim() !== limitText(open)
      ? open.name
      : null

  return (
    <section className="section">
      <span className="ctl-eyebrow has-q">
        Ceilings
        {/* What a write here DOES NOT do -- evict anything -- is the one
            thing on this screen a reader is likely to get wrong before
            acting, so it keeps the screen's one `?`. The same fact is said
            again inside the open editor, beside the Save it qualifies. */}
        <HelpCard topic="ceiling-change-evicts-nothing" />
      </span>
      {admin === false && (
        // NON-ADMINS READ EVERYTHING (decided 2026-10-01): every ceiling is on
        // screen, Edit is locked, and this one line says why.
        <p className="ctl-panel-note adm-locked">
          Admins only. You can read every ceiling; changing one needs the platform admin group.
        </p>
      )}
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
      {/* THE TABLE STAYS STILL WHILE EDITING (L2). The editor is a side panel
          beside the families rather than a field inside a row, so opening it
          reflows no column, and it has room for the impact and the history. */}
      <div className={`adm-split${open !== undefined ? ' is-editing' : ''}`}>
        {/* GROUPED AS CAPACITY › POOLS GROUPS THEM (#132): the same families,
            in the same order, under the same headings. `.cap-families` is that
            board's own container, so the headings, borders and phone scroll
            rules are its rules and not a copy of them. */}
        <div className="cap-families">
          {families.map(({ kind, rows }) => (
            <Family
              key={kind}
              kind={kind}
              pools={rows}
              saved={saved}
              editing={editing?.pool ?? null}
              target={target}
              held={held}
              admin={admin}
              onOpen={(pool) => setEditing({ pool, draft: null })}
            />
          ))}
        </div>
        {open !== undefined && (
          <SideEditor
            // A fresh editor per pool: its busy flag, its error and its typed
            // confirmation belong to the pool they were for.
            key={open.name}
            pool={open}
            capacity={capacity}
            draft={draft}
            admin={admin}
            onDraft={(draft) => {
              setEditing({ pool: open.name, draft })
              onEdit(open.name)
            }}
            onClose={() => close(open.name)}
            onWriting={(on) => onWriting(open.name, on)}
            onSaved={() => onSaved(open.name)}
          />
        )}
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
  admin,
  onOpen,
}: {
  kind: PoolKind
  pools: Pool[]
  saved: Readonly<Record<string, SaveMark>>
  /** The pool whose side editor is open, if one is. */
  editing: string | null
  target: string | null
  /** The pool whose open editor holds an unsaved value, if one does. */
  held: string | null
  admin: boolean | null
  onOpen: (pool: string) => void
}) {
  // `Set by` ONLY WHERE IT SAYS SOMETHING (#132). On a pool whose ceiling is
  // its configured value the column read `configured` on every row, which is
  // the column restating the Ceiling beside it.
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
                {/* The unit rides on the column name (§8.4.3), on BOTH figures
                    (CP-24). */}
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
                  open={editing === p.name}
                  target={target === p.name}
                  locked={held !== null && held !== p.name}
                  admin={admin}
                  onOpen={() => onOpen(p.name)}
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
  open,
  target,
  locked,
  admin,
  onOpen,
}: {
  pool: Pool
  /** What the last save of this row said. Held by the screen, not the row (AH-7). */
  mark: SaveMark | null
  showSetBy: boolean
  /** This pool's side editor is the one open. */
  open: boolean
  /** The row a link named (#134). */
  target: boolean
  /** Another pool's editor holds an unsaved value: this row's `edit` waits. */
  locked: boolean
  admin: boolean | null
  onOpen: () => void
}) {
  const by = setBy(pool)
  const editable = isEditable(pool)
  const refused = admin === false
  const classes = [isPaused(pool) ? 'is-paused' : '', target ? 'is-target' : '', open ? 'is-editing' : '']
    .filter(Boolean)
    .join(' ')

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
        {pool.effective_limit === null ? (
          <span className="adm-ceiling is-unset" title={by.detail}>no limit set</span>
        ) : (
          <span className="adm-ceiling">{pool.effective_limit}</span>
        )}
      </td>
      {showSetBy && (
        <td role="cell" data-label="Set by" title={by.term === 'configured' ? undefined : by.detail}>
          {by.term === 'configured' ? null : by.term}
        </td>
      )}
      <td role="cell" data-label="Change">
        <span className="limit-edit">
          {/* A pool nothing can write gets no editor to open: an enabled
              control that silently does nothing is worse than none. The
              sentence is on the button, where a keyboard reader reaching it
              gets it and a sighted reader gets the two-word marker instead. */}
          <button
            type="button"
            onClick={onOpen}
            disabled={!editable || locked || refused}
            aria-expanded={open}
            aria-label={
              !editable
                ? `Edit ceiling for ${pool.name}. This pool kind has no write route, so it is read-only.`
                : refused
                  ? `Edit ceiling for ${pool.name}. Admins only: changing a ceiling needs the platform admin group.`
                  : locked
                    ? `Edit ceiling for ${pool.name}. Another pool has an unsaved value: save or cancel it first.`
                    : `Edit ceiling for ${pool.name}`
            }
            title={locked ? 'another pool has an unsaved value' : refused ? 'admins only' : undefined}
          >
            edit
          </button>
          {!editable && <span className="client-side">read-only</span>}
          {mark !== null && <span className="tag ok">saved</span>}
          {mark === 'unread' && (
            <span className="warn-text" title="The write succeeded; reading the pools back afterwards did not, so the figures in this row are from before it.">
              not re-read
            </span>
          )}
        </span>
      </td>
    </tr>
  )
}

/**
 * THE SIDE EDITOR (admin-help.html L2, the owner's pick 2026-10-01): the pool's
 * figures, the new ceiling, what the change does in a sentence, a typed
 * confirmation for a drastic change, and the pool's history.
 */
function SideEditor({
  pool,
  capacity,
  draft,
  admin,
  onDraft,
  onClose,
  onWriting,
  onSaved,
}: {
  pool: Pool
  capacity: Capacity
  draft: string | null
  admin: boolean | null
  onDraft: (draft: string) => void
  onClose: () => void
  /** From the click on Save until the read-back settles, or the write fails. */
  onWriting: (on: boolean) => void
  /** Resolves true once the pools have been read back after the write. */
  onSaved: () => Promise<boolean>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [typed, setTyped] = useState('')
  const messageId = useId()
  const titleId = useId()

  // WHAT SOMEBODY TYPED, OR THE POOL'S OWN VALUE. An editor nobody has typed
  // in follows a re-read -- including one that picked up another operator's
  // change -- instead of showing the old limit as an edit a stray Save would
  // write back.
  const value = draft ?? limitText(pool)
  const dirty = value.trim() !== limitText(pool)
  const parsed = Number(value)
  // An empty field is not 0: `Number('')` is 0, and a cleared field must not
  // be one click from closing the pool.
  const valid = value.trim() !== '' && Number.isInteger(parsed) && parsed >= 0 && parsed <= 100_000
  const invalid = dirty && !valid
  const next = dirty && valid ? parsed : null
  // MEASURED FROM THE CEILING ADMISSION APPLIES -- the "Ceiling" this editor
  // shows -- not the hard limit. With AIMD or quota holding a hard limit of 20
  // at 8, 20 -> 10 changes nothing admission does and 8 -> 3 is the cut that
  // stops work; comparing against 20 asked for the first and judged the
  // second against the wrong base.
  const drastic = next !== null && isDrasticCut(pool.effective_limit, next)
  const confirmed = !drastic || typed === pool.name
  const editable = isEditable(pool) && admin !== false

  const save = () => {
    if (next === null || busy || !confirmed || !editable) return
    setBusy(true)
    setError(null)
    onWriting(true)
    setPoolLimit(pool.name, next).then(async (r) => {
      setBusy(false)
      if (r.status === 'ok' || r.status === 'stale') {
        // The editor closes only once the read-back carries the value, so the
        // field never flicks back to the old limit in between. If the
        // read-back fails the editor stays open on what was written, and the
        // row says `not re-read`.
        const reread = await onSaved()
        onWriting(false)
        if (reread) onClose()
      } else {
        onWriting(false)
        if (r.status === 'error') setError(r.error)
      }
    })
  }

  const impact = next === null ? [] : impactOf(pool, next, capacity)

  return (
    <aside
      className="adm-side"
      aria-labelledby={titleId}
      data-pool={pool.name}
      onKeyDown={(e) => {
        // Escape closes the editor -- but not while a write is in flight: the
        // value is already on its way, and closing would hide the outcome.
        if (e.key !== 'Escape' || busy || e.defaultPrevented) return
        e.preventDefault()
        setError(null)
        onClose()
      }}
    >
      <h3 className="adm-side-title" id={titleId}>
        {poolLabel(pool.name)}
        <span className="ctl-sub">{pool.name}</span>
      </h3>
      <dl className="adm-side-facts">
        <dt>In use</dt>
        <dd>{pool.active} units</dd>
        <dt>Ceiling</dt>
        <dd>{pool.effective_limit === null ? 'no limit set' : `${pool.effective_limit} units`}</dd>
        {pool.effective_limit !== pool.hard_limit && pool.hard_limit !== null && (
          <>
            <dt>Hard limit</dt>
            <dd>{pool.hard_limit}</dd>
          </>
        )}
        <dt>Last changed</dt>
        <dd className="adm-not-recorded" title={NOT_RECORDED_WHY}>
          not recorded
        </dd>
      </dl>

      <div className="adm-side-field">
        <label className="adm-side-k" htmlFor={`${titleId}-new`}>
          New ceiling
        </label>
        <input
          id={`${titleId}-new`}
          type="number"
          min={0}
          max={100000}
          value={value}
          disabled={busy || !editable}
          onChange={(e) => onDraft(e.target.value)}
          aria-label={`Hard limit for ${pool.name}`}
          // THE INPUT SAYS IT IS WRONG, AND WHY (AH-8): the stylesheet draws
          // the warn border off `aria-invalid`, and the range is the message
          // the field points at.
          aria-invalid={invalid ? true : undefined}
          aria-describedby={invalid ? messageId : undefined}
          autoFocus
        />
        {next !== null && <span className="adm-delta">{deltaWords(pool.hard_limit, next)}</span>}
      </div>
      {/* BELOW THE FIELD, ON A LINE OF ITS OWN (AH-8). */}
      {invalid && (
        <div className="warn-text limit-message" id={messageId}>
          0–100000
        </div>
      )}

      {impact.length > 0 && (
        <>
          <span className="adm-side-k">Impact</span>
          <p className="adm-impact">{impact.join(' ')}</p>
        </>
      )}
      {/* WHAT A WRITE HERE DOES NOT DO, beside the control it qualifies. */}
      <div className="limit-note">lowering a ceiling evicts nothing</div>

      {drastic && (
        <div className="adm-drastic">
          <p className="adm-drastic-why">This is a drastic change: to 0, or a cut of half or more.</p>
          <label className="adm-side-k" htmlFor={`${titleId}-confirm`}>
            Type <b className="mono">{pool.name}</b> to confirm
          </label>
          <input
            id={`${titleId}-confirm`}
            type="text"
            className="mono"
            autoComplete="off"
            spellCheck={false}
            value={typed}
            disabled={busy}
            onChange={(e) => setTyped(e.target.value)}
            aria-label={`Type ${pool.name} to confirm`}
          />
        </div>
      )}

      <div className="adm-side-actions">
        <button
          type="button"
          className={drastic ? 'is-primary is-danger' : 'is-primary'}
          onClick={save}
          disabled={next === null || busy || !editable || !confirmed}
        >
          {busy ? 'Saving…' : drastic ? `Set to ${next}` : 'Save'}
        </button>
        <button
          type="button"
          onClick={() => {
            setError(null)
            onClose()
          }}
          disabled={busy}
        >
          Cancel
        </button>
        {error && (
          <span className="warn-text" title={error.message}>
            {errorHeading(error)}
          </span>
        )}
      </div>

      <span className="adm-side-k">History</span>
      <p className="adm-not-recorded" title={NOT_RECORDED_WHY}>
        not recorded
      </p>
    </aside>
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
