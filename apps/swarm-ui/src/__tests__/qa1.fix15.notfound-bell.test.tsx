/**
 * VISUAL QA PASS 1, LANE QA1-FIX-15 (part of #2596): the not-found page and
 * the bell.
 *
 *   admin-shell-11  `/admin/bogus` offered `/admin/counts`, four edits off, as
 *                   a raw path in mono. It offers the section's landing, by
 *                   its name: Admin › Pool limits, linking `/admin/limits`.
 *   C-05            `/work/running` offered Overview, and the rail lit
 *                   Overview, a page the reader did not open. It offers Work's
 *                   landing, Agents, and the rail lights Work.
 *   C-27            The bell said "14 need you" beside Overview's "Needs a
 *                   look 3" and Approvals' "Nothing waiting", with nothing
 *                   saying what the 14 were. The drop-down says where each
 *                   is answered: "2 in Work › Agents · 1 in Admin › People and
 *                   roles".
 *
 * MUTATIONS: drop the section-landing step from `nearestPath` (back to the
 * whole-path edit distance), render the offer as `{near}` again, or remove the
 * `nt-split` line -- each turns a case below red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { serve } from './repofixture'

const WAIT = { timeout: 4000 }

afterEach(() => {
  window.history.replaceState(null, '', '/')
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('admin-shell-11 and C-05: an unknown path under a section offers that section', () => {
  it("offers the section's landing, not a page a few letters away", async () => {
    const { nearestPath } = await import('../NotFound')
    // MUTATION: the old whole-path edit distance gives '/admin/counts' and '/overview'.
    expect(nearestPath('/admin/bogus')).toBe('/admin/limits')
    expect(nearestPath('/work/running')).toBe('/agents')
    expect(nearestPath('/capacity/nothing-here')).toBe('/capacity/pools')
    // A typo in the last segment still finds its page.
    expect(nearestPath('/admin/count')).toBe('/admin/counts')
    expect(nearestPath('/admin/tenant')).toBe('/admin/tenants')
    expect(nearestPath('/capacity/quota')).toBe('/capacity/accounts/quota')
  })

  it('names the offered page in the panel’s words and links its path', async () => {
    const { NotFound } = await import('../NotFound')
    render(<NotFound path="/admin/bogus" go={() => {}} />)
    const page = within(document.querySelector<HTMLElement>('.nf-page')!)
    // MUTATION: render `{near}` as the link text -- the name is gone and '/admin/limits' is back in mono.
    const near = page.getByRole('link', { name: 'Admin › Pool limits' })
    expect(near.getAttribute('href')).toBe('/admin/limits')
    expect(near.classList.contains('mono')).toBe(false)
    expect(page.queryByText('/admin/counts')).toBeNull()
  })

  it('names a page drawn under another by its own name', async () => {
    const { pageName } = await import('../NotFound')
    // MUTATION: name the parent -- 'Capacity › Accounts'.
    expect(pageName('/capacity/accounts/quota')).toBe('Capacity › Provider quota')
    expect(pageName('/overview')).toBe('Overview')
  })

  it('lights Work in the rail for /work/running, not Overview', async () => {
    window.history.replaceState(null, '', '/work/running')
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    await screen.findByRole('heading', { name: /No page at \/work\/running/ })
    const page = within(document.querySelector<HTMLElement>('.nf-page')!)
    expect(page.getByRole('link', { name: 'Work › Agents' }).getAttribute('href')).toBe('/agents')
    // MUTATION: offer '/overview' -- the rail lights ['overview'].
    const lit = [...document.querySelectorAll('.sk-ri[data-sec].is-on')].map((a) => a.getAttribute('data-sec'))
    expect(lit).toEqual(['work'])
  })
})

describe('C-27: the bell says where its count is answered', () => {
  const at = (m: number) => new Date(Date.now() - m * 60_000).toISOString()
  const item = (id: string, over: Record<string, unknown>) => ({
    id,
    group: 'task',
    needs_you: true,
    title: `item ${id}`,
    detail: null,
    href: '/agents',
    at: at(5),
    read: false,
    ...over,
  })

  it('splits the unread items that need you by the page that answers them', async () => {
    const { needsSplit } = await import('../Notifications')
    const split = needsSplit([
      item('p', { group: 'admin', href: '/admin/people', at: at(60) }),
      item('q1', { href: '/agents/live/task_one/questions' }),
      item('q2', { href: '/agents/live/task_two/questions' }),
      item('r', { group: 'approval', href: '/automate/approvals', read: true }),
      item('u', { needs_you: false, group: 'finished' }),
    ] as never)
    // MUTATION: count read items or updates too -- an Approvals part appears.
    expect(split).toBe('2 in Work › Agents · 1 in Admin › People and roles')
    expect(split).not.toContain('task_one')
  })

  it('draws the split under the drop-down’s head', async () => {
    const items = [
      item('p', { group: 'admin', href: '/admin/people', at: at(60) }),
      item('q1', { href: '/agents/live/task_one/questions' }),
    ]
    serve((m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/notifications'
        ? { status: 200, body: { items, unread_needs_you: 2, unread_updates: 0, admin_pending: 1, read_at: at(0) } }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { NotificationBell } = await import('../Notifications')
    render(<NotificationBell go={() => {}} />)
    await waitFor(() => expect(document.querySelector('button.nt-bell')).not.toBeNull(), WAIT)
    fireEvent.click(document.querySelector<HTMLButtonElement>('button.nt-bell')!)
    // MUTATION: remove the `nt-split` line.
    await waitFor(
      () => expect(document.querySelector('.nt-panel .nt-split')?.textContent).toBe('1 in Admin › People and roles · 1 in Work › Agents'),
      WAIT,
    )
  })
})
