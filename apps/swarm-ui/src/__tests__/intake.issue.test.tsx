// /submit/issue (intake-tenants.html 1A, picked 2026-10-02), against the live
// API shapes of #511: `GET /v1/issues/preview` (routes/issues.py, forge.py)
// and `POST /v1/runs` (routes/runs.py, schemas.RunCreate).
//
// WHAT EACH CASE HOLDS:
//   * the issue is SHOWN before anything is created -- title, body as served,
//     labels, open/closed, comment count, the canonical link;
//   * each refusal the preview route serves has its own form, keyed on the
//     server's `code` (forge.py: not_found, no_access, is_pull_request,
//     no_forge_credential, read_failed; validation_failed for a malformed
//     reference), and none of them prints a figure it did not read;
//   * a CLOSED issue warns and needs "Plan it anyway", never a refusal;
//   * auto-merge is drawn OFF and DISABLED with #295 (the API refuses it);
//   * fix rounds are 3 by default within 1-5;
//   * the body POSTed is exactly RunCreate's fields, and the page lands on the
//     new run.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { expectNoFigures } from './setup'

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
  pools: [pool('global'), pool('tenant:eng'), pool('resource:standard')],
  runner_profiles: {
    'claude-code': { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
      pools: ['global', 'tenant:eng', 'resource:standard'], admission },
    codex: { resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'openai', units: 1,
      pools: ['global', 'tenant:eng', 'resource:standard'], admission },
  },
  tenant_id: 'eng',
  generated_at: '2026-10-02T10:00:00Z',
}
const PROVIDERS = {
  tenant_id: 'eng',
  providers: [{ provider: 'anthropic', credential_registered: true }],
  generated_at: '2026-10-02T10:00:00Z',
}

function preview(over: Record<string, unknown> = {}) {
  return {
    issue: {
      ref: 'example-org/infra#512', owner: 'example-org', repo: 'infra', number: 512,
      url: 'https://github.com/example-org/infra/issues/512',
      repository_url: 'https://github.com/example-org/infra',
      title: 'The workflow list shows no cost column',
      body: 'What are you trying to do?\nSee what a finished workflow cost.',
      body_truncated: false, body_redacted: false,
      labels: ['enhancement', 'ui'], state: 'open', comments: 4,
      ...over,
    },
    tenant_id: 'eng',
  }
}

type Reply = { status: number; body: unknown }

interface Served {
  calls: { method: string; url: string; body: unknown }[]
}

function serve(previewReply: Reply, runReply: Reply = { status: 201, body: { run: { id: 'run_new' } } }): Served {
  const served: Served = { calls: [] }
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    served.calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : null })
    const reply = (r: Reply) => new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
    if (url.startsWith('/v1/capacity')) return reply({ status: 200, body: CAPACITY })
    if (url.startsWith('/v1/providers')) return reply({ status: 200, body: PROVIDERS })
    if (url.startsWith('/v1/issues/preview')) return reply(previewReply)
    if (url.startsWith('/v1/runs') && method === 'POST') return reply(runReply)
    return reply({ status: 404, body: { code: 'not_found', message: `no stub for ${url}` } })
  }) as unknown as typeof fetch
  return served
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

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('step 1: the issue is read and shown before anything is created', () => {
  it('shows title, body, labels, state, comments and the link, read through the preview route', async () => {
    const served = serve({ status: 200, body: preview() })
    const { container } = await mount()
    expect(planButton().disabled, 'the button is live before any issue was read').toBe(true)
    await readIssue('example-org/infra#512')
    const card = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.in-preview')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const asked = served.calls.find((c) => c.url.startsWith('/v1/issues/preview'))!
    expect(new URL(asked.url, 'http://x').searchParams.get('issue')).toBe('example-org/infra#512')
    expect(visible(card.querySelector('h3'))).toBe('The workflow list shows no cost column')
    expect(visible(card)).toContain('See what a finished workflow cost.')
    expect(visible(card)).toContain('open')
    expect(visible(card)).toContain('4 comments')
    // The issue's labels are canonical tags: labels someone attached.
    const chips = [...card.querySelectorAll('.c-tag')].map(visible)
    expect(chips).toEqual(['enhancement', 'ui'])
    const link = within(card).getByRole('link', { name: /example-org\/infra#512/ })
    expect(link.getAttribute('href')).toBe('https://github.com/example-org/infra/issues/512')
    expect(planButton().disabled).toBe(false)
  })

  it('says when the body was cut or masked, as the API flagged it', async () => {
    serve({ status: 200, body: preview({ body_truncated: true, body_redacted: true }) })
    const { container } = await mount()
    await readIssue('example-org/infra#512')
    const card = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.in-preview')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(card)).toMatch(/first part of the body/)
    expect(visible(card)).toMatch(/masked/)
  })

  it('a closed issue warns and needs "Plan it anyway"; it is never refused', async () => {
    serve({ status: 200, body: preview({ state: 'closed' }) })
    const { container } = await mount()
    await readIssue('example-org/infra#512')
    const warn = await screen.findByText(/This issue is closed\./, undefined, WAIT)
    expect(container.querySelector('.in-refusal'), 'a closed issue was drawn as a refusal').toBeNull()
    expect(planButton().disabled, 'a closed issue can be planned without saying so').toBe(true)
    fireEvent.click(within(warn.closest('.in-closed') as HTMLElement).getByRole('button', { name: 'Plan it anyway' }))
    expect(planButton().disabled).toBe(false)
  })

  const REFUSALS: Array<[string, number, RegExp]> = [
    ['not_found', 404, /not found or not visible/i],
    ['no_access', 403, /forge credential was refused/i],
    ['is_pull_request', 422, /is a pull request/i],
    ['no_forge_credential', 409, /no forge credential/i],
    ['read_failed', 502, /did not finish/i],
    ['validation_failed', 422, /not an issue reference/i],
  ]
  for (const [code, status, words] of REFUSALS) {
    it(`draws the ${code} refusal in its own words, with no figure it did not read`, async () => {
      serve({ status, body: { code, message: `server says ${code}` } })
      const { container } = await mount()
      await readIssue('example-org/infra#512')
      const refusal = await waitFor(() => {
        const el = container.querySelector<HTMLElement>('.in-refusal')
        expect(el).not.toBeNull()
        return el!
      }, WAIT)
      expect(refusal.getAttribute('data-code')).toBe(code)
      expect(visible(refusal)).toMatch(words)
      // The server's own sentence is kept: it names the tenant and the secret.
      expect(visible(refusal)).toContain(`server says ${code}`)
      expect(container.querySelector('.in-preview')).toBeNull()
      expect(planButton().disabled).toBe(true)
      // No count, no state: nothing was read. `#295` and the reference's own
      // number are the only digits on the page that are not a measurement.
      expectNoFigures(refusal, ['512', `server says ${code}`])
    })
  }

  it('a pull request points at the task form', async () => {
    serve({ status: 422, body: { code: 'is_pull_request', message: 'a pull request' } })
    const { go } = await mount()
    await readIssue('example-org/infra#498')
    fireEvent.click(await screen.findByRole('button', { name: 'Submit a task instead' }, WAIT))
    expect(go).toHaveBeenCalledWith('work/new')
  })

  it('a failed read offers to read again, and says the state is unknown', async () => {
    const served = serve({ status: 502, body: { code: 'read_failed', message: 'GitHub answered HTTP 500' } })
    await mount()
    await readIssue('example-org/infra#512')
    const again = await screen.findByRole('button', { name: 'Read again' }, WAIT)
    expect(visible(again.closest('.in-refusal'))).toMatch(/unknown/)
    fireEvent.click(again)
    await waitFor(() => expect(served.calls.filter((c) => c.url.startsWith('/v1/issues/preview'))).toHaveLength(2), WAIT)
  })

  it('a reference edited after its read must be read again', async () => {
    serve({ status: 200, body: preview() })
    const { container } = await mount()
    await readIssue('example-org/infra#512')
    await waitFor(() => expect(container.querySelector('.in-preview')).not.toBeNull(), WAIT)
    fireEvent.change(screen.getByLabelText('Issue reference'), { target: { value: 'example-org/infra#513' } })
    expect(container.querySelector('.in-preview'), 'the old issue stayed under a new reference').toBeNull()
    expect(planButton().disabled).toBe(true)
  })
})

describe('steps 2 and 3: the runner and what it may do on its own', () => {
  it('offers claude-code by name and holds every other runner back with the reason', async () => {
    serve({ status: 200, body: preview() })
    await mount()
    const group = screen.getByRole('radiogroup', { name: 'runner' })
    const cc = group.querySelector<HTMLInputElement>('input[value="claude-code"]')!
    expect(cc.checked).toBe(true)
    // ONLY THE USABLE CHOICE (walkthrough D): the rest are behind "Other
    // runners", each with a one-line reason, and none is a radio.
    expect(group.querySelector('input[value="codex"]')).toBeNull()
    const more = document.querySelector('details.sbf-runners-more')!
    const codex = [...more.querySelectorAll('li')].find((li) => li.querySelector('.sbf-runner-name')?.textContent === 'codex')!
    expect(codex, 'codex is not listed as unavailable').toBeTruthy()
    expect(codex.querySelector('.sbf-runner-why')?.textContent).toMatch(/^(Issue runs always use claude-code|Not enabled yet\. Use claude-code)$/)
  })

  it('plan approval defaults to Required; auto-merge is off and disabled until #295; fix rounds 3 in 1-5', async () => {
    serve({ status: 200, body: preview() })
    await mount()
    const required = screen.getByRole('radio', { name: /Required/ }) as HTMLInputElement
    const auto = screen.getByRole('radio', { name: /Auto/ }) as HTMLInputElement
    expect(required.checked).toBe(true)
    expect(auto.checked).toBe(false)
    const merge = screen.getByRole('switch', { name: 'Merge the pull request when it is ready' }) as HTMLInputElement
    expect(merge.checked).toBe(false)
    expect(merge.disabled).toBe(true)
    // No issue number on the form (walkthrough E): the why is the line's tooltip.
    expect(visible(merge.closest('.in-merge'))).toContain('Not available yet')
    expect(merge.closest('.in-merge')!.querySelector('small')!.getAttribute('title')).toMatch(/merge chain has not shipped/)
    const rounds = screen.getByLabelText('Fix rounds when checks go red') as HTMLInputElement
    expect(rounds.value).toBe('3')
    expect(rounds.min).toBe('1')
    expect(rounds.max).toBe('5')
  })

  it('the summary repeats the choices', async () => {
    serve({ status: 200, body: preview() })
    const { container } = await mount()
    const side = () => visible(container.querySelector('.in-send'))
    expect(side()).toContain('Not ready to send')
    await readIssue('example-org/infra#512')
    await waitFor(() => expect(side()).toContain('Ready to send'), WAIT)
    expect(side()).toContain('example-org/infra#512')
    expect(side()).toContain('claude-code')
    expect(side()).toContain('waits for approval')
    expect(side()).toContain('off · not available yet')
    expect(side()).toContain('up to 3')
    fireEvent.click(screen.getByRole('radio', { name: /Auto/ }))
    fireEvent.change(screen.getByLabelText('Fix rounds when checks go red'), { target: { value: '5' } })
    expect(side()).toContain('runs straight on')
    expect(side()).toContain('up to 5')
  })
})

describe('submit creates the run', () => {
  it('POSTs RunCreate exactly and lands on the run', async () => {
    const served = serve({ status: 200, body: preview() })
    const { go } = await mount()
    await readIssue('https://github.com/example-org/infra/issues/512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    fireEvent.change(screen.getByLabelText('Fix rounds when checks go red'), { target: { value: '2' } })
    fireEvent.click(planButton())
    await waitFor(() => expect(go).toHaveBeenCalledWith('work/runs?run=run_new'), WAIT)
    const post = served.calls.find((c) => c.method === 'POST')!
    expect(post.url).toBe('/v1/runs')
    // The reference AS THE PREVIEW SERVED IT, so what is planned is what was shown.
    expect(post.body).toEqual({ issue: 'example-org/infra#512', plan_approval: 'required', auto_merge: false, fix_rounds: 2 })
  })

  it('a fix-round count outside 1-5 is not sent', async () => {
    const served = serve({ status: 200, body: preview() })
    await mount()
    await readIssue('example-org/infra#512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    fireEvent.change(screen.getByLabelText('Fix rounds when checks go red'), { target: { value: '9' } })
    expect(planButton().disabled).toBe(true)
    expect(served.calls.some((c) => c.method === 'POST')).toBe(false)
  })

  it('a refused submission says so and does not navigate', async () => {
    serve({ status: 200, body: preview() },
      { status: 422, body: { code: 'auto_merge_unavailable', message: 'auto_merge requires the merge chain (#295)' } })
    const { go } = await mount()
    await readIssue('example-org/infra#512')
    await waitFor(() => expect(planButton().disabled).toBe(false), WAIT)
    fireEvent.click(planButton())
    await screen.findByText(/auto_merge requires the merge chain/, undefined, WAIT)
    expect(go).not.toHaveBeenCalled()
  })
})
