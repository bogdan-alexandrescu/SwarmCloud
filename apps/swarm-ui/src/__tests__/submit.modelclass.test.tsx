// MODEL-UI (docs/design/model-per-step.md §4.8, part of #1435): a step picks
// its model class BY NAME, from a select that offers only the classes its
// runner accepts (read from `/v1/runtimes`), defaults to the runner's default,
// labels each class with the model this deployment pins for it, and is not
// drawn at all for a runner that runs no Claude model (`codex`).
//
// The request carries `model_class` only when someone chose a class other
// than the default: naming the default stores nothing at the API (§4.3), and
// leaving it out keeps the request a console without this select would send.
// Never a model ID (invariant 10).
//
// BREAK IT: offer every class in the vocabulary rather than the runner's;
// draw the select for `codex`; send the default; send the model ID instead of
// the class -- each case fails.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { ModelClassOffer } from '../api'
import type { Result } from '../fetch'
import type { Capacity } from '../types'

const mocks = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadModelClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // The submit answers through fetch here, not the fixture build's 201
  // (QA2-FIX-07, runs2-01 and runs2-02).
  return { ...actual, loadCapacity: mocks.loadCapacity, loadModelClasses: mocks.loadModelClasses, USE_FIXTURES: false }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')
const { ModelClassSelect } = await import('../RunnerPicker')

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const PINS = {
  opus: 'claude-opus-5-5',
  fable: 'claude-fable-5-1',
  sonnet: 'claude-sonnet-5-5',
  haiku: 'claude-haiku-5-5',
}

/** `claude-code` accepts all four; `browser` only two; `codex` none. */
const OFFERS: Record<string, ModelClassOffer> = {
  'claude-code': { model_classes: ['opus', 'fable', 'sonnet', 'haiku'], default_model_class: 'opus', models: PINS },
  browser: { model_classes: ['opus', 'haiku'], default_model_class: 'opus', models: { opus: PINS.opus, haiku: PINS.haiku } },
  codex: { model_classes: [], default_model_class: null, models: {} },
}

/** The fixture's capacity, with `codex` and `browser` available so a step can pick them. */
async function capacity(): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  for (const name of ['codex', 'browser']) {
    const p = data.runner_profiles[name]! as unknown as Record<string, unknown>
    p.available = true
    p.disabled_reason = ''
  }
  return ok(data)
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/workflows')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      return new Response(JSON.stringify({
        workflow: { workflow_id: 'wf_class_1', steps: [], created_at: '2026-10-19T12:00:00Z' },
        dispatch: null,
      }), { status: 201, headers: JSON_HEADERS })
    }
    throw new Error(`unexpected request ${String(input)}`)
  }) as unknown as typeof fetch
  return posts
}

const stepCards = (root: HTMLElement) => Array.from(root.querySelectorAll<HTMLElement>('.wfb-step'))
const classSelect = (step: HTMLElement) => step.querySelector<HTMLSelectElement>('select.sb-model-class')

function choose(root: HTMLElement, i: number, profile: string, prompt?: string) {
  fireEvent.change(stepCards(root)[i]!.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: profile } })
  const area = stepCards(root)[i]!.querySelector<HTMLTextAreaElement>('textarea')
  if (prompt !== undefined && area !== null) fireEvent.change(area, { target: { value: prompt } })
}

async function form(): Promise<HTMLElement> {
  const { container } = render(<SubmitWorkflowScreen />)
  await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
  return container
}

const go = () => screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement

beforeEach(async () => {
  mocks.loadCapacity.mockResolvedValue(await capacity())
  mocks.loadModelClasses.mockResolvedValue(ok(OFFERS))
})

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the class select offers only what the runner accepts', () => {
  it('offers the runner\'s own classes, each labelled with its model, on the default', () => {
    render(<ModelClassSelect id="c" label="task model class" offer={OFFERS.browser!} value={null} onPick={() => {}} />)
    const select = screen.getByRole('combobox', { name: 'task model class' }) as HTMLSelectElement
    const options = Array.from(select.options)
    expect(options.map((o) => o.value)).toEqual(['opus', 'haiku'])
    expect(options[0]!.textContent).toContain(PINS.opus)
    expect(options[0]!.textContent).toContain('default')
    expect(options[1]!.textContent).toContain(PINS.haiku)
    expect(select.value).toBe('opus')
  })

  it('is not drawn for a runner that runs no Claude model', () => {
    const { container } = render(<ModelClassSelect id="c" label="task model class" offer={OFFERS.codex!} value={null} onPick={() => {}} />)
    expect(container.querySelector('select')).toBeNull()
  })

  it('is not drawn when the runner\'s classes were not read', () => {
    const { container } = render(<ModelClassSelect id="c" label="task model class" offer={null} value={null} onPick={() => {}} />)
    expect(container.querySelector('select')).toBeNull()
  })
})

describe('a workflow step picks its model class', () => {
  it('draws the select for claude-code with its four classes, and none for codex', async () => {
    const root = await form()
    choose(root, 0, 'claude-code', 'summarise the notes')
    const select = await waitFor(() => {
      const s = classSelect(stepCards(root)[0]!)
      expect(s).not.toBeNull()
      return s!
    }, WAIT)
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['opus', 'fable', 'sonnet', 'haiku'])
    expect(select.value).toBe('opus')

    choose(root, 0, 'codex')
    expect(classSelect(stepCards(root)[0]!)).toBeNull()
  })

  it('sends the chosen class by name, and nothing when the default is left', async () => {
    const posts = capturePosts()
    const root = await form()
    choose(root, 0, 'claude-code', 'classify the feedback')
    const select = await waitFor(() => {
      const s = classSelect(stepCards(root)[0]!)
      expect(s).not.toBeNull()
      return s!
    }, WAIT)
    fireEvent.change(select, { target: { value: 'haiku' } })
    await waitFor(() => expect(go().disabled).toBe(false), WAIT)
    fireEvent.click(go())
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    const step = (posts[0]!.steps as Array<Record<string, unknown>>)[0]!
    expect(step.model_class).toBe('haiku')
    expect(step).not.toHaveProperty('model')
  })

  it('leaves `model_class` out of a step left on its default', async () => {
    const posts = capturePosts()
    const root = await form()
    choose(root, 0, 'claude-code', 'implement the change')
    await waitFor(() => expect(classSelect(stepCards(root)[0]!)).not.toBeNull(), WAIT)
    await waitFor(() => expect(go().disabled).toBe(false), WAIT)
    fireEvent.click(go())
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    const step = (posts[0]!.steps as Array<Record<string, unknown>>)[0]!
    expect(step).not.toHaveProperty('model_class')
  })

  it('falls back to the new runner\'s default when the runner changes to one without the chosen class', async () => {
    const root = await form()
    choose(root, 0, 'claude-code', 'draft the plan')
    const select = await waitFor(() => {
      const s = classSelect(stepCards(root)[0]!)
      expect(s).not.toBeNull()
      return s!
    }, WAIT)
    fireEvent.change(select, { target: { value: 'fable' } })
    choose(root, 0, 'browser')
    const after = classSelect(stepCards(root)[0]!)!
    expect(Array.from(after.options).map((o) => o.value)).toEqual(['opus', 'haiku'])
    expect(after.value).toBe('opus')
  })
})
