// QA G5 (2026-10-07), Runtimes: three findings.
//
//   G5-04  A card's "Pools it must clear" chip for a pool with no document
//          linked to Pools, where the pool is not listed. It is drawn dashed,
//          titled with what admission does with it, from the capacity read's
//          own `admission.uncapped` -- never re-derived here.
//   G5-09  At 390 the off-screen "Resolves from" cell wrapped every runtime
//          name into a narrow column and made its row ~200px tall. Below 700px
//          the cell holds one line and the scroll table carries it.
//   G5-10  "real zero" -- the console's internal honesty term -- was drawn as
//          a chip under "Sets it apart" and after a provider under "cred",
//          where it read as "no credential" for a step that does use one.
//
// BREAK IT: drop `is-phantom` from the chip; drop the phone rule from
// capacity.css; put `real zero` back in `Credential` -- each case fails.

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Runtime } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen, Credential } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  document.body.innerHTML = ''
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-07T10:00:00Z' }
}

const PHANTOM = 'provider:git:tenant:eng'

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
    ...over,
  } as Runtime
}

function topology(): RuntimeTopology {
  return {
    runtimes: {
      alpha: runtime('alpha', { provider: 'git' }),
      beta: runtime('beta', { provider: 'git' }),
    },
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [],
    poolsDetail: null,
    profilePools: { alpha: ['global', PHANTOM], beta: ['global'] },
    profileUncapped: { alpha: [PHANTOM], beta: [] },
  } as unknown as RuntimeTopology
}

function card(name: string): HTMLElement {
  const head = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')].find((h) =>
    (h.textContent ?? '').includes(name),
  )
  expect(head, `no card for ${name}`).toBeTruthy()
  return head!.closest<HTMLElement>('section')!
}

describe('G5-04: a pool chip for a pool nobody configured says so', () => {
  it('draws it dashed with what admission does with it, and leaves configured pools plain', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const chips = [...card('alpha').querySelectorAll<HTMLElement>('.rt-pool-chips > *')]
    const phantom = chips.find((c) => c.textContent === PHANTOM)!
    const global = chips.find((c) => c.textContent === 'global')!
    expect(phantom.classList.contains('is-phantom')).toBe(true)
    expect(phantom.getAttribute('title')).toBe('no such pool configured: admission treats it as uncapped')
    expect(global.classList.contains('is-phantom'), 'a configured pool drawn as missing').toBe(false)
    expect(painted(phantom, ['border-style', 'border'], WIDE)).toMatch(/dashed/)
  })
})

describe('G5-09: Sizing rows stay one line tall on a phone', () => {
  it('holds the Resolves from cell on one line below 700px, and still wraps it at desktop', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const cell = document.querySelector('.rt-sizing td[data-label="Resolves from"]')!
    expect(cell.textContent).toBe('alpha, beta')
    expect(painted(cell, 'white-space', PHONE)).toBe('nowrap')
    expect(painted(cell, 'white-space', WIDE)).toBe('normal')
  })
})

describe('G5-10: no "real zero" chip on a runtime card', () => {
  it('says "nothing: same as the others" in muted text when nothing sets a runtime apart', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const apart = card('alpha').querySelector('.rt-apart')!
    expect(apart.textContent).toContain('nothing: same as the others')
    expect(apart.textContent).not.toContain('real zero')
    expect(apart.querySelector('.ctl-mark'), 'still drawn as a chip').toBeNull()
    expect(apart.querySelector('.rt-apart-none')).not.toBeNull()
  })

  it('names the provider and says the catalogue declares no secret, rather than "real zero"', () => {
    const { container } = render(<Credential runtime={runtime('alpha', { provider: 'git' })} />)
    expect(container.textContent).toBe('git · no secret declared in the catalogue')
    expect(container.querySelector('.ctl-mark'), 'still drawn as a chip').toBeNull()
  })
})
