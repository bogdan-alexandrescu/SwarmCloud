// THE TEAMMATE'S PART, IN THREE LINES, beside the one-time link's panel (part
// of #2688; the owner's pick for the Day One console finish, the checklist
// beside a working panel, 2026-11-02).
//
// WHAT EACH CASE HOLDS, from fixture answers of `POST /v1/setup/links/redeem`:
//   * an unused link: nothing is redeemed by drawing the page, and a refusal
//     that kept the link unused shows the hours left it served, or "time left:
//     not recorded" when it served none, never a guess;
//   * a used link and an expired link: swarm-api answers both with one 404, so
//     the first line says it no longer opens and never claims which, and the
//     two pages read the same;
//   * a link already opened in this tab (a reload): the first line is done;
//   * a pool link signed in to the end: three of three done.
//
// MUTATIONS, each of which turns a case red: drop YourPart (no list); tick a
// line by hand (a fresh page reads a line done); invent a time left (the
// unrecorded case reads hours); tell used from expired (the two pages differ);
// lose the sign-in's end (the last line stays undone).
//
// Stubbed fetch only; no network, no credentials. Tokens are built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { visible } from './repofixture'

type Json = Record<string, unknown>
type Call = { method: string; url: string; body: Json | null }
type Answer = { status: number; body: unknown } | null

const rand = (n: number) => Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => (b % 36).toString(36)).join('')
const WAIT = { timeout: 10_000 } as const

function stub(handler: (m: string, url: string, body: Json | null) => Answer): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? (JSON.parse(String(init.body)) as Json) : null
    calls.push({ method, url, body })
    const r = handler(method, url, body) ?? { status: 404, body: { detail: 'Not Found' } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: { 'content-type': 'application/json' } })
  }) as unknown as typeof fetch
  return calls
}

async function page(token: string | null) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const mod = await import('../SetupLink')
  render(<mod.SetupLinkScreen token={token} />)
  return mod
}

const part = () => document.querySelector('nav[aria-label="Your part"]')
const line = (n: number) => document.querySelector(`nav[aria-label="Your part"] [data-part="${n}"]`)
const marks = () => [1, 2, 3].map((n) => line(n)?.getAttribute('data-state'))

async function clickOpen() {
  const label = await screen.findByText('Open the link', { selector: 'button, button *' }, WAIT)
  fireEvent.click(label.closest('button')!)
}

/** swarm-api's one answer for a used, an expired and an unknown link (setup_links.LinkInvalid). */
const INVALID: Answer = { status: 404, body: { code: 'not_found', message: 'this link is not valid: ask the admin for a new one' } }

/** A link's fixture record, as swarm-api keeps it (setup_links.SetupLinks.mint), with its workspace's readiness. */
interface LinkRecord {
  expires_at: number
  used_at: number | null
  ready: boolean
  /** Whether the 409 serves the link's `expires_at` (the question this lane asks of swarm-api). */
  serves_expiry: boolean
}

/** `POST /v1/setup/links/redeem` over fixture records, refusing as `SetupLinks._check` does. */
function redeemOver(records: Record<string, LinkRecord>) {
  return (m: string, url: string, body: Json | null): Answer => {
    if (m !== 'POST' || url !== '/v1/setup/links/redeem') return null
    const rec = records[String(body?.token)]
    if (rec === undefined || rec.used_at !== null || Date.now() >= rec.expires_at) return INVALID
    if (!rec.ready) return notReady(rec.serves_expiry ? { expires_at: new Date(rec.expires_at).toISOString() } : {})
    rec.used_at = Date.now()
    return { status: 200, body: { kind: 'pool-account', label: 'lea', tenant: 'engineering', sign_in: {} } }
  }
}

const HOUR = 3600_000

/** The 409 a pool link answers while its workspace is not ready: the link stays unused. */
function notReady(detail: Json): Answer {
  return {
    status: 409,
    body: {
      code: 'conflict',
      message: "this link's workspace is not ready yet, so its sign-in would be refused; the link is still unused, open it again in a moment",
      detail: { workspace: 'engineering', workspace_state: 'provisioning', retry: true, ...detail },
    },
  }
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  sessionStorage.clear()
})

describe("the teammate's part beside the link page", () => {
  it('an unopened link: three lines, none ticked, and nothing redeemed by drawing them', async () => {
    const calls = stub(() => null)
    await page(rand(43))
    await waitFor(() => expect(part()).not.toBeNull(), WAIT)
    expect(visible(part())).toMatch(/Your part of the setup · 0 of 3 done/)
    expect(marks()).toEqual(['todo', 'todo', 'todo'])
    expect(visible(line(1))).toMatch(/one click; it works once, for you only, within 24 hours of being made/)
    expect(visible(line(2))).toMatch(/Sign in your Claude account/)
    expect(visible(line(3))).toMatch(/That is all/)
    // The panel is today's, beside the list.
    expect(screen.getByText('Your setup link')).toBeTruthy()
    expect(calls.filter((c) => c.url.includes('/links/redeem'))).toHaveLength(0)
  })

  it('an unused link shows the hours left the platform served', async () => {
    const token = rand(43)
    // Made three hours ago: 21 hours left.
    stub(redeemOver({ [token]: { expires_at: Date.now() + 21 * HOUR + 20_000, used_at: null, ready: false, serves_expiry: true } }))
    await page(token)
    await clickOpen()
    await screen.findByText('Your workspace is still being set up.', {}, WAIT)
    await waitFor(() => expect(visible(line(1))).toMatch(/still unused · 21 h left on the link/), WAIT)
    expect(marks()).toEqual(['in_progress', 'todo', 'todo'])
    expect(visible(line(1))).toMatch(/You/)
  })

  it('an unused link whose time left was not served says so, never a guess', async () => {
    const token = rand(43)
    stub(redeemOver({ [token]: { expires_at: Date.now() + 21 * HOUR, used_at: null, ready: false, serves_expiry: false } }))
    await page(token)
    await clickOpen()
    await waitFor(() => expect(visible(line(1))).toMatch(/still unused · time left: not recorded/), WAIT)
    expect(visible(line(1))).not.toMatch(/\d+ h|\d+ min/)
  })

  it('the hours left are drawn from expires_at as swarm-api serves it, on the 409 and on a value link\'s 422', async () => {
    // Python's isoformat in universal time: microseconds and a +00:00 offset, not toISOString's Z.
    const served = (t: number) => new Date(t).toISOString().replace(/\.(\d{3})Z$/, '.$1456+00:00')
    const token = rand(43)
    stub((m, url, body) => {
      if (m !== 'POST' || url !== '/v1/setup/links/redeem' || body?.token !== token) return null
      return {
        status: 422,
        body: {
          code: 'validation_failed',
          message: 'a team-git link is opened with its value; the link is still unused, open it again with the value',
          detail: { expires_at: served(Date.now() + 5 * HOUR + 20_000) },
        },
      }
    })
    await page(token)
    await clickOpen()
    await waitFor(() => expect(visible(line(1))).toMatch(/still unused · 5 h left on the link/), WAIT)
    expect(visible(line(2))).toMatch(/Store the team's GitHub token/)
    cleanup()
    const pool = rand(43)
    stub((m, url, body) => (m === 'POST' && url === '/v1/setup/links/redeem' && body?.token === pool ? notReady({ expires_at: served(Date.now() + 7 * HOUR + 20_000) }) : null))
    await page(pool)
    await clickOpen()
    await waitFor(() => expect(visible(line(1))).toMatch(/still unused · 7 h left on the link/), WAIT)
  })

  it('a used link and an expired link read the same: no longer opens, and the platform does not say which', async () => {
    const seen: string[] = []
    const used: LinkRecord = { expires_at: Date.now() + 10 * HOUR, used_at: Date.now() - HOUR, ready: true, serves_expiry: true }
    const expired: LinkRecord = { expires_at: Date.now() - HOUR, used_at: null, ready: true, serves_expiry: true }
    // swarm-api answers both with one 404, so a probe learns nothing.
    for (const record of [used, expired]) {
      const token = rand(43)
      const calls = stub(redeemOver({ [token]: record }))
      await page(token)
      await clickOpen()
      await waitFor(() => expect(line(1)?.getAttribute('data-state')).toBe('failed'), WAIT)
      expect(marks()).toEqual(['failed', 'todo', 'todo'])
      expect(visible(line(1))).toMatch(/it was used, it expired \(a link lasts 24 hours\) or was never issued, and the platform does not say which/)
      expect(visible(line(1))).toMatch(/The admin who sent it: a new link/)
      expect(calls.filter((c) => c.url === '/v1/setup/links/redeem')).toHaveLength(1)
      seen.push(visible(part()))
      cleanup()
    }
    expect(seen[0]).toBe(seen[1])
  })

  it('a link already opened in this tab is used: the first line is done and the sign-in is now', async () => {
    stub(() => null)
    sessionStorage.setItem('swarm.setup-link.pool', JSON.stringify({ label: 'lea', tenant: 'engineering' }))
    await page(null)
    await waitFor(() => expect(part()).not.toBeNull(), WAIT)
    expect(marks()).toEqual(['done', 'in_progress', 'todo'])
    expect(visible(line(1))).toMatch(/opened; the link is now used/)
    expect(visible(part())).toMatch(/1 of 3 done/)
  })

  it('a pool link signed in to the end reads three of three done', async () => {
    const token = rand(43)
    const state = rand(24)
    const redeem = redeemOver({ [token]: { expires_at: Date.now() + 21 * HOUR, used_at: null, ready: true, serves_expiry: false } })
    stub((m, url, body) => {
      if (url === '/v1/setup/links/redeem') return redeem(m, url, body)
      if (m === 'POST' && url === '/v1/accounts/authorize') {
        return { status: 200, body: { authorize_url: 'https://claude.example.test/oauth/authorize', state, expires_in_seconds: 600 } }
      }
      if (m === 'POST' && url === '/v1/accounts/exchange') {
        return { status: 201, body: { account: { account_id: 'acct-1', label: 'lea', owner_tenant: 'engineering' }, expires_at: null } }
      }
      return null
    })
    await page(token)
    await clickOpen()
    await screen.findByText('Open the Claude sign-in page', {}, WAIT)
    expect(marks()).toEqual(['done', 'in_progress', 'todo'])
    // Today's sign-in, unchanged: the person pastes the code Claude shows.
    fireEvent.change(screen.getByLabelText('The code Claude shows'), { target: { value: rand(20) } })
    fireEvent.click((await screen.findByText('Finish the sign-in', { selector: 'button, button *' }, WAIT)).closest('button')!)
    await waitFor(() => expect(marks()).toEqual(['done', 'done', 'done']), WAIT)
    expect(visible(part())).toMatch(/3 of 3 done/)
  })
})

describe('partLines', () => {
  it('a served time already past reads as run out, not as time left', async () => {
    const { partLines } = await page(null)
    const now = Date.now()
    const [open] = partLines({ link: 'unused', kind: 'pool-account', expiresAt: now - 1000, done: false }, now)
    expect(open!.text).toMatch(/its time has run out/)
  })

  it('a value link names its own second line', async () => {
    const { partLines } = await page(null)
    const ls = partLines({ link: 'unused', kind: 'team-git', expiresAt: null, done: false }, Date.now())
    expect(ls[1]!.title).toBe("Store the team's GitHub token")
    expect(ls[1]!.state).toBe('in_progress')
  })
})
