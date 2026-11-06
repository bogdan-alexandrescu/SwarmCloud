// MODEL-SUBMIT (docs/design/model-per-step.md §4.8 "a class select per task
// and per step", part of #1435): the task form offers the chosen runner's
// model classes by name, from the same `ModelClassSelect` a workflow step
// draws, so a task submitted from the console can run a cheaper class.
//
//   * the select is drawn for a runner with classes, and not at all when the
//     `/v1/runtimes` read fails;
//   * a class other than the runner's default is sent as `model_class`, a
//     CLASS NAME, never a model ID (invariant 10);
//   * the default sends no `model_class` key: naming it stores nothing (§4.3);
//   * picking another runner puts the choice back to that runner's default.
//
// BREAK IT: draw the select whatever the read said; send the default; send
// the model ID; keep the class across a runner change -- each case fails.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { ModelClassOffer } from '../api'
import type { Result } from '../fetch'

const api = vi.hoisted(() => ({
  loadModelClasses: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // The submit answers through fetch here, not the fixture build's 201
  // (QA2-FIX-07, runs2-01 and runs2-02).
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { SubmitScreen } = await import('../Submit')

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

/** `claude-code` accepts all four; `claude-code-browser` two; `mock` none. */
const OFFERS: Record<string, ModelClassOffer> = {
  'claude-code': { model_classes: ['opus', 'fable', 'sonnet', 'haiku'], default_model_class: 'opus', models: PINS },
  'claude-code-browser': { model_classes: ['opus', 'haiku'], default_model_class: 'opus', models: { opus: PINS.opus, haiku: PINS.haiku } },
  mock: { model_classes: [], default_model_class: null, models: {} },
}

function capturePosts() {
  const posts: Array<Record<string, unknown>> = []
  const prior = globalThis.fetch
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? 'GET') === 'POST' && String(input).startsWith('/v1/tasks')) {
      posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
      return new Response(JSON.stringify({ task: { id: 't_mc_1', state: 'READY' }, scheduler_woken: true }), { status: 201, headers: JSON_HEADERS })
    }
    return prior(input, init)
  }) as unknown as typeof fetch
  return posts
}

const classSelect = () => screen.queryByRole('combobox', { name: 'agent model class' }) as HTMLSelectElement | null

async function taskForm(profile: string) {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Start one agent' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${profile}"]`)!)
  const prompt = r.container.querySelector<HTMLTextAreaElement>('.sbf-build textarea')!
  fireEvent.change(prompt, { target: { value: 'tidy the changelog' } })
  return r.container
}

async function shownSelect(): Promise<HTMLSelectElement> {
  return waitFor(() => {
    const s = classSelect()
    expect(s, 'no class select for a runner with classes').not.toBeNull()
    return s!
  }, WAIT)
}

beforeEach(() => {
  api.loadModelClasses.mockResolvedValue(ok(OFFERS))
})

afterEach(() => {
  globalThis.fetch = realFetch
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the task form draws the class select', () => {
  it('for a runner with classes, offering its own classes on the default', async () => {
    await taskForm('claude-code')
    const select = await shownSelect()
    expect(Array.from(select.options).map((o) => o.value)).toEqual(['opus', 'fable', 'sonnet', 'haiku'])
    expect(select.value).toBe('opus')
    expect(select.id).toBe('task-model-class')
  })

  it('not at all when the classes could not be read', async () => {
    api.loadModelClasses.mockResolvedValue({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'down' } } as Result<Record<string, ModelClassOffer>>)
    await taskForm('claude-code')
    // The control: the read was asked for, so its absence is the failure, not a skipped read.
    await waitFor(() => expect(api.loadModelClasses).toHaveBeenCalled(), WAIT)
    expect(classSelect(), 'a select drawn over a failed read offers classes nobody read').toBeNull()
  })
})

describe('what the click sends', () => {
  it('a class other than the default is sent as model_class, by name', async () => {
    const c = await taskForm('claude-code')
    fireEvent.change(await shownSelect(), { target: { value: 'haiku' } })
    const posts = capturePosts()
    fireEvent.submit(c.querySelector('form')!)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect(posts[0]!.runner_profile).toBe('claude-code')
    expect(posts[0]!.model_class).toBe('haiku')
    expect(posts[0]).not.toHaveProperty('model')
  })

  it('the default sends no model_class key', async () => {
    const c = await taskForm('claude-code')
    await shownSelect()
    const posts = capturePosts()
    fireEvent.submit(c.querySelector('form')!)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect(posts[0]!.runner_profile).toBe('claude-code')
    expect(posts[0]).not.toHaveProperty('model_class')
  })

  it('picking another runner resets the choice to that runner\'s default', async () => {
    const c = await taskForm('claude-code')
    fireEvent.change(await shownSelect(), { target: { value: 'haiku' } })
    fireEvent.click(c.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code-browser"]')!)
    const after = await shownSelect()
    expect(Array.from(after.options).map((o) => o.value)).toEqual(['opus', 'haiku'])
    expect(after.value).toBe('opus')
    const posts = capturePosts()
    fireEvent.submit(c.querySelector('form')!)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect(posts[0]!.runner_profile).toBe('claude-code-browser')
    expect(posts[0]).not.toHaveProperty('model_class')
  })
})
