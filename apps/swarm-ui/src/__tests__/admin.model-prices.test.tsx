// PRICES-UI (part of #1481): Admin › Pool limits shows and edits the
// `model_prices` platform setting beside the other platform settings.
//
//   * one row per model, with the four per-million-token figures, the
//     currency, the source and `as_of`, as `GET /v1/admin/settings` served them;
//   * an admin edits, and a save PUTs `model_prices` with the changed row only,
//     a whole entry, dated today; anyone else reads the figures with no field;
//   * a refusal whose `loc` names a figure is printed at that figure;
//   * a model a runner pins that the table does not price reads "cost not
//     reported" -- and a dated spelling of a priced model is not that row;
//   * a saved change says who saved it and when, off the response.
//
// MUTATIONS, each of which turns a case red: drop the card from the screen;
// PUT every row instead of the changed one; send `as_of` unchanged; print the
// refusal in the banner only; draw `0` for a model with no figure; drop the
// suffix rule so `claude-haiku-4-5-20251001` grows an unpriced row; draw an
// input for a non-admin.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'

const mocks = vi.hoisted(() => ({ loadModelClasses: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, loadModelClasses: mocks.loadModelClasses }
})

const { ModelPricesCard } = await import('../AdminSettings')

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch

const entry = (inp: number, out: number, read: number, write: number, over: Record<string, unknown> = {}) => ({
  input: inp, output: out, cache_read: read, cache_write: write,
  currency: 'USD', source: 'code', as_of: '2026-10-05', updated_by: 'agent_worker.lifecycle._LIST_PRICES', ...over,
})

const SETTINGS = {
  merge_by_default: false, max_additional_repositories: 5, additional_repositories_budget_gib: 6,
  model_prices: {
    'claude-opus-5-5': entry(4, 20, 0.2, 5),
    'claude-haiku-4-5': entry(1, 5, 0.1, 1.25, { source: 'console', as_of: '2026-10-18', updated_by: 'ops@example.com' }),
  },
  updated_at: null, updated_by: null, settings_document: 'present',
}

/** Two runners: one pins a model the table prices by a dated spelling, one a model it does not price. */
function pins(): Result<Record<string, { model_classes: string[]; default_model_class: string | null; models: Record<string, string> }>> {
  return {
    status: 'ok', fetchedAt: Date.now(),
    data: {
      'claude-code': { model_classes: ['deep', 'quick'], default_model_class: 'deep',
        models: { deep: 'claude-opus-5-5', quick: 'claude-haiku-4-5-20251001' } },
      'claude-code-gke': { model_classes: ['middle'], default_model_class: 'middle', models: { middle: 'claude-sonnet-5-5' } },
    },
  }
}

interface Call { method: string; url: string; body: unknown }
type Answer = { status: number; body: unknown }

function serve(put: (body: Record<string, unknown>) => Answer): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = init?.body ? JSON.parse(String(init.body)) : null
    calls.push({ method, url, body })
    const r = url !== '/v1/admin/settings' ? null
      : method === 'GET' ? { status: 200, body: SETTINGS }
      : method === 'PUT' ? put(body as Record<string, unknown>) : null
    if (r === null) throw new Error(`unexpected request ${method} ${url}`)
    return new Response(JSON.stringify(r.body), { status: r.status, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  return calls
}

const row = (model: string) => document.querySelector(`tr[data-model="${model}"]`) as HTMLTableRowElement
const field = (label: string, model: string) => screen.getByLabelText(`${label}, ${model}`) as HTMLInputElement
const save = () => screen.getByRole('button', { name: 'Save prices' }) as HTMLButtonElement
const puts = (calls: Call[]) => calls.filter((c) => c.method === 'PUT').map((c) => c.body)

describe('Admin › Pool limits shows and edits the model price table', () => {
  afterEach(() => {
    globalThis.fetch = realFetch
  })

  it('draws one row per model with the served figures, the currency, the source and as_of', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    serve(() => ({ status: 500, body: {} }))
    render(<ModelPricesCard admin />)
    await waitFor(() => expect(field('Input', 'claude-opus-5-5').value).toBe('4'), WAIT)
    expect(field('Output', 'claude-opus-5-5').value).toBe('20')
    expect(field('Cache read', 'claude-opus-5-5').value).toBe('0.2')
    expect(field('Cache write', 'claude-opus-5-5').value).toBe('5')
    expect(field('As of', 'claude-opus-5-5').value).toBe('2026-10-05')
    const opus = row('claude-opus-5-5')
    expect(opus.querySelector('td[data-label="Currency"]')!.textContent).toBe('USD')
    expect(opus.querySelector('td[data-label="Source"]')!.textContent).toBe('code')
    const haiku = row('claude-haiku-4-5')
    expect(haiku.querySelector('td[data-label="Source"]')!.textContent).toBe('console')
    expect(haiku.querySelector('td[data-label="Set by"]')!.textContent).toBe('ops@example.com')
    expect(save().disabled, 'nothing changed yet').toBe(true)
  })

  it('reads "cost not reported" for a pinned model with no figure, and draws no figure for it', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    serve(() => ({ status: 500, body: {} }))
    render(<ModelPricesCard admin={false} />)
    await waitFor(() => expect(row('claude-sonnet-5-5')).not.toBeNull(), WAIT)
    const sonnet = row('claude-sonnet-5-5')
    expect(within(sonnet).getByText('cost not reported')).toBeTruthy()
    for (const td of sonnet.querySelectorAll('td.is-num')) expect(td.textContent).toBe('—')
    // A dated spelling of a priced model is that model, not an unpriced row.
    expect(row('claude-haiku-4-5-20251001')).toBeNull()
    expect(document.querySelectorAll('tbody tr')).toHaveLength(3)
  })

  it('lets a non-admin read the figures and draws no field to type in', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    serve(() => ({ status: 500, body: {} }))
    render(<ModelPricesCard admin={false} />)
    await waitFor(() => expect(row('claude-opus-5-5')).not.toBeNull(), WAIT)
    expect(row('claude-opus-5-5').querySelector('td[data-label="Input /M"]')!.textContent).toBe('4')
    expect(document.querySelector('.adm-prices input')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Save prices' })).toBeNull()
  })

  it('PUTs only the changed row, as a whole entry dated today, and says who saved it and when', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    const at = new Date().toISOString()
    const calls = serve((body) => ({
      status: 200,
      body: {
        ...SETTINGS,
        model_prices: { ...SETTINGS.model_prices, ...Object.fromEntries(Object.entries(body.model_prices as Record<string, object>)
          .map(([m, e]) => [m, { ...e, currency: 'USD', source: 'console', updated_by: 'admin@example.com' }])) },
        updated_at: at, updated_by: 'admin@example.com',
      },
    }))
    render(<ModelPricesCard admin />)
    await waitFor(() => expect(field('Input', 'claude-haiku-4-5').value).toBe('1'), WAIT)
    fireEvent.change(field('Input', 'claude-haiku-4-5'), { target: { value: '1.5' } })
    fireEvent.click(save())
    const status = await screen.findByRole('status', {}, WAIT)
    expect(puts(calls)).toEqual([{
      model_prices: { 'claude-haiku-4-5': { input: 1.5, output: 5, cache_read: 0.1, cache_write: 1.25, as_of: at.slice(0, 10) } },
    }])
    expect(status.textContent).toContain('claude-haiku-4-5')
    expect(status.textContent).toContain('admin@example.com')
    expect(status.textContent).toMatch(/ago|now/)
    expect(row('claude-haiku-4-5').querySelector('td[data-label="Set by"]')!.textContent).toBe('admin@example.com')
  })

  it('gives an unpriced model its first figure, and sends nothing while one of its four is blank', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    const calls = serve((body) => ({ status: 200, body: { ...SETTINGS, ...body } }))
    render(<ModelPricesCard admin />)
    await waitFor(() => expect(row('claude-sonnet-5-5')).not.toBeNull(), WAIT)
    fireEvent.change(field('Input', 'claude-sonnet-5-5'), { target: { value: '3' } })
    fireEvent.change(field('Output', 'claude-sonnet-5-5'), { target: { value: '15' } })
    fireEvent.change(field('Cache read', 'claude-sonnet-5-5'), { target: { value: '0.3' } })
    expect(save().disabled, 'cache write is still blank').toBe(true)
    fireEvent.change(field('Cache write', 'claude-sonnet-5-5'), { target: { value: '3.75' } })
    fireEvent.change(field('As of', 'claude-sonnet-5-5'), { target: { value: '2026-10-19' } })
    fireEvent.click(save())
    await screen.findByRole('status', {}, WAIT)
    expect(puts(calls)).toEqual([{
      model_prices: { 'claude-sonnet-5-5': { input: 3, output: 15, cache_read: 0.3, cache_write: 3.75, as_of: '2026-10-19' } },
    }])
  })

  it('prints a refusal at the figure it names', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    serve(() => ({
      status: 422,
      body: {
        code: 'validation_failed', message: 'request body failed validation',
        detail: { errors: [{ loc: ['body', 'model_prices', 'claude-opus-5-5', 'output'],
          msg: 'Input should be less than or equal to 10000', type: 'less_than_equal' }] },
      },
    }))
    render(<ModelPricesCard admin />)
    await waitFor(() => expect(field('Output', 'claude-opus-5-5').value).toBe('20'), WAIT)
    fireEvent.change(field('Output', 'claude-opus-5-5'), { target: { value: '20000' } })
    fireEvent.click(save())
    await waitFor(() => expect(field('Output', 'claude-opus-5-5').getAttribute('aria-invalid')).toBe('true'), WAIT)
    const cell = row('claude-opus-5-5').querySelector('td[data-label="Output /M"]') as HTMLElement
    expect(within(cell).getByRole('alert').textContent).toBe('Input should be less than or equal to 10000')
    expect(field('Input', 'claude-opus-5-5').getAttribute('aria-invalid')).toBeNull()
    expect(screen.queryByText('The prices were not saved')).toBeNull()
  })

  it('says a non-admin cannot read the table when the API refuses the read', async () => {
    mocks.loadModelClasses.mockResolvedValue(pins())
    globalThis.fetch = vi.fn(async () => new Response(JSON.stringify({ code: 'admin_required', message: 'admins only' }),
      { status: 403, headers: JSON_HEADERS })) as unknown as typeof fetch
    render(<ModelPricesCard admin={false} />)
    expect(await screen.findByText('Platform admins only.', {}, WAIT)).toBeTruthy()
    expect(document.querySelector('.adm-prices table')).toBeNull()
  })
})
