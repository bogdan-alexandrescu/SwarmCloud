// MODEL-UI (docs/design/model-per-step.md §4.6, §4.8, part of #1435): each
// attempt shows the model it was started with (`Attempt.model`) and the task's
// model class; and when the CLI's own stream (`result_summary.runner.usage
// .models`) does not include the pinned model, BOTH are shown, with "the CLI
// ran a different model from the one pinned".
//
// The rule is the worker's `_note_model_mismatch`: the pin missing from the
// stream's list is a mismatch, and another model BESIDE the pin is not -- the
// CLI reports the small model it runs for its own housekeeping too. A `[...]`
// context suffix is the same model.
//
// BREAK IT: print only the pinned model on a mismatch; call a housekeeping
// model beside the pin a mismatch; drop the class; compare a stream from the
// last attempt against an earlier one -- each case fails.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { AttemptRow, Task } from '../types'
import { at, attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { AttemptModel } = await import('../AgentDetail')
const { modelMismatch } = await import('../types')

const PINNED = 'claude-sonnet-5-5'
const OTHER = 'claude-opus-5-5'
const HOUSEKEEPING = 'claude-haiku-5-5'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function ran(models: string[]): Task['result_summary'] {
  return { runner: { usage: { models } } } as unknown as Task['result_summary']
}

function finished(models: string[] | null, over: Partial<Task> = {}): Task {
  return task({
    state: 'SUCCEEDED', started_at: at(1), completed_at: at(9),
    model_class: 'sonnet', model_class_defaulted: false,
    result_summary: models === null ? null : ran(models),
    ...over,
  })
}

async function mount(t: Task, attempts: AttemptRow[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts }))
  api.loadAgentDetail.mockResolvedValue(ok({ task: t, events: [], eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={t.id} />)
  await waitFor(() => expect(container.querySelector('.att-card')).not.toBeNull())
  return container as HTMLElement
}

function modelFact(root: HTMLElement): HTMLElement {
  const fact = [...root.querySelectorAll<HTMLElement>('.ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'model')
  expect(fact, 'no "model" fact').toBeTruthy()
  return fact!
}

describe('modelMismatch, the worker\'s rule', () => {
  it('is the stream\'s models when the pin is not among them', () => {
    expect(modelMismatch(PINNED, finished([OTHER]))).toEqual([OTHER])
  })

  it('is null when the pin is among them, beside a housekeeping model or with a context suffix', () => {
    expect(modelMismatch(PINNED, finished([PINNED, HOUSEKEEPING]))).toBeNull()
    expect(modelMismatch(PINNED, finished([`${PINNED}[1m]`]))).toBeNull()
  })

  it('is null when either side was not recorded', () => {
    expect(modelMismatch(null, finished([OTHER]))).toBeNull()
    expect(modelMismatch(undefined, finished([OTHER]))).toBeNull()
    expect(modelMismatch(PINNED, finished(null))).toBeNull()
    expect(modelMismatch(PINNED, finished([]))).toBeNull()
  })
})

describe('an attempt shows its model and the task\'s class', () => {
  it('prints the attempt\'s model and the class', () => {
    const { container } = render(
      <ul className="ctl-facts"><AttemptModel a={attempt(1, { model: PINNED })} task={finished([PINNED])} isLatest /></ul>,
    )
    const fact = modelFact(container)
    expect(fact.textContent).toContain(PINNED)
    expect(fact.textContent).toContain('sonnet')
    expect(container.textContent).not.toContain('different model')
  })

  it('marks a class the task did not name as its runner\'s default', () => {
    const t = finished([OTHER], { model_class: 'opus', model_class_defaulted: true })
    const { container } = render(<ul><AttemptModel a={attempt(1, { model: OTHER })} task={t} isLatest /></ul>)
    expect(modelFact(container).textContent).toMatch(/opus.*default/)
  })

  it('says no model was recorded, never a blank, for an attempt without one', () => {
    const { container } = render(<ul><AttemptModel a={attempt(1, { model: null })} task={finished(null)} isLatest /></ul>)
    const fact = modelFact(container)
    expect(fact.classList.contains('is-absent')).toBe(true)
    expect(fact.querySelector('.ctl-mark')).not.toBeNull()
  })
})

describe('a mismatch shows both models', () => {
  it('on the Attempts pane, with the sentence, on the last attempt', async () => {
    const el = await mount(finished([OTHER]), [attempt(1, { model: PINNED })])
    const fact = modelFact(el)
    expect(fact.textContent).toContain(PINNED)
    const said = el.querySelector<HTMLElement>('.att-model-mismatch')
    expect(said, 'no mismatch line').not.toBeNull()
    expect(said!.textContent).toContain(PINNED)
    expect(said!.textContent).toContain(OTHER)
    expect(said!.textContent).toContain('the CLI ran a different model from the one pinned')
  })

  it('not when the stream ran the pin beside a housekeeping model', async () => {
    const el = await mount(finished([PINNED, HOUSEKEEPING]), [attempt(1, { model: PINNED })])
    expect(el.querySelector('.att-model-mismatch')).toBeNull()
  })

  it('compares the stream with the LAST attempt only: the summary is that attempt\'s', async () => {
    const el = await mount(finished([PINNED]), [
      attempt(1, { model: OTHER, completed_at: at(4) }),
      attempt(2, { model: PINNED, started_at: at(5) }),
    ])
    expect(el.querySelector('.att-model-mismatch')).toBeNull()
  })
})
