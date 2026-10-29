// SUBMIT › FROM A GITHUB ISSUE, VISUAL QA PASS 1 (QA1-FIX-12; part of #2596).
//
//   runs-submit-13  the summary's tenant read the capacity read's before the
//                   issue was read and the preview's after: two tenants for
//                   one submission. One source throughout.
//   runs-submit-14  an issue with a Planned and a Running run was offered
//                   "Plan this issue" as if new. Its open runs are listed,
//                   linked, before the button.
//   chrome-C29      "Not available yet. A person merges the run's pull
//                   request" was drawn before any issue was read, and its
//                   tooltip said the merge chain had not shipped -- while
//                   issue runs merge on their own. Before a read the switch
//                   says it is decided by the read; after, it says what the
//                   API answered.
//   runs-submit-23  with an unparseable reference the summary said "read one
//                   first" beside a Read that cannot read it.
//   runs-submit-24  the summary's repository was cut with an ellipsis; it
//                   wraps now.
//
// MUTATION: put `read?.tenant_id ?? capacity.tenant_id` back -- the first
// case reads `eng` after the read. Drop the open-runs read -- the second
// finds no run_4c1e09d2 link before the button. Put "Not available yet" back
// in the no-read branch -- the fourth finds it. Drop the `in-repo` class or
// its rule -- the last fails.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

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

const planButton = () => screen.getByRole('button', { name: 'Plan this issue' }) as HTMLButtonElement
const fact = (c: HTMLElement, key: string) =>
  [...c.querySelectorAll('.in-send .ctl-fact')].find((li) => li.querySelector('b')?.textContent === key) as HTMLElement
/** A fact as read: its key, a space, its value. */
const factText = (c: HTMLElement, key: string) =>
  [...fact(c, key).childNodes].map((n) => n.textContent ?? '').join(' ').replace(/\s+/g, ' ').trim()

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('runs-submit-13: one tenant before and after the read', () => {
  it('keeps the tenant the page was read for, whatever the preview names', async () => {
    serve({ status: 200, body: preview() }, RUNS)
    const { container } = await mount()
    expect(factText(container as HTMLElement, 'tenant')).toBe('tenant u-bogdan')
    await readIssue('example-org/agent-swarm-infra#512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    expect(factText(container as HTMLElement, 'tenant'), 'the tenant changed with the read').toBe('tenant u-bogdan')
  })
})

describe('runs-submit-14: an issue with open runs says so before the button', () => {
  it('lists the issue\'s open runs, linked, with their state, and not its finished or other issues\' runs', async () => {
    const { go, container } = await (async () => {
      serve({ status: 200, body: preview() }, RUNS)
      return mount()
    })()
    await readIssue('example-org/agent-swarm-infra#512')
    const open = await waitFor(() => {
      const el = container.querySelector('.in-open-runs')
      expect(el, 'nothing says the issue already has open runs').not.toBeNull()
      return el as HTMLElement
    }, WAIT)
    // By state and age since QA2-FIX-16 (runs2-35); the id is the link's title.
    expect(visible(open)).toMatch(/^Open runs for this issue: Planned \S+ ago, Running \S+ ago\./)
    expect(visible(open)).not.toContain('run_0ld0ne00')
    expect(visible(open)).not.toContain('run_e1sewhere')
    // Before the button, in reading order.
    expect(open.compareDocumentPosition(planButton()) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const link = [...open.querySelectorAll('a')].find((a) => a.getAttribute('title') === 'run_4c1e09d2')!
    expect(link.getAttribute('href')).toBe('/runs/run_4c1e09d2')
    fireEvent.click(link, { button: 0 })
    expect(go).toHaveBeenCalledWith('work/runs?run=run_4c1e09d2')
  })

  it('says nothing when the issue has no open run', async () => {
    serve({ status: 200, body: preview() }, { status: 200, body: { runs: [], next_page_token: null, tenant_id: 'u-bogdan' } })
    const { container } = await mount()
    await readIssue('example-org/agent-swarm-infra#512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    await new Promise((r) => setTimeout(r, 50))
    expect(container.querySelector('.in-open-runs')).toBeNull()
    expect(container.querySelector('.in-open-runs-unread')).toBeNull()
  })

  it('says it could not check when the runs read fails: unknown, never "none"', async () => {
    serve({ status: 200, body: preview() }, { status: 500, body: { code: 'internal', message: 'runs read broke' } })
    const { container } = await mount()
    await readIssue('example-org/agent-swarm-infra#512')
    await waitFor(() => expect(container.querySelector('.in-open-runs-unread')).not.toBeNull(), WAIT)
    expect(visible(container.querySelector('.in-open-runs-unread'))).toMatch(/^Whether this issue already has open runs was not checked/)
  })
})

describe('chrome-C29: auto-merge says what the API answered, and nothing before it answers', () => {
  it('before a read, says the read decides it, with no "not available yet" or "has not shipped"', async () => {
    serve({ status: 200, body: preview() }, RUNS)
    const { container } = await mount()
    const merge = container.querySelector('.in-merge')!
    expect(visible(merge)).not.toMatch(/not available yet/i)
    expect(merge.querySelector('small')!.getAttribute('title') ?? '').not.toMatch(/has not shipped/)
    expect(visible(merge)).toContain('Decided when the issue is read')
    expect(factText(container as HTMLElement, 'auto-merge')).toBe('auto-merge off · decided when the issue is read')
  })

  it('after a read the API refused it on, says refused with the API\'s reason', async () => {
    const reason = 'the merge profile is disabled'
    serve({ status: 200, body: preview({ auto_merge: { available: false, requires: '#295', reason } }) }, RUNS)
    const { container } = await mount()
    await readIssue('example-org/agent-swarm-infra#512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    const merge = container.querySelector('.in-merge')!
    expect(visible(merge)).toContain('Not available for this run')
    expect(visible(merge)).toContain(reason)
    expect(factText(container as HTMLElement, 'auto-merge')).toBe('auto-merge off · refused by the API')
  })
})

describe('runs-submit-23: an unparseable reference', () => {
  it('leaves Read disabled and the summary names the reference, not "read one first"', async () => {
    serve({ status: 200, body: preview() }, RUNS)
    const { container } = await mount()
    fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: 'hello world' } })
    expect((screen.getByRole('button', { name: 'Read' }) as HTMLButtonElement).disabled).toBe(true)
    expect(factText(container as HTMLElement, 'issue')).toBe('issue — not an issue reference yet')
  })
})

describe('runs-submit-24: the repository wraps in the summary', () => {
  it('carries the wrapping class, whose rule overrides the one-line cut', async () => {
    serve({ status: 200, body: preview() }, RUNS)
    const { container } = await mount()
    await readIssue('example-org/agent-swarm-infra#512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    const code = fact(container as HTMLElement, 'repository').querySelector('code')!
    expect(code.className).toContain('in-repo')
    expect(code.getAttribute('title')).toBe('example-org/agent-swarm-infra')
    const css = readFileSync(join(__dirname, '..', 'styles', 'intake.css'), 'utf8')
    const rule = /\.in-send \.ctl-fact code\.in-repo\s*\{([^}]*)\}/.exec(css)
    expect(rule, 'no rule for the repository value').not.toBeNull()
    expect(rule![1]).toMatch(/white-space:\s*normal/)
    expect(rule![1]).toMatch(/overflow-wrap:\s*anywhere/)
  })
})
