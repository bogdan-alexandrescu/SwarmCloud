// AUTOMATE › APPROVALS, REDESIGNED (#2417 A4 from proposal A, A5 from
// proposal B; lane AUTO-A4A5).
//
// WHAT EACH CASE HOLDS:
//   * Waiting is cards, oldest first, each with its expiry and its approvers
//     (`may_decide.deciders`), and a card opens the item's own page at
//     `/automate/approvals/<id>` (the cards mock-up, 12 November 2026);
//   * Decided, Expired and Superseded are drawn from what the read serves in
//     those states, and not drawn when it serves none;
//   * an empty inbox is one "Nothing waiting" line, with no card;
//   * the decision page offers Reject (never Deny) and Approve to a reader who
//     may approve, Reject asking for its reason once pressed; a reader who may
//     do neither sees no button and is told who decides, why not them in a
//     tooltip; a reader who may only reject sees only Reject;
//   * each decided state says its word on the strip: approved, rejected (its
//     reason quoted), expired, superseded (the head it moved to, the newer
//     request); a waiting item's strip carries the verbs, no word;
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
  it('counts the waiting items on the Waiting panel, oldest first, the reader’s or not', async () => {
    await mount('', routes(INBOX))
    const waiting = await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: count only the items the reader may decide (2), not every item (3).
    expect(visible(waiting.querySelector('.apc-panel-h'))).toMatch(/^Waiting\s*3, oldest first$/)
    // The cards mock-up draws no lead above the list.
    expect(screen.queryByTestId('apl-lead')).toBeNull()
  })

  it('lists waiting items oldest first with expiry and who may decide, each opening its own page', async () => {
    await mount('', routes(INBOX))
    const card = await screen.findByTestId('apl-waiting', undefined, WAIT)
    const rows = [...card.querySelectorAll<HTMLElement>('.apc-card[data-approval]')]
    expect(rows.map((r) => r.dataset.approval)).toEqual(['apr_oldest', 'run:run_4f0c', 'apr_newest'])
    // MUTATION: read who may decide off `approvers` when `deciders` is served.
    const fact = (r: HTMLElement, name: string) => visible([...r.querySelectorAll('dt')].find((t) => t.textContent === name)?.nextElementSibling ?? null)
    const who = (r: HTMLElement) => fact(r, 'Approvers')
    expect(who(rows[0]!)).toBe('the platform owner')
    expect(who(rows[1]!)).toBe('the platform owner · not you')
    expect(who(rows[2]!)).toBe('any member of this workspace')
    for (const r of rows) expect(fact(r, 'Expires')).toMatch(/^in \d+[hd]$/)
    // MUTATION: open the item in a pane beside the list again.
    expect(within(rows[1]!).getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/automate/approvals/run%3Arun_4f0c')
    expect(document.querySelector('[data-testid="ap-detail"]')).toBeNull()
    expect(screen.queryByTestId('apl-decided')).toBeNull()
    expect(screen.queryByTestId('apl-expired')).toBeNull()
    expect(screen.queryByTestId('apl-superseded')).toBeNull()
  })

  it('counts each kind on its chip from the whole inbox, and draws no chip for a kind with nothing waiting', async () => {
    await mount('', routes(INBOX))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    const chips = within(screen.getByRole('group', { name: 'Kind' }))
    expect(visible(chips.getByRole('button', { name: /^Every kind/ }))).toBe('Every kind · 3')
    expect(visible(chips.getByRole('button', { name: /^Merge/ }))).toBe('Merge · 1')
    // Visual QA automate-22: a zero chip is not drawn unless it is the chosen kind.
    expect(chips.queryByRole('button', { name: /^Spec/ })).toBeNull()
  })

  it('draws decided, expired and superseded items the read serves in their own panels, with who decided', async () => {
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
    expect(screen.getByTestId('apl-waiting').querySelectorAll('.apc-card')).toHaveLength(3)
    expect(visible(within(decided).getByRole('heading', { level: 2 }))).toBe('Decided')
    const r = decided.querySelector<HTMLElement>('.apc-card[data-approval="apr_r"]')!
    expect(visible(r)).toMatch(/Rejected by\s*teammate@example\.com/)
    expect(visible(decided.querySelector('.apc-card[data-approval="apr_a"]'))).toMatch(/Approved by\s*owner@example\.com/)
    expect(screen.getByTestId('apl-expired').querySelector('[data-approval="apr_x"]')).not.toBeNull()
    expect(visible(screen.getByTestId('apl-superseded').querySelector('[data-approval="apr_s"]'))).toContain('head moved')
  })

  it('is the Absent state when nothing waits: no lead, no table, the filter kept', async () => {
    await mount('', routes([]))
    // MUTATION: draw an empty Waiting grid over a measured zero.
    await screen.findByTestId('apl-nothing', undefined, WAIT)
    expect(visible(screen.getByTestId('apl-nothing'))).toMatch(/^Nothing waiting/)
    expect(document.querySelector('.apc-card')).toBeNull()
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
    expect(visible(document.body)).not.toMatch(/\bden(y|ied)\b/i)
    fireEvent.click(within(d).getByRole('button', { name: 'Reject' }))
    expect((within(d).getByRole('button', { name: 'Reject' }) as HTMLButtonElement).disabled).toBe(true)
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
    expect(visible(bar)).toContain('The platform owner only')
    // Why not them is the tooltip's.
    expect(within(bar).getByRole('tooltip', { hidden: true }).textContent).toBe('Only the platform owner may approve an item held NEEDS_OWNER.')
    // The evidence is still read in full.
    expect(visible(d)).toContain('of 10 checks passed')
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
      [{}, 'pending', /Approve merge at dbd00f0/],
      [{ state: 'approved', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1) }, 'Approved', /Approved\s*by owner@example\.com/],
      [{ state: 'rejected', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1), reason: 'split the workflow change out' }, 'Rejected', /Rejected\s*by owner@example\.com.*split the workflow change out/],
      [{ state: 'expired', expires_at: hoursFromNow(-1) }, 'Expired', /Expired.*nobody decided.*the pull request stays open/],
    ]
    for (const [over, word, said] of cases) {
      const view = await mount('approval=apr_oldest', routes(INBOX, detail(over)))
      const d = await page()
      const strip = await within(d).findByTestId('apd-decide', undefined, WAIT)
      // MUTATION: print the API's `pending` instead of the page's word, or a
      // word on a waiting item, which asks through its verbs.
      if (word === 'pending') expect(within(d).queryByTestId('ap-state')).toBeNull()
      else expect(within(strip).getByTestId('ap-state').textContent).toBe(word)
      expect(visible(strip)).toMatch(said)
      if (word !== 'pending') expect(within(d).queryByRole('button', { name: /Approve|Reject/ })).toBeNull()
      view.unmount()
    }
  })

  it('says a superseded merge moved head, and opens the newer request', async () => {
    const { go } = await mount(
      'approval=apr_oldest',
      routes(INBOX, detail({ state: 'superseded', head_sha_now: MOVED, superseded_by: { approval_id: 'apr_newer', head_sha: MOVED, at: hoursFromNow(-1) } }, { green_sha: MOVED })),
    )
    const d = await page()
    const bar = await within(d).findByTestId('apd-decide', undefined, WAIT)
    expect(visible(bar)).toContain('Superseded')
    expect(visible(bar)).toContain('the head moved')
    expect(visible(bar)).toContain('This approval was for dbd00f0')
    expect(visible(within(d).getByTestId('ad-head-now'))).toBe('Head moved to 7a3e19c')
    // MUTATION: leave out `superseded_by`, and the reader cannot find what to decide instead.
    const next = within(bar).getByRole('link', { name: 'Open the new approval ›' })
    expect(next.getAttribute('href')).toBe('/automate/approvals/apr_newer')
    fireEvent.click(next)
    expect(go).toHaveBeenCalledWith('automate/approvals?approval=apr_newer')
  })

  it('draws the check counts when served and, not served at a green head, says so on the green row; pairs each reason with its files', async () => {
    const view = await mount('approval=apr_oldest', routes(INBOX, detail()))
    let d = await page()
    await waitFor(() => expect(within(d).getByTestId('ad-check-counts')).toBeTruthy(), WAIT)
    expect(visible(within(d).getByTestId('ad-check-counts'))).toMatch(/^10\s*of 10 checks passed\s*head dbd00f0$/)
    expect(d.querySelector('.ad-cbar')!.getAttribute('aria-label')).toBe('10 passed, 0 failed, 0 pending')
    const held = within(d).getByRole('region', { name: 'Why it is held' })
    expect(visible(held)).toMatch(/\.github\/workflows\/unit-weights-refresh\.yml\s*workflows/)
    expect(visible(held)).toMatch(/scripts\/lib\/common\.sh\s*shared/)
    view.unmount()
    await mount('approval=apr_oldest', routes(INBOX, detail({}, { checks: undefined })))
    d = await page()
    await waitFor(() => expect(d.querySelector('li[data-check="ci"]')).not.toBeNull(), WAIT)
    // MUTATION: draw "0 passed" for counts nobody read.
    expect(visible(d)).not.toMatch(/\b0 passed/)
    // Visual QA automate-23: the head is green, so the green row says the
    // counts are not served, and no dash line sits above it.
    expect(within(d).queryByTestId('ad-check-counts')).toBeNull()
    expect(visible(d.querySelector('li[data-check="ci"]'))).toContain('per-check counts not served')
    expect(d.querySelector('.ad-cbar')).toBeNull()
  })

  it('leads back to the list it was opened from, its kind kept', async () => {
    await mount('approval=apr_oldest&kind=merge', routes(INBOX, detail()))
    await page()
    expect(screen.getByRole('link', { name: '‹ Approvals, Merge' }).getAttribute('href')).toBe('/automate/approvals?kind=merge')
  })
})

describe('the layout', () => {
  it('pins the decision strip on a desktop, and lays nothing out wider than a 390px phone', async () => {
    await mount('approval=apr_oldest', routes(INBOX, { approval: item('apr_oldest'), merge: merge() }))
    const d = await page()
    const bar = await within(d).findByTestId('apd-decide', undefined, WAIT)
    // MUTATION: drop the sticky rule, and the verbs scroll away under the evidence.
    expect(painted(bar, 'position', WIDE)).toBe('sticky')
    expect(painted(bar, 'position', PHONE)).toBe('static')
    // MUTATION: keep two evidence columns at 390px.
    const cols = d.querySelector<HTMLElement>('.apd-cols')!
    expect(painted(cols, 'grid-template-columns', WIDE)).toBe('repeat(2, minmax(0, 1fr))')
    expect(painted(cols, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })

  it('stacks the list’s cards on one column at 390px, each card’s action a 44px target', async () => {
    await mount('', routes(INBOX))
    const card = await screen.findByTestId('apl-waiting', undefined, WAIT)
    // MUTATION: keep three cards across at 390px, and the page scrolls sideways.
    expect(painted(card.querySelector<HTMLElement>('.apc-grid')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(card.querySelector<HTMLElement>('.apc-card-f > .c-btn')!, 'min-height', PHONE)).toBe('44px')
  })
})
