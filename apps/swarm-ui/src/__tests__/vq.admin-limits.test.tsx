// VQ-054 (lane VQ-ADMIN, part of #2391): Admin › Pool limits, against
// config-e.html S10b ("The cards below it (Additional repositories, Model
// prices, GitHub fallback token) are unchanged and folded here").
//
//   * the three cards under the ceilings table are drawn folded, each a head
//     that opens it, and a card drawn on its own (as its tests draw it) is
//     open, with no fold;
//   * the Model prices card's Set by wraps a writer that is not an address
//     (`agent_worker.lifecycle._LIST_PRICES`) at its dots, rather than holding
//     the whole name on one line past the card's right edge.
//
// Fixtures only; no network. Every value is built at runtime.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'

const CARDS = ['Additional repositories', 'Model prices', 'GitHub fallback token'] as const

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../AdminSettings')
}

describe('VQ-054: the three cards under the ceilings are folded', () => {
  it('draws each as a closed head that opens it', async () => {
    serve(() => null)
    const { AdminSettingsScreen } = await load()
    render(<AdminSettingsScreen />)
    for (const title of CARDS) {
      // MUTATION: draw the cards open again (no `fold` from the page) -> no such button and this fails.
      const head = await screen.findByRole('button', { name: title })
      expect(head.getAttribute('aria-expanded'), title).toBe('false')
      const body = document.getElementById(head.getAttribute('aria-controls')!)!
      expect(body.hidden, `${title} is drawn open`).toBe(true)
      // The head is the card's own heading, not a second one above it.
      expect(head.closest('h2')).not.toBeNull()
      expect(screen.getAllByRole('heading', { name: title })).toHaveLength(1)
    }
    const prices = screen.getByRole('button', { name: 'Model prices' })
    fireEvent.click(prices)
    expect(prices.getAttribute('aria-expanded')).toBe('true')
    expect(document.getElementById(prices.getAttribute('aria-controls')!)!.hidden).toBe(false)
    // The others stay folded: one press opens one card.
    expect(screen.getByRole('button', { name: 'Additional repositories' }).getAttribute('aria-expanded')).toBe('false')
  })

  it('a card drawn on its own is open, with no fold', async () => {
    serve(() => null)
    const { FallbackTokenCard } = await load()
    render(<FallbackTokenCard />)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'GitHub fallback token' })).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'GitHub fallback token' })).toBeNull()
    expect(screen.getByLabelText('Personal access token')).toBeTruthy()
  })
})

describe('VQ-054: Set by wraps a writer that is not an address', () => {
  it('breaks `agent_worker.lifecycle._LIST_PRICES` after its dots, and an address after its @ only', async () => {
    const entry = (by: string) => ({ input: 4, output: 20, cache_read: 0.2, cache_write: 5, currency: 'USD', source: 'code', as_of: '2026-10-05', updated_by: by })
    serve((_m, url) => (url === '/v1/settings/model-prices'
      ? { status: 200, body: { model_prices: { 'claude-opus-5-5': entry('agent_worker.lifecycle._LIST_PRICES'), 'claude-haiku-4-5': entry('ops@example.com') } } }
      : null))
    const { ModelPricesCard } = await load()
    render(<ModelPricesCard admin={false} />)
    const table = await screen.findByRole('table', { name: 'Model prices' })
    await waitFor(() => expect(table.querySelector('tr[data-model="claude-opus-5-5"]')).not.toBeNull())
    const segs = (model: string) =>
      [...within(table.querySelector<HTMLElement>(`tr[data-model="${model}"]`)!).getByText((_, el) => el?.tagName === 'TD' && el.getAttribute('data-label') === 'Set by').querySelectorAll('.adm-addr-seg')].map((s) => s.textContent)
    // MUTATION: break every writer after `@` only -> one nowrap run of 35 characters and this fails.
    expect(segs('claude-opus-5-5')).toEqual(['agent_worker.', 'lifecycle.', '_LIST_PRICES'])
    expect(segs('claude-haiku-4-5')).toEqual(['ops@', 'example.com'])
  })
})
