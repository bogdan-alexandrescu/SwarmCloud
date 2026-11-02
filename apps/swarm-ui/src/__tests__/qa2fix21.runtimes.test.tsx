// QA2-FIX-21 (part of #2694): the Runtimes screen's pass-2 findings.
//
//   capacity-14  the Backends table and each card's `runs on` printed the
//                API's backend enum where Pools, Holders and By runner profile
//                print its name;
//   cap2-10      a profile that left the backend to the platform read `← ` and
//                the declared token, and a size no runtime uses read
//                `unreachable` with its reason only in a title.

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
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-20T10:00:00Z' }
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
    resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
    model_classes: [],
    default_model_class: null,
    models: {},
    ...over,
  } as Runtime
}

/** The fixture's shape: one keyless runtime, two of one provider, one AUTO. */
function topology(): RuntimeTopology {
  return {
    runtimes: {
      scribe: runtime('scribe', {
        provider: 'vendor',
        secrets: ['V_ONE', 'V_TWO'],
        secrets_any_of: true,
        model_classes: ['deep'],
        default_model_class: 'deep',
        models: { deep: 'vendor-model-deep-2' },
      }),
      probe: runtime('probe'),
      viewport: runtime('viewport', { provider: 'vendor', secrets: ['V_ONE'] }),
      wide: runtime('wide', { backend: 'AUTO', provider: 'other', secrets: ['O_ONE'] }),
    },
    classes: {
      standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
      roomy: { name: 'roomy', cpu: 4, memory_gib: 8, disk_gib: 4, units: 4 },
    },
    classesDetail: null,
    pools: [],
    poolsDetail: null,
    profilePools: { scribe: [], probe: [], viewport: [], wide: [] },
    profileUncapped: { scribe: [], probe: [], viewport: [], wide: [] },
  } as unknown as RuntimeTopology
}

async function shown(): Promise<void> {
  api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
  render(<RuntimesScreen />)
  await screen.findByText('Sizing', undefined, WAIT)
}

function card(name: string): HTMLElement {
  const head = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')].find(
    (h) => h.querySelector('h2')?.textContent?.trim().startsWith(name) ?? false,
  )
  expect(head, `no card for ${name}`).toBeTruthy()
  return head!.closest<HTMLElement>('section')!
}


describe('capacity-14: backends by name', () => {
  it('names each backend in the Backends table and in runs on', async () => {
    await shown()
    const heads = [...document.querySelectorAll<HTMLElement>('.rt-backends tbody th')].map((h) => h.textContent?.trim())
    // MUTATION: print `{backend}` verbatim again and this reads the enum.
    expect(heads).toEqual(['Cloud Run'])
    const value = card('probe').querySelector<HTMLElement>('.rt-runs-on-value')!
    expect(value.textContent).toBe('Cloud Run')
    expect(document.querySelector('.ctl-cards')!.textContent).not.toMatch(/CLOUD_RUN_JOB/)
  })
})

describe('cap2-10: chosen automatically, and why a size is unreachable', () => {
  it('says the platform chose the backend in words', async () => {
    await shown()
    const value = card('wide').querySelector<HTMLElement>('.rt-runs-on-value')!
    // MUTATION: put back `← {runtime.backend}` and this reads `← AUTO`.
    expect(value.querySelector('.rt-declared')?.textContent).toBe('chosen automatically')
    expect(value.textContent).not.toContain('←')
  })

  it('gives the reason beside an unreachable size, on the page', async () => {
    await shown()
    const cell = [...document.querySelectorAll<HTMLElement>('.rt-sizing tbody tr')]
      .find((r) => r.querySelector('th')?.textContent?.trim() === 'roomy')!
      .querySelector<HTMLElement>('td[data-label="Resolves from"]')!
    // MUTATION: drop the visible reason and this reads `unreachable` alone.
    expect(cell.textContent).toContain('unreachable')
    expect(cell.textContent).toContain('no runtime uses this size')
  })
})
