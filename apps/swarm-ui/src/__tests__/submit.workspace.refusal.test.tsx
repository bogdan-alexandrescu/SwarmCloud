// THE SUBMISSION GATE'S TWO REFUSALS ON EVERY SUBMIT SCREEN (#847, lane W8;
// docs/workspaces.md §5.2, §6.1).
//
// WHAT EACH CASE HOLDS:
//   * a 403 WORKSPACE_NOT_READY and a 403 NO_CLAUDE_ACCOUNT, on the task, the
//     workflow and the issue screen, each render the API's `message` word for
//     word and a "Finish setup" link to the `setup_url` the API sent;
//   * neither refusal is drawn as a failure that "may exist anyway": the gate
//     runs before any write, so the banner says nothing was created;
//   * the form is never disabled ahead of time: the submit button is live
//     before the request, and the server is the one that refuses;
//   * any other 403 still draws the screen's own failure panel, not a banner;
//   * a setup_url that is not a path or https falls back to this console's
//     own Setup page, at the anchor the code names.
//
// The bodies are the shapes `workspaces.Workspaces.check` raises
// (tests/unit/control_plane/test_workspaces.py). Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { SubmitScreen } from '../Submit'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'
import { workspaceRefusal } from '../WorkspaceRefusal'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }

const NOT_READY_MESSAGE =
  "Your SwarmCloud workspace is waiting for an admin's approval, so no task or workflow can start. Finish setup: create your workspace."
const NO_ACCOUNT_MESSAGE = 'No Claude account yet. Add your own, or ask an admin to lend you one, and then try again.'
const CONSOLE = 'https://console.example.com'

const NOT_READY = {
  code: 'WORKSPACE_NOT_READY',
  message: NOT_READY_MESSAGE,
  detail: { workspace_id: 'w-3f9a2c', state: 'requested', setup_url: `${CONSOLE}/setup#workspace`, setup_command: '/sc:setup' },
}
const NO_ACCOUNT = {
  code: 'NO_CLAUDE_ACCOUNT',
  message: NO_ACCOUNT_MESSAGE,
  detail: { setup_url: `${CONSOLE}/setup#claude-account`, setup_command: '/sc:setup' },
}

const CASES = [
  { name: 'WORKSPACE_NOT_READY', body: NOT_READY, href: `${CONSOLE}/setup#workspace`, message: NOT_READY_MESSAGE },
  { name: 'NO_CLAUDE_ACCOUNT', body: NO_ACCOUNT, href: `${CONSOLE}/setup#claude-account`, message: NO_ACCOUNT_MESSAGE },
] as const

const realFetch = globalThis.fetch

/** Answer the one write with `reply`; every other request goes where it went before (the fixture build reads none). */
function refuseWrite(path: string, reply: { status: number; body: unknown }) {
  const calls: { method: string; url: string }[] = []
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    calls.push({ method, url })
    if (method === 'POST' && url.startsWith(path)) {
      return new Response(JSON.stringify(reply.body), { status: reply.status, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
  return calls
}

afterEach(() => {
  globalThis.fetch = realFetch
  vi.unstubAllEnvs()
})

function expectBanner(root: HTMLElement, code: string, message: string, href: string) {
  const banner = root.querySelector<HTMLElement>(`.ws-refusal[data-code="${code}"]`)
  expect(banner, `no ${code} banner`).not.toBeNull()
  expect(banner!.textContent).toContain(message)
  expect(banner!.textContent).toContain('Nothing was created.')
  const link = within(banner!).getByRole('link', { name: 'Finish setup' })
  expect(link.getAttribute('href')).toBe(href)
  // The banner replaces the write-failure panel: the gate created nothing.
  expect(root.textContent).not.toContain('may exist anyway')
}

describe('the task form', () => {
  for (const c of CASES) {
    it(`draws a 403 ${c.name} as the banner, with the form live before it`, async () => {
      const { container } = render(<SubmitScreen />)
      const go = (await screen.findByRole('button', { name: 'Submit one task' }, WAIT)) as HTMLButtonElement
      const radio = container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!
      fireEvent.click(radio)
      const calls = refuseWrite('/v1/tasks', { status: 403, body: c.body })
      expect(go.disabled, 'the form was disabled ahead of the server').toBe(false)
      fireEvent.click(go)
      await waitFor(() => expectBanner(container, c.name, c.message, c.href), WAIT)
      expect(calls.filter((x) => x.method === 'POST')).toHaveLength(1)
    })
  }

  it('keeps drawing any other 403 as the failure panel, not a banner', async () => {
    const { container } = render(<SubmitScreen />)
    const go = (await screen.findByRole('button', { name: 'Submit one task' }, WAIT)) as HTMLButtonElement
    fireEvent.click(container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="mock"]')!)
    refuseWrite('/v1/tasks', { status: 403, body: { code: 'forbidden', message: 'Refused for another reason.' } })
    fireEvent.click(go)
    await waitFor(() => expect(container.textContent).toContain('Refused for another reason.'), WAIT)
    expect(container.querySelector('.ws-refusal')).toBeNull()
  })
})

describe('the workflow form', () => {
  for (const c of CASES) {
    it(`draws a 403 ${c.name} as the banner, with the form live before it`, async () => {
      const { container } = render(<SubmitWorkflowScreen />)
      const go = (await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)) as HTMLButtonElement
      const select = within(container.querySelector<HTMLElement>('.wfb-step')!).getByRole('combobox', { name: 'step-1 runner' })
      fireEvent.change(select, { target: { value: 'mock' } })
      const calls = refuseWrite('/v1/workflows', { status: 403, body: c.body })
      expect(go.disabled, 'the form was disabled ahead of the server').toBe(false)
      fireEvent.click(go)
      await waitFor(() => expectBanner(container, c.name, c.message, c.href), WAIT)
      expect(calls.filter((x) => x.method === 'POST')).toHaveLength(1)
    })
  }
})

// The issue screen reads through the live client, as intake.issue.test.tsx does.
const admission = { units: 1, headroom: 4, basis: 'measured', blockers: [], binding: [], counterfactual: [], complete: true, unread: [], uncapped: [] }
const pool = (name: string) => ({
  name, hard_limit: 10, adaptive_target: null, quota_derived_limit: null, effective_limit: 10,
  active: 2, available: 8, enabled: true, updated_at: '2026-10-02T10:00:00Z',
})
const CAPACITY = {
  pools: [pool('global'), pool('tenant:eng'), pool('resource:standard')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng', 'resource:standard'], admission },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-02T10:00:00Z',
}
const PREVIEW = {
  issue: {
    ref: 'example-org/infra#512', owner: 'example-org', repo: 'infra', number: 512,
    url: 'https://github.com/example-org/infra/issues/512', repository_url: 'https://github.com/example-org/infra',
    title: 'The workflow list shows no cost column', body: 'See what a finished workflow cost.',
    body_truncated: false, body_redacted: false, labels: [], state: 'open', comments: 0,
  },
  tenant_id: 'eng',
}

function serveIssue(runBody: unknown) {
  const posts: string[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const reply = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: JSON_HEADERS })
    if (url.startsWith('/v1/capacity')) return reply(200, CAPACITY)
    if (url.startsWith('/v1/providers')) return reply(200, { tenant_id: 'eng', providers: [{ provider: 'anthropic', credential_registered: true }], generated_at: '2026-10-02T10:00:00Z' })
    if (url.startsWith('/v1/issues/preview')) return reply(200, PREVIEW)
    if (url.startsWith('/v1/runs') && method === 'POST') {
      posts.push(url)
      return reply(403, runBody)
    }
    return reply(404, { code: 'not_found', message: `no stub for ${url}` })
  }) as unknown as typeof fetch
  return posts
}

describe('the issue screen', () => {
  for (const c of CASES) {
    it(`draws a 403 ${c.name} as the banner, with the form live before it`, async () => {
      const posts = serveIssue(c.body)
      vi.stubEnv('VITE_LIVE', '1')
      vi.resetModules()
      const { IssueSubmitScreen } = await import('../IssueSubmit')
      const { container } = render(<IssueSubmitScreen go={vi.fn()} />)
      await screen.findByRole('radiogroup', { name: 'runner' }, WAIT)
      fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: 'example-org/infra#512' } })
      fireEvent.click(screen.getByRole('button', { name: 'Read' }))
      const plan = screen.getByRole('button', { name: 'Plan this issue' }) as HTMLButtonElement
      await waitFor(() => expect(plan.disabled, 'the form was disabled ahead of the server').toBe(false), WAIT)
      fireEvent.click(plan)
      await waitFor(() => expectBanner(container, c.name, c.message, c.href), WAIT)
      expect(posts).toHaveLength(1)
    })
  }
})

describe('the link is the API\'s, or this console\'s Setup page', () => {
  const error = (detail: unknown, code = 'WORKSPACE_NOT_READY') => ({ kind: 'forbidden' as const, httpStatus: 403, code, message: 'm', detail })

  it('takes a path or an https address as sent', () => {
    expect(workspaceRefusal(error({ setup_url: '/setup#workspace' }))?.setupUrl).toBe('/setup#workspace')
    expect(workspaceRefusal(error({ setup_url: `${CONSOLE}/setup#workspace` }))?.setupUrl).toBe(`${CONSOLE}/setup#workspace`)
  })

  it('falls back to the Setup page at the code\'s anchor for anything else', () => {
    expect(workspaceRefusal(error({ setup_url: 'javascript:alert(1)' }))?.setupUrl).toBe('/setup#workspace')
    expect(workspaceRefusal(error({ setup_url: '//elsewhere.example.com/x' }))?.setupUrl).toBe('/setup#workspace')
    expect(workspaceRefusal(error(undefined, 'NO_CLAUDE_ACCOUNT'))?.setupUrl).toBe('/setup#claude-account')
  })

  it('is no refusal of the gate\'s on another status or code', () => {
    expect(workspaceRefusal({ ...error({}), httpStatus: 409 })).toBeNull()
    expect(workspaceRefusal(error({}, 'forbidden'))).toBeNull()
    expect(workspaceRefusal(null)).toBeNull()
  })
})
