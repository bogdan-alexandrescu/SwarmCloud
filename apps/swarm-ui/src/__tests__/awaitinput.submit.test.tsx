/**
 * LANE HW-UI (docs/design/human-steps.md §4.1, §4.8): composing an
 * `await-input` step on Submit › Workflow.
 *
 *   * DARK WHILE UNAVAILABLE: the catalogue serves `await-input` unavailable
 *     until the owner applies request D, and the runner select offers it
 *     refused, so nothing about the form changes while it is;
 *   * once offered, the step's `await_input` block is composed -- the form,
 *     the files, the expiry in hours, what happens at expiry -- and sent on
 *     that step only, in seconds, as the API takes it;
 *   * a wait over 24 hours (Q4) is refused by the form, before a round trip.
 *
 * Each case fails without the change. Placeholders only.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity } from '../types'

const mocks = vi.hoisted(() => ({ loadCapacity: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  mocks.loadCapacity.mockImplementation(actual.loadCapacity)
  // The submit answers through fetch here, not the fixture build's 201
  // (QA2-FIX-07, runs2-01 and runs2-02).
  return { ...actual, loadCapacity: mocks.loadCapacity, USE_FIXTURES: false }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')

const WAIT = { timeout: 4000 }
const realFetch = globalThis.fetch

async function capacity(available: boolean): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  // The development fixture's catalogue predates the entry (request C): it is
  // added here as the catalogue serves it, a control-plane profile with no input.
  const base = data.runner_profiles['mock']
  if (base === undefined) throw new Error('the fixture catalogue has no mock entry to shape the entry from')
  data.runner_profiles['await-input'] = {
    ...structuredClone(base),
    available,
    disabled_reason: available ? null : 'await-input steps are being built (docs/design/human-steps.md)',
    input_contract: { required_keys: [] },
  } as unknown as (typeof data.runner_profiles)[string]
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/workflows')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      return new Response(JSON.stringify({ workflow: { workflow_id: 'wf_await_1', steps: [], created_at: '2026-10-18T12:00:00Z' }, dispatch: null }), {
        status: 201,
        headers: { 'content-type': 'application/json' },
      })
    }
    throw new Error(`unexpected request ${String(input)}`)
  }) as unknown as typeof fetch
  return posts
}

async function form(): Promise<HTMLElement> {
  const { container } = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
  return container
}

const firstStep = (root: HTMLElement) => root.querySelector<HTMLElement>('.wfb-step')!
const runner = (root: HTMLElement) => firstStep(root).querySelector<HTMLSelectElement>('select.sb-runner-select')!
const submit = () => screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('while await-input is unavailable', () => {
  beforeEach(async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(false))
  })

  it('offers it refused, and draws no await_input fields', async () => {
    const root = await form()
    const option = Array.from(runner(root).options).find((o) => o.value === 'await-input')
    expect(option, 'the catalogue entry is drawn').toBeTruthy()
    expect(option!.disabled).toBe(true)
    expect(root.querySelector('[data-testid="wfb-await"]')).toBeNull()
  })
})

describe('once await-input is offered', () => {
  beforeEach(async () => {
    mocks.loadCapacity.mockResolvedValue(await capacity(true))
  })

  it('composes the await_input block and sends it on that step, in seconds', async () => {
    const posts = capturePosts()
    const root = await form()
    fireEvent.change(runner(root), { target: { value: 'await-input' } })
    const fields = await waitFor(() => {
      const f = root.querySelector<HTMLElement>('[data-testid="wfb-await"]')
      expect(f).toBeTruthy()
      return f!
    })
    fireEvent.change(screen.getByLabelText('expires after, in hours (24 at most)'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('files with the answer (0 offers no upload)'), { target: { value: '0' } })
    fireEvent.change(screen.getByLabelText('if nobody answers'), { target: { value: 'default' } })
    fireEvent.change(screen.getByLabelText('the default verdict'), { target: { value: 'NOT_YET' } })
    expect(fields.textContent).toContain('any member may answer')
    await waitFor(() => expect(submit().disabled).toBe(false))
    fireEvent.click(submit())
    await waitFor(() => expect(posts.length).toBe(1), WAIT)
    const steps = posts[0]!.steps as Array<Record<string, unknown>>
    expect(steps[0]!.runner_profile).toBe('await-input')
    expect(steps[0]!.await_input).toEqual({
      form: 'verdict',
      max_files: 0,
      expires_after_seconds: 7200,
      on_expire: 'default',
      default: { verdict: 'NOT_YET', findings: [] },
    })
  })

  it('refuses a wait over 24 hours before sending', async () => {
    const posts = capturePosts()
    const root = await form()
    fireEvent.change(runner(root), { target: { value: 'await-input' } })
    await waitFor(() => expect(root.querySelector('[data-testid="wfb-await"]')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('expires after, in hours (24 at most)'), { target: { value: '25' } })
    await waitFor(() => expect(root.textContent).toContain('24 hours at most'))
    expect(submit().disabled).toBe(true)
    fireEvent.click(submit())
    expect(posts.length).toBe(0)
  })
})
