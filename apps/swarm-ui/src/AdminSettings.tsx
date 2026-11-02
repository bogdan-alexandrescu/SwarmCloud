import { Fragment, useCallback, useEffect, useId, useRef, useState, useSyncExternalStore, type FormEvent, type ReactNode } from 'react'
import {
  BUDGET_DOES, budgetCeilingOf, budgetLimitsWords, loadAdminPools, loadAdminQuota, loadCapacity, loadMe, loadModelClasses, loadModelPrices, loadPlatformSettings,
  parseBudgetAmount, priceRefusals, resetAdaptiveTarget, saveBudgetCeiling, saveModelPrices, setPoolLimit, setQuotaHardMax, storeOwnerToken,
  type BudgetLimits, type BudgetUnit,
} from './api'
import { Button, Card, CIcon } from './components'
import { errorHeading, isPaused, route, write, type ApiError, type Result } from './fetch'
import { Refusal } from './GitHubConnect'
import { ACCESS } from './Onboarding'
import { addressToPath } from './paths'
import { HelpCard } from './HelpCard'
import { POOLS_POLL_MS, capacityPoll, comparePools, poolViewer, tableMode, usePhoneTables } from './capacityPoll'
import { Screen, timeAgo } from './Shell'
import {
  FAMILY_TITLE,
  POOL_FAMILY_ORDER,
  poolKind,
  poolLabel,
  poolLabelAmong,
  setBy,
  unitsIn,
  type AdminPool,
  type Capacity,
  type Me,
  type ModelPrice,
  type ModelPriceChange,
  type ModelPriceRate,
  type ModelPriceTable,
  type ModelPriceTier,
  type OwnerTokenResponse,
  type PlatformSettings,
  type Pool,
  type PoolKind,
  type QuotaState,
  type Weight,
} from './types'
import { AGE_TICK_MS, useNow } from './useNow'
// The Config pages' lead (`.ws-lead`, WS-SETTINGS-PAGE), which every Admin
// page's lead draws with (`AdminLead` below).
import './styles/workspace-settings.css'
import './styles/admin.css'

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
  /** What each pool's last save's RESPONSE said the ceiling became, against what was asked. */
  const [outcomes, setOutcomes] = useState<Readonly<Record<string, SaveOutcome>>>({})
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

  const reread = async (over: Capacity, pool: string, outcome: SaveOutcome): Promise<boolean> => {
    setOutcomes((all) => ({ ...all, [pool]: outcome }))
    mark(pool, 'rereading')
    // With the record (#133), so the row names the change just made.
    const r = await loadLimits()
    if (r.status === 'ok' || r.status === 'stale') {
      setFresh({ over, data: r.data })
      mark(pool, 'saved')
      return true
    }
    mark(pool, 'unread')
    return false
  }

  const read = useLeadRead(loadLimits)

  return (
    <>
      <Screen
        // "Pool limits", not "Admin settings". The tab says Pool limits and it
        // is the accurate one twice over: this screen edits concurrency ceilings
        // and nothing else, so "Admin settings" over-claimed a settings page
        // that does not exist, and it restated the section it already sits under
        // ("Admin") instead of naming the thing on the screen.
        title="Pool limits"
        load={read.load}
        // READ ON CAPACITY'S CADENCE (live QA ADM-7, 2026-10-11): read once,
        // In use went stale -- global 14 here while the sidebar's live read
        // said 10 -- and the open editor's impact reasoned from the stale 14.
        // A re-read hands down a new object, which wins over a save's
        // read-back as the newer one; the open editor and what is typed in it
        // are held above the rows (AH-7), so a re-read loses neither.
        pollMs={capacityPoll(POOLS_POLL_MS)}
        // A count, not a promise. "changes take effect immediately" was a
        // rationale in the one slot on this screen a reader cannot skip.
        summary={(d) =>
          `${d.pools.length} ${d.pools.length === 1 ? 'pool' : 'pools'} · ${Object.keys(d.runner_profiles).length} ${Object.keys(d.runner_profiles).length === 1 ? 'profile' : 'profiles'}`
        }
        empty={{
          heading: 'No pools exist',
          body: 'Pools are created at provisioning time.',
        }}
      >
        {(d) => (
          <Body
            capacity={fresh !== null && fresh.over === d ? fresh.data : d}
            unread={read.failed}
            admin={admin}
            saved={saved}
            outcomes={outcomes}
            onSaved={(pool, outcome) => reread(d, pool, outcome)}
            onEdit={(pool) => {
              mark(pool, null)
              setOutcomes((all) => {
                const next = { ...all }
                delete next[pool]
                return next
              })
            }}
          />
        )}
      </Screen>
      {read.failed !== null && !read.held && read.failed.kind !== 'admin_required' && <PoolLeadView lead={null} unread={read.failed} />}
      {/* FOLDED UNDER THE CEILINGS (VQ-054, part of #2391; config-e.html
          S10b): the page is the ceilings table, and these four are opened
          when wanted. Each card drawn on its own is open (`FoldCard`). */}
      <AdditionalRepositoryLimitsCard fold />
      <AgentBudgetCeilingCard fold />
      <ModelPricesCard admin={admin} fold />
      <FallbackTokenCard fold />
    </>
  )
}

/**
 * AN ADMIN PAGE'S LEAD (ADMIN-LEADS; part of #2157, proposal E S10): what
 * needs an admin now, in one sentence with at most one button, and a green
 * "Nothing waiting" when that is the truth. Pool limits, Runner sizes and
 * Tenants draw it.
 *
 * THE CONFIG PAGES' LEAD, NOT A NEW ONE. It is drawn with `.ws-lead`, the
 * lead Config › Workspace Settings opens with (WorkspaceSettings.tsx `Lead`,
 * styles/workspace-settings.css), so a later tint of that class (lane
 * CONFIG-LEAD-TINT) reaches these too. `adm-lead` adds the two tones the
 * Config pages have no use for (styles/admin.css): `warn`, something an admin
 * has to act on, and `unread`.
 *
 * `unread` IS A DASH WITH ITS REASON, NEVER A GREEN LEAD (the honesty rule,
 * docs/web-ui/redesign-v2.md: an em dash means "not measured"). A page whose
 * read failed has not judged anything, and "Nothing waiting" over a read that
 * never landed would be the one claim nobody made.
 */
export type AdminLeadTone = 'warn' | 'next' | 'ok' | 'unread'

export function AdminLead({
  kicker,
  title,
  sub,
  body,
  tone,
  action,
}: {
  kicker: string
  title: string
  /** An id under the title, in mono (`.id`): the key a title names by its label. */
  sub?: string
  body?: ReactNode
  tone: AdminLeadTone
  action?: ReactNode
}) {
  return (
    <section className={`ws-lead adm-lead is-${tone}`} aria-label={kicker} data-lead={tone}>
      <div className="ws-lead-t">
        <span className="ws-lead-k">{kicker}</span>
        <b>{title}</b>
        {sub !== undefined && <span className="ws-lead-s id">{sub}</span>}
        {body !== undefined && <span className="ws-lead-s">{body}</span>}
      </div>
      {action !== undefined && <div className="ws-lead-a">{action}</div>}
    </section>
  )
}

/** The dash lead: what could not be read, and the API's reason in its own heading. */
export function UnreadLead({ what, error }: { what: string; error: ApiError }) {
  return <AdminLead tone="unread" kicker="Not read" title={`—, ${what} could not be read`} body={`${errorHeading(error)}.`} />
}

/**
 * WHETHER A `Screen`'S READ FAILED, for its lead. `Screen` keeps its result to
 * itself, so its `load` is wrapped: `failed` is the last read's error (null
 * once one lands), and `held` says whether rows from an earlier read are still
 * on screen -- `Screen`'s stale rule, which hands those rows to its children.
 * With rows, the lead inside them draws the dash; without, `Screen` draws its
 * failure panel and no children, and the page draws the dash under it.
 *
 * The wrapper calls `load` synchronously, so `Screen`'s `pageReads` still
 * counts the read as the page's.
 */
export function useLeadRead<T>(load: () => Promise<Result<T>>): {
  load: () => Promise<Result<T>>
  failed: ApiError | null
  held: boolean
} {
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [held, setHeld] = useState(false)
  // A read that lands after the page has gone sets nothing: there is no lead
  // left to draw, and a test's torn-down document has no window to schedule on.
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
    }
  }, [])
  const wrapped = useCallback(
    () =>
      load().then((r) => {
        if (!live.current) return r
        if (r?.status === 'error') setFailed(r.error)
        else if (r?.status === 'ok' || r?.status === 'stale') {
          setFailed(null)
          setHeld(true)
        } else if (r?.status === 'empty') {
          setFailed(null)
          setHeld(false)
        }
        return r
      }),
    // `load` is a module function on every page that uses this, so it never changes.
    [],
  )
  return { load: wrapped, failed, held }
}

/**
 * NEAR A CEILING: 80% of it in use, or more. Under that a pool still has a
 * fifth of its ceiling free, which is room for the next tasks rather than a
 * reason to call an admin; the mock-up's lead (S10b) is a pool at 92%.
 */
export const NEAR_CEILING = 0.8

/**
 * What Pool limits leads with (S10b). `unset` first: a pool with no ceiling
 * set refuses every task that takes it (#374), which is worse than any busy
 * pool. Then the pool with the largest share of its ceiling in use, if it is
 * near it. A paused pool (`enabled: false`) is held on purpose and is not
 * ranked, nor is a ceiling of 0 with nothing in use -- nothing is waiting on
 * either.
 */
/**
 * `label` IS THE NAME THE TABLE GIVES THE POOL (visual QA pass 1,
 * admin-shell-03, part of #2596): the lead read "Pool
 * provider:anthropic:tenant:u-bogdan is at 100%" over a row named
 * `anthropic · u-bogdan`. The key stays `pool`, for the button's target and
 * the lead's mono sub-line.
 */
export type PoolLead =
  | { kind: 'unset'; pool: string; label: string }
  | { kind: 'near'; pool: string; label: string; active: number; ceiling: number; share: number }
  | { kind: 'clear' }

export function poolLead(pools: readonly Pool[]): PoolLead {
  const names = pools.map((p) => p.name)
  const unset = pools.find((p) => p.enabled !== false && p.effective_limit === null)
  if (unset !== undefined) return { kind: 'unset', pool: unset.name, label: poolLabelAmong(unset.name, names) }
  let top: { pool: string; active: number; ceiling: number; share: number } | null = null
  for (const p of pools) {
    if (p.enabled === false || p.effective_limit === null) continue
    const ceiling = p.effective_limit
    if (ceiling <= 0 && p.active <= 0) continue
    const share = ceiling <= 0 ? Number.POSITIVE_INFINITY : p.active / ceiling
    if (top === null || share > top.share || (share === top.share && p.active > top.active)) top = { pool: p.name, active: p.active, ceiling, share }
  }
  if (top !== null && top.share >= NEAR_CEILING) return { kind: 'near', ...top, label: poolLabelAmong(top.pool, names) }
  return { kind: 'clear' }
}

const percent = (share: number) => (Number.isFinite(share) ? `${Math.round(share * 100)}%` : 'over')

/**
 * Pool limits' lead, drawn. `onOpen` opens the named pool's side editor (L2);
 * absent -- a non-admin, for whom Edit is locked -- the lead has no button.
 */
function PoolLeadView({
  lead,
  unread,
  onOpen,
  openWhy,
}: {
  lead: PoolLead | null
  unread: ApiError | null
  onOpen?: (pool: string) => void
  /** Why the button waits: another pool's editor holds an unsaved value. */
  openWhy?: string
}) {
  if (unread !== null) return <UnreadLead what="pool use" error={unread} />
  if (lead === null) return null
  if (lead.kind === 'clear') {
    // No second sentence: the table under it carries every pool's figures,
    // and Pool limits is held to a prose budget (prose.operator.test.tsx).
    return <AdminLead tone="ok" kicker="Nothing waiting" title="No pool is near its ceiling" />
  }
  const button =
    onOpen === undefined ? undefined : (
      <Button kind="primary" disabledReason={openWhy} onClick={() => onOpen(lead.pool)}>
        {`Open ${lead.label}`}
      </Button>
    )
  // The key under the name only when it adds something, as the row and the
  // side editor draw it: `global` would read "global / global".
  const key = lead.label === lead.pool ? undefined : lead.pool
  if (lead.kind === 'unset') {
    return (
      <AdminLead
        tone="warn"
        kicker="Needs a look"
        title={`Pool ${lead.label} has no ceiling set`}
        sub={key}
        body="Admission refuses every task that takes it until one is."
        action={button}
      />
    )
  }
  return (
    <AdminLead
      tone="warn"
      kicker="Needs a look"
      title={`Pool ${lead.label} is at ${percent(lead.share)} of its ceiling`}
      sub={key}
      body={`${lead.active} of ${lead.ceiling} units in use.`}
      action={button}
    />
  )
}

/**
 * WHERE THE RECORD OF WHO CHANGED A POOL CAME FROM (#133), per pool.
 *
 *   served       the admin pool read answered: the pool's record is what it
 *                served, and null fields mean no admin route has changed it
 *   admins-only  the admin pool read refused (403): the record exists for
 *                admins, and saying "not recorded" would be untrue
 *   unread       the admin pool read failed, or this pool was not in it
 */
type RecordRead = 'served' | 'admins-only' | 'unread'

/** A pool as this screen draws it: `/v1/capacity`'s, with the admin record joined on. */
type LimitPool = Pool & Partial<Pick<AdminPool, 'admin_changed_by' | 'admin_changed_at' | 'admin_change'>> & {
  record?: RecordRead
  /**
   * The quota document a `provider:<p>:tenant:<t>` pool is fed by, from
   * `GET /v1/admin/quota` (POOL-LIMITS-QUOTA). Absent: not a provider-tenant
   * pool, no such document, or the read was refused or failed -- the row
   * then draws only what `/v1/capacity` served.
   */
  quota?: QuotaState
}

/**
 * `/v1/capacity`, WITH WHO CHANGED EACH POOL JOINED ON FROM `/v1/admin/pools`
 * (#133), by pool name.
 *
 * TWO READS, NOT ONE. The record is served on the admin read only, because
 * `/v1/capacity` serves pools to every tenant member and an admin's email is
 * not tenant data. Reading `/v1/capacity` alone is why every row once said
 * "not recorded" against an API that held the record. The figures stay
 * `/v1/capacity`'s -- the profiles' arithmetic is taken over them -- and the
 * admin read contributes the three record fields and nothing else.
 *
 * The admin read failing never fails the screen: the ceilings are still
 * editable, and the rows say the record was not read.
 */
export async function loadLimits(): Promise<Result<Capacity>> {
  const [cap, admin, quota] = await Promise.all([
    loadCapacity(),
    Promise.resolve(loadAdminPools()).catch(() => undefined),
    // THE THIRD READ, FOR THE PROVIDER-TENANT ROWS (POOL-LIMITS-QUOTA, BGD
    // 2026-10-25): their ceiling is the least of the hard limit this screen
    // edits, the broker's configured max and the AIMD target, and only this
    // admin read serves the last two. It failing, or being refused, never
    // fails the screen: those rows draw what `/v1/capacity` served.
    Promise.resolve()
      .then(() => loadAdminQuota())
      .catch(() => undefined),
  ])
  if (cap === undefined || (cap.status !== 'ok' && cap.status !== 'stale')) return cap
  const served = admin !== undefined && (admin.status === 'ok' || admin.status === 'stale' || admin.status === 'empty')
  const refused = admin !== undefined && admin.status === 'error' && admin.error.kind === 'admin_required'
  const byName = new Map(
    admin !== undefined && (admin.status === 'ok' || admin.status === 'stale') ? admin.data.pools.map((p) => [p.name, p]) : [],
  )
  const quotas = new Map(
    quota !== undefined && (quota.status === 'ok' || quota.status === 'stale')
      ? quota.data.quota.map((q) => [`provider:${q.provider}:tenant:${q.tenant_id}`, q])
      : [],
  )
  const pools = cap.data.pools.map((p): LimitPool => {
    const q = quotas.get(p.name)
    const fed = q === undefined ? p : { ...p, quota: q }
    if (refused) return { ...fed, record: 'admins-only' }
    const found = byName.get(p.name)
    if (found === undefined) return { ...fed, record: served && byName.size === 0 ? 'served' : 'unread' }
    return {
      ...fed,
      admin_changed_by: found.admin_changed_by,
      admin_changed_at: found.admin_changed_at,
      admin_change: found.admin_change,
      record: 'served',
    }
  })
  return { ...cap, data: { ...cap.data, pools } }
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
 * What a save's RESPONSE said, against what was asked: `moved` only when the
 * pool it returned has the hard limit that was typed.
 *
 * WHY THE RESPONSE IS COMPARED (2026-10-07). The owner raised tenant smoke's
 * ceiling 8 -> 20 here. The route answered 200 with the pool still at 8 --
 * `capacity_units` held it there -- and this screen said saved, because a 200
 * was all it read. Every pool kind `setPoolLimit` routes returns the pool, so
 * every save is checked, not only a tenant's. A response that carries no pool
 * cannot confirm anything and is not a success either.
 */
export interface SaveOutcome {
  moved: boolean
  /** `tenant:smoke ceiling 8 → 20`, or `Saved, but the ceiling is still 8: …`. */
  text: string
}

/** The response's own words for what held a pool below the limit (`capped_by`, routes/admin.py). */
const CAPPED_BY: Readonly<Record<string, string>> = {
  capacity_units: "the tenant's capacity_units",
  max_active: "the tenant's max_active",
  unknown: 'something the API could not name',
}

export function saveOutcome(name: string, from: number | null, asked: number, data: unknown): SaveOutcome {
  const body = typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {}
  const pool = typeof body.pool === 'object' && body.pool !== null ? (body.pool as Record<string, unknown>) : null
  const now = pool === null ? undefined : pool.hard_limit
  if (now === asked) {
    return { moved: true, text: `${name} ceiling ${from === null ? 'no limit set' : from} → ${asked}` }
  }
  if (typeof now !== 'number') {
    return {
      moved: false,
      text:
        pool === null
          ? `Saved, but the response did not return ${name}, so the new ceiling is not confirmed`
          : `Saved, but ${name} has no ceiling set`,
    }
  }
  const cap = typeof body.capped_by === 'string' ? body.capped_by : null
  const tenant = typeof body.tenant === 'object' && body.tenant !== null ? (body.tenant as Record<string, unknown>) : {}
  const held = cap === null ? null : tenant[cap]
  const reason =
    cap === null
      ? 'the response gave no reason'
      : `capped by ${CAPPED_BY[cap] ?? cap}${typeof held === 'number' ? ` (${held})` : ''}`
  return { moved: false, text: `Saved, but the ceiling is still ${now}: ${reason}` }
}

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
 *
 * EACH POOL'S LIMIT IS DIVIDED BY WHAT ONE AGENT TAKES IN THAT POOL
 * (`unitsIn`, contract request 129, #2950): `units` is the profile, whose
 * served `pool_units` puts a provider pool at 1. A provider limit of 50 is
 * 50 large agents, not 12.
 */
function arithmetic(
  pools: string[],
  units: Weight,
  byName: Map<string, Pool>,
): { operands: Operand[]; ceiling: number | null; binding: string[]; unset: boolean } {
  const operands: Operand[] = pools.map((pool) => {
    const p = byName.get(pool)
    if (p !== undefined && p.effective_limit === null) return { pool, agents: null, unset: true }
    const share = unitsIn(units, pool)
    const w = share !== null && share > 0 ? share : 1
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
  unread,
  admin,
  saved,
  outcomes,
  onSaved,
  onEdit,
}: {
  capacity: Capacity
  /** The last capacity read failed: these rows are an earlier read's, and the lead is a dash. */
  unread: ApiError | null
  admin: boolean | null
  saved: Readonly<Record<string, SaveMark>>
  outcomes: Readonly<Record<string, SaveOutcome>>
  onSaved: (pool: string, outcome: SaveOutcome) => Promise<boolean>
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
  return (
    <PoolEditor capacity={capacity} unread={unread} admin={admin} saved={saved} outcomes={outcomes} onSaved={onSaved} onEdit={onEdit} />
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
 * WHERE THE SIDE EDITOR FLOATS BESIDE THE FAMILIES (VQA V024/V025): the width
 * `.adm-split.is-editing` (styles/admin.css) lays the editor over the right of
 * the families from. Below it the editor is drawn in the flow, under the row
 * it edits. Kept equal to the stylesheet's `min-width: 1280px`.
 */
const BESIDE_QUERY = '(min-width: 1280px)'

function subscribeBeside(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(BESIDE_QUERY)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

/** True where there is no `matchMedia` (jsdom): the desktop's answer, the editor beside. */
function atBesideWidth(): boolean {
  return typeof window === 'undefined' || typeof window.matchMedia !== 'function' || window.matchMedia(BESIDE_QUERY).matches
}

/**
 * MORE POOLS THAN A SCREEN HOLDS GET THE FILTER AT EVERY WIDTH (VQA V077). It
 * was a phone control only, and a platform with 59 pools ran to several
 * screens of rows at 1440 with nothing to narrow them by. Twenty rows is
 * about what one 900px-tall screen shows of the families.
 */
const FILTER_FROM_POOLS = 20

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
 * One sentence of the impact: words, and the pools it names. A named pool is
 * drawn as a link to its row (#133), so an operand the preview says binds is
 * one click from the editor that changes it.
 */
type Sentence = readonly (string | { pool: string })[]

/** The pools in `names`, joined `a and b`, each as an operand. */
function operands(names: readonly string[]): Sentence {
  return names.flatMap((pool, i) => (i === 0 ? [{ pool }] : [' and ', { pool }]))
}

/**
 * WHAT THE CHANGE DOES, IN SENTENCES, FROM DATA THIS SCREEN ALREADY HAS: the
 * pool's own figures and every runner profile's ceiling before and after
 * (`arithmetic`, the minimum across a profile's pools). Nothing here is a
 * forecast of queue length -- the response carries no per-pool waiting count,
 * so the impact names ceilings and units in use, and nothing else.
 *
 * A MOVED CEILING SAYS WHICH POOL IT BINDS ON AFTER THE CHANGE (#133): the
 * edited pool, or another that the change took below it. The pools are raw
 * names, the spelling `pool-limit.sh` takes and the row prints under its label.
 */
function impactOf(pool: Pool, next: number, capacity: Capacity): Sentence[] {
  const after = effectiveAt(pool, next)
  const said: Sentence[] = []

  if (after === pool.effective_limit) {
    said.push([`The ceiling admission applies stays ${after}: ${setByWords(pool).term} holds it there.`])
  }

  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const changed = new Map(byName)
  changed.set(pool.name, { ...pool, effective_limit: after })
  const moved: Sentence[] = []
  const held: Sentence[] = []
  const profiles = Object.entries(capacity.runner_profiles)
    .filter(([, prof]) => prof.pools.includes(pool.name))
    .sort(([a], [b]) => a.localeCompare(b))
  for (const [name, prof] of profiles) {
    const before = arithmetic(prof.pools, prof, byName).ceiling
    const now = arithmetic(prof.pools, prof, changed)
    if (before !== now.ceiling) {
      const binds: Sentence = now.binding.length === 0 ? [] : [`, ${now.unset ? 'no limit set on' : 'binds on'} `, ...operands(now.binding)]
      moved.push([`${name} ${before ?? '—'} → ${now.ceiling ?? '—'}`, ...binds])
    } else {
      const others = now.binding.filter((b) => b !== pool.name)
      held.push(
        others.length === 0 ? [name] : [`${name} (`, ...operands(others), ` bind${others.length === 1 ? 's' : ''})`],
      )
    }
  }
  const list = (items: Sentence[]): Sentence => items.flatMap((it, i) => (i === 0 ? it : ['; ', ...it]))
  if (profiles.length === 0) said.push(['No runner profile takes this pool.'])
  if (moved.length > 0) said.push(['Max agents: ', ...list(moved), '.'])
  if (held.length > 0) said.push(['Unchanged: ', ...list(held), '.'])

  if (after === 0) {
    said.push([
      pool.active > 0
        ? `Nothing new is admitted to it. The ${unitsWord(pool.active)} in use ${pool.active === 1 ? 'is' : 'are'} not stopped.`
        : 'Nothing new is admitted to it.',
    ])
  } else if (after < pool.active) {
    said.push([
      `Nothing running is stopped: the ${unitsWord(pool.active)} in use finish${pool.active === 1 ? 'es' : ''}, and new work waits until fewer than ${after} are in use.`,
    ])
  } else if (pool.effective_limit !== null && after < pool.effective_limit) {
    said.push([`The ${unitsWord(pool.active)} in use ${pool.active === 1 ? 'fits' : 'fit'} under it; nothing is stopped.`])
  } else if (pool.effective_limit === null || after > pool.effective_limit) {
    said.push([`${unitsWord(after - pool.active)} free on this pool after the change.`])
  }
  return said
}

/**
 * THE ADDRESS OF A POOL'S ROW, the one the Tenants roster's Enforced links
 * write (#134), so following it moves the `is-target` mark to that row.
 * A PATH, NOT THE OLD HASH (QA2 admin2-21, 2026-10-29): `#admin/limits?pool=`
 * worked through the redirect, but a copied or middle-clicked link was the
 * legacy address. The pool's colons are encoded, as a query value's are.
 */
export function rowHref(pool: string): string {
  return addressToPath(`admin/limits?pool=${encodeURIComponent(pool)}`)
}

/**
 * A POOL AS A CONTROL'S NAME SAYS IT (QA2 admin2-20, 2026-10-29). The row
 * shows `eng` under the Workspaces head, but its Edit button and its editor's
 * field were named `tenant:eng`, the raw key the visible text had dropped. A
 * control is heard without its family's head, so the kind follows the label:
 * `eng (workspace)`. A provider's tenant pool is already two names,
 * `anthropic · u-bogdan`; `global` is its own kind.
 */
const SPOKEN_KIND: Readonly<Partial<Record<PoolKind, string>>> = {
  tenant: 'workspace',
  resource: 'resource class',
  runner: 'runner profile',
  backend: 'backend',
  provider: 'provider',
}

export function poolSpoken(name: string): string {
  const kind = SPOKEN_KIND[poolKind(name)]
  if (kind === undefined || name.split(':').length > 2) return poolLabel(name)
  return `${poolLabel(name)} (${kind})`
}

function Impact({ said }: { said: readonly Sentence[] }) {
  return (
    <p className="adm-impact">
      {said.map((sentence, i) => (
        <span key={i}>
          {i > 0 && ' '}
          {sentence.map((part, j) =>
            typeof part === 'string' ? (
              part
            ) : (
              <a key={j} className="ctl-link mono" href={rowHref(part.pool)} title={poolLabel(part.pool)}>
                {part.pool}
              </a>
            ),
          )}
        </span>
      ))}
    </p>
  )
}

/** `20 → 14 · −30%`. No percentage off a zero. */
function deltaWords(from: number | null, to: number): string {
  if (from === null) return `no limit set → ${to}`
  if (from === 0) return `${from} → ${to}`
  const pct = Math.round(((to - from) / from) * 100)
  return `${from} → ${to} · ${pct < 0 ? '−' : '+'}${Math.abs(pct)}%`
}

/**
 * WHY A POOL SHOWS NO RECORD, by where the record came from (#133). Never the
 * pool's `updated_at` in its place: admission rewrites the pool document on
 * every lease, so it moves with traffic, and printing it as "last changed"
 * would name a time nobody changed anything.
 */
function notRecordedWhy(pool: Pool): string {
  const record = (pool as LimitPool).record
  if (record === 'served')
    return 'Not recorded: no admin has changed this ceiling through the API since changes began to be recorded, so admin_changed_by and admin_changed_at are empty for this pool.'
  if (record === 'admins-only')
    return 'Visible to admins only: who changed a ceiling, and when (admin_changed_by, admin_changed_at), is served on the admin pool read, which needs the platform admin group.'
  return 'Not known here: the admin pool read (/v1/admin/pools), which serves admin_changed_by and admin_changed_at, did not answer for this pool, so who changed this ceiling, and when, is not known.'
}

/**
 * THE THREE NO-RECORDS LOOK DIFFERENT, NOT ONLY READ DIFFERENT (VQA V078).
 * Each was the same faint `—`, told apart by its `title` alone, so a failed
 * admin read looked exactly like a pool no admin had ever changed. Now only
 * "nobody changed it" is the dash; the other two carry the console's marks:
 * `admin only` (blue, solid) where the record is an admin's to read, and
 * `not read` (the failed-read mark) where the read did not answer.
 */
function NoRecord({ pool, as: Tag }: { pool: Pool; as: 'i' | 'dd' | 'p' }) {
  const record = (pool as LimitPool).record
  const why = notRecordedWhy(pool)
  if (record !== 'served') {
    const admin = record === 'admins-only'
    const mark = (
      <i className={`ctl-mark ${admin ? 'is-admin' : 'is-unread'} adm-record-gap`} title={why} aria-label={why}>
        {admin ? 'admin only' : 'not read'}
      </i>
    )
    return Tag === 'i' ? mark : <Tag className="adm-not-recorded is-gap">{mark}</Tag>
  }
  if (Tag === 'i')
    return (
      <i className="ctl-em adm-changed-none" title={why} aria-label={why}>
        —
      </i>
    )
  return (
    <Tag className="adm-not-recorded" title={why}>
      not recorded
    </Tag>
  )
}

/** The last admin write to a pool: who, when, what, and whether the pool still holds it. */
interface Change {
  by: string
  at: string
  /** `20 → 10`, `drained`; null when the record names no field (written before #133 recorded one). */
  what: string | null
  /** Set when the pool no longer holds what this change wrote: why, in a sentence. */
  since: string | null
}

/**
 * The last admin write to a pool, when the response carries it (#133).
 *
 * READ DEFENSIVELY, OFF THE POOL AS SERVED: `loadLimits` joins the record on
 * from the admin read, and a pool without both `admin_changed_by` and
 * `admin_changed_at` reads "not recorded". Both or nothing: a name with no
 * time, or a time with no name, is half a record and is not drawn as a whole
 * one.
 *
 * A CEILING WRITTEN SINCE IS NOT CREDITED TO THE LAST ADMIN. The record is the
 * newest change made through the API. `scripts/pool-limit.sh` writes Firestore
 * directly and records nothing, so when the pool's hard limit is no longer the
 * `to` of the record, the record is drawn with `changed since` and the
 * sentence why, rather than as the reason for the value on screen.
 */
function changeOf(pool: Pool): Change | null {
  const served = pool as LimitPool & { admin_changed_by?: unknown; admin_changed_at?: unknown }
  const by = served.admin_changed_by
  const at = served.admin_changed_at
  if (typeof by !== 'string' || by === '' || typeof at !== 'string' || Number.isNaN(Date.parse(at))) return null
  const limit = served.admin_change?.hard_limit
  const enabled = served.admin_change?.enabled
  const what: string[] = []
  const since: string[] = []
  if (limit !== undefined) {
    // A SAVE THAT CHANGED NOTHING IS NOT DRAWN AS A CHANGE (visual QA pass 1,
    // chrome-C23, part of #2596): the live console listed `100 → 100` under
    // Last changed. The write is still the last one recorded, so it stays,
    // said as what it was.
    what.push(limit.from === limit.to ? `${limit.to}, unchanged` : `${limit.from === null ? 'no limit set' : limit.from} → ${limit.to}`)
    if (pool.hard_limit !== limit.to)
      since.push(
        `The hard limit is ${pool.hard_limit === null ? 'not set' : pool.hard_limit} now, not the ${limit.to} this change wrote.`,
      )
  }
  if (enabled !== undefined) {
    what.push(enabled.to ? 'reopened' : 'drained')
    if (pool.enabled !== enabled.to) since.push(`The pool is ${pool.enabled ? 'open' : 'drained'} now, not ${enabled.to ? 'open' : 'drained'}.`)
  }
  return {
    by,
    at,
    what: what.length === 0 ? null : what.join(' · '),
    since:
      since.length === 0
        ? null
        : `${since.join(' ')} It was written since by something that records nothing -- scripts/pool-limit.sh writes Firestore directly -- so this record is not why the pool reads as it does.`,
  }
}

/** "1 unit", "3 units" (browser QA D32: the editor said "1 units"). */
export function unitsWord(n: number): string {
  return `${n} ${n === 1 ? 'unit' : 'units'}`
}

/**
 * WHAT SET A POOL'S CEILING, NEVER A BLANK CELL (browser QA D32, 2026-10-04).
 * #132 left the configured case empty so the column would not restate the
 * Ceiling, and every row then read as a cell nobody filled in. The configured
 * case is the word in the dim ink (QA-099, run 2); AIMD and provider quota
 * stand out in ink.
 */
export function AdmSetBy({ pool }: { pool: Pool }) {
  const by = setByWords(pool)
  // A faint dot, not the word (QA G5-19, capacity.html §G): `configured` on
  // nearly every row hid the rows where something else set the ceiling.
  // NAMED FOR THE MOUSE TOO (VQA V190): the word was on `aria-label` alone,
  // so a sighted reader hovering a lone `·` learnt nothing. Its own `title`
  // says the word and the sentence why, whatever cell it is drawn in.
  // THE WORD, QUIETER, NOT A MARK (visual QA QA-099, run 2, 2026-10-20).
  // A 3px `·` read as an empty cell; PR 1707's 6px disc was a mark a
  // sighted reader still had no word, legend or tooltip for beside `AIMD
  // back-off` in words -- only the accessibility tree said `configured`.
  // The word is now printed, in the dim ink at the cell's size
  // (`.adm-setby-cfg`, styles/admin.css), so the overrides in full ink still
  // stand out from it, which is what G5-19 hid the word for.
  if (by.term === 'configured')
    return (
      <span className="adm-setby-cfg" title={`configured. ${by.detail}`}>
        configured
      </span>
    )
  // The cell wraps between words; `(back-off)` is one run, never cut at its
  // hyphen (VQA V024, `.adm-setby-nb`, styles/admin.css).
  return by.term === BACKED_OFF ? (
    <>
      lowered automatically <span className="adm-setby-nb">(back-off)</span>
    </>
  ) : (
    <>{by.term}</>
  )
}

const BACKED_OFF = 'lowered automatically (back-off)'

/**
 * SET BY IN A READER'S WORDS (visual QA pass 1, admin-shell-13, part of
 * #2596). `setBy` (types.ts) named the adaptive case `AIMD back-off`, the
 * quota broker's algorithm (since QA1B-13 it reads `lowered automatically
 * after errors` there); on Pool limits the eng row read it beside rows that
 * said `configured` and nothing said what it was. Here it is what happened to
 * the ceiling, and the cell's title (its help on hover) says why and what
 * undoes it: the broker raises the target again as calls succeed
 * (quota_broker/aimd.py, additive increase).
 */
export function setByWords(pool: Pool): { term: string; detail: string } {
  const by = setBy(pool)
  // `setBy`'s adaptive case: `lowered automatically after errors` since
  // QA1B-13 (#2660), which Capacity › Pools prints; Pool limits says which
  // errors and that the target climbs back.
  if (by.term !== 'lowered automatically after errors') return by
  return {
    term: BACKED_OFF,
    detail: `Lowered automatically to ${pool.adaptive_target} after the provider rate-limited it (back-off); configured is ${pool.hard_limit}. It climbs back as calls succeed.`,
  }
}

/**
 * A pool key that breaks only after a colon (QA G5-11): the family tables
 * wrap anywhere, and `provider:anthropic:tenant:smok / e` is not a key anyone
 * can read or paste. The text is unchanged; `<wbr>` only offers the break.
 *
 * EACH SEGMENT IS ONE UNBROKEN RUN (VQA V076). `keep-all` stops no break in
 * Latin text, so a hyphen inside a segment was still one: `runner:claude- /
 * code` split the id mid-name. `.adm-key-seg` (styles/admin.css) holds each
 * segment on its line, and the `<wbr>` after its colon is the only break.
 *
 * A SEGMENT IS A `kind:name` PAIR, NOT A WORD (visual QA run 3, admin-003,
 * 2026-10-21). With a break after every colon, the greedy line at 1440 took
 * `provider:anthropic:tenant:` and left `u-bogdan` alone under it: the
 * tenant's kind on one line and its name on the next. A pool key is a path of
 * pairs, so it breaks only between them -- `provider:anthropic:` over
 * `tenant:u-bogdan` -- and a one-pair key (`tenant:eng`, `runner:claude-code`)
 * offers no break at all. A pair wider than the cell still wraps inside its
 * segment, at its hyphens first (VQA V160, styles/admin.css).
 */
export function keyBreaks(name: string): ReactNode {
  const parts = name.split(':')
  const pairs: string[] = []
  for (let i = 0; i < parts.length; i += 2) pairs.push(parts.slice(i, i + 2).join(':'))
  return pairs.map((pair, i) =>
    i < pairs.length - 1 ? (
      <Fragment key={i}>
        <span className="adm-key-seg">{`${pair}:`}</span>
        <wbr />
      </Fragment>
    ) : (
      <span className="adm-key-seg" key={i}>
        {pair}
      </span>
    ),
  )
}

/**
 * AN ADDRESS THAT BREAKS ONLY WHERE IT IS ASKED TO (visual QA run 6,
 * 6:admin-006 and 6:admin-008, part of #1680). `value` as unbroken runs, each
 * ending at a character of `after`, with a `<wbr>` between two runs: the
 * Model prices card's Set by breaks after its `@` and nowhere else, and the
 * Tenants phone record after `@` and `.`. Each run is a `className` span the
 * sheet holds whole (styles/admin.css); the text is unchanged, so a selection
 * still copies the whole value.
 */
export function addressBreaks(value: string, after: string, className: string): ReactNode {
  const runs: string[] = []
  let run = ''
  for (const ch of value) {
    run += ch
    if (after.includes(ch)) {
      runs.push(run)
      run = ''
    }
  }
  if (run !== '') runs.push(run)
  return runs.map((r, i) => (
    <Fragment key={i}>
      <span className={className}>{r}</span>
      {i < runs.length - 1 && <wbr />}
    </Fragment>
  ))
}

/**
 * `ops@… · 20 → 10 · 3h ago`, with the instant on the `time` element.
 *
 * EACH `·` LEADS THE PART IT INTRODUCES (VQA V191). The separator trailed
 * the part before it, so at 400 the line broke after it -- `ops@example.com ·
 * 40 → 20 ·` over `3h ago` -- and left a `·` alone at the line end. Every
 * part after the author is one unbreakable run, `· 40 → 20`, `· 3h ago`
 * (`.adm-changed-part`, styles/admin.css), and the only breaks are the
 * spaces before them, so a line never ends on a separator.
 *
 * AND ON A PHONE NO LINE STARTS ON ONE (visual QA QA-043, 2026-10-20). The
 * leading `·` then began the line under `ops@example.com`: `· 40 → 20 · 3h
 * ago`. A ~190px value column never holds the whole run, so in the stacked
 * record the author and each part are a line of their own and the
 * separators are not drawn (`.adm-changed-sep`, styles/admin.css). A break
 * CSS cannot see is never left to decide where a `·` falls.
 */
function Sep() {
  return <span className="adm-changed-sep">·</span>
}

function Changed({ change, now, as: Tag }: { change: Change; now: number; as: 'dd' | 'p' | 'span' }) {
  return (
    <Tag className="adm-changed">
      <span className="adm-changed-by">{change.by}</span>
      {change.what !== null && (
        <>
          {' '}
          <span className="adm-changed-part">
            <Sep /> <span className="mono">{change.what}</span>
          </span>
        </>
      )}{' '}
      <span className="adm-changed-part">
        <Sep /> <time dateTime={change.at} title={change.at}>{timeAgo(change.at, now)}</time>
      </span>
      {change.since !== null && (
        <>
          {' '}
          <span className="adm-changed-part">
            <Sep />{' '}
            <span className="adm-changed-since" title={change.since} aria-label={`changed since. ${change.since}`}>
              changed since
            </span>
          </span>
        </>
      )}
    </Tag>
  )
}

function PoolEditor({
  capacity,
  unread,
  admin,
  saved,
  outcomes,
  onSaved,
  onEdit,
}: {
  capacity: Capacity
  unread: ApiError | null
  /** From the session read. null: not known (yet), and treated as allowed. */
  admin: boolean | null
  saved: Readonly<Record<string, SaveMark>>
  outcomes: Readonly<Record<string, SaveOutcome>>
  onSaved: (pool: string, outcome: SaveOutcome) => Promise<boolean>
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
  const beside = useSyncExternalStore(subscribeBeside, atBesideWidth, () => true)

  // THE FILTER IS FOR A LIST LONGER THAN A SCREEN (#132, VQA V077). At 390
  // the families run to several screens of rows, and so do 59 pools at 1440;
  // a short list fits, and a box nobody needs is noise. A filter typed where
  // it is drawn and then not drawn stops applying rather than hiding rows
  // behind a box that is no longer there.
  const filtered = phone || pools.length > FILTER_FROM_POOLS
  const needle = filtered ? filter.trim().toLowerCase() : ''
  const matches = (p: Pool) =>
    needle === '' ||
    p.name.toLowerCase().includes(needle) ||
    poolLabel(p.name).toLowerCase().includes(needle) ||
    // The open editor's row is never filtered away from under what was typed.
    p.name === editing?.pool
  // ONE POOL ORDER (QA G5-22): Pools' -- family, problems first, this
  // tenant, name (`comparePools`). It was alphabetical here, so `standard`
  // sat third on this screen and first on Pools.
  const order = comparePools(poolViewer(capacity))
  const families = POOL_FAMILY_ORDER.flatMap((kind) => {
    const rows = pools
      .filter((p) => poolKind(p.name) === kind && matches(p))
      .sort(order)
    return rows.length === 0 ? [] : [{ kind, rows }]
  })
  const globalPool = pools.find((p) => p.name === 'global')

  // TO THE ROW A LINK NAMED, once it is on screen. jsdom implements no
  // `scrollIntoView`, hence the guard. AND FOCUS WITH IT (QA2 admin2-21,
  // 2026-10-29): the row was marked but focus stayed on the page's H1, so a
  // keyboard or screen reader user arrived at the top, not at the row the
  // link named. The row takes focus (`tabIndex={-1}` while it is the
  // target), without a second scroll.
  const landed = target !== null && pools.some((p) => p.name === target)
  useEffect(() => {
    if (!landed) return
    const row = document.getElementById(`limit-${target}`)
    if (row === null) return
    if (typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center' })
    row.focus({ preventScroll: true })
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

  const editor =
    open === undefined ? null : (
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
        onSaved={(outcome) => onSaved(open.name, outcome)}
      />
    )

  // THE LEAD'S BUTTON IS THE ROW'S `edit` (S10b): it opens the named pool's
  // side editor, under the same rules -- none for a non-admin or a pool kind
  // with no write route, and it waits while another pool holds an unsaved
  // value. The row is brought into view, since a phone draws the editor under
  // it.
  const lead = poolLead(pools)
  const leadPool = lead.kind === 'clear' ? undefined : pools.find((p) => p.name === lead.pool)
  const leadOpens = leadPool !== undefined && admin !== false && isEditable(leadPool)
  const openFromLead = (pool: string) => {
    setEditing({ pool, draft: null })
    const row = document.getElementById(`limit-${pool}`)
    if (row !== null && typeof row.scrollIntoView === 'function') row.scrollIntoView({ block: 'center' })
  }

  return (
    <>
    <PoolLeadView
      lead={lead}
      unread={unread}
      onOpen={leadOpens ? openFromLead : undefined}
      openWhy={held !== null && held !== leadPool?.name ? 'Save or cancel the open edit first.' : undefined}
    />
    {/* `has-filter` moves the sticky family heads below the filter, which
        sticks too (VQA V077, V187; styles/admin.css `.adm-ceilings`). */}
    <section className={`section adm-ceilings${filtered ? ' has-filter' : ''}`}>
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
      {filtered && (
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
      {target !== null && target !== '' && !landed && (
        // A LINK TO NO POOL SAYS SO (live QA ADM-9, 2026-10-11): an unknown
        // `?pool=` drew the full table with nothing marked and no word, so a
        // mistyped or stale link read as a page that had lost its row.
        <p className="ctl-panel-note adm-no-target" role="status">
          No pool named <code>{target}</code>; every pool is listed below.
        </p>
      )}
      {families.length === 0 && <p className="ctl-panel-note">no pool matches</p>}
      {/* THE TABLE STAYS STILL WHILE EDITING (L2). The editor is a panel
          rather than a field inside a row, and it has room for the impact and
          the history. From 1280px it floats over the right of the families
          (VQA V024): it took a grid column of its own, which narrowed every
          family to ~660px and reflowed every row -- `edit` wrapped under its
          figure and `AIMD back-off` (Set by's term then) broke in two -- the
          moment it opened.
          Below 1280px it is drawn under the row it edits (VQA V025): placed
          after all six families, tapping `edit` on a phone's first record
          jumped ~5,000px down to it. */}
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
              note={familyNote(kind, pools, globalPool, capacity.pools_complete !== false)}
              pools={rows}
              saved={saved}
              outcomes={outcomes}
              editing={editing?.pool ?? null}
              target={target}
              held={held}
              admin={admin}
              onOpen={(pool) => setEditing({ pool, draft: null })}
              inline={beside ? null : editor}
            />
          ))}
        </div>
        {beside && editor}
      </div>
    </section>
    </>
  )
}

/**
 * THE COLUMNS OF EVERY FAMILY TABLE, ONCE (#503). One list, so no family can
 * draw a column another does not -- the defect that moved `edit` -- and the
 * colgroup and the head are drawn from the same entries. `key` names the
 * `col` class the widths in styles/admin.css are written against.
 */
const LIMIT_COLUMNS = [
  { key: 'pool', head: 'Pool', num: false },
  // The unit rides on the column name (§8.4.3), on BOTH figures (CP-24).
  // NOT `num` (visual QA QA-045, 2026-10-20): `.is-num` right-aligned In
  // use's figure at its column's right edge while Ceiling's sat in a 6ch slot
  // at its column's left. Both figures are now the same slot (`.adm-inuse`,
  // `.adm-ceiling`), so both heads align the same way over them.
  { key: 'use', head: 'In use (units)', num: false },
  { key: 'ceiling', head: 'Ceiling (units)', num: false },
  { key: 'by', head: 'Set by', num: false },
  { key: 'changed', head: 'Last changed', num: false },
] as const

/**
 * WHERE THE REST OF GLOBAL'S UNITS ARE (visual QA pass 1, admin-shell-14,
 * part of #2596). Every task takes `global` and one pool of each of these
 * kinds (`pool_names_for`), so a family's rows add up to global's units in
 * use when every pool is configured. The fixture console read global 7 over
 * Resource classes adding up to 5, and nothing said where the other 2 were.
 * A unit the family's rows do not hold is in a pool of that kind nobody has
 * configured, which this page cannot list; the family says so in one line.
 *
 * Not for Providers: a profile with no provider takes no provider pool, and
 * `provider:<p>` and `provider:<p>:tenant:<t>` count the same task twice. Not
 * when the listing was cut short (`pools_complete: false`): the missing units
 * may be in a pool that was not served, and the line would be a guess.
 * Summed over the whole family, not the filtered rows, so typing a filter
 * does not move it.
 */
const SUMMED_FAMILIES: Readonly<Partial<Record<PoolKind, string>>> = {
  tenant: 'workspace',
  resource: 'class',
  runner: 'runner profile',
  backend: 'backend',
}

export function familyNote(kind: PoolKind, pools: readonly Pool[], global: Pool | undefined, complete: boolean): string | null {
  const noun = SUMMED_FAMILIES[kind]
  if (noun === undefined || global === undefined || !complete) return null
  const sum = pools.filter((p) => poolKind(p.name) === kind).reduce((n, p) => n + p.active, 0)
  if (sum === global.active) return null
  if (sum > global.active) return `These pools add up to ${sum} units in use, more than global's ${global.active}.`
  const rest = global.active - sum
  return `These pools add up to ${sum} of global's ${global.active} units in use: ${rest} ${rest === 1 ? 'is' : 'are'} outside any ${noun}.`
}

function Family({
  kind,
  note,
  pools,
  saved,
  outcomes,
  editing,
  target,
  held,
  admin,
  onOpen,
  inline,
}: {
  kind: PoolKind
  /** `familyNote`: where the rest of global's units in use are, or null when the rows add up. */
  note: string | null
  pools: Pool[]
  saved: Readonly<Record<string, SaveMark>>
  outcomes: Readonly<Record<string, SaveOutcome>>
  /** The pool whose side editor is open, if one is. */
  editing: string | null
  target: string | null
  /** The pool whose open editor holds an unsaved value, if one does. */
  held: string | null
  admin: boolean | null
  onOpen: (pool: string) => void
  /** The open editor, when it is drawn in the flow: under its row, if the row is in this family. */
  inline: ReactNode
}) {
  // UNDER THE ROW IT EDITS (VQA V025). The table is cut after the open row and
  // the rows after it continue in a second table of the same columns, so the
  // editor is beside what it edits at every width it is drawn in the flow.
  const at = inline === null ? -1 : pools.findIndex((p) => p.name === editing)
  const parts = at === -1 ? [pools] : [pools.slice(0, at + 1), pools.slice(at + 1)]
  // NAMED BY ITS FAMILY (visual QA run 3, a11y-keyboard-003, 2026-10-21):
  // six tables on one page each announced as "table". The rows after an open
  // editor are a second table of the same family, and say so.
  const rows = (some: Pool[], part: number) => (
    <LimitTable
      label={`${FAMILY_TITLE[kind]} ceilings${part === 0 ? '' : ', continued'}`}
      pools={some}
      saved={saved}
      outcomes={outcomes}
      editing={editing}
      target={target}
      held={held}
      admin={admin}
      onOpen={onOpen}
    />
  )
  return (
    <section className="ctl-card adm-family">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{FAMILY_TITLE[kind]}</h2>
      </div>
      <div className="ctl-card-body is-flush">
        {rows(parts[0]!, 0)}
        {at !== -1 && (
          <>
            <div className="adm-inline">{inline}</div>
            {parts[1]!.length > 0 && rows(parts[1]!, 1)}
          </>
        )}
        {note !== null && <p className="adm-family-note">{note}</p>}
      </div>
    </section>
  )
}

function LimitTable({
  label,
  pools,
  saved,
  outcomes,
  editing,
  target,
  held,
  admin,
  onOpen,
}: {
  /** The table's accessible name: its family's, `Tenants ceilings`. */
  label: string
  pools: Pool[]
  saved: Readonly<Record<string, SaveMark>>
  outcomes: Readonly<Record<string, SaveOutcome>>
  editing: string | null
  target: string | null
  held: string | null
  admin: boolean | null
  onOpen: (pool: string) => void
}) {
  // A RECORD PER POOL ON A PHONE (QA G5-08): at 390 the table ran 553-626px in
  // a 356px box and cut `edit` to `edi`.
  const phone = usePhoneTables()
  return (
    <div className={`ctl-table ${tableMode(phone)}`}>
      {/* EVERY FAMILY TABLE HAS THE SAME COLUMNS, IN THE SAME ORDER, AT THE
          SAME WIDTHS (#503, measured at 1440). `Set by` was drawn only in a
          family where some pool was not at its configured value, so the
          Providers table had a column the others did not and its `edit`
          sat at x≈1068 against x≈828 everywhere else: the one control on
          the screen moved from table to table. The columns are L2's
          (admin-help.html), and `.adm-limits` (styles/admin.css) fixes
          their widths through this colgroup, so `edit` -- first in the
          Ceiling cell, after a figure of fixed width -- is at one x in
          every family. */}
      <table role="table" className="adm-limits" aria-label={label}>
        <colgroup>
          {LIMIT_COLUMNS.map((c) => (
            <col key={c.key} className={`adm-col-${c.key}`} />
          ))}
        </colgroup>
        <thead role="rowgroup">
          <tr role="row">
            {LIMIT_COLUMNS.map((c) => (
              <th key={c.key} role="columnheader" scope="col" className={c.num ? 'is-num' : undefined}>
                {c.head}
              </th>
            ))}
          </tr>
        </thead>
        <tbody role="rowgroup">
          {pools.map((p) => (
            <PoolRow
              key={p.name}
              pool={p}
              mark={saved[p.name] ?? null}
              outcome={outcomes[p.name] ?? null}
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
  )
}

function PoolRow({
  pool,
  mark,
  outcome,
  open,
  target,
  locked,
  admin,
  onOpen,
}: {
  pool: Pool
  /** What the last save of this row said. Held by the screen, not the row (AH-7). */
  mark: SaveMark | null
  /** What the last save's response said the ceiling became. Held by the screen too. */
  outcome: SaveOutcome | null
  /** This pool's side editor is the one open. */
  open: boolean
  /** The row a link named (#134). */
  target: boolean
  /** Another pool's editor holds an unsaved value: this row's `edit` waits. */
  locked: boolean
  admin: boolean | null
  onOpen: () => void
}) {
  const by = setByWords(pool)
  const editable = isEditable(pool)
  const refused = admin === false
  // Any reason the press would be refused (QA-037: `aria-disabled`, not `disabled`).
  const blocked = !editable || locked || refused
  const change = changeOf(pool)
  const now = useNow(AGE_TICK_MS)
  const classes = [isPaused(pool) ? 'is-paused' : '', target ? 'is-target' : '', open ? 'is-editing' : '']
    .filter(Boolean)
    .join(' ')

  return (
    // THE ID A LINK NAMES (#132, #134): `limit-<pool>`, the raw pool name,
    // so the Tenants roster can point at `limit-tenant:<id>`.
    <tr role="row" id={`limit-${pool.name}`} className={classes === '' ? undefined : classes} tabIndex={target ? -1 : undefined}>
      <th role="rowheader" scope="row" className="pool-name">
        {poolLabel(pool.name)}
        {/* The raw name, because it is what you paste into pool-limit.sh and a
            prettified label is not. */}
        {/* THE WHOLE ID ON ITS TITLE (visual QA run 3, admin-003): a pair too
            long for the cell is cut rather than broken (`.adm-key-seg`). */}
        <span className="ctl-sub" title={pool.name}>
          {keyBreaks(pool.name)}
        </span>
        <QuotaLine pool={pool} now={now} />
      </th>
      <td role="cell" data-label="In use (units)">
        <span className="adm-inuse">{pool.active}</span>
      </td>
      <td role="cell" data-label="Ceiling (units)" className="adm-ceiling-cell">
        {/* THE FIGURE, THEN EDIT, THEN WHAT THE ROW SAYS ABOUT ITSELF. The
            figure has a fixed width (`.adm-ceiling`), so `edit` starts at the
            same x in every row of every family, and the tags after it -- over
            ceiling, read-only, saved -- wrap below rather than push it. */}
        {/* N /M IS ONE RUN (visual QA run 3, admin-004): the figure and its
            `/10` share `.adm-fig`, which does not wrap, so they never land on
            two lines; `edit` and its tags stay outside it, free to wrap. */}
        <span className="adm-fig">
          {pool.effective_limit === null ? (
            // A dash in the figure's slot, its reason on it, and the words after
            // `edit` -- so the words cannot widen the slot and move `edit`.
            <span className="adm-ceiling is-unset" title={by.detail} aria-label={`No limit set. ${by.detail}`}>
              —
            </span>
          ) : (
            <span className="adm-ceiling">{pool.effective_limit}</span>
          )}
          {/* THE CONFIGURED HARD LIMIT BESIDE THE EFFECTIVE ONE, AS POOLS DRAWS
              IT (G5-21, QA 2026-10-07): a quota-held pool read "50" here and
              "50/100" on Pools, and its editor then prefilled 100. A slot of its
              own and a fixed width, drawn empty when the two agree, so `edit`
              still starts at one x in every row. */}
          <span className="adm-was">
            {pool.effective_limit !== null && pool.hard_limit !== null && pool.effective_limit < pool.hard_limit && (
              <span className="cap-was" title={`Configured hard limit is ${pool.hard_limit}`}>
                /{pool.hard_limit}
              </span>
            )}
          </span>
        </span>
        <span className="limit-edit">
          {/* A pool nothing can write gets no editor to open: an enabled
              control that silently does nothing is worse than none. The
              sentence is on the button, where a keyboard reader reaching it
              gets it and a sighted reader gets the two-word marker instead.
              `aria-disabled`, NOT `disabled` (visual QA QA-037, 2026-10-20):
              a natively disabled button leaves the tab order, so the keyboard
              reader that sentence is for never reached it. The press does
              nothing, and `.is-blocked` (styles/admin.css) draws it as
              `:disabled` was drawn. */}
          <button
            type="button"
            className={blocked ? 'is-blocked' : undefined}
            onClick={blocked ? undefined : onOpen}
            aria-disabled={blocked || undefined}
            aria-expanded={open}
            aria-label={
              !editable
                ? `Edit ceiling for ${poolSpoken(pool.name)}. This pool kind has no write route, so it is read-only.`
                : refused
                  ? `Edit ceiling for ${poolSpoken(pool.name)}. Admins only: changing a ceiling needs the platform admin group.`
                  : locked
                    ? `Edit ceiling for ${poolSpoken(pool.name)}. Another pool has an unsaved value: save or cancel it first.`
                    : `Edit ceiling for ${poolSpoken(pool.name)}`
            }
            title={locked ? 'another pool has an unsaved value' : refused ? 'admins only' : undefined}
          >
            {/* Sentence case, as every other button in the console (VQ-054). */}
            Edit
          </button>
          {/* OVER ITS CEILING, AND NEUTRAL (#133). A ceiling lowered below what
              is in use is a legal state Help explains
              (`ceiling-change-evicts-nothing`): the work in flight finishes and
              nothing new is admitted. So the mark is neutral, not the fault or
              the warning colour -- it is an operator's own change read back. */}
          {pool.effective_limit !== null && pool.active > pool.effective_limit && (
            <span
              className="adm-over"
              title={`${unitsWord(pool.active - pool.effective_limit)} over: lowering a ceiling evicts nothing, so the work in use finishes and nothing new is admitted until fewer than ${pool.effective_limit} are in use.`}
            >
              over ceiling
            </span>
          )}
          {/* AT ITS CEILING, AND NEUTRAL (visual QA run 3, admin-005; owner
              decision 2026-10-22, Console Design Calls 2, c04). A full pool is
              admission working, not a fault: the over-ceiling tag's look, in
              its slot, exactly at the ceiling and never past it. */}
          {pool.effective_limit !== null && pool.active === pool.effective_limit && (
            <span
              className="adm-full"
              title={`At its ceiling: ${pool.active} of ${unitsWord(pool.effective_limit)} in use. New work that needs this pool waits.`}
            >
              full
            </span>
          )}
          {pool.effective_limit === null && (
            <span className="adm-unset" title={by.detail}>
              no limit set
            </span>
          )}
          {!editable && <span className="client-side">read-only</span>}
          {/* A SAVE IS A SUCCESS ONLY WHEN THE POOL MOVED TO WHAT WAS ASKED
              (2026-10-07): tenant smoke was raised 8 -> 20, the route answered
              200 with a pool still at 8, and this tag said saved. The
              response's pool is compared with the request (`saveOutcome`);
              a pool that did not move says so, with the reason the response
              gave, and never wears the success tag. */}
          {mark !== null && (outcome === null || outcome.moved) && <span className="tag ok">saved</span>}
          {mark !== null && outcome !== null && (
            <span className={outcome.moved ? 'adm-saved-what' : 'adm-saved-what warn-text'} role="status">
              {outcome.text}
            </span>
          )}
          {mark === 'unread' && (
            <span className="warn-text" title="The write succeeded; reading the pools back afterwards did not, so the figures in this row are from before it.">
              not re-read
            </span>
          )}
        </span>
      </td>
      {/* `configured` IN THE DIM INK WHERE THE CEILING IS THE CONFIGURED
          VALUE (#132, QA-099): never blank, and quieter than an override.
          The column itself is in every family now (#503). */}
      <td role="cell" data-label="Set by" className="adm-setby" title={by.detail}>
        <AdmSetBy pool={pool} />
      </td>
      <td role="cell" data-label="Last changed">
        {/* THE LAST ADMIN WRITE, OR A DASH WITH ITS REASON. Never `updated_at`,
            which admission rewrites on every lease (notRecordedWhy). */}
        {change === null ? (
          <NoRecord pool={pool} as="i" />
        ) : (
          <Changed change={change} now={now} as="span" />
        )}
      </td>
    </tr>
  )
}

/**
 * THE TERMS OF A PROVIDER-TENANT POOL'S CEILING (POOL-LIMITS-QUOTA, BGD
 * 2026-10-25, part of #2302).
 *
 * WHY THE ROW SAYS WHICH TERM BINDS. anthropic · eng sat full at 14 while this
 * screen showed its hard limit, 100 -- the one term it could edit and not the
 * one that bound. The pool's effective limit is the least of its hard limit,
 * the broker's `configured_hard_max` (50) and the AIMD `adaptive_target` (14,
 * halved by one 429), and a provider-reported quota cap when there is one;
 * an EXHAUSTED, COOLDOWN or DISABLED quota closes the pool at 0 whatever the
 * terms say (`QuotaState.effective_limit`). Every term tied at the least is
 * named (AH-1: naming only the first of two tied terms sends the reader to
 * raise one that changes nothing).
 *
 * A null target is "no target": AIMD has not lowered the tenant, and the
 * configured max is the term. A null hard limit binds the pool by itself:
 * admission refuses on a pool with no limit (#374).
 */
type TermKey = 'hard' | 'max' | 'target' | 'quota'

interface QuotaTerm {
  key: TermKey
  label: string
  value: number | null
}

/** The quota states that close a pool at 0 (`QuotaState.effective_limit`). */
const CLOSING_STATES: Readonly<Record<string, string>> = {
  EXHAUSTED: 'exhausted',
  COOLDOWN: 'cooldown',
  DISABLED: 'disabled',
}

export function quotaTerms(pool: Pool, q: QuotaState): { terms: QuotaTerm[]; binds: TermKey[]; closed: string | null } {
  const terms: QuotaTerm[] = [
    { key: 'hard', label: 'hard', value: pool.hard_limit },
    { key: 'max', label: 'max', value: q.configured_hard_max },
    { key: 'target', label: 'target', value: q.adaptive_target },
  ]
  if (q.quota_derived_limit !== null) terms.push({ key: 'quota', label: 'quota', value: q.quota_derived_limit })
  const closed = CLOSING_STATES[q.state] ?? null
  if (closed !== null) return { terms, binds: [], closed }
  if (pool.hard_limit === null) return { terms, binds: ['hard'], closed: null }
  const set = terms.filter((t): t is QuotaTerm & { value: number } => t.value !== null)
  const least = Math.min(...set.map((t) => t.value))
  return { terms, binds: set.filter((t) => t.value === least).map((t) => t.key), closed: null }
}

/** Minutes left on a cooldown, or null when there is none in force. */
function cooldownLeft(q: QuotaState, now: number): number | null {
  if (q.cooldown_until === null) return null
  const until = Date.parse(q.cooldown_until)
  if (!Number.isFinite(until) || until <= now) return null
  return Math.max(1, Math.ceil((until - now) / 60_000))
}

/**
 * The terms on the row itself, under the pool's name: `hard 100 · max 50 ·
 * target 14 binds · 429 1h ago`. Drawn only for a pool the quota read fed;
 * a member, whose quota read is refused, sees the row as before.
 */
function QuotaLine({ pool, now }: { pool: Pool; now: number }) {
  const q = (pool as LimitPool).quota
  if (q === undefined) return null
  const { terms, binds, closed } = quotaTerms(pool, q)
  const cooldown = cooldownLeft(q, now)
  return (
    <span className="ctl-sub adm-quota-line" data-quota-line="">
      {terms.map((t, i) => (
        <Fragment key={t.key}>
          {i > 0 && ' · '}
          {binds.includes(t.key) ? (
            <b className="adm-quota-binds">
              {t.label} {t.value ?? '—'} binds
            </b>
          ) : (
            <span>
              {t.label} {t.value ?? '—'}
            </span>
          )}
        </Fragment>
      ))}
      {closed !== null && (
        <>
          {' · '}
          <b className="adm-quota-binds">{closed} binds</b>
        </>
      )}
      {q.last_429_at !== null && (
        <>
          {' · '}
          <time dateTime={q.last_429_at} title={`Last 429 at ${q.last_429_at}`}>
            429 {timeAgo(q.last_429_at, now)}
          </time>
        </>
      )}
      {cooldown !== null && <> · cooldown {cooldown}m</>}
    </span>
  )
}

/** What a reset's answer said, in the row's words: `target 14 → 50`, and whether it was clamped. */
export function resetWords(name: string, data: unknown): SaveOutcome {
  const body = typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {}
  const reset = typeof body.reset === 'object' && body.reset !== null ? (body.reset as Record<string, unknown>) : null
  const from = reset?.from_target
  const to = reset?.to_target
  if (typeof to !== 'number') {
    return { moved: false, text: `Reset, but the response did not say what ${name}'s target became` }
  }
  const was = typeof from === 'number' ? String(from) : '—'
  return { moved: true, text: `${name} target ${was} → ${to}${reset?.clamped === true ? ', clamped to the max' : ''}` }
}

/** What a configured-max change's answer said, and the broker's clamp of the target if it applied one. */
export function hardMaxWords(name: string, data: unknown): SaveOutcome {
  const body = typeof data === 'object' && data !== null ? (data as Record<string, unknown>) : {}
  const change = typeof body.change === 'object' && body.change !== null ? (body.change as Record<string, unknown>) : null
  const to = change?.to_hard_max
  if (typeof to !== 'number') {
    return { moved: false, text: `Saved, but the response did not say what ${name}'s max became` }
  }
  const from = typeof change?.from_hard_max === 'number' ? String(change.from_hard_max) : '—'
  const clamp =
    change?.target_clamped === true && typeof change.from_target === 'number' && typeof change.to_target === 'number'
      ? `; target clamped ${change.from_target} → ${change.to_target}`
      : ''
  return { moved: true, text: `${name} max ${from} → ${to}${clamp}` }
}

/**
 * THE QUOTA TERMS IN THE SIDE EDITOR, AND THE TWO CONTROLS OVER THEM
 * (POOL-LIMITS-QUOTA; BGD 2026-10-25: "Console reset button", "Console edits
 * configured max").
 *
 * PLATFORM ADMINS ONLY, AND ONLY WHEN KNOWN TO BE ONE. The hard-limit editor
 * treats an unknown session as allowed because its route is the gate; these
 * two move what the broker admits for a whole tenant, so they are drawn only
 * for `is_admin` read as true. Both routes refuse everyone else 403 anyway
 * (routes/admin.py `reset_adaptive_target`, `set_quota_hard_max`), a pool
 * admin included.
 *
 * Both writes go through the quota broker, the only writer of quota
 * documents, and both carry a reason: the broker audits the reset, swarm-api
 * the max. After either, the rows are read again and the row says what the
 * answer said -- `target 14 → 50`, clamped or not; `max 50 → 30; target
 * clamped 40 → 30`.
 */
function QuotaSection({
  pool,
  admin,
  now,
  onWriting,
  onSaved,
}: {
  pool: Pool
  admin: boolean | null
  now: number
  onWriting: (on: boolean) => void
  onSaved: (outcome: SaveOutcome) => Promise<boolean>
}) {
  const q = (pool as LimitPool).quota
  const id = useId()
  const [target, setTarget] = useState<string | null>(null)
  const [targetWhy, setTargetWhy] = useState('')
  const [clear, setClear] = useState(false)
  const [max, setMax] = useState<string | null>(null)
  const [maxWhy, setMaxWhy] = useState('')
  const [busy, setBusy] = useState<'target' | 'max' | null>(null)
  const [said, setSaid] = useState<{ kind: 'target' | 'max'; outcome: SaveOutcome } | null>(null)
  const [error, setError] = useState<{ kind: 'target' | 'max'; error: ApiError } | null>(null)
  if (q === undefined) return null

  const { binds, closed } = quotaTerms(pool, q)
  const cooldown = cooldownLeft(q, now)
  const bindWord = closed !== null ? `${closed} (0)` : binds.length === 0 ? '—' : binds.join(', ')
  const exhausted = q.state === 'EXHAUSTED'
  // THE TARGET THE AIMD LOOP IS AT: a null target is the configured max.
  const current = q.adaptive_target ?? q.configured_hard_max
  const whole = (typed: string): number | null => {
    const n = Number(typed)
    return typed.trim() !== '' && Number.isInteger(n) && n >= 0 && n <= 100_000 ? n : null
  }
  const targetText = target ?? String(q.configured_hard_max)
  const targetNext = whole(targetText)
  const maxText = max ?? String(q.configured_hard_max)
  const maxNext = whole(maxText)
  const name = poolLabel(pool.name)

  const run = (kind: 'target' | 'max', call: () => Promise<Result<unknown>>, words: (data: unknown) => SaveOutcome) => {
    setBusy(kind)
    setError(null)
    setSaid(null)
    onWriting(true)
    call().then(async (r) => {
      if (r.status === 'ok' || r.status === 'stale') {
        const outcome = words(r.data)
        setSaid({ kind, outcome })
        if (kind === 'target') {
          setTarget(null)
          setTargetWhy('')
          setClear(false)
        } else {
          setMax(null)
          setMaxWhy('')
        }
        await onSaved(outcome)
      } else if (r.status === 'error') {
        setError({ kind, error: r.error })
      }
      setBusy(null)
      onWriting(false)
    })
  }

  const reset = (e: FormEvent) => {
    e.preventDefault()
    if (targetNext === null || targetWhy.trim() === '' || busy !== null) return
    run(
      'target',
      () => resetAdaptiveTarget(q.provider, q.tenant_id, { target: targetNext, reason: targetWhy.trim(), clear_cooldown: exhausted && clear }),
      (data) => resetWords(name, data),
    )
  }
  const saveMax = (e: FormEvent) => {
    e.preventDefault()
    if (maxNext === null || maxWhy.trim() === '' || busy !== null) return
    run('max', () => setQuotaHardMax(q.provider, q.tenant_id, { hard_max: maxNext, reason: maxWhy.trim() }), (data) => hardMaxWords(name, data))
  }

  // WHAT THE ANSWER SAID IS DRAWN ON THE ROW, ONCE, as the hard limit's save
  // is (`outcomes`); here only an answer that did not confirm, and a refusal.
  const answer = (kind: 'target' | 'max') => (
    <>
      {said?.kind === kind && !said.outcome.moved && (
        <span className="adm-saved-what warn-text" role="status">
          {said.outcome.text}
        </span>
      )}
      {error?.kind === kind && (
        <span className="warn-text" title={error.error.message}>
          {errorHeading(error.error)}
        </span>
      )}
    </>
  )

  return (
    <div className="adm-quota" data-quota-section="">
      <span className="adm-side-k">Quota · {q.state.toLowerCase()}</span>
      <dl className="adm-side-facts">
        <dt>Hard limit</dt>
        <dd>{pool.hard_limit === null ? 'no limit set' : pool.hard_limit}</dd>
        <dt>Configured max</dt>
        <dd>{q.configured_hard_max}</dd>
        <dt>Adaptive target</dt>
        <dd>{q.adaptive_target === null ? `none (max ${q.configured_hard_max})` : q.adaptive_target}</dd>
        {q.quota_derived_limit !== null && (
          <>
            <dt>Provider quota</dt>
            <dd>{q.quota_derived_limit}</dd>
          </>
        )}
        <dt>Binds</dt>
        <dd>{bindWord}</dd>
        <dt>Last 429</dt>
        <dd>
          {q.last_429_at === null ? (
            'none'
          ) : (
            <time dateTime={q.last_429_at} title={q.last_429_at}>
              {timeAgo(q.last_429_at, now)}
            </time>
          )}
        </dd>
        <dt>Cooldown</dt>
        <dd>{cooldown === null ? 'none' : `${cooldown}m left`}</dd>
      </dl>

      {admin === true && (
        <form className="adm-quota-form" onSubmit={reset} aria-label={`Reset the adaptive target for ${poolSpoken(pool.name)}`}>
          <div className="adm-side-field">
            <label className="adm-side-k" htmlFor={`${id}-target`}>
              Reset target (now {current}, max {q.configured_hard_max})
            </label>
            <input
              id={`${id}-target`}
              type="number"
              min={0}
              max={100000}
              value={targetText}
              disabled={busy !== null}
              onChange={(e) => setTarget(e.target.value)}
              aria-label={`New adaptive target for ${poolSpoken(pool.name)}`}
            />
            <input
              type="text"
              className="adm-quota-why"
              placeholder="reason"
              maxLength={500}
              value={targetWhy}
              disabled={busy !== null}
              onChange={(e) => setTargetWhy(e.target.value)}
              aria-label={`Reason for resetting the target of ${poolSpoken(pool.name)}`}
              required
            />
          </div>
          {exhausted && (
            <label className="adm-quota-check">
              <input type="checkbox" checked={clear} disabled={busy !== null} onChange={(e) => setClear(e.target.checked)} />
              clear cooldown
            </label>
          )}
          {targetNext !== null && targetNext > q.configured_hard_max && (
            <div className="limit-note">above the max: the broker clamps it to {q.configured_hard_max}</div>
          )}
          <div className="adm-side-actions">
            <button type="submit" disabled={targetNext === null || targetWhy.trim() === '' || busy !== null}>
              {busy === 'target' ? 'Resetting…' : 'Reset target'}
            </button>
            {answer('target')}
          </div>
        </form>
      )}

      {admin === true && (
        <form className="adm-quota-form" onSubmit={saveMax} aria-label={`Set the configured max for ${poolSpoken(pool.name)}`}>
          <div className="adm-side-field">
            <label className="adm-side-k" htmlFor={`${id}-max`}>
              Configured max (now {q.configured_hard_max})
            </label>
            <input
              id={`${id}-max`}
              type="number"
              min={0}
              max={100000}
              value={maxText}
              disabled={busy !== null}
              onChange={(e) => setMax(e.target.value)}
              aria-label={`New configured max for ${poolSpoken(pool.name)}`}
            />
            <input
              type="text"
              className="adm-quota-why"
              placeholder="reason"
              maxLength={500}
              value={maxWhy}
              disabled={busy !== null}
              onChange={(e) => setMaxWhy(e.target.value)}
              aria-label={`Reason for changing the max of ${poolSpoken(pool.name)}`}
              required
            />
          </div>
          {maxNext !== null && maxNext < current && (
            <div className="limit-note">below the target ({current}): the broker lowers the target to {maxNext}</div>
          )}
          <div className="adm-side-actions">
            <button
              type="submit"
              disabled={maxNext === null || maxNext === q.configured_hard_max || maxWhy.trim() === '' || busy !== null}
            >
              {busy === 'max' ? 'Saving…' : 'Set max'}
            </button>
            {answer('max')}
          </div>
        </form>
      )}
    </div>
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
  onSaved: (outcome: SaveOutcome) => Promise<boolean>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [typed, setTyped] = useState('')
  const now = useNow(AGE_TICK_MS)
  const change = changeOf(pool)
  const messageId = useId()
  const titleId = useId()
  const fed = (pool as LimitPool).quota !== undefined

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
        const reread = await onSaved(saveOutcome(pool.name, pool.hard_limit, next, r.data))
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
      {/* THE NAME UNDER THE LABEL ONLY WHEN IT ADDS SOMETHING (browser QA
          N19, 2026-10-04): `global` read "global / global". */}
      <h3 className="adm-side-title" id={titleId}>
        {poolLabel(pool.name)}
        {poolLabel(pool.name) !== pool.name && <span className="ctl-sub">{pool.name}</span>}
      </h3>
      <dl className="adm-side-facts">
        <dt>In use</dt>
        <dd>
          {unitsWord(pool.active)}
          {/* Free beside it for a quota-fed pool, whose terms below say why it is what it is. */}
          {fed && pool.available !== null && ` · ${pool.available} free`}
        </dd>
        <dt>Ceiling</dt>
        <dd>{pool.effective_limit === null ? 'no limit set' : unitsWord(pool.effective_limit)}</dd>
        {/* A quota-fed pool's hard limit is the first of its terms below. */}
        {!fed && pool.effective_limit !== pool.hard_limit && pool.hard_limit !== null && (
          <>
            <dt>Hard limit</dt>
            <dd>{unitsWord(pool.hard_limit)}</dd>
          </>
        )}
        <dt>Last changed</dt>
        {change === null ? (
          <NoRecord pool={pool} as="dd" />
        ) : (
          <Changed change={change} now={now} as="dd" />
        )}
      </dl>

      <div className="adm-side-field">
        {/* THE FIELD SAYS WHAT IT EDITS AND WHAT IT HOLDS (G5-21): the write
            sets the HARD limit, and the prefill is that hard limit -- not the
            effective ceiling printed above it when a quota or an adaptive
            target holds the pool lower. */}
        <label className="adm-side-k" htmlFor={`${titleId}-new`}>
          {pool.hard_limit === null ? 'New hard limit' : `New hard limit (now ${pool.hard_limit})`}
        </label>
        <input
          id={`${titleId}-new`}
          type="number"
          min={0}
          max={100000}
          value={value}
          disabled={busy || !editable}
          onChange={(e) => onDraft(e.target.value)}
          aria-label={`Hard limit for ${poolSpoken(pool.name)}`}
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
      {/* THE LINE SAYS WHAT IS WRONG (QA2 admin2-19, 2026-10-29): the bare
          range `0–100000` turned amber and Save went disabled, and nothing
          said the value was out of range or why Save would not press. */}
      {invalid && (
        <div className="warn-text limit-message" id={messageId}>
          {value.trim() !== '' && !Number.isInteger(parsed) && parsed >= 0 && parsed <= 100_000
            ? 'A ceiling is a whole number of units, 0 to 100000.'
            : 'A ceiling is 0 to 100000 units.'}
        </div>
      )}

      {impact.length > 0 && (
        <>
          <span className="adm-side-k">Impact</span>
          <Impact said={impact} />
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

      {/* THE QUOTA TERMS AND THEIR TWO CONTROLS, AFTER THE HARD LIMIT'S OWN
          SAVE (POOL-LIMITS-QUOTA): the hard limit is what this editor opened
          for, so its field stays first. */}
      <QuotaSection pool={pool} admin={admin} now={now} onWriting={onWriting} onSaved={onSaved} />

      {/* THE NEWEST WRITE IS ALL THE API CAN SERVE: the pool document holds
          the last admin change -- who, when, and the value before and after
          (#133) -- and no earlier one, so History is that entry and says
          nothing about the ones before it. */}
      <span className="adm-side-k">History</span>
      {change === null ? (
        <NoRecord pool={pool} as="p" />
      ) : (
        <Changed change={change} now={now} as="p" />
      )}
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

/**
 * A CARD UNDER THE CEILINGS, FOLDED ON POOL LIMITS (VQ-054, part of #2391;
 * config-e.html S10b: "The cards below it (Additional repositories, Model
 * prices, GitHub fallback token) are unchanged and folded here"). Model
 * prices drawn open was a long editable table under the one table the page
 * is for.
 *
 * THE HEAD IS THE CARD'S OWN HEADING, a button inside its `h2` that says
 * whether it is open (`aria-expanded`), as People's Recent admin actions is
 * drawn (People.tsx `Audit`), so a folded card is one line with its title and
 * its `?`, and an open one has no second heading. The body stays mounted and
 * is `hidden`: a card's read lands while it is folded, and what is typed in
 * it survives folding it again.
 *
 * `fold` is the page's to set. A card drawn on its own (its tests, and any
 * page that wants it open) is the plain `Card` it was.
 */
function FoldCard({ fold, className, title, action, children }: {
  fold: boolean
  className: string
  title: string
  action?: ReactNode
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const bodyId = useId()
  if (!fold) {
    return <Card className={className} title={title} level={2} action={action}>{children}</Card>
  }
  return (
    <Card
      className={`${className} adm-fold-card${open ? ' is-open' : ''}`}
      level={2}
      action={action}
      title={
        <button type="button" className="adm-fold" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((o) => !o)}>
          <CIcon name="chevron" className="c-ic adm-fold-ic" />
          {title}
        </button>
      }
    >
      <div id={bodyId} className="adm-fold-b" hidden={!open}>{children}</div>
    </Card>
  )
}

/**
 * GITHUB FALLBACK TOKEN (#780, D5; docs/onboarding.md §3.2). The owner's note
 * on D5: "lets also allow it to be done via the UI admin settings page" --
 * beside `uv run sc setup token --owner <org>`, for an org that will not
 * install the SwarmCloud App.
 *
 * IT STORES THE SIGNED-IN PERSON'S OWN TOKEN, FOR ONE OWNER. The route takes
 * the person from the verified identity and the tenant from `tenant_scope`,
 * never the body, so there is no field here for whose token it is: an admin
 * stores their own, as anyone does. It lands in the person's per-owner slot
 * (`swarm-tenant-<tenant>-git-u-<16 hex>`), is used by Access and by
 * submission for that owner only, and is revoked when the owner is removed
 * on Access.
 *
 * THE VALUE IS IN THIS PAGE FOR ONE POST. The input is uncontrolled -- the
 * value is never React state -- and is read and cleared in the same handler,
 * before the answer, so it is cleared whatever the answer. It is posted once,
 * never retried, never logged, never put in storage, and the answer carries
 * the token's names, never the value. A refusal draws the server's §2.3 copy
 * (`CLASSIC_PAT_BLOCKED`, `SSO_NOT_AUTHORISED`, ...) word for word.
 *
 * ITS WORDS SIT BEHIND `?`. Pool limits is held to a prose budget
 * (`prose.operator.test.tsx`), so the fields are named by `aria-label` and a
 * placeholder, and what the token is for is the `github-fallback-token` topic
 * (help.ts), behind the same `HelpCard` badge as the page's other two (visual
 * QA run 4, admin-009: a ghost `?` toggling an inline paragraph read as a
 * bare grey glyph beside them).
 */
export function FallbackTokenCard({ fold = false }: { fold?: boolean } = {}) {
  const id = useId()
  const tokenRef = useRef<HTMLInputElement>(null)
  const inFlight = useRef(false)
  const [owner, setOwner] = useState('')
  const [busy, setBusy] = useState(false)
  const [missing, setMissing] = useState<string | null>(null)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [stored, setStored] = useState<OwnerTokenResponse | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (inFlight.current) return
    const input = tokenRef.current
    const name = owner.trim()
    if (name === '' || input === null || input.value.trim() === '') {
      setMissing(name === '' ? 'Name the GitHub owner the token is for.' : 'Paste the token for ' + name + '.')
      return
    }
    const value = input.value.trim()
    input.value = ''
    inFlight.current = true
    setMissing(null)
    setRefused(null)
    setStored(null)
    setBusy(true)
    const res = await storeOwnerToken(name, value)
    inFlight.current = false
    setBusy(false)
    if (res.status === 'error') setRefused(res.error)
    else if (res.status === 'ok') setStored(res.data)
  }

  return (
    <FoldCard
      fold={fold}
      className="adm-fallback-token"
      title="GitHub fallback token"
      action={<HelpCard topic="github-fallback-token" />}
    >
      {/* ITS OWN FORM ROW, NOT THE CEILING EDITOR'S (VQA V023). It reused
          `.adm-side-field`, whose 96px input is sized for a six-digit
          ceiling, so `owner, e.g. example-org` and a pasted token were cut
          at a dozen characters. */}
      <form className="adm-token-form" onSubmit={(e) => void submit(e)} autoComplete="off">
        <input
          id={`${id}-owner`}
          type="text"
          className="mono"
          aria-label="Owner"
          autoComplete="off"
          spellCheck={false}
          placeholder="owner, e.g. example-org"
          value={owner}
          disabled={busy}
          onChange={(e) => setOwner(e.target.value)}
        />
        <input
          id={`${id}-token`}
          ref={tokenRef}
          type="password"
          aria-label="Personal access token"
          placeholder="personal access token"
          autoComplete="new-password"
          spellCheck={false}
          disabled={busy}
        />
        <Button type="submit" kind="primary" size="sm" busy={busy}>
          Store token
        </Button>
      </form>
      {missing !== null && <p className="warn-text">{missing}</p>}
      {refused !== null && <Refusal error={refused} title="The token was not stored" />}
      {stored !== null && (
        <p className="ur-small" role="status">
          {stored.org.owner} is enabled through your token
          {stored.token.forge_login !== null ? `, as @${stored.token.forge_login}` : ''}. Its value is in Secret Manager
          and is not shown again. Choose its repositories on <a className="c-link" href={addressToPath(ACCESS)}>Access</a>.
        </p>
      )}
    </FoldCard>
  )
}

/** The two platform settings this card edits, in the order it draws them. */
const REPO_LIMITS = [
  { key: 'max_additional_repositories', label: 'Most additional repositories per step', unit: '' },
  { key: 'additional_repositories_budget_gib', label: 'Size budget per step, GiB', unit: ' GiB' },
] as const
type RepoLimitKey = (typeof REPO_LIMITS)[number]['key']

/** A typed value as a whole number of at least 1, or null. The upper bound is
 *  the API's to name: it refuses a value past its range with 422. */
function wholeAtLeastOne(typed: string): number | null {
  const t = typed.trim()
  if (!/^\d+$/.test(t)) return null
  const n = Number(t)
  return n >= 1 ? n : null
}

/**
 * ADDITIONAL REPOSITORIES' TWO LIMITS (#1434, owner 2026-10-17): how many
 * repositories one step may read beside its own (default 5), and the GiB they
 * may total (default 6). PLATFORM SETTINGS, NOT CONSTANTS: the owner moved
 * them out of the design's code so an admin changes them live, here, where
 * the other run-time ceilings are edited. `PUT /v1/admin/settings` with only
 * the values that changed, so a save never rewrites a setting this card did
 * not touch (`merge_by_default` lives in the same document). swarm-api reads
 * them at every submission, so a change applies to the next one; a task
 * already submitted keeps what it was pinned with.
 *
 * What the save did is read off its RESPONSE -- the settings as stored -- not
 * off the form: the form says what was asked, the response what holds.
 */
export function AdditionalRepositoryLimitsCard({ fold = false }: { fold?: boolean } = {}) {
  const id = useId()
  const inFlight = useRef(false)
  const [settings, setSettings] = useState<Result<PlatformSettings> | null>(null)
  const [typed, setTyped] = useState<Record<RepoLimitKey, string>>({ max_additional_repositories: '', additional_repositories_budget_gib: '' })
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [savedAt, setSavedAt] = useState<PlatformSettings | null>(null)

  const take = (s: PlatformSettings) => {
    setTyped({
      max_additional_repositories: String(s.max_additional_repositories),
      additional_repositories_budget_gib: String(s.additional_repositories_budget_gib),
    })
  }
  useEffect(() => {
    let live = true
    // Through the loader, not a bare read: the fixture build answers it there
    // (visual QA run 4 admin-001), and a bare read went to the dev server's 404.
    // Settled, as the price card's loaders are: a partial mock of `./api` that
    // lacks it must not throw the whole Admin screen away.
    void settle(() => loadPlatformSettings()).then((r) => {
      if (!live) return
      setSettings(r)
      if (r.status === 'ok' || r.status === 'stale') take(r.data)
    })
    return () => {
      live = false
    }
  }, [])

  const data = settings !== null && (settings.status === 'ok' || settings.status === 'stale') ? settings.data : null
  const values = REPO_LIMITS.map((l) => ({ ...l, n: wholeAtLeastOne(typed[l.key]) }))
  const changes: Partial<Record<RepoLimitKey, number>> = {}
  for (const v of values) if (data !== null && v.n !== null && v.n !== data[v.key]) changes[v.key] = v.n
  const invalid = values.some((v) => v.n === null)
  const nothing = Object.keys(changes).length === 0

  async function save(e: FormEvent) {
    e.preventDefault()
    if (inFlight.current || invalid || nothing) return
    inFlight.current = true
    setBusy(true)
    setRefused(null)
    setSavedAt(null)
    const r = await write(route('/v1/admin/settings'), 'PUT', changes)
    inFlight.current = false
    setBusy(false)
    if (r.status === 'error') {
      setRefused(r.error)
      return
    }
    if (r.status === 'ok' && typeof r.data === 'object' && r.data !== null) {
      const stored = r.data as PlatformSettings
      setSettings({ status: 'ok', data: stored, fetchedAt: r.fetchedAt })
      take(stored)
      setSavedAt(stored)
    }
  }

  return (
    // NO INTRO PARAGRAPH, AND A READ FAILURE IS TWO WORDS: Pool limits is held
    // to 40 words (prose.operator.test.tsx) and measured 36 without this card
    // on 2026-10-18, so the card has four. The heading and the two labelled
    // inputs carry the meaning; when a change applies is in the docblock
    // above, not on screen. A failed read says "Not read", the console's mark
    // for it, with what failed and why one hover away in `title`, as the pool
    // editor's save error on this screen already keeps its message.
    <FoldCard fold={fold} className="adm-repo-limits" title="Additional repositories">
      {settings === null || settings.status === 'loading' ? (
        <p className="ur-small">Reading…</p>
      ) : data === null ? (
        settings.status === 'error' && settings.error.httpStatus === 403 ? (
          <p className="ur-small">Platform admins only.</p>
        ) : (
          <p className="warn-text" role="alert"
            title={settings.status === 'error' ? `${errorHeading(settings.error)}: ${settings.error.message}` : undefined}>
            Not read
          </p>
        )
      ) : (
        <form className="adm-token-form" onSubmit={(e) => void save(e)}>
          {values.map((v) => (
            <label key={v.key} htmlFor={`${id}-${v.key}`} className="ur-small">
              {v.label}
              <input id={`${id}-${v.key}`} type="number" min={1} step={1} className="mono"
                value={typed[v.key]} disabled={busy} aria-invalid={v.n === null || undefined}
                onChange={(e) => {
                  setSavedAt(null)
                  setTyped((was) => ({ ...was, [v.key]: e.target.value }))
                }} />
            </label>
          ))}
          <Button type="submit" kind="primary" size="sm" busy={busy} disabled={invalid || nothing}>
            Save limits
          </Button>
        </form>
      )}
      {data !== null && invalid && <p className="warn-text">Each limit is a whole number, 1 or more.</p>}
      {data !== null && data.settings_document === 'missing' && savedAt === null && (
        <p className="ur-small">Both are at their defaults: nobody has set them.</p>
      )}
      {refused !== null && <Refusal error={refused} title="The limits were not saved" />}
      {savedAt !== null && (
        <p className="ur-small" role="status">
          Saved: at most {savedAt.max_additional_repositories} additional{' '}
          {savedAt.max_additional_repositories === 1 ? 'repository' : 'repositories'} per step,{' '}
          {savedAt.additional_repositories_budget_gib} GiB in total.
        </p>
      )}
    </FoldCard>
  )
}

const NO_CEILING: BudgetLimits = { usd: null, tokens: null }

/** A ceiling as the fields hold it: each unit's figure, or blank for none. */
function typedCeiling(c: BudgetLimits): Record<BudgetUnit, string> {
  return { usd: c.usd === null ? '' : String(c.usd), tokens: c.tokens === null ? '' : String(c.tokens) }
}

/**
 * THE PER-AGENT BUDGET CEILING (BUDGET-UI-SETTINGS; part of #2459, docs/design/agent-budget.md §2, §4): the most any
 * workspace's per-agent budget, or a step's, may be, per unit. `agent_budget_ceiling` in the platform settings,
 * `{usd, tokens}`, either or both: a workspace picks its budget's unit, so a ceiling in dollars alone does not cap a
 * workspace that budgets in tokens. Off by default (`{}`).
 *
 * THE CEILING ALONE TURNS NOTHING ON (BGD, 2026-10-26, question 4): a workspace that sets no budget has none, whatever
 * this says. A workspace default above it is refused on save (422 `budget_above_ceiling`), and one saved earlier
 * above a ceiling lowered now is capped at the next submission, because every attempt's limit is the least of the
 * three. `PUT /v1/admin/settings` with only `agent_budget_ceiling`, replaced whole; what the save did is read off its
 * RESPONSE, as the cards above read theirs.
 */
export function AgentBudgetCeilingCard({ fold = false }: { fold?: boolean } = {}) {
  const id = useId()
  const inFlight = useRef(false)
  const [settings, setSettings] = useState<Result<PlatformSettings> | null>(null)
  const [on, setOn] = useState(false)
  const [typed, setTyped] = useState<Record<BudgetUnit, string>>(() => typedCeiling(NO_CEILING))
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [savedAt, setSavedAt] = useState<BudgetLimits | null>(null)

  const take = (c: BudgetLimits) => {
    setOn(c.usd !== null || c.tokens !== null)
    setTyped(typedCeiling(c))
  }
  useEffect(() => {
    let live = true
    // Settled, as the other platform-settings cards' reads are: a partial mock
    // of `./api` that lacks it must not throw the whole Admin screen away.
    void settle(() => loadPlatformSettings()).then((r) => {
      if (!live) return
      setSettings(r)
      if (r.status === 'ok' || r.status === 'stale') take(budgetCeilingOf(r.data))
    })
    return () => {
      live = false
    }
  }, [])

  const data = settings !== null && (settings.status === 'ok' || settings.status === 'stale') ? settings.data : null
  const stored = data === null ? null : budgetCeilingOf(data)
  const blank = (u: BudgetUnit) => typed[u].trim() === ''
  const asked: BudgetLimits = {
    usd: !on || blank('usd') ? null : parseBudgetAmount('usd', typed.usd),
    tokens: !on || blank('tokens') ? null : parseBudgetAmount('tokens', typed.tokens),
  }
  const bad = (u: BudgetUnit) => on && !blank(u) && asked[u] === null
  const neither = on && blank('usd') && blank('tokens')
  const invalid = bad('usd') || bad('tokens') || neither
  const nothing = stored !== null && stored.usd === asked.usd && stored.tokens === asked.tokens

  async function save(e: FormEvent) {
    e.preventDefault()
    if (inFlight.current || invalid || nothing) return
    inFlight.current = true
    setBusy(true)
    setRefused(null)
    setSavedAt(null)
    const r = await saveBudgetCeiling(asked)
    inFlight.current = false
    setBusy(false)
    if (r.status === 'error') {
      setRefused(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setSettings(r)
      const now = budgetCeilingOf(r.data)
      take(now)
      setSavedAt(now)
    }
  }

  const edit = (u: BudgetUnit, value: string) => {
    setSavedAt(null)
    setTyped((was) => ({ ...was, [u]: value }))
  }
  const saidNow = savedAt === null ? null : budgetLimitsWords(savedAt)
  return (
    <FoldCard fold={fold} className="adm-budget-ceiling" title="Agent budget ceiling">
      {settings === null || settings.status === 'loading' ? (
        <p className="ur-small">Reading…</p>
      ) : data === null ? (
        settings.status === 'error' && settings.error.httpStatus === 403 ? (
          <p className="ur-small">Platform admins only.</p>
        ) : (
          <p className="warn-text" role="alert"
            title={settings.status === 'error' ? `${errorHeading(settings.error)}: ${settings.error.message}` : undefined}>
            Not read
          </p>
        )
      ) : (
        // The words are drawn with the form only: over a failed read they
        // would explain a control that is not there (and Pool limits is held
        // to a prose budget, prose.operator.test.tsx).
        <form className="adm-token-form" aria-label="Agent budget ceiling" onSubmit={(e) => void save(e)}>
          <p className="ur-small">
            The most any workspace&rsquo;s per-agent budget can be. It turns nothing on: a workspace that sets no budget
            has none. {BUDGET_DOES}
          </p>
          <div className="jb-options" role="radiogroup" aria-label="Ceiling">
            {([false, true] as const).map((v) => (
              <label key={String(v)} className={`jb-option${on === v ? ' is-on' : ''}`}>
                <input type="radio" name={`${id}-on`} checked={on === v} disabled={busy}
                  onChange={() => {
                    setSavedAt(null)
                    setOn(v)
                  }} />
                <span className="jb-text">
                  <span className="jb-name">{v ? 'A limit' : 'Off'}{!v && <span className="jb-default"> (the default)</span>}</span>
                </span>
              </label>
            ))}
          </div>
          {on && (['usd', 'tokens'] as const).map((u) => (
            <label key={u} htmlFor={`${id}-${u}`} className="ur-small">
              {u === 'usd' ? 'Dollars per agent, at most' : 'Tokens per agent, at most'}
              <input id={`${id}-${u}`} type="text" inputMode={u === 'usd' ? 'decimal' : 'numeric'} className="mono"
                autoComplete="off" placeholder="none" value={typed[u]} disabled={busy}
                aria-invalid={bad(u) || undefined} onChange={(e) => edit(u, e.target.value)} />
            </label>
          ))}
          <Button type="submit" kind="primary" size="sm" busy={busy} disabled={invalid || nothing}>
            Save ceiling
          </Button>
        </form>
      )}
      {data !== null && neither && <p className="warn-text">Name a ceiling in dollars, in tokens or in both, or choose Off.</p>}
      {data !== null && (bad('usd') || bad('tokens')) && (
        <p className="warn-text">Dollars are a number above 0; tokens a whole number, 1 or more.</p>
      )}
      {refused !== null && <Refusal error={refused} title="The ceiling was not saved" />}
      {savedAt !== null && (
        <p className="ur-small" role="status">
          {saidNow === null ? 'Saved: no ceiling.' : `Saved: at most ${saidNow} per agent.`}
        </p>
      )}
    </FoldCard>
  )
}

/** The four rates the price table edits, in the order it draws them. Per million tokens. */
const PRICE_COLUMNS: ReadonlyArray<{ key: ModelPriceRate; head: string }> = [
  { key: 'input', head: 'Input' },
  { key: 'output', head: 'Output' },
  { key: 'cache_read', head: 'Cache read' },
  { key: 'cache_write', head: 'Cache write' },
]
type PriceField = ModelPriceRate | 'as_of'
type TypedPrices = Record<string, Partial<Record<PriceField, string>>>

/** `claude-haiku-4-5-20251001`, `claude-opus-4-8[1m]`: what may follow a priced
 *  name and still be that model, as `modelprices.price_for` reads it. */
const PRICED_SUFFIX = /^(?:-\d{8})?(?:\[[a-z0-9]+\])?$/
const AS_OF = /^\d{4}-\d{2}-\d{2}$/

/** Whether `model` is priced by an entry of `prices`: its own, or a dated or
 *  context-marked spelling of one. A pinned model that is not reads "cost not reported". */
function isPriced(model: string, prices: Record<string, ModelPrice>): boolean {
  return Object.keys(prices).some((name) => model.startsWith(name) && PRICED_SUFFIX.test(model.slice(name.length)))
}

/** A typed rate as a number of 0 or more, or null. The ceiling is the API's to
 *  name (`modelprices.MAX_RATE`): it refuses a figure past it with 422. */
function rateOf(typed: string | undefined): number | null {
  const t = (typed ?? '').trim()
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(t)) return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

function typedOf(prices: Record<string, ModelPrice>): TypedPrices {
  const out: TypedPrices = {}
  for (const [name, p] of Object.entries(prices)) {
    out[name] = { input: String(p.input), output: String(p.output), cache_read: String(p.cache_read), cache_write: String(p.cache_write), as_of: p.as_of }
  }
  return out
}

/** `100,000`: a tier ceiling as the pricing page writes it. */
const tokensOf = (n: number) => n.toLocaleString('en-US')

/** One tier, read-only: its prompt range, then its four rates in the table's column order. */
function tierLine(tier: ModelPriceTier, before: ModelPriceTier | undefined): string {
  const range = tier.up_to_prompt_tokens !== null
    ? `≤${tokensOf(tier.up_to_prompt_tokens)}`
    : `>${tokensOf(before?.up_to_prompt_tokens ?? 0)}`
  return `${range}: ${PRICE_COLUMNS.map((c) => String(tier[c.key])).join(' / ')}`
}

/** A loader that a test's partial mock of `./api` may lack, as a failed read rather than a thrown render. */
function settle<T>(load: () => Promise<Result<T>>): Promise<Result<T>> {
  return Promise.resolve()
    .then(load)
    .catch((e: unknown): Result<T> => ({
      status: 'error',
      error: { kind: 'unreachable', httpStatus: null, code: null, message: e instanceof Error ? e.message : String(e) },
    }))
}

/**
 * MODEL LIST PRICES (#1481, owner 2026-10-17): the per-million-token figures
 * swarm-api prices a stopped attempt's usage at. PRICES ARE DATA, NOT CODE:
 * the `model_prices` platform setting, edited here beside the other platform
 * settings and read afresh at every estimate, so a save prices the next
 * stopped attempt with no deploy.
 *
 * ONE ROW PER MODEL: every model the table prices, and every model a runner
 * pins (`/v1/runtimes`, `loadModelClasses`) that it does not. NOTHING IS
 * INVENTED: a pinned model with no entry reads "cost not reported" -- what
 * its stopped attempts read -- and an admin may give it a figure here. A
 * figure is never filled in for it.
 *
 * Admins edit; anyone else reads the figures with no field to type in. The
 * table is read from `GET /v1/settings/model-prices`, which every signed-in
 * member may read (PRICES-FOLLOWUP); only the save is admin-only. A save
 * PUTs `model_prices` with only the rows that changed, each a whole entry
 * (`schemas.ModelPriceRequest`); swarm-api stamps each with the admin who
 * made it and keeps every model not named. A
 * changed figure's `as_of` becomes today unless the admin typed another day.
 *
 * A MODEL PRICED IN TIERS by prompt length (Haiku 5.5) shows each tier under
 * its name, prompt range then the four rates in the columns' order, and its
 * row is read-only for an admin too: an entry's own rates are its first
 * tier's, so a typed figure would have to rewrite a tier. Its tiers are the
 * weekly refresh's to write, or an admin's through the API.
 *
 * What the save did is read off its RESPONSE -- the table as stored, with who
 * saved it and when -- not off the form. A refusal that names a figure
 * (`detail.errors[].loc`, `priceRefusals`) is printed at that figure.
 */
export function ModelPricesCard({ admin, fold = false }: { admin: boolean | null; fold?: boolean }) {
  const id = useId()
  const inFlight = useRef(false)
  const phone = usePhoneTables()
  const [settings, setSettings] = useState<Result<ModelPriceTable> | null>(null)
  /** Every model a runner pins, or null when `/v1/runtimes` was not read. */
  const [pinned, setPinned] = useState<string[] | null>(null)
  const [typed, setTyped] = useState<TypedPrices>({})
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [atField, setAtField] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<{ settings: PlatformSettings; models: string[] } | null>(null)

  useEffect(() => {
    let live = true
    void settle(() => loadModelPrices()).then((r) => {
      if (!live) return
      setSettings(r)
      if (r.status === 'ok' || r.status === 'stale') setTyped(typedOf(r.data.model_prices))
    })
    void settle(() => loadModelClasses()).then((r) => {
      if (!live || (r.status !== 'ok' && r.status !== 'stale')) return
      setPinned([...new Set(Object.values(r.data).flatMap((o) => Object.values(o.models)))])
    })
    return () => {
      live = false
    }
  }, [])

  const data = settings !== null && (settings.status === 'ok' || settings.status === 'stale') ? settings.data : null
  const prices = data?.model_prices ?? {}
  const models = [...new Set([...Object.keys(prices), ...(pinned ?? []).filter((m) => !isPriced(m, prices))])].sort()
  const editable = data !== null && admin !== false

  const changes: Record<string, ModelPriceChange> = {}
  let invalid = false
  for (const m of models) {
    const t = typed[m] ?? {}
    const price = prices[m]
    const rates = PRICE_COLUMNS.map((c) => rateOf(t[c.key]))
    const asOf = (t.as_of ?? '').trim()
    const touched = price === undefined
      ? PRICE_COLUMNS.some((c) => (t[c.key] ?? '').trim() !== '') || asOf !== ''
      : PRICE_COLUMNS.some((c, i) => rates[i] !== price[c.key]) || asOf !== price.as_of
    if (!touched) continue
    if (rates.some((r) => r === null) || !AS_OF.test(asOf)) {
      invalid = true
      continue
    }
    changes[m] = { input: rates[0]!, output: rates[1]!, cache_read: rates[2]!, cache_write: rates[3]!, as_of: asOf }
  }
  const nothing = Object.keys(changes).length === 0

  const type = (model: string, field: PriceField, value: string) => {
    setSaved(null)
    setTyped((was) => {
      const row = was[model] ?? {}
      const stored = prices[model]?.as_of ?? ''
      // A changed figure was read today, unless the admin has said otherwise.
      const asOf = field !== 'as_of' && (row.as_of ?? '') === stored ? new Date().toISOString().slice(0, 10) : row.as_of
      return { ...was, [model]: { ...row, as_of: asOf, [field]: value } }
    })
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (inFlight.current || invalid || nothing) return
    inFlight.current = true
    setBusy(true)
    setRefused(null)
    setAtField({})
    setSaved(null)
    const sent = Object.keys(changes).sort()
    const r = await saveModelPrices(changes)
    inFlight.current = false
    setBusy(false)
    if (r.status === 'error') {
      const { fields, unattributed } = priceRefusals(r.error)
      setAtField(fields)
      setRefused(unattributed === null ? null : r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setSettings({ ...r, data: { model_prices: r.data.model_prices } })
      setTyped(typedOf(r.data.model_prices))
      setSaved({ settings: r.data, models: sent })
    }
  }

  const said = (key: string) => atField[key]
  const cell = (m: string, field: PriceField, label: string) => {
    const msg = said(`${m}/${field}`)
    return (
      <>
        {/* Sized to its figure (`.adm-price-in`, admin.css), or each field is
            the browser's 20 characters and the table outgrows its card. */}
        <input id={`${id}-${m}-${field}`} type="text" inputMode={field === 'as_of' ? 'numeric' : 'decimal'}
          className={field === 'as_of' ? 'mono adm-price-in is-day' : 'mono adm-price-in'}
          aria-label={`${label}, ${m}`} value={typed[m]?.[field] ?? ''} disabled={busy}
          placeholder={field === 'as_of' ? 'YYYY-MM-DD' : undefined}
          aria-invalid={msg !== undefined || undefined}
          aria-describedby={msg !== undefined ? `${id}-${m}-${field}-why` : undefined}
          onChange={(e) => type(m, field, e.target.value)} />
        {msg !== undefined && <span id={`${id}-${m}-${field}-why`} className="warn-text" role="alert">{msg}</span>}
      </>
    )
  }

  return (
    <FoldCard fold={fold} className="adm-prices" title="Model prices">
      {settings === null || settings.status === 'loading' ? (
        <p className="ur-small">Reading…</p>
      ) : data === null ? (
        // Two words, the console's mark for a failed read, with what failed
        // one hover away: Pool limits is held to a word budget
        // (prose.operator.test.tsx), as the card above says.
        <p className="warn-text" role="alert"
          title={settings.status === 'error' ? `${errorHeading(settings.error)}: ${settings.error.message}` : undefined}>
          Not read
        </p>
      ) : (
        <form onSubmit={(e) => void save(e)}>
          <div className={`ctl-table ${tableMode(phone)}`}>
            <table role="table" className="adm-price-table" aria-label="Model prices">
              <thead role="rowgroup">
                {/* EACH HEAD NAMES ITS COLUMN (`data-col`), and its class
                    is the width styles/admin.css sets: the table is laid out fixed, so it
                    is its card's width and never scrolls at 1440 (visual QA
                    run 6, 6:admin-006). */}
                <tr role="row">
                  <th role="columnheader" scope="col" className="adm-pc-model" data-col="model">Model</th>
                  {PRICE_COLUMNS.map((c) => (
                    <th key={c.key} role="columnheader" scope="col" className="is-num adm-pc-fig" data-col={c.key}>{c.head} /M</th>
                  ))}
                  <th role="columnheader" scope="col" className="adm-pc-cur" data-col="currency">Currency</th>
                  <th role="columnheader" scope="col" className="adm-pc-src" data-col="source">Source</th>
                  <th role="columnheader" scope="col" className="adm-pc-day" data-col="as_of">As of</th>
                  <th role="columnheader" scope="col" className="adm-pc-by" data-col="by">Set by</th>
                </tr>
              </thead>
              <tbody role="rowgroup">
                {models.map((m) => {
                  const p = prices[m]
                  const rowMsg = said(m)
                  const tiers = p?.tiers
                  // A tiered row is read-only for an admin too (the card's comment says why).
                  const edits = editable && tiers === undefined
                  return (
                    <tr key={m} role="row" data-model={m}>
                      <th role="rowheader" scope="row" className="mono" data-label="Model">
                        {m}
                        {tiers !== undefined && (
                          <ul className="adm-price-tiers ur-small" aria-label={`Tiers by prompt tokens, ${m}`}>
                            {tiers.map((t, i) => (
                              <li key={i} data-tier={i}>{tierLine(t, tiers[i - 1])}</li>
                            ))}
                          </ul>
                        )}
                        {rowMsg !== undefined && <span className="warn-text" role="alert"> {rowMsg}</span>}
                      </th>
                      {PRICE_COLUMNS.map((c) => (
                        <td key={c.key} className="is-num mono" data-label={`${c.head} /M`}>
                          {edits ? cell(m, c.key, c.head) : p === undefined ? '—' : String(p[c.key])}
                        </td>
                      ))}
                      <td data-label="Currency">{p === undefined ? '—' : p.currency}</td>
                      <td data-label="Source">{p === undefined ? <span className="is-absent">cost not reported</span> : p.source}</td>
                      <td className="mono" data-label="As of">{edits ? cell(m, 'as_of', 'As of') : p === undefined ? '—' : p.as_of}</td>
                      {/* Whole at 1440, and where a card is narrower it
                          breaks after the `@` only (6:admin-006). A writer
                          that is not an address -- the seed's
                          `agent_worker.lifecycle._LIST_PRICES` -- breaks
                          after its dots: one unbreakable run of it ran past
                          the card's right edge (VQ-054). */}
                      <td data-label="Set by" className="adm-price-by">
                        {p?.updated_by == null ? '—' : addressBreaks(p.updated_by, p.updated_by.includes('@') ? '@' : '.', 'adm-addr-seg')}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          {editable && (
            <Button type="submit" kind="primary" size="sm" busy={busy} disabled={invalid || nothing}>
              Save prices
            </Button>
          )}
        </form>
      )}
      {data !== null && models.length === 0 && <p className="ur-small">No model is priced, and no runner pins one.</p>}
      {editable && invalid && <p className="warn-text">Each figure is a number, 0 or more, with its day as YYYY-MM-DD.</p>}
      {refused !== null && <Refusal error={refused} title="The prices were not saved" />}
      {saved !== null && (
        <p className="ur-small" role="status">
          Saved {saved.models.join(', ')}
          {saved.settings.updated_by !== null ? ` · ${saved.settings.updated_by}` : ''}
          {saved.settings.updated_at !== null ? ` · ${timeAgo(saved.settings.updated_at)}` : ''}
        </p>
      )}
    </FoldCard>
  )
}
