// JOIN-UI-PERSON (docs/design/join-requests.md §4.2, §4.6, §4.8, §6 row 7;
// part of #2325): a person asks to join a group workspace with its join link,
// and the workspace's admins turn the link on and see who joined by request.
//
// WHAT EACH CASE HOLDS (§6 row 7's three, then the page's own):
//   * the code is read from the URL's FRAGMENT and posted in a BODY: no
//     request's URL carries it, and the bar is rewritten to `/join` without it;
//   * the person's request list shows each state -- requested (with
//     Withdraw), approved, denied with its reason, withdrawn, expired;
//   * the link is shown ONCE, from the answer that made it: no read carries
//     it, and a second visit to the page does not show it;
//   * a code that does not work says so, with no form; no code reads nothing;
//   * only a group workspace's admin reads the link or the memberships, and
//     a removal sends the email in the body.
//
// MUTATIONS, each of which turns a case red: put the code in the lookup's
// path or query; leave the fragment in the bar; drop a state from
// `requestWords`; keep the made link in a module-level variable or show it
// from the read; draw the join cards for a viewer.
//
// Fixtures only; no network. The code is built at runtime: nothing here is
// credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Call = { method: string; url: string; body: unknown }

// A made-up code of a join link's shape (22 url-safe characters), built at runtime.
const CODE = ['jc', 'Q4', 'x'.repeat(8), '-_', 'k'.repeat(8)].join('')
const LINK = `https://console.example.test/join#${CODE}`
const HOUR = 3600 * 1000
const iso = (ms: number) => new Date(Date.now() + ms).toISOString()

function request(ref: string, state: string, over: Json = {}): Json {
  return {
    ref, state, display_name: 'Example Labs',
    requested_at: iso(-2 * HOUR), expires_at: iso(14 * 24 * HOUR - 2 * HOUR),
    ...over,
  }
}

const ALL_STATES = [
  request('j-000000000001', 'requested'),
  request('j-000000000002', 'approved', { display_name: 'Example Team', decided_at: iso(-HOUR) }),
  request('j-000000000003', 'denied', { display_name: 'Studio', decided_at: iso(-HOUR), reason: 'this workspace is for the labs rota' }),
  request('j-000000000004', 'withdrawn', { display_name: 'Data', decided_at: iso(-HOUR) }),
  request('j-000000000005', 'expired', { display_name: 'Ops', requested_at: iso(-15 * 24 * HOUR), expires_at: iso(-24 * HOUR) }),
]

const MINE_URL = '/v1/join-requests/mine'
const LOOKUP_URL = '/v1/join/lookup'
const ASK_URL = '/v1/join-requests'
const LINK_URL = '/v1/workspace/join-link'
const MEMBERS_URL = '/v1/workspace/members'
const ROLES_URL = '/v1/workspace/roles'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

async function openApp(at: string) {
  window.history.replaceState(null, '', at)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { App } = await import('../App')
  render(<App />)
}

async function loadJoin() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../JoinWorkspace')
}

/** The person's side: a working code names Example Labs; `mine` is what the list reads. */
function personApi(mine: () => Json[], over?: (m: string, url: string, b: unknown) => { status: number; body: unknown } | null) {
  return serve((m, url, b) => {
    const own = over?.(m, url, b)
    if (own) return own
    if (m === 'GET' && url === MINE_URL) return { status: 200, body: { requests: mine() } }
    if (m === 'POST' && url === LOOKUP_URL) {
      return (b as Json | null)?.code === CODE
        ? { status: 200, body: { display_name: 'Example Labs' } }
        : { status: 404, body: { code: 'join_link_unknown', message: "this join link does not work; ask the workspace's admin for a new one" } }
    }
    return null
  })
}

function noUrlCarriesTheCode(calls: Call[]) {
  for (const c of calls) expect(c.url, `${c.method} ${c.url} carries the join code`).not.toContain(CODE)
}

describe('/join: the code travels in a body, never a path', () => {
  it('is its own page, and the route is written back as /join with no fragment', async () => {
    window.history.replaceState(null, '', `/join#${CODE}`)
    const { fromLocation, canonical } = await import('../App')
    const r = fromLocation()
    expect(r).toMatchObject({ sectionId: 'join', taskId: null })
    expect(canonical(r)).toBe('join')
  })

  it('reads the code from the fragment, posts it in the lookup body, and takes it off the bar', async () => {
    const calls = personApi(() => [])
    await openApp(`/join#${CODE}`)
    await screen.findByRole('heading', { name: 'Ask to join Example Labs' })
    const lookups = calls.filter((c) => c.url === LOOKUP_URL)
    expect(lookups.length).toBeGreaterThan(0)
    for (const c of lookups) {
      expect(c.method).toBe('POST')
      expect(c.body).toEqual({ code: CODE })
    }
    noUrlCarriesTheCode(calls)
    // The bar no longer holds the code: a screenshot or a copied address cannot leak it.
    await waitFor(() => expect(window.location.pathname).toBe('/join'))
    expect(window.location.hash).toBe('')
    expect(window.location.href).not.toContain(CODE)
  })

  it('sends the ask with the code and the note in its body, then shows the request waiting', async () => {
    let mine: Json[] = []
    const calls = personApi(() => mine, (m, url) => {
      if (m === 'POST' && url === ASK_URL) {
        mine = [request('j-0000000000aa', 'requested')]
        return { status: 202, body: mine[0] }
      }
      return null
    })
    const { JoinWorkspaceScreen } = await loadJoin()
    render(<JoinWorkspaceScreen code={CODE} />)
    await screen.findByRole('heading', { name: 'Ask to join Example Labs' })
    fireEvent.change(screen.getByLabelText('A note for its admins (optional)'), { target: { value: "I'm on the labs rota from Monday" } })
    fireEvent.click(screen.getByRole('button', { name: 'Ask to join' }))
    await screen.findByText('Your request to join Example Labs is waiting for its admins.')
    const asks = calls.filter((c) => c.url === ASK_URL)
    expect(asks).toHaveLength(1)
    expect(asks[0]!.method).toBe('POST')
    expect(asks[0]!.body).toEqual({ code: CODE, message: "I'm on the labs rota from Monday" })
    // The list re-reads and shows it, with a way to withdraw.
    const table = await screen.findByRole('table', { name: 'Your requests to join' })
    await waitFor(() => expect(visible(table)).toMatch(/Waiting for an admin/))
    noUrlCarriesTheCode(calls)
  })

  it('shows a refusal in the API’s words and claims nothing', async () => {
    personApi(() => [], (m, url) => (m === 'POST' && url === ASK_URL
      ? { status: 409, body: { code: 'join_already_member', message: 'you are already in Example Labs; switch to it from the workspace menu' } }
      : null))
    const { JoinWorkspaceScreen } = await loadJoin()
    render(<JoinWorkspaceScreen code={CODE} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }))
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/not sent: you are already in Example Labs.*\(join_already_member\)/)
    expect(screen.queryByText(/is waiting for its admins/)).toBeNull()
  })

  it('says a code that does not work does not, with no form to ask', async () => {
    const calls = personApi(() => [])
    const { JoinWorkspaceScreen } = await loadJoin()
    const other = CODE.split('').reverse().join('')
    render(<JoinWorkspaceScreen code={other} />)
    await screen.findByText('This join link does not work.')
    expect(screen.queryByRole('button', { name: 'Ask to join' })).toBeNull()
    expect(calls.filter((c) => c.url === ASK_URL)).toHaveLength(0)
  })

  it('without a code reads only the person’s own requests and says how to ask', async () => {
    const calls = personApi(() => [])
    await openApp('/join')
    await screen.findByRole('heading', { name: 'Ask with a join link' })
    await screen.findByText('You have not asked to join a workspace.')
    expect(calls.filter((c) => c.url === LOOKUP_URL)).toHaveLength(0)
  })
})

describe('/join: the person’s own requests, in every state', () => {
  it('shows each state in words, a denial with its reason, and Withdraw only on an open one', async () => {
    personApi(() => ALL_STATES)
    const { JoinWorkspaceScreen } = await loadJoin()
    render(<JoinWorkspaceScreen code={null} />)
    const table = await screen.findByRole('table', { name: 'Your requests to join' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(5)
    const byState = (s: string) => rows.find((r) => r.getAttribute('data-state') === s)!
    expect(visible(byState('requested'))).toMatch(/Example Labs.*Waiting for an admin.*open 14 more days/)
    expect(visible(byState('approved'))).toMatch(/Example Team.*Approved.*workspace menu/)
    expect(visible(byState('denied'))).toMatch(/Studio.*Denied.*this workspace is for the labs rota/)
    expect(visible(byState('withdrawn'))).toMatch(/Data.*Withdrawn/)
    expect(visible(byState('expired'))).toMatch(/Ops.*Expired.*ask again/)
    expect(screen.getAllByRole('button', { name: /^Withdraw/ })).toHaveLength(1)
    expect(within(byState('requested')).getByRole('button', { name: 'Withdraw your request to join Example Labs' })).toBeTruthy()
  })

  it('withdraws by its ref, and re-reads', async () => {
    let state = 'requested'
    const calls = personApi(() => [request('j-000000000001', state)], (m, url) => {
      if (m === 'POST' && url === '/v1/join-requests/j-000000000001/withdraw') {
        state = 'withdrawn'
        return { status: 200, body: { ref: 'j-000000000001', state } }
      }
      return null
    })
    const { JoinWorkspaceScreen } = await loadJoin()
    render(<JoinWorkspaceScreen code={null} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Withdraw your request to join Example Labs' }))
    await screen.findByText('Your request to join Example Labs is withdrawn.')
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Withdraw/ })).toBeNull())
    expect(calls.filter((c) => c.method === 'POST' && c.url.endsWith('/withdraw'))).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// Settings › People and roles: the join link and Joined by request
// ---------------------------------------------------------------------------

function roles(over: Json): Json {
  return {
    tenant_id: 'example-labs', kind: 'group', default_role: 'member', default_source: 'set',
    me: { email: 'alex@example.com', role: 'admin', source: 'override', via: 'member' },
    people: [], roles: [], can_manage: true, audit: [], ...over,
  }
}

type Link = { on: boolean; set_by: string; set_at: string }

function adminApi(rolesDoc: Json, start: Link, members: Json[] = [], platformMemberships = true) {
  let link: Link = { ...start }
  let held = [...members]
  const calls = serve((m, url, b) => {
    if (m === 'GET' && url === ROLES_URL) return { status: 200, body: rolesDoc }
    // The read never carries a link: only the PUT that made a code does.
    if (m === 'GET' && url === LINK_URL) return { status: 200, body: link }
    if (m === 'PUT' && url === LINK_URL) {
      const body = b as { on: boolean; rotate: boolean }
      const made = body.rotate || (body.on && !link.on && start.set_at === '')
      link = { on: body.on, set_by: 'alex@example.com', set_at: new Date().toISOString() }
      return { status: 200, body: made && body.on ? { ...link, link: LINK } : link }
    }
    if (m === 'GET' && url === MEMBERS_URL) return { status: 200, body: { platform_memberships: platformMemberships, members: held } }
    if (m === 'DELETE' && url === '/v1/workspace/members/person') {
      const email = (b as Json).email
      held = held.filter((x) => x.email !== email)
      return { status: 200, body: { email, state: 'removed', still_in_group: true } }
    }
    return null
  })
  return calls
}

async function loadPeople() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../WorkspacePeople')
}

describe('Settings › People and roles: the join link is shown once', () => {
  it('turns the link on, shows the link from that answer, and never from a read', async () => {
    const calls = adminApi(roles({}), { on: false, set_by: '', set_at: '' })
    const { WorkspacePeopleScreen } = await loadPeople()
    render(<WorkspacePeopleScreen />)
    const card = await screen.findByLabelText('Join link')
    expect(visible(card)).toMatch(/The join link is off/)
    fireEvent.click(screen.getByRole('button', { name: 'Turn the link on' }))
    const field = await screen.findByLabelText('Link to share')
    expect((field as HTMLInputElement).value).toBe(LINK)
    expect(screen.getByText('Copy the link now: it is shown this once.')).toBeTruthy()
    const puts = calls.filter((c) => c.method === 'PUT' && c.url === LINK_URL)
    expect(puts.map((c) => c.body)).toEqual([{ on: true, rotate: false }])
    await waitFor(() => expect(visible(screen.getByLabelText('Join link'))).toMatch(/The join link is on/))
    // Still shown after the re-read: the answer is what holds it, and the read never carried it.
    expect((screen.getByLabelText('Link to share') as HTMLInputElement).value).toBe(LINK)
  })

  it('does not show it again on the next visit, and offers a new link instead', async () => {
    adminApi(roles({}), { on: false, set_by: '', set_at: '' })
    const { WorkspacePeopleScreen } = await loadPeople()
    const first = render(<WorkspacePeopleScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Turn the link on' }))
    await screen.findByLabelText('Link to share')
    first.unmount()
    render(<WorkspacePeopleScreen />)
    await screen.findByRole('button', { name: 'Make a new link' })
    await waitFor(() => expect(visible(screen.getByLabelText('Join link'))).toMatch(/The join link is on/))
    expect(screen.queryByLabelText('Link to share')).toBeNull()
    expect(document.body.textContent ?? '').not.toContain(CODE)
  })

  it('rotates with rotate: true and shows the new link; turning it off takes it away', async () => {
    const calls = adminApi(roles({}), { on: true, set_by: 'sam@example.com', set_at: iso(-HOUR) })
    const { WorkspacePeopleScreen } = await loadPeople()
    render(<WorkspacePeopleScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Make a new link' }))
    expect(((await screen.findByLabelText('Link to share')) as HTMLInputElement).value).toBe(LINK)
    fireEvent.click(screen.getByRole('button', { name: 'Turn the link off' }))
    await screen.findByText(/The join link is off: nobody can ask to join example-labs with it\./)
    expect(screen.queryByLabelText('Link to share')).toBeNull()
    expect(calls.filter((c) => c.method === 'PUT').map((c) => c.body)).toEqual([
      { on: true, rotate: true },
      { on: false, rotate: false },
    ])
  })

  it('reads neither the link nor the memberships for a viewer, or in a personal workspace', async () => {
    const viewer = adminApi(roles({ can_manage: false, me: { email: 'lee@example.com', role: 'viewer', source: 'default', via: 'member' } }), { on: true, set_by: '', set_at: '' })
    let mod = await loadPeople()
    const a = render(<mod.WorkspacePeopleScreen />)
    await screen.findByText(/only this workspace.s admins change roles/i)
    expect(screen.queryByLabelText('Join link')).toBeNull()
    expect(viewer.some((c) => c.url === LINK_URL || c.url === MEMBERS_URL)).toBe(false)
    a.unmount()

    const personal = adminApi(roles({ tenant_id: 'u-alex', kind: 'user', can_manage: true }), { on: false, set_by: '', set_at: '' })
    mod = await loadPeople()
    render(<mod.WorkspacePeopleScreen />)
    await screen.findByText(/a personal workspace has only you/)
    expect(screen.queryByLabelText('Join link')).toBeNull()
    expect(personal.some((c) => c.url === LINK_URL || c.url === MEMBERS_URL)).toBe(false)
  })
})

describe('Settings › People and roles: a link a person can open', () => {
  it('completes a bare /join link with the console’s own origin, and leaves a full one alone', async () => {
    const { shareableLink } = await loadPeople()
    expect(shareableLink(`/join#${CODE}`, 'https://console.example.test')).toBe(LINK)
    expect(shareableLink(LINK, 'https://elsewhere.example.test')).toBe(LINK)
  })
})

describe('Settings › People and roles: Joined by request', () => {
  const LEE = { email: 'lee@example.com', by: 'alex@example.com', at: iso(-3 * HOUR), via: 'workspace_admin', group_changed: false, still_in_group: false }
  const JO = { email: 'jo@example.com', by: 'ops@example.com', at: iso(-30 * HOUR), via: 'platform_admin', group_changed: true, still_in_group: null }

  it('lists who joined by request, with where their access comes from', async () => {
    adminApi(roles({}), { on: false, set_by: '', set_at: '' }, [LEE, JO])
    const { WorkspacePeopleScreen } = await loadPeople()
    render(<WorkspacePeopleScreen />)
    const table = await screen.findByRole('table', { name: 'People who joined by request' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    expect(visible(rows[0]!)).toMatch(/lee@example\.com.*alex@example\.com.*Joined by request only/)
    expect(visible(rows[1]!)).toMatch(/jo@example\.com.*ops@example\.com \(platform admin\).*group changed/)
  })

  it('removes with the email in the body, never the path, and says the group is the other half', async () => {
    const calls = adminApi(roles({}), { on: false, set_by: '', set_at: '' }, [LEE])
    const { WorkspacePeopleScreen } = await loadPeople()
    render(<WorkspacePeopleScreen />)
    fireEvent.click(await screen.findByRole('button', { name: "Remove lee@example.com's membership by request" }))
    await screen.findByText(/still in example-labs's Google group/)
    const del = calls.filter((c) => c.method === 'DELETE')
    expect(del).toHaveLength(1)
    expect(del[0]!.url).toBe('/v1/workspace/members/person')
    expect(del[0]!.body).toEqual({ email: 'lee@example.com' })
    await screen.findByText(/Nobody holds a membership of example-labs by request/)
  })

  it('says an approval admits nobody while platform memberships are off', async () => {
    adminApi(roles({}), { on: false, set_by: '', set_at: '' }, [], false)
    const { WorkspacePeopleScreen } = await loadPeople()
    render(<WorkspacePeopleScreen />)
    await screen.findByText(/platform memberships are off/)
  })
})
