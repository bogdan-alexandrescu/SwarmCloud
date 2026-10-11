/**
 * CREATE AND EDIT A SCHEDULE (docs/schedules.md §6.2, create variant A: one
 * page from a type template). Every field starts at the type's default, so
 * most schedules are a type, a repository and a time; the cron's words and
 * its next five firings sit under the field, so the whole decision is on one
 * page.
 *
 * THE BROWSER PARSES NO CRON. The words, the next five and the type's
 * minimum-interval refusal all come from `POST /v1/schedules:preview`, which
 * answers from the tick's own parser (§6.3) -- a second parser here would be a
 * second answer, and the form and the tick could disagree about when a
 * schedule fires.
 *
 * WHAT THE FORM SENDS IS WHAT THE PERSON CHANGED. A create sends a gate,
 * parameter or policy key only where it differs from the type's default, and
 * an edit only where it differs from the stored schedule: the server lays the
 * body over the stored document (`schedules.apply_edit`), so an edit of one
 * field never resets another. Nothing here is an image, a command or a
 * resource spec (invariant 10): the type is picked by name from the served
 * catalogue.
 *
 * MERGE `auto` IS NOT AN OPTION HERE. `PATCH` refuses it (422
 * `use_merge_switch`); the SD3 switch on the Gate tab is the one way there,
 * because it is audited and needs a second person (§4.2).
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import {
  createSchedule,
  loadRepositories,
  loadScheduleTypes,
  previewSchedule,
  updateSchedule,
  type ParamSchema,
  type Schedule,
  type ScheduleCreateBody,
  type ScheduleEditBody,
  type SchedulePreview,
  type ScheduleTypeRow,
} from './api'
import { Button } from './components'
import { errorHeading, type ApiError, type Result } from './fetch'
import { Absent } from './primitives'
import type { RepoRecord } from './RepositoriesData'
import { InApp, SCHEDULES_ADDRESS, scheduleAddress } from './Schedules'
import { Screen } from './Shell'

/** §6.3's presets. The raw field takes anything the tick's parser accepts. */
export const CRON_PRESETS: readonly { label: string; cron: string }[] = [
  { label: 'Every hour', cron: '0 * * * *' },
  { label: 'Weekdays at 09:00', cron: '0 9 * * 1-5' },
  { label: 'Nightly', cron: '0 2 * * *' },
  { label: 'Weekly on Monday', cron: '0 9 * * 1' },
]

/** How long the form waits after a keystroke before it asks for a preview. */
const PREVIEW_DEBOUNCE_MS = 400

/**
 * A new schedule's zone is the viewer's, because "09:00" typed by a person
 * means their 09:00; the API's own default is UTC, which is nobody's morning.
 */
function viewerZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

type Scope = Schedule['scope']['mode']
type Gate = Schedule['gate']

interface Draft {
  type: string
  name: string
  scopeMode: Scope
  repoIds: string[]
  cron: string
  timezone: string
  gate: Gate
  /** Text as typed; blank is "the type's default", never 0. */
  budget: { per_run_usd: string; per_day_usd: string; max_concurrent: string }
  policy: Schedule['policy']
  params: Record<string, unknown>
  startPaused: boolean
}

/**
 * PARAMETERS THAT RESTATE THE GATE ARE NOT FIELDS. issue-sweep's `merge` and
 * `plan_approval` repeat the gate for the runs a firing creates, and the
 * server keeps them equal to it (`schedules._sweep_follows_gate`): a value
 * that differs is refused, and `merge: auto` here would be the one option the
 * form says it never offers. They are drawn read-only, as the gate makes them,
 * and never sent.
 */
const GATE_RESTATED: Readonly<Record<string, Readonly<Record<string, (g: Gate) => string>>>> = {
  'issue-sweep': {
    plan_approval: (g) => (g.plan === 'approve' ? 'required' : g.plan === 'auto' ? 'auto' : ''),
    merge: (g) => g.merge ?? '',
  },
}

function restated(type: string, key: string): ((g: Gate) => string) | undefined {
  return GATE_RESTATED[type]?.[key]
}

/** `params` without the keys the gate restates for `type`. */
function ownParams(type: string, params: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(params)) if (restated(type, k) === undefined) out[k] = v
  return out
}

/** The parameters the form draws a field for: a scalar or an enum. Others are shown, not edited. */
function editable(p: ParamSchema): boolean {
  return Array.isArray(p.enum) || p.type === 'string' || p.type === 'integer' || p.type === 'number' || p.type === 'boolean'
}

function schemaDefaults(t: ScheduleTypeRow | null): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [k, p] of Object.entries(t?.params_schema.properties ?? {})) if (p.default !== undefined) out[k] = p.default
  return out
}

function budgetText(v: number | undefined): string {
  return v === undefined ? '' : String(v)
}

function draftFor(t: ScheduleTypeRow | null, s: Schedule | null, repos: RepoRecord[]): Draft {
  if (s !== null) {
    return {
      type: s.type,
      name: s.name,
      scopeMode: s.scope.mode,
      repoIds: [...(s.scope.repo_ids ?? [])],
      cron: s.cron,
      timezone: s.timezone,
      gate: { ...s.gate },
      budget: { per_run_usd: budgetText(s.budget.per_run_usd), per_day_usd: budgetText(s.budget.per_day_usd), max_concurrent: budgetText(s.budget.max_concurrent) },
      policy: { ...s.policy },
      params: { ...s.params },
      startPaused: false,
    }
  }
  const scopes = t === null ? [] : scopesOf(t)
  return {
    type: t?.name ?? '',
    name: '',
    scopeMode: (scopes[0] as Scope | undefined) ?? 'repos',
    repoIds: repos.length === 1 ? [repos[0]!.repo_id] : [],
    cron: '0 9 * * 1-5',
    timezone: viewerZone(),
    gate: { ...(t?.default_gate ?? {}) },
    budget: { per_run_usd: '', per_day_usd: '', max_concurrent: '' },
    policy: {},
    params: schemaDefaults(t),
    startPaused: false,
  }
}

function scopesOf(t: ScheduleTypeRow): string[] {
  return (t.creatable_scopes ?? t.scopes).filter((m) => m === 'repos' || m === 'all' || m === 'platform')
}

const SCOPE_WORD: Readonly<Record<Scope, string>> = {
  repos: 'Chosen repositories',
  all: 'Every registered repository',
  platform: 'The platform',
}

/** The keys of `next` whose value differs from `base`'s. */
function changed<T extends object>(next: T, base: T): Partial<T> {
  const out: Partial<T> = {}
  for (const k of Object.keys(next) as (keyof T)[]) {
    if (JSON.stringify(next[k]) !== JSON.stringify(base[k])) out[k] = next[k]
  }
  return out
}

function numberOrUndefined(text: string): number | undefined {
  if (text.trim() === '') return undefined
  const n = Number(text)
  return Number.isFinite(n) ? n : undefined
}

function budgetOf(d: Draft): Schedule['budget'] {
  const out: Schedule['budget'] = {}
  const run = numberOrUndefined(d.budget.per_run_usd)
  const day = numberOrUndefined(d.budget.per_day_usd)
  const live = numberOrUndefined(d.budget.max_concurrent)
  if (run !== undefined) out.per_run_usd = run
  if (day !== undefined) out.per_day_usd = day
  if (live !== undefined) out.max_concurrent = Math.trunc(live)
  return out
}

/** A refusal in words: the server's message, or the parser's. */
function refusalWords(r: unknown): string | null {
  if (r === null || r === undefined) return null
  if (typeof r === 'string') return r
  if (typeof r === 'object' && typeof (r as { message?: unknown }).message === 'string') return (r as { message: string }).message
  return JSON.stringify(r)
}

/** `2026-10-12T09:00:00+01:00` in the schedule's own zone, read off the ISO itself. */
function inZone(iso: string, zone: string): string {
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} ${zone}`
}

function inViewer(iso: string): string {
  const d = new Date(iso)
  return Number.isFinite(d.getTime())
    ? d.toLocaleString(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
    : iso
}

/**
 * THE CRON IN WORDS, AND THE NEXT FIVE (§6.3): in the schedule's zone and in
 * the viewer's. A bad expression is the parser's own reason, said in place.
 */
function CronPreview({ cron, timezone, type }: { cron: string; timezone: string; type: string }) {
  const [r, setR] = useState<Result<{ preview: SchedulePreview }> | null>(null)
  useEffect(() => {
    if (cron.trim() === '' || timezone.trim() === '') {
      setR(null)
      return
    }
    let live = true
    const timer = setTimeout(() => {
      void previewSchedule({ cron: cron.trim(), timezone: timezone.trim(), ...(type === '' ? {} : { type }) }).then((next) => {
        if (live) setR(next)
      })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [cron, timezone, type])
  if (r === null) return <p className="c-hint" data-testid="sc-preview">Type an expression to see it in words.</p>
  if (r.status === 'loading') return <p className="c-hint" data-testid="sc-preview">Reading the preview…</p>
  if (r.status === 'error') {
    return (
      <p className="c-hint" role="alert" data-testid="sc-preview" data-refused="true">
        {r.error.kind === 'invalid' ? `Not a schedule: ${r.error.message}` : `Preview not read: ${errorHeading(r.error)}`}
      </p>
    )
  }
  if (r.status === 'empty') return <p className="c-hint" data-testid="sc-preview">The preview answered nothing.</p>
  const p = r.data.preview
  const refusal = refusalWords(p.refusal)
  return (
    <div data-testid="sc-preview" data-refused={refusal === null ? 'false' : 'true'}>
      <p className="c-hint">
        <b>{p.words}</b> · at least {p.min_gap_minutes} min apart
      </p>
      {refusal !== null && (
        <p className="c-hint" role="alert">
          This type would refuse it: {refusal}
        </p>
      )}
      <ol className="sc-next" aria-label="The next five firings">
        {p.next.map((iso) => (
          <li key={iso}>
            <span className="mono">{inZone(iso, timezone)}</span> <span className="sc-sub">{inViewer(iso)} your time</span>
          </li>
        ))}
      </ol>
    </div>
  )
}

function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="c-field">
      <span className="c-lbl">{label}</span>
      {children}
      {hint !== undefined && <span className="c-hint">{hint}</span>}
    </label>
  )
}

function Choice<T extends string>({
  label,
  value,
  options,
  onChange,
  hint,
}: {
  label: string
  value: T | undefined
  options: readonly { value: T | ''; label: string }[]
  onChange: (v: T | undefined) => void
  hint?: ReactNode
}) {
  return (
    <Field label={label} hint={hint}>
      <select className="c-inp" value={value ?? ''} onChange={(e) => onChange(e.target.value === '' ? undefined : (e.target.value as T))}>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </Field>
  )
}

function ParamField({ name, schema, value, onChange }: { name: string; schema: ParamSchema; value: unknown; onChange: (v: unknown) => void }) {
  const label = schema.title ?? name
  const hint = schema.description
  if (Array.isArray(schema.enum)) {
    return (
      <Field label={label} hint={hint}>
        <select className="c-inp" value={String(value ?? '')} onChange={(e) => onChange(schema.enum!.find((x) => String(x) === e.target.value))}>
          {schema.enum.map((x) => (
            <option key={String(x)} value={String(x)}>
              {String(x)}
            </option>
          ))}
        </select>
      </Field>
    )
  }
  if (schema.type === 'boolean') {
    return (
      <label className="c-field">
        <span>
          <input type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} /> {label}
        </span>
        {hint !== undefined && <span className="c-hint">{hint}</span>}
      </label>
    )
  }
  const numeric = schema.type === 'integer' || schema.type === 'number'
  return (
    <Field label={label} hint={hint}>
      <input
        className="c-inp"
        type={numeric ? 'number' : 'text'}
        min={schema.minimum}
        max={schema.maximum}
        value={value === undefined || value === null ? '' : String(value)}
        onChange={(e) => {
          const t = e.target.value
          if (!numeric) onChange(t)
          else if (t.trim() === '') onChange(undefined)
          else onChange(schema.type === 'integer' ? Math.trunc(Number(t)) : Number(t))
        }}
      />
    </Field>
  )
}

const RUN_PLAN: readonly { value: 'auto' | 'approve'; label: string }[] = [
  { value: 'auto', label: 'auto' },
  { value: 'approve', label: 'approve' },
]

/** What the request would send: the create body, or the edit's changes. */
export function createBody(d: Draft, t: ScheduleTypeRow, requestId: string): ScheduleCreateBody {
  const body: ScheduleCreateBody = {
    name: d.name.trim(),
    type: d.type,
    scope: d.scopeMode === 'repos' ? { mode: 'repos', repo_ids: d.repoIds } : { mode: d.scopeMode },
    cron: d.cron.trim(),
    timezone: d.timezone.trim(),
    state: d.startPaused ? 'paused' : 'enabled',
    client_request_id: requestId,
  }
  const gate = changed(d.gate, t.default_gate)
  if (Object.keys(gate).length > 0) body.gate = gate
  const params = changed(ownParams(d.type, d.params), ownParams(d.type, schemaDefaults(t)))
  if (Object.keys(params).length > 0) body.params = params
  const budget = budgetOf(d)
  if (Object.keys(budget).length > 0) body.budget = budget
  const policy = changed(d.policy, {})
  if (Object.keys(policy).length > 0) body.policy = policy
  return body
}

export function editBody(d: Draft, s: Schedule): ScheduleEditBody {
  const body: ScheduleEditBody = { revision: s.revision }
  if (d.name.trim() !== s.name) body.name = d.name.trim()
  const scope: Schedule['scope'] = d.scopeMode === 'repos' ? { mode: 'repos', repo_ids: d.repoIds } : { mode: d.scopeMode }
  const stored: Schedule['scope'] = s.scope.mode === 'repos' ? { mode: 'repos', repo_ids: s.scope.repo_ids ?? [] } : { mode: s.scope.mode }
  if (JSON.stringify(scope) !== JSON.stringify(stored)) body.scope = scope
  if (d.cron.trim() !== s.cron) body.cron = d.cron.trim()
  if (d.timezone.trim() !== s.timezone) body.timezone = d.timezone.trim()
  const gate = changed(d.gate, s.gate)
  if (Object.keys(gate).length > 0) body.gate = gate
  const params = changed(ownParams(s.type, d.params), ownParams(s.type, s.params))
  if (Object.keys(params).length > 0) body.params = params
  const budget = changed(budgetOf(d), s.budget)
  if (Object.keys(budget).length > 0) body.budget = budget
  const policy = changed(d.policy, s.policy)
  if (Object.keys(policy).length > 0) body.policy = policy
  return body
}

function ScheduleForm({
  types,
  repos,
  schedule,
  onSaved,
  cancel,
}: {
  types: ScheduleTypeRow[]
  repos: Result<RepoRecord[]>
  schedule: Schedule | null
  /** The saved schedule, or null when the write took but its echo was unreadable. */
  onSaved: (s: Schedule | null) => void
  cancel: ReactNode
}) {
  const repoList = repos.status === 'ok' || repos.status === 'stale' ? repos.data : []
  const usable = useMemo(() => types.filter((t) => t.available), [types])
  const initialType = schedule === null ? (usable[0] ?? null) : (types.find((t) => t.name === schedule.type) ?? null)
  const [d, setD] = useState<Draft>(() => draftFor(initialType, schedule, repoList))
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  // One id per form: a double submit, or a retry after a lost answer, returns
  // the schedule the first request made instead of a second one (§7.1).
  const requestId = useRef(`ui-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`)
  const t = types.find((x) => x.name === d.type) ?? null
  const set = (patch: Partial<Draft>) => setD((prev) => ({ ...prev, ...patch }))

  const pickType = (name: string) => {
    const next = types.find((x) => x.name === name) ?? null
    setD((prev) => ({ ...draftFor(next, null, repoList), name: prev.name, cron: prev.cron, timezone: prev.timezone, repoIds: prev.repoIds }))
  }

  const scopes = t === null ? [d.scopeMode] : scopesOf(t)
  const why =
    d.name.trim() === ''
      ? 'Name the schedule first.'
      : d.type === ''
        ? 'Pick a type first.'
        : d.scopeMode === 'repos' && d.repoIds.length === 0
          ? 'Choose at least one repository.'
          : d.cron.trim() === ''
            ? 'Say when it runs.'
            : undefined

  const submit = async () => {
    setBusy(true)
    setRefused(null)
    const r =
      schedule === null
        ? await createSchedule(createBody(d, t!, requestId.current))
        : await updateSchedule(schedule.schedule_id, editBody(d, schedule))
    setBusy(false)
    if (r.status === 'ok') onSaved(r.data !== null && typeof r.data === 'object' && 'schedule' in r.data ? r.data.schedule : null)
    else if (r.status === 'error') setRefused(r.error)
  }

  // With no served type, the stored parameters are shown as they are: `{}` is
  // not `editable`, so each draws read-only.
  const params: [string, ParamSchema][] =
    t !== null ? Object.entries(t.params_schema.properties ?? {}) : Object.keys(d.params).map((k) => [k, {}])
  return (
    <form
      className="sc-form"
      data-testid="sc-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (why === undefined && !busy) void submit()
      }}
    >
      <div className="sc-facts">
        {schedule === null ? (
          <Field label="Type" hint={t === null ? undefined : t.description}>
            <select className="c-inp" value={d.type} onChange={(e) => pickType(e.target.value)}>
              {types.map((x) => (
                <option key={x.name} value={x.name} disabled={!x.available}>
                  {x.available ? x.name : `${x.name} (not available: ${x.disabled_reason ?? 'no reason served'})`}
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <Field label="Type" hint="A different type is a different schedule.">
            <input className="c-inp" value={schedule.type} readOnly />
          </Field>
        )}
        <Field label="Name">
          <input className="c-inp" value={d.name} maxLength={80} onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Choice<Scope>
          label="Scope"
          value={d.scopeMode}
          options={scopes.map((m) => ({ value: m as Scope, label: SCOPE_WORD[m as Scope] }))}
          onChange={(v) => set({ scopeMode: v ?? 'repos' })}
        />
      </div>

      {d.scopeMode === 'repos' && (
        <fieldset className="c-field">
          <legend className="c-lbl">Repositories</legend>
          {repos.status === 'loading' && <p className="c-hint">Reading the registered repositories…</p>}
          {repos.status === 'error' && <p className="c-hint" role="alert">Repositories not read: {errorHeading(repos.error)}</p>}
          {(repos.status === 'empty' || ((repos.status === 'ok' || repos.status === 'stale') && repoList.length === 0)) && (
            <p className="c-hint">No repository is registered. Register one under Work › Repositories first.</p>
          )}
          {repoList.map((r) => (
            <label key={r.repo_id} className="sc-check">
              <input
                type="checkbox"
                checked={d.repoIds.includes(r.repo_id)}
                onChange={(e) => set({ repoIds: e.target.checked ? [...d.repoIds, r.repo_id] : d.repoIds.filter((x) => x !== r.repo_id) })}
              />{' '}
              {r.owner}/{r.repo}
            </label>
          ))}
        </fieldset>
      )}

      <h3 className="sc-h3">When</h3>
      <div className="sc-presets" role="group" aria-label="Presets">
        {CRON_PRESETS.map((p) => (
          <Button key={p.cron} size="sm" kind={d.cron.trim() === p.cron ? 'primary' : 'secondary'} aria-pressed={d.cron.trim() === p.cron} onClick={() => set({ cron: p.cron })}>
            {p.label}
          </Button>
        ))}
      </div>
      <div className="sc-facts">
        <Field label="Cron expression" hint="Minute, hour, day of month, month, day of week.">
          <input className="c-inp is-mono" value={d.cron} spellCheck={false} onChange={(e) => set({ cron: e.target.value })} />
        </Field>
        <Field label="Timezone" hint="An IANA zone, such as Europe/London.">
          <input className="c-inp is-mono" value={d.timezone} spellCheck={false} onChange={(e) => set({ timezone: e.target.value })} />
        </Field>
      </div>
      <CronPreview cron={d.cron} timezone={d.timezone} type={d.type} />

      <h3 className="sc-h3">Gate</h3>
      <div className="sc-facts">
        <Choice label="Run" value={d.gate.run as 'auto' | 'approve' | undefined} options={RUN_PLAN} onChange={(v) => set({ gate: { ...d.gate, run: v } })}
          hint={t?.default_gate.run === undefined ? undefined : `type default: ${t.default_gate.run}`} />
        <Choice label="Plan" value={d.gate.plan as 'auto' | 'approve' | undefined} options={RUN_PLAN} onChange={(v) => set({ gate: { ...d.gate, plan: v } })}
          hint={t?.default_gate.plan === undefined ? undefined : `type default: ${t.default_gate.plan}`} />
        {d.gate.merge === 'auto' ? (
          <Field label="Merge" hint="Automatic, set by the merge switch on the Gate tab; it is changed back there.">
            <input className="c-inp" value="auto" readOnly />
          </Field>
        ) : (
          <Choice<'off' | 'approve'>
            label="Merge"
            value={d.gate.merge as 'off' | 'approve' | undefined}
            options={[
              { value: 'off', label: 'off' },
              { value: 'approve', label: 'approve' },
            ]}
            onChange={(v) => set({ gate: { ...d.gate, merge: v } })}
            hint="Automatic merging is only ever the audited switch on the Gate tab, never this form."
          />
        )}
        {typeof d.gate.approvers === 'string' || d.gate.approvers === undefined ? (
          <Choice<'members' | 'owner_only'>
            label="Approvers"
            value={d.gate.approvers as 'members' | 'owner_only' | undefined}
            options={[
              { value: 'members', label: 'any member' },
              { value: 'owner_only', label: 'the owner only' },
            ]}
            onChange={(v) => set({ gate: { ...d.gate, approvers: v } })}
          />
        ) : (
          <Field label="Approvers" hint="A named list, set through the API; unchanged here.">
            <input className="c-inp" value={(d.gate.approvers as unknown as string[]).join(', ')} readOnly />
          </Field>
        )}
        <Field label="Approval expires after (hours)">
          <input
            className="c-inp"
            type="number"
            min={1}
            value={d.gate.approval_ttl_hours ?? ''}
            onChange={(e) => set({ gate: { ...d.gate, approval_ttl_hours: e.target.value === '' ? undefined : Math.trunc(Number(e.target.value)) } })}
          />
        </Field>
      </div>
      <p className="c-hint">A gate below the type's default needs a platform admin; the hard stops hold whatever the gate.</p>

      <h3 className="sc-h3">Budget</h3>
      <div className="sc-facts">
        <Field label="Per run (USD)" hint="Blank: the type's default.">
          <input className="c-inp" type="number" min={0} step="0.01" value={d.budget.per_run_usd} onChange={(e) => set({ budget: { ...d.budget, per_run_usd: e.target.value } })} />
        </Field>
        <Field label="Per day (USD)" hint="Blank: the type's default.">
          <input className="c-inp" type="number" min={0} step="0.01" value={d.budget.per_day_usd} onChange={(e) => set({ budget: { ...d.budget, per_day_usd: e.target.value } })} />
        </Field>
        <Field label="Live runs at most" hint="Blank: the type's default.">
          <input className="c-inp" type="number" min={1} value={d.budget.max_concurrent} onChange={(e) => set({ budget: { ...d.budget, max_concurrent: e.target.value } })} />
        </Field>
      </div>

      {params.length > 0 && (
        <>
          <h3 className="sc-h3">Parameters</h3>
          <div className="sc-facts">
            {params.map(([k, p]) => {
              const follows = restated(d.type, k)
              return follows !== undefined ? (
                <Field key={k} label={p.title ?? k} hint="Follows the gate above; it is changed there, never here.">
                  <input className="c-inp" value={follows(d.gate)} readOnly data-testid={`sc-param-${k}`} />
                </Field>
              ) : editable(p) ? (
                <ParamField key={k} name={k} schema={p} value={d.params[k]} onChange={(v) => set({ params: { ...d.params, [k]: v } })} />
              ) : (
                <Field key={k} label={p.title ?? k} hint="Set through the API; unchanged here.">
                  <input className="c-inp is-mono" value={d.params[k] === undefined ? '' : JSON.stringify(d.params[k])} readOnly />
                </Field>
              )
            })}
          </div>
        </>
      )}

      <h3 className="sc-h3">Policy</h3>
      <div className="sc-facts">
        <Choice<'skip' | 'queue_one'>
          label="If the last firing is still running"
          value={d.policy.overlap as 'skip' | 'queue_one' | undefined}
          options={[
            { value: '', label: "the type's default" },
            { value: 'skip', label: 'skip this one' },
            { value: 'queue_one', label: 'queue one' },
          ]}
          onChange={(v) => set({ policy: { ...d.policy, overlap: v } })}
        />
        <Choice<'skip' | 'run_once'>
          label="After an outage"
          value={d.policy.catch_up as 'skip' | 'run_once' | undefined}
          options={[
            { value: '', label: "the type's default" },
            { value: 'skip', label: 'skip what was missed' },
            { value: 'run_once', label: 'run once' },
          ]}
          onChange={(v) => set({ policy: { ...d.policy, catch_up: v } })}
        />
        <label className="c-field">
          <span>
            <input type="checkbox" checked={d.policy.dry_run === true} onChange={(e) => set({ policy: { ...d.policy, dry_run: e.target.checked } })} /> Dry run
          </span>
          <span className="c-hint">Fires and records what it would do, and creates nothing.</span>
        </label>
        {schedule === null && (
          <label className="c-field">
            <span>
              <input type="checkbox" checked={d.startPaused} onChange={(e) => set({ startPaused: e.target.checked })} /> Start paused
            </span>
          </label>
        )}
      </div>

      {refused !== null && (
        <p className="c-hint" role="alert" data-code={refused.code ?? ''}>
          {refused.code === 'schedule_changed'
            ? 'Someone changed this schedule since you opened it. Close the editor to read it again, then edit.'
            : `Not saved: ${refused.message}`}
        </p>
      )}
      <div className="c-acts">
        {cancel}
        <Button kind="primary" type="submit" busy={busy ? 'Saving…' : false} disabledReason={why}>
          {schedule === null ? 'Create schedule' : 'Save changes'}
        </Button>
      </div>
    </form>
  )
}

function useRepositories(): Result<RepoRecord[]> {
  const [repos, setRepos] = useState<Result<RepoRecord[]>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    void loadRepositories().then((r) => {
      if (live) setRepos(r)
    })
    return () => {
      live = false
    }
  }, [])
  return repos
}

function NewScheduleBody({ types, go }: { types: ScheduleTypeRow[]; go: (to: string) => void }) {
  const repos = useRepositories()
  if (types.length === 0) {
    return <Absent kind="zero" heading="No type you may create" say="The catalogue was read and offers this account no schedule type." />
  }
  // The form's first draft reads the repositories (one registration is
  // preselected), so it is drawn once that read has answered.
  if (repos.status === 'loading') return <p className="sb-note">Reading the registered repositories…</p>
  return (
    <ScheduleForm
      types={types}
      repos={repos}
      schedule={null}
      onSaved={(s) => go(s === null ? SCHEDULES_ADDRESS : scheduleAddress(s.schedule_id))}
      cancel={
        <InApp go={go} to={SCHEDULES_ADDRESS} className="c-btn is-ghost">
          Cancel
        </InApp>
      }
    />
  )
}

/** `/automate/schedules/new`: the create form, from the served catalogue. */
export function NewSchedule({ go }: { go: (to: string) => void }) {
  return (
    <div className="sc-detail">
      <InApp go={go} to={SCHEDULES_ADDRESS} className="sc-back">‹ All schedules</InApp>
      <Screen title="New schedule" load={loadScheduleTypes}>
        {(d) => <NewScheduleBody types={d.types} go={go} />}
      </Screen>
    </div>
  )
}

/** The edit form, in place on one schedule's page. */
export function EditSchedule({ schedule, onSaved, onCancel }: { schedule: Schedule; onSaved: () => void; onCancel: () => void }) {
  const repos = useRepositories()
  const [types, setTypes] = useState<Result<{ types: ScheduleTypeRow[] }>>({ status: 'loading', since: Date.now() })
  useEffect(() => {
    let live = true
    void loadScheduleTypes().then((r) => {
      if (live) setTypes(r)
    })
    return () => {
      live = false
    }
  }, [])
  if (types.status === 'loading' || repos.status === 'loading') return <p className="sb-note">Reading the type and the repositories…</p>
  // A type this account may not create (an owner-only scope) is not in the
  // catalogue it is served; the schedule is still editable, with its
  // parameters shown as stored rather than drawn from a schema.
  const list = types.status === 'ok' || types.status === 'stale' ? types.data.types : []
  return (
    <section className="ctl-card" aria-label="Edit this schedule">
      <h2 className="sc-h2">Edit</h2>
      {types.status === 'error' && <p className="c-hint">The type catalogue was not read ({errorHeading(types.error)}), so the type's defaults are not shown.</p>}
      <ScheduleForm
        types={list}
        repos={repos}
        schedule={schedule}
        onSaved={() => onSaved()}
        cancel={
          <Button kind="ghost" onClick={onCancel}>
            Cancel
          </Button>
        }
      />
    </section>
  )
}
