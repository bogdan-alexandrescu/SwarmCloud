// AUTOMATE › APPROVALS, REDESIGNED (#2417 A4 from proposal A, A5 from
// proposal B; lane AUTO-A4A5).
//
// WHAT EACH CASE HOLDS:
//   * the list's lead counts every waiting item -- not only the reader's --
//     and names the oldest item's expiry;
//   * Waiting is oldest first as the route serves it, each row with its
//     expiry and who may decide (`may_decide.deciders`), and a row opens the
//     item's own page at `/automate/approvals/<id>`;
//   * Decided and Expired-or-superseded are drawn from what the read serves in
//     those states, and not drawn when it serves none;
//   * an empty inbox is the Absent state, with no lead and no table;
//   * the decision page offers Reject (never Deny) and Approve to a reader who
//     may approve, with the reason box beside them; a reader who may do
//     neither sees no button and is told who decides and why not them; a
//     reader who may only reject sees only Reject;
//   * each state says its word: waiting, approved, rejected (its reason
//     quoted), expired, superseded (the head it moved to, the newer request);
//   * a merge's check counts are drawn when served and a dash when not, and
//     a hold's reasons are paired with the files they matched;
//   * at 390px nothing is laid out wider than the screen.
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

// Heads built at runtime: a 40-character hex literal is not something this
// file needs to carry.
const HEAD = 'dbd00f0' + '6'.repeat(33)
const MOVED = '7a3e19c' + '0'.repeat(33)

const MAY = { approve: true, reject: true, why: null, deciders: ['owner_only'] }
const MAY_NOT = {
  approve: false,
  reject: false,
  why: 'Only the platform owner may approve an item held NEEDS_OWNER.',
  deciders: ['owner_only'],
}

function item(id: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'merge',
    subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#2364', schedule_id: 'sch_a', firing_id: 'sch_a:1' },
    digest: { head_sha: HEAD, verdict: 'MERGE' },
    summary: `example-org/example-api#2364: pull request #2392 is green (${id})`,
    approvers: 'owner_only',
    state: 'pending',
    requested_at: hoursFromNow(-6),
    expires_at: hoursFromNow(66),
    may_decide: MAY,
    ...over,
  }
}

const INBOX = [
  item('apr_oldest', { requested_at: hoursFromNow(-30), expires_at: hoursFromNow(42) }),
  item('run:run_4f0c', { kind: 'hold', approvers: 'members', may_decide: MAY_NOT, hold: { code: 'NEEDS_OWNER', approvers: 'owner_only' } }),
  item('apr_newest', { kind: 'run', approvers: 'members', may_decide: { approve: true, reject: true, why: null, deciders: ['members'] } }),
]

function merge(over: Json = {}): Json {
  return {
    green_sha: HEAD,
    pull_request: { number: 2392, url: 'https://github.com/example-org/example-api/pull/2392' },
    requirements_met: true,
    requirements_unmet: [],
    files_checked_head: HEAD,
    approval_hold: {
      code: 'NEEDS_OWNER',
      approvers: 'owner_only',
      reasons: ['workflows', 'shared'],
      matched: ['.github/workflows/unit-weights-refresh.yml', 'scripts/lib/common.sh'],
      reason_files: { workflows: ['.github/workflows/unit-weights-refresh.yml'], shared: ['scripts/lib/common.sh'] },
    },
    checks: { passed: 10, failed: 0, pending: 0 },
    ...over,
  }
}

function routes(inbox: Json[], detail: Json | null = null, extra: Handler = () => null): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: inbox, tenant_id: 'eng' } }
    if (m === 'GET' && path.startsWith('/v1/approvals/') && detail !== null) return { status: 200, body: detail }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const page = () =>
  waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)

const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) =>
  calls.filter((c) => c.method === method && c.url.split('?')[0] === path)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Approvals list (A4)', () => {
  it('counts every waiting item in its lead, the reader’s or not, and names the oldest one’s expiry', async () => {
    await mount('', routes(INBOX))
    const lead = await screen.findByTestId('apl-lead', undefined, WAIT)
    // MUTATION: count only the items the reader may decide (2), not every item (3).
    expect(visible(within(lead).getByTestId('apl-count'))).toBe('3 items wait for a decision.')
    // MUTATION: name the newest item, or leave its expiry out.
    const oldest = visible(within(lead).getByTestId('apl-oldest'))
    expect(oldest).toContain('(apr_oldest)')
    expect(oldest).toMatch(/expires in 4[12]h/)
    expect(within(lead).getByRole('link', { name: 'Open the oldest' }).getAttribute('href')).toBe('/automate/approvals/apr_oldest')
  })

  it('lists waiting items oldest first with expiry and who may decide, each opening its own page', async () => {
    await mount('', routes(INBOX))
    const card = await screen.findByTestId('apl-waiting', undefined, WAIT)
    const rows = [...card.querySelectorAll<HTMLElement>('tbody tr[data-approval]')]
    expect(rows.map((r) => r.dataset.approval)).toEqual(['apr_oldest', 'run:run_4f0c', 'apr_newest'])
    const heads = [...card.querySelectorAll('thead th')].map((th) => th.textContent)
    expect(heads).toEqual(['Kind', 'What', 'Asked', 'Expires in', 'Who may decide'])
    // MUTATION: read who may decide off `approvers` when `deciders` is served.
    const who = (r: HTMLElement) => visible(r.querySelector('td[data-l="Who may decide"]'))
    expect(who(rows[0]!)).toMatch(/^the platform owner\s*you may decide$/)
    expect(who(rows[1]!)).toMatch(/^the platform owner\s*not you$/)
    expect(who(rows[2]!)).toMatch(/^any member of this tenant\s*you may decide$/)
    expect(visible(rows[1]!)).toContain('held for the platform owner')
    // MUTATION: open the item in a pane beside the list again.
    expect(rows[1]!.querySelector('a')!.getAttribute('href')).toBe('/automate/approvals/run%3Arun_4f0c')
    expect(document.querySelector('[data-testid="ap-detail"]')).toBeNull()
    expect(screen.queryByTestId('apl-decided')).toBeNull()
    expect(screen.queryByTestId('apl-ended')).toBeNull()
  })

  it('counts each kind on its chip from the whole inbox, and only the chosen kind on a filtered read', async () => {
    await mount('', routes(INBOX))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    const chips = within(screen.getByRole('group', { name: 'Kind' }))
    expect(visible(chips.getByRole('button', { name: /^Every kind/ }))).toBe('Every kind · 3')
    expect(visible(chips.getByRole('button', { name: /^Merge/ }))).toBe('Merge · 1')
    expect(visible(chips.getByRole('button', { name: /^Spec/ }))).toBe('Spec · 0')
  })

  it('draws decided and expired items the read serves in their own cards, with who decided and the reason', async () => {
    const inbox = [
      ...INBOX,
      item('apr_r', { state: 'rejected', decided_by: 'teammate@example.com', decided_at: hoursFromNow(-3), reason: 'split the workflow change out' }),
      item('apr_a', { state: 'approved', decided_by: 'owner@example.com', decided_at: hoursFromNow(-4) }),
      item('apr_x', { state: 'expired', expires_at: hoursFromNow(-1) }),
      item('apr_s', { state: 'superseded', decided_at: hoursFromNow(-2) }),
    ]
    await mount('', routes(inbox))
    const decided = await screen.findByTestId('apl-decided', undefined, WAIT)
    // MUTATION: draw a decided item among the waiting ones.
    expect([...screen.getByTestId('apl-waiting').querySelectorAll('tbody tr')]).toHaveLength(3)
    expect(visible(within(decided).getByRole('heading'))).toBe('Decided · 2')
    const r = decided.querySelector<HTMLElement>('tr[data-approval="apr_r"]')!
    expect(visible(r.querySelector('td[data-l="Decision"]'))).toBe('rejected')
    expect(visible(r)).toContain('teammate@example.com')
    expect(visible(r)).toContain('split the workflow change out')
    const ended = screen.getByTestId('apl-ended')
    expect(visible(within(ended).getByRole('heading'))).toBe('Expired or superseded · 2')
    expect(visible(ended.querySelector('tr[data-approval="apr_x"] td[data-l="Ended"]'))).toBe('expired')
    expect(visible(ended.querySelector('tr[data-approval="apr_s"] td[data-l="Ended"]'))).toBe('superseded')
    // The lead counts the waiting items only.
    expect(visible(screen.getByTestId('apl-count'))).toBe('3 items wait for a decision.')
  })

  it('is the Absent state when nothing waits: no lead, no table, the filter kept', async () => {
    await mount('', routes([]))
    // MUTATION: draw the lead or an empty Waiting table over a measured zero.
    await screen.findByText('Nothing waiting', undefined, WAIT)
    expect(screen.queryByTestId('apl-lead')).toBeNull()
    expect(document.querySelector('table')).toBeNull()
    expect(screen.getByRole('group', { name: 'Kind' })).toBeTruthy()
  })
})

describe('one approval’s decision page (A5)', () => {
  const detail = (over: Json = {}, m: Json = {}) => ({ approval: item('apr_oldest', over), merge: merge(m) })

  it('offers Reject and Approve to an approver, the reason box beside them, and never says Deny', async () => {
    const { calls } = await mount(
      'approval=apr_oldest',
      routes(INBOX, detail(), (m, url) => (m === 'POST' && url === '/v1/approvals/apr_oldest:reject' ? { status: 200, body: { approval: { approval_id: 'apr_oldest', state: 'rejected' } } } : null)),
    )
    const d = await page()
    await within(d).findByRole('button', { name: 'Approve merge at dbd00f0' }, WAIT)
    // MUTATION: the old "Reject…" that opens a form, or B's "Deny".
    const reject = within(d).getByRole('button', { name: 'Reject' }) as HTMLButtonElement
    expect(visible(document.body)).not.toMatch(/\bden(y|ied)\b/i)
    expect(reject.disabled).toBe(true)
    fireEvent.change(within(d).getByRole('textbox', { name: /Reason/ }), { target: { value: 'split the workflow change out' } })
    fireEvent.click(within(d).getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/apr_oldest:reject')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/approvals/apr_oldest:reject')[0]!.body).toEqual({ reason: 'split the workflow change out' })
    // The page is the item's own: the list is not drawn beside it.
    expect(screen.queryByTestId('apl-waiting')).toBeNull()
  })

  it('shows a reader who may not decide no button at all, and who decides and why not them (A5f)', async () => {
    await mount('approval=apr_oldest', routes(INBOX, detail({ may_decide: MAY_NOT })))
    const d = await page()
    const bar = await within(d).findByTestId('ap-no-decide', undefined, WAIT)
    // MUTATION: draw Reject for everyone, as the old page did.
    expect(within(d).queryByRole('button', { name: /Approve|Reject/ })).toBeNull()
    expect(within(d).queryByRole('textbox')).toBeNull()
    expect(visible(bar)).toContain('Only the platform owner decides this')
    expect(visible(bar)).toContain('Only the platform owner may approve an item held NEEDS_OWNER.')
    // The evidence is still read in full.
    expect(visible(d)).toContain('10 passed')
  })

  it('offers only Reject to a reader the server lets reject but not approve', async () => {
    await mount('approval=apr_oldest', routes(INBOX, detail({ may_decide: { approve: false, reject: true, why: 'You last edited the gate.', deciders: ['dave@example.com'] } })))
    const d = await page()
    await within(d).findByRole('button', { name: 'Reject' }, WAIT)
    expect(within(d).queryByRole('button', { name: /Approve/ })).toBeNull()
    expect(visible(d)).toContain('You last edited the gate.')
  })

  it('says each state in its word: waiting, approved, rejected with its reason, expired, superseded', async () => {
    const cases: [Json, string, RegExp][] = [
      [{}, 'waiting', /You may decide this/],
      [{ state: 'approved', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1) }, 'approved', /Approved by owner@example\.com/],
      [{ state: 'rejected', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1), reason: 'split the workflow change out' }, 'rejected', /Rejected by owner@example\.com.*split the workflow change out/],
      [{ state: 'expired', expires_at: hoursFromNow(-1) }, 'expired', /Expired .*nobody decided it in time.*The pull request stays open/],
    ]
    for (const [over, word, said] of cases) {
      const view = await mount('approval=apr_oldest', routes(INBOX, detail(over)))
      const d = await page()
      // MUTATION: print the API's `pending` instead of the page's word.
      await waitFor(() => expect(within(d).getByTestId('ap-state').textContent).toBe(word), WAIT)
      expect(visible(d)).toMatch(said)
      if (word !== 'waiting') expect(within(d).queryByRole('button', { name: /Approve|Reject/ })).toBeNull()
      view.unmount()
    }
  })

  it('says a superseded merge moved head, and opens the newer request', async () => {
    const { go } = await mount(
      'approval=apr_oldest',
      routes(INBOX, detail({ state: 'superseded', head_sha_now: MOVED, superseded_by: { approval_id: 'apr_newer', head_sha: MOVED, at: hoursFromNow(-1) } }, { green_sha: MOVED })),
    )
    const d = await page()
    const bar = await within(d).findByTestId('ap-decided', undefined, WAIT)
    expect(visible(bar)).toContain('Superseded: the head moved to 7a3e19c.')
    expect(visible(bar)).toContain('This approval was for dbd00f0')
    expect(within(d).getByTestId('ad-head-now').textContent).toBe('now 7a3e19c')
    // MUTATION: leave out `superseded_by`, and the reader cannot find what to decide instead.
    const next = within(bar).getByRole('link', { name: 'Open the newer request ›' })
    expect(next.getAttribute('href')).toBe('/automate/approvals/apr_newer')
    fireEvent.click(next)
    expect(go).toHaveBeenCalledWith('automate/approvals?approval=apr_newer')
  })

  it('draws the check counts when served and a dash with its reason when not, and pairs each reason with its files', async () => {
    const view = await mount('approval=apr_oldest', routes(INBOX, detail()))
    let d = await page()
    await waitFor(() => expect(within(d).getByTestId('ad-check-counts')).toBeTruthy(), WAIT)
    expect(visible(within(d).getByTestId('ad-check-counts'))).toBe('10 passed · 0 failed · 0 pending at dbd00f0')
    expect(d.querySelector('.ad-cbar')!.getAttribute('aria-label')).toBe('10 passed, 0 failed, 0 pending')
    const held = within(d).getByRole('region', { name: 'Why it is held' })
    expect(visible(held)).toMatch(/workflows\s*\.github\/workflows\/unit-weights-refresh\.yml/)
    expect(visible(held)).toMatch(/shared\s*scripts\/lib\/common\.sh/)
    view.unmount()
    await mount('approval=apr_oldest', routes(INBOX, detail({}, { checks: undefined })))
    d = await page()
    await waitFor(() => expect(within(d).getByTestId('ad-check-counts')).toBeTruthy(), WAIT)
    // MUTATION: draw "0 passed" for counts nobody read.
    const unread = visible(within(d).getByTestId('ad-check-counts'))
    expect(unread).not.toMatch(/\b0 passed/)
    expect(unread).toContain('no check counts are served')
    expect(d.querySelector('.ad-cbar')).toBeNull()
  })

  it('leads back to the list it was opened from, its kind kept', async () => {
    await mount('approval=apr_oldest&kind=merge', routes(INBOX, detail()))
    await page()
    expect(screen.getByRole('link', { name: '‹ Approvals, Merge' }).getAttribute('href')).toBe('/automate/approvals?kind=merge')
  })
})

describe('the layout', () => {
  it('pins the decision bar on a desktop, and lays nothing out wider than a 390px phone', async () => {
    await mount('approval=apr_oldest', routes(INBOX, { approval: item('apr_oldest'), merge: merge() }))
    const d = await page()
    const bar = await within(d).findByTestId('ad-decide', undefined, WAIT)
    // MUTATION: drop the sticky rule, and the verbs scroll away under the evidence.
    expect(painted(bar, 'position', WIDE)).toBe('sticky')
    expect(painted(bar, 'position', PHONE)).toBe('static')
    // MUTATION: keep two evidence columns at 390px.
    expect(painted(d.querySelector<HTMLElement>('.ad-ev')!, 'grid-template-columns', WIDE)).toBe('minmax(0, 1fr) minmax(0, 1fr)')
    expect(painted(d.querySelector<HTMLElement>('.ad-ev')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })

  it('stacks the list’s rows at 390px, each cell under its column’s name, and the row’s link a 44px target', async () => {
    await mount('', routes(INBOX))
    const card = await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: leave the five-column table at 390px, and the page scrolls sideways.
    expect(painted(card.querySelector<HTMLElement>('thead')!, 'display', PHONE)).toBe('none')
    expect(painted(card.querySelector<HTMLElement>('tbody tr')!, 'display', PHONE)).toBe('block')
    expect(painted(card.querySelector<HTMLElement>('td')!, 'content', PHONE, 'before')).toBe('attr(data-l)')
    expect(painted(card.querySelector<HTMLElement>('.apl-open')!, 'min-height', PHONE)).toBe('44px')
  })
})
