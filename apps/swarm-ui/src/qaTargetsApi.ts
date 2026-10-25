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
 *   POST   /v1/qa-targets/{id}:run               a member starts a run: {environments?, file, browser_tool?}
 *   POST   /v1/qa-targets/{id}:discover          a member starts its discovery (§4.1, lane QA-DISCOVER)
 *   POST   /v1/qa-targets/{id}:approve-host      a PLATFORM admin approves the host: {host, extra_origin?, idp_token_path?} (VQ-Q1, audited; QA-IDP-POST)
 *   POST   /v1/qa-targets/{id}/credential-links  a one-time link to the credential form (an admin): {environment?}
 *   PUT    /v1/qa-targets/{id}/credential        the form's write, with the link's token
 *   PUT    /v1/qa-targets/{id}/challenge-bypass  its challenge bypass's value (a workspace admin, §2.5)
 *
 * A TARGET IS COPIED FIELD BY FIELD (`normQaTarget`), its credential as
 * status only -- set, when, by whom, which version -- so a reply that
 * (wrongly) carried a value could not reach the page. The credential itself
 * is taken by `putQaCredential` alone and held by nothing here.
 *
 * A CHALLENGE BYPASS (lane CAPTCHA-BYPASS, part of #2058; §2.5) is a header or
 * cookie the app's owner configured to skip their own CAPTCHA for these runs.
 * Its kind and name are registration fields; its value is status only here,
 * exactly as the credential's (`challenge_bypass_value`), and is taken by
 * `putQaChallengeBypass` alone.
 *
 * THE FLOW BROWSER (lane VQ9-TOOL; BGD, 2026-10-23) is the tool a target's
 * goal-driven flows drive the live browser with: Playwright MCP unless the
 * target, or one run, chooses Jev. Batch bench3-2026-10-23 passed Playwright
 * on 8 of 10 prompts at $0.14 and 44.5 s a run, Jev on 6 of 10 at $0.17 and
 * 51.3 s (docs/browser-bench.md). Capture stays the fixed capture tool.
 *
 * A TARGET'S ENVIRONMENTS (lane QAENV-UI-FORM, part of #2136;
 * docs/design/qa-environments.md §1.1, §7). A target stored with
 * `environments[]` is served with one entry per leg (`_entry_api`): its own
 * host and approval, sign-in, credential status, discovered routes and
 * `new_since_last_run`. The card draws each as a leg (`legsOf`), and Run
 * sends the ticked legs' NAMES and nothing else (invariant 10). A target the
 * API still serves in the one-origin shape is one leg with no name, and its
 * Run names none, which the API reads as its only entry.
 */

/** `swarm_api.qatargets._TARGET_ID`. */
export const QA_TARGET_ID = /^qat_[0-9a-f]{16}$/

/**
 * A one-time link's path, as `QaTargets.mint_link` spells it:
 * `/qa-targets/<id>/credential?t=<token>`, or `/challenge-bypass?t=..` for a
 * `field: "challenge_bypass"` link, either with `&environment=<entry>` on a
 * target of several entries (qa-environments.md §1.4).
 */
const LINK_PATH = /^\/qa-targets\/(qat_[0-9a-f]{16})\/(credential|challenge-bypass)\/?$/

/** An entry's name, `qaspec.ENVIRONMENT_NAME`; anything else on a link is read as none. */
const ENTRY_NAME = /^[a-z0-9-]{1,20}$/

/** The API's code for a link that is not this person's, used, expired or for another entry or field. */
export const QA_LINK_INVALID = 'qa_credential_link_invalid'

/** The API's code for the QA secret it could not read or write: raised after the link was consumed. */
const QA_CREDENTIAL_UNAVAILABLE = 'qa_credential_unavailable'

/** `qatargets.SECRET_FULL`: the 422 `_write_values` raises at the write, after the link was consumed. */
const QA_SECRET_FULL = 'qa_secret_full'

export type HostState = 'pending' | 'approved' | 'revoked'

/** `swarm_api.qaspec.FLOW_BROWSER_TOOLS`, the default first. */
export const FLOW_BROWSER_TOOLS = ['playwright', 'jev'] as const
export type FlowBrowserTool = (typeof FLOW_BROWSER_TOOLS)[number]

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

/** `qaspec.challenge_bypass`: the owner's header or cookie, by kind and name. Never its value. */
export interface QaChallengeBypass {
  kind: 'header' | 'cookie'
  name: string
}

export interface QaCredentialStatus {
  set: boolean
  set_at: string | null
  set_by: string | null
  version: string | null
}

/** A route a settled run found that `screens` does not list (§5): an admin may Promote it. */
export interface QaFoundScreen extends QaScreen {
  first_seen: string | null
}

/** The discovery running now (`discovering`): its workflow, once submitted. */
export interface QaDiscovering {
  workflow_id: string | null
  at: string | null
}

/**
 * One leg of a target (`_entry_api`, qa-environments.md §1.1): the fields
 * that differ per environment. `name` is null for a target served in the
 * one-origin shape, whose Run names no entry.
 */
export interface QaEnvironment {
  name: string | null
  base_url: string | null
  host: string | null
  environment: 'production' | 'non_production'
  host_state: HostState | null
  approved_by: string | null
  approved_at: string | null
  /** The one IdP path the gate lets a POST through to after this leg's sign-in (QA-IDP-POST). */
  approved_idp_token_path: string | null
  extra_origin: string | null
  sign_in: QaSignIn
  credential: QaCredentialStatus
  challenge_bypass: QaChallengeBypass | null
  challenge_bypass_value: QaCredentialStatus
  discovered: QaScreen[]
  discovered_at: string | null
  discovery_error: string | null
  discovering: QaDiscovering | null
  new_since_last_run: QaFoundScreen[]
}

export interface QaTarget {
  id: string
  name: string
  base_url: string | null
  host: string | null
  host_state: HostState | null
  approved_by: string | null
  approved_at: string | null
  /** The one path on the extra origin the gate lets a POST through to after its sign-in (QA-IDP-POST). */
  approved_idp_token_path: string | null
  repo_id: string | null
  /** The registration's repository URL, `https://github.com/<owner>/<repo>`. */
  repository: string | null
  sign_in: QaSignIn
  credential: QaCredentialStatus
  /** Its challenge bypass, or null when it has none (§2.5). */
  challenge_bypass: QaChallengeBypass | null
  /** Whether the bypass's value is set: status only, as `credential`. */
  challenge_bypass_value: QaCredentialStatus
  screens: QaScreen[]
  environment: 'production' | 'non_production'
  /** The tool its flows run with; an unset field is Playwright, `qaspec`'s default. */
  flows_browser_tool: FlowBrowserTool
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
  /** Its entries as served (`environments`), in order; empty for the one-origin shape. */
  environments: QaEnvironment[]
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
  flows_browser_tool?: FlowBrowserTool
  /** `{kind, name}`; null on a change removes it (and the API removes its value). */
  challenge_bypass?: QaChallengeBypass | null
}

/** A body: the form's fields, and any `MORE_FIELDS` (null clears one on a change). */
export type QaTargetSend = QaTargetBody & Partial<Record<MoreField, unknown>>

export interface QaRun {
  workflow_id: string | null
}

/** One leg of a run's answer: submitted with its workflow, or not started and why. */
export interface QaRunLeg {
  environment: string | null
  workflow_id: string | null
  /** The refusal's sentence of a leg the group could not start (a 207); null when it started. */
  refused: string | null
}

/** `:run`'s answer: the run group the run page reads, and each leg. */
export interface QaRunGroupStart {
  group_id: string | null
  workflow_id: string | null
  legs: QaRunLeg[]
}

export interface QaLink {
  targetId: string
  /** Null when the link's path came without its `t`. */
  token: string | null
  /**
   * The entry the link was minted for (`&environment=`), sent back on the
   * PUT; null when the link names none (a target of one entry, or a link
   * minted without one), and the API reads it off the link record (#2280).
   */
  environment: string | null
  /** The form the link opens: the credential's, or the challenge bypass value's. */
  field: 'credential' | 'challenge_bypass'
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

function normBypass(raw: unknown): QaChallengeBypass | null {
  const b = rec(raw)
  const name = str(b.name)
  if (name === null || (b.kind !== 'header' && b.kind !== 'cookie')) return null
  return { kind: b.kind, name }
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

function normFound(raw: unknown): QaFoundScreen[] {
  return Array.isArray(raw)
    ? raw.flatMap((s) => {
        const r = rec(s)
        const sid = str(r.id)
        const path = str(r.path)
        return sid !== null && path !== null ? [{ id: sid, path, first_seen: str(r.first_seen) }] : []
      })
    : []
}

function normHostState(state: unknown): HostState | null {
  return state === 'pending' || state === 'approved' || state === 'revoked' ? state : null
}

/** One served entry, field by field: its credential and bypass as status only. */
function normEnvironment(raw: unknown): QaEnvironment | null {
  const e = rec(raw)
  const name = str(e.name)
  if (name === null || !ENTRY_NAME.test(name)) return null
  const signIn = normSignIn(e.sign_in)
  return {
    name,
    base_url: str(e.base_url),
    host: str(e.host),
    environment: e.environment === 'non_production' ? 'non_production' : 'production',
    host_state: normHostState(e.host_state),
    approved_by: str(e.approved_by),
    approved_at: str(e.approved_at),
    approved_idp_token_path: str(e.approved_idp_token_path),
    extra_origin: signIn.extra_origin,
    sign_in: signIn,
    credential: normCredential(e.credential),
    challenge_bypass: normBypass(e.challenge_bypass),
    challenge_bypass_value: normCredential(e.challenge_bypass_value),
    discovered: normScreens(e.discovered),
    discovered_at: str(e.discovered_at),
    discovery_error: str(e.discovery_error),
    discovering: normDiscovering(e.discovering),
    new_since_last_run: normFound(e.new_since_last_run),
  }
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
    host_state: normHostState(state),
    approved_by: str(t.approved_by),
    approved_at: str(t.approved_at),
    approved_idp_token_path: str(t.approved_idp_token_path),
    repo_id: str(t.repo_id),
    repository: str(t.repository),
    sign_in: normSignIn(t.sign_in),
    credential: normCredential(t.credential),
    challenge_bypass: normBypass(t.challenge_bypass),
    challenge_bypass_value: normCredential(t.challenge_bypass_value),
    screens,
    environment: t.environment === 'non_production' ? 'non_production' : 'production',
    flows_browser_tool: t.flows_browser_tool === 'jev' ? 'jev' : 'playwright',
    authorized_by: str(t.authorized_by),
    revision: typeof t.revision === 'number' ? t.revision : null,
    more: Object.fromEntries(MORE_FIELDS.flatMap((k) => (t[k] === undefined || t[k] === null ? [] : [[k, t[k]]]))),
    discovers: t.discover !== undefined && t.discover !== null,
    discovered: normScreens(t.discovered),
    discovered_at: str(t.discovered_at),
    discovered_workflow: str(t.discovered_workflow),
    discovery_error: str(t.discovery_error),
    discovering: normDiscovering(t.discovering),
    environments: Array.isArray(t.environments) ? t.environments.flatMap((e) => normEnvironment(e) ?? []) : [],
  }
}

/**
 * The target's legs, in order: its served entries, or, for a target served
 * in the one-origin shape, that one origin as a leg with no name.
 */
export function legsOf(t: QaTarget): QaEnvironment[] {
  if (t.environments.length > 0) return t.environments
  return [
    {
      name: null,
      base_url: t.base_url,
      host: t.host,
      environment: t.environment,
      host_state: t.host_state,
      approved_by: t.approved_by,
      approved_at: t.approved_at,
      approved_idp_token_path: t.approved_idp_token_path,
      extra_origin: t.sign_in.extra_origin,
      sign_in: t.sign_in,
      credential: t.credential,
      challenge_bypass: t.challenge_bypass,
      challenge_bypass_value: t.challenge_bypass_value,
      discovered: t.discovered,
      discovered_at: t.discovered_at,
      discovery_error: t.discovery_error,
      discovering: t.discovering,
      new_since_last_run: [],
    },
  ]
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
 * Why a run of this leg (the target's only one when none is named) would be
 * refused now, in the API's terms, or null. The API is still the gate
 * (`QaTargets._runnable_view`): this is said beside a disabled leg, before
 * anything is asked. A target that discovers runs with nothing stored, its
 * capture steps crawling from the base (QAENV-RUN, qa-environments.md §3.1).
 */
export function cannotRunBecause(t: QaTarget, leg: QaEnvironment = legsOf(t)[0]!): string | null {
  const browse = cannotBrowseBecause(t, leg)
  if (browse !== null) return browse
  if (t.screens.length === 0 && leg.discovered.length === 0 && !t.discovers) return 'It lists no screens.'
  return null
}

/**
 * Why nothing may browse this leg now -- a run or a discovery -- or null
 * (`QaTargets._startable`, with nothing filed).
 */
export function cannotBrowseBecause(t: QaTarget, leg: QaEnvironment = legsOf(t)[0]!): string | null {
  const host = leg.host ?? 'its host'
  if (leg.host_state === 'pending') {
    return `${host} is waiting for a platform admin to approve it; no run starts against a host until then.`
  }
  if (leg.host_state === 'revoked') return `${host}'s approval was revoked; a platform admin approves it again before a run.`
  if (leg.host_state !== 'approved') return `${host} is not approved.`
  if (leg.sign_in.kind === 'own_login' && !leg.credential.set) {
    return 'It signs in with its own login and no test credential is set; a workspace admin sets it.'
  }
  return null
}

/**
 * A one-time link's target, token, entry and form, read off a location: only
 * on the link's own paths, so a `t` anywhere else is never taken for one.
 */
export function qaCredentialLinkFrom(pathname: string, search: string): QaLink | null {
  const m = LINK_PATH.exec(pathname)
  if (m === null) return null
  const q = new URLSearchParams(search)
  const token = q.get('t')
  const environment = q.get('environment')
  return {
    targetId: m[1]!,
    token: token === null || token === '' ? null : token,
    environment: environment !== null && ENTRY_NAME.test(environment) ? environment : null,
    field: m[2] === 'challenge-bypass' ? 'challenge_bypass' : 'credential',
  }
}

/**
 * Whether a PUT's answer spent its link, read from `QaTargets.set_credential`'s
 * order (and `set_challenge_bypass`'s, the same): the entry, the target's
 * sign-in or the value's shape, then `consume_link`, then the secret store,
 * then the record. A success consumed it; `qa_credential_link_invalid` is a
 * link that was dead, or is for another entry or field; the store's 503,
 * and the 422 whose `detail.reason` is `qa_secret_full` (the tenant's map
 * would pass 56 KiB), come only after the link was consumed. Every other refusal (a 422, a
 * missing entry, a 5xx with no code, no answer at all) came before the link
 * was checked or cannot say, and the link is kept: a second save of a link
 * that was in fact spent is refused as invalid, and dropped then.
 */
export function linkSpent(r: Result<unknown>): boolean {
  if (r.status !== 'error') return true
  if (r.error.code === QA_LINK_INVALID || r.error.code === QA_CREDENTIAL_UNAVAILABLE) return true
  const detail = r.error.detail
  return typeof detail === 'object' && detail !== null && (detail as { reason?: unknown }).reason === QA_SECRET_FULL
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

/**
 * Promote (qa-environments.md §5): a workspace admin's PATCH of `screens`,
 * the stored list with the found route added at its end. A run never writes
 * `screens`; this is the one way a found route enters it.
 */
export function promoteQaScreen(t: QaTarget, found: QaScreen): Promise<Result<unknown>> {
  const screens = [...t.screens.map((s) => ({ id: s.id, path: s.path })), { id: found.id, path: found.path }]
  return patchQaTarget(t.id, { screens })
}

export function deleteQaTarget(id: string): Promise<Result<unknown>> {
  return write(route('/v1/qa-targets/{target_id}', { target_id: id }), 'DELETE')
}

/**
 * `:run` with the legs to run by name (`environments`, qa-environments.md
 * §2.1), `file`, and the flows' tool for this one run when one is chosen
 * (`browser_tool`, by name), and nothing else the caller picks (invariant
 * 10: never a URL, a sign-in or a credential). Null `environments` names
 * none: a target in the one-origin shape, whose only entry the API runs.
 * Null `browserTool` sends no `browser_tool`: the target's own applies.
 *
 * The answer is the run group (`group_id`, the run page's id) and each leg;
 * a 207 is a group one of whose legs the API could not start, said per leg.
 */
export async function runQaTarget(
  id: string,
  file: 'issues' | 'none',
  browserTool: FlowBrowserTool | null = null,
  environments: string[] | null = null,
): Promise<Result<QaRunGroupStart>> {
  const body: Record<string, unknown> = environments === null ? {} : { environments }
  body.file = file
  if (browserTool !== null) body.browser_tool = browserTool
  const r = await write(route('/v1/qa-targets/{target_id}:run', { target_id: id }), 'POST', body)
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The run was not answered.' } }
  const answer = rec(r.data)
  const legs = Array.isArray(answer.legs)
    ? answer.legs.map((raw): QaRunLeg => {
        const l = rec(raw)
        const reason = rec(l.reason)
        return {
          environment: str(l.environment),
          workflow_id: str(l.workflow_id),
          refused: l.state === 'not_started' ? (str(reason.message) ?? 'it was not started') : null,
        }
      })
    : []
  return { ...r, data: { group_id: str(answer.group_id), workflow_id: str(answer.workflow_id), legs } }
}

/**
 * `:discover`, with nothing the caller picks but the leg by name (`LegBody`;
 * none for the one-origin shape): the API renders the discovery from the
 * stored target (invariant 10). A second press while one runs is the API's
 * 409 (`detail.reason` `qa_discovery_running`), said in its own words.
 */
export async function discoverQaTarget(id: string, environment: string | null = null): Promise<Result<QaRun>> {
  const r = await write(route('/v1/qa-targets/{target_id}:discover', { target_id: id }), 'POST', environment === null ? {} : { environment })
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The discovery was not answered.' } }
  return { ...r, data: { workflow_id: str(rec(r.data).workflow_id) } }
}

/**
 * A platform admin's approval of one leg's host they typed (§1.2, VQ-Q1), and the
 * login's extra origin only when the target declares one: the API approves
 * an origin only when it is named. The answer is the target as it now is.
 */
export async function approveQaTargetHost(
  id: string,
  host: string,
  extraOrigin: string | null,
  idpTokenPath: string | null = null,
  environment: string | null = null,
): Promise<Result<QaTarget | null>> {
  const body: { host: string; extra_origin?: string; idp_token_path?: string; environment?: string } = { host }
  // The entry whose host this is (§1.3, `ApproveBody.environment`); none for the one-origin shape.
  if (environment !== null) body.environment = environment
  if (extraOrigin !== null) body.extra_origin = extraOrigin
  // QA-IDP-POST: only with the extra origin it is on; the API refuses it otherwise.
  if (extraOrigin !== null && idpTokenPath !== null) body.idp_token_path = idpTokenPath
  const r = await write(route('/v1/qa-targets/{target_id}:approve-host', { target_id: id }), 'POST', body)
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The approval was not answered.' } }
  return { ...r, data: normQaTarget(rec(r.data).qa_target) }
}

/**
 * A one-time link for the signed-in admin, bound to the leg it names
 * (`environment`, required by the API on a target of several entries; null
 * for the one-origin shape, which sends no body). Only its token is kept, by
 * the caller, in memory.
 */
export async function mintQaCredentialLink(id: string, environment: string | null = null): Promise<Result<{ token: string | null; expires_at: string | null }>> {
  const target = route('/v1/qa-targets/{target_id}/credential-links', { target_id: id })
  const r = environment === null ? await write(target, 'POST') : await write(target, 'POST', { environment })
  if (r.status !== 'ok') return r.status === 'error' ? r : { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The link was not answered.' } }
  const body = rec(r.data)
  return { ...r, data: { token: tokenOfLinkPath(body.path), expires_at: str(body.expires_at) } }
}

/**
 * The one call that carries a challenge bypass's value, in that one request's
 * body, with a `field: "challenge_bypass"` link's token and its entry when
 * the link names one.
 */
export function putQaChallengeBypass(
  id: string,
  value: string,
  link: { token: string; environment: string | null } | null = null,
): Promise<Result<unknown>> {
  const body: { value: string; link_token?: string; environment?: string } = { value }
  if (link !== null) {
    body.link_token = link.token
    if (link.environment !== null) body.environment = link.environment
  }
  return write(route('/v1/qa-targets/{target_id}/challenge-bypass', { target_id: id }), 'PUT', body)
}

/** The one call that carries the credential, in that one request's body, with the link's entry when it names one. */
export function putQaCredential(
  id: string,
  username: string,
  password: string,
  linkToken: string,
  environment: string | null = null,
): Promise<Result<unknown>> {
  const body: { username: string; password: string; link_token: string; environment?: string } = { username, password, link_token: linkToken }
  if (environment !== null) body.environment = environment
  return write(route('/v1/qa-targets/{target_id}/credential', { target_id: id }), 'PUT', body)
}
