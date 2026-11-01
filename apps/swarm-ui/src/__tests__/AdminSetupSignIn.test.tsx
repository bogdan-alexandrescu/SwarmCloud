// ADMIN › SETUP'S SIGN-IN CLIENT STEP (part of #2688; issue #2964).
//
// WHAT EACH CASE HOLDS:
//   * the Sign-in client row is the FIRST row of the checklist, before the
//     cloud row and the GitHub App (owner's pick, 2026-11-01);
//   * its three links (Branding, Audience, Create client) are the route's
//     `links`, each opening in a new tab with one line saying what to set;
//   * Save posts exactly `{client_id, client_secret}` to
//     `POST /v1/admin/setup/sign-in-client`, the secret field never echoes and
//     is empty after Save;
//   * each of the route's states (waiting, saved, applied) draws its own
//     words, and a route that serves no `sign_in_client` draws "not read
//     yet", never done;
//   * a refusal shows the route's own reason.
//
// Fixtures only; no network. Fake values are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const rand = (n: number) => Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => (b % 36).toString(36)).join('')

const NOW_MS = Date.parse('2026-11-01T10:00:00Z')
const PROJECT = `swarm-${rand(6)}`
const LINKS = {
  branding: `https://console.cloud.google.com/auth/branding?project=${PROJECT}`,
  audience: `https://console.cloud.google.com/auth/audience?project=${PROJECT}`,
  create_client: `https://console.cloud.google.com/auth/clients/create?project=${PROJECT}`,
}
const CLIENT_ID = `${rand(12)}-${rand(24)}.apps.googleusercontent.com`

function signIn(over: Json = {}): Json {
  return { state: 'waiting', client_id: null, saved_at: null, saved_by: null, links: LINKS, ...over }
}

/** An install mid-edge: stage 1 done, swarm install waiting on the sign-in client. */
function state(sign_in_client: Json | undefined): Json {
  return {
    env: 'prod',
    engine: {
      release: '2026.10.1-rc.4',
      hostname: 'swarm.example.com',
      stages: Array.from({ length: 16 }, (_, i) => (i <= 1 ? { state: 'done', at: '2026-11-01T09:20:00Z', by: 'ana@example.com' } : null)),
      lease: { holder: 'laptop', heartbeat: new Date(NOW_MS - 10_000).toISOString() },
      verify: null,
      hand_off_at: null,
      ready_at: null,
    },
    cli_running: true,
    github_app: null,
    teams: [],
    accounts: {},
    finished: null,
    addons: [],
    hostname: 'swarm.example.com',
    github_org: 'example-org',
    ...(sign_in_client === undefined ? {} : { sign_in_client }),
  }
}

async function mountSteps(doc: Json, onChanged?: () => void) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../AdminSetupSteps')
  const { normSetupState } = await import('../api')
  return render(<mod.AdminSetupSteps state={normSetupState(doc)} now={NOW_MS} onChanged={onChanged} />)
}

const row = () => document.querySelector('ol[aria-label="Setup steps"] > [data-step="sign-in-client"]') as HTMLElement
const idField = () => within(row()).getByLabelText('Client id') as HTMLInputElement
const secretField = () => within(row()).getByLabelText('Client secret') as HTMLInputElement

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('Admin › Setup › Sign-in client', () => {
  it('is the first step, before the cloud row and the GitHub App', async () => {
    await mountSteps(state(signIn()))
    const order = Array.from(document.querySelectorAll('ol[aria-label="Setup steps"] > [data-step]')).map((el) => el.getAttribute('data-step'))
    // MUTATION: leave the step out, or draw it after the GitHub App.
    expect(order[0]).toBe('sign-in-client')
    expect(order.indexOf('sign-in-client')).toBeLessThan(order.indexOf('github'))
    expect(visible(row())).toContain('Sign-in client')
    // While it waits it is the next step: swarm install waits on it.
    expect(row().className).toContain('is-next')
  })

  it('links the route’s three Google pages in order, each in a new tab with what to set there', async () => {
    await mountSteps(state(signIn()))
    const links = within(row()).getAllByRole('link') as HTMLAnchorElement[]
    // MUTATION: build the URLs in the console instead of reading the route's `links`.
    expect(links.map((a) => a.getAttribute('href'))).toEqual([LINKS.branding, LINKS.audience, LINKS.create_client])
    for (const a of links) {
      expect(a.getAttribute('target')).toBe('_blank')
      expect(a.getAttribute('rel')).toContain('noreferrer')
    }
    const items = Array.from(row().querySelectorAll('ol[aria-label="Google pages"] > li')).map((li) => visible(li))
    expect(items).toHaveLength(3)
    expect(items[0]).toMatch(/Branding/)
    expect(items[0]).toMatch(/SwarmCloud/)
    expect(items[0]).toMatch(/support email/)
    expect(items[1]).toMatch(/Audience/)
    expect(items[1]).toMatch(/Internal/)
    expect(items[2]).toMatch(/Desktop app/)
    expect(items[2]).toMatch(/SwarmCloud sc/)
    // Google shows the secret only in the create dialog.
    expect(items[2]).toMatch(/keep .*dialog open/i)
  })

  it('draws a link the route did not serve as text, never as a link of its own', async () => {
    await mountSteps(state(signIn({ links: { ...LINKS, audience: 'https://evil.example.com/audience' } })))
    const hrefs = (within(row()).getAllByRole('link') as HTMLAnchorElement[]).map((a) => a.getAttribute('href'))
    // MUTATION: link whatever the answer names.
    expect(hrefs).toEqual([LINKS.branding, LINKS.create_client])
    expect(visible(row())).toMatch(/Audience/)
  })

  it('Save posts exactly {client_id, client_secret} and clears the secret', async () => {
    const secret = rand(32)
    const calls = serve((m, url) =>
      m === 'POST' && url === '/v1/admin/setup/sign-in-client'
        ? { status: 200, body: signIn({ state: 'saved', client_id: CLIENT_ID, saved_at: '2026-11-01T09:59:00Z', saved_by: 'ana@example.com' }) }
        : null,
    )
    const changed = vi.fn()
    await mountSteps(state(signIn()), changed)
    // MUTATION: echo the secret (a text field instead of a hidden one).
    expect(secretField().type).toBe('password')
    fireEvent.change(idField(), { target: { value: ` ${CLIENT_ID} ` } })
    fireEvent.change(secretField(), { target: { value: secret } })
    fireEvent.click(within(row()).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/sign-in-client')).toBe(true))
    const posted = calls.filter((c) => c.method === 'POST' && c.url === '/v1/admin/setup/sign-in-client')
    expect(posted).toHaveLength(1)
    // MUTATION: post a third field, or the untrimmed id.
    expect(posted[0].body).toEqual({ client_id: CLIENT_ID, client_secret: secret })
    // MUTATION: keep the secret in the field after Save.
    expect(secretField().value).toBe('')
    // The row re-reads the route; it is not marked saved by hand.
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect(visible(document.body)).not.toContain(secret)
  })

  it('offers no Save until both the id and the secret are typed', async () => {
    const calls = serve(() => null)
    await mountSteps(state(signIn()))
    const save = () => within(row()).getByRole('button', { name: 'Save' }) as HTMLButtonElement
    expect(save().disabled).toBe(true)
    fireEvent.change(idField(), { target: { value: CLIENT_ID } })
    // MUTATION: post an id with no secret.
    expect(save().disabled).toBe(true)
    fireEvent.click(save())
    expect(calls.some((c) => c.method === 'POST')).toBe(false)
    fireEvent.change(secretField(), { target: { value: rand(20) } })
    expect(save().disabled).toBe(false)
  })

  it('shows the route’s own reason for a refusal', async () => {
    const reason = `The client id must end .apps.googleusercontent.com (${rand(6)})`
    serve((m, url) => (m === 'POST' && url === '/v1/admin/setup/sign-in-client' ? { status: 422, body: { code: 'client_id_invalid', message: reason } } : null))
    await mountSteps(state(signIn()))
    fireEvent.change(idField(), { target: { value: `${rand(10)}.example.com` } })
    fireEvent.change(secretField(), { target: { value: rand(20) } })
    fireEvent.click(within(row()).getByRole('button', { name: 'Save' }))
    // MUTATION: say a generic failure instead of the route's message.
    await waitFor(() => expect(visible(row())).toContain(reason))
    expect(within(row()).getByRole('alert')).toBeTruthy()
    // The secret is gone even when the save was refused.
    expect(secretField().value).toBe('')
  })

  it('draws waiting as to do, with the three pages and the form', async () => {
    await mountSteps(state(signIn()))
    expect(row().className).toContain('is-todo')
    expect(visible(row())).toMatch(/swarm install waits/)
    expect(idField()).toBeTruthy()
  })

  it('draws saved as in progress, naming the id and waiting for the infra pass', async () => {
    await mountSteps(state(signIn({ state: 'saved', client_id: CLIENT_ID, saved_at: '2026-11-01T09:59:00Z', saved_by: 'ana@example.com' })))
    // MUTATION: read `saved` as done: IAP does not admit the client until the infra pass runs.
    expect(row().className).toContain('is-in_progress')
    const text = visible(row())
    expect(text).toContain(CLIENT_ID)
    expect(text).toContain('ana@example.com')
    expect(text).toMatch(/infra pass/)
    expect(text).toMatch(/programmatic/)
  })

  it('draws applied as done, naming the id IAP admits', async () => {
    await mountSteps(state(signIn({ state: 'applied', client_id: CLIENT_ID, saved_at: '2026-11-01T09:59:00Z', saved_by: 'ana@example.com' })))
    expect(row().className).toContain('is-done')
    const text = visible(row())
    expect(text).toContain(CLIENT_ID)
    expect(text).toMatch(/IAP/)
    // MUTATION: draw the saved words for applied.
    expect(text).not.toMatch(/waiting for/)
  })

  it('a second save replaces the client from the saved and applied rows', async () => {
    await mountSteps(state(signIn({ state: 'applied', client_id: CLIENT_ID, saved_at: '2026-11-01T09:59:00Z', saved_by: 'ana@example.com' })))
    // MUTATION: offer no way to replace a client whose secret was rotated.
    expect(within(row()).getByText('Replace the client')).toBeTruthy()
    expect(secretField().type).toBe('password')
  })

  it('draws a route that serves no sign_in_client as not read yet, never as done', async () => {
    await mountSteps(state(undefined))
    // MUTATION: draw a missing field as done.
    expect(row().className).not.toContain('is-done')
    expect(visible(row())).toMatch(/not read yet/i)
    cleanup()
    // A state the route never writes is not one of the three either.
    await mountSteps(state(signIn({ state: 'done' })))
    expect(row().className).not.toContain('is-done')
    expect(visible(row())).toMatch(/not read yet/i)
  })
})
