/**
 * THE PARTS OF EDIT A THAT ARE NOT FIELDS (docs/schedules.md §6.2, the
 * owner-picked "Edit A", 2026-10-11): the template tiles a new schedule starts
 * from, the template card in the side column, the Start choice and the
 * tenant's "N of 25 used".
 *
 * EVERYTHING HERE IS READ OFF THE SERVED CATALOGUE (`GET /v1/schedule-types`).
 * A type the catalogue marks unavailable is a greyed tile that says why, in
 * the catalogue's own words, as the runner catalogue shows an unavailable
 * profile; it is never hidden, because "why can I not pick this" is the
 * question a missing tile leaves open. The budget defaults are the type's
 * `budget_caps`: `schedules.resolve_budget` starts a schedule at its caps, so
 * one number is both the default and the most the type allows.
 */
import { useEffect, useState } from 'react'
import { loadSchedules, type Schedule, type ScheduleTypeRow } from './api'
import { errorHeading, type Result } from './fetch'
import { usd } from './measure'

/**
 * The per-tenant limit when the list does not serve one: `SCHEDULES_PER_TENANT`
 * in `swarm_api/schedules.py` (§1.1, §5.6). An admin may raise it per tenant,
 * and `GET /v1/schedules` does not say so yet, so a served `limit` wins.
 */
export const SCHEDULES_PER_TENANT = 25

export type Start = 'enabled' | 'dry_run' | 'paused'

const START: readonly { key: Start; label: string }[] = [
  { key: 'enabled', label: 'Enabled' },
  { key: 'dry_run', label: 'Dry run first' },
  { key: 'paused', label: 'Paused' },
]

const START_HINT: Readonly<Record<Start, string>> = {
  enabled: 'It fires at its next slot.',
  dry_run: 'It fires and records what it would do, and creates nothing. Turn dry run off under Edit when the record looks right.',
  paused: 'It is saved and does not fire until it is resumed.',
}

/** A gap in the largest whole unit: 1440 is "1 day", 90 is "90 min". */
export function gapWords(minutes: number): string {
  const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'}`
  if (minutes > 0 && minutes % 1440 === 0) return plural(minutes / 1440, 'day')
  if (minutes > 0 && minutes % 60 === 0) return plural(minutes / 60, 'hour')
  return `${minutes} min`
}

type BudgetKey = keyof NonNullable<Schedule['budget']>

/** "Default $15.00, the most this type allows", or why there is no default to state. */
export function budgetDefaultWords(t: ScheduleTypeRow | null, key: BudgetKey): string {
  if (t === null) return 'The type is not in the catalogue, so its default is not known here.'
  const v = t.budget_caps?.[key]
  if (v === undefined || v === null) return 'This type serves no default yet.'
  return `Default ${key === 'max_concurrent' ? String(v) : usd(v)}, the most this type allows.`
}

/** The type's default gate in words: "run auto · plan auto · merge approve". */
export function gateWords(g: Schedule['gate']): string {
  const out: string[] = []
  if (g.run !== undefined) out.push(`run ${g.run}`)
  if (g.plan !== undefined) out.push(`plan ${g.plan}`)
  if (g.merge !== undefined) out.push(`merge ${g.merge}`)
  if (typeof g.approvers === 'string') out.push(g.approvers === 'owner_only' ? 'the owner approves' : 'any member approves')
  return out.length === 0 ? 'none served' : out.join(' · ')
}

function budgetWords(t: ScheduleTypeRow): string {
  const c = t.budget_caps
  if (c === null || c === undefined) return 'none served yet'
  const parts: string[] = []
  if (c.per_run_usd !== undefined) parts.push(`${usd(c.per_run_usd)} / run`)
  if (c.per_day_usd !== undefined) parts.push(`${usd(c.per_day_usd)} / day`)
  if (c.max_concurrent !== undefined) parts.push(`${c.max_concurrent} live`)
  return parts.join(' · ')
}

/** The catalogue's tier for a type, where an API serves one; the list does not yet. */
function servedTier(t: ScheduleTypeRow): string | null {
  const tier = (t as ScheduleTypeRow & { tier?: unknown }).tier
  return typeof tier === 'string' && tier !== '' ? tier : null
}

/**
 * THE TEMPLATE TILES, in place of a type `<select>`. One tile per catalogue
 * entry, with radio semantics: a schedule is exactly one type. An unavailable
 * type is a disabled tile carrying its `disabled_reason`.
 */
export function TemplateTiles({ types, value, onPick }: { types: ScheduleTypeRow[]; value: string; onPick: (name: string) => void }) {
  return (
    <div className="sc-tiles" role="radiogroup" aria-label="Type" data-testid="sc-tiles">
      {types.map((t) => (
        <button
          key={t.name}
          type="button"
          role="radio"
          className={t.available ? 'sc-tile' : 'sc-tile is-off'}
          aria-checked={t.name === value}
          disabled={!t.available}
          title={t.available ? undefined : `Not available: ${t.disabled_reason ?? 'no reason served'}`}
          onClick={() => onPick(t.name)}
        >
          <b>{t.name}</b>
          <small>{t.available ? t.description : `Not available: ${t.disabled_reason ?? 'no reason served'}`}</small>
        </button>
      ))}
    </div>
  )
}

/**
 * THE TEMPLATE CARD: the type's tier, default gate, budget defaults and
 * shortest interval, beside the form. An edit passes the schedule's own tier,
 * which the server resolved from its gate (§4.1).
 */
export function TemplateCard({ type, tier }: { type: ScheduleTypeRow | null; tier?: string | null }) {
  if (type === null) {
    return (
      <section className="sc-tpl-card" aria-label="The template" data-testid="sc-tpl-card">
        <p className="c-hint">The type catalogue does not list this type, so its defaults are not shown.</p>
      </section>
    )
  }
  const shownTier = tier ?? servedTier(type)
  return (
    <section className="sc-tpl-card" aria-label="The template" data-testid="sc-tpl-card">
      <b className="sc-tpl-name">{type.name}</b>
      <dl className="sc-tpl-facts">
        <div>
          <dt>Tier</dt>
          <dd>{shownTier ?? 'set from the gate when the schedule is saved'}</dd>
        </div>
        <div>
          <dt>Gate</dt>
          <dd>{gateWords(type.default_gate)}</dd>
        </div>
        <div>
          <dt>Budget</dt>
          <dd>{budgetWords(type)}</dd>
        </div>
        <div>
          <dt>Shortest interval</dt>
          <dd>{gapWords(type.min_interval_minutes)}</dd>
        </div>
      </dl>
      {type.gate_notes ? <p className="c-hint">{type.gate_notes}</p> : null}
    </section>
  )
}

/** Enabled / Dry run first / Paused: how a new schedule starts. */
export function StartChoice({ value, onChange }: { value: Start; onChange: (s: Start) => void }) {
  return (
    <div className="c-field">
      <span className="c-lbl" id="sc-start-lbl">
        Start
      </span>
      <div className="c-seg" role="radiogroup" aria-labelledby="sc-start-lbl">
        {START.map((o) => (
          <button key={o.key} type="button" role="radio" aria-checked={o.key === value} onClick={() => onChange(o.key)}>
            {o.label}
          </button>
        ))}
      </div>
      <span className="c-hint">{START_HINT[value]}</span>
    </div>
  )
}

/** "tenant eng · 6 of 25 used", from the tenant's own list. */
export function TenantRoom() {
  const [r, setR] = useState<Result<{ schedules: Schedule[]; tenant_id: string }> | null>(null)
  useEffect(() => {
    let live = true
    void loadSchedules().then((next) => {
      if (live) setR(next)
    })
    return () => {
      live = false
    }
  }, [])
  if (r === null || r.status === 'loading') {
    return <p className="sc-room" data-testid="sc-room">Reading how many schedules this tenant has…</p>
  }
  if (r.status === 'error') {
    return (
      <p className="sc-room" data-testid="sc-room">
        Schedules used not read: {errorHeading(r.error)}
      </p>
    )
  }
  if (r.status === 'empty') return <p className="sc-room" data-testid="sc-room">0 of {SCHEDULES_PER_TENANT} used</p>
  const served = (r.data as { limit?: unknown }).limit
  const limit = typeof served === 'number' && served > 0 ? served : SCHEDULES_PER_TENANT
  const used = r.data.schedules.length
  // Only a served limit is sure enough to say a create will be refused: the
  // fallback is the platform default, and this tenant's may have been raised.
  const full = typeof served === 'number' && used >= limit
  return (
    <p className="sc-room" data-testid="sc-room" data-full={full ? 'true' : 'false'}>
      tenant {r.data.tenant_id} · {used} of {limit} used
      {full ? ' · this tenant has every schedule it may have, so a new one will be refused' : ''}
    </p>
  )
}
