/**
 * ADMIN › RUNNER SIZES (SIZES-UI; part of #1689, docs/design/runner-sizes.md
 * §4.6, §4.9 and §7).
 *
 * WHY THIS SCREEN EXISTS. A class's CPU request, CPU limit and memory were a
 * frozen-contract change, a pull request and a release. The owner, 2026-10-20:
 * any platform admin changes them here, with a reason, and every version is
 * audited and restorable (Q4). The scheduler reads the current version once
 * per drain pass and pins it on the attempt, so a save applies to the NEXT
 * admission and never to a running agent.
 *
 * NOTHING THE CONTRACT OWNS IS COPIED HERE. The sizes, the defaults, the
 * frozen bounds, which profiles follow a resize and the list prices are all
 * served by `GET /v1/admin/runner-sizes`. The client checks a keystroke
 * against the SERVED bounds so a refusal shows at once, and the preview
 * (`POST …:preview`, debounced) is the authority: when its answer is for the
 * draft on screen, its refusals replace the client's.
 *
 * WHAT IS NOT EDITED. `disk_gib` is shown, never an input: it is a tmpfs
 * slice of memory and nothing measured asks to change it (owner Q5). `units`
 * is derived by the API and never sent. Cloud Run profiles do not follow a
 * resize -- Cloud Run fixes resources on the Job and bills its limit -- and
 * each card lists them as such (owner Q3).
 *
 * A MEMORY CUT BELOW THE 7-DAY MAX PEAK asks for the class name typed (owner
 * Q6; CLAUDE.md: anything destructive takes a typed confirmation). Memory
 * never bursts, so a peak above it is a kill.
 *
 * THE HINTS (§4.9) are guidance held here, not frozen bounds: they refuse
 * nothing. They are drawn only over 30 or more attempts, because a handful of
 * short runs looks like a fix. A share the API reports as null had no record
 * to feed it, and reads "no data" -- never 0%.
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react'

import { AdminLead, UnreadLead } from './AdminSettings'
import { loadRunnerSizes, loadRunnerSizeVersions, previewRunnerSizes, restoreRunnerSizes, saveRunnerSizes } from './api'
import { usePhoneTables } from './capacityPoll'
import { Banner, Button, Card, Dialog, ToneMark, TypedConfirm } from './components'
import type { ApiError, Result } from './fetch'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { poolLabel } from './types'
import type {
  RunnerRecentUse, RunnerSize, RunnerSizeChanges, RunnerSizeRefusal, RunnerSizesDoc, RunnerSizesPreview, RunnerSizeVersion,
} from './types'
import './styles/admin.css'

/** The preview waits this long after the last keystroke (§4.6: 400 ms). */
export const PREVIEW_DEBOUNCE_MS = 400

/** §4.9 "enough data first": below this many attempts the readings show without hints. */
export const HINT_MIN_ATTEMPTS = 30
/** Long runs at the limit this share of the time or more is "pinned". Today's 30% is three times over; short runs' 7% stays under. */
const PINNED_SHARE = 0.1
/** "Rarely at the limit": under 5% of samples. */
const RARELY_SHARE = 0.05
/** "Often above the request": 5% of samples or more. */
const OFTEN_ABOVE_SHARE = 0.05
/** §4.9: a memory max peak within about 15% of the class. */
const MEMORY_NEAR = 0.85
/** §4.9: a memory max peak under half the class. */
const MEMORY_SLACK = 0.5
/** §4.9: a mean under half the request, idle often. */
const OVER_REQUESTED = 0.5
const IDLE_OFTEN = 0.15

const ROLES = ['worker', 'display'] as const
type Role = (typeof ROLES)[number]
const SHARE_FIELDS = ['cpu_request', 'cpu_limit', 'memory_gib'] as const
type ShareField = (typeof SHARE_FIELDS)[number]

/** A draft: per class, per editable field key (`cpu_limit`, `live_split.worker.cpu_limit`), the text typed. */
type Draft = Record<string, Record<string, string>>

const FIELD_WORDS: Record<ShareField, string> = { cpu_request: 'CPU request', cpu_limit: 'CPU limit', memory_gib: 'memory (GiB)' }

/**
 * A FIELD KEY IN WORDS, AND ITS UNIT (QA2-FIX-20, admin2-05 and admin2-13).
 * History read `large memory_gib 16 → 32` and the Save line only "A size is
 * outside its bound.": a wire key in prose, and no field named. A key reads
 * as its words (`live_split.worker.memory_gib` is `worker memory`), and a
 * figure carries the unit its last part names.
 */
const KEY_WORDS: Record<string, string> = {
  cpu_request: 'CPU request', cpu_limit: 'CPU limit', memory_gib: 'memory', memory_per_cpu: 'memory per core', live_split: 'split',
}

export function fieldWords(field: string): string {
  const parts = field.split('.')
  const words = parts.filter((p, i) => !(p === 'live_split' && i < parts.length - 1)).map((p) => KEY_WORDS[p] ?? p.replace(/_/g, ' '))
  return words.join(' ')
}

export function fieldUnit(field: string): string {
  const last = field.split('.').pop() ?? field
  if (last === 'memory_gib') return 'GiB'
  if (last === 'cpu_request' || last === 'cpu_limit') return 'cores'
  if (last === 'memory_per_cpu') return 'GiB per core'
  return ''
}

/** A figure with its field's unit: `32 GiB`; a dash for no figure, never a 0. */
function withUnit(value: number | null | undefined, field: string): string {
  if (value === null || value === undefined) return '—'
  const unit = fieldUnit(field)
  return unit === '' ? fmt(value) : `${fmt(value)} ${unit}`
}

/** One History or banner line: `large memory 16 → 32 GiB`. */
export function changeLine(cls: string, field: string, from: number | null | undefined, to: number | null | undefined): string {
  const unit = fieldUnit(field)
  const a = from === null || from === undefined ? '—' : fmt(from)
  const c = to === null || to === undefined ? '—' : fmt(to)
  return `${cls} ${fieldWords(field)} ${a} → ${c}${unit === '' ? '' : ` ${unit}`}`
}

function keysOf(size: RunnerSize): string[] {
  const out: string[] = [...SHARE_FIELDS]
  if (size.live_split !== undefined && size.live_split !== null) {
    for (const role of ROLES) for (const f of SHARE_FIELDS) out.push(`live_split.${role}.${f}`)
  }
  return out
}

function valueAt(size: RunnerSize, key: string): number {
  const parts = key.split('.')
  if (parts.length === 3 && size.live_split) return size.live_split[parts[1] as Role][parts[2] as ShareField]
  return size[key as ShareField]
}

function draftOf(doc: RunnerSizesDoc): Draft {
  const out: Draft = {}
  for (const [name, size] of Object.entries(doc.classes)) {
    out[name] = Object.fromEntries(keysOf(size).map((k) => [k, String(valueAt(size, k))]))
  }
  return out
}

/** A typed number, or null for anything that is not one. */
function num(text: string | undefined): number | null {
  if (text === undefined) return null
  const t = text.trim()
  if (t === '') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

/** A figure as the API writes it: 4, 0.5, 6.25. */
function fmt(n: number): string {
  return String(Math.round(n * 10000) / 10000)
}

/**
 * The body's `classes`: only the fields the draft changed, a live share's
 * under `live_split.<role>`. A field that is not a number is left out here and
 * refused by `clientRefusals`, so Save stays shut.
 */
export function changesOf(doc: RunnerSizesDoc, draft: Draft): RunnerSizeChanges {
  const out: RunnerSizeChanges = {}
  for (const [name, size] of Object.entries(doc.classes)) {
    const typed = draft[name] ?? {}
    for (const key of keysOf(size)) {
      const n = num(typed[key])
      if (n === null || n === valueAt(size, key)) continue
      const cls = (out[name] ??= {})
      const parts = key.split('.')
      if (parts.length === 3) {
        const split = (cls.live_split ?? {}) as Record<string, Record<string, number>>
        const share = (split[parts[1]!] ??= {})
        share[parts[2]!] = n
        cls.live_split = split
      } else {
        cls[key] = n
      }
    }
  }
  return out
}

function refusal(cls: string, field: string, value: number | null, bound: string): RunnerSizeRefusal {
  return { class: cls, field, value, bound }
}

/**
 * The served bounds, checked as an admin types, in the bound's own words. The
 * same rules the frozen `check_size` applies; the preview remains the
 * authority, and replaces these when its answer is for this draft.
 */
export function clientRefusals(doc: RunnerSizesDoc, draft: Draft): RunnerSizeRefusal[] {
  const b = doc.bounds
  const out: RunnerSizeRefusal[] = []
  const typedOf = (name: string, key: string) => num(draft[name]?.[key])

  const checkCpu = (name: string, prefix: string, req: number | null, lim: number | null, minReq: number): boolean => {
    if (req === null) {
      out.push(refusal(name, `${prefix}cpu_request`, null, 'request is a number of cores'))
      if (lim === null) out.push(refusal(name, `${prefix}cpu_limit`, null, 'limit is a number of cores'))
      return false
    }
    if (req < minReq || req > b.cpu_request.max) out.push(refusal(name, `${prefix}cpu_request`, req, `request is ${fmt(minReq)} to ${fmt(b.cpu_request.max)} cores`))
    if (!Number.isInteger(req / b.cpu_request.step)) out.push(refusal(name, `${prefix}cpu_request`, req, `request is a multiple of ${fmt(b.cpu_request.step)}`))
    const ratio = b.cpu_burst_max_ratio
    if (lim === null) out.push(refusal(name, `${prefix}cpu_limit`, null, 'limit is a number of cores'))
    else if (lim < req) out.push(refusal(name, `${prefix}cpu_limit`, lim, `limit is at least the request: ${fmt(req)}`))
    else if (lim > ratio * req) out.push(refusal(name, `${prefix}cpu_limit`, lim, `limit is at most ${fmt(ratio)} × request: ${fmt(ratio * req)}`))
    return true
  }

  for (const [name, size] of Object.entries(doc.classes)) {
    const req = typedOf(name, 'cpu_request')
    const lim = typedOf(name, 'cpu_limit')
    const mem = typedOf(name, 'memory_gib')
    const cpuOk = checkCpu(name, '', req, lim, b.cpu_request.min)
    const memOk = mem !== null && Number.isInteger(mem)
    if (!memOk) out.push(refusal(name, 'memory_gib', mem, 'memory is a whole number of GiB'))
    else if (mem < b.memory_gib.min || mem > b.memory_gib.max) out.push(refusal(name, 'memory_gib', mem, `memory is ${fmt(b.memory_gib.min)} to ${fmt(b.memory_gib.max)} GiB`))
    if (cpuOk && memOk && req !== null && mem !== null && req > 0) {
      const per = mem / req
      if (per < b.memory_per_cpu.min || per > b.memory_per_cpu.max) {
        out.push(refusal(name, 'memory_per_cpu', per,
          `memory is ${fmt(b.memory_per_cpu.min)} to ${fmt(b.memory_per_cpu.max)} GiB per requested core: ${fmt(req * b.memory_per_cpu.min)} to ${fmt(req * b.memory_per_cpu.max)} GiB at ${fmt(req)} cores`))
      }
    }
    if (size.live_split) {
      const ls = b.live_split
      const mins: Record<Role, [number, number]> = { worker: [ls.worker_min_cpu, ls.worker_min_memory_gib], display: [ls.display_min_cpu, ls.display_min_memory_gib] }
      const shares: Partial<Record<Role, { req: number | null; mem: number | null }>> = {}
      for (const role of ROLES) {
        const p = `live_split.${role}.`
        const sReq = typedOf(name, `${p}cpu_request`)
        const sMem = typedOf(name, `${p}memory_gib`)
        checkCpu(name, p, sReq, typedOf(name, `${p}cpu_limit`), mins[role][0])
        if (sMem === null || !Number.isInteger(sMem)) out.push(refusal(name, `${p}memory_gib`, sMem, 'memory is a whole number of GiB'))
        else if (sMem < mins[role][1]) out.push(refusal(name, `${p}memory_gib`, sMem, `the ${role} needs at least ${fmt(mins[role][1])} GiB`))
        shares[role] = { req: sReq, mem: sMem }
      }
      const w = shares.worker!
      const d = shares.display!
      if (req !== null && w.req !== null && d.req !== null && w.req + d.req !== req) {
        out.push(refusal(name, 'live_split.cpu_request', w.req + d.req, `worker + display requests make the class's ${fmt(req)} cores`))
      }
      if (mem !== null && w.mem !== null && d.mem !== null && w.mem + d.mem !== mem) {
        out.push(refusal(name, 'live_split.memory_gib', w.mem + d.mem, `worker + display memory makes the class's ${fmt(mem)} GiB`))
      }
    }
  }

  // The ordering: a step may name a SMALLER class, so a `standard` larger than
  // `browser` would let a browser step grow by naming "smaller" standard.
  const names = Object.keys(doc.classes)
  for (let i = 0; i + 1 < names.length; i++) {
    const low = names[i]!
    const high = names[i + 1]!
    for (const [key, words] of [['cpu_request', 'CPU request'], ['memory_gib', 'memory']] as const) {
      const a = typedOf(low, key)
      const c = typedOf(high, key)
      if (a !== null && c !== null && a > c) {
        // admin2-13: the rule in the sentence, with the figures in their units.
        const bound = `${low}’s ${words} (${withUnit(a, key)}) must be no larger than ${high}’s (${withUnit(c, key)}).`
        out.push(refusal(low, key, a, bound), refusal(high, key, c, bound))
      }
    }
  }
  return out
}

/** A share as a percentage, or "no data" for a null one: never a confident 0%. */
export function pct(share: number | null | undefined): string {
  return typeof share === 'number' && Number.isFinite(share) ? `${Math.round(share * 100)}%` : 'no data'
}

function gib(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(1)} GiB` : 'no data'
}

function dollars(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `$${value.toFixed(4)}` : '—'
}

/**
 * §4.9's patterns, for a class read over enough attempts. Each says what the
 * reading means and the change it leads to; none refuses anything.
 */
export function hintsFor(size: RunnerSize, use: RunnerRecentUse, ratio: number): string[] {
  if (use.attempts < HINT_MIN_ATTEMPTS) return []
  const out: string[] = []
  const atLong = use.share_at_limit_long
  if (typeof atLong === 'number' && atLong >= PINNED_SHARE) {
    out.push(
      `Long runs sit at the CPU limit ${pct(atLong)} of the time. ` +
        (size.cpu_limit < ratio * size.cpu_request
          ? `Raise the limit toward ${fmt(ratio * size.cpu_request)}: Autopilot bills requests, so it costs nothing and units do not move.`
          : `The limit is already ${fmt(ratio)} × the request, so only a larger request helps; it costs money and may change units, and it is the owner's call (§7 Q2).`),
    )
  }
  const above = use.share_above_request
  const at = use.share_at_limit
  if (typeof above === 'number' && above >= OFTEN_ABOVE_SHARE && typeof at === 'number' && at < RARELY_SHARE) {
    out.push(`Above the request ${pct(above)} of the time and rarely at the limit: bursting is doing what it is for. No change.`)
  }
  const mean = use.cpu_mean_cores_median
  if (typeof mean === 'number' && mean < OVER_REQUESTED * size.cpu_request && typeof use.share_idle === 'number' && use.share_idle >= IDLE_OFTEN && typeof at === 'number' && at < RARELY_SHARE) {
    out.push(
      `The median attempt uses ${fmt(mean)} of ${fmt(size.cpu_request)} requested cores and is rarely at the limit: a candidate to lower the request. ` +
        'Lowering the guarantee makes contended runs slower than today, so it is the owner’s decision (§7 Q2).',
    )
  }
  const peak = use.memory_peak_gib_max
  if ((typeof peak === 'number' && peak >= MEMORY_NEAR * size.memory_gib) || use.oom_near_miss > 0) {
    out.push(`Memory peaked at ${gib(peak)} of ${fmt(size.memory_gib)} GiB${use.oom_near_miss > 0 ? `, with ${use.oom_near_miss} near-miss${use.oom_near_miss === 1 ? '' : 'es'}` : ''}: raise memory. Memory never bursts, so a peak above it is a kill.`)
  } else if (typeof peak === 'number' && peak < MEMORY_SLACK * size.memory_gib) {
    out.push(`Memory never passed ${gib(peak)} of ${fmt(size.memory_gib)} GiB: it may come down. A cut under the 7-day max asks for the class name typed.`)
  }
  return out
}

/** The classes whose drafted memory cuts below their 7-day max peak (owner Q6), from the recent use read. */
function cutsBelowPeak(doc: RunnerSizesDoc, changes: RunnerSizeChanges, recent: Record<string, RunnerRecentUse>): string[] {
  return Object.entries(changes)
    .filter(([name, c]) => {
      const mem = c.memory_gib
      const peak = recent[name]?.memory_peak_gib_max
      return typeof mem === 'number' && mem < doc.classes[name]!.memory_gib && typeof peak === 'number' && mem < peak
    })
    .map(([name]) => name)
}

function errorOf(r: Result<unknown>): ApiError | null {
  return r.status === 'error' ? r.error : null
}

/** The changes between two versions' tables, for the 409 banner: what moved under the admin. */
function movedBetween(before: RunnerSizesDoc, after: RunnerSizesDoc): string[] {
  const out: string[] = []
  for (const [name, size] of Object.entries(after.classes)) {
    const old = before.classes[name]
    if (old === undefined) continue
    for (const key of keysOf(size)) {
      const a = keysOf(old).includes(key) ? valueAt(old, key) : null
      const c = valueAt(size, key)
      if (a !== c) out.push(changeLine(name, key, a, c))
    }
  }
  return out
}

/**
 * WHAT THE POOLS ADMIT OF THE SAVED SIZES, as the page's own preview read
 * answered it, for the lead. Every preview's `admits_before` is the saved
 * version's (the body only moves `admits_after`), so any answer for the
 * version on screen will do -- the empty one the page posts on arrival, or
 * one for a draft typed since. `failed` is the last answer's error, when no
 * answer for this version landed.
 */
type Admits = { version: number; data: RunnerSizesPreview | null; failed: ApiError | null }

function admitsAfter(was: Admits | null, version: number, r: Result<RunnerSizesPreview>): Admits | null {
  const same = was !== null && was.version === version ? was : { version, data: null, failed: null }
  if (r.status === 'ok' || r.status === 'stale') return { version, data: r.data, failed: null }
  if (r.status === 'error') return same.data !== null ? same : { version, data: null, failed: r.error }
  return was
}

/**
 * A CLASS A PLATFORM-WIDE POOL ADMITS NONE OF: one agent of it weighs more
 * units than the pool's ceiling, the row the pools table marks `too small`
 * (`AdmitsCell`). A pool at limit 0 admits nothing of any class and is not
 * this: Pool limits leads with it.
 */
export function tooSmall(doc: RunnerSizesDoc, preview: RunnerSizesPreview): { pool: string; cls: string; units: number; ceiling: number } | null {
  for (const p of Array.isArray(preview.pools) ? preview.pools : []) {
    for (const cls of Object.keys(p.admits_before ?? {})) {
      const units = doc.classes[cls]?.units ?? preview.classes?.[cls]?.units_before
      if (typeof units === 'number' && p.hard_limit > 0 && units > p.hard_limit) return { pool: p.pool, cls, units, ceiling: p.hard_limit }
    }
  }
  return null
}

/**
 * THE POOL IN THE LEAD, IN WORDS (QA1-FIX-23, admin-shell-04). The lead read
 * `Pool resource:large admits no large agent` while Pool limits names the
 * same row `large`: a key in prose. A class's own pool is that class's pool,
 * `global` is the platform-wide one, and any other is `poolLabel`'s name.
 */
export function poolSubject(pool: string, classes: Record<string, unknown>): string {
  if (pool === 'global') return 'The platform-wide pool'
  const cls = pool.startsWith('resource:') ? pool.slice('resource:'.length) : null
  if (cls !== null && cls in classes) return `The ${cls} class’s pool`
  return `The ${poolLabel(pool)} pool`
}

/** The class card's memory field, which the lead's button lands on: memory is what moves a class's units most. */
function openClass(cls: string): void {
  const card = document.querySelector<HTMLElement>(`.rs-class[data-class="${cls}"]`)
  if (card === null) return
  if (typeof card.scrollIntoView === 'function') card.scrollIntoView({ block: 'start' })
  const label = Array.from(card.querySelectorAll('label')).find((l) => l.textContent === `${cls} ${FIELD_WORDS.memory_gib}`)
  const input = label === undefined ? card.querySelector('input') : document.getElementById(label.htmlFor)
  if (input instanceof HTMLElement) input.focus()
}

/**
 * RUNNER SIZES' LEAD (ADMIN-LEADS; proposal E S10c, S10h). What needs an
 * admin, in order: a stored version that could not be read (the platform is
 * on the code defaults), then a class a pool admits none of. Otherwise the
 * green lead. A dash when the sizes, or what the pools admit, could not be
 * read; nothing while either is still being read: a lead is never a guess.
 */
function SizesLead({ state, admits }: { state: Result<RunnerSizesDoc>; admits: Admits | null }) {
  if (state.status === 'error' || state.status === 'stale') return <UnreadLead what="the runner sizes" error={state.error} />
  if (state.status !== 'ok') return null
  const doc = state.data
  if (doc.malformed) {
    return (
      <AdminLead
        tone="warn"
        kicker="Needs a look"
        title={`Version ${doc.current_version} could not be read`}
        body="The platform runs at the code defaults shown here until a save writes a valid version."
      />
    )
  }
  const mine = admits !== null && admits.version === doc.current_version ? admits : null
  if (mine === null) return null
  if (mine.data === null) return mine.failed === null ? null : <UnreadLead what="what the pools admit" error={mine.failed} />
  const small = tooSmall(doc, mine.data)
  if (small !== null) {
    return (
      <AdminLead
        tone="warn"
        kicker="Needs a look"
        title={`${poolSubject(small.pool, doc.classes)} admits no ${small.cls} agent`}
        body={`A ${small.cls} agent takes ${small.units} units and the pool’s ceiling is ${small.ceiling}.`}
        action={
          <Button kind="primary" onClick={() => openClass(small.cls)}>
            {`Open ${small.cls}`}
          </Button>
        }
      />
    )
  }
  return <AdminLead tone="ok" kicker="Nothing waiting" title="Every class fits every pool’s ceiling" body="A change shows what the pools would admit before you save it." />
}

export function RunnerSizesScreen() {
  const sizes = useUrRead(loadRunnerSizes, 'runner-sizes')
  const versions = useUrRead(loadRunnerSizeVersions, 'runner-size-versions')
  const [admits, setAdmits] = useState<Admits | null>(null)
  return (
    <div className="ur-page rs-page">
      <PageHead title="Runner sizes">
        <UrRefresh reads={[sizes, versions]} ageOf={[sizes]} />
      </PageHead>
      <SizesLead state={sizes.state} admits={admits} />
      <p className="ur-sub">
        Each class’s CPU request, CPU limit and memory. A save is a new version: the next admission takes it, and a running agent keeps the size it was admitted with.
      </p>
      <UrRegion state={sizes.state} route="GET /v1/admin/runner-sizes" what="Runner sizes" plural onRetry={sizes.reload} lines={6}>
        {(doc) => (
          <RunnerSizesBody
            doc={doc}
            onPreview={(version, r) => setAdmits((was) => admitsAfter(was, version, r))}
            reload={() => {
              sizes.reload()
              versions.reload()
            }}
            history={
              <UrRegion
                state={versions.state}
                route="GET /v1/admin/runner-sizes/versions"
                what="Runner size versions"
                plural
                onRetry={versions.reload}
                empty={<p className="rs-none">No version has been saved: the platform runs at the code defaults.</p>}
              >
                {(rows) => <VersionList rows={rows} current={doc.current_version} doc={doc} reload={() => { sizes.reload(); versions.reload() }} />}
              </UrRegion>
            }
          />
        )}
      </UrRegion>
    </div>
  )
}

type Confirm = { classes: string[]; send: () => void } | null

function RunnerSizesBody({
  doc,
  onPreview,
  reload,
  history,
}: {
  doc: RunnerSizesDoc
  /** Every preview answer, with the version it was asked over: the lead reads what the pools admit from it. */
  onPreview: (version: number, r: Result<RunnerSizesPreview>) => void
  reload: () => void
  history: ReactNode
}) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(doc))
  const [reason, setReason] = useState('')
  const [preview, setPreview] = useState<{ key: string; result: Result<RunnerSizesPreview> } | null>(null)
  const [recent, setRecent] = useState<Record<string, RunnerRecentUse>>({})
  // Whether any preview has answered, and the last failure while none has: a
  // class with no recent use is then a dash with its reason, not `reading…`.
  const [recentRead, setRecentRead] = useState<{ read: boolean; failed: string | null }>({ read: false, failed: null })
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [saved, setSaved] = useState<number | null>(null)
  const [stale, setStale] = useState<{ base: RunnerSizesDoc; version: number | null } | null>(null)
  const [confirm, setConfirm] = useState<Confirm>(null)
  const reasonId = useId()

  // A new version on screen (a save, a restore, another admin's 409) starts the draft again from it.
  const shown = useRef(doc.current_version)
  useEffect(() => {
    if (shown.current === doc.current_version) return
    shown.current = doc.current_version
    setDraft(draftOf(doc))
  }, [doc])

  const changes = useMemo(() => changesOf(doc, draft), [doc, draft])
  const key = JSON.stringify(changes)

  useEffect(() => {
    let live = true
    const version = doc.current_version
    const timer = setTimeout(() => {
      void previewRunnerSizes(changes, version).then((result) => {
        if (!live) return
        onPreview(version, result)
        setPreview({ key, result })
        if (result.status === 'ok' || result.status === 'stale') {
          const use = result.data.recent_use
          if (use && typeof use === 'object') setRecent((was) => ({ ...was, ...use }))
          setRecentRead({ read: true, failed: null })
        } else if (result.status === 'error') {
          const message = result.error.message
          setRecentRead((was) => (was.read ? was : { read: false, failed: message }))
        }
      })
    }, PREVIEW_DEBOUNCE_MS)
    return () => {
      live = false
      clearTimeout(timer)
    }
    // `changes` is named by `key`.
  }, [key, doc.current_version])

  const fresh = preview !== null && preview.key === key && preview.result.status === 'ok' ? preview.result.data : null
  // The preview is the authority for the draft it was asked about; until it answers, the client's check stands.
  const refusals = fresh !== null ? (Array.isArray(fresh.refusals) ? fresh.refusals : []) : clientRefusals(doc, draft)
  const previewError = preview !== null && preview.key === key ? errorOf(preview.result) : null
  const changed = Object.keys(changes).length > 0
  const cuts = [...new Set([...(fresh?.below_recent_peak ?? []), ...cutsBelowPeak(doc, changes, recent)])].sort()

  const why = !changed
    ? 'Change a size first.'
    : refusals.length > 0
      ? `${refusals[0]!.class} ${fieldWords(refusals[0]!.field)} is outside its bound.`
      : reason.trim() === ''
        ? 'Say why: every version records a reason.'
        : undefined

  const answer = (r: Result<RunnerSizesDoc>, again: (confirmed: boolean) => void, base: RunnerSizesDoc): void => {
    setBusy(false)
    const e = errorOf(r)
    if (e === null) {
      setConfirm(null)
      setFailed(null)
      setStale(null)
      setReason('')
      setSaved(r.status === 'ok' ? r.data.current_version : null)
      reload()
      return
    }
    if (e.code === 'stale_base_version') {
      const d = e.detail as { current_version?: unknown } | undefined
      setConfirm(null)
      setStale({ base, version: typeof d?.current_version === 'number' ? d.current_version : null })
      reload()
      return
    }
    if (e.code === 'below_recent_peak') {
      const d = e.detail as { classes?: Record<string, unknown> } | undefined
      const names = d?.classes && typeof d.classes === 'object' ? Object.keys(d.classes).sort() : []
      setConfirm({ classes: names.length > 0 ? names : ['confirm'], send: () => again(true) })
      return
    }
    setFailed(e.message)
  }

  const send = (confirmed: boolean): void => {
    setBusy(true)
    setFailed(null)
    setSaved(null)
    const base = doc
    void saveRunnerSizes({ base_version: doc.current_version, reason: reason.trim(), classes: changes, confirm_below_recent_peak: confirmed }).then((r) =>
      answer(r, send, base),
    )
  }

  const save = (): void => {
    if (cuts.length > 0) setConfirm({ classes: cuts, send: () => send(true) })
    else send(false)
  }

  const set = (cls: string, field: string, value: string): void => {
    setSaved(null)
    setDraft((was) => ({ ...was, [cls]: { ...(was[cls] ?? {}), [field]: value } }))
  }

  return (
    <>
      {/* A STORED VERSION THAT COULD NOT BE READ IS THE PAGE'S LEAD
          (`SizesLead`), which says what this banner said. Drawn here as well
          it was the same sentence twice, one screen apart. */}
      {stale !== null && (
        <div className="rs-stale">
          <Banner tone="warn" title={`Another admin saved ${stale.version === null ? 'a newer version' : `version ${stale.version}`} while you edited`}>
            Your edit was not saved. The page now shows {stale.version === null ? 'the current version' : `version ${stale.version}`}
            {doc.current_version !== stale.base.current_version && movedBetween(stale.base, doc).length > 0
              ? `; what changed: ${movedBetween(stale.base, doc).join(', ')}.`
              : '.'}
          </Banner>
        </div>
      )}
      <p className="rs-version-line">
        {doc.in_effect_version === 0 ? 'In effect: the code defaults (version 0).' : `In effect: version ${doc.in_effect_version}.`}
        {doc.updated_by ? ` Last saved by ${doc.updated_by}${doc.updated_at ? ` at ${String(doc.updated_at).replace('T', ' ').slice(0, 16)} UTC` : ''}.` : ''}
      </p>
      <div className="rs-classes">
        {Object.entries(doc.classes).map(([name, size]) => (
          <ClassCard
            key={name}
            name={name}
            size={size}
            doc={doc}
            typed={draft[name] ?? {}}
            onType={(field, value) => set(name, field, value)}
            refusals={refusals.filter((r) => r.class === name)}
            preview={fresh?.classes?.[name] ?? null}
            changes={changes[name]}
            use={recent[name] ?? (recentRead.read ? { kind: 'none' } : recentRead.failed !== null ? { kind: 'failed', message: recentRead.failed } : { kind: 'reading' })}
          />
        ))}
      </div>
      {refusals.some((r) => !(r.class in doc.classes)) && (
        <ul className="rs-class-refusals">
          {refusals.filter((r) => !(r.class in doc.classes)).map((r, i) => (
            <li key={i} className="rs-refusal" role="alert">{r.bound}</li>
          ))}
        </ul>
      )}
      <Card title="What the pools admit" className="rs-pools-card">
        <PoolsTable pools={fresh?.pools ?? null} classes={fresh?.classes ?? null} />
        {previewError !== null && <p className="rs-note">The preview could not be read ({previewError.message}); the sizes are checked against the served bounds only, and the save is judged again by the API.</p>}
      </Card>
      <div className="rs-save">
        <div className="c-field">
          <label className="c-lbl" htmlFor={reasonId}>Reason</label>
          <textarea id={reasonId} className="c-inp" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
          <p className="c-hint">Every version records who, when and why.</p>
        </div>
        <Button kind="primary" busy={busy ? 'Saving…' : false} disabledReason={why} onClick={save}>
          Save
        </Button>
        {saved !== null && <p className="rs-saved" role="status">Saved version {saved}. The next admission takes it.</p>}
        {failed !== null && <p className="rs-error" role="alert">Not saved: {failed}</p>}
      </div>
      {confirm !== null && (
        <TypedConfirm
          title="Cut memory below what it used?"
          name={confirm.classes.join(' ')}
          verb="Save the cut"
          keep="Keep editing"
          busy={busy ? 'Saving…' : false}
          onConfirm={confirm.send}
          onClose={() => setConfirm(null)}
          error={failed === null ? undefined : `Not saved: ${failed}`}
        >
          <p>
            {confirm.classes
              .map((c) => `${c}: memory ${changes[c]?.memory_gib ?? '—'} GiB, under its 7-day max peak of ${gib(recent[c]?.memory_peak_gib_max)}`)
              .join('; ')}
            . Memory never bursts: an attempt that needs more is killed and goes back to its last checkpoint.
          </p>
        </TypedConfirm>
      )}
      <Card title="History" className="rs-history">
        {history}
      </Card>
    </>
  )
}

function ClassCard({
  name, size, doc, typed, onType, refusals, preview, changes, use,
}: {
  name: string
  size: RunnerSize
  doc: RunnerSizesDoc
  typed: Record<string, string>
  onType: (field: string, value: string) => void
  refusals: RunnerSizeRefusal[]
  preview: RunnerSizesPreview['classes'][string] | null
  changes: RunnerSizeChanges[string] | undefined
  use: RunnerRecentUse | NoRecentUse
}) {
  const b = doc.bounds
  const ratio = b.cpu_burst_max_ratio
  const followers = doc.followers?.[name] ?? { follows: [], fixed: [] }
  const atField = new Set(keysOf(size))
  const loose = refusals.filter((r) => !atField.has(r.field))
  // Only CPU limits changed: Autopilot bills requests, so the change is free.
  const limitsOnly =
    changes !== undefined &&
    Object.entries(changes).every(([k, v]) => k === 'cpu_limit' || (k === 'live_split' && Object.values(v as Record<string, Record<string, number>>).every((s) => Object.keys(s).every((f) => f === 'cpu_limit'))))
  const req = num(typed.cpu_request) ?? size.cpu_request
  const bounds: Record<ShareField, string> = {
    cpu_request: `${fmt(b.cpu_request.min)} to ${fmt(b.cpu_request.max)} cores, steps of ${fmt(b.cpu_request.step)}`,
    cpu_limit: `${fmt(req)} to ${fmt(ratio * req)} (up to ${fmt(ratio)} × request)`,
    memory_gib: `${fmt(b.memory_gib.min)} to ${fmt(b.memory_gib.max)} GiB; never bursts`,
  }
  return (
    <section className="rs-class" data-class={name} aria-labelledby={`rs-h-${name}`}>
      <h2 id={`rs-h-${name}`} className="rs-class-h">{name}</h2>
      <div className="rs-fields">
        {SHARE_FIELDS.map((f) => (
          <SizeField key={f} label={`${name} ${FIELD_WORDS[f]}`} value={typed[f] ?? ''} bound={bounds[f]} onChange={(v) => onType(f, v)} refusals={refusals.filter((r) => r.field === f)} />
        ))}
      </div>
      <p className="rs-disk">Workspace {fmt(size.disk_gib)} GiB, a slice of memory: shown, not edited.</p>
      {size.live_split && (
        <div className="rs-split">
          <h3 className="rs-split-h">Live browser split</h3>
          {ROLES.map((role) => {
            const sReq = num(typed[`live_split.${role}.cpu_request`]) ?? size.live_split![role].cpu_request
            const minCpu = role === 'worker' ? b.live_split.worker_min_cpu : b.live_split.display_min_cpu
            const minMem = role === 'worker' ? b.live_split.worker_min_memory_gib : b.live_split.display_min_memory_gib
            const sb: Record<ShareField, string> = {
              cpu_request: `at least ${fmt(minCpu)} core${minCpu === 1 ? '' : 's'}`,
              cpu_limit: `${fmt(sReq)} to ${fmt(ratio * sReq)}`,
              memory_gib: `at least ${fmt(minMem)} GiB`,
            }
            return (
              <div className="rs-fields" key={role}>
                {SHARE_FIELDS.map((f) => {
                  const k = `live_split.${role}.${f}`
                  return <SizeField key={k} label={`${name} live ${role} ${FIELD_WORDS[f]}`} value={typed[k] ?? ''} bound={sb[f]} onChange={(v) => onType(k, v)} refusals={refusals.filter((r) => r.field === k)} />
                })}
              </div>
            )
          })}
          <p className="rs-note">The two requests and memories add up to the class’s, so the live pod’s units and bill are the class’s.</p>
        </div>
      )}
      {loose.length > 0 && (
        <ul className="rs-class-refusals">
          {loose.map((r, i) => (
            <li key={i} className="rs-refusal" role="alert">{r.bound}</li>
          ))}
        </ul>
      )}
      <dl className="rs-facts">
        <dt>Units</dt>
        <dd className="rs-units">{preview ? moved(String(preview.units_before), typeof preview.units_after === 'number' ? String(preview.units_after) : '—') : String(size.units)}</dd>
        <dt>Per agent, an hour</dt>
        <dd className="rs-cost">
          {preview ? moved(dollars(preview.cost_hour_before), dollars(preview.cost_hour_after)) : '—'}
          {limitsOnly && <span className="rs-free"> · $0.00 more an hour: Autopilot bills requests</span>}
        </dd>
      </dl>
      <p className="rs-follows">
        {followers.follows.length > 0 ? `Follows a resize: ${followers.follows.join(', ')}.` : 'No GKE profile names this class today.'}
      </p>
      {followers.fixed.length > 0 && (
        <p className="rs-fixed">
          These Cloud Run profiles do not follow this size: {followers.fixed.join(', ')}. Cloud Run fixes resources on the Job and bills its limit, so these keep the code default.
        </p>
      )}
      <RecentUse name={name} use={use} size={size} ratio={ratio} />
    </section>
  )
}

/**
 * ONE VALUE UNTIL AN EDIT MOVES IT (QA1-FIX-23, admin-shell-16). The page
 * previews the saved version on arrival, so with nothing edited every card
 * read `Units 1 → 1`: a change arrow over no change. The arrow is drawn only
 * when the two differ.
 */
function moved(before: string, after: string): string {
  return before === after ? before : `${before} → ${after}`
}

function SizeField({ label, value, bound, onChange, refusals }: { label: string; value: string; bound: string; onChange: (v: string) => void; refusals: RunnerSizeRefusal[] }) {
  const id = useId()
  const hintId = useId()
  return (
    <div className="c-field rs-field">
      <label className="c-lbl" htmlFor={id}>{label}</label>
      <input
        id={id}
        className="c-inp is-mono"
        inputMode="decimal"
        value={value}
        aria-invalid={refusals.length > 0 || undefined}
        aria-describedby={hintId}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="rs-bound" id={hintId}>{bound}</span>
      {refusals.map((r, i) => (
        <p key={i} className="rs-refusal" role="alert">{r.bound}</p>
      ))}
    </div>
  )
}

const READINGS: readonly { key: keyof RunnerRecentUse; label: string }[] = [
  { key: 'share_idle', label: 'Idle (under 0.5 core)' },
  { key: 'share_above_request', label: 'Above the request' },
  { key: 'share_at_limit', label: 'At the limit' },
  { key: 'share_at_limit_long', label: 'At the limit, runs of 40 min or more' },
  { key: 'throttled_share', label: 'Throttled' },
]

/**
 * A CLASS'S RECENT USE WHILE IT IS NOT ON SCREEN (QA1-FIX-23, chrome-C24).
 * It arrives with the preview, which the live deployment answered in 6-30 s,
 * and the card said only `reading…` meanwhile -- and for good when the
 * preview failed or answered without the class. Reading says what it waits
 * for; a failed read and a class with no record are each a dash with its
 * reason (never 0 attempts).
 */
type NoRecentUse = { kind: 'reading' } | { kind: 'none' } | { kind: 'failed'; message: string }

function RecentUse({ name, use, size, ratio }: { name: string; use: RunnerRecentUse | NoRecentUse; size: RunnerSize; ratio: number }) {
  if ('kind' in use) {
    if (use.kind === 'reading') return <p className="rs-recent">Recent use: reading the last 7 days of attempts with the preview…</p>
    if (use.kind === 'failed') return <p className="rs-recent">Recent use: — could not be read ({use.message}).</p>
    return <p className="rs-recent">Recent use: — the preview returned no record for {name}.</p>
  }
  const hints = hintsFor(size, use, ratio)
  return (
    <div className="rs-use">
      <p className="rs-recent">
        Last 7 days, {use.attempts} attempt{use.attempts === 1 ? '' : 's'}: memory max peak {gib(use.memory_peak_gib_max)}, p95 {gib(use.memory_peak_gib_p95)}
        {use.oom_near_miss > 0 ? `, ${use.oom_near_miss} near-miss${use.oom_near_miss === 1 ? '' : 'es'}` : ''}.
      </p>
      <dl className="rs-readings">
        <div data-reading="cpu_mean_cores_median">
          <dt>Median mean CPU</dt>
          <dd>{typeof use.cpu_mean_cores_median === 'number' ? `${fmt(use.cpu_mean_cores_median)} of ${fmt(size.cpu_request)} cores` : 'no data'}</dd>
        </div>
        {READINGS.map((r) => (
          <div key={r.key} data-reading={r.key}>
            <dt>{r.label}</dt>
            <dd>{pct(use[r.key] as number | null)}</dd>
          </div>
        ))}
      </dl>
      {use.attempts < HINT_MIN_ATTEMPTS ? (
        <p className="rs-hints-few">Fewer than {HINT_MIN_ATTEMPTS} attempts in 7 days: the readings are shown without hints.</p>
      ) : (
        <ul className="rs-hints">
          {hints.length === 0 ? <li className="rs-hint">No pattern in these readings asks for a change.</li> : hints.map((h, i) => <li key={i} className="rs-hint">{h}</li>)}
        </ul>
      )}
    </div>
  )
}

/**
 * WHAT THE POOLS ADMIT, A RECORD PER POOL AND CLASS ON A PHONE (visual QA run
 * 5b, QA-019, part of #1680). Four columns at 390 overflowed the card: the
 * last head was cut and `20 → 20` wrapped. Below 560px it is the console's
 * stacked record (styles.css `.is-stacked`): the pool at lead rank, its three
 * fields keyed by `data-label`. The roles are written out because a table
 * drawn as blocks loses its implicit ones. Wider, it is the table it was, in a
 * wrapper that scrolls inside the card rather than cutting a head.
 */
/**
 * The units one agent of `cls` weighs after the save, or before it where the
 * body leaves the class refused; null where the preview does not say.
 */
function unitsAfter(classes: RunnerSizesPreview['classes'] | null, cls: string): number | null {
  const c = classes?.[cls]
  if (c === undefined) return null
  return c.units_after ?? c.units_before
}

/**
 * A CLASS ITS POOL ADMITS NONE OF IS MARKED (visual QA run 6, 6:admin-013,
 * part of #1680). `resource:large` read `0 → 0` against a 2-unit ceiling with
 * a 4-unit class and nothing else, so a save that left the class unable to
 * start a single agent was silent. The row carries the mark Pools gives the
 * same pool (Capacity.tsx `classifyPool`, `too small`, in the parked tone: no
 * wait clears it) and says so in words. A pool at limit 0 is not too small:
 * it admits nothing of any class, and Pools marks it `limit 0`.
 */
export function PoolsTable({
  pools,
  classes = null,
}: {
  pools: RunnerSizesPreview['pools'] | null
  /** The preview's per-class units; without it a class is judged by the figure the pool admits. */
  classes?: RunnerSizesPreview['classes'] | null
}) {
  const phone = usePhoneTables()
  if (pools === null) return <p className="rs-note">Reading what the platform-wide pools admit…</p>
  if (pools.length === 0) return <p className="rs-note">No platform-wide pool has a ceiling set, so a resize moves no ceiling.</p>
  // admin2-05: where every row is a class's own pool admitting that class, the
  // Class column only repeats the Pool column's name, so it is not drawn.
  const ownOnly = pools.every((p) => Object.keys(p.admits_before).every((cls) => p.pool === `resource:${cls}`))
  return (
    <div className={phone ? 'rs-pools-wrap is-stacked' : 'rs-pools-wrap'}>
      <table className="rs-pools" role="table" aria-label="What the pools admit">
        <thead role="rowgroup">
          <tr role="row">
            <th role="columnheader" scope="col">Pool</th>
            <th role="columnheader" scope="col">Ceiling (units)</th>
            {!ownOnly && <th role="columnheader" scope="col">Class</th>}
            <th role="columnheader" scope="col">Agents admitted, before → after</th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {pools.flatMap((p) =>
            Object.entries(p.admits_before).map(([cls, before]) => (
              <tr key={`${p.pool}/${cls}`} role="row" data-pool={p.pool} data-class={cls}>
                <th role="rowheader" scope="row">
                  <PoolName pool={p.pool} />
                </th>
                <td role="cell" data-label="Ceiling (units)">{p.hard_limit}</td>
                {!ownOnly && <td role="cell" data-label="Class">{cls}</td>}
                <td role="cell" data-label="Agents admitted, before → after">
                  <AdmitsCell pool={p} cls={cls} before={before} units={unitsAfter(classes, cls)} />
                </td>
              </tr>
            )),
          )}
        </tbody>
      </table>
    </div>
  )
}

/**
 * THE POOL BY ITS NAME, THE KEY UNDER IT (QA2-FIX-20, admin2-05). The Pool
 * column read `resource:standard`; Pool limits names the same row `standard`
 * with the raw key as a mono line under it, the spelling a script takes.
 */
function PoolName({ pool }: { pool: string }) {
  const name = poolLabel(pool)
  if (name === pool) return <>{pool}</>
  return (
    <>
      {name}
      <span className="rs-pool-key">{pool}</span>
    </>
  )
}

function AdmitsCell({
  pool,
  cls,
  before,
  units,
}: {
  pool: RunnerSizesPreview['pools'][number]
  cls: string
  before: number
  units: number | null
}) {
  const after = pool.admits_after[cls]
  // ONE FIGURE UNTIL A SAVE WOULD MOVE IT (admin2-12), as the cards' `moved`.
  const figures = moved(String(before), after === undefined ? '—' : String(after))
  // By the units where the preview names them, else by the figure it admits.
  const small = pool.hard_limit > 0 && (units !== null ? units > pool.hard_limit : after === 0)
  if (!small) return <>{figures}</>
  const weighs = units === null ? 'more units than this pool’s ceiling' : `${units} units, more than this pool’s ${pool.hard_limit}`
  // ONE ELEMENT, so the stacked record keeps every part in its value column.
  return (
    <span className="rs-admits">
      {figures}{' '}
      <ToneMark tone="is-paused" title={`This pool's ceiling is ${pool.hard_limit} units, below what one ${cls} agent weighs, so it admits no ${cls} agent. Raise the ceiling or shrink the class.`}>
        too small
      </ToneMark>
      <span className="rs-admits-none">{`One ${cls} agent weighs ${weighs}: none of it can start.`}</span>
    </span>
  )
}

function VersionList({ rows, current, doc, reload }: { rows: RunnerSizeVersion[]; current: number; doc: RunnerSizesDoc; reload: () => void }) {
  const [restoring, setRestoring] = useState<number | null>(null)
  return (
    <>
      <ol className="rs-versions">
        {rows.map((v) => (
          <li key={v.version} className="rs-version" data-version={v.version}>
            <div className="rs-version-head">
              <b>Version {v.version}</b>
              {v.version === current && <span className="rs-current"> · current</span>}
              {v.created_at && <span> · <time dateTime={v.created_at}>{v.created_at.replace('T', ' ').slice(0, 16)} UTC</time></span>}
              {v.created_by && <span> · {v.created_by}</span>}
              {v.restored_from !== null && v.restored_from !== undefined && <span> · restored from version {v.restored_from}</span>}
            </div>
            {v.reason && <p className="rs-version-why">{v.reason}</p>}
            {Array.isArray(v.changed) && v.changed.length > 0 && (
              <ul className="rs-version-changes">
                {v.changed.map((c, i) => (
                  <li key={i}>{changeLine(c.class, c.field, c.from, c.to)}</li>
                ))}
              </ul>
            )}
            {v.version !== current && (
              <Button size="sm" onClick={() => setRestoring(v.version)}>
                Restore version {v.version}
              </Button>
            )}
          </li>
        ))}
      </ol>
      {/* ITS OWN BOX, SO IT KEEPS THE BUTTON'S WIDTH (QA2-FIX-20, admin2-10): a
          direct child of the card's flex column was stretched across the card,
          the older option drawn as the page's main action. */}
      {current > 0 && (
        <div className="rs-restore-defaults">
          <Button size="sm" onClick={() => setRestoring(0)}>
            Restore the code defaults
          </Button>
        </div>
      )}
      {restoring !== null && <RestoreDialog version={restoring} doc={doc} onClose={() => setRestoring(null)} reload={reload} />}
    </>
  )
}

function RestoreDialog({ version, doc, onClose, reload }: { version: number; doc: RunnerSizesDoc; onClose: () => void; reload: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [cut, setCut] = useState<string[] | null>(null)
  const id = useId()
  const what = version === 0 ? 'the code defaults' : `version ${version}`

  const send = (confirmed: boolean): void => {
    setBusy(true)
    setFailed(null)
    void restoreRunnerSizes({ base_version: doc.current_version, version, reason: reason.trim(), confirm_below_recent_peak: confirmed }).then((r) => {
      setBusy(false)
      const e = errorOf(r)
      if (e === null) {
        onClose()
        reload()
        return
      }
      if (e.code === 'below_recent_peak') {
        const d = e.detail as { classes?: Record<string, unknown> } | undefined
        const names = d?.classes && typeof d.classes === 'object' ? Object.keys(d.classes).sort() : []
        setCut(names.length > 0 ? names : ['confirm'])
        return
      }
      if (e.code === 'stale_base_version') {
        setFailed('Another admin saved a version since this page was read. It has been read again; look at History before restoring.')
        reload()
        return
      }
      setFailed(e.message)
    })
  }

  if (cut !== null) {
    return (
      <TypedConfirm
        title={`Restore ${what}, cutting memory?`}
        name={cut.join(' ')}
        verb="Restore and cut"
        keep="Keep the current sizes"
        busy={busy ? 'Restoring…' : false}
        onConfirm={() => send(true)}
        onClose={onClose}
        error={failed === null ? undefined : `Not restored: ${failed}`}
      >
        <p>Restoring {what} cuts memory for {cut.join(', ')} below the class’s 7-day max peak. Memory never bursts: an attempt that needs more is killed.</p>
      </TypedConfirm>
    )
  }
  return (
    <Dialog
      title={`Restore ${what}?`}
      onClose={onClose}
      actions={
        <>
          <Button kind="ghost" onClick={onClose}>
            Keep the current sizes
          </Button>
          <Button kind="primary" busy={busy ? 'Restoring…' : false} disabled={reason.trim() === ''} onClick={() => send(false)}>
            Restore
          </Button>
        </>
      }
    >
      <p>A restore writes a NEW version with {what}’s sizes; History keeps every version, and the next admission takes it.</p>
      <div className="c-field">
        <label className="c-lbl" htmlFor={id}>Reason for the restore</label>
        <textarea id={id} className="c-inp" rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      </div>
      {failed !== null && <p className="c-hint" role="alert">Not restored: {failed}</p>}
    </Dialog>
  )
}
