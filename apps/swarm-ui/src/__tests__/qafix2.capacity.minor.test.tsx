// Visual QA run 2 (wf_b6e2441b419f423a84eb, 2026-10-20, the fixture build of
// main at 75148351): the minor findings on Capacity and Admin › Pool limits,
// epic #1680, lane QAFIX2-CAPACITY-MINOR.
//
//   admin-009     Pool limits' `Admins only. ...` sentence was in the code face:
//                 it is a `.ctl-panel-note`, whose primitive is mono.
//   capacity-006  Runtimes said `Sets it apart: nothing` for the one runtime
//                 that alone offers two model classes and takes any one of its
//                 secrets.
//   capacity-007  `← AUTO` wrapped to the start of the `runs on` row, under
//                 the key, instead of staying with CLOUD_RUN_JOB in the value
//                 column.
//   capacity-008  Pools' Set by cell (`AIMD back-off` then, `lowered automatically
//                 after errors` since QA1B-13) was at the table's body
//                 size beside the 12px marks and links of its row.
//
// capacity-009 (the half-height amber rule) is the sheet's deliberate warn
// form and capacity-010 is held in vqa2.r2.capacity.test.tsx, where the rule
// it replaces was pinned.

import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Runtime } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen } = await import('../Runtimes')

const WAIT = { timeout: 5000 } as const
const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
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

/**
 * The fixture's shape: `scribe` alone offers `quick` and `later` and takes
 * either of two secrets; `viewport` shares its provider and its `deep`; `wide`
 * declares AUTO.
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
        model_classes: ['deep'],
        default_model_class: 'deep',
        models: { deep: 'vendor-model-deep-2' },
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

describe('admin-009: the non-admin line on Pool limits is prose, in the prose face', () => {
  it('sets `.adm-locked` in the sans face, not the panel note\'s mono', () => {
    const note = build('section.adm-ceilings > p.ctl-panel-note.adm-locked', hosts)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: delete the `.ctl-panel-note.adm-locked` rule in
      // styles/admin.css and the primitive's `font: ... var(--mono)` wins;
      // this reads the mono shorthand.
      expect(painted(note, ['font', 'font-family'], env)).toBe('var(--font)')
    }
  })
})

describe('capacity-006: `Sets it apart` names the classes and the credential form only one runtime has', () => {
  it('says which model classes nothing else offers', async () => {
    await shown()
    // MUTATION: drop the `model classes` distinction and scribe reads only
    // `nothing: same as the others` again; this array loses the line.
    expect(apart('scribe')).toContain('model classes: the only runtime with the model classes quick, later')
    // MUTATION: count a class shared with another runtime and viewport is
    // told apart by `deep`, which scribe offers too; this gains a line.
    expect(apart('viewport').some((l) => l.startsWith('model classes'))).toBe(false)
  })

  it('says it is the only runtime that accepts any one of its credentials', async () => {
    await shown()
    // MUTATION: drop the any-one-of distinction and this line is gone.
    expect(apart('scribe')).toContain('credential: the only runtime that accepts any one of its credentials')
    // MUTATION: say it of a single-secret runtime too and viewport gains it.
    expect(apart('viewport')).toEqual([])
    // The card's own nothing-mark is gone from scribe.
    // MUTATION: render the empty mark whatever `facts` holds; this finds it.
    expect(card('scribe').querySelector('.rt-apart-none')).toBeNull()
  })
})

describe('capacity-006: one `credential` line when the provider and the any-one-of form are both its own', () => {
  it('says the provider line alone, never two `credential` lines', async () => {
    const t = topology()
    // scribe is now the only `vendor` runtime AND the only any-one-of one.
    t.runtimes.viewport = { ...t.runtimes.viewport!, provider: 'third' }
    api.loadRuntimeTopology.mockResolvedValue(ok(t))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const creds = apart('scribe').filter((l) => l.startsWith('credential'))
    // MUTATION: turn the any-one-of `else if` into a plain `if` and scribe
    // gains a second `credential` line; this reads two.
    expect(creds).toEqual(['credential: the only runtime that needs this credential'])
  })
})

describe('capacity-007: `← AUTO` stays with the backend it was resolved to', () => {
  it('puts the declared backend in the value column, beside CLOUD_RUN_JOB', async () => {
    await shown()
    const fact = card('wide').querySelector<HTMLElement>('.ctl-fact.rt-runs-on')!
    const values = [...fact.children].filter((c) => c.tagName !== 'B')
    // ONE value item: the key column is `b`, and everything after it starts at
    // the value column's x, so a wrap can never land under `runs on`.
    // MUTATION: put `.rt-declared` back as the fact's own child and this
    // reads two items, the second of which wraps to the key's x.
    expect(values).toHaveLength(1)
    const value = values[0] as HTMLElement
    // The words since QA2-FIX-21 (capacity-14, cap2-10).
    expect(value.querySelector('.rt-backend')?.textContent).toBe('Cloud Run')
    expect(value.querySelector('.rt-declared')?.textContent).toBe('chosen automatically')
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop `flex-wrap: wrap` from `.rt-runs-on-value` and
      // backend plus annotation overflow a narrow card; this reads null.
      expect(painted(value, 'flex-wrap', env)).toBe('wrap')
      // MUTATION: set `.rt-runs-on-value` to `display: block` and `← AUTO`
      // drops onto its own line under CLOUD_RUN_JOB at 1440; this reads block.
      expect(painted(value, 'display', env)).toBe('inline-flex')
      // MUTATION: drop the gap and `← AUTO` touches the backend; this reads null.
      expect(painted(value, 'gap', env)).toBe('0 var(--ctl-s2)')
    }
  })
})

describe('capacity-008: Pools\' Set by cell is the size of the marks and links beside it', () => {
  function row(): { setBy: HTMLElement; links: HTMLElement } {
    const host = document.createElement('div')
    host.innerHTML =
      '<div class="ctl-table cap-pools"><table><tbody><tr>' +
      '<td data-label="Set by">lowered automatically after errors</td><td data-label="Links" class="cap-links">0 holders · limit</td>' +
      '</tr></tbody></table></div>'
    document.body.appendChild(host)
    hosts.push(host)
    return {
      setBy: host.querySelector<HTMLElement>('td[data-label="Set by"]')!,
      links: host.querySelector<HTMLElement>('td.cap-links')!,
    }
  }

  it('sets the Set by word at the micro size, not the figures\'', () => {
    const { setBy, links } = row()
    for (const env of [WIDE, PHONE]) {
      // MUTATION: delete the `.cap-pools td[data-label="Set by"]` rule and
      // the cell inherits the table's body size; this reads null.
      expect(painted(setBy, ['font', 'font-size'], env)).toBe('var(--t-micro)')
      expect(painted(links, ['font', 'font-size'], env)).toBe('var(--t-micro)')
    }
  })
})
