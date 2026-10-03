// POOL LIMITS AND PROVIDER QUOTA, AS BEHAVIOUR.
//
// Visual QA 2026-09-25 (#85, #86) found these two admin screens saying less
// than they know, and one of them saying something false:
//
//   * when two pools tie for the smallest ceiling, Pool limits marked ONE of
//     them as binding and named one in its foot -- so raising the named pool
//     leaves the ceiling exactly where it was (AH-1, S1);
//   * saving a ceiling remounted the whole screen, so the `saved` tag never
//     painted, an unsaved edit in another row was thrown away, and a failed
//     re-read blanked the screen right after a write that succeeded (AH-7);
//   * an invalid ceiling was not marked on its input and its message reflowed
//     the row it appeared in (AH-8);
//   * Provider quota's summary said `1 providers` (CP-22).
//
// Every block here was pushed before the fix it demands.

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { HELP } from '../help'
import type { Capacity, Pool, QuotaState, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  setPoolLimit: vi.fn(),
  loadAdminQuota: vi.fn(),
  // Pool limits reads the session to lock Edit for a non-admin. Unanswered
  // here (undefined), which the screen treats as not known: Edit stays open.
  loadMe: vi.fn(),
}))
vi.mock('../api', () => api)

const { AdminSettingsScreen } = await import('../AdminSettings')
const { QuotaDetailScreen } = await import('../QuotaDetail')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-25T10:00:00Z' }
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 2,
    available: 8,
    enabled: true,
    updated_at: '2026-09-25T10:00:00Z',
    ...over,
  }
}

function profile(pools: string[], units = 1): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units, pools }
}

/**
 * `global` and `tenant:eng` TIE at 10, `resource:standard` sits at 40. The
 * tie is the case AH-1 is about: the strict `<` in `arithmetic()` kept the
 * first of the two and dropped the other.
 */
function capacity(over: Partial<Capacity> = {}): Capacity {
  return {
    pools: [pool('global'), pool('tenant:eng'), pool('resource:standard', { hard_limit: 40, effective_limit: 40 })],
    runner_profiles: { 'claude-code': profile(['global', 'tenant:eng', 'resource:standard']) },
    tenant_id: 'eng',
    generated_at: '2026-09-25T10:00:00Z',
    ...over,
  }
}

/**
 * Pool limits has drawn its ceilings: the first editor row is on screen. (It
 * waited on a per-profile card's title until those cards were removed, owner
 * decision 2026-10-01.)
 */
async function limitsDrawn(): Promise<void> {
  await waitFor(() => expect(document.querySelector('tr[id^="limit-"]')).not.toBeNull(), WAIT)
}

/** The editor row for one pool, found by the id a link can name (#132). */
function editorRow(name: string): HTMLElement {
  const row = document.getElementById(`limit-${name}`)
  expect(row, `no row with id limit-${name}`).not.toBeNull()
  return row as HTMLElement
}

/**
 * The side editor (L2, 2026-10-01): one panel beside the families, for the
 * pool whose `edit` was pressed. The Save, Cancel and field live here now,
 * not inside the row.
 */
function side(): HTMLElement {
  const panel = document.querySelector<HTMLElement>('aside.adm-side')
  expect(panel, 'no side editor is open').not.toBeNull()
  return panel!
}

/**
 * The ceiling field for one pool. Only the row being edited draws one (#132),
 * so this opens that row's editor first when it is not already open.
 */
function input(name: string): HTMLInputElement {
  const open = screen.queryByLabelText(`Hard limit for ${name}`, { exact: false })
  if (open !== null) return open as HTMLInputElement
  fireEvent.click(within(editorRow(name)).getByRole('button', { name: `Edit ceiling for ${name}` }))
  return screen.getByLabelText(`Hard limit for ${name}`, { exact: false }) as HTMLInputElement
}

describe('Pool limits draws no per-profile card (owner decision 2026-10-01)', () => {
  it('draws the ceilings alone: no Binding pool eyebrow, no profile card, no operand list', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    expect(screen.queryByText('Binding pool')).toBeNull()
    expect(document.querySelector('.ctl-cards')).toBeNull()
    expect(document.querySelector('.adm-operands')).toBeNull()
    expect(screen.queryByText('claude-code', { selector: '.ctl-card-title' })).toBeNull()
  })
})

describe('Pool limits labels its units on both figures (CP-24)', () => {
  it('says (units) on In use and on Ceiling, and in the stacked keys', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    const heads = [...document.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
    expect(heads).toContain('In use (units)')
    expect(heads).toContain('Ceiling (units)')
    for (const td of document.querySelectorAll('td[data-label^="In use"], td[data-label^="Ceiling"]')) {
      expect(td.getAttribute('data-label')).toContain('(units)')
    }
  })
})

describe('saving a ceiling re-reads without throwing the screen away (AH-7)', () => {
  it('paints saved, keeps it after the editor closes, and shows the re-read', async () => {
    const after = capacity({
      pools: [
        pool('global', { hard_limit: 25, effective_limit: 25 }),
        pool('tenant:eng'),
        pool('resource:standard', { hard_limit: 40, effective_limit: 40 }),
      ],
    })
    api.loadCapacity.mockResolvedValueOnce(ok(capacity())).mockResolvedValueOnce(ok(after))
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    render(<AdminSettingsScreen />)
    await limitsDrawn()

    fireEvent.change(input('global'), { target: { value: '25' } })
    fireEvent.click(within(side()).getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(api.loadCapacity).toHaveBeenCalledTimes(2)
      // The re-read is what the row now shows.
      expect(editorRow('global').querySelector('td[data-label^="Ceiling"] .adm-ceiling')!.textContent).toBe('25')
      // The read-back carries the value, so the editor closes on it.
      expect(screen.queryByLabelText('Hard limit for global', { exact: false })).toBeNull()
    }, WAIT)
    // The verdict painted, and stayed, on the closed row.
    expect(within(editorRow('global')).getByText('saved')).toBeTruthy()
    // Nothing was remounted: the other rows are the same elements.
    expect(api.setPoolLimit).toHaveBeenCalledWith('global', 25)
  })

  it('keeps an open editor, and what was typed in it, across a re-read', async () => {
    const after = capacity({
      pools: [
        pool('global', { hard_limit: 25, effective_limit: 25 }),
        pool('tenant:eng'),
        pool('resource:standard', { hard_limit: 40, effective_limit: 40 }),
      ],
    })
    let land: (r: Result<Capacity>) => void = () => {}
    api.loadCapacity
      .mockResolvedValueOnce(ok(capacity()))
      .mockReturnValueOnce(new Promise<Result<Capacity>>((resolve) => (land = resolve)))
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    render(<AdminSettingsScreen />)
    await limitsDrawn()

    fireEvent.change(input('global'), { target: { value: '25' } })
    fireEvent.click(within(side()).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.loadCapacity).toHaveBeenCalledTimes(2), WAIT)
    // While the read-back is in flight, somebody opens another row.
    fireEvent.change(input('tenant:eng'), { target: { value: '12' } })
    land(ok(after))
    await waitFor(() => {
      expect(editorRow('global').querySelector('td[data-label^="Ceiling"] .adm-ceiling')!.textContent).toBe('25')
    }, WAIT)
    expect(input('tenant:eng').value, 'the re-read threw the open edit away').toBe('12')
    expect(within(editorRow('global')).getByText('saved')).toBeTruthy()
  })

  it('a failed re-read after a successful write leaves the screen and the verdict on it', async () => {
    api.loadCapacity.mockResolvedValueOnce(ok(capacity())).mockResolvedValueOnce({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'Firestore did not answer.' },
    })
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    render(<AdminSettingsScreen />)
    await limitsDrawn()

    fireEvent.change(input('global'), { target: { value: '25' } })
    fireEvent.click(within(side()).getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(api.loadCapacity).toHaveBeenCalledTimes(2)
      expect(document.querySelector('table'), 'the write blanked the screen').not.toBeNull()
      expect(within(editorRow('global')).getByText('saved')).toBeTruthy()
      expect(within(editorRow('global')).getByText('not re-read')).toBeTruthy()
    }, WAIT)
  })
})

describe('an invalid ceiling is marked on its input (AH-8)', () => {
  it('sets aria-invalid, points at the message, and puts the message below the control row', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()

    const field = input('global')
    expect(field.getAttribute('aria-invalid')).not.toBe('true')

    fireEvent.change(field, { target: { value: '-1' } })
    expect(field.getAttribute('aria-invalid')).toBe('true')
    const ids = (field.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean)
    const messages = ids.map((id) => document.getElementById(id)).filter((el): el is HTMLElement => el !== null)
    const message = messages.find((m) => (m.textContent ?? '').includes('100000'))
    expect(message, 'the input does not point at the range it broke').toBeTruthy()
    // BELOW the row, not inside the wrapping flex strip it used to reflow.
    expect(message!.closest('.limit-edit')).toBeNull()

    fireEvent.change(field, { target: { value: '12' } })
    expect(field.getAttribute('aria-invalid')).not.toBe('true')
  })
})

// ---------------------------------------------------------------------------
// Pool limits: the owner's decisions on #132. Every block was written before
// the change it demands.
// ---------------------------------------------------------------------------

/** One pool of every family, handed over in an order that is none of them. */
function everyFamily(): Capacity {
  return capacity({
    pools: [
      pool('provider:anthropic', { hard_limit: 30, effective_limit: 30 }),
      pool('runner:claude-code'),
      pool('tenant:eng'),
      pool('backend:cloudrun'),
      pool('resource:standard', { hard_limit: 40, effective_limit: 40 }),
      pool('global'),
      pool('tenant:research'),
    ],
  })
}

/** Stub `matchMedia`, which jsdom does not have, as a phone or a desktop. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

function rowIds(): string[] {
  return [...document.querySelectorAll('tbody tr[id^="limit-"]')].map((tr) => tr.id)
}

describe('Pool limits groups its ceilings the way Capacity > Pools does (#132)', () => {
  it('draws one family per group, in POOL_FAMILY_ORDER, under the Pools eyebrows', async () => {
    api.loadCapacity.mockResolvedValue(ok(everyFamily()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    const families = [...document.querySelectorAll('.adm-family')]
    const titles = families.map((f) => f.querySelector('.ctl-card-title')?.textContent)
    expect(titles).toEqual(['Global', 'Tenants', 'Resource classes', 'Runner profiles', 'Backends', 'Providers'])
    // Each family holds its own pools and nothing else, sorted by name.
    const tenants = families[1]!
    expect([...tenants.querySelectorAll('tbody tr')].map((tr) => tr.id)).toEqual(['limit-tenant:eng', 'limit-tenant:research'])
    // MUTATION: sort every row by name again, as one table.
    expect(rowIds()[0]).toBe('limit-global')
    expect(rowIds().at(-1)).toBe('limit-provider:anthropic')
  })

  it('gives every row an id a link can name', async () => {
    api.loadCapacity.mockResolvedValue(ok(everyFamily()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    expect(rowIds().sort()).toEqual(everyFamily().pools.map((p) => `limit-${p.name}`).sort())
  })

  it('lands on the row a link names, and marks it', async () => {
    window.location.hash = '#admin/limits?pool=tenant%3Aresearch'
    try {
      api.loadCapacity.mockResolvedValue(ok(everyFamily()))
      render(<AdminSettingsScreen />)
      await limitsDrawn()
      await waitFor(() => expect(editorRow('tenant:research').classList.contains('is-target')).toBe(true), WAIT)
      expect(document.querySelectorAll('tr.is-target').length).toBe(1)
    } finally {
      window.location.hash = ''
    }
  })
})

describe('Pool limits shows a ceiling as a value with one editor open at a time (#132)', () => {
  it('draws no input, save or cancel until a row is edited, and then only in that row', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    // At rest: every row is its value and an edit control.
    expect(document.querySelectorAll('tbody input').length, 'a row draws a field nobody opened').toBe(0)
    expect(screen.queryAllByRole('button', { name: 'Save' })).toHaveLength(0)
    expect(screen.queryAllByRole('button', { name: 'Cancel' })).toHaveLength(0)
    for (const tr of document.querySelectorAll('tbody tr')) {
      expect(within(tr as HTMLElement).getByRole('button', { name: /^Edit ceiling for / }).textContent).toBe('edit')
      expect(tr.querySelector('td[data-label^="Ceiling"] .adm-ceiling')!.textContent).toMatch(/^\d+$/)
    }

    // THE TABLE STAYS STILL (L2): the field opens in the side editor, never
    // inside a row, and the row being edited is marked.
    expect(document.querySelector('aside.adm-side'), 'an editor nobody opened').toBeNull()
    input('global')
    expect(document.querySelectorAll('tbody input').length, 'the editor reflowed a row').toBe(0)
    expect(document.querySelectorAll('aside.adm-side').length).toBe(1)
    expect(side().getAttribute('data-pool')).toBe('global')
    expect(within(side()).getByRole('button', { name: 'Save' })).toBeTruthy()
    expect(within(side()).getByRole('button', { name: 'Cancel' })).toBeTruthy()
    expect(editorRow('global').classList.contains('is-editing')).toBe(true)

    // Opening another row replaces the one editor.
    input('tenant:eng')
    expect(document.querySelectorAll('aside.adm-side').length).toBe(1)
    expect(side().getAttribute('data-pool')).toBe('tenant:eng')
    expect(screen.queryByLabelText('Hard limit for global', { exact: false })).toBeNull()
    expect(editorRow('global').classList.contains('is-editing')).toBe(false)

    // Cancel closes it and writes nothing.
    fireEvent.change(input('tenant:eng'), { target: { value: '3' } })
    fireEvent.click(within(side()).getByRole('button', { name: 'Cancel' }))
    expect(document.querySelector('aside.adm-side')).toBeNull()
    expect(api.setPoolLimit).not.toHaveBeenCalled()
    // And a re-open starts from the pool's own value, not the abandoned one.
    expect(input('tenant:eng').value).toBe('10')
  })

  it('keeps other rows’ edit disabled while the open row holds an unsaved value', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    fireEvent.change(input('global'), { target: { value: '7' } })
    const other = within(editorRow('tenant:eng')).getByRole('button', { name: /^Edit ceiling for tenant:eng/ })
    expect((other as HTMLButtonElement).disabled, 'a click elsewhere would drop the typed 7').toBe(true)
    expect(other.getAttribute('aria-label')).toMatch(/unsaved value/)
    // Typed back to the pool's own value, nothing is held: the others open again.
    fireEvent.change(input('global'), { target: { value: '10' } })
    expect((within(editorRow('tenant:eng')).getByRole('button', { name: 'Edit ceiling for tenant:eng' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('says lowering evicts nothing beside the open editor, not in the caption', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    // What a reader sees: the `?` card's copy is in the DOM whether it is
    // open or shut, and is not on the glass.
    const seen = () => {
      const copy = document.body.cloneNode(true) as HTMLElement
      for (const hidden of copy.querySelectorAll('[data-help-description], button[aria-label^="Help: "]')) hidden.remove()
      return copy.textContent ?? ''
    }
    for (const caption of document.querySelectorAll('caption')) {
      expect(caption.textContent ?? '').not.toMatch(/evicts nothing/)
    }
    expect(seen()).not.toMatch(/evicts nothing/)
    input('tenant:eng')
    const said = within(side()).getByText('lowering a ceiling evicts nothing')
    // Beside the control it qualifies: in the open editor, with its Save.
    expect(said.closest('aside')).toBe(side())
    expect(seen().match(/evicts nothing/g)).toHaveLength(1)
  })

  it('names who set a ceiling only when it is not the configured value', async () => {
    api.loadCapacity.mockResolvedValue(
      ok(
        capacity({
          pools: [
            pool('global'),
            pool('tenant:eng'),
            pool('provider:anthropic', { hard_limit: 30, quota_derived_limit: 6, effective_limit: 6 }),
          ],
        }),
      ),
    )
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    expect(document.body.textContent ?? '').not.toMatch(/configured/)
    const quotaRow = editorRow('provider:anthropic')
    expect(quotaRow.querySelector('td[data-label="Set by"]')?.textContent).toBe('provider quota')
    // A family where everything is as configured still draws the Set by
    // column, so every family has the same columns (#503) -- but its cells
    // are empty: `configured` there is the column restating the Ceiling.
    const global = document.querySelector('.adm-family')!
    expect([...global.querySelectorAll('thead th')].map((th) => th.textContent)).toContain('Set by')
    expect(editorRow('global').querySelector('td[data-label="Set by"]')?.textContent).toBe('')
  })
})

describe('Pool limits filters its rows by name at phone width (#132)', () => {
  it('draws no filter on a desktop', async () => {
    media(false)
    api.loadCapacity.mockResolvedValue(ok(everyFamily()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    expect(screen.queryByRole('searchbox', { name: /filter pools/i })).toBeNull()
  })

  it('filters the rows, and the families, by pool name at 560px and under', async () => {
    media(true)
    api.loadCapacity.mockResolvedValue(ok(everyFamily()))
    render(<AdminSettingsScreen />)
    await limitsDrawn()
    const box = screen.getByRole('searchbox', { name: /filter pools/i })
    fireEvent.change(box, { target: { value: 'ENG' } })
    expect(rowIds()).toEqual(['limit-tenant:eng'])
    expect([...document.querySelectorAll('.adm-family .ctl-card-title')].map((t) => t.textContent)).toEqual(['Tenants'])
    fireEvent.change(box, { target: { value: 'nothing-is-called-this' } })
    expect(rowIds()).toEqual([])
    expect(screen.getByText('no pool matches')).toBeTruthy()
    fireEvent.change(box, { target: { value: '' } })
    expect(rowIds()).toHaveLength(everyFamily().pools.length)
  })
})

// ---------------------------------------------------------------------------
// Provider quota (CP-22)
// ---------------------------------------------------------------------------

function quota(over: Partial<QuotaState>): QuotaState {
  return {
    provider: 'anthropic',
    tenant_id: 'eng',
    state: 'HEALTHY',
    updated_at: '2026-09-25T10:00:00Z',
    configured_hard_max: 50,
    adaptive_target: null,
    quota_derived_limit: null,
    requests_remaining: 120,
    tokens_remaining: null,
    reset_at: null,
    cooldown_until: null,
    last_429_at: null,
    retry_after_seconds: null,
    success_count: 10,
    rate_limit_count: 0,
    effective_limit: 50,
    ...over,
  } as QuotaState
}

describe('Provider quota counts in English (CP-22)', () => {
  it('says 1 document, 1 provider, 1 tenant', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota({})] }))
    render(<QuotaDetailScreen />)
    await screen.findByRole('rowheader', { name: 'eng' }, WAIT)
    const summary = document.querySelector('p.sub')!.textContent ?? ''
    expect(summary).toContain('1 document')
    expect(summary).toContain('1 provider')
    expect(summary).toContain('1 tenant')
    expect(summary).not.toMatch(/\b1 (documents|providers|tenants)\b/)
  })

  it('keeps the plural for more than one', async () => {
    api.loadAdminQuota.mockResolvedValue(
      ok({ quota: [quota({}), quota({ tenant_id: 'research' }), quota({ provider: 'openai' })] }),
    )
    render(<QuotaDetailScreen />)
    await screen.findAllByRole('rowheader', { name: 'eng' }, WAIT)
    const summary = document.querySelector('p.sub')!.textContent ?? ''
    expect(summary).toContain('3 documents')
    expect(summary).toContain('2 providers')
    expect(summary).toContain('2 tenants')
  })

  it('puts each provider’s tenant count in the card-note slot its siblings use', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota({})] }))
    render(<QuotaDetailScreen />)
    await screen.findByRole('rowheader', { name: 'eng' }, WAIT)
    expect(document.querySelector('.count-chip, .c-chip.is-n')).toBeNull()
    const note = document.querySelector('.ctl-toolbar > .ctl-card-note')
    expect(note, 'the tenant count is not a card note').not.toBeNull()
    expect(note!.textContent).toBe('1 tenant')
  })
})

// ---------------------------------------------------------------------------
// Provider quota: the owner's decisions on #85, 2026-09-25 (CP-8, CP-9,
// CP-10). Each block was pushed before the change it pins.
// ---------------------------------------------------------------------------

/** A reported instant `minutes` before now. */
function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString()
}

/** The column names of the first quota table, as a reader reads them. */
function quotaHeads(): string[] {
  return [...document.querySelectorAll('table thead th')].map((th) => (th.textContent ?? '').trim())
}

async function renderQuota(rows: QuotaState[]): Promise<HTMLElement> {
  api.loadAdminQuota.mockResolvedValue(ok({ quota: rows }))
  render(<QuotaDetailScreen />)
  const [th] = await screen.findAllByRole('rowheader', { name: rows[0]!.tenant_id }, WAIT)
  return th!.closest('tr') as HTMLElement
}

describe('Provider quota says what its cap is and which pool it feeds (CP-8)', () => {
  it('calls the cap a quota cap, in the header and in the stacked key', async () => {
    const row = await renderQuota([quota({ state: 'AVAILABLE', updated_at: minutesAgo(1) })])
    expect(quotaHeads()).toContain('Quota cap')
    expect(quotaHeads()).not.toContain('Limit')
    // The value is unchanged: the document's effective_limit.
    expect(row.querySelector('td[data-label="Quota cap"]')?.textContent).toBe('50')
    expect(row.querySelector('td[data-label="Limit"]')).toBeNull()
  })

  it('names the pool the cap feeds, right after it, as a link to Pools', async () => {
    const row = await renderQuota([quota({ state: 'AVAILABLE', updated_at: minutesAgo(1) })])
    const heads = quotaHeads()
    expect(heads.indexOf('Feeds pool'), heads.join(' | ')).toBe(heads.indexOf('Quota cap') + 1)
    const link = row.querySelector('td[data-label="Feeds pool"] a')
    expect(link, 'the pool is not a link').not.toBeNull()
    // To THAT pool's row on Pools (#128), not to the top of the screen.
    expect(link!.getAttribute('href')).toBe(`#capacity/pools?pool=${encodeURIComponent('provider:anthropic:tenant:eng')}`)
    expect(link!.textContent).toBe('provider:anthropic:tenant:eng')
    // The full name survives an ellipsis below 900px (CH-13).
    expect(link!.getAttribute('title')).toBe('provider:anthropic:tenant:eng')
    expect(link!.className).toContain('ctl-link')
  })
})

describe('Provider quota says which run its 429 count is of (CP-10)', () => {
  it('names the window in the header and in the stacked key', async () => {
    const row = await renderQuota([
      quota({ state: 'AVAILABLE', updated_at: minutesAgo(1), rate_limit_count: 0, last_429_at: minutesAgo(25 * 60) }),
    ])
    expect(quotaHeads()).toContain('429s (this run)')
    expect(quotaHeads()).not.toContain('429s')
    const count = row.querySelector('td[data-label="429s (this run)"]')
    expect(count?.textContent).toBe('0')
    // The time of the last 429 stays, beside a count that has been reset.
    expect(row.querySelector('td[data-label="Last 429"]')?.textContent).toContain('ago')
  })

  it('puts no help glyph inside the quota tables, and indexes the row fields in the footer', async () => {
    await renderQuota([
      quota({ state: 'AVAILABLE', updated_at: minutesAgo(1) }),
      quota({ provider: 'openai', state: 'AVAILABLE', updated_at: minutesAgo(1) }),
    ])
    const tables = [...document.querySelectorAll('table')]
    expect(tables.length).toBe(2)
    for (const t of tables) {
      expect(t.querySelector('button[aria-expanded]'), 'a ? sits inside a quota table').toBeNull()
    }
    expect(document.querySelector('a[href="#help/quota-row-fields"]'), 'the footer does not index the row fields').not.toBeNull()
  })
})

describe('Provider quota never draws an old reading as a current verdict (CP-9)', () => {
  /** The State cell of the only row. */
  function state(row: HTMLElement): HTMLElement {
    const cell = row.querySelector('td[data-label="State"]')
    expect(cell, 'the row has no State cell').not.toBeNull()
    return cell as HTMLElement
  }

  it('draws a reading five days old with the stale mark and its age, not the ok chip', async () => {
    const cell = state(await renderQuota([quota({ state: 'AVAILABLE', updated_at: minutesAgo(5 * 24 * 60) })]))
    expect(cell.querySelector('.sk-st[data-tone="ok"]'), 'a five-day-old reading is drawn as a current verdict').toBeNull()
    expect(cell.querySelector('.ctl-stale-mark')?.textContent).toContain('5d')
    // The word it last reported is still on the row, marked, not hidden.
    expect((cell.textContent ?? '').toLowerCase()).toContain('available')
  })

  // SINCE #503 `available` IS ONE PICTURE: the neutral word, no verdict mark,
  // fresh or stale -- it was a grey dot on one row and QUEUED's ring on
  // another. What says a reading is old is the stale mark, and only that, so
  // a current reading is the one WITHOUT it.
  it('draws a reading well inside twice the broker’s interval as current', async () => {
    const cell = state(await renderQuota([quota({ state: 'AVAILABLE', updated_at: minutesAgo(4) })]))
    expect(cell.querySelector('.quota-state[data-tone="neu"]')?.textContent).toBe('available')
    expect(cell.querySelector('.ctl-stale-mark')).toBeNull()
  })

  it('draws a reading past twice the broker’s five-minute interval as stale', async () => {
    const cell = state(await renderQuota([quota({ state: 'AVAILABLE', updated_at: minutesAgo(11) })]))
    expect(cell.querySelector('.sk-st[data-tone="ok"]')).toBeNull()
    expect(cell.querySelector('.ctl-stale-mark')?.textContent).toContain('11m')
  })

  it('keeps a verdict that is not ok on a stale reading, and marks its age', async () => {
    const cell = state(await renderQuota([quota({ state: 'THROTTLED', updated_at: minutesAgo(60) })]))
    // A condition, so the amber warning triangle (brand §3), kept on a stale row.
    expect(cell.querySelector('.quota-state [data-mark="warn"]')).not.toBeNull()
    expect(cell.querySelector('.ctl-stale-mark')?.textContent).toContain('1h')
  })

  /**
   * #159 REVIEW: THE FIVE MINUTES ARE THE BROKER'S SWEEP, AND NOTHING REPORTS
   * ON THEM. The constant is the `quota-refresh` Cloud Scheduler cron, which
   * runs `QuotaService.sweep` -- and the sweep rewrites a document only when
   * its state or its derived cap changes. `updated_at` moves when a WORKER
   * reports: at the end of a clean run, or on a 429. The screen told its
   * reader the threshold was twice "the broker's reporting interval", a report
   * that does not exist. The words have to name the tick they are twice of.
   */
  it('names the tick its threshold is twice of as the broker’s sweep, not a reporting interval', async () => {
    const cell = state(await renderQuota([quota({ state: 'AVAILABLE', updated_at: minutesAgo(11) })]))
    const title = cell.querySelector('.ctl-stale-mark')?.getAttribute('title') ?? ''
    expect(title, 'the stale mark has no title').not.toBe('')
    expect(title).not.toMatch(/reporting interval/i)
    expect(title).toMatch(/sweep/i)

    const topic = HELP['provider-quota-states']
    const said = [topic.short, ...topic.long, ...(topic.values?.() ?? []).map((v) => `${v.term} ${v.note ?? ''}`)].join(' ')
    expect(said).not.toMatch(/reporting interval/i)
    expect(said).toMatch(/sweep/i)
  })
})
