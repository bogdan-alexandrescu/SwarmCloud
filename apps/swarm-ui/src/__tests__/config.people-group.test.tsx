/**
 * THE CONFIG DOT THAT NEVER CLEARED (2026-11-01). The spine's Config button
 * drew its amber dot ("Workspace setup has a step left") on a group workspace
 * where every Config page had nothing to do: People invited read "not done"
 * because nobody besides the reader held a role override, while the
 * workspace's people come from its Google group and the People page offers
 * nothing to act on. swarm-api now answers that People invited does not
 * apply there (config_setup.py people_step), unless requests to join wait on
 * the reader.
 *
 *   * A step the server says does not apply is left out, as a personal
 *     workspace's is: the strip and the meter count 3 of 3, the spine draws
 *     no dot, and the panel's People row says why.
 *   * Requests to join waiting on the reader are still something to act on:
 *     the step is drawn, not done, with the server's count, and the dot shows.
 *
 * MUTATION: drop the `peopleNotApplying` test from `setupSteps` -- the step
 * comes back unknown, the meter reads "3 of 4" and the first cases fail.
 * Drop the server's reason from `peopleState`'s todo line -- the pending case
 * reads "only you so far" and fails.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, waitFor } from '@testing-library/react'

type Json = Record<string, unknown>

const ME: Json = {
  tenant: { tenant_id: 'eng', kind: 'group', display_name: 'eng@example.com' },
  principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: false },
  environment: 'dev',
  environment_declared: true,
}
const PERSONAL: Json = { tenant_id: 'u-ana', display_name: 'ana.b@example.com', kind: 'user', via: 'personal', role: { name: 'admin', source: 'personal' } }
const GROUP: Json = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group', via: 'member', role: { name: 'admin', source: 'override' } }

const step = (name: string, state: string, evidence: Json = {}): Json => ({ step: name, state, code: null, copy: null, checked_at: null, evidence, issues: [] })
const SOURCES = { mcp_servers: { needed: 0, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } }
const SECRETS_NONE: Json = { needed: 0, done: true, items: [], sources: SOURCES }
const FROM_GROUP = "this workspace's people come from its Google group; add them there. Nothing here is waiting on you"
/** The live deployment's answer for the group workspace, as CLD measured it, with the server's new rule. */
const PEOPLE_FROM_GROUP: Json = { applies: false, done: null, holders: 0, admins: 0, pending: 0, reason: FROM_GROUP, pending_reason: null }
const PEOPLE_WAITING: Json = { applies: true, done: false, holders: 0, admins: 0, pending: 2, reason: '2 requests to join are waiting for you', pending_reason: null }

const onboarding = (people: Json): Json => ({
  tenant_id: 'eng',
  user: 'ana.b@example.com',
  user_hash: 'h',
  derived_at: '2026-11-01T04:00:00Z',
  steps: [step('github_connected', 'done', { login: 'ana' }), step('repos_chosen', 'done', { repositories: [{ full_name: 'acme/api' }] })],
  next_step: null,
  complete: true,
  source: 'derived',
  config_setup: { secrets: SECRETS_NONE, people },
})

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

function serve(doc: Json) {
  globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/v1/tenants/mine')) return Promise.resolve(json([PERSONAL, GROUP]))
    if (url.includes('/v1/tenants/me')) return Promise.resolve(json(ME))
    if (url.includes('/v1/onboarding')) return Promise.resolve(json(doc))
    return Promise.resolve(json({ detail: 'Not Found' }, 404))
  }) as unknown as typeof fetch
}

async function app(path: string) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../App')
  window.history.replaceState(null, '', path)
  render(<mod.App />)
}

const dot = () => document.querySelector('i.sk-dot[title="Workspace setup has a step left"]')

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  window.history.replaceState(null, '', '/')
})

describe('a group workspace whose people come from its group', () => {
  it('has three steps, all done, and the panel row says why People invited is not one', async () => {
    const { setupSteps, setupCount, setupUnfinished, configBadges } = await import('../ConfigSetup')
    const steps = setupSteps(onboarding(PEOPLE_FROM_GROUP) as never, false)
    expect(steps.map((s) => s.key)).toEqual(['github', 'repositories', 'secrets'])
    expect(setupCount(steps)).toEqual({ done: 3, of: 3, unknown: 0 })
    expect(setupUnfinished(steps)).toBe(false)
    const people = configBadges(onboarding(PEOPLE_FROM_GROUP) as never, false).people
    expect(people?.tone).toBe('note')
    expect(people?.why).toBe(`People invited: ${FROM_GROUP}`)
  })

  it('the spine draws no Config dot, and the strip folds to "3 of 3"', async () => {
    serve(onboarding(PEOPLE_FROM_GROUP))
    await app('/config/secrets')
    await waitFor(() => expect(document.querySelector('.cfg-done')?.textContent ?? '').toContain('3 of 3'))
    expect(dot()).toBeNull()
  })
})

describe('requests to join waiting on the reader are still something to act on', () => {
  it('draws People invited not done, with the server\'s count', async () => {
    const { setupSteps, setupUnfinished } = await import('../ConfigSetup')
    const steps = setupSteps(onboarding(PEOPLE_WAITING) as never, false)
    const people = steps.find((s) => s.key === 'people')
    expect(people?.state).toBe('todo')
    expect(people?.line).toBe('2 requests to join are waiting for you')
    expect(setupUnfinished(steps)).toBe(true)
  })

  it('the spine draws the Config dot', async () => {
    serve(onboarding(PEOPLE_WAITING))
    await app('/config/secrets')
    await waitFor(() => expect(dot()).not.toBeNull())
  })
})
