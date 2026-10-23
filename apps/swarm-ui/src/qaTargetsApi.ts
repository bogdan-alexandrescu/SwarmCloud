import { USE_FIXTURES } from './api'
import { read, route, write, type Result } from './fetch'

/**
 * QA TARGETS' ROUTES (lane VQ-6 CONSOLE-PLUGIN, part of #1933;
 * docs/design/visual-qa-any-app.md §1, §2.1, §6). VQ-2's routes
 * (`swarm_api/routes/qatargets.py`), used exactly as they are:
 *
 *   GET    /v1/qa-targets                        the workspace's targets (any role)
 *   POST   /v1/qa-targets                        register (a workspace admin)
 *   PATCH  /v1/qa-targets/{id}                   change (a workspace admin); a new host is pending
 *   DELETE /v1/qa-targets/{id}                   delete, with its credential (a workspace admin)
 *   POST   /v1/qa-targets/{id}:run               a member starts a run: {screens?, file}
 *   POST   /v1/qa-targets/{id}:discover          a member starts its discovery (§4.1, lane QA-DISCOVER)
 *   POST   /v1/qa-targets/{id}:approve-host      a PLATFORM admin approves the host: {host, extra_origin?} (VQ-Q1, audited)
 *   POST   /v1/qa-targets/{id}/credential-links  a one-time link to the credential form (an admin)
 *   PUT    /v1/qa-targets/{id}/credential        the form's write, with the link's token
 *
 * A TARGET IS COPIED FIELD BY FIELD (`normQaTarget`), its credential as
 * status only -- set, when, by whom, which version -- so a reply that
 * (wrongly) carried a value could not reach the page. The credential itself
 * is taken by `putQaCredential` alone and held by nothing here.
 */

/** `swarm_api.qatargets._TARGET_ID`. */
export const QA_TARGET_ID = /^qat_[0-9a-f]{16}$/

/** The credential link's path, as `QaTargets.mint_link` spells it: `/qa-targets/<id>/credential?t=<token>`. */
const LINK_PATH = /^\/qa-targets\/(qat_[0-9a-f]{16})\/credential\/?$/

export type HostState = 'pending' | 'approved' | 'revoked'

/**
 * The registration's other §1 fields (`swarm_api.qatargets.SPEC_FIELDS` less
 * the ones the form edits one by one). The console edits them as one JSON
 * object, and VQ-2 checks each; nothing outside this list is sent.
 */
export const MORE_FIELDS = ['discover', 'flows', 'widths', 'themes', 'exclusions', 'read_posts', 'design_docs', 'file', 'rate', 'clock'] as const
export type MoreField = (typeof MORE_FIELDS)[number]

export interface QaScreen {
  id: string
  path: string
}

/** A click the gate makes on the login page before the form (§2.2): the
 *  control's exact visible text, and optionally a CSS selector narrowing which
 *  elements are candidates. Up to three, in order. */
export interface QaStartClick {
  text: string
  css?: string
}

export interface QaSignIn {
  kind: 'none' | 'own_login'
  login_path: string | null
  signed_in_selector: string | null
  username_selector: string | null
  password_selector: string | null
  submit_selector: string | null
  extra_origin: string | null
  start_clicks: QaStartClick[]
}

export interface QaCredentialStatus {
  set: boolean
  set_at: string | null
  set_by: string | null
  version: string | null
}

/** The discovery running now (`discovering`): its workflow, once submitted. */
export interface QaDiscovering {
  workflow_id: string | null
  at: string | null
}

export interface QaTarget {
  id: string
  name: string
  base_url: string | null
  host: string | null
  host_state: HostState | null
  approved_by: string | null
  approved_at: string | null
  repo_id: string | null
  /** The registration's repository URL, `https://github.com/<owner>/<repo>`. */
  repository: string | null
  sign_in: QaSignIn
  credential: QaCredentialStatus
  screens: QaScreen[]
  environment: 'production' | 'non_production'
  authorized_by: string | null
  revision: number | null
  /** The `MORE_FIELDS` the target holds, as served. */
  more: Partial<Record<MoreField, unknown>>
  /** It asks to discover its screens (`discover` is set). */
  discovers: boolean
  /** What its last successful discovery stored, which a run starts from when `screens` is empty. */
  discovered: QaScreen[]
  discovered_at: string | null
  discovered_workflow: string | null
  /** The last discovery's failure, in the API's own sentence; null after a success. */
  discovery_error: string | null
  discovering: QaDiscovering | null
}

/** What a registration or a change sends: `TargetBody`'s fields the form edits. */
export interface QaTargetBody {
  name?: string
  base_url?: string
  repository?: string
  authorized_by?: string
  sign_in?: Record<string, unknown>
  screens?: QaScreen[]
  environment?: 'production' | 'non_production'
}

/** A body: the form's fields, and any `MORE_FIELDS` (null clears one on a change). */
export type QaTargetSend = QaTargetBody & Partial<Record<MoreField, unknown>>

export interface QaRun {
  workflow_id: string | null
}

export interface QaLink {
  targetId: string
  /** Null when the link's path came without its `t`. */
  token: string | null
}

const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const rec = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {})

function normSignIn(raw: unknown): QaSignIn {
  const s = rec(raw)
  return {
    kind: s.kind === 'own_login' ? 'own_login' : 'none',
    login_path: str(s.login_path),
    signed_in_selector: str(s.signed_in_selector),
    username_selector: str(s.username_selector),
    password_selector: str(s.password_selector),
    submit_selector: str(s.submit_selector),
    extra_origin: str(s.extra_origin),
    start_clicks: normStartClicks(s.start_clicks),
  }
}

function normStartClicks(raw: unknown): QaStartClick[] {
  if (!Array.isArray(raw)) return []
  const out: QaStartClick[] = []
  for (const item of raw) {
    const c = rec(item)
    const text = str(c.text)
    if (text === null) continue
    const css = str(c.css)
    out.push(css === null ? { text } : { text, css })
  }
  return out
}

/** `credential_status`: never a value, a length or any part of one. */
function normCredential(raw: unknown): QaCredentialStatus {
  const c = rec(raw)
  const version = c.version
  return {
    set: c.set === true,
    set_at: str(c.set_at),
    set_by: str(c.set_by),
    version: typeof version === 'number' ? String(version) : str(version),
  }
}

function normScreens(raw: unknown): QaScreen[] {
  return Array.isArray(raw)
    ? raw.flatMap((s) => {
        const r = rec(s)
        const sid = str(r.id)
        const path = str(r.path)
        return sid !== null && path !== null ? [{ id: sid, path }] : []
      })
    : []
}

function normDiscovering(raw: unknown): QaDiscovering | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const d = rec(raw)
  return { workflow_id: str(d.workflow_id), at: str(d.at) }
}

export function normQaTarget(raw: unknown): QaTarget | null {
  const t = rec(raw)
  const id = str(t.id)
  if (id === null) return null
  const state = t.host_state
  const screens = normScreens(t.screens)
  return {
    id,
    name: str(t.name) ?? id,
    base_url: str(t.base_url),
    host: str(t.host),
    host_state: state === 'pending' || state === 'approved' || state === 'revoked' ? state : null,
    approved_by: str(t.approved_by),
    approved_at: str(t.approved_at),
    repo_id: str(t.repo_id),
    repository: str(t.repository),
    sign_in: normSignIn(t.sign_in),
    credential: normCredential(t.credential),
    screens,
    environment: t.environment === 'non_production' ? 'non_production' : 'production',
    authorized_by: str(t.authorized_by),
    revision: typeof t.revision === 'number' ? t.revision : null,
    more: Object.fromEntries(MORE_FIELDS.flatMap((k) => (t[k] === undefined || t[k] === null ? [] : [[k, t[k]]]))),
    discovers: t.discover !== undefined && t.discover !== null,
    discovered: normScreens(t.discovered),
    discovered_at: str(t.discovered_at),
    discovered_workflow: str(t.discovered_workflow),
    discovery_error: str(t.discovery_error),
    discovering: normDiscovering(t.discovering),
  }
}

function normList(raw: unknown): QaTarget[] {
  const list = rec(raw).qa_targets
  return Array.isArray(list) ? list.flatMap((t) => normQaTarget(t) ?? []) : []
}

/** `owner/repo` of a registration's repository URL, or null. */
export function repoSlug(url: string | null): string | null {
  if (url === null) return null
  const m = /^https:\/\/github\.com\/([^/]+\/[^/]+?)(?:\.git)?\/?$/i.exec(url.trim())
  return m === null ? null : m[1]!
}

/**
 * Why a run of this target would be refused now, in the API's terms, or
 * null. The API is still the gate (`QaTargets.runnable`): this is said
 * beside a disabled Run, before anything is asked.
 */
export function cannotRunBecause(t: QaTarget): string | null {
  const browse = cannotBrowseBecause(t)
  if (browse !== null) return browse
  if (t.screens.length === 0 && t.discovered.length === 0) {
    return t.discovers
      ? 'No screens are discovered yet; press Run discovery, and Run is enabled once it has found them.'
      : 'It lists no screens.'
  }
  return null
}

/**
 * Why nothing may browse this target now -- a run or a discovery -- or null
 * (`QaTargets._startable`, with nothing filed).
 */
export function cannotBrowseBecause(t: QaTarget): string | null {
  const host = t.host ?? 'its host'
  if (t.host_state === 'pending') {
    return `${host} is waiting for a platform admin to approve it; no run starts against a host until then.`
  }
  if (t.host_state === 'revoked') return `${host}'s approval was revoked; a platform admin approves it again before a run.`
  if (t.host_state !== 'approved') return `${host} is not approved.`
  if (t.sign_in.kind === 'own_login' && !t.credential.set) {
    return 'It signs in with its own login and no test credential is set; a workspace admin sets it.'
  }
  return null
}

/**
 * The credential link's target and token, read off a location: only on the
 * link's own path, so a `t` anywhere else is never taken for one.
 */
export function qaCredentialLinkFrom(pathname: string, search: string): QaLink | null {
  const m = LINK_PATH.exec(pathname)
  if (m === null) return null
  const token = new URLSearchParams(search).get('t')
  return { targetId: m[1]!, token: token === null || token === '' ? null : token }
}

/** The token in a minted link's `path`, held by the caller in memory and never drawn. */
export function tokenOfLinkPath(path: unknown): string | null {
  if (typeof path !== 'string') return null
  const q = path.indexOf('?')
  const token = q === -1 ? null : new URLSearchParams(path.slice(q + 1)).get('t')
  return token === null || token === '' ? null : token
}

export async function loadQaTargets(): Promise<Result<QaTarget[]>> {
  // The fixture build has no QA targets to show, and invents none.
  if (USE_FIXTURES) return { status: 'empty', fetchedAt: Date.now() }
  const r = await read<unknown>(route('/v1/qa-targets'), () => false)
  if (r.status === 'ok') {
    const data = normList(r.data)
    return data.length === 0 ? { status: 'empty', fetchedAt: r.fetchedAt, serverAt: r.serverAt } : { ...r, data }
  }
  if (r.status === 'stale') return { ...r, data: normList(r.data) }
  return r
}

export function createQaTarget(body: QaTargetSend): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets'), 'POST', body)
}

export function patchQaTarget(id: string, changes: QaTargetSend): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets/{target_id}', { target_id: id }), 'PATCH', changes)
}

export function deleteQaTarget(id: string): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets/{target_id}', { target_id: id }), 'DELETE')
}

/** `:run` with `file` and nothing else the caller picks (invariant 10). */
export async function runQaTarget(id: string, file: 'issues' | 'none'): Promise<Result<QaRun>> {
  const r = await write(route('/v1/qa-targets/{target_id}:run', { target_id: id }), 'POST', { file })
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The run was not answered.' } }
  return { ...r, data: { workflow_id: str(rec(r.data).workflow_id) } }
}

/**
 * `:discover`, with nothing the caller picks: the API renders the discovery
 * from the stored target (invariant 10). A second press while one runs is the
 * API's 409 (`detail.reason` `qa_discovery_running`), said in its own words.
 */
export async function discoverQaTarget(id: string): Promise<Result<QaRun>> {
  const r = await write(route('/v1/qa-targets/{target_id}:discover', { target_id: id }), 'POST', {})
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The discovery was not answered.' } }
  return { ...r, data: { workflow_id: str(rec(r.data).workflow_id) } }
}

/**
 * A platform admin's approval of the host they typed (§1.2, VQ-Q1), and the
 * login's extra origin only when the target declares one: the API approves
 * an origin only when it is named. The answer is the target as it now is.
 */
export async function approveQaTargetHost(id: string, host: string, extraOrigin: string | null): Promise<Result<QaTarget | null>> {
  const body: { host: string; extra_origin?: string } = { host }
  if (extraOrigin !== null) body.extra_origin = extraOrigin
  const r = await write(route('/v1/qa-targets/{target_id}:approve-host', { target_id: id }), 'POST', body)
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The approval was not answered.' } }
  return { ...r, data: normQaTarget(rec(r.data).qa_target) }
}

/** A one-time link for the signed-in admin; only its token is kept, by the caller, in memory. */
export async function mintQaCredentialLink(id: string): Promise<Result<{ token: string | null; expires_at: string | null }>> {
  const r = await write(route('/v1/qa-targets/{target_id}/credential-links', { target_id: id }), 'POST')
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The link was not answered.' } }
  const body = rec(r.data)
  return { ...r, data: { token: tokenOfLinkPath(body.path), expires_at: str(body.expires_at) } }
}

/** The one call that carries the credential, in that one request's body. */
export function putQaCredential(id: string, username: string, password: string, linkToken: string): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets/{target_id}/credential', { target_id: id }), 'PUT', { username, password, link_token: linkToken })
}
