// VISUAL QA RUN 5b (wf_025ae735511546d99eff, 2026-10-22, the fixture build of
// main at 58cd0c1e), LANE QAFIX5-CAPACITY (part of #1680).
//
//   r2:capacity-007  The Catalogue's demo-wide card drew its whole `runs on`
//                    value, `CLOUD_RUN_JOB ← AUTO`, on the line under the
//                    key at 1440, so `← AUTO` still read as a second label.
//                    Run 2's fix made the backend and its annotation one value
//                    item, but left the FACT `flex-wrap: wrap` (QA-010's rule,
//                    written when the annotation was the fact's own item):
//                    when key plus value outgrew the card's track, the one
//                    value item wrapped as a whole to the row's start. The
//                    fact no longer wraps; the value item shrinks into the
//                    value column and `← AUTO` wraps inside it, under
//                    CLOUD_RUN_JOB.
//
// Not held here, because nothing changed for them in this lane (the PR body
// and questions.json say why): r3:capacity-011 (ragged card heights, then
// V088's `align-items: start`, which QAFIX6-CAPACITY-2 has since retired for
// `align-items: stretch` on `.rt-cards > .ctl-cards` in styles/capacity.css),
// r3:capacity-014 ('too small' shares the paused glyph; the owner's
// question) and r3:capacity-006 ('0 holders' with no drift mark; the owner's
// question).

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
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-22T10:00:00Z' }
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

/** `wide` declares AUTO, which the platform resolves to CLOUD_RUN_JOB, as the fixture's demo-wide does. */
function topology(): RuntimeTopology {
  return {
    runtimes: {
      probe: runtime('probe'),
      wide: runtime('wide', { backend: 'AUTO', provider: 'other', secrets: ['O_ONE'] }),
    },
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [],
    poolsDetail: null,
    profilePools: { probe: [], wide: [] },
    profileUncapped: { probe: [], wide: [] },
  } as unknown as RuntimeTopology
}

function card(name: string): HTMLElement {
  const head = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')].find(
    (h) => h.querySelector('h2')?.textContent?.trim().startsWith(name) ?? false,
  )
  expect(head, `no card for ${name}`).toBeTruthy()
  return head!.closest<HTMLElement>('section')!
}

describe('r2:capacity-007: `CLOUD_RUN_JOB ← AUTO` stays in the value column beside `runs on`', () => {
  it('never wraps the value item under the key: the fact is one line, the value wraps inside itself', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const fact = card('wide').querySelector<HTMLElement>('.ctl-facts.is-rows > .ctl-fact.rt-runs-on')!
    expect(fact.querySelector('b')?.textContent).toBe('runs on')
    const value = fact.querySelector<HTMLElement>(':scope > .rt-runs-on-value')!
    // MUTATION: render the value as two items again; this reads `CLOUD_RUN_JOB` alone.
    // The words since QA2-FIX-21 (capacity-14, cap2-10): the backend's name and `chosen automatically`.
    expect(value.textContent).toBe('Cloud Runchosen automatically')
    for (const env of [WIDE, PHONE]) {
      // MUTATION: put `flex-wrap: wrap` back on `.rt-runs-on` (capacity.css)
      // and the whole value item drops to the row's start, under `runs on`,
      // whenever key plus value outgrow the card's track; this reads `wrap`.
      expect(painted(fact, 'flex-wrap', env)).toBe('nowrap')
      // The value item can shrink to the room left beside the key, so the
      // wrap happens INSIDE it: `← AUTO` under CLOUD_RUN_JOB, not under the key.
      // MUTATION: give `.rt-runs-on-value` `flex: none` and it keeps its
      // one-line width and overflows the card instead; this reads `none`.
      expect(painted(value, 'flex', env) ?? 'initial').not.toBe('none')
      // MUTATION: drop the primitive's `min-width: 0` for the value and it
      // cannot shrink below its one-line width; this reads null.
      expect(painted(value, 'min-width', env)).toBe('0')
      // MUTATION: drop `flex-wrap: wrap` from `.rt-runs-on-value` and the
      // annotation overflows instead of wrapping under the backend.
      expect(painted(value, 'flex-wrap', env)).toBe('wrap')
    }
  })
})
