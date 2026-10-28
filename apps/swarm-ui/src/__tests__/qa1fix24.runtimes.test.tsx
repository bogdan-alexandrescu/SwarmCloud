// QA1-FIX-24 (part of #2596): two findings on Capacity › Runtimes.
//
//   capacity-13  Runtimes still ended in the `Reading this screen:` row of
//                underlined monospace help links (`<HelpLinks>`) that VQ-033
//                took off Pools, Accounts and Provider quota. The row is gone;
//                the `?` beside the page title opens every topic it listed,
//                and it is still the page's one `?` by the title.
//   capacity-23  `Sets it apart` said `takes any one of its secrets` and `the
//                only runtime offering demo-quick, demo-later`; the card's keys
//                said `cred` and `mem` and the workspace row `GiB of mem`. Each
//                is said in the reader's words now.
//
// BREAK IT: put `<HelpLinks topics={RUNTIME_TOPICS} />` back at the foot of
// `Topology`, drop `help` from the `Screen`, or restore any old phrase or
// abbreviated key in Runtimes.tsx -- a case below turns red.

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import { HELP, type TopicId } from '../help'
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
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-28T10:00:00Z' }
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

/**
 * `scribe` alone accepts any one of two credentials and alone has `quick` and
 * `later`; `viewport` shares scribe's provider and `deep`, and alone has
 * `solo`, so it is told apart by exactly one class.
 */
function topology(): RuntimeTopology {
  return {
    runtimes: {
      scribe: runtime('scribe', {
        provider: 'vendor',
        secrets: ['V_ONE', 'V_TWO'],
        secrets_any_of: true,
        model_classes: ['deep', 'quick', 'later'],
        default_model_class: 'deep',
        models: { deep: 'vendor-model-deep-2', quick: 'vendor-model-quick-1', later: 'vendor-model-later-1' },
      }),
      probe: runtime('probe'),
      viewport: runtime('viewport', {
        provider: 'vendor',
        secrets: ['V_ONE'],
        model_classes: ['deep', 'solo'],
        default_model_class: 'deep',
        models: { deep: 'vendor-model-deep-2', solo: 'vendor-model-solo-1' },
      }),
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

/** `Sets it apart`'s lines on a card, as `key: value`. */
function apart(name: string): string[] {
  return [...card(name).querySelectorAll('.rt-apart-facts > .ctl-fact')].map((f) => {
    const key = f.querySelector('b')?.textContent ?? ''
    return `${key}: ${(f.textContent ?? '').slice(key.length).trim()}`
  })
}

/** Every key a card draws, in its fact rows and under `Sets it apart`. */
function keys(name: string): string[] {
  return [...card(name).querySelectorAll('.ctl-fact > b')].map((b) => b.textContent ?? '')
}

/** The card's own fact row under `key`, without the key. */
function row(name: string, key: string): string {
  const f = [...card(name).querySelectorAll('.ctl-facts.is-rows:not(.rt-apart-facts) > .ctl-fact')].find(
    (x) => x.querySelector('b')?.textContent === key,
  )
  expect(f, `no ${key} row on ${name}`).toBeTruthy()
  return (f!.textContent ?? '').slice(key.length).trim()
}

const RUNTIME_TOPICS: readonly TopicId[] = [
  'catalogue-from-route',
  'runner-profile-by-name',
  'not-a-machine-inventory',
  'declared-vs-resolved-backend',
  'workspace-memory',
  'requests-are-ceilings',
  'units-not-agents',
  'credential-names-not-values',
  'runtime-needs-no-provider',
  'what-sets-it-apart-is-arithmetic',
]

describe('capacity-13: Runtimes ends in no help-link footer; its topics sit behind the title’s ?', () => {
  it('draws no `Reading this screen:` row and no help-link list after Sizing', async () => {
    // MUTATION: restoring `<HelpLinks topics={RUNTIME_TOPICS} />` at the foot
    // of `Topology` brings the row back and this finds its label.
    await shown()
    expect(screen.queryByText('Reading this screen:')).toBeNull()
    // Outside the title's head and its dialog, nothing links to a `#help/` topic
    // that only the footer used to carry.
    const head = document.querySelector('.c-phead')!
    const strays = [...document.querySelectorAll('a[href^="#help/"]')].filter((a) => !head.contains(a))
    expect(strays.map((a) => a.getAttribute('href'))).not.toContain(`#${HELP['catalogue-from-route'].anchor}`)
  })

  it('opens every topic the footer listed, in its order, from the one ? by the title', async () => {
    // MUTATION: dropping `help={RUNTIME_TOPICS}` from the `Screen` leaves the
    // section's lone question there; this reads no topic links. Dropping a
    // topic from RUNTIME_TOPICS loses it from this list.
    await shown()
    const head = document.querySelector<HTMLElement>('.c-phead')!
    const glyphs = within(head).getAllByRole('button', { name: /^Help:/ })
    // THE PAGE'S ONE `?`: no second glyph grew beside the title.
    expect(glyphs.length).toBe(1)
    fireEvent.click(glyphs[0]!)
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    const hrefs = [...dialog.querySelectorAll('a[href^="#help/"]')].map((a) => a.getAttribute('href'))
    expect(hrefs).toEqual(RUNTIME_TOPICS.map((id) => `#${HELP[id].anchor}`))
  })
})

describe('capacity-23: Sets it apart says it in the reader’s words', () => {
  it('says the runtime accepts any one of its credentials', async () => {
    // MUTATION: restoring `takes any one of its secrets` (or the `cred` key)
    // fails this line, and the old phrase is found on the screen.
    await shown()
    expect(apart('scribe')).toContain('credential: the only runtime that accepts any one of its credentials')
    expect(document.body.textContent).not.toContain('takes any one of its secrets')
  })

  it('names the classes as model classes, plural for two and singular for one', async () => {
    // MUTATION: restoring `the only runtime offering ${...}` fails both lines;
    // dropping the singular branch makes viewport read `the model classes solo`.
    await shown()
    expect(apart('scribe')).toContain('model classes: the only runtime with the model classes quick, later')
    expect(apart('viewport')).toContain('model classes: the only runtime with the model class solo')
    expect(document.body.textContent).not.toContain('the only runtime offering')
  })
})

describe('capacity-23: the card’s keys are words, not abbreviations', () => {
  it('labels the rows `credential` and `memory`, and no key is a bare `cred` or `mem`', async () => {
    // MUTATION: restoring `<b>cred</b>`, `<b>mem</b>` or a `key: 'cred'`
    // distinction puts the abbreviation back in this list.
    await shown()
    for (const name of ['scribe', 'probe', 'viewport', 'wide']) {
      const k = keys(name)
      expect(k, name).toContain('credential')
      expect(k, name).toContain('memory')
      expect(k, name).not.toContain('cred')
      expect(k, name).not.toContain('mem')
    }
    expect(row('probe', 'memory')).toBe('2 GiB')
  })

  it('says the workspace is `of memory`, not `of mem`', async () => {
    // MUTATION: restoring `{spec.disk_gib} GiB of mem` fails both lines.
    await shown()
    expect(row('probe', 'workspace')).toBe('1 GiB of memory')
    expect(card('probe').textContent).not.toMatch(/of mem\b/)
  })
})
