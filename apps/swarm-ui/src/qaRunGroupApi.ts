import { USE_FIXTURES, loadArtifactContent, loadWorkflow } from './api'
import { read, route, write, type Result } from './fetch'
import { normQaTarget, type QaTarget } from './qaTargetsApi'

/**
 * ONE QA DISPATCH'S LEGS, READ FOR THE RUN PAGE (lane QAENV-UI-RUNPAGE, part
 * of #2136; docs/design/qa-environments.md §7, §3.5, §5). Every figure comes
 * from a route that exists; nothing here is computed from a URL, a sign-in
 * or anything a caller could pick (invariant 10):
 *
 *   GET /v1/qa-runs/{group_id}               the group, in its own tenant (QAENV-RUN's `QaTargets.group`):
 *                                            each leg's name, state, workflow and that workflow's state
 *   GET /v1/workflows/{id}                   a leg's steps and their tasks
 *   GET /v1/tasks/{id}/artifacts/content     a step's own file, by the NAME its manifest lists:
 *                                            `capture-<g>/manifest.json` and `capture-<g>/found.json`
 *                                            (§3.5), the report's `findings.json`, the file step's `filed.json`
 *   GET /v1/qa-targets/{id}                  the target, whose per-leg `discovered` says which
 *                                            routes one leg has and the other lacks (§5), and
 *                                            whose per-leg `new_since_last_run` is what a run
 *                                            found that `screens` does not list yet, less the
 *                                            target's `dismissed_routes`
 *   POST /v1/qa-targets/{id}:dismiss-route   S9b's Remove, a workspace admin's: {route_id}, so no
 *                                            leg's later run offers that route again (QA-RUN-REMOVE)
 *   POST /v1/qa-targets/{id}:restore-route   the undo: {route_id}
 *
 * A FIGURE THAT WAS NOT READ IS NEVER A ZERO. Each is a value, or absent with
 * the sentence that says why (a step still running, a file the step did not
 * upload, a read that failed), and the page draws the second as a dash with
 * that sentence. A partial sum says how many of the steps it counts.
 *
 * Artifacts are read only for a step that has ended: a running step's files
 * are uploaded when its attempt ends, and asking earlier is a read that can
 * only fail.
 */

/** `swarm_api.qatargets._GROUP_ID`. */
export const QA_RUN_GROUP_ID = /^qrg_[0-9a-f]{16}$/

/** `swarm_api.qaspec.SEVERITIES`, most severe first. */
export const SEVERITIES = ['blocker', 'major', 'minor', 'nit'] as const
export type Severity = (typeof SEVERITIES)[number]

/** `LEG_PENDING`, `LEG_SUBMITTED`, `LEG_NOT_STARTED` (qatargets.py). */
export type LegState = 'pending' | 'submitted' | 'not_started'

export interface QaRunLeg {
  environment: string
  state: LegState
  workflow_id: string | null
  /** The workflow's derived state, read by the API in the same tenant; null when there is none. */
  workflow_state: string | null
  reason: { code: string | null; message: string } | null
}

export interface QaRunGroup {
  group_id: string
  target_id: string | null
  file: string | null
  by: string | null
  at: string | null
  legs: QaRunLeg[]
}

/** A figure: its value (with how much of it was read), or absent with why. */
export type Figure<T> =
  | { kind: 'value'; value: T; note: string | null }
  | { kind: 'absent'; why: string }

export interface LegStep {
  step_id: string
  state: string | null
}

export interface LegCoverage {
  /** New routes captured this run, with their paths (§3.5). */
  newPaths: string[]
  /** Found, but past the owning step's cap. */
  overCap: number
  /** Found by a step that did not own them, and not captured by the step that did. */
  notReached: number
  /** Each bound a crawl stopped at, other than `done`: a missing route is then not evidence it is gone. */
  stoppedBy: string[]
}

export interface LegDetail {
  steps: Figure<LegStep[]>
  screens: Figure<{ stored: number; new: number }>
  coverage: Figure<LegCoverage>
  findings: Figure<Record<Severity, number>>
  issues: Figure<number>
  refusals: Figure<number>
}

/** The routes one leg's `discovered` holds and another leg's does not (§5 "Between legs"). */
export interface OneLegOnly {
  on: string
  notOn: string[]
  paths: string[]
}

const TERMINAL = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED', 'DEAD_LETTERED'])

/** Whether a task or workflow state is an end state. */
export function ended(state: string | null): boolean {
  return state !== null && TERMINAL.has(state)
}

// -- copying the answers, field by field ---------------------------------------

function rec(v: unknown): Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function list(v: unknown): unknown[] {
  return Array.isArray(v) ? v : []
}

function count(v: unknown): number | null {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null
}

function legOf(raw: unknown): QaRunLeg | null {
  const l = rec(raw)
  const environment = str(l.environment)
  if (environment === null) return null
  const s = l.state
  const state: LegState = s === 'submitted' || s === 'not_started' ? s : 'pending'
  const why = rec(l.reason)
  const message = str(why.message)
  return {
    environment,
    state,
    workflow_id: str(l.workflow_id),
    workflow_state: str(l.workflow_state),
    reason: message === null ? null : { code: str(why.code), message },
  }
}

/** The route's `{qa_run}`, copied; null when it is not a group's shape. */
export function normQaRunGroup(raw: unknown): QaRunGroup | null {
  const g = rec(rec(raw).qa_run ?? raw)
  const id = str(g.group_id)
  if (id === null || !QA_RUN_GROUP_ID.test(id)) return null
  return {
    group_id: id,
    target_id: str(g.target_id),
    file: str(g.file),
    by: str(g.by),
    at: str(g.at),
    legs: list(g.legs).map(legOf).filter((l): l is QaRunLeg => l !== null),
  }
}

/**
 * WHY A LEG HAS NO WORKFLOW, in a sentence. A `not_started` leg carries the
 * refusal's own code and sentence. A leg still `pending` while another leg
 * started is §2.1's crash window between two submissions; while none has, the
 * dispatch is still submitting.
 */
export function notStartedWhy(leg: QaRunLeg, group: QaRunGroup): string {
  if (leg.reason !== null) return leg.reason.message
  const started = group.legs.filter((l) => l !== leg && l.workflow_id !== null).map((l) => l.environment)
  if (started.length > 0) {
    return `${started.join(' and ')} started, and this leg was never submitted: the dispatch stopped between the two submissions.`
  }
  return 'The dispatch has not submitted this leg yet.'
}

// -- the steps' own files, summed (§3.5) ------------------------------------------

/** One capture step's `manifest.json`, as much of it as the page counts. */
export interface CaptureManifest {
  routeIds: string[]
  refusals: number | null
}

export function manifestOf(raw: unknown): CaptureManifest | null {
  const m = rec(raw)
  if (!Array.isArray(m.routes)) return null
  const ids = new Set<string>()
  for (const r of m.routes) {
    const id = str(rec(r).id)
    if (id !== null) ids.add(id)
  }
  const gate = rec(m.gate_refusals)
  return { routeIds: [...ids], refusals: gate.read === false ? null : count(gate.count) }
}

/** One crawling capture step's `found.json` (§3.5). */
export interface FoundFile {
  capturedNew: string[]
  notOwned: string[]
  overCap: string[]
  stoppedBy: string | null
}

export function foundOf(raw: unknown): FoundFile | null {
  const f = rec(raw)
  if (!Array.isArray(f.captured_new)) return null
  const paths = (v: unknown) => list(v).map((r) => str(rec(r).path)).filter((p): p is string => p !== null)
  return {
    capturedNew: paths(f.captured_new),
    notOwned: paths(f.not_owned),
    overCap: paths(f.over_cap),
    stoppedBy: str(rec(f.crawl).stopped_by),
  }
}

/** Screens captured: stored routes and new ones, over every manifest read. */
export function screensOf(manifests: readonly CaptureManifest[], founds: readonly FoundFile[]): { stored: number; new: number } {
  const fresh = founds.reduce((n, f) => n + f.capturedNew.length, 0)
  const captured = manifests.reduce((n, m) => n + m.routeIds.length, 0)
  return { stored: Math.max(0, captured - fresh), new: fresh }
}

/** Coverage (§3.5) over every `found.json` read. */
export function coverageOf(founds: readonly FoundFile[]): LegCoverage {
  const captured = new Set(founds.flatMap((f) => f.capturedNew))
  const over = new Set(founds.flatMap((f) => f.overCap).filter((p) => !captured.has(p)))
  const notReached = new Set(founds.flatMap((f) => f.notOwned).filter((p) => !captured.has(p) && !over.has(p)))
  const stops = new Set(founds.map((f) => f.stoppedBy).filter((s): s is string => s !== null && s !== 'done'))
  return { newPaths: [...captured].sort(), overCap: over.size, notReached: notReached.size, stoppedBy: [...stops].sort() }
}

/** Findings by severity, from the report's `findings.json`; null when it is not that file. */
export function severitiesOf(raw: unknown): Record<Severity, number> | null {
  const doc = rec(raw)
  if (!Array.isArray(doc.findings)) return null
  const out = Object.fromEntries(SEVERITIES.map((s) => [s, 0])) as Record<Severity, number>
  for (const f of doc.findings) {
    const s = rec(f).severity
    if (typeof s === 'string' && (SEVERITIES as readonly string[]).includes(s)) out[s as Severity] += 1
  }
  return out
}

function issueNumber(v: unknown): number | null {
  if (typeof v === 'number' && Number.isInteger(v) && v > 0) return v
  if (typeof v !== 'string') return null
  const m = /(?:^#?|\/issues\/)(\d+)$/.exec(v.trim())
  return m === null ? null : Number(m[1])
}

/**
 * Issues filed or commented on, from the file step's `filed.json`: the
 * distinct issue numbers its rows name. The file step writes it from a
 * prompt (qaspec `_file_prompt`), so its keys are read leniently -- `issue`,
 * `issue_number`, `number` or an issue URL -- and a file none of whose rows
 * names one is not counted as zero but as unread (null).
 */
export function issuesOf(raw: unknown): number | null {
  const doc = rec(raw)
  const rows = Array.isArray(raw) ? raw : list(doc.filed ?? doc.findings ?? doc.issues)
  if (rows.length === 0) return Array.isArray(raw) || Array.isArray(doc.filed) ? 0 : null
  const seen = new Set<number>()
  let read = false
  for (const r of rows) {
    const row = rec(r)
    if ('filed' in row && row.filed === false) {
      read = true
      continue
    }
    const n = issueNumber(row.issue ?? row.issue_number ?? row.number ?? row.url)
    if (n !== null) {
      seen.add(n)
      read = true
    }
  }
  return read ? seen.size : null
}

/** The routes each leg alone holds in its `discovered`, from the target's answer (§5). */
export function oneLegOnlyOf(target: unknown, legs: readonly string[]): OneLegOnly[] {
  const t = rec(rec(target).qa_target ?? target)
  const held = new Map<string, Set<string>>()
  for (const e of list(t.environments)) {
    const entry = rec(e)
    const name = str(entry.name)
    if (name === null || !legs.includes(name)) continue
    const paths = new Set<string>()
    for (const r of list(entry.discovered)) {
      const route = rec(r)
      const path = str(route.path)
      // A route a complete crawl no longer saw is not "seen" on this leg.
      if (path !== null && str(route.missing_since) === null) paths.add(path)
    }
    held.set(name, paths)
  }
  if (held.size < 2) return []
  const out: OneLegOnly[] = []
  for (const name of legs) {
    const mine = held.get(name)
    if (mine === undefined) continue
    const others = legs.filter((l) => l !== name && held.has(l))
    const paths = [...mine].filter((p) => others.every((o) => !held.get(o)!.has(p))).sort()
    if (paths.length > 0) out.push({ on: name, notOn: others, paths })
  }
  return out
}

// -- the reads -------------------------------------------------------------------

export async function loadQaRunGroup(groupId: string): Promise<Result<QaRunGroup>> {
  if (USE_FIXTURES) return { status: 'ok', data: fixtureGroup(groupId), fetchedAt: Date.now() }
  const r = await read<unknown>(route('/v1/qa-runs/{group_id}', { group_id: groupId }), () => false)
  if (r.status !== 'ok' && r.status !== 'stale') return r
  const data = normQaRunGroup(r.data)
  if (data === null) {
    return { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The run group read did not answer a run group.' } }
  }
  return { ...r, data }
}

/**
 * A route a run found that the target does not store as a screen yet (§5):
 * its legs' `new_since_last_run`, one row per path. `on` names each leg it
 * was found on; `thisRun` says the route's last sighting (`workflow_id`) is
 * one of this group's own legs' workflows, so "first seen" can say "this run".
 */
export interface NewRoute {
  id: string
  path: string
  on: string[]
  firstSeen: string | null
  thisRun: boolean
}

/**
 * The routes this group's legs found and `screens` does not list, from the
 * target's answer: each leg's `new_since_last_run` (`discovery_status`, which
 * already leaves out a route `screens` lists), or the target's own on a
 * target served in the one-origin shape. `workflows` maps a leg to its
 * workflow, so a route last seen by this run says so.
 */
export function newRoutesOf(raw: unknown, legs: readonly string[], workflows: Readonly<Record<string, string>> = {}): NewRoute[] {
  const t = rec(rec(raw).qa_target ?? raw)
  const ours = new Set(Object.values(workflows))
  const byPath = new Map<string, NewRoute>()
  const take = (found: unknown, on: string) => {
    for (const r of list(found)) {
      const route = rec(r)
      const id = str(route.id)
      const path = str(route.path)
      if (id === null || path === null) continue
      const wf = str(route.workflow_id)
      const was = byPath.get(path)
      if (was === undefined) {
        byPath.set(path, { id, path, on: [on], firstSeen: str(route.first_seen), thisRun: wf !== null && ours.has(wf) })
        continue
      }
      if (!was.on.includes(on)) was.on.push(on)
      if (wf !== null && ours.has(wf)) was.thisRun = true
    }
  }
  const entries = list(t.environments).map(rec)
  if (entries.length === 0) take(t.new_since_last_run, legs.length === 1 ? legs[0]! : (str(t.host) ?? 'the target'))
  for (const entry of entries) {
    const name = str(entry.name)
    if (name !== null && legs.includes(name)) take(entry.new_since_last_run, name)
  }
  return [...byPath.values()].sort((a, b) => a.path.localeCompare(b.path))
}

/** A route a workspace admin dismissed (S9b's Remove): the target's `dismissed_routes`, which no leg offers again. */
export interface DismissedRoute {
  id: string
  path: string
  by: string | null
  at: string | null
}

/** The target's `dismissed_routes`, in the order they were dismissed; a row without an id or a path is not one. */
export function dismissedOf(raw: unknown): DismissedRoute[] {
  const t = rec(rec(raw).qa_target ?? raw)
  const out: DismissedRoute[] = []
  for (const d of list(t.dismissed_routes)) {
    const row = rec(d)
    const id = str(row.id)
    const path = str(row.path)
    if (id !== null && path !== null) out.push({ id, path, by: str(row.by), at: str(row.at) })
  }
  return out
}

/**
 * S9b's Remove: the found route `routeId` names leaves every leg's
 * `new_since_last_run`, in this run's answer and every later run's. A
 * workspace admin's; the API answers anyone else 403 and writes nothing.
 */
export function dismissQaRoute(targetId: string, routeId: string): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets/{target_id}:dismiss-route', { target_id: targetId }), 'POST', { route_id: routeId })
}

/** The undo of `dismissQaRoute`: the route is offered again where a leg's last run found it. */
export function restoreQaRoute(targetId: string, routeId: string): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets/{target_id}:restore-route', { target_id: targetId }), 'POST', { route_id: routeId })
}

/** What the run page reads off the target: its name, each leg's host, §5's one-leg-only routes, its new routes and its dismissed ones. */
export interface TargetFacts {
  name: string | null
  hosts: Record<string, string>
  oneLegOnly: OneLegOnly[]
  newRoutes: NewRoute[]
  dismissed: DismissedRoute[]
  /** The target as the QA targets page reads it, which Promote's PATCH is built from; null when it is not one. */
  target: QaTarget | null
}

/** The target's facts for these legs; read from the target's own answer. */
export function targetFactsOf(raw: unknown, legs: readonly string[], workflows: Readonly<Record<string, string>> = {}): TargetFacts {
  const t = rec(rec(raw).qa_target ?? raw)
  const hosts: Record<string, string> = {}
  for (const e of list(t.environments)) {
    const entry = rec(e)
    const name = str(entry.name)
    const host = str(entry.host)
    if (name !== null && host !== null) hosts[name] = host
  }
  return {
    name: str(t.name),
    hosts,
    oneLegOnly: oneLegOnlyOf(raw, legs),
    newRoutes: newRoutesOf(raw, legs, workflows),
    dismissed: dismissedOf(raw),
    target: normQaTarget(t),
  }
}

/** The target's facts, or why they were not read. */
export async function loadTargetFacts(
  targetId: string,
  legs: readonly string[],
  workflows: Readonly<Record<string, string>> = {},
): Promise<Figure<TargetFacts>> {
  if (USE_FIXTURES) return { kind: 'value', value: targetFactsOf(FIXTURE_TARGET, legs, workflows), note: null }
  const r = await read<unknown>(route('/v1/qa-targets/{target_id}', { target_id: targetId }), () => false)
  if (r.status === 'error') return { kind: 'absent', why: `The target was not read: ${r.error.message}` }
  if (r.status !== 'ok' && r.status !== 'stale') return { kind: 'absent', why: 'The target was not read.' }
  return { kind: 'value', value: targetFactsOf(r.data, legs, workflows), note: null }
}

async function artifactJson(taskId: string, name: string): Promise<{ ok: true; data: unknown } | { ok: false; why: string }> {
  const r = await loadArtifactContent(taskId, name)
  if (r.status === 'error') return { ok: false, why: r.error.message }
  if (r.status !== 'ok' && r.status !== 'stale') return { ok: false, why: `${name} was not read` }
  const c = r.data
  if (c.status !== 'ok' || c.content === null) return { ok: false, why: c.detail ?? `${name} was not uploaded` }
  if (c.truncated) return { ok: false, why: `${name} is larger than one read` }
  try {
    return { ok: true, data: JSON.parse(c.content) }
  } catch {
    return { ok: false, why: `${name} is not JSON` }
  }
}

const absent = <T,>(why: string): Figure<T> => ({ kind: 'absent', why })
const partial = (read: number, of: number, what: string): string | null => (read === of ? null : `${read} of ${of} ${what} read`)

/**
 * ONE LEG'S FIGURES: its workflow's steps, and from the steps that have ended,
 * their files. A leg with no workflow has none of these, said once by the page.
 */
export async function loadLegDetail(workflowId: string, environment: string): Promise<LegDetail> {
  if (USE_FIXTURES) return FIXTURE_DETAIL[environment] ?? FIXTURE_DETAIL.staging!
  const wf = await loadWorkflow(workflowId)
  if (wf.status !== 'ok' && wf.status !== 'stale') {
    const why = wf.status === 'error' ? `The workflow was not read: ${wf.error.message}` : 'The workflow was not read.'
    return { steps: absent(why), screens: absent(why), coverage: absent(why), findings: absent(why), issues: absent(why), refusals: absent(why) }
  }
  const tasks = list(rec(wf.data).tasks).map(rec)
  const declared = list(rec(rec(wf.data).workflow).steps).map((s) => str(rec(s).step_id)).filter((s): s is string => s !== null)
  const byStep = new Map<string, { id: string; state: string | null }>()
  for (const t of tasks) {
    const step = str(t.step_id)
    const id = str(t.id)
    if (step !== null && id !== null) byStep.set(step, { id, state: str(t.state) })
  }
  const stepIds = declared.length > 0 ? declared : [...byStep.keys()]
  const steps: LegStep[] = stepIds.map((s) => ({ step_id: s, state: byStep.get(s)?.state ?? null }))

  const captures = stepIds.filter((s) => s.startsWith('capture-'))
  const endedCaptures = captures.filter((s) => ended(byStep.get(s)?.state ?? null))
  const manifests: CaptureManifest[] = []
  const founds: FoundFile[] = []
  await Promise.all(endedCaptures.map(async (s) => {
    const id = byStep.get(s)!.id
    const [m, f] = await Promise.all([artifactJson(id, `${s}/manifest.json`), artifactJson(id, `${s}/found.json`)])
    const man = m.ok ? manifestOf(m.data) : null
    if (man !== null) manifests.push(man)
    const found = f.ok ? foundOf(f.data) : null
    if (found !== null) founds.push(found)
  }))

  const noCaptures = 'This leg has no capture step.'
  const waiting = 'When a capture step ends.'
  const screens: Figure<{ stored: number; new: number }> = captures.length === 0 ? absent(noCaptures)
    : endedCaptures.length === 0 ? absent(waiting)
    : manifests.length === 0 ? absent('No ended capture step uploaded a readable manifest.json.')
    : { kind: 'value', value: screensOf(manifests, founds), note: partial(manifests.length, captures.length, 'capture steps') }
  const coverage: Figure<LegCoverage> = captures.length === 0 ? absent(noCaptures)
    : endedCaptures.length === 0 ? absent(waiting)
    : founds.length === 0 ? absent('No ended capture step uploaded a readable found.json.')
    : { kind: 'value', value: coverageOf(founds), note: partial(founds.length, captures.length, 'crawls') }
  const counted = manifests.filter((m) => m.refusals !== null)
  const refusals: Figure<number> = captures.length === 0 ? absent(noCaptures)
    : endedCaptures.length === 0 ? absent(waiting)
    : counted.length === 0 ? absent("No ended capture step's gate refusals were read.")
    : { kind: 'value', value: counted.reduce((n, m) => n + (m.refusals ?? 0), 0), note: partial(counted.length, captures.length, 'capture steps') }

  const report = byStep.get('report')
  let findings: Figure<Record<Severity, number>>
  if (report === undefined) findings = absent('This leg has no report step.')
  else if (!ended(report.state)) findings = absent('When the report step ends.')
  else {
    const r = await artifactJson(report.id, 'findings.json')
    const v = r.ok ? severitiesOf(r.data) : null
    findings = v !== null ? { kind: 'value', value: v, note: null } : absent(r.ok ? 'findings.json is not a findings list.' : r.why)
  }

  const file = byStep.get('file')
  let issues: Figure<number>
  if (file === undefined) issues = { kind: 'value', value: 0, note: 'report only: this run files no issues' }
  else if (!ended(file.state)) issues = absent('When the file step ends.')
  else {
    const r = await artifactJson(file.id, 'filed.json')
    const v = r.ok ? issuesOf(r.data) : null
    issues = v !== null ? { kind: 'value', value: v, note: null } : absent(r.ok ? 'filed.json names no issue number.' : r.why)
  }

  return { steps: { kind: 'value', value: steps, note: null }, screens, coverage, findings, issues, refusals }
}

// -- the fixture build's one group (made-up data, never product data) --------------

const FIXTURE_GROUP: QaRunGroup = {
  group_id: 'qrg_' + '0'.repeat(16),
  target_id: 'qat_' + '0'.repeat(16),
  file: 'issues',
  by: 'riley@example.com',
  at: '2026-10-23T21:12:00Z',
  legs: [
    { environment: 'staging', state: 'submitted', workflow_id: 'wf_' + 'a'.repeat(20), workflow_state: 'SUCCEEDED', reason: null },
    { environment: 'production', state: 'submitted', workflow_id: 'wf_' + 'b'.repeat(20), workflow_state: 'RUNNING', reason: null },
  ],
}

/** The fixture group, and under FIXTURE_HALF_STARTED the dispatch that stopped between its two submissions (§2.1). */
export const FIXTURE_HALF_STARTED = 'qrg_' + '1'.repeat(16)

function fixtureGroup(groupId: string): QaRunGroup {
  if (groupId !== FIXTURE_HALF_STARTED) return { ...FIXTURE_GROUP, group_id: groupId }
  return {
    ...FIXTURE_GROUP,
    group_id: groupId,
    legs: [FIXTURE_GROUP.legs[0]!, { environment: 'production', state: 'pending', workflow_id: null, workflow_state: null, reason: null }],
  }
}

const fixtureFound = (id: string, path: string, workflow_id: string) => ({ id, path, workflow_id, first_seen: FIXTURE_GROUP.at })

const FIXTURE_TARGET = {
  qa_target: {
    id: FIXTURE_GROUP.target_id,
    name: 'Studio',
    screens: [{ id: 'home', path: '/' }, { id: 'settings', path: '/settings' }],
    environments: [
      {
        name: 'staging', host: 'staging.app.example.com',
        discovered: [{ path: '/beta/reports' }, { path: '/settings/labs' }, { path: '/settings' }],
        new_since_last_run: [
          fixtureFound('beta-reports', '/beta/reports', FIXTURE_GROUP.legs[0]!.workflow_id!),
          fixtureFound('settings-labs', '/settings/labs', FIXTURE_GROUP.legs[0]!.workflow_id!),
        ],
      },
      {
        name: 'production', host: 'app.example.com',
        discovered: [{ path: '/settings' }, { path: '/legal/imprint' }],
        new_since_last_run: [fixtureFound('legal-imprint', '/legal/imprint', FIXTURE_GROUP.legs[1]!.workflow_id!)],
      },
    ],
    dismissed_routes: [{ id: 'print-preview', path: '/print/preview', by: FIXTURE_GROUP.by, at: '2026-10-22T09:40:00Z' }],
  },
}

const fixtureSteps = (states: string[]): LegStep[] =>
  ['capture-g1', 'capture-g2', 'review-g1', 'review-g2', 'report', 'file'].map((s, i) => ({ step_id: s, state: states[i] ?? null }))

const FIXTURE_DETAIL: Record<string, LegDetail> = {
  staging: {
    steps: { kind: 'value', value: fixtureSteps(['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED']), note: null },
    screens: { kind: 'value', value: { stored: 18, new: 2 }, note: null },
    coverage: { kind: 'value', value: { newPaths: ['/beta/reports', '/settings/labs'], overCap: 1, notReached: 0, stoppedBy: [] }, note: null },
    findings: { kind: 'value', value: { blocker: 0, major: 1, minor: 2, nit: 0 }, note: null },
    issues: { kind: 'value', value: 3, note: null },
    refusals: { kind: 'value', value: 0, note: null },
  },
  production: {
    steps: { kind: 'value', value: fixtureSteps(['SUCCEEDED', 'RUNNING', 'RUNNING', 'READY', 'READY', 'READY']), note: null },
    screens: { kind: 'value', value: { stored: 11, new: 1 }, note: '1 of 2 capture steps read' },
    coverage: { kind: 'value', value: { newPaths: ['/legal/imprint'], overCap: 0, notReached: 0, stoppedBy: ['time'] }, note: '1 of 2 crawls read' },
    findings: { kind: 'absent', why: 'When the report step ends.' },
    issues: { kind: 'absent', why: 'When the file step ends.' },
    refusals: { kind: 'value', value: 0, note: '1 of 2 capture steps read' },
  },
}
