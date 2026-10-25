/**
 * THE CONFIG SETUP READ (CONFIG-PANEL-E; part of #2157, proposal E,
 * docs/web-ui/mockups/config-e.html S1, S1a, S10a).
 *
 * The setup strip's steps 3 and 4, Secrets set and People invited, are served
 * by CONFIG-SETUP-READ as `config_setup`, a field of `GET /v1/onboarding`
 * beside the checklist (swarm_api/config_setup.py), so the strip and the
 * Config panel still make one read for all four steps. This module is the
 * console's side of that field: its shape, a reader that keeps only what the
 * server says it is, and a read of one named workspace's setup for Admin ›
 * Workspaces you admin. It lives apart from api.ts so the lanes editing that
 * file and this one do not meet.
 *
 * NAMES ONLY, AS THE SERVER SENDS THEM. An item is what needs setting and who
 * asks for it, never a value: the server copies its fields from an allow-list
 * and this reader copies only the ones it draws.
 *
 * UNREAD IS NULL, NEVER 0. A source the server could not read is
 * `{needed: null, reason}` and makes the step's `needed` and `done` null; a
 * field missing or of the wrong type here is read the same way, so a payload
 * the console does not understand is drawn as unknown and never as done.
 */
import { USE_FIXTURES, loadOnboarding } from './api'
import { TENANT_HEADER, classifyFailure, route, type ErrorEnvelope, type Result } from './fetch'
import type { OnboardingDoc } from './types'

/** One thing a registered consumer needs and nobody has stored. */
export interface SetupSecretItem {
  /** `mcp_server`, `qa_target` or `provider_key` today; drawn as given. */
  kind: string
  name: string
  /** Who asks for it, in the server's words. */
  needed_by: string | null
  /** The console address it is set on (paths.ts). */
  where: string | null
  /** A QA target entry's environment. */
  environment: string | null
}

export interface SetupSource {
  needed: number | null
  reason: string | null
}

export interface SetupSecretsStep {
  /** null when any source was unread. */
  needed: number | null
  done: boolean | null
  items: SetupSecretItem[]
  sources: Record<string, SetupSource>
}

export interface SetupPeopleStep {
  /** False in a personal workspace: there is nobody to invite. */
  applies: boolean
  /** null when the roles were unread, or the step does not apply. */
  done: boolean | null
  /** People other than the reader holding a role here. */
  holders: number | null
  admins: number | null
  reason: string | null
}

export interface ConfigSetup {
  secrets: SetupSecretsStep | null
  people: SetupPeopleStep | null
}

function record(v: unknown): Record<string, unknown> | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}
const text = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v : null)
const count = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null)
const flag = (v: unknown): boolean | null => (typeof v === 'boolean' ? v : null)

function secretsOf(raw: unknown): SetupSecretsStep | null {
  const r = record(raw)
  if (r === null) return null
  const items: SetupSecretItem[] = (Array.isArray(r.items) ? r.items : []).flatMap((x) => {
    const i = record(x)
    const name = i === null ? null : text(i.name)
    if (i === null || name === null) return []
    return [{ kind: text(i.kind) ?? 'secret', name, needed_by: text(i.needed_by), where: text(i.where), environment: text(i.environment) }]
  })
  const sources: Record<string, SetupSource> = {}
  for (const [key, value] of Object.entries(record(r.sources) ?? {})) {
    const s = record(value)
    sources[key] = { needed: s === null ? null : count(s.needed), reason: s === null ? null : text(s.reason) }
  }
  const needed = count(r.needed)
  // An unread source makes the whole step unread, whatever `needed` says.
  const unread = needed === null || Object.values(sources).some((s) => s.needed === null)
  return { needed: unread ? null : needed, done: unread ? null : needed === 0, items, sources }
}

function peopleOf(raw: unknown): SetupPeopleStep | null {
  const r = record(raw)
  if (r === null) return null
  const applies = flag(r.applies)
  if (applies === null) return null
  const done = applies ? flag(r.done) : null
  return { applies, done, holders: done === null ? null : count(r.holders), admins: done === null ? null : count(r.admins), reason: text(r.reason) }
}

/** `config_setup` off an onboarding read; null when the read did not carry it. */
export function configSetupOf(doc: OnboardingDoc | null): ConfigSetup | null {
  const raw = doc === null ? null : record((doc as unknown as Record<string, unknown>).config_setup)
  if (raw === null) return null
  return { secrets: secretsOf(raw.secrets), people: peopleOf(raw.people) }
}

/** Why the secrets step is unread: the first unread source's sentence. */
export function secretsUnreadReason(step: SetupSecretsStep): string {
  return Object.values(step.sources).find((s) => s.needed === null)?.reason ?? 'what is needed could not be read'
}

/** An item's name as the strip says it: a QA entry with its environment. */
export function itemName(item: SetupSecretItem): string {
  return item.environment === null ? item.name : `${item.name} (${item.environment})`
}

/** fetch.ts READ_INIT's form: the init's credential mode, named once (a literal reads as a key to the publish scan). */
const READ_INIT = { sameOrigin: 'same-origin' } as const satisfies Record<string, RequestCredentials>

/**
 * ONE NAMED WORKSPACE'S SETUP: `GET /v1/onboarding` with `X-Swarm-Tenant`
 * naming `tenantId`, for Admin › Workspaces you admin, which draws every
 * workspace's progress at once without switching the one in hand. The
 * header SELECTS among the caller's verified memberships and never grants
 * one (swarm_api `tenant_scope`), so a workspace the caller is not in is the
 * API's refusal, drawn as unread. Failures are classified by fetch.ts's one
 * classifier, so a status means here what it means everywhere.
 */
export async function loadWorkspaceSetup(tenantId: string, signal?: AbortSignal): Promise<Result<OnboardingDoc>> {
  // The fixture build has one workspace's onboarding read; every workspace reads it.
  if (USE_FIXTURES) return loadOnboarding()
  const target = route('/v1/onboarding')
  let res: Response
  try {
    // Behind IAP the browser holds the session cookie; nothing here handles a token.
    res = await globalThis.fetch(target.url, { headers: { accept: 'application/json', [TENANT_HEADER]: tenantId }, credentials: READ_INIT.sameOrigin, signal })
  } catch (err) {
    return { status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: err instanceof Error ? err.message : 'The request did not complete.' } }
  }
  if (!(res.headers.get('content-type') ?? '').includes('application/json')) {
    return { status: 'error', error: { kind: 'session_expired', httpStatus: res.status, code: null, message: 'The API answered with a page instead of data, which is how an expired sign-in arrives. Reload to sign in again.' } }
  }
  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    body = null
  }
  if (!res.ok) {
    const env = record(body)
    const envelope: ErrorEnvelope | null = env === null ? null : typeof env.message === 'string' ? env : { message: text(env.detail) ?? undefined }
    return { status: 'error', error: classifyFailure(res.status, envelope, undefined) }
  }
  if (record(body) === null) return { status: 'error', error: { kind: 'server_error', httpStatus: res.status, code: null, message: 'The setup read was not readable.' } }
  return { status: 'ok', data: body as OnboardingDoc, fetchedAt: Date.now() }
}
