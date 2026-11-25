// THE PLAN'S THREAD IN THE DECISION PAGE'S MARGIN (docs/design/plans-pipeline.md
// §2.3 `D:P3a`, `D:P4a`-`D:P4c`, §3.4, §3.5, §5.3, §5.4, §8; lane U4
// PLAN-THREAD-UI). Red first: PlanThread.tsx does not exist yet.
//
// WHAT EACH CASE HOLDS:
//   * a comment posts with the shown digest and its anchor (a step, a
//     requirement by its number, the whole plan with no ref), and lands in
//     that part's margin;
//   * comment text is other people's text: markup is drawn as characters;
//   * Request changes sends the shown digest, the thread says "re-planning",
//     and the planner's revision then arrives with its diff and its cost,
//     the comment marked answered by it;
//   * Approve during a re-plan says it cancels it, and the re-plan can be
//     cancelled;
//   * the events after approval draw in time order;
//   * resolved comments fold, and Resolve sends the fold;
//   * a member who may not comment or edit sees the thread with no Comment
//     and no Request changes (§8);
//   * the cap's refusal shows the server's sentence and disables Request
//     changes;
//   * at 390 pixels the thread wraps instead of widening the page.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { visible } from './repofixture'
import { painted } from './marks'
import type { CascadeEnv } from './cssgate'
import { COMMENT, D2, D3, PLAN, PLAN_R3, REV1, REV2, REV3, REVISE, WAIT, at, comment, mount, routes, runRead, sent, type Json } from './planthreadfixture'

const doc = () => screen.findByTestId('pd-doc', undefined, WAIT)
const bar = () => screen.findByTestId('pd-bar', undefined, WAIT)
const part = async (name: string) => within(await doc()).getByRole('region', { name })

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('Comment', () => {
  it('posts a step comment with the shown digest and the step’s anchor, and draws it in that step’s margin', async () => {
    const posted = comment({ entry_id: 'pth_new1', anchor: { part: 'step', ref: 'overlaps-read' }, by: 'cat@example.com', text: 'Cache the read per digest.', at: at(0) })
    const { calls } = await mount(routes({ extra: (m, url) => (m === 'POST' && url.split('?')[0] === COMMENT ? { status: 201, body: { entry: posted } } : null) }))
    const s1 = await part('Step 1')
    fireEvent.click(within(s1).getByRole('button', { name: 'Comment' }))
    fireEvent.change(within(s1).getByRole('textbox', { name: 'Comment on Step 1' }), { target: { value: 'Cache the read per digest.' } })
    fireEvent.click(within(s1).getByRole('button', { name: 'Post' }))
    await waitFor(() => expect(sent(calls, 'POST', COMMENT)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', COMMENT)[0]!.body).toEqual({ plan_digest: D2, anchor: { part: 'step', ref: 'overlaps-read' }, text: 'Cache the read per digest.' })
    await waitFor(() => expect(visible(within(s1).getByTestId('pt-thread'))).toContain('Cache the read per digest.'), WAIT)
  })

  it('anchors a requirement comment by the requirement’s number and a summary comment to the plan with no ref', async () => {
    const { calls } = await mount(routes({ extra: (m, url, body) => (m === 'POST' && url.split('?')[0] === COMMENT ? { status: 201, body: { entry: comment({ entry_id: `pth_${Math.random().toString(16).slice(2, 10)}`, ...(body as Json), at: at(0) }) } } : null) }))
    const reqs = await part('Requirements')
    fireEvent.click(within(reqs).getByRole('button', { name: 'Comment' }))
    fireEvent.change(within(reqs).getByRole('combobox', { name: 'Requirement' }), { target: { value: '2' } })
    fireEvent.change(within(reqs).getByRole('textbox', { name: 'Comment on Requirements' }), { target: { value: 'Say what [] means.' } })
    fireEvent.click(within(reqs).getByRole('button', { name: 'Post' }))
    await waitFor(() => expect(sent(calls, 'POST', COMMENT)).toHaveLength(1), WAIT)
    expect((sent(calls, 'POST', COMMENT)[0]!.body as Json).anchor).toEqual({ part: 'requirement', ref: '2' })
    const summary = await part('Summary')
    fireEvent.click(within(summary).getByRole('button', { name: 'Comment' }))
    fireEvent.change(within(summary).getByRole('textbox', { name: 'Comment on Summary' }), { target: { value: 'Fine overall.' } })
    fireEvent.click(within(summary).getByRole('button', { name: 'Post' }))
    await waitFor(() => expect(sent(calls, 'POST', COMMENT)).toHaveLength(2), WAIT)
    expect((sent(calls, 'POST', COMMENT)[1]!.body as Json).anchor).toEqual({ part: 'plan', ref: null })
  })

  it('shows a refused comment in the server’s own words', async () => {
    await mount(routes({ extra: (m, url) => (m === 'POST' && url.split('?')[0] === COMMENT ? { status: 409, body: { code: 'plan_changed', message: 'the plan of run moved; read it again' } } : null) }))
    const s1 = await part('Step 1')
    fireEvent.click(within(s1).getByRole('button', { name: 'Comment' }))
    fireEvent.change(within(s1).getByRole('textbox', { name: 'Comment on Step 1' }), { target: { value: 'x' } })
    fireEvent.click(within(s1).getByRole('button', { name: 'Post' }))
    const alert = await within(s1).findByRole('alert', undefined, WAIT)
    expect(visible(alert)).toContain('the plan of run moved; read it again')
  })

  it('draws a comment’s text as text, never as markup', async () => {
    const markup = '<img src=x onerror="alert(1)"><b>bold</b>'
    await mount(routes({ run: () => ({ plan_thread: [comment({ text: markup })] }) }))
    const s2 = await part('Step 2')
    const thread = within(s2).getByTestId('pt-thread')
    await waitFor(() => expect(visible(thread)).toContain(markup), WAIT)
    expect(thread.querySelector('img')).toBeNull()
    expect(thread.querySelector('b')).toBeNull()
  })
})

describe('Request changes', () => {
  it('sends the shown digest, says re-planning, then draws the new revision’s diff and cost with the comment answered', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let state: 'before' | 'running' | 'after' = 'before'
    const replan = { replan_id: 'rpl_0a1b2c', task_id: 'task_replan1', from_digest: D2, from_revision: 2, entries: ['pth_open1'], requested_by: 'ana@example.com', state: 'running', cancelled: null, at: at(0) }
    const { calls } = await mount(
      routes({
        run: () =>
          state === 'before'
            ? {}
            : state === 'running'
              ? { plan_replan: replan, plan_replans: 1 }
              : {
                  plan: PLAN_R3,
                  plan_digest: D3,
                  plan_revision: 3,
                  plan_revisions: [REV3, REV2, REV1],
                  plan_replan: { ...replan, state: 'applied', revision: 3 },
                  plan_replans: 1,
                  plan_thread: [
                    ...(runRead().plan_thread as Json[]).slice(0, 2),
                    comment({ answered_by_revision: 3 }),
                    { kind: 'event', event: 'revised', at: REV3.at, ref: null, revision: 3, digest: D3, by: 'planner', made_by: 'revise' },
                    comment({ entry_id: 'pth_ans1', kind: 'answer', by: 'planner', text: 'Write is not needed; the read binding is enough.', at: REV3.at }),
                  ],
                },
        extra: (m, url) => {
          if (m !== 'POST' || url.split('?')[0] !== REVISE) return null
          state = 'running'
          return { status: 202, body: { replan } }
        },
      }),
    )
    const b = await bar()
    fireEvent.click(await within(b).findByRole('button', { name: 'Request changes' }, WAIT))
    await waitFor(() => expect(sent(calls, 'POST', REVISE)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', REVISE)[0]!.body).toEqual({ plan_digest: D2 })
    await waitFor(() => expect(visible(screen.getByTestId('pt-replanning'))).toContain('re-planning'), WAIT)
    state = 'after'
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000)
    })
    const summary = await part('Summary')
    const revised = await waitFor(() => {
      const row = within(summary).getAllByTestId('pt-entry').find((e) => e.getAttribute('data-event') === 'revised' && e.getAttribute('data-revision') === '3')
      expect(row).toBeTruthy()
      return row!
    }, WAIT)
    expect(visible(revised)).toContain('$1.80')
    const diff = await within(revised).findByTestId('pr-diff', undefined, WAIT)
    expect(visible(diff)).toContain('Serve overlaps on the plan read, cached per digest.')
    expect(visible(diff)).toContain('terraform/infra/buckets.tf')
    expect(screen.queryByTestId('pt-replanning')).toBeNull()
    const s2 = await part('Step 2')
    const asked = within(s2).getAllByTestId('pt-entry').find((e) => e.getAttribute('data-entry') === 'pth_open1')!
    expect(visible(asked)).toContain('answered r3')
    // The page still shows revision 2 (`D:P2c`): a revision never swaps the plan under Approve.
    expect(visible(screen.getByTestId('pd-summary'))).toBe(PLAN.summary)
  })

  it('shows the cap’s refusal in its own sentence and disables Request changes', async () => {
    const sentence = 'run has asked for 5 re-plans, the most plan_review_caps.replans allows'
    await mount(routes({ extra: (m, url) => (m === 'POST' && url.split('?')[0] === REVISE ? { status: 429, body: { code: 'replan_cap', message: sentence } } : null) }))
    const b = await bar()
    fireEvent.click(await within(b).findByRole('button', { name: 'Request changes' }, WAIT))
    await waitFor(() => expect(visible(b)).toContain(sentence), WAIT)
    expect((within(b).getByRole('button', { name: 'Request changes' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('is not offered while no comment is open on the revision', async () => {
    await mount(routes({ run: () => ({ plan_thread: [comment({ resolved: true })] }) }))
    const b = await bar()
    await waitFor(() => expect(within(b).getByRole('button', { name: 'Request changes' })).toBeTruthy(), WAIT)
    expect((within(b).getByRole('button', { name: 'Request changes' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('Approve while a re-plan runs', () => {
  const running = { replan_id: 'rpl_0a1b2c', task_id: 'task_replan1', from_digest: D2, from_revision: 2, entries: ['pth_open1', 'pth_open2'], requested_by: 'ana@example.com', state: 'running', cancelled: null, at: at(-1) }

  it('says approving the shown revision cancels the re-plan, and offers to cancel it', async () => {
    const { calls } = await mount(
      routes({
        run: () => ({ plan_replan: running, plan_replans: 1 }),
        extra: (m, url) => (m === 'POST' && url.split('?')[0] === REVISE ? { status: 200, body: { replan: { ...running, state: 'cancelling', cancelled: 'cancelled' } } } : null),
      }),
    )
    const b = await bar()
    const note = await within(b).findByTestId('pt-approve-cancels', undefined, WAIT)
    expect(visible(note)).toContain('revision 2')
    expect(visible(note)).toContain('cancels')
    expect(visible(note)).toContain('2 comments')
    expect(within(b).getByRole('button', { name: 'Approve' })).toBeTruthy()
    expect(within(b).queryByRole('button', { name: 'Request changes' })).toBeNull()
    fireEvent.click(within(b).getByRole('button', { name: 'Cancel the re-plan' }))
    await waitFor(() => expect(sent(calls, 'POST', REVISE)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', REVISE)[0]!.body).toEqual({ cancel: 'rpl_0a1b2c' })
  })
})

describe('after approval', () => {
  it('draws the events in time order, whatever order they arrive in', async () => {
    const events: Json[] = [
      { kind: 'event', event: 'merged', at: at(50), ref: '41', number: 41 },
      { kind: 'event', event: 'approved', at: at(-10), ref: null, by: 'ana@example.com', digest: D2, revision: 2 },
      { kind: 'event', event: 'ci_round', at: at(30), ref: 'wf_fix1', round: 1, workflow_id: 'wf_fix1', state: 'SUCCEEDED' },
      { kind: 'event', event: 'step_task', at: at(0), ref: 'overlaps-read', task_id: 'task_step1', state: 'SUCCEEDED' },
      { kind: 'event', event: 'pull_request', at: at(20), ref: '41', number: 41, url: 'https://github.com/example-org/example-api/pull/41' },
      { kind: 'event', event: 'keyword', at: at(51), ref: '#12', closes: true, keyword: 'Closes #12' },
      { kind: 'event', event: 'review', at: at(40), ref: null, requirements_met: true, requirements_unmet: [] },
    ]
    await mount(routes({ item: { state: 'approved', decided_by: 'ana@example.com', decided_at: at(-10) }, run: () => ({ state: 'DONE', may_decide: null, plan_thread: events }) }))
    const summary = await part('Summary')
    const order = await waitFor(() => {
      const rows = within(summary).getAllByTestId('pt-entry').map((e) => e.getAttribute('data-event'))
      expect(rows.length).toBe(7)
      return rows
    }, WAIT)
    expect(order).toEqual(['approved', 'step_task', 'pull_request', 'ci_round', 'review', 'merged', 'keyword'])
    expect(visible(summary)).toContain('Closes #12')
  })
})

describe('resolved comments', () => {
  it('fold under a closed summary, and Resolve sends the fold', async () => {
    const { calls } = await mount(
      routes({
        run: () => ({ plan_thread: [comment(), comment({ entry_id: 'pth_done1', text: 'Name the bucket.', resolved: true, at: at(-50) })] }),
        extra: (m, url, body) => (m === 'POST' && url.split('?')[0] === COMMENT ? { status: 200, body: { entry: comment({ entry_id: (body as Json).entry_id, resolved: true }) } } : null),
      }),
    )
    const s2 = await part('Step 2')
    const fold = await within(s2).findByTestId('pt-resolved', undefined, WAIT)
    expect((fold as HTMLDetailsElement).open).toBe(false)
    expect(visible(fold)).toContain('Name the bucket.')
    const open = within(s2).getAllByTestId('pt-entry').find((e) => e.getAttribute('data-entry') === 'pth_open1')!
    expect(fold.contains(open)).toBe(false)
    fireEvent.click(within(open).getByRole('button', { name: 'Resolve' }))
    await waitFor(() => expect(sent(calls, 'POST', COMMENT)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', COMMENT)[0]!.body).toEqual({ entry_id: 'pth_open1', resolved: true })
    await waitFor(() => expect(within(s2).getByTestId('pt-resolved').contains(within(s2).getAllByTestId('pt-entry').find((e) => e.getAttribute('data-entry') === 'pth_open1')!)).toBe(true), WAIT)
  })
})

describe('who may', () => {
  it('shows a member who may not comment or edit the thread, with no Comment, Resolve or Request changes', async () => {
    const viewer = { approve: false, reject: false, why: 'Your role in eng is viewer; members decide here.', deciders: ['members'], edit: false, comment: false }
    await mount(routes({ item: { may_decide: viewer }, run: () => ({ may_decide: viewer }) }))
    const s2 = await part('Step 2')
    await waitFor(() => expect(visible(within(s2).getByTestId('pt-thread'))).toContain('Does the planner need write on the bucket too?'), WAIT)
    const d = await doc()
    expect(within(d).queryByRole('button', { name: 'Comment' })).toBeNull()
    expect(within(d).queryByRole('button', { name: 'Resolve' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Request changes' })).toBeNull()
  })

  it('lets a member who may comment but not edit comment, without Request changes', async () => {
    const commenter = { approve: false, reject: false, why: null, deciders: ['members'], edit: false, comment: true }
    await mount(routes({ item: { may_decide: commenter }, run: () => ({ may_decide: commenter }) }))
    const s1 = await part('Step 1')
    await waitFor(() => expect(within(s1).getByRole('button', { name: 'Comment' })).toBeTruthy(), WAIT)
    expect(screen.queryByRole('button', { name: 'Request changes' })).toBeNull()
  })

  it('draws no thread for a run read that serves none (an older server)', async () => {
    await mount(routes({ run: () => ({ plan_thread: undefined, thread_truncated: undefined, may_decide: undefined }) }))
    const d = await doc()
    expect(within(d).queryByTestId('pt-thread')).toBeNull()
    expect(within(d).queryByRole('button', { name: 'Comment' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Request changes' })).toBeNull()
  })
})

describe('at 390 pixels', () => {
  const PHONE: CascadeEnv = { width: 390 }
  it('wraps the thread’s text and the composer inside the column', async () => {
    await mount(routes())
    const s2 = await part('Step 2')
    const entry = await within(s2).findByTestId('pt-entry', undefined, WAIT)
    expect(painted(entry, 'overflow-wrap', PHONE)).toBe('anywhere')
    fireEvent.click(within(s2).getByRole('button', { name: 'Comment' }))
    const box = within(s2).getByRole('textbox', { name: 'Comment on Step 2' })
    expect(painted(box, 'max-width', PHONE)).toBe('100%')
    expect(painted(screen.getByTestId('pd-bar'), 'flex-wrap', PHONE)).toBe('wrap')
  })
})

