// MODEL-UI (docs/design/model-per-step.md §4.8, part of #1435): each runtime
// card on Runtimes lists the model classes a step on it may name, each as a
// chip carrying the model ID this deployment pins for it, the default marked;
// and `—` for a profile that runs no Claude model.
//
// The names here are invented: the screen restates nothing from the frozen
// catalogue, and neither does its test.
//
// BREAK IT: drop the `model classes` row; print the class without its ID;
// drop the default mark; render an empty list as nothing instead of `—` --
// each case fails.

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Runtime } from '../types'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  document.body.innerHTML = ''
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-19T10:00:00Z' }
}

function runtime(name: string, over: Partial<Runtime> = {}): Runtime {
  return {
    name,
    available: true,
    disabled_reason: '',
    image: 'agent-runtime-base',
    backend: 'CLOUD_RUN_JOB',
    resolved_backend: 'CLOUD_RUN_JOB',
    provider: null,
    secrets: [],
    secrets_any_of: false,
    timeout_seconds: 600,
    resource_class: 'standard',
    model_classes: [],
    default_model_class: null,
    models: {},
    resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
    ...over,
  }
}

function topology(runtimes: Runtime[]): RuntimeTopology {
  return {
    runtimes: Object.fromEntries(runtimes.map((r) => [r.name, r])),
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [],
    poolsDetail: null,
  }
}

function card(name: string): HTMLElement {
  const head = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')].find((h) =>
    (h.textContent ?? '').includes(name),
  )
  expect(head, `no card for ${name}`).toBeTruthy()
  return head!.closest<HTMLElement>('section')!
}

function classesFact(name: string): HTMLElement {
  const fact = [...card(name).querySelectorAll<HTMLElement>('.ctl-fact')].find(
    (f) => f.querySelector('b')?.textContent === 'model classes',
  )
  expect(fact, `no "model classes" row on ${name}`).toBeTruthy()
  return fact!
}

const DEEP = 'vendor-deep-7'
const QUICK = 'vendor-quick-7'

const classed = runtime('alpha', {
  provider: 'vendor',
  model_classes: ['deep', 'quick', 'unpinned'],
  default_model_class: 'deep',
  models: { deep: DEEP, quick: QUICK },
})
const classless = runtime('beta')

describe('Runtimes lists each profile\'s model classes', () => {
  it('draws one chip per accepted class, carrying its pinned ID, with the default marked', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology([classed, classless])))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const chips = [...classesFact('alpha').querySelectorAll<HTMLElement>('.rt-model-class')]
    expect(chips.map((c) => c.dataset.modelClass)).toEqual(['deep', 'quick', 'unpinned'])
    expect(chips[0]!.textContent).toContain('deep')
    expect(chips[0]!.textContent).toContain(DEEP)
    expect(chips[0]!.textContent).toContain('default')
    expect(chips[1]!.textContent).toContain(QUICK)
    expect(chips[1]!.textContent).not.toContain('default')
    // A class this deployment pins no ID for says so, rather than printing a blank.
    expect(chips[2]!.textContent).toContain('no model pinned')
  })

  it('draws `—` for a profile that runs no Claude model', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology([classed, classless])))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const fact = classesFact('beta')
    expect(fact.querySelectorAll('.rt-model-class')).toHaveLength(0)
    expect(fact.querySelector('.ctl-em')?.textContent).toBe('—')
  })
})
