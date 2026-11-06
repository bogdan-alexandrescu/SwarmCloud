/**
 * CREATE AND EDIT A SCHEDULE (docs/schedules.md §6.2; Automate proposal B's
 * A3, picked by BGD 2026-10-26, #2417, docs/web-ui/mockups/automate-b.html
 * #A3a and #A3b). One page of numbered cards -- type and template, target,
 * cadence, gates and approvers, budget and tripwire, and how a new one starts
 * -- beside a sheet that keeps every problem, the change against the stored
 * revision, the next five firings and Create in view. Every field starts at
 * the type's default, so most schedules are a type, a repository and a time.
 *
 * THE SHEET DRAWS THE SERVER'S OWN ANSWERS: the preview's refusal and the
 * type's served `budget_caps`, judged as `schedules.resolve_budget` judges
 * them, so a problem shows before Create is pressed, not after. An edit of a
 * paused schedule leads with "Saving does not resume": resuming is its own
 * step on the schedule's page.
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
import { cloneElement, isValidElement, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type KeyboardEvent, type ReactElement, type ReactNode, type RefObject } from 'react'
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
import { InApp, SCHEDULES_ADDRESS, firingReaderTime, firingWallTime, scheduleAddress } from './Schedules'
import { budgetDefaultWords, gapWords, StartChoice, TemplateCard, TemplateTiles, TenantRoom, type Start } from './ScheduleTemplates'
import { Screen } from './Shell'
import { usd } from './measure'
import './styles/automate-edit.css'

/** §6.3's presets. The raw field takes anything the tick's parser accepts. */
export const CRON_PRESETS: readonly { label: string; cron: string }[] = [
  { label: 'Every hour', cron: '0 * * * *' },
  { label: 'Weekdays at 09:00', cron: '0 9 * * 1-5' },
  { label: 'Nightly', cron: '0 2 * * *' },
  { label: 'Weekly on Monday', cron: '0 9 * * 1' },
]

/**
 * THE HARD STOPS, SAID ONCE (QA1 automate-09). The form's Gates card and the
 * schedule page's Gate tab said different things -- "held for the owner"
 * against "held for a second member, or in a platform repository for the
 * owner", and workflows "held" against "refused". This is the page's newer
 * wording, which is the server's: a protected change is held for a second
 * member (the owner, in a platform repository) whatever the gate, and a plan
 * touching `.github/workflows/` is refused because SwarmCloud cannot push it.
 * Both places draw this, so they cannot drift apart again.
 */
export function HardStops() {
  return (
    <>
      A change to IAM, the frozen contract, security, CODEOWNERS or a protected path is held for a second member, or in a platform repository for
      the owner, whatever the gate; a change to <code>.github/workflows/</code> is refused.
    </>
  )
}

/**
 * A served calendar day, `2026-10-28`, as the console writes a day: "Oct 28"
 * (QA1 automate-21). The day is a date, not an instant, so it is read and
 * written in UTC and never shifts by the viewer's zone. Anything that is not
 * a real `YYYY-MM-DD` is shown as served rather than guessed at.
 */
export function dayWords(day: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  if (m === null) return day
  const at = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])))
  if (at.getUTCMonth() !== Number(m[2]) - 1 || at.getUTCDate() !== Number(m[3])) return day
  return at.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })
}

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
  /** Text as typed; blank is the type's default (its `budget_caps`), never 0. */
  budget: { per_run_usd: string; per_day_usd: string; max_concurrent: string }
  policy: Schedule['policy']
  params: Record<string, unknown>
  /** A create's Start choice. "Dry run first" is enabled with `policy.dry_run`. */
  start: Start
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

/**
 * The parts of a served JSON-schema property the form reads beyond
 * `ParamSchema`: a list's items and bounds, a nullable value's `anyOf`, and a
 * reference to a model the form has no editor for.
 */
type Prop = ParamSchema & {
  items?: { type?: string; enum?: unknown[] }
  minItems?: number
  maxItems?: number
  anyOf?: { type?: string }[]
  $ref?: string
}

/** A list of strings, or of one enum's values: `labels_include`, `paths`, `focus`. */
function isList(p: Prop): boolean {
  return p.type === 'array' && (p.items?.type === 'string' || Array.isArray(p.items?.enum))
}

/** `string | null`, served as `anyOf`: `branch`, `post_to_issue`, `file_issues_repo_id`. Blank is null. */
function isNullableText(p: Prop): boolean {
  const kinds = (p.anyOf ?? []).map((x) => x.type)
  return p.type === undefined && kinds.length === 2 && kinds.includes('string') && kinds.includes('null')
}

/**
 * The parameters the form draws a field for: a scalar, an enum, a list of
 * strings and a nullable string. A map (`pr-shepherd`'s `labels`) and a model
 * (`custom-prompt`'s `spec`) are shown, not edited, and say why.
 */
function editable(p: Prop): boolean {
  return (
    Array.isArray(p.enum) ||
    p.type === 'string' ||
    p.type === 'integer' ||
    p.type === 'number' ||
    p.type === 'boolean' ||
    isList(p) ||
    isNullableText(p)
  )
}

/** Why a parameter is not a field here, in the person's words. */
function notEditableWhy(p: Prop): string {
  if (p.$ref !== undefined) return 'A workflow spec, which this form cannot write: it is set through the API (POST /v1/schedules with params.spec) or the MCP schedule tools.'
  if (p.type === 'object') return 'A map of settings, set through the API; shown here as stored.'
  return 'Set through the API; unchanged here.'
}

/** A value the server needs and this form leaves unset: a required key not set, or a list emptier than its minimum. */
function missingParam(p: Prop, required: boolean, v: unknown): boolean {
  if (Array.isArray(v)) return (p.minItems ?? 0) > 0 && v.length < (p.minItems ?? 0)
  return required && (v === undefined || v === null || v === '')
}

function schemaDefaults(t: ScheduleTypeRow | null): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [k, p] of Object.entries(t?.params_schema.properties ?? {})) if (p.default !== undefined) out[k] = p.default
  return out
}

function budgetText(v: number | undefined): string {
  return v === undefined ? '' : String(v)
}

/** The type's default in the empty field, so a blank field shows what it means. */
function placeholderOf(t: ScheduleTypeRow | null, key: keyof Schedule['budget']): string | undefined {
  const v = t?.budget_caps?.[key]
  return v === undefined || v === null ? undefined : String(v)
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
      start: 'enabled',
    }
  }
  const scopes = t === null ? [] : scopesOf(t)
  return {
    type: t?.name ?? '',
    name: '',
    // Chosen repositories first wherever the type offers them (AUT-2): a
    // form left at its defaults must not reach every registered repository.
    scopeMode: scopes.includes('repos') ? 'repos' : ((scopes[0] as Scope | undefined) ?? 'repos'),
    repoIds: repos.length === 1 ? [repos[0]!.repo_id] : [],
    cron: '0 9 * * 1-5',
    timezone: viewerZone(),
    gate: { ...(t?.default_gate ?? {}) },
    budget: { per_run_usd: '', per_day_usd: '', max_concurrent: '' },
    policy: {},
    params: schemaDefaults(t),
    // DRY RUN FIRST (AUT-2; the mockup's create variant A): a new schedule
    // records what it would do and creates nothing until the person turns
    // dry run off. Enabled is one deliberate click away, never the default.
    start: 'dry_run',
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

/** A scalar or a list of scalars, in words; null for anything with structure. */
export function plainValue(v: unknown): string | null {
  if (v === null || v === undefined) return '— not set'
  if (typeof v === 'string') return v
  if (typeof v === 'number' || typeof v === 'boolean') return String(v)
  if (Array.isArray(v) && v.every((x) => x === null || ['string', 'number', 'boolean'].includes(typeof x))) {
    return v.length === 0 ? 'none' : v.map(String).join(', ')
  }
  return null
}

/**
 * A value this form shows but cannot write, in words rather than JSON (visual
 * QA V247): a list is its items, comma-separated, never `["swarm"]`; a value
 * with structure is one `key: value` line per key, so a spec's prompt is read
 * whole in a box that grows to it, not cut at `{"name":…` in a one-line input.
 */
export function paramText(v: unknown): string {
  const plain = plainValue(v)
  if (plain !== null) return plain
  if (Array.isArray(v)) return v.map((x) => paramText(x)).join('\n')
  return Object.entries(v as Record<string, unknown>)
    .map(([k, x]) => {
      const said = paramText(x)
      return said.includes('\n') ? `${k}:\n${said.replace(/^/gm, '  ')}` : `${k}: ${said}`
    })
    .join('\n')
}

/** A refusal in words: the server's message, or the parser's. */
function refusalWords(r: unknown): string | null {
  if (r === null || r === undefined) return null
  if (typeof r === 'string') return r
  if (typeof r === 'object' && typeof (r as { message?: unknown }).message === 'string') return (r as { message: string }).message
  return JSON.stringify(r)
}

/**
 * ONE PREVIEW READ PER FORM, debounced. The sheet's problem list, the error
 * under the cron field and the next five all draw this one answer, so they
 * cannot disagree and the route is asked once per pause in typing. `fresh` is
 * false from the keystroke until the next answer: an expression being retyped
 * is not yet refused, so a stale refusal stops nothing.
 */
function usePreview(cron: string, timezone: string, type: string): { r: Result<{ preview: SchedulePreview }> | null; fresh: boolean } {
  const [r, setR] = useState<Result<{ preview: SchedulePreview }> | null>(null)
  const [fresh, setFresh] = useState(false)
  useEffect(() => {
    setFresh(false)
    if (cron.trim() === '' || timezone.trim() === '') {
      setR(null)
      return
    }
    let live = true
    const timer = setTimeout(() => {
      void previewSchedule({ cron: cron.trim(), timezone: timezone.trim(), ...(type === '' ? {} : { type }) }).then((next) => {
        if (!live) return
        setR(next)
        setFresh(true)
      })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [cron, timezone, type])
  return { r, fresh }
}

/**
 * Why the preview stops a save, or null: the parser's 422 and the type's
 * minimum-interval refusal alike (AUT-10). A preview that was not read stops
 * nothing -- the server still checks the expression -- and one being retyped
 * stops nothing until it answers. THE REASON IS SAID ONCE WHERE THE PREVIEW
 * SAYS IT (review of AUTO-A3): the preview leads with "Not a schedule" /
 * "This type would refuse it" and the reason, so the sheet's problem names
 * the cadence without the reason (`cadenceProblem`), and the cron field
 * carries the reason in place only beside the cards, where the preview is in
 * the sheet; on one column the preview sits right under the field instead.
 */
/** The sheet's line for a cadence `previewBlocks` stops: what is wrong, not the reason the preview says. */
function cadenceProblem(r: Result<{ preview: SchedulePreview }> | null): string {
  return r !== null && r.status === 'error' ? 'Cadence: not a schedule.' : 'Cadence: this type refuses it.'
}

export function previewBlocks(p: { r: Result<{ preview: SchedulePreview }> | null; fresh: boolean }): string | null {
  if (!p.fresh || p.r === null) return null
  if (p.r.status === 'error') return p.r.error.kind === 'invalid' ? p.r.error.message : null
  if (p.r.status !== 'ok' && p.r.status !== 'stale') return null
  return refusalWords(p.r.data.preview.refusal)
}

/**
 * THE CRON IN WORDS, AND THE NEXT FIVE (§6.3): in the schedule's zone and in
 * the viewer's. A bad expression is the parser's own reason, said in place.
 */
function CronPreview({ r, timezone }: { r: Result<{ preview: SchedulePreview }> | null; timezone: string }) {
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
  // NO STRAY SEPARATOR (visual QA V244): a refused cadence can come back with
  // no words, and the line began "· at least 1 min apart". The refusal is the
  // first thing said, in the bad ink, because it is what stops the save.
  const words = typeof p.words === 'string' && p.words.trim() !== '' ? p.words : null
  return (
    <div data-testid="sc-preview" data-refused={refusal === null ? 'false' : 'true'}>
      {refusal !== null && (
        <p className="c-hint sc-refused" role="alert">
          This type would refuse it: {refusal}
        </p>
      )}
      <p className="c-hint">
        {words !== null && (
          <>
            <b>{words}</b> ·{' '}
          </>
        )}
        {/* In the template card's own words (QA1 automate-21): "at least
            1440 min apart" sat beside "Shortest interval 1 day". */}
        at least {gapWords(p.min_gap_minutes)} apart
      </p>
      {p.next.length > 0 && (
        <>
          {/* THE ZONE ONCE, NOT ON EVERY LINE (visual QA V246): five copies of
              "America/Argentina/Buenos_Aires" took three lines each, and "your
              time" was orphaned onto a line of its own. */}
          <p className="c-hint">
            Next five, in {timezone}:
          </p>
          <ol className={refusal === null ? 'sc-next' : 'sc-next is-refused'} aria-label="The next five firings">
            {/* ONE FORMAT, AS THE SHEET'S CHIPS (QA2 automate2-07): "Fri, Oct
                30 09:11", the reader's time beside it only when it is a
                different wall time. */}
            {p.next.map((iso) => {
              const yours = firingReaderTime(iso)
              return (
                <li key={iso}>
                  <span>{firingWallTime(iso, timezone)}</span>
                  {yours !== null && <> <span className="sc-sub sc-yours">{yours} your time</span></>}
                </li>
              )
            })}
          </ol>
          {/* A3a: the firings a refused cadence would have had, struck
              through, so the sheet shows what the server will not do. */}
          {refusal !== null && <p className="c-hint">Struck through: the server refuses this cadence.</p>}
        </>
      )}
    </div>
  )
}

/**
 * A labelled control. The `<label>` wraps the control so a click on the words
 * reaches it, and the control is NAMED by the words alone and DESCRIBED by the
 * hint (AUT-16): a wrapped `<select>` otherwise takes every option and the
 * hint as its accessible name ("Type issue-sweep issue-plan-only …").
 */
function Field({ label, hint, error, children }: { label: string; hint?: ReactNode; error?: string | null; children: ReactNode }) {
  const id = useId()
  const hinted = hint !== undefined && hint !== ''
  const wrong = error !== undefined && error !== null && error !== ''
  const described = [hinted ? `${id}-h` : null, wrong ? `${id}-e` : null].filter((x): x is string => x !== null)
  const control = isValidElement(children)
    ? cloneElement(children as ReactElement<Record<string, unknown>>, {
        'aria-labelledby': `${id}-l`,
        ...(described.length > 0 ? { 'aria-describedby': described.join(' ') } : {}),
        ...(wrong ? { 'aria-invalid': 'true' } : {}),
      })
    : children
  return (
    <label className="c-field">
      <span className="c-lbl" id={`${id}-l`}>{label}</span>
      {control}
      {/* A3a: the field's own problem, in place, in the bad ink; the sheet
          lists it again with the rest so nothing is found only by scrolling. */}
      {wrong && (
        <span className="ae-err" id={`${id}-e`}>
          {error}
        </span>
      )}
      {hinted && <span className="c-hint" id={`${id}-h`}>{hint}</span>}
    </label>
  )
}

/** A checkbox named by its words and described by its hint, as `Field` does for the rest. */
function Check({ label, hint, checked, onChange }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void }) {
  const id = useId()
  return (
    <label className="c-field">
      <span>
        <input
          type="checkbox"
          checked={checked}
          aria-labelledby={`${id}-l`}
          aria-describedby={hint === undefined ? undefined : `${id}-h`}
          onChange={(e) => onChange(e.target.checked)}
        />{' '}
        <span id={`${id}-l`}>{label}</span>
      </span>
      {hint !== undefined && <span className="c-hint" id={`${id}-h`}>{hint}</span>}
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

/**
 * A SEGMENTED CONTROL, AS PROPOSAL B DRAWS SCOPE, RUN, PLAN, MERGE AND WHO MAY
 * APPROVE (A3a cards 2 and 4; BGD picked it over dropdowns, 2026-10-28). One
 * radio group named by its label, every value visible at once and the chosen
 * one checked, in the shared `.c-seg` the Cadence presets and Start already
 * draw. A value the API refuses carries its reason, under the group and as
 * the button's description. Arrows move and choose within the group, as a
 * radio group does; Tab enters on the checked value and leaves the group.
 * The wrapper is not a `<label>`: a label around several buttons hands every
 * click on its words to the first of them. `wide` spans every column of a
 * `.sc-facts` grid, as Scope's row does in A3a card 2.
 */
function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  hint,
  wide = false,
}: {
  label: string
  value: T | undefined
  options: readonly { value: T; label: string; disabled?: string }[]
  onChange: (v: T) => void
  hint?: ReactNode
  wide?: boolean
}) {
  const id = useId()
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  const live = options.map((o, i) => (o.disabled === undefined ? i : -1)).filter((i) => i >= 0)
  const checked = options.findIndex((o) => o.value === value)
  // The checked value takes the Tab stop; with none checked, the first live one.
  const stop = checked >= 0 && options[checked]!.disabled === undefined ? checked : (live[0] ?? -1)
  const refused = options.filter((o) => o.disabled !== undefined)
  const move = (from: number, step: 1 | -1) => {
    const next = live[(live.indexOf(from) + step + live.length) % live.length]
    if (next === undefined) return
    onChange(options[next]!.value)
    refs.current[next]?.focus()
  }
  const hinted = hint !== undefined && hint !== ''
  return (
    <div className={wide ? 'c-field sc-wide' : 'c-field'}>
      <span className="c-lbl" id={`${id}-l`}>
        {label}
      </span>
      <div className="c-seg ae-seg" role="radiogroup" aria-labelledby={`${id}-l`} {...(hinted ? { 'aria-describedby': `${id}-h` } : {})}>
        {options.map((o, i) => (
          <button
            key={o.value}
            ref={(el) => {
              refs.current[i] = el
            }}
            type="button"
            role="radio"
            aria-checked={i === checked}
            disabled={o.disabled !== undefined}
            tabIndex={i === stop ? 0 : -1}
            {...(o.disabled === undefined ? {} : { 'aria-describedby': `${id}-r${i}`, title: o.disabled })}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowRight' || e.key === 'ArrowDown') move(i, 1)
              else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') move(i, -1)
              else return
              e.preventDefault()
            }}
          >
            {o.label}
          </button>
        ))}
      </div>
      {refused.map((o) => (
        <span key={o.value} className="c-hint" id={`${id}-r${options.indexOf(o)}`}>
          {o.label}: {o.disabled}
        </span>
      ))}
      {hinted && (
        <span className="c-hint" id={`${id}-h`}>
          {hint}
        </span>
      )}
    </div>
  )
}

/** "security, needs-owner" -- a served list default, in the words a blank field means. */
function listWords(v: unknown): string {
  return Array.isArray(v) ? v.map(String).join(', ') : ''
}

/**
 * A LIST OF STRINGS, comma-separated as typed (AUT-12). The text is the
 * field's own, so "a, " is not cut back to "a" while it is being typed; the
 * list is what goes up. A list of one enum's values is a checkbox each.
 */
function ListField({ label, schema, value, onChange }: { label: string; schema: Prop; value: unknown; onChange: (v: string[]) => void }) {
  const list = Array.isArray(value) ? value.map(String) : []
  const [text, setText] = useState(list.join(', '))
  const dflt = Array.isArray(schema.default) ? schema.default : undefined
  const hint = [
    schema.description,
    dflt === undefined ? null : dflt.length === 0 ? 'Default: none.' : `Default: ${listWords(dflt)}.`,
    schema.minItems !== undefined && schema.minItems > 0 ? `At least ${schema.minItems}.` : null,
  ]
    .filter((x): x is string => typeof x === 'string' && x !== '')
    .join(' ')
  const options = schema.items?.enum
  if (Array.isArray(options)) {
    return (
      <fieldset className="c-field sc-list-field">
        <legend className="c-lbl">{label}</legend>
        {options.map((o) => (
          <label key={String(o)} className="sc-check">
            <input
              type="checkbox"
              checked={list.includes(String(o))}
              onChange={(e) => onChange(e.target.checked ? [...list, String(o)] : list.filter((x) => x !== String(o)))}
            />{' '}
            {String(o)}
          </label>
        ))}
        {hint !== '' && <span className="c-hint">{hint}</span>}
      </fieldset>
    )
  }
  return (
    <Field label={label} hint={(hint === '' ? '' : `${hint} `) + 'Separate them with commas.'}>
      <input
        className="c-inp"
        value={text}
        spellCheck={false}
        placeholder={dflt === undefined || dflt.length === 0 ? 'none' : undefined}
        onChange={(e) => {
          setText(e.target.value)
          onChange(
            e.target.value
              .split(',')
              .map((x) => x.trim())
              .filter((x) => x !== ''),
          )
        }}
      />
    </Field>
  )
}

function ParamField({ name, schema, value, onChange }: { name: string; schema: Prop; value: unknown; onChange: (v: unknown) => void }) {
  const label = schema.title ?? name
  const hint = schema.description
  if (isList(schema)) return <ListField label={label} schema={schema} value={value} onChange={onChange} />
  if (isNullableText(schema)) {
    return (
      <Field label={label} hint={[hint, 'Blank: not set.'].filter(Boolean).join(' ')}>
        <input
          className="c-inp"
          value={typeof value === 'string' ? value : ''}
          spellCheck={false}
          onChange={(e) => onChange(e.target.value.trim() === '' ? null : e.target.value)}
        />
      </Field>
    )
  }
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
  if (schema.type === 'boolean') return <Check label={label} hint={hint} checked={value === true} onChange={onChange} />
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

/** Each gate point's values, least strict first, as `scheduletypes.GATE_POINTS` orders them. */
const GATE_ORDER: Readonly<Record<'run' | 'plan' | 'merge', readonly string[]>> = {
  run: ['auto', 'approve'],
  plan: ['auto', 'approve'],
  merge: ['auto', 'approve', 'off'],
}

/**
 * WHY A GATE VALUE IS OFF, OR NOTHING. A value below the type's floor is
 * refused for everyone, an admin included (422 `gate_below_floor`), so it is
 * drawn disabled with that reason. Below the default is not disabled: an
 * admin may set it, and the card's caption says so.
 */
function floorWhy(t: ScheduleTypeRow | null, point: 'run' | 'plan' | 'merge', value: string): string | undefined {
  const floor = t?.floor_gate?.[point]
  if (t === null || floor === undefined) return undefined
  const order = GATE_ORDER[point]
  return order.indexOf(value) < order.indexOf(floor) ? `below ${t.name}’s floor (${floor}), refused for everyone` : undefined
}

/** A gate point's segments, each value below the type's floor disabled with its reason. */
function gateOptions<V extends string>(t: ScheduleTypeRow | null, point: 'run' | 'plan' | 'merge', options: readonly { value: V; label: string }[]) {
  return options.map((o) => {
    const why = floorWhy(t, point, o.value)
    return why === undefined ? o : { ...o, disabled: why }
  })
}

/**
 * WHAT THE PARAMETERS STILL NEED (AUT-5). A type whose required parameter
 * this form cannot write -- custom-prompt's `spec` -- cannot be created here,
 * and Create says so instead of sending a body the server refuses. A field
 * the form does draw says which value it is missing.
 */
function paramsWhy(t: ScheduleTypeRow | null, d: Draft): { say: string; key: string } | null {
  if (t === null) return null
  const required = t.params_schema.required ?? []
  for (const [k, p] of Object.entries(t.params_schema.properties ?? {}) as [string, Prop][]) {
    if (restated(d.type, k) !== undefined) continue
    const name = p.title ?? k
    if (!editable(p)) {
      if (required.includes(k) && d.params[k] === undefined) {
        return { say: `${t.name} needs ${name}, which this form cannot write. Create it through the API (POST /v1/schedules with params.${k}) or the MCP schedule tools.`, key: k }
      }
      continue
    }
    if (missingParam(p, required.includes(k), d.params[k])) {
      return { say: Array.isArray(d.params[k]) ? `${name} needs at least ${p.minItems ?? 1}.` : `${name} needs a value.`, key: k }
    }
  }
  return null
}

/** What the request would send: the create body, or the edit's changes. */
export function createBody(d: Draft, t: ScheduleTypeRow, requestId: string): ScheduleCreateBody {
  const body: ScheduleCreateBody = {
    name: d.name.trim(),
    type: d.type,
    scope: d.scopeMode === 'repos' ? { mode: 'repos', repo_ids: d.repoIds } : { mode: d.scopeMode },
    cron: d.cron.trim(),
    timezone: d.timezone.trim(),
    state: d.start === 'paused' ? 'paused' : 'enabled',
    client_request_id: requestId,
  }
  const gate = changed(d.gate, t.default_gate)
  if (Object.keys(gate).length > 0) body.gate = gate
  const params = changed(ownParams(d.type, d.params), ownParams(d.type, schemaDefaults(t)))
  // A list left empty where the catalogue serves no default is not a
  // choice; the server's own default applies (an older API serves none).
  for (const [k, v] of Object.entries(params)) if (Array.isArray(v) && v.length === 0 && schemaDefaults(t)[k] === undefined) delete params[k]
  if (Object.keys(params).length > 0) body.params = params
  const budget = budgetOf(d)
  if (Object.keys(budget).length > 0) body.budget = budget
  // The Start choice owns dry run on a create: the form draws no second
  // control for it there, so the two can never disagree.
  const policy = changed({ ...d.policy, dry_run: d.start === 'dry_run' ? true : undefined }, {})
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

/** A budget field's own problem (in place) and the sheet's line for it. */
interface BudgetProblem {
  key: keyof Schedule['budget']
  field: string
  sheet: string
}

/**
 * THE BUDGET AGAINST THE TYPE'S SERVED CAPS (A3a), as `schedules.resolve_budget`
 * will judge it: each of per run and per day above zero and at most its cap,
 * live runs 1 to its cap, and a day at least one run. A blank field is the
 * stored value on an edit and the cap on a create, which is what the server
 * starts from. A type with no served caps is judged by the server alone.
 */
export function budgetProblems(d: Draft, t: ScheduleTypeRow | null, s: Schedule | null): BudgetProblem[] {
  const caps = t?.budget_caps ?? null
  const typed = budgetOf(d)
  const out: BudgetProblem[] = []
  const name = t?.name ?? d.type
  const money = (key: 'per_run_usd' | 'per_day_usd', unit: string, words: string) => {
    const v = typed[key]
    if (v === undefined) return
    const cap = caps?.[key]
    if (v <= 0) out.push({ key, field: 'Must be above $0.00.', sheet: `${words} must be above $0.00.` })
    else if (cap !== undefined && cap !== null && Math.round(v * 100) > Math.round(cap * 100)) {
      out.push({ key, field: `Over ${name}’s cap of ${usd(cap)} a ${unit}.`, sheet: `${words} ${usd(v)} is over ${name}’s cap of ${usd(cap)}.` })
    }
  }
  money('per_run_usd', 'run', 'Per run')
  money('per_day_usd', 'day', 'Per day')
  const run = typed.per_run_usd ?? s?.budget.per_run_usd ?? caps?.per_run_usd ?? undefined
  const day = typed.per_day_usd ?? s?.budget.per_day_usd ?? caps?.per_day_usd ?? undefined
  const dayMine = out.some((p) => p.key === 'per_day_usd')
  if (!dayMine && run !== undefined && day !== undefined && run > 0 && Math.round(day * 100) < Math.round(run * 100)) {
    out.push({ key: 'per_day_usd', field: `Under one run’s ${usd(run)}.`, sheet: `Per day ${usd(day)} is under one run’s ${usd(run)}.` })
  }
  const live = typed.max_concurrent
  const liveCap = caps?.max_concurrent
  if (live !== undefined && live < 1) out.push({ key: 'max_concurrent', field: 'At least 1.', sheet: 'Live runs must be at least 1.' })
  else if (live !== undefined && liveCap !== undefined && liveCap !== null && live > liveCap) {
    out.push({ key: 'max_concurrent', field: `Over ${name}’s cap of ${liveCap}.`, sheet: `Live runs ${live} is over ${name}’s cap of ${liveCap}.` })
  }
  return out
}

/** A changed value in words for the sheet's "Changes against revision N". */
function changeWords(key: string, v: unknown): string {
  if (v === undefined) return 'the type’s default'
  if ((key === 'per_run_usd' || key === 'per_day_usd') && typeof v === 'number') return usd(v)
  if (key === 'approvers') return v === 'owner_only' ? 'the owner only' : v === 'members' ? 'any member' : (plainValue(v) ?? paramText(v))
  return plainValue(v) ?? paramText(v)
}

const CHANGE_LABEL: Readonly<Record<string, string>> = {
  run: 'Run',
  plan: 'Plan',
  merge: 'Merge',
  approvers: 'Who may approve',
  approval_ttl_hours: 'Approval expires after (hours)',
  per_run_usd: 'Per run',
  per_day_usd: 'Per day',
  max_concurrent: 'Live runs at most',
  overlap: 'If the last firing is still running',
  catch_up: 'After an outage',
  dry_run: 'Dry run',
}

function scopeWords(scope: Schedule['scope']): string {
  if (scope.mode !== 'repos') return SCOPE_WORD[scope.mode]
  const n = scope.repo_ids?.length ?? 0
  return `${n} chosen ${n === 1 ? 'repository' : 'repositories'}`
}

/**
 * WHAT A SAVE WOULD CHANGE (A3b), one line a field, read off `editBody` so
 * the sheet lists exactly what the PATCH sends and nothing it does not.
 */
export function changeLines(d: Draft, s: Schedule, t: ScheduleTypeRow | null): { label: string; from: string; to: string }[] {
  const body = editBody(d, s)
  const out: { label: string; from: string; to: string }[] = []
  if (body.name !== undefined) out.push({ label: 'Name', from: s.name, to: body.name })
  if (body.scope !== undefined) out.push({ label: 'Scope', from: scopeWords(s.scope), to: scopeWords(body.scope) })
  if (body.cron !== undefined) out.push({ label: 'Cadence', from: s.cron, to: body.cron })
  if (body.timezone !== undefined) out.push({ label: 'Timezone', from: s.timezone, to: body.timezone })
  const group = (changes: Record<string, unknown> | undefined, stored: Record<string, unknown>, title: (k: string) => string) => {
    for (const [k, v] of Object.entries(changes ?? {})) out.push({ label: title(k), from: changeWords(k, stored[k]), to: changeWords(k, v) })
  }
  const known = (k: string) => CHANGE_LABEL[k] ?? k
  group(body.gate as Record<string, unknown> | undefined, s.gate as Record<string, unknown>, known)
  group(body.params, s.params, (k) => t?.params_schema.properties?.[k]?.title ?? k)
  group(body.budget as Record<string, unknown> | undefined, s.budget as Record<string, unknown>, known)
  group(body.policy as Record<string, unknown> | undefined, s.policy as Record<string, unknown>, known)
  return out
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "26 Oct 07:41Z": a served instant, in UTC as the mock-up writes it. */
function utcWords(iso: string | undefined | null): string | null {
  if (iso === undefined || iso === null) return null
  const d = new Date(iso)
  if (!Number.isFinite(d.getTime())) return null
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.toISOString().slice(11, 16)}Z`
}

/**
 * THE PAUSED LEAD (A3b). Saving an edit never resumes a schedule: resuming
 * is a separate step on its page, and the lead says so before the person
 * saves and waits for a firing that will not come. The word is the API's
 * `state_word` where it is served; the stored `state` otherwise. An admin's
 * `disabled` reads paused, the decided word (#2417), never "disabled".
 */
export function pausedLead(s: Schedule): { title: string; say: string } | null {
  if (s.state === 'enabled') return null
  const word = (s as Schedule & { state_word?: unknown }).state_word
  const tripwire = word === 'paused_by_tripwire' || (word === undefined && s.state === 'auto_paused' && s.pause?.code === 'RUN_OVER_BUDGET')
  const at = utcWords(s.pause?.at)
  const when = at === null ? '' : ` at ${at}`
  const said = tripwire ? `It was paused by its tripwire${when}.` : at === null ? 'It is paused.' : `It was paused${when}.`
  return { title: `Saving does not resume ${s.name}`, say: `${said} Save the change, then resume it from its page.` }
}

/** One numbered card of the form (B's Config E card): a titled surface. */
/** What a problem's jump focuses inside its box when the box is no control itself. */
const FOCUSABLE = 'input:not(:disabled), select:not(:disabled), textarea:not(:disabled), button:not(:disabled)'

/** A radio group's own radios, not those of a group nested inside it. */
function radiosOf(group: Element): HTMLButtonElement[] {
  return [...group.querySelectorAll<HTMLButtonElement>('[role="radio"]')].filter((r) => r.closest('[role="radiogroup"]') === group)
}

/**
 * ONE TAB STOP PER RADIO GROUP, AND THE ARROWS MOVE AND SELECT (QA1
 * automate-16), as the radio pattern requires. `Segmented` does this for
 * itself; the Type tiles and the Start choice (`ScheduleTemplates`) drew every
 * radio as a tab stop and ignored the arrows. The form holds the pattern for
 * every radio group inside it, by the same rule as `Segmented`: the checked
 * live radio is the stop, or the first live one when none is checked.
 */
function useRovingRadios(form: RefObject<HTMLFormElement | null>) {
  useLayoutEffect(() => {
    form.current?.querySelectorAll('[role="radiogroup"]').forEach((group) => {
      const radios = radiosOf(group)
      const live = radios.filter((r) => !r.disabled)
      const stop = live.find((r) => r.getAttribute('aria-checked') === 'true') ?? live[0]
      radios.forEach((r) => {
        r.tabIndex = r === stop ? 0 : -1
      })
    })
  })
}

/** The arrows on a radio: focus and select the next live one, wrapping. A group that moved already (`Segmented`) prevented the default. */
function roveRadios(e: KeyboardEvent<HTMLFormElement>) {
  if (e.defaultPrevented) return
  const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0
  if (step === 0 || !(e.target instanceof HTMLElement)) return
  const radio = e.target.closest<HTMLButtonElement>('[role="radio"]')
  const group = radio?.closest('[role="radiogroup"]') ?? null
  if (radio === null || group === null) return
  const live = radiosOf(group).filter((r) => !r.disabled)
  const at = live.indexOf(radio)
  if (at < 0) return
  const next = live[(at + step + live.length) % live.length]!
  e.preventDefault()
  next.focus()
  next.click()
}

function Card({ title, right, children }: { title: string; right?: ReactNode; children: ReactNode }) {
  const id = useId()
  return (
    <section className="ae-card" aria-labelledby={id}>
      <div className="ae-card-h">
        <h3 id={id}>{title}</h3>
        {right}
      </div>
      {children}
    </section>
  )
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
    setD((prev) => ({ ...draftFor(next, null, repoList), name: prev.name, cron: prev.cron, timezone: prev.timezone, repoIds: prev.repoIds, start: prev.start }))
  }

  const preview = usePreview(d.cron, d.timezone, d.type)
  const scopes = t === null ? [d.scopeMode] : scopesOf(t)
  const cadenceWhy = previewBlocks(preview)
  const budgetWhy = budgetProblems(d, t, schedule)
  const budgetErr = (key: keyof Schedule['budget']) => budgetWhy.find((p) => p.key === key)?.field ?? null
  // EVERY PROBLEM AT ONCE (A3a): the sheet lists them all, so a person fixes
  // the form in one pass instead of meeting each refusal after the last.
  // EACH PROBLEM GOES TO ITS FIELD (QA1 automate-24): on a phone the sheet
  // sits some 2,000px under the fields it names, so each line is a button
  // that scrolls to the field and focuses it. `at` is the field's
  // `data-jump`; null where the form draws no field for it.
  const paramsSay = paramsWhy(t, d)
  const problems = [
    d.name.trim() === '' ? { say: 'Name the schedule first.', at: 'name' } : null,
    d.type === '' ? { say: 'Pick a type first.', at: 'type' } : null,
    d.scopeMode === 'repos' && d.repoIds.length === 0 ? { say: 'Choose at least one repository.', at: 'repos' } : null,
    d.cron.trim() === '' ? { say: 'Say when it runs.', at: 'cron' } : cadenceWhy === null ? null : { say: cadenceProblem(preview.r), at: 'cron' },
    paramsSay === null ? null : { say: paramsSay.say, at: `param-${paramsSay.key}` },
    ...budgetWhy.map((p) => ({ say: p.sheet, at: `budget-${p.key}` })),
  ].filter((x): x is { say: string; at: string } => x !== null)
  const formRef = useRef<HTMLFormElement>(null)
  const jump = (at: string) => {
    const box = formRef.current?.querySelector<HTMLElement>(`[data-jump="${at}"]`) ?? null
    // A radio group's stop is its checked radio, so a jump lands there.
    const target =
      box === null || box.matches('input, select, textarea')
        ? box
        : (box.querySelector<HTMLElement>('[role="radio"][tabindex="0"]') ?? box.querySelector<HTMLElement>(FOCUSABLE))
    if (target === null) return
    target.scrollIntoView?.({ block: 'center' })
    target.focus()
  }
  useRovingRadios(formRef)
  const verb = schedule === null ? 'create' : 'save'
  const unchanged = schedule !== null && Object.keys(editBody(d, schedule)).length === 1
  const why =
    problems.length === 1
      ? `Fix the problem to ${verb}.`
      : problems.length > 1
        ? `Fix the ${problems.length} problems to ${verb}.`
        : unchanged
          ? 'Nothing has changed yet.'
          : undefined
  const changes = schedule === null ? [] : changeLines(d, schedule, t)
  const lead = schedule === null ? null : pausedLead(schedule)
  const cronRef = useRef<HTMLInputElement>(null)
  const preset = CRON_PRESETS.find((p) => p.cron === d.cron.trim())

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
  const params: [string, Prop][] =
    t !== null ? Object.entries(t.params_schema.properties ?? {}) : Object.keys(d.params).map((k) => [k, {}])
  // A parameter that restates the gate is drawn in the gates card, under the
  // control it follows; the type's own parameters are part of the target.
  const restatedOnes = params.filter(([k]) => restated(d.type, k) !== undefined)
  const ownOnes = params.filter(([k]) => restated(d.type, k) === undefined)
  // THE PREVIEW SITS UNDER THE CRON ON ONE COLUMN (visual QA V245). Beside
  // the cards it is the sheet's; on one column the sheet comes after every
  // card (B's phone frame), so the cron's words and next five are drawn in
  // the Cadence card instead, right under the field being typed in. One
  // element either way, so the preview is read once.
  const oneColumn = useOneColumn()
  const previewNode = <CronPreview r={preview.r} timezone={d.timezone} />
  const templateNode = <TemplateCard type={t} tier={schedule?.tier} />
  const sheet = (
    <aside className={oneColumn ? 'sc-edit-side ae-sheet is-inline' : 'sc-edit-side ae-sheet'} aria-label={schedule === null ? 'Before you create' : 'Before you save'} data-testid="ae-sheet">
      <div className="ae-sheet-h">
        <b>{schedule === null ? 'Before you create' : 'Before you save'}</b>
        <span className={problems.length > 0 ? 'ae-count is-bad' : 'ae-count'}>
          {problems.length === 0 ? 'no problems' : problems.length === 1 ? '1 problem' : `${problems.length} problems`}
        </span>
      </div>
      {problems.length > 0 && (
        <ul className="ae-probs" aria-label="Problems">
          {problems.map((p) => (
            <li key={p.say}>
              <button type="button" className="ae-prob-jump" onClick={() => jump(p.at)}>
                {p.say}
              </button>
            </li>
          ))}
        </ul>
      )}
      {schedule !== null && changes.length > 0 && (
        <div className="ae-diff">
          <b>Changes against revision {schedule.revision}</b>
          <dl>
            {changes.map((c) => (
              <div key={c.label}>
                <dt>{c.label}</dt>
                <dd>
                  {c.from} → {c.to}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}
      {!oneColumn && (
        <div className="ae-next">
          {previewNode}
          <span className="c-hint">From the server’s own parser, read as you type.</span>
        </div>
      )}
      {!oneColumn && templateNode}
      {refused !== null && (
        <p className="c-hint ae-refused" role="alert" data-code={refused.code ?? ''}>
          {refused.code === 'schedule_changed'
            ? 'Someone changed this schedule since you opened it. Close the editor to read it again, then edit.'
            : `Not saved: ${refused.message}`}
        </p>
      )}
      <div className="c-acts ae-acts">
        <Button kind="primary" busy={busy ? 'Saving…' : false} disabledReason={why} onClick={() => void (why === undefined && !busy && submit())}>
          {schedule === null ? 'Create schedule' : 'Save changes'}
        </Button>
        {cancel}
      </div>
    </aside>
  )
  return (
    // ENTER NEVER SUBMITS (AUT-2). Pressing Enter in Name created a live
    // schedule over every repository in one keystroke. The form has no submit
    // button, and the browser's implicit submission is swallowed here, so
    // only a click on Create or Save writes.
    <form ref={formRef} className="sc-form ae-form" data-testid="sc-form" onSubmit={(e) => e.preventDefault()} onKeyDown={roveRadios}>
      <div className="sc-edit-cols">
        <div className="sc-edit-main">
          {lead !== null && (
            <section className="ae-lead" data-testid="ae-lead" aria-label="Editing a paused schedule">
              <span className="ae-lead-k">Editing a paused schedule</span>
              <b>{lead.title}</b>
              <span>{lead.say}</span>
            </section>
          )}

          {schedule === null ? (
            <Card title="1 · Type and template">
              <div className="ae-jump" data-jump="type">
                <TemplateTiles types={types} value={d.type} onPick={pickType} />
              </div>
              {oneColumn && templateNode}
            </Card>
          ) : (
            <Card title="1 · Type">
              <Field label="Type" hint="The type is fixed once a schedule exists. Make a new schedule to change it.">
                <input className="c-inp" value={schedule.type} readOnly />
              </Field>
              {oneColumn && templateNode}
            </Card>
          )}

          <Card title="2 · Target">
            <div className="sc-facts">
              <Field label="Name">
                <input className="c-inp" value={d.name} maxLength={80} data-jump="name" onChange={(e) => set({ name: e.target.value })} />
              </Field>
              <Segmented<Scope>
                wide
                label="Scope"
                value={d.scopeMode}
                options={scopes.map((m) => ({ value: m as Scope, label: SCOPE_WORD[m as Scope] }))}
                onChange={(v) => set({ scopeMode: v })}
              />
            </div>
            {d.scopeMode === 'repos' && (
              <fieldset className="c-field" data-jump="repos">
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
                    <span className="ae-repo">
                      {r.owner}/{r.repo}
                    </span>
                  </label>
                ))}
              </fieldset>
            )}
            {ownOnes.length > 0 && (
              <>
                <h4 className="ae-sub">What it works on</h4>
                <div className="sc-facts">
                  {ownOnes.map(([k, p]) => (
                    // Keyed by the type too: a list field keeps its own text,
                    // and two types share a key (`paths`) with different defaults.
                    // `.ae-jump` draws no box: the field stays the grid's item.
                    <div key={`${d.type}:${k}`} className="ae-jump" data-jump={`param-${k}`}>
                    {editable(p) ? (
                      <ParamField name={k} schema={p} value={d.params[k]} onChange={(v) => set({ params: { ...d.params, [k]: v } })} />
                    ) : (
                      <Field label={p.title ?? k} hint={notEditableWhy(p)}>
                        {plainValue(d.params[k]) !== null || d.params[k] === undefined ? (
                          <input className="c-inp" value={d.params[k] === undefined ? '' : paramText(d.params[k])} readOnly data-testid={`sc-param-${k}`} />
                        ) : (
                          <textarea
                            className="c-inp sc-ptext"
                            value={paramText(d.params[k])}
                            rows={Math.min(12, paramText(d.params[k]).split('\n').length)}
                            readOnly
                            data-testid={`sc-param-${k}`}
                          />
                        )}
                      </Field>
                    )}
                    </div>
                  ))}
                </div>
              </>
            )}
          </Card>

          <Card title="3 · Cadence">
            <div className="sc-presets" role="group" aria-label="Presets">
              {CRON_PRESETS.map((p) => (
                <Button key={p.cron} size="sm" kind={d.cron.trim() === p.cron ? 'primary' : 'secondary'} aria-pressed={d.cron.trim() === p.cron} onClick={() => set({ cron: p.cron })}>
                  {p.label}
                </Button>
              ))}
              {/* Custom is the field itself: pressed when no preset matches,
                  and a click puts the cursor in the expression. */}
              <Button size="sm" kind={preset === undefined ? 'primary' : 'secondary'} aria-pressed={preset === undefined} onClick={() => cronRef.current?.focus()}>
                Custom
              </Button>
            </div>
            <div className="sc-facts">
              <Field label="Cron expression" hint="Minute, hour, day of month, month, day of week." error={d.cron.trim() === '' || oneColumn ? null : cadenceWhy}>
                <input
                  ref={cronRef}
                  data-jump="cron"
                  className="c-inp is-mono"
                  value={d.cron}
                  spellCheck={false}
                  onChange={(e) => set({ cron: e.target.value })}
                  {...(oneColumn && d.cron.trim() !== '' && cadenceWhy !== null ? { 'aria-invalid': 'true' } : {})}
                />
              </Field>
              <Field label="Timezone" hint="An IANA zone, such as Europe/London.">
                <input className="c-inp is-mono" value={d.timezone} spellCheck={false} onChange={(e) => set({ timezone: e.target.value })} />
              </Field>
            </div>
            {oneColumn && previewNode}
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
              {schedule !== null && (
                <Check
                  label="Dry run"
                  hint="Fires and records what it would do, and creates nothing."
                  checked={d.policy.dry_run === true}
                  onChange={(v) => set({ policy: { ...d.policy, dry_run: v } })}
                />
              )}
            </div>
          </Card>

          <Card title="4 · Gates and approvers">
            <div className="ae-g3">
              <Segmented label="Run" value={d.gate.run as 'auto' | 'approve' | undefined} options={gateOptions(t, 'run', RUN_PLAN)} onChange={(v) => set({ gate: { ...d.gate, run: v } })}
                hint={t?.default_gate.run === undefined ? undefined : `type default: ${t.default_gate.run}`} />
              <Segmented label="Plan" value={d.gate.plan as 'auto' | 'approve' | undefined} options={gateOptions(t, 'plan', RUN_PLAN)} onChange={(v) => set({ gate: { ...d.gate, plan: v } })}
                hint={t?.default_gate.plan === undefined ? undefined : `type default: ${t.default_gate.plan}`} />
              {d.gate.merge === 'auto' ? (
                <Field label="Merge" hint="Automatic, set by the audited switch on the schedule’s page; it is changed back there.">
                  <input className="c-inp" value="auto" readOnly />
                </Field>
              ) : (
                <Segmented<'off' | 'approve'>
                  label="Merge"
                  value={d.gate.merge as 'off' | 'approve' | undefined}
                  options={gateOptions(t, 'merge', [
                    { value: 'off', label: 'off' },
                    { value: 'approve', label: 'approve' },
                  ] as const)}
                  onChange={(v) => set({ gate: { ...d.gate, merge: v } })}
                  hint={t?.default_gate.merge === undefined ? undefined : `type default: ${t.default_gate.merge}`}
                />
              )}
            </div>
            <p className="c-hint">
              {/* The API's rule (`gate_below_default`), not the mock-up's caption:
                  `merge: off` is the strictest merge, so anyone may set it. */}
              A gate below the type’s default needs a platform admin. Merging automatically is the audited switch on the schedule’s page, never this form.
            </p>
            <div className="sc-facts">
              {typeof d.gate.approvers === 'string' || d.gate.approvers === undefined ? (
                <Segmented<'members' | 'owner_only'>
                  label="Who may approve"
                  value={d.gate.approvers as 'members' | 'owner_only' | undefined}
                  options={[
                    { value: 'members', label: 'any member' },
                    { value: 'owner_only', label: 'the owner only' },
                  ]}
                  onChange={(v) => set({ gate: { ...d.gate, approvers: v } })}
                />
              ) : (
                <Field label="Who may approve" hint="A named list, set through the API; unchanged here.">
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
            {restatedOnes.length > 0 && (
              <div className="sc-facts">
                {restatedOnes.map(([k, p]) => (
                  <Field key={k} label={p.title ?? k} hint="Follows the gate above; it is changed there, never here.">
                    <input className="c-inp" value={restated(d.type, k)!(d.gate)} readOnly data-testid={`sc-param-${k}`} />
                  </Field>
                ))}
              </div>
            )}
            <p className="c-hint" data-testid="ae-hard-stops">
              Hard stops stay on whatever is chosen here. <HardStops />
            </p>
          </Card>

          <Card title="5 · Budget and tripwire">
            <div className="ae-g3">
              <Field label="Per run (USD)" hint={budgetDefaultWords(t, 'per_run_usd')} error={budgetErr('per_run_usd')}>
                <input className="c-inp" type="number" min={0} step="0.01" placeholder={placeholderOf(t, 'per_run_usd')} data-jump="budget-per_run_usd" value={d.budget.per_run_usd} onChange={(e) => set({ budget: { ...d.budget, per_run_usd: e.target.value } })} />
              </Field>
              <Field label="Per day (USD)" hint={budgetDefaultWords(t, 'per_day_usd')} error={budgetErr('per_day_usd')}>
                <input className="c-inp" type="number" min={0} step="0.01" placeholder={placeholderOf(t, 'per_day_usd')} data-jump="budget-per_day_usd" value={d.budget.per_day_usd} onChange={(e) => set({ budget: { ...d.budget, per_day_usd: e.target.value } })} />
              </Field>
              <Field label="Live runs at most" hint={budgetDefaultWords(t, 'max_concurrent')} error={budgetErr('max_concurrent')}>
                <input className="c-inp" type="number" min={1} placeholder={placeholderOf(t, 'max_concurrent')} data-jump="budget-max_concurrent" value={d.budget.max_concurrent} onChange={(e) => set({ budget: { ...d.budget, max_concurrent: e.target.value } })} />
              </Field>
            </div>
            {/* §4.3 in words: what the per-run amount does is the one thing
                here a person cannot guess from the field's name. */}
            <p className="c-hint">
              A run that costs more than its per-run amount ends failed and <b>pauses the schedule at once</b>. A firing that would pass the day’s amount is
              skipped. An unreported cost counts at the per-run amount.
            </p>
          </Card>

          {schedule === null && (
            <Card title="6 · Start">
              <StartChoice value={d.start} onChange={(v) => set({ start: v })} />
            </Card>
          )}
        </div>
        {sheet}
      </div>
    </form>
  )
}

/** The width below which the side column folds under the fields: `.sc-edit-cols` in styles/schedules.css. */
const ONE_COLUMN = '(max-width: 1023px)'

function oneColumnNow(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function' && window.matchMedia(ONE_COLUMN).matches
}

function subscribeOneColumn(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(ONE_COLUMN)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}

/** Whether the form is one column, so the preview is drawn under the cron rather than beside it. */
function useOneColumn(): boolean {
  return useSyncExternalStore(subscribeOneColumn, oneColumnNow, () => false)
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
    <>
      <TenantRoom />
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
    </>
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
    // NOT A CARD (visual QA V170): `.ctl-card` has no padding of its own, so
    // the heading, the labels and the preview touched its border. The create
    // form has no card and reads well; the edit form is drawn the same way.
    <section className="sc-edit" aria-label="Edit this schedule">
      {/* A3b: the head names what is edited and the revision the save is
          checked against, so "Changes against revision N" has its N. */}
      <div className="ae-head">
        <h2 className="sc-h2">Edit {schedule.name}</h2>
        <span className="ae-rev">revision {schedule.revision}</span>
      </div>
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
