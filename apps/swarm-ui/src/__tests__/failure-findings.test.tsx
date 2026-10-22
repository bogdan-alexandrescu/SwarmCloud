// A FAILED AGENT'S FAILURE FINDING (docs/design/failure-review.md §1; lane
// FR-4 FINDINGS, part of #1933). The failure review found the cause in the
// tenant's own repository; the bell's item lands on this card, which:
//
//   * reads only `GET /v1/failure-findings?task_id=<this agent>`;
//   * draws the cause sentence and the platform-recorded evidence;
//   * never draws a platform-tenant field, even when the API sends one;
//   * draws nothing for an agent with no finding;
//   * marks the finding's bell item read once it is on screen.
//
// Fixtures only; no network. The platform-tenant values are built at runtime.

import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { serve, visible } from './repofixture'

const feed = vi.hoisted(() => ({
  snapshot: { status: 'loading', since: 0 } as unknown,
  markRead: vi.fn(async () => {}),
}))

vi.mock('../notificationFeed', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../notificationFeed')>()
  return {
    ...actual,
    notificationsSnapshot: () => feed.snapshot,
    subscribeNotifications: () => () => {},
    markRead: feed.markRead,
  }
})

const TASK = 'task_' + 'a1'.repeat(10)
const FID = 'ff_' + 'b2'.repeat(10)
const FINGERPRINT = 'fr-' + Math.random().toString(16).slice(2, 14)
const FIX = 'Patch platform internals ' + Math.random().toString(16).slice(2, 10)
const CAUSE = "The repository's test_login fails on a missing fixture"

const FINDING = {
  finding_id: FID,
  kind: 'agent',
  task_id: TASK,
  failure_id: TASK,
  state: 'open',
  cause: CAUSE,
  evidence: { kind: 'agent', state: 'FAILED', profile: 'claude-code', end_cause: 'agent_failed', last_error: 'pytest exited 1' },
  created_at: new Date(Date.now() - 600_000).toISOString(),
  // What the API must never send, sent anyway: the card must not draw it.
  fingerprint: FINGERPRINT,
  fix: FIX,
  firing_id: 'fir_' + 'c3'.repeat(6),
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../FailureFindingCard')
}

beforeEach(() => {
  feed.snapshot = { status: 'loading', since: 0 }
  feed.markRead.mockClear()
})

describe('the failure finding card', () => {
  it('reads only this agent’s findings and draws the cause sentence and evidence', async () => {
    const calls = serve((_m, url) => (url.startsWith('/v1/failure-findings') ? { status: 200, body: { findings: [FINDING] } } : null))
    const { FailureFindingCard } = await load()
    render(<FailureFindingCard taskId={TASK} />)
    const card = await screen.findByRole('heading', { name: /failure review found the cause/i })
    const text = visible(card.closest('section'))
    // MUTATION: draw `fix` instead of `cause` and the cause sentence is missing.
    expect(text).toContain(CAUSE)
    // MUTATION: drop the `Evidence` block and the end cause and last error are not drawn.
    expect(text).toContain('agent_failed')
    expect(text).toContain('pytest exited 1')
    // MUTATION: list `/v1/failure-findings` without `task_id` and every finding of the tenant is read.
    expect(calls.map((c) => c.url)).toEqual([`/v1/failure-findings?task_id=${TASK}`])
    // MUTATION: give the card no `id="failure-findings"` and the bell's link lands nowhere.
    expect(card.closest('section')?.id).toBe('failure-findings')
  })

  it('never draws a platform-tenant field, even one the API sent', async () => {
    serve((_m, url) => (url.startsWith('/v1/failure-findings') ? { status: 200, body: { findings: [FINDING] } } : null))
    const { FailureFindingCard } = await load()
    const { container } = render(<FailureFindingCard taskId={TASK} />)
    await screen.findByText(CAUSE)
    // MUTATION: spread the raw row in `asFinding` and draw its fields, and the fix and fingerprint appear.
    expect(container.textContent).not.toContain(FIX)
    expect(container.textContent).not.toContain(FINGERPRINT)
    expect(container.innerHTML).not.toContain('fir_')
  })

  it('draws nothing for an agent with no finding', async () => {
    const calls = serve((_m, url) => (url.startsWith('/v1/failure-findings') ? { status: 200, body: { findings: [] } } : null))
    const { FailureFindingCard } = await load()
    const { container } = render(<FailureFindingCard taskId={TASK} />)
    await waitFor(() => expect(calls.length).toBe(1))
    // MUTATION: draw the card on an empty answer and every failed agent shows an empty finding.
    await waitFor(() => expect(container.innerHTML).toBe(''))
  })

  it('says so when the findings could not be read', async () => {
    serve((_m, url) => (url.startsWith('/v1/failure-findings') ? { status: 503, body: { code: 'unavailable', message: 'down' } } : null))
    const { FailureFindingCard } = await load()
    render(<FailureFindingCard taskId={TASK} />)
    // MUTATION: return null on an error and a finding that exists is hidden silently.
    expect(await screen.findByText(/could not be read/i)).toBeTruthy()
  })

  it('marks the finding’s bell item read once it is drawn', async () => {
    serve((_m, url) => (url.startsWith('/v1/failure-findings') ? { status: 200, body: { findings: [FINDING] } } : null))
    const { FailureFindingCard, findingNotificationId } = await load()
    // MUTATION: name the item anything but `failure_review:<id>` and it never matches the bell's.
    expect(findingNotificationId(FID)).toBe(`failure_review:${FID}`)
    feed.snapshot = {
      status: 'ok',
      fetchedAt: Date.now(),
      data: { items: [{ id: `failure_review:${FID}`, read: false }, { id: 'task:questions:other', read: false }] },
    }
    render(<FailureFindingCard taskId={TASK} />)
    await screen.findByText(CAUSE)
    // MUTATION: drop `useReadsNotifications` and the bell keeps ringing after the card is read.
    await waitFor(() => expect(feed.markRead).toHaveBeenCalledWith([`failure_review:${FID}`]))
    // MUTATION: mark every unread item and the unrelated question is cleared too.
    expect(feed.markRead).toHaveBeenCalledTimes(1)
  })
})
