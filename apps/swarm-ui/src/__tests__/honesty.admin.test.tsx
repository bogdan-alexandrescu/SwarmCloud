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
import type { Capacity, Pool, QuotaState, RunnerProfile } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  setPoolLimit: vi.fn(),
  loadAdminQuota: vi.fn(),
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

/** The Pool limits card for one runner profile. */
async function limitCard(name: string): Promise<HTMLElement> {
  const title = await screen.findByText(name, { selector: '.ctl-card-title' }, WAIT)
  return title.closest('.ctl-card') as HTMLElement
}

/** The editor row for one pool, found by the raw name it prints. */
function editorRow(name: string): HTMLElement {
  const input = screen.getByLabelText(`Hard limit for ${name}`, { exact: false })
  return input.closest('tr') as HTMLElement
}

function input(name: string): HTMLInputElement {
  return screen.getByLabelText(`Hard limit for ${name}`, { exact: false }) as HTMLInputElement
}

describe('Pool limits names every pool that binds (AH-1)', () => {
  it('marks every operand equal to the minimum, not the first of them', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    const card = await limitCard('claude-code')
    const binding = [...card.querySelectorAll('.adm-operands > li.is-binding')].map((li) => li.getAttribute('title'))
    expect(binding.sort()).toEqual(['global', 'tenant:eng'])
    expect(card.querySelector('li[title="resource:standard"]')!.className).not.toContain('is-binding')
  })

  it('names all of them in the foot and in the figure’s accessible name', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    const card = await limitCard('claude-code')
    const foot = card.querySelector('.ctl-card-foot')!.textContent ?? ''
    expect(foot).toContain('global')
    expect(foot).toContain('eng')
    const figure = card.querySelector('.ctl-figure')!.getAttribute('aria-label') ?? ''
    expect(figure).toContain('global')
    expect(figure).toContain('eng')
  })

  it('never prints two different pools under one label', async () => {
    api.loadCapacity.mockResolvedValue(
      ok(
        capacity({
          pools: [pool('resource:browser'), pool('runner:browser')],
          runner_profiles: { browser: profile(['resource:browser', 'runner:browser']) },
        }),
      ),
    )
    render(<AdminSettingsScreen />)
    const card = await limitCard('browser')
    const labels = [...card.querySelectorAll('.adm-operands > li > b')].map((b) => b.textContent ?? '')
    expect(labels.length).toBe(2)
    expect(new Set(labels).size, `two pools share a label: ${labels.join(' | ')}`).toBe(2)
    expect(card.querySelector('.ctl-card-foot')!.textContent).not.toMatch(/browser and browser/)
  })

  it('puts each operand’s figure in its own element, so it can sit on a numeric track (AH-19)', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    const card = await limitCard('claude-code')
    for (const li of card.querySelectorAll('.adm-operands > li')) {
      const value = li.querySelector('.adm-value')
      expect(value, `${li.getAttribute('title')}: the figure is a bare text node`).not.toBeNull()
      expect(value!.textContent).toMatch(/^\d+$|^—$/)
    }
  })
})

describe('Pool limits labels its units on both figures (CP-24)', () => {
  it('says (units) on In use and on Ceiling, and in the stacked keys', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitCard('claude-code')
    const heads = [...document.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
    expect(heads).toContain('In use (units)')
    expect(heads).toContain('Ceiling (units)')
    for (const td of document.querySelectorAll('td[data-label^="In use"], td[data-label^="Ceiling"]')) {
      expect(td.getAttribute('data-label')).toContain('(units)')
    }
  })
})

describe('saving a ceiling re-reads without throwing the screen away (AH-7)', () => {
  it('keeps an unsaved edit in another row, paints saved, and shows the re-read', async () => {
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
    await limitCard('claude-code')

    // An edit somebody has not saved yet.
    fireEvent.change(input('tenant:eng'), { target: { value: '12' } })
    // And a save in another row.
    fireEvent.change(input('global'), { target: { value: '25' } })
    fireEvent.click(within(editorRow('global')).getByRole('button', { name: 'save' }))

    await waitFor(() => {
      expect(api.loadCapacity).toHaveBeenCalledTimes(2)
      // The re-read is what the row now shows.
      expect(editorRow('global').querySelector('td[data-label^="Ceiling"]')!.textContent).toBe('25')
    }, WAIT)
    // The verdict painted, and stayed.
    expect(within(editorRow('global')).getByText('saved')).toBeTruthy()
    // The other row's edit survived the re-read.
    expect(input('tenant:eng').value).toBe('12')
    expect(api.setPoolLimit).toHaveBeenCalledWith('global', 25)
  })

  it('a failed re-read after a successful write leaves the screen and the verdict on it', async () => {
    api.loadCapacity.mockResolvedValueOnce(ok(capacity())).mockResolvedValueOnce({
      status: 'error',
      error: { kind: 'upstream_degraded', httpStatus: 503, code: 'unavailable', message: 'Firestore did not answer.' },
    })
    api.setPoolLimit.mockResolvedValue({ status: 'ok', data: {}, fetchedAt: Date.now() })
    render(<AdminSettingsScreen />)
    await limitCard('claude-code')

    fireEvent.change(input('global'), { target: { value: '25' } })
    fireEvent.click(within(editorRow('global')).getByRole('button', { name: 'save' }))

    await waitFor(() => {
      expect(api.loadCapacity).toHaveBeenCalledTimes(2)
      expect(document.querySelector('table'), 'the write blanked the screen').not.toBeNull()
      expect(within(editorRow('global')).getByText('saved')).toBeTruthy()
    }, WAIT)
  })
})

describe('an invalid ceiling is marked on its input (AH-8)', () => {
  it('sets aria-invalid, points at the message, and puts the message below the control row', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    render(<AdminSettingsScreen />)
    await limitCard('claude-code')

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
    expect(document.querySelector('.count-chip')).toBeNull()
    const note = document.querySelector('.ctl-toolbar > .ctl-card-note')
    expect(note, 'the tenant count is not a card note').not.toBeNull()
    expect(note!.textContent).toBe('1 tenant')
  })
})
