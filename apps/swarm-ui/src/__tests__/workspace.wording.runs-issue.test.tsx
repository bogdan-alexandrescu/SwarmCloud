// WORKSPACE WORDING ON THE ISSUE PAGE AND THE RUN PAGES (visual QA VQ-056 and
// the runs-table findings, re-checked on main 2026-11-12).
//
//   1. The issue page's summary row said `tenant <id>` and a run's approval
//      banner `Anyone in tenant <id>`, beside a switcher that names the
//      workspace. Both say workspace now, named as the switcher names it.
//   2. The Runs table told two runs of one issue apart only by hex ids: a
//      run of an issue read more than once says which, `2/3`.
//   3. The rest of the member-facing copy on these pages says workspace.
//
// MUTATIONS: label the summary row `tenant` again, print the raw id in either
// place while a name is served, drop the run's number from the Run cell, or
// put back any phrase in OLD_COPY -- each goes red here.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'

import { runAttempts } from '../Runs'

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
const capacity = (tenant: string) => ({
  pools: [pool('global'), pool(`tenant:${tenant}`)],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
      pools: ['global', `tenant:${tenant}`], admission },
  },
  tenant_id: tenant,
  generated_at: '2026-10-02T10:00:00Z',
})

const ISSUE = {
  ref: 'example-org/example-repo#512', owner: 'example-org', repo: 'example-repo', number: 512,
  url: 'https://github.com/example-org/example-repo/issues/512',
  repository_url: 'https://github.com/example-org/example-repo',
}

function run(id: string, over: Record<string, unknown> = {}) {
  return {
    id, tenant_id: 'eng', state: 'PLANNED', terminal: false, issue: ISSUE, plan_approval: 'required', auto_merge: false,
    fix_rounds: 3, planner_task_id: 'task_p', plan: { summary: 'Plan it.', steps: [] }, plan_digest: 'sha256:' + 'ab'.repeat(32),
    plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-10-02T09:00:00Z', updated_at: '2026-10-02T09:00:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null, error: null,
    history: [], issue_read: null, issue_read_error: null,
    ...over,
  }
}

const ME_GROUP = { tenant: { tenant_id: 'eng', display_name: 'Engineering', kind: 'group' }, principal: { email: 'operator@example.com', domain: 'example.com', groups: [], is_admin: false } }
const ME_OWN = { tenant: { tenant_id: 'u-owner', display_name: null, kind: 'user' }, principal: { email: 'operator@example.com', domain: 'example.com', groups: [], is_admin: false } }

function serve(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const hit = Object.keys(routes).find((p) => url === p || url.startsWith(`${p}?`))
    return new Response(JSON.stringify(hit === undefined ? { code: 'not_found', message: `no stub for ${url}` } : routes[hit]), {
      status: hit === undefined ? 404 : 200, headers: JSON_HEADERS,
    })
  }) as unknown as typeof fetch
}

const fact = (c: HTMLElement, key: string) =>
  [...c.querySelectorAll('.in-send .ctl-fact')].find((li) => li.querySelector('b')?.textContent === key) as HTMLElement | undefined

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('the issue page names the workspace as the switcher does', () => {
  async function mount(routes: Record<string, unknown>) {
    serve(routes)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { IssueSubmitScreen } = await import('../IssueSubmit')
    const utils = render(<IssueSubmitScreen go={vi.fn()} />)
    await screen.findByRole('heading', { name: 'Submit from a GitHub issue', level: 1 }, WAIT)
    return utils
  }

  it('labels the summary row workspace and says its name, the id in the title', async () => {
    const { container } = await mount({ '/v1/capacity': capacity('eng'), '/v1/tenants/me': ME_GROUP })
    await waitFor(() => expect(visible(fact(container as HTMLElement, 'workspace')?.querySelector('code') ?? null)).toBe('Engineering'), WAIT)
    expect(fact(container as HTMLElement, 'workspace')!.querySelector('code')!.getAttribute('title')).toBe('eng')
    expect(fact(container as HTMLElement, 'tenant'), 'no row is labelled tenant').toBeUndefined()
  })

  it('says "personal" for the person\'s own, as the switcher\'s line under its name does', async () => {
    const { container } = await mount({ '/v1/capacity': capacity('u-owner'), '/v1/tenants/me': ME_OWN })
    await waitFor(() => expect(visible(fact(container as HTMLElement, 'workspace')?.querySelector('code') ?? null)).toBe('personal'), WAIT)
  })

  it('says the id while no name is served', async () => {
    const { container } = await mount({ '/v1/capacity': capacity('eng') })
    await waitFor(() => expect(visible(fact(container as HTMLElement, 'workspace')?.querySelector('code') ?? null)).toBe('eng'), WAIT)
  })
})

describe('a run\'s approval banner names the workspace', () => {
  async function mount(body: unknown, me: unknown) {
    serve({ '/v1/runs/run_a1': { run: body }, '/v1/tenants/me': me })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const utils = render(<RunsScreen view="run=run_a1" go={vi.fn()} />)
    await waitFor(() => expect(utils.container.querySelector('.rn-wait-t')).not.toBeNull(), WAIT)
    return utils
  }

  it('says "Anyone in workspace <name>", never tenant', async () => {
    const { container } = await mount(run('run_a1'), ME_GROUP)
    const line = () => visible(container.querySelector('.rn-wait-t'))
    await waitFor(() => expect(line()).toContain('Anyone in workspace Engineering may approve, edit or reject.'), WAIT)
    expect(line()).not.toMatch(/tenant/i)
  })

  it('says "your workspace" for the person\'s own, on an automatic approval too', async () => {
    const { container } = await mount(run('run_a1', { tenant_id: 'u-owner', plan_approval: 'auto' }), ME_OWN)
    await waitFor(() => expect(visible(container.querySelector('.rn-wait-t'))).toContain('Anyone in your workspace may act on it first.'), WAIT)
  })
})

describe('the Runs table tells two runs of one issue apart', () => {
  it('numbers each run of an issue read more than once, oldest first', () => {
    const other = { ...ISSUE, ref: 'example-org/example-repo#9' }
    const got = runAttempts([
      { id: 'run_c', issue: ISSUE, created_at: '2026-10-03T09:00:00Z' },
      { id: 'run_o', issue: other, created_at: '2026-10-02T12:00:00Z' },
      { id: 'run_b', issue: ISSUE, created_at: '2026-10-02T09:00:00Z' },
      { id: 'run_a', issue: ISSUE, created_at: '2026-10-01T09:00:00Z' },
    ])
    expect(got.get('run_a')).toEqual({ n: 1, of: 3 })
    expect(got.get('run_b')).toEqual({ n: 2, of: 3 })
    expect(got.get('run_c')).toEqual({ n: 3, of: 3 })
    expect(got.has('run_o'), 'an issue read once needs no number').toBe(false)
  })

  it('says "N/M" under the id in the Run cell, the sentence in its title', async () => {
    serve({
      '/v1/runs': {
        runs: [
          run('run_new', { created_at: '2026-10-03T09:00:00Z' }),
          run('run_only', { issue: { ...ISSUE, ref: 'example-org/example-repo#9', number: 9 } }),
          run('run_old', { created_at: '2026-10-01T09:00:00Z' }),
        ],
        next_page_token: null, tenant_id: 'eng',
      },
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    await waitFor(() => expect(container.querySelectorAll('.rn-row')).toHaveLength(3), WAIT)
    const cell = (id: string) => [...container.querySelectorAll('.rn-row td[data-label="Run"]')].find((td) => visible(td).startsWith(id))!
    const marker = (id: string) => cell(id).querySelector('.rn-attempt')
    expect(visible(marker('run_new'))).toBe('2/2')
    expect(visible(marker('run_old'))).toBe('1/2')
    expect(marker('run_new')!.getAttribute('title')).toBe('example-org/example-repo#512: run 2 of 2 read here, oldest first')
    expect(marker('run_only'), 'an issue read once needs no number').toBeNull()
    // The marker's words are paid for: "all 3 runs read", not "3 runs read · no older runs".
    expect(visible(container.querySelector('.rn-count'))).toBe('all 3 runs read')
  })
})

describe('the member-facing copy says workspace', () => {
  const OLD_COPY: [string, string][] = [
    ['IssueSubmit.tsx', 'pools this tenant is admitted'],
    ['IssueSubmit.tsx', 'this tenant&rsquo;s'],
    ['IssueSubmit.tsx', 'disabled for this tenant'],
    ['IssueSubmit.tsx', 'that this tenant can see'],
    ['IssueSubmit.tsx', 'Another tenant&rsquo;s'],
    ['IssueSubmit.tsx', "'This tenant has'"],
    ['IssueSubmit.tsx', '<>Tenant <code>'],
    ['Runs.tsx', 'This tenant has not planned'],
    ['Runs.tsx', 'for this tenant.'],
    ['Runs.tsx', 'Anyone in tenant'],
    ['Submit.tsx', 'pools this tenant is admitted'],
    ['Submit.tsx', 'a tenant this response does not name'],
    ['SubmitChooser.tsx', 'the tenant\\u2019s'],
    ['RepositoriesDetail.tsx', 'from the tenant default'],
    ['RepositoriesDetail.tsx', 'second member of the tenant'],
    ['RepositoriesDetail.tsx', 'Inherited from the tenant default'],
    ['RepositoriesDetail.tsx', 'nor the tenant token'],
    ['RepositoriesDetail.tsx', 'then tenant token'],
  ]
  it.each(OLD_COPY)('%s no longer says "%s"', (file, phrase) => {
    expect(readFileSync(join(__dirname, '..', file), 'utf8')).not.toContain(phrase)
  })
})
