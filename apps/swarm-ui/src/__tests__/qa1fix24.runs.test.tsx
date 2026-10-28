// CONSOLE VISUAL QA PASS 1, WORK › RUNS (QA1-FIX-24, part of #2596 and #2391,
// BGD, 2026-10-28). One case per finding this lane changed, each red on main
// before the change:
//
//   runs-submit-03  a decision shows its outcome where it was taken, and a
//                   later read that still serves the run as Planned does not
//                   draw the undecided run back; a refusal is drawn beside the
//                   buttons pressed;
//   runs-submit-06  Progress says when the record stops short of the state;
//   runs-submit-08  the reject form and the plan editor take the focus, and
//                   Escape closes the form and gives the focus back to Reject;
//   runs-submit-09  a disabled Save names what blocks it; blank fields are
//                   marked; Estimate says its form;
//   runs-submit-28  a step's role once, and the requirements sentence reads;
//   runs-submit-29  the foot's reject form opens in flow, not in the sticky bar;
//   runs-submit-31  an unread pull request says why in words;
//   chrome-C12      a schedule creator is named, not `schedule:sch_…`;
//   chrome-C13      an automatic approval is a word, not a dash;
//   chrome-C14      the issue's state says it was read at submission.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { FULL_PLAN, digest, issueRun } from './runfixture'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const run = issueRun

type Handler = (method: string, url: string) => { status: number; body: unknown } | null

function serve(handler: Handler) {
  const calls: { method: string; url: string }[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url })
    const r = handler(method, url) ?? { status: 404, body: { code: 'not_found', message: `no stub for ${url}` } }
    return new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  return calls
}

async function mount(view: string | null) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  return render(<RunsScreen view={view} go={vi.fn()} />)
}

async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.useRealTimers()
})

const RUNNING = {
  state: 'RUNNING', workflow_id: 'wf_new', approved_by: 'operator@example.com', approved_at: '2026-10-02T14:20:00Z',
  approved_digest: digest('a1'),
}

describe('runs-submit-03: a decision shows its outcome where it was taken', () => {
  it('after Approve and run the page says Approved, offers no buttons, and a later read of the run as Planned does not undo it', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
    let reads = 0
    serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') {
        reads += 1
        return { status: 200, body: { run: run() } }
      }
      if (url.endsWith('plan:approve')) return { status: 200, body: { run: run(RUNNING) } }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    const { RUN_POLL_MS } = await import('../Runs')
    await advance(0)
    fireEvent.click(screen.getAllByRole('button', { name: 'Approve and run' })[0]!)
    await advance(0)
    // MUTATION: drop `answer('top')` and nothing beside the decision says what it did.
    expect(visible(container.querySelector('.rn-decided'))).toContain('Approved.')
    expect(visible(container.querySelector('.rn-decided'))).toContain('Running')
    // The poll serves the run still Planned on the plan just approved: an
    // answer older than the decision.
    await advance(RUN_POLL_MS)
    expect(reads).toBe(2)
    // MUTATION: drop the `decidedRef` check in the served effect and the
    // page reads "Planned · Waiting for a person" with the buttons again.
    expect(visible(container.querySelector('.rn-state'))).toContain('Running')
    expect(screen.queryAllByRole('button', { name: 'Approve and run' })).toHaveLength(0)
    expect(container.querySelector('.rn-decided')).not.toBeNull()
  })

  it('after Reject this plan the page says Rejected where the form was', async () => {
    serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run() } }
      if (url.endsWith('plan:reject')) {
        return { status: 200, body: { run: run({ state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com' }) } }
      }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Reject' }, WAIT))[0]!)
    fireEvent.click(screen.getByRole('button', { name: 'Reject this plan' }))
    // MUTATION: drop `{!canAct && answer('top')}` and the decision says nothing where it was taken.
    await waitFor(() => expect(visible(container.querySelector('.rn-decided'))).toContain('Rejected.'), WAIT)
    expect(screen.queryAllByRole('button', { name: 'Reject' })).toHaveLength(0)
  })

  it('a refused approval is drawn beside the buttons pressed, not under the risks', async () => {
    serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') return { status: 200, body: { run: run() } }
      if (url.endsWith('plan:approve')) return { status: 500, body: { code: 'internal', message: 'the store refused it' } }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Approve and run' }, WAIT))[0]!)
    // MUTATION: draw the FailedPanel at the foot of the page again and `.rn-wait` holds no refusal.
    await waitFor(() => expect(visible(container.querySelector('.rn-wait .state.failed'))).toContain('the store refused it'), WAIT)
  })
})

describe('runs-submit-06: Progress says when the record stops short of the state', () => {
  it('a Running run whose history ends at Planned says the transitions between were not recorded', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run(RUNNING) } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    // MUTATION: drop the `historyGap` line and Progress ends at Planned under a Running badge with no word.
    await waitFor(() => expect(visible(container.querySelector('.rn-history [data-gap]'))).toBe(
      'The record ends at Planned; the run is Running now. The transitions between were not recorded.'), WAIT)
  })

  it('a history that reaches the state adds no line', async () => {
    const { historyGap } = await import('../Runs')
    expect(historyGap(run() as never)).toBeNull()
  })
})

describe('runs-submit-08: the forms take the focus they open', () => {
  it('Reject focuses Why; Escape closes the form and gives the focus back to Reject', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run() } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Reject' }, WAIT))[0]!)
    const why = container.querySelector<HTMLTextAreaElement>('.rn-reject textarea')!
    // MUTATION: drop the `why.current?.focus()` effect and the focus stays on <body>.
    expect(document.activeElement).toBe(why)
    fireEvent.keyDown(why, { key: 'Escape' })
    // MUTATION: drop the form's onKeyDown and Escape leaves it open.
    expect(container.querySelector('.rn-reject')).toBeNull()
    // MUTATION: drop the refocus in `keep` and the focus falls to <body>.
    await waitFor(() => expect(document.activeElement?.getAttribute('data-act')).toBe('reject-top'), WAIT)
  })

  it('Edit plan focuses the Summary', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run() } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit plan' }, WAIT))[0]!)
    // MUTATION: drop the editor's `first.current?.focus()` and the focus stays on <body>.
    expect(document.activeElement).toBe(container.querySelector('.rn-edit textarea'))
  })
})

describe('runs-submit-09: a disabled Save names what blocks it', () => {
  it('the blank fields are marked and described, and Save says which', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run() } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit plan' }, WAIT))[0]!)
    const summary = container.querySelector<HTMLTextAreaElement>('.rn-edit textarea')!
    fireEvent.change(summary, { target: { value: '' } })
    const title = container.querySelectorAll<HTMLInputElement>('.rn-field-set input')[0]!
    fireEvent.change(title, { target: { value: ' ' } })
    const save = screen.getByRole('button', { name: 'Save the plan' })
    expect(save).toHaveProperty('disabled', true)
    // MUTATION: go back to `disabled={busy || blank}` with no reason and Save is described by nothing.
    const reason = document.getElementById(save.getAttribute('aria-describedby') ?? '')
    expect(visible(reason)).toBe("Not saved: the summary, step 1's title are empty.")
    // MUTATION: drop `aria-invalid` / the hint and the blank field is not marked.
    expect(summary.getAttribute('aria-invalid')).toBe('true')
    expect(visible(document.getElementById(summary.getAttribute('aria-describedby') ?? ''))).toContain('Required')
    expect(title.getAttribute('aria-invalid')).toBe('true')
    // MUTATION: drop the Estimate hint and its free text has no stated form.
    const estimate = container.querySelectorAll<HTMLInputElement>('.rn-edit > label input')[0]!
    expect(visible(document.getElementById(estimate.getAttribute('aria-describedby') ?? ''))).toContain('about 2 hours')
  })
})

describe('runs-submit-28: the role once, and a sentence that reads', () => {
  it("a step titled with its id has no second id line, and the requirements sentence quotes 'part of #N'", async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run() } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    await waitFor(() => expect(container.querySelectorAll('.rn-step')).toHaveLength(2), WAIT)
    // MUTATION: draw `.rn-step-id` with the mono id again under "api: …".
    expect(container.querySelector('.rn-step .rn-step-id')).toBeNull()
    // MUTATION: put back "the pull request says part of." and this is red.
    expect(visible(container.querySelector('.rn-plan'))).toContain('the pull request says part of #N, not Closes.')
  })

  it('a step whose title does not name it keeps its id line', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET'
      ? { status: 200, body: { run: run({ plan: { ...FULL_PLAN, steps: [{ step_id: 'ui', title: 'Draw the column', prompt: 'p' }] } }) } }
      : null))
    const { container } = await mount('run=run_4c1e09d2')
    await waitFor(() => expect(visible(container.querySelector('.rn-step .rn-step-id'))).toBe('ui'), WAIT)
  })
})

describe('runs-submit-29: the foot reject form opens in flow', () => {
  it('Reject in the sticky bar opens its form outside the bar', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run() } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    await waitFor(() => expect(container.querySelector('[data-act="reject-foot"]')).not.toBeNull(), WAIT)
    fireEvent.click(container.querySelector('[data-act="reject-foot"]')!)
    // MUTATION: keep `rn-actbar` on the foot while the form is open and the form grows the sticky bar.
    expect(container.querySelector('.rn-actbar .rn-reject')).toBeNull()
    expect(container.querySelector('.rn-actflow .rn-reject')).not.toBeNull()
  })
})

describe('runs-submit-31: an unread pull request says why in words', () => {
  it('a Running run whose workflow could not be read says so beside Pull request', async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET' ? { status: 200, body: { run: run(RUNNING) } } : null))
    const { container } = await mount('run=run_4c1e09d2')
    // MUTATION: put back the bare `<Dash>` and the fact reads "—".
    await waitFor(() => expect(visible(container.querySelector('[data-pr-unknown]'))).toBe('not known · its workflow was not read'), WAIT)
  })
})

describe('chrome-C12: a schedule is named', () => {
  const SCHEDULE = 'schedule:sch_54e9828e6772'
  const SCHEDULES = { schedules: [{ schedule_id: 'sch_54e9828e6772', name: 'Failure review' }], tenant_id: 'eng' }

  it('byWord names a schedule, and says when its name was not read', async () => {
    const { byWord } = await import('../Runs')
    // MUTATION: return `by` as recorded and the id is all the reader gets.
    expect(byWord(SCHEDULE, { sch_54e9828e6772: 'Failure review' })).toBe('schedule Failure review')
    expect(byWord(SCHEDULE, null)).toBe('schedule sch_54e9828e6772 · name not read')
    expect(byWord('operator@example.com', null)).toBe('operator@example.com')
  })

  it("the list's By column names the schedule once the schedules are read", async () => {
    serve((_m, url) => {
      if (url === '/v1/runs?view=summary') {
        return { status: 200, body: { runs: [run({ created_by: SCHEDULE, plan: undefined, history: undefined })], next_page_token: null, tenant_id: 'eng' } }
      }
      if (url === '/v1/schedules') return { status: 200, body: SCHEDULES }
      return null
    })
    const { container } = await mount(null)
    // MUTATION: drop the `want` effect in RunList and the schedules are never read.
    await waitFor(() => expect(visible(container.querySelector('.rn-row td[data-label="By"]'))).toBe('schedule Failure review'), WAIT)
  })

  it("the run's Progress and Chosen at submission name it too", async () => {
    serve((m, url) => {
      if (url === '/v1/runs/run_4c1e09d2' && m === 'GET') {
        return { status: 200, body: { run: run({ created_by: SCHEDULE, history: [
          { at: '2026-10-02T14:02:00Z', from: null, to: 'PLANNING', by: SCHEDULE },
          { at: '2026-10-02T14:09:00Z', from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
        ] }) } }
      }
      if (url === '/v1/schedules') return { status: 200, body: SCHEDULES }
      return null
    })
    const { container } = await mount('run=run_4c1e09d2')
    await waitFor(() => expect(visible(container.querySelector('.rn-history li'))).toContain('by schedule Failure review'), WAIT)
    expect(visible(container.querySelector('.rn-chosen'))).toContain('schedule Failure review')
    expect(visible(container)).not.toContain('schedule:sch_')
  })
})

describe('chrome-C13: an automatic approval is a word', () => {
  it("an automatic gate still to approve reads 'automatic', one that approved reads 'approved'", async () => {
    const { planApprovalWord } = await import('../Runs')
    // MUTATION: return '—' for every automatic run again.
    expect(planApprovalWord(run({ plan_approval: 'auto' }) as never)).toBe('automatic')
    expect(planApprovalWord(run({ plan_approval: 'auto', ...RUNNING }) as never)).toBe('approved')
    expect(planApprovalWord(run({ plan_approval: 'auto', state: 'PLANNING', plan: null }) as never)).toBe('no plan')
  })
})

describe('chrome-C14: the issue state is a reading at submission', () => {
  it("the state row says 'at submission', and a reading with no time says so", async () => {
    serve((m, url) => (url === '/v1/runs/run_4c1e09d2' && m === 'GET'
      ? { status: 200, body: { run: run({ issue_read: { title: 'T', state: 'open', labels: [], body: '', body_truncated: false,
        body_redacted: false, comments: 0, read_at: null, url: 'https://github.com/example-org/infra/issues/512' } }) } }
      : null))
    const { container } = await mount('run=run_4c1e09d2')
    // MUTATION: print `{read.state}` alone and 'open' reads as the issue now.
    await waitFor(() => expect(visible(container.querySelector('.rn-read [data-state-then]'))).toBe('open · at submission'), WAIT)
    // MUTATION: put back the bare '—' for a reading with no time.
    expect(visible(container.querySelector('.rn-read'))).toContain('the time was not recorded')
  })
})
