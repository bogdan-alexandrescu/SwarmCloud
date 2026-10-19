/**
 * THE HEADER BELL AND THE ADMIN PILL (#1206, proposal A, lane NOTIF-UI).
 *
 * Built against the shape NOTIF-API serves -- `GET /v1/notifications`,
 * `POST /v1/notifications/read`, `POST /v1/notifications/read-all` -- with a
 * stubbed fetch, so this file says what the console does with each answer:
 *
 *   * the bell's count is `unread_needs_you`, and nothing at all when it is 0;
 *   * "Needs you" is drawn above "Updates", needs-you oldest first, updates
 *     newest first, whatever order the items arrived in;
 *   * a click on an item goes to its `href`, marks it read and closes;
 *   * "Mark all read" posts read-all and the count goes;
 *   * an empty feed says "Nothing needs you";
 *   * a route that answers 404 (an API without NOTIF-API) hides the bell
 *     rather than drawing an error in every page's header;
 *   * the Admin rail item carries `admin_pending` as a pill, for admins only;
 *   * a muted item that needs you is quiet, never invisible: "1 muted item
 *     needs you" under the list when `muted.needs_you` is 1, and nothing when
 *     it is 0 (user-profile.md §4.6, Q3).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const WAIT = { timeout: 4000 }
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()

function note(id: string, over: Json = {}): Json {
  return {
    id,
    group: 'task',
    needs_you: false,
    title: `item ${id}`,
    detail: null,
    href: '/agents',
    at: minutesAgo(5),
    read: false,
    ...over,
  }
}

function feed(items: Json[], over: Json = {}): Json {
  const unread = (needs: boolean) => items.filter((i) => i.needs_you === needs && i.read !== true).length
  return {
    items,
    unread_needs_you: unread(true),
    unread_updates: unread(false),
    admin_pending: 0,
    read_at: minutesAgo(0),
    ...over,
  }
}

/** The notification routes, answering `body`; anything else is the fixture's 404. */
function notifications(body: () => Json, extra: Handler = () => null): Handler {
  return (m, url, b) => {
    const hit = extra(m, url, b)
    if (hit !== null) return hit
    const path = url.split('?')[0]
    if (m === 'GET' && path === '/v1/notifications') return { status: 200, body: body() }
    if (m === 'POST' && (path === '/v1/notifications/read' || path === '/v1/notifications/read-all')) return { status: 200, body: { ok: true } }
    return null
  }
}

async function mountBell(handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { NotificationBell } = await import('../Notifications')
  const go = vi.fn()
  const utils = render(<NotificationBell go={go} />)
  return { ...utils, calls, go }
}

const bell = () => document.querySelector<HTMLButtonElement>('button.nt-bell')
const panel = () => document.querySelector<HTMLElement>('.nt-panel')
const posted = (calls: { method: string; url: string; body: unknown }[], path: string) =>
  calls.filter((c) => c.method === 'POST' && c.url.split('?')[0] === path)

async function openPanel() {
  await waitFor(() => expect(bell(), 'no bell').not.toBeNull(), WAIT)
  fireEvent.click(bell()!)
  await waitFor(() => expect(panel(), 'the bell opened nothing').not.toBeNull(), WAIT)
  return panel()!
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the bell', () => {
  it('counts only the unread items that need you', async () => {
    const items = [
      note('a', { needs_you: true, group: 'admin' }),
      note('b', { needs_you: true, group: 'approval' }),
      note('c', { needs_you: true, group: 'task', read: true }),
      note('d', { group: 'finished' }),
    ]
    await mountBell(notifications(() => feed(items)))
    // MUTATION: count `unread_updates` too, or every item -- the badge says 3+.
    await waitFor(() => expect(bell()?.querySelector('.nt-count')?.textContent).toBe('2'), WAIT)
    expect(bell()!.getAttribute('aria-label')).toContain('2 need you')
    // The count is announced, not only drawn.
    expect(document.querySelector('[aria-live="polite"]')?.textContent ?? '').toContain('2')
  })

  it('draws no count when nothing needs you', async () => {
    await mountBell(notifications(() => feed([note('d', { group: 'finished' })])))
    await waitFor(() => expect(bell()).not.toBeNull(), WAIT)
    // MUTATION: draw the badge for a measured 0.
    expect(bell()!.querySelector('.nt-count')).toBeNull()
  })

  it('is hidden, not an error, when the route answers 404', async () => {
    const { calls } = await mountBell(() => null)
    await waitFor(() => expect(calls.some((c) => c.url.startsWith('/v1/notifications'))).toBe(true), WAIT)
    // Let the 404 land and render.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    // MUTATION: draw the bell (or an error) for a 404.
    expect(bell()).toBeNull()
    expect(document.body.textContent ?? '').not.toMatch(/not found|error/i)
  })
})

describe('the drop-down', () => {
  it('draws Needs you above Updates, needs-you oldest first, updates newest first', async () => {
    // Served deliberately out of order: the console's order is its own.
    const items = [
      note('u-old', { title: 'Older update', at: minutesAgo(90), group: 'finished' }),
      note('n-new', { needs_you: true, title: 'Newer ask', at: minutesAgo(3), group: 'approval', href: '/approvals' }),
      note('u-new', { title: 'Newer update', at: minutesAgo(2), group: 'finished' }),
      note('n-old', { needs_you: true, title: 'Older ask', at: minutesAgo(60), group: 'admin', href: '/admin/people' }),
    ]
    await mountBell(notifications(() => feed(items)))
    const p = await openPanel()
    expect(within(p).getByText('Notifications')).toBeTruthy()
    expect(visible(p.querySelector('.nt-chip'))).toBe('2 need you')
    const groups = [...p.querySelectorAll('.nt-group > h3')].map((h) => h.textContent)
    // MUTATION: draw the groups the other way round.
    expect(groups).toEqual(['Needs you', 'Updates'])
    const titles = [...p.querySelectorAll('.nt-item .nt-title')].map((t) => t.textContent)
    // MUTATION: keep the served order, or sort both groups the same way.
    expect(titles).toEqual(['Older ask', 'Newer ask', 'Newer update', 'Older update'])
    // Each item says where it leads, from its href.
    const first = p.querySelector('.nt-item')!
    expect(first.classList.contains('is-unread')).toBe(true)
    expect(visible(first.querySelector('.nt-dest'))).toMatch(/Admin.*People/)
    expect(p.textContent).toContain('Read items clear after 30 days')
  })

  it('goes to an item’s page, marks it read and closes when the item is clicked', async () => {
    const items = [note('n1', { needs_you: true, group: 'admin', title: 'Workspace request', href: '/admin/people' })]
    const { calls, go } = await mountBell(notifications(() => feed(items)))
    const p = await openPanel()
    fireEvent.click(within(p).getByText('Workspace request'))
    // MUTATION: drop the navigation, the read or the close.
    expect(go).toHaveBeenCalledWith('/admin/people')
    await waitFor(() => expect(posted(calls, '/v1/notifications/read').map((c) => c.body)).toEqual([{ ids: ['n1'] }]), WAIT)
    expect(panel()).toBeNull()
  })

  it('marks one item read from its own control, without navigating', async () => {
    const items = [note('n1', { needs_you: true, group: 'admin', title: 'Workspace request' })]
    const { calls, go } = await mountBell(notifications(() => feed(items)))
    const p = await openPanel()
    fireEvent.click(within(p).getByRole('button', { name: /Mark read/ }))
    await waitFor(() => expect(posted(calls, '/v1/notifications/read').map((c) => c.body)).toEqual([{ ids: ['n1'] }]), WAIT)
    expect(go).not.toHaveBeenCalled()
  })

  it('marks everything read and the count goes', async () => {
    let read = false
    const items = () => [note('n1', { needs_you: true, group: 'admin', read }), note('n2', { needs_you: true, group: 'task', read })]
    const { calls } = await mountBell(
      notifications(() => feed(items()), (m, url) => {
        if (m === 'POST' && url === '/v1/notifications/read-all') read = true
        return null
      }),
    )
    await waitFor(() => expect(bell()?.querySelector('.nt-count')?.textContent).toBe('2'), WAIT)
    const p = await openPanel()
    fireEvent.click(within(p).getByRole('button', { name: 'Mark all read' }))
    // MUTATION: post nothing, or leave the count standing.
    await waitFor(() => expect(posted(calls, '/v1/notifications/read-all')).toHaveLength(1), WAIT)
    await waitFor(() => expect(bell()?.querySelector('.nt-count')).toBeNull(), WAIT)
  })

  it('says “Nothing needs you” when the feed is empty', async () => {
    await mountBell(notifications(() => feed([])))
    const p = await openPanel()
    // MUTATION: draw empty groups instead.
    expect(within(p).getByText('Nothing needs you')).toBeTruthy()
    expect(within(p).getByText('Approvals, sign-ins and finished work will show up here.')).toBeTruthy()
    expect(p.querySelector('.nt-group')).toBeNull()
  })

  it('closes on Escape and on a click outside it, and gives focus back to the bell', async () => {
    await mountBell(notifications(() => feed([note('d')])))
    await openPanel()
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(panel()).toBeNull(), WAIT)
    expect(document.activeElement).toBe(bell())
    fireEvent.click(bell()!)
    await waitFor(() => expect(panel()).not.toBeNull(), WAIT)
    fireEvent.mouseDown(document.body)
    await waitFor(() => expect(panel()).toBeNull(), WAIT)
  })
})

describe('the muted line (Q3)', () => {
  const mutedLine = () => document.querySelector<HTMLElement>('.nt-panel .nt-muted')

  it('says "1 muted item needs you" when muted.needs_you is 1, and leads to Settings › Profile', async () => {
    // MUTATION: drop the line, or count muted updates in it.
    const { go } = await mountBell(notifications(() => feed([note('d')], { muted: { needs_you: 1, updates: 4 } })))
    const p = await openPanel()
    const line = mutedLine()
    expect(line, 'a muted item that needs you is invisible').not.toBeNull()
    expect(line!.textContent).toBe('1 muted item needs you · change in Settings › Profile')
    const link = within(p).getByRole('link', { name: 'change in Settings › Profile' })
    expect(link.getAttribute('href')).toBe('/settings/profile')
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith('/settings/profile')
  })

  it('counts several in the plural, and still says it over an empty list', async () => {
    await mountBell(notifications(() => feed([], { muted: { needs_you: 2, updates: 0 } })))
    await openPanel()
    expect(mutedLine()?.textContent).toContain('2 muted items need you')
  })

  it('says nothing when muted.needs_you is 0, or when the API sends no muted counts', async () => {
    await mountBell(notifications(() => feed([note('d')], { muted: { needs_you: 0, updates: 12 } })))
    const p = await openPanel()
    expect(mutedLine()).toBeNull()
    expect(p.textContent).not.toContain('muted')
  })

  it('says nothing for an API that predates muting', async () => {
    await mountBell(notifications(() => feed([note('d')])))
    await openPanel()
    expect(mutedLine()).toBeNull()
  })
})

describe('the bell in the shell', () => {
  it('sits at the spine’s foot on a section page, with no header bar to hold it (V178)', async () => {
    serve(notifications(() => feed([note('a', { needs_you: true })])))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    window.history.replaceState(null, '', '/capacity/pools')
    const { App } = await import('../App')
    render(<App />)
    // MUTATION: drop the bell from the frame.
    await waitFor(() => expect(document.querySelector('.sk-spine .sk-bell button.nt-bell')).not.toBeNull(), WAIT)
    expect(document.querySelector('.ctl-bar')).toBeNull()
  })
})

describe('the Admin rail pill', () => {
  const ME = (isAdmin: boolean) => ({
    tenant: { tenant_id: 'eng', display_name: 'Engineering' },
    principal: { email: 'ana.b@example.com', domain: 'example.com', groups: [], is_admin: isAdmin },
    environment: 'dev',
    environment_declared: true,
  })

  async function shell(isAdmin: boolean) {
    serve(
      notifications(
        () => feed([], { admin_pending: isAdmin ? 2 : 0 }),
        (m, url) => (m === 'GET' && url.startsWith('/v1/tenants/me') ? { status: 200, body: ME(isAdmin) } : null),
      ),
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="overview" tab="" title="Overview" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
  }

  const admin = () => document.querySelector<HTMLAnchorElement>('.sk-spine a[data-sec="admin"]')!

  it('shows the requests waiting on any admin', async () => {
    await shell(true)
    // MUTATION: drop the pill from the Admin rail item.
    await waitFor(() => expect(admin().querySelector('.sk-apill')?.textContent).toBe('2'), WAIT)
    expect(admin().querySelector('.sk-apill')?.getAttribute('aria-label')).toContain('2')
  })

  it('is never drawn for a non-admin', async () => {
    await shell(false)
    await waitFor(() => expect(admin().querySelector('.sk-lkd')).not.toBeNull(), WAIT)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(admin().querySelector('.sk-apill')).toBeNull()
  })

  it('is not drawn for a non-admin even when the feed carries a count', async () => {
    // A non-admin whose feed (wrongly) carried a count still gets no pill:
    // the pill is gated on the session read, not on the number alone.
    serve(
      notifications(
        () => feed([], { admin_pending: 4 }),
        (m, url) => (m === 'GET' && url.startsWith('/v1/tenants/me') ? { status: 200, body: ME(false) } : null),
      ),
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="overview" tab="" title="Overview" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    await waitFor(() => expect(admin().querySelector('.sk-lkd')).not.toBeNull(), WAIT)
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    // MUTATION: gate the pill on `admin_pending > 0` alone.
    expect(admin().querySelector('.sk-apill')).toBeNull()
  })
})

describe('the phone', () => {
  it('opens a full-screen Notifications page with Mark all read', async () => {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('max-width: 759px'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
    await mountBell(notifications(() => feed([note('n1', { needs_you: true })])))
    const p = await openPanel()
    // MUTATION: open the anchored drop-down on a phone.
    expect(p.classList.contains('is-page')).toBe(true)
    expect(p.getAttribute('role')).toBe('dialog')
    expect(within(p).getByRole('button', { name: 'Mark all read' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Close notifications/ })).toBeTruthy()
  })
})

// QA14-UI (owner QA 2026-10-13): "Your agent asked 1 question" named only the
// task id. The item shows the first open question's text, read from
// `GET /v1/tasks/{id}/questions`, and the feed's detail names the step.
// MUTATION: drop the question read from the bell's item.
describe('an agent-questions item', () => {
  const asked = (question: string, answered = false) => ({
    question,
    options: [],
    recommended: null,
    context: null,
    answer: answered ? { question_index: 0, question, choice: 'a', text: null, by: 'a@x', at: minutesAgo(1), history: [] } : null,
  })

  it('shows the first open question under its title', async () => {
    const items = [
      note('task:questions:task_q', {
        needs_you: true,
        title: 'Your agent asked 2 questions',
        detail: 'fix-heartbeat · Task task_q',
        href: '/agents/recent/task_q#questions',
      }),
    ]
    const extra: Handler = (m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/tasks/task_q/questions'
        ? {
            status: 200,
            body: {
              task_id: 'task_q',
              count: 2,
              status: 'ok',
              questions: [asked('Which branch should I base it on?', true), asked('Should the retry cap be 3 or 5?')],
              redacted: false,
              redaction_count: 0,
            },
          }
        : null
    await mountBell(notifications(() => feed(items), extra))
    const p = await openPanel()
    await waitFor(() => expect(p.querySelector('.nt-question')?.textContent).toContain('Should the retry cap be 3 or 5?'), WAIT)
    expect(visible(p.querySelector('.nt-detail'))).toContain('fix-heartbeat')
  })

  it('cuts a long question on a word, and says nothing for an item that is no question', async () => {
    const { firstQuestion, QUESTION_CHARS } = await import('../Notifications')
    const long = 'Should I '.concat('rewrite the scheduler tick loop '.repeat(6), 'now?')
    const read = { task_id: 't', count: 1, status: 'ok' as const, questions: [asked(long)], redacted: false, redaction_count: 0 }
    const cut = firstQuestion(read)!
    expect(cut.length).toBeLessThanOrEqual(QUESTION_CHARS)
    expect(cut.endsWith('…')).toBe(true)
    expect(long.startsWith(cut.slice(0, -1))).toBe(true)
    expect(firstQuestion({ ...read, questions: [] })).toBeNull()
  })
})
