// #3118: a live-browser runtime's card says what the agent's container gets.
//
// The class's `memory_gib` is the whole pod: the agent's container plus the
// live display's. On 3 November 2026 the catalogue read 10 GiB for a browser
// runtime whose agent was OOM-killed at 5, so a caller sized a step for 10.
// The runtime entry now serves `agent_memory_gib`, `display_memory_gib` and
// `pod_memory_gib` beside `resources`; the card's `memory` row leads with the
// agent's share and names the display's and the pod's after it.
//
// BREAK IT: render `{spec.memory_gib} GiB` alone in the `memory` row again --
// the first case turns red.

import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Runtime } from '../types'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-11-03T10:00:00Z' }
}

const WIDE = { name: 'wide', cpu: 4, memory_gib: 10, disk_gib: 4, units: 2 }

function runtime(name: string, over: Partial<Runtime> = {}): Runtime {
  return {
    name,
    available: true,
    disabled_reason: '',
    image: 'agent-runtime-base',
    backend: 'GKE_AUTOPILOT',
    resolved_backend: 'GKE_AUTOPILOT',
    provider: null,
    secrets: [],
    secrets_any_of: false,
    timeout_seconds: 600,
    resource_class: 'wide',
    resources: WIDE,
    model_classes: [],
    default_model_class: null,
    models: {},
    ...over,
  } as Runtime
}

function topology(): RuntimeTopology {
  return {
    runtimes: {
      // Two runtimes of one class: one runs a display beside its agent.
      viewer: runtime('viewer', { agent_memory_gib: 5, display_memory_gib: 5, pod_memory_gib: 10 }),
      plain: runtime('plain', { agent_memory_gib: 10, display_memory_gib: null, pod_memory_gib: 10 }),
      // A platform from before the split was served.
      older: runtime('older'),
    },
    classes: { wide: WIDE },
    classesDetail: null,
    pools: [],
    poolsDetail: null,
    profilePools: { viewer: [], plain: [], older: [] },
    profileUncapped: { viewer: [], plain: [], older: [] },
  } as unknown as RuntimeTopology
}

async function shown(): Promise<void> {
  api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
  render(<RuntimesScreen />)
  await screen.findByText('Sizing', undefined, WAIT)
}

function memoryRow(name: string): string {
  const head = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')].find(
    (h) => h.querySelector('h2')?.textContent?.trim().startsWith(name) ?? false,
  )
  expect(head, `no card for ${name}`).toBeTruthy()
  const f = [...head!.closest('section')!.querySelectorAll('.ctl-facts.is-rows:not(.rt-apart-facts) > .ctl-fact')].find(
    (x) => x.querySelector('b')?.textContent === 'memory',
  )
  expect(f, `no memory row on ${name}`).toBeTruthy()
  return (f!.textContent ?? '').slice('memory'.length).trim()
}

describe('#3118: the memory row says what the agent gets', () => {
  it('leads a live-display runtime with the agent’s share, then the display’s and the pod’s total', async () => {
    // MUTATION: the row printing `resources.memory_gib` alone reads `10 GiB`.
    await shown()
    expect(memoryRow('viewer')).toBe('5 GiB for the agent · display 5 GiB · pod 10 GiB')
  })

  it('prints the whole figure for a runtime with no display, and for a platform that served no split', async () => {
    // MUTATION: drawing the split whenever `agent_memory_gib` is present turns
    // `plain` into `10 GiB for the agent · display  GiB ...`.
    await shown()
    expect(memoryRow('plain')).toBe('10 GiB')
    expect(memoryRow('older')).toBe('10 GiB')
  })
})
