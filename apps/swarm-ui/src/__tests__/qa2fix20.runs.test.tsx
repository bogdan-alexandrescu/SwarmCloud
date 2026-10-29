// QA2-FIX-20 (part of #2694): Work › Runs, the console visual QA pass 2
// findings on the fixture console (QA2-RUNS-SUBMIT, 2026-10-29).
//
//   runs2-03  a row whose run kept no issue title was a bare run id beside a
//             bare `#512`: nothing named the work, and nothing said why.
//   runs2-11  the plan's facts read `mode not stated` for a plan without a mode.
//
// Every assertion names the mutation that turns it red. Nothing here is
// token-shaped.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FULL_PLAN, issueRun } from './runfixture'

const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const JSON_HEADERS = { 'content-type': 'application/json' }
const WAIT = { timeout: 4000 }

afterEach(() => {
  vi.unstubAllEnvs()
})

function serve(handler: (url: string) => unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const body = handler(String(input))
    return body === null
      ? new Response(JSON.stringify({ code: 'not_found', message: 'no stub' }), { status: 404, headers: JSON_HEADERS })
      : new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
}

async function mount(view: string | null) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  return render(<RunsScreen view={view} go={vi.fn()} />).container
}

const READ = {
  title: 'The workflow list shows no cost column', state: 'open', url: 'https://github.com/example-org/infra/issues/512',
  read_at: '2026-10-02T14:02:00Z', labels: [], comments: 2, body: '', body_truncated: false, body_redacted: false,
}

describe('runs2-03: a row with no issue title says why', () => {
  async function rows(runs: unknown[]) {
    serve((url) => (url === '/v1/runs?view=summary' ? { runs, next_page_token: null, tenant_id: 'eng' } : null))
    const container = await mount(null)
    await waitFor(() => expect(container.querySelectorAll('.rn-row').length).toBe(runs.length), WAIT)
    return [...container.querySelectorAll<HTMLElement>('.rn-row td[data-label="Issue"]')]
  }

  it('a run that kept no reading says the title was not kept, with the reason whole in its title', async () => {
    const [cell] = await rows([issueRun({ plan: undefined, history: undefined })])
    // MUTATION: drop `<NoTitle>` and the cell is a bare `#512` again.
    const note = cell!.querySelector('.rn-issue-none')
    expect(visible(note)).toBe('title not read · not kept on this run')
    expect(note!.getAttribute('title')).toContain('created before runs kept the title')
  })

  it('a read that failed says so, with its error in the title', async () => {
    const [cell] = await rows([issueRun({ plan: undefined, history: undefined, issue_read: null, issue_read_error: { code: 'forbidden', message: 'the forge refused the read' } })])
    // MUTATION: give a failed read the not-kept reason.
    const note = cell!.querySelector('.rn-issue-none')
    expect(visible(note)).toBe('title not read · the read failed')
    expect(note!.getAttribute('title')).toContain('the forge refused the read')
  })

  it('a run with its title leads with it and carries no note', async () => {
    const [cell] = await rows([issueRun({ plan: undefined, history: undefined, issue_read: READ })])
    expect(visible(cell!.querySelector('.rn-issue-t'))).toBe(READ.title)
    // MUTATION: draw the note on every row.
    expect(cell!.querySelector('.rn-issue-none')).toBeNull()
  })
})

describe('runs2-11: a plan without a mode says nothing of it', () => {
  it('draws no `mode not stated`, and keeps the estimate', async () => {
    serve((url) => (url === '/v1/runs/run_4c1e09d2' ? { run: issueRun({ plan: { ...FULL_PLAN, mode: undefined } }) } : null))
    const container = await mount('run=run_4c1e09d2')
    const facts = await waitFor(() => {
      const f = container.querySelector<HTMLElement>('.rn-plan-facts')
      expect(f).not.toBeNull()
      return f!
    }, WAIT)
    // MUTATION: restore the `mode not stated` note.
    expect(visible(facts)).not.toContain('mode not stated')
    expect(visible(facts)).toContain('estimate about 3 agent-hours')
  })
})
