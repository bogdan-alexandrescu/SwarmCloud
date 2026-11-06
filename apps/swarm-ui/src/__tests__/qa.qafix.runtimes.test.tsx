// Visual QA wf_9e9bf74834634db7bcc3 (2026-10-20), Runtimes: three findings,
// epic #1680.
//
//   QA-009  At 390 the page scrolled sideways. Measured in Chromium against the
//           fixture build: the one box past the viewport was the default
//           model-class chip `demo-deep · demo-model-deep-2 · default`, 308px of
//           `white-space: nowrap` (`.c-chip`) in a ~220px value column, right
//           edge 412 on a 390 scroller. Backends' and Sizing's hits were their
//           visually-hidden 1px theads, which clip and add no scroll width.
//   QA-010  `CLOUD_RUN_JO` / `B`: the `runs on` value is a flex item with
//           `min-width: 0; overflow-wrap: anywhere`, so `← AUTO` beside it
//           squeezed the backend until it broke mid-token.
//   QA-011  `cred: the only runtime that needs this credential` on a runtime
//           whose cred row says `none needed`, and on two runtimes of one
//           provider that differ only in which secrets they mount.

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Runtime } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

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
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
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

/** The `credential` line under `Sets it apart`, or null when nothing is said there. */
// The key is `credential`, spelled out since QA1-FIX-24 (capacity-23, part of #2596).
function apartCred(name: string): string | null {
  const facts = [...card(name).querySelectorAll('.rt-apart-facts > .ctl-fact')]
  const f = facts.find((x) => x.querySelector('b')?.textContent === 'credential')
  return f === undefined ? null : (f.textContent ?? '').replace(/^credential/, '').trim()
}

describe('QA-009: a model-class chip fits its card at 390', () => {
  it('lets the chip wrap between its parts and never be wider than its row', async () => {
    await shown()
    const chip = card('scribe').querySelector<HTMLElement>('.rt-model-class.is-default')!
    expect(chip.textContent).toBe('deep · vendor-model-deep-2 · default')
    for (const env of [PHONE, WIDE]) {
      // MUTATION: drop `flex-wrap: wrap` from `.rt-model-class` and the chip
      // is one 308px nowrap line again; this reads `nowrap`.
      expect(painted(chip, 'flex-wrap', env)).toBe('wrap')
      // MUTATION: drop `max-width: 100%` and a chip may still outgrow the
      // value column it sits in; this reads null.
      expect(painted(chip, 'max-width', env)).toBe('100%')
    }
    // Each part stays one token: the wrap is BETWEEN the class and its model
    // ID, never inside an identifier a reader pastes.
    const id = [...chip.querySelectorAll('.mono')].find((m) => m.textContent === 'vendor-model-deep-2')!
    // MUTATION: give the parts `overflow-wrap: anywhere` and the ID breaks
    // mid-token instead; this reads `anywhere`.
    expect(painted(id, 'white-space', PHONE)).toBe('nowrap')
  })
})

describe('QA-010: the resolved backend never breaks mid-word beside `← AUTO`', () => {
  it('keeps the backend whole and moves the annotation to the next line instead', async () => {
    await shown()
    const fact = [...card('wide').querySelectorAll<HTMLElement>('.ctl-facts.is-rows > .ctl-fact')].find(
      (f) => f.querySelector('b')?.textContent === 'runs on',
    )!
    const backend = fact.querySelector<HTMLElement>('.rt-backend')!
    // The words since QA2-FIX-21 (capacity-14, cap2-10).
    expect(backend.textContent).toBe('Cloud Run')
    expect(fact.querySelector('.rt-declared')?.textContent).toBe('chosen automatically')
    for (const env of [PHONE, WIDE]) {
      // MUTATION: drop `flex: none` from `.rt-backend` and the value shrinks
      // to give `← AUTO` room again; this reads null.
      expect(painted(backend, 'flex', env)).toBe('none')
      // MUTATION: drop `overflow-wrap: normal` and the primitive's `anywhere`
      // wins, which is the `CLOUD_RUN_JO` / `B` break; this reads `anywhere`.
      expect(painted(backend, 'overflow-wrap', env)).toBe('normal')
      // The annotation's wrap is the value item's own (`.rt-runs-on-value`,
      // capacity-007); the fact never wraps, or the whole value drops under
      // the key (r2:capacity-007, run 5b, qafix5.capacity.test.tsx).
      // MUTATION: put `flex-wrap: wrap` back on `.rt-runs-on`; reads `wrap`.
      expect(painted(fact, 'flex-wrap', env)).toBe('nowrap')
    }
  })
})

describe('QA-011: `Sets it apart` agrees with the cred row above it', () => {
  it('calls the one keyless runtime the only one that needs NO credential', async () => {
    await shown()
    expect(card('probe').textContent).toContain('none needed')
    // MUTATION: put `the only runtime that needs this credential` back for a
    // null provider and this reads that sentence.
    expect(apartCred('probe')).toBe('the only runtime that needs no credential')
  })

  it('tells nothing apart for two runtimes of one provider that mount different secrets', async () => {
    await shown()
    // MUTATION: key the credential on its secret names again and each of
    // these reads `the only runtime that needs this credential`. (scribe is
    // told apart by its any-one-of form instead -- capacity-006, run 2 --
    // which is about how it takes its secrets, not whose they are.)
    expect(apartCred('scribe')).toBe('the only runtime that accepts any one of its credentials')
    expect(apartCred('viewport')).toBeNull()
  })

  it('still names the runtime whose provider nothing else needs', async () => {
    await shown()
    // MUTATION: drop the `cred` distinction entirely and this reads null.
    expect(apartCred('wide')).toBe('the only runtime that needs this credential')
  })
})
