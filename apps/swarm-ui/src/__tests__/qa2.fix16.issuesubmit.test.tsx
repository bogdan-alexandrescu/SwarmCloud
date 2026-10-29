// SUBMIT › FROM A GITHUB ISSUE, VISUAL QA PASS 2 (QA2-FIX-16; part of #2694).
//
//   runs2-08  the head counted pools nobody picks here: "6 runner profiles ·
//             20 pools you are admitted against". It is plain now: the
//             catalogue's size and the one runner an issue run takes.
//   runs2-34  the auto-merge switch was named by an aria-label beside a <b>
//             tied to nothing: `input.labels` was empty and a click on its
//             words did nothing. The words are its <label>.
//   runs2-35  the open runs were named by raw id: "run_4c1e09d2 (Planned)".
//             Each is its state and age, linked, the id in the title.
//
// MUTATION: put the pools back in `issueSubmitHead` -- the first case finds
// "pools". Put the aria-label back and the <b> outside a <label> -- the
// second finds no label. Print `{r.id}` as the link text again -- the third
// finds the id where "Planned 10d ago" should be.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

const admission = {
  units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [],
  counterfactual: [], complete: true, unread: [], uncapped: [],
}
const pool = (name: string) => ({
  name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10,
  active: 2, available: 8, enabled: true, updated_at: '2026-10-02T10:00:00Z',
})
const CAPACITY = {
  pools: [pool('global'), pool('tenant:u-bogdan')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
      pools: ['global', 'tenant:u-bogdan'], admission },
  },
  tenant_id: 'u-bogdan',
  generated_at: '2026-10-02T10:00:00Z',
}
const PROVIDERS = { tenant_id: 'u-bogdan', providers: [{ provider: 'anthropic', credential_registered: true }], generated_at: '2026-10-02T10:00:00Z' }

const ISSUE = {
  ref: 'example-org/agent-swarm-infra#512', owner: 'example-org', repo: 'agent-swarm-infra', number: 512,
  url: 'https://github.com/example-org/agent-swarm-infra/issues/512',
  repository_url: 'https://github.com/example-org/agent-swarm-infra',
}

function preview(top: Record<string, unknown> = {}) {
  return {
    issue: { ...ISSUE, title: 'A cost column', body: 'What?', body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 0 },
    tenant_id: 'eng',
    ...top,
  }
}

function run(id: string, state: string, over: Record<string, unknown> = {}) {
  return {
    id, tenant_id: 'u-bogdan', state, terminal: false, issue: ISSUE, plan_approval: 'required', auto_merge: false, fix_rounds: 3,
    planner_task_id: 'task_p', plan_digest: null, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-10-02T09:00:00Z', updated_at: '2026-10-02T09:00:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null, error: null,
    ...over,
  }
}

type Reply = { status: number; body: unknown }

function serve(previewReply: Reply, runsReply: Reply): { calls: string[] } {
  const served = { calls: [] as string[] }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    served.calls.push(`${method} ${url}`)
    const reply = (r: Reply) => new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
    if (url.startsWith('/v1/capacity')) return reply({ status: 200, body: CAPACITY })
    if (url.startsWith('/v1/providers')) return reply({ status: 200, body: PROVIDERS })
    if (url.startsWith('/v1/issues/preview')) return reply(previewReply)
    if (url.startsWith('/v1/runs') && method === 'GET') return reply(runsReply)
    return reply({ status: 404, body: { code: 'not_found', message: `no stub for ${url}` } })
  }) as unknown as typeof fetch
  return served
}

const RUNS = {
  status: 200,
  body: {
    runs: [
      run('run_4c1e09d2', 'PLANNED'),
      run('run_19ab77e0', 'RUNNING', { workflow_id: 'wf_8b21d0e4' }),
      run('run_0ld0ne00', 'DONE', { terminal: true }),
      run('run_e1sewhere', 'RUNNING', { issue: { ...ISSUE, ref: 'example-org/agent-swarm-infra#9', number: 9 } }),
    ],
    next_page_token: null,
    tenant_id: 'u-bogdan',
  },
}

async function mount(go = vi.fn()) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { IssueSubmitScreen } = await import('../IssueSubmit')
  const utils = render(<IssueSubmitScreen go={go} />)
  await screen.findByRole('heading', { name: 'Submit from a GitHub issue', level: 1 }, WAIT)
  await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
  return { ...utils, go }
}

async function readIssue(ref: string) {
  fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: ref } })
  fireEvent.click(screen.getByRole('button', { name: 'Read' }))
}


afterEach(() => {
  vi.unstubAllEnvs()
})

describe('runs2-08: a plain page head', () => {
  it('says how many runners there are and which one an issue run uses, and no pools', async () => {
    serve({ status: 200, body: preview() }, RUNS)
    const { container } = await mount()
    const head = visible(container)
    expect(head).toContain('1 runner · issue runs use claude-code')
    expect(head).not.toMatch(/pools you are admitted against/)
  })
})

describe('runs2-34: the auto-merge switch is labelled by its words', () => {
  it('has a <label> whose text names it, so the words toggle it', async () => {
    serve({ status: 200, body: preview() }, RUNS)
    await mount()
    const sw = screen.getByRole('switch') as HTMLInputElement
    expect(sw.labels?.length ?? 0, 'the switch has no <label>').toBe(1)
    expect(visible(sw.labels![0]!)).toBe('Merge the pull request when it is ready')
    expect(screen.getByLabelText('Merge the pull request when it is ready')).toBe(sw)
    // The issue reference is named too (its aria-label: the house style for a lone field).
    expect(screen.getByRole('textbox', { name: 'Issue reference' })).toBeTruthy()
  })
})

describe('runs2-35: open runs by state and age, linked', () => {
  it('names each open run "Planned 10d ago", with the id only in the link\'s title', async () => {
    const ago = (d: number) => new Date(Date.now() - d * 86_400_000).toISOString()
    const runs = {
      status: 200,
      body: {
        runs: [run('run_4c1e09d2', 'PLANNED', { created_at: ago(10) }), run('run_19ab77e0', 'RUNNING', { created_at: ago(3) })],
        next_page_token: null,
        tenant_id: 'u-bogdan',
      },
    }
    serve({ status: 200, body: preview() }, runs)
    const { container, go } = await mount()
    await readIssue('example-org/agent-swarm-infra#512')
    const open = await waitFor(() => {
      const el = container.querySelector('.in-open-runs')
      expect(el).not.toBeNull()
      return el as HTMLElement
    }, WAIT)
    expect(visible(open)).toBe('Open runs for this issue: Planned 10d ago, Running 3d ago. Planning it again starts another run.')
    const links = [...open.querySelectorAll('a')]
    expect(links.map((a) => a.textContent)).toEqual(['Planned 10d ago', 'Running 3d ago'])
    expect(links[0]!.getAttribute('title')).toBe('run_4c1e09d2')
    expect(links[0]!.getAttribute('href')).toBe('/runs/run_4c1e09d2')
    fireEvent.click(links[0]!, { button: 0 })
    expect(go).toHaveBeenCalledWith('work/runs?run=run_4c1e09d2')
  })
})
