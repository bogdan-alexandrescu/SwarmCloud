// AUTOMATE › APPROVALS AS CARDS (the picked mock-up, cards direction).
//
// WHAT EACH CASE HOLDS:
//   * the list is cards, not a table: Waiting oldest first, each card with
//     its expiry and its approvers; Decided with by whom; Expired and
//     Superseded on their own; the kind filter above them;
//   * one approval is a card page in every state -- pending, approved,
//     rejected, expired, superseded when the head moved, and as seen by
//     someone who may not decide -- and each state draws its own head and
//     its own actions;
//   * a reader who may not decide sees no decision button, and the reason is
//     a tooltip, not a paragraph;
//   * expiry and approvers are on the page in every state;
//   * at 390px nothing is laid out wider than the screen;
//   * the page's visible words stay under the cards' ceiling.
//
// The worked example is a merge: green at its head, ten of ten checks passed,
// held for the owner because it touches a workflow file and a shared script,
// approvers owner only, asked for by a schedule's firing, expiring in a few
// days. Invented fixture data only; nothing is token-shaped.

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

// Heads built at runtime, so no 40-character hex literal sits in the file.
const HEAD = '3e7a91c' + '4'.repeat(33)
const MOVED = 'b04c2d8' + '1'.repeat(33)

const MAY = { approve: true, reject: true, why: null, deciders: ['owner_only'] }
const MAY_NOT = { approve: false, reject: false, why: 'Only the platform owner may approve an item held for the owner.', deciders: ['owner_only'] }

function item(id: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'merge',
    subject: { run_id: 'run_release', issue: 'example-org/release-tools#480', schedule_id: 'sch_review', firing_id: 'sch_review:20261019' },
    digest: { head_sha: HEAD, verdict: 'MERGE' },
    summary: `Merge pull request #482 (${id})`,
    approvers: 'owner_only',
    state: 'pending',
    requested_at: hoursFromNow(-3),
    expires_at: hoursFromNow(69),
    may_decide: MAY,
    ...over,
  }
}

function merge(over: Json = {}): Json {
  return {
    green_sha: HEAD,
    pull_request: { number: 482, url: 'https://example.invalid/example-org/release-tools/pull/482' },
    requirements_met: true,
    requirements_unmet: [],
    files_checked_head: HEAD,
    approval_hold: {
      code: 'NEEDS_OWNER',
      approvers: 'owner_only',
      reasons: ['workflows', 'shared'],
      matched: ['.github/workflows/release.yml', 'scripts/lib/upload.sh'],
      reason_files: { workflows: ['.github/workflows/release.yml'], shared: ['scripts/lib/upload.sh'] },
    },
    checks: { passed: 10, failed: 0, pending: 0 },
    ...over,
  }
}

const INBOX = [
  item('apr_newest', { kind: 'run', requested_at: hoursFromNow(-1), approvers: 'members', may_decide: { approve: true, reject: true, why: null, deciders: ['members'] } }),
  item('apr_oldest', { requested_at: hoursFromNow(-30), expires_at: hoursFromNow(42) }),
  item('apr_mid', { kind: 'plan', requested_at: hoursFromNow(-6), may_decide: MAY_NOT }),
  item('apr_r', { state: 'rejected', decided_by: 'teammate@example.com', decided_at: hoursFromNow(-3), reason: 'keep the workflow change in its own pull request' }),
  item('apr_a', { state: 'approved', decided_by: 'owner@example.com', decided_at: hoursFromNow(-4) }),
  item('apr_x', { state: 'expired', expires_at: hoursFromNow(-1) }),
  item('apr_s', { state: 'superseded', decided_at: hoursFromNow(-2) }),
]

function routes(inbox: Json[], detail: Json | null = null): Handler {
  return (m, url) => {
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: inbox, tenant_id: 'eng' } }
    if (m === 'GET' && path.startsWith('/v1/approvals/') && detail !== null) return { status: 200, body: detail }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const utils = render(<ApprovalsScreen view={view} go={vi.fn()} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return utils
}

const page = () =>
  waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)

/** What a reader sees: the text, less any hidden tooltip. */
function shown(el: Element | null): string {
  if (el === null) return ''
  const copy = el.cloneNode(true) as Element
  for (const t of copy.querySelectorAll('[hidden], [role="tooltip"]')) t.remove()
  return visible(copy)
}

const words = (el: Element | null) => shown(el).split(/\s+/).filter((w) => /[A-Za-z]/.test(w)).length

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Approvals list, as cards', () => {
  it('draws waiting items as cards, oldest first, each with its expiry and approvers', async () => {
    await mount('', routes(INBOX))
    const waiting = await screen.findByTestId('apl-waiting', undefined, WAIT)
    expect(waiting.querySelector('table')).toBeNull()
    const cards = [...waiting.querySelectorAll<HTMLElement>('.apc-card[data-approval]')]
    expect(cards.map((c) => c.dataset.approval)).toEqual(['apr_oldest', 'apr_mid', 'apr_newest'])
    for (const c of cards) {
      expect(visible(c)).toMatch(/Expires\s*in \d/)
      expect(visible(c)).toMatch(/Approvers\s*\S/)
      expect(within(c).getByRole('link', { name: 'Review' })).toBeTruthy()
    }
    expect(visible(cards[0]!)).toMatch(/Approvers\s*the platform owner/)
    expect(visible(cards[2]!)).toMatch(/Approvers\s*any member of this workspace/)
    expect(within(cards[0]!).getByRole('link', { name: 'Review' }).getAttribute('href')).toBe('/automate/approvals/apr_oldest')
    expect(within(waiting).getByRole('heading', { level: 2 }).textContent).toBe('Waiting')
  })

  it('draws decided cards with by whom, and expired and superseded each on their own', async () => {
    await mount('', routes(INBOX))
    const decided = await screen.findByTestId('apl-decided', undefined, WAIT)
    const r = decided.querySelector<HTMLElement>('.apc-card[data-approval="apr_r"]')!
    expect(visible(r)).toMatch(/Rejected by\s*teammate@example\.com/)
    expect(visible(decided.querySelector('.apc-card[data-approval="apr_a"]'))).toMatch(/Approved by\s*owner@example\.com/)
    expect(within(r).getByRole('link', { name: 'Open' })).toBeTruthy()
    const expired = screen.getByTestId('apl-expired')
    expect(expired.querySelector('[data-approval="apr_x"]')).not.toBeNull()
    expect(visible(expired)).toMatch(/Expired\s*\w/)
    const superseded = screen.getByTestId('apl-superseded')
    expect(superseded.querySelector('[data-approval="apr_s"]')).not.toBeNull()
    expect(superseded.querySelector('[data-approval="apr_x"]')).toBeNull()
  })

  it('keeps the kind filter above the cards', async () => {
    await mount('', routes(INBOX))
    await screen.findByTestId('apl-waiting', undefined, WAIT)
    const chips = within(screen.getByRole('group', { name: 'Kind' }))
    expect(visible(chips.getByRole('button', { name: /^Every kind/ }))).toBe('Every kind · 3')
    expect(visible(chips.getByRole('button', { name: /^Merge/ }))).toBe('Merge · 1')
  })

  it('lays the cards out on one column at 390px and three on a desktop', async () => {
    await mount('', routes(INBOX))
    const waiting = await screen.findByTestId('apl-waiting', undefined, WAIT)
    const grid = waiting.querySelector<HTMLElement>('.apc-grid')!
    expect(painted(grid, 'grid-template-columns', WIDE)).toBe('repeat(3, minmax(0, 1fr))')
    expect(painted(grid, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(screen.getByTestId('apl-lapsed'), 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(waiting.querySelector<HTMLElement>('.apc-card')!, 'min-width', PHONE)).toBe('0')
  })
})

type State = 'pending' | 'approved' | 'rejected' | 'expired' | 'superseded' | 'viewer'

function detailOf(st: State): Json {
  const over: Json =
    st === 'pending'
      ? {}
      : st === 'viewer'
        ? { may_decide: MAY_NOT }
        : st === 'approved'
          ? { state: 'approved', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1) }
          : st === 'rejected'
            ? { state: 'rejected', decided_by: 'owner@example.com', decided_at: hoursFromNow(-1), reason: 'keep the workflow change in its own pull request' }
            : st === 'expired'
              ? { state: 'expired', expires_at: hoursFromNow(-2) }
              : { state: 'superseded', decided_at: hoursFromNow(-1), head_sha_now: MOVED, superseded_by: { approval_id: 'apr_next', head_sha: MOVED } }
  return { approval: item('apr_merge', over), merge: merge() }
}

describe('one approval, as a card page', () => {
  const STATES: { st: State; word: string | null; buttons: string[]; links: string[] }[] = [
    { st: 'pending', word: null, buttons: ['Approve merge at 3e7a91c', 'Reject'], links: [] },
    { st: 'viewer', word: null, buttons: [], links: [] },
    { st: 'approved', word: 'Approved', buttons: [], links: ['Open the run'] },
    { st: 'rejected', word: 'Rejected', buttons: [], links: ['Open the run'] },
    { st: 'expired', word: 'Expired', buttons: [], links: ['Open the schedule'] },
    { st: 'superseded', word: 'Superseded', buttons: [], links: ['Open the new approval'] },
  ]

  for (const { st, word, buttons, links } of STATES) {
    it(`draws the ${st} head and its own actions, with the expiry and the approvers`, async () => {
      await mount('approval=apr_merge', routes(INBOX, detailOf(st)))
      const d = await page()
      const head = await within(d).findByTestId('apd-head', undefined, WAIT)
      expect(within(head).getByRole('heading', { level: 2 }).textContent).toContain('Merge pull request #482')
      // Expiry and approvers, in every state.
      const expiry = within(head).getByTestId('apd-expiry')
      expect(visible(expiry)).toMatch(st === 'expired' ? /^Expired/ : st === 'pending' || st === 'viewer' ? /^Expires\s*in \dd/ : /^Was due/)
      expect(visible(within(head).getByTestId('ad-who'))).toBe('the platform owner')
      // The head says the state's word only where it asks for something:
      // a waiting item's head carries no state chip.
      const strip = within(d).getByTestId('apd-decide')
      expect(strip.dataset.state).toBe(st === 'viewer' ? 'pending' : st)
      if (word === null) expect(within(d).queryByTestId('ap-state')).toBeNull()
      else expect(visible(within(strip).getByTestId('ap-state'))).toBe(word)
      const drawn = within(strip)
        .queryAllByRole('button')
        .filter((b) => !b.classList.contains('info-btn') && b.getAttribute('aria-label')?.startsWith('About') !== true)
        .map((b) => visible(b))
      expect(drawn).toEqual(buttons)
      for (const l of links) expect(within(strip).getByRole('link', { name: new RegExp(`^${l}`) })).toBeTruthy()
    })
  }

  it('asks for the reason only once Reject is pressed, and Cancel puts the verbs back', async () => {
    await mount('approval=apr_merge', routes(INBOX, detailOf('pending')))
    const d = await page()
    const strip = await within(d).findByTestId('apd-decide', undefined, WAIT)
    expect(within(strip).queryByRole('textbox')).toBeNull()
    fireEvent.click(within(strip).getByRole('button', { name: 'Reject' }))
    expect(within(strip).getByRole('textbox', { name: 'Reason' })).toBeTruthy()
    expect(within(strip).queryByRole('button', { name: /^Approve/ })).toBeNull()
    fireEvent.click(within(strip).getByRole('button', { name: 'Cancel' }))
    expect(within(strip).getByRole('button', { name: /^Approve/ })).toBeTruthy()
  })

  it('shows a viewer no decision button, who decides, and the reason only as a tooltip', async () => {
    await mount('approval=apr_merge', routes(INBOX, detailOf('viewer')))
    const d = await page()
    const strip = await within(d).findByTestId('ap-no-decide', undefined, WAIT)
    expect(within(d).queryByRole('button', { name: /^Approve|^Reject/ })).toBeNull()
    expect(shown(strip)).toContain('The platform owner only')
    expect(shown(strip)).not.toContain(MAY_NOT.why)
    expect(visible(strip)).toContain(MAY_NOT.why)
    expect(within(strip).getByRole('button', { name: /^About/ })).toBeTruthy()
  })

  it('draws the merge as at its head, ten of ten, and held for the owner with each path and its reason', async () => {
    await mount('approval=apr_merge', routes(INBOX, detailOf('pending')))
    const d = await page()
    const at = await within(d).findByRole('region', { name: 'At its head' }, WAIT)
    expect(visible(at.querySelector('.apd-huge'))).toBe('10')
    expect(visible(at)).toMatch(/of 10 checks passed/)
    const held = within(d).getByRole('region', { name: 'Why it is held' })
    expect(visible(held)).toContain('Held for the platform owner')
    expect(visible(held)).toMatch(/\.github\/workflows\/release\.yml\s*workflows/)
    expect(visible(held)).toMatch(/scripts\/lib\/upload\.sh\s*shared/)
  })

  it('says the head moved on a superseded merge', async () => {
    await mount('approval=apr_merge', routes(INBOX, detailOf('superseded')))
    const d = await page()
    const at = await within(d).findByRole('region', { name: 'At its head' }, WAIT)
    expect(visible(at)).toContain('Head moved to b04c2d8')
  })

  it('lays the page out on one column at 390px', async () => {
    await mount('approval=apr_merge', routes(INBOX, detailOf('pending')))
    const d = await page()
    const cols = await waitFor(() => d.querySelector<HTMLElement>('.apd-cols')!, WAIT)
    expect(painted(cols, 'grid-template-columns', WIDE)).toBe('repeat(2, minmax(0, 1fr))')
    expect(painted(cols, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(within(d).getByTestId('apd-head'), 'min-width', PHONE)).toBe('0')
    expect(painted(within(d).getByTestId('apd-expiry'), 'text-align', PHONE)).toBe('left')
  })

  it('keeps the page’s visible words under the cards’ ceiling', async () => {
    await mount('approval=apr_merge', routes(INBOX, detailOf('pending')))
    const d = await page()
    await within(d).findByTestId('apd-decide', undefined, WAIT)
    expect(words(d)).toBeLessThanOrEqual(120)
  })
})
