// HIDE IS THE PERSON'S, NOT THE BROWSER'S (#780; docs/onboarding.md §3.1,
// §3.2 `POST /v1/onboarding/dismiss`).
//
// WHAT EACH CASE HOLDS:
//   * Hide on the Overview card POSTs /v1/onboarding/dismiss once and hides
//     the card; nothing is written to localStorage;
//   * a fresh load whose GET /v1/onboarding says `dismissed: true` draws no
//     card -- in any browser, since the flag is the server's;
//   * a refused dismiss keeps the card and says so;
//   * Work › Setup still shows every step of a dismissed checklist, and its
//     Show on Overview posts {dismissed: false}.
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve } from './repofixture'

function doc(over: Record<string, unknown> = {}) {
  const steps = [
    { step: 'signed_in', state: 'done' },
    { step: 'github_connected', state: 'todo' },
    { step: 'app_installed', state: 'todo', evidence: { waiting_for: 'github_connected' } },
    { step: 'orgs_enabled', state: 'todo', evidence: { waiting_for: 'github_connected' } },
    { step: 'repos_chosen', state: 'todo', evidence: { waiting_for: 'github_connected' } },
    { step: 'access_verified', state: 'todo', evidence: { waiting_for: 'github_connected' } },
    { step: 'ready', state: 'todo', evidence: { waiting_for: 'github_connected' } },
  ].map((s) => ({ code: null, copy: null, checked_at: null, evidence: {}, issues: [], ...s }))
  return {
    tenant_id: 'eng',
    user: 'dev@swarm.example.com',
    user_hash: '89abcdef01234567',
    derived_at: new Date().toISOString(),
    next_step: 'github_connected',
    complete: false,
    dismissed: false,
    dismissed_at: null,
    source: 'derived on this read',
    steps,
    ...over,
  }
}

const DISMISSED = { tenant_id: 'eng', user_hash: '89abcdef01234567', dismissed: true, dismissed_at: new Date().toISOString() }

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../Onboarding')
}

afterEach(() => {
  vi.unstubAllEnvs()
  window.localStorage.clear()
})

describe('Hide on the Overview card', () => {
  it('POSTs /v1/onboarding/dismiss once, hides the card, and keeps nothing in this browser', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: doc() }
      if (m === 'POST' && url === '/v1/onboarding/dismiss') return { status: 200, body: DISMISSED }
      return null
    })
    const { SetupCard } = await load()
    render(<SetupCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Hide' }))
    await waitFor(() => expect(screen.queryByText('Set up SwarmCloud')).toBeNull())
    expect(calls.filter((c) => c.method === 'POST')).toEqual([expect.objectContaining({ url: '/v1/onboarding/dismiss', body: { dismissed: true } })])
    expect(window.localStorage.length).toBe(0)
  })

  it('draws no card on a fresh load that says dismissed', async () => {
    const calls = serve((m, url) => (m === 'GET' && url === '/v1/onboarding' ? { status: 200, body: doc({ dismissed: true, dismissed_at: new Date().toISOString() }) } : null))
    const { SetupCard } = await load()
    const { container } = render(<SetupCard />)
    await waitFor(() => expect(calls.length).toBeGreaterThan(0))
    await new Promise((r) => setTimeout(r, 20))
    expect(container.innerHTML).toBe('')
  })

  it('keeps the card and says so when the dismiss is refused', async () => {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: doc() }
      if (m === 'POST' && url === '/v1/onboarding/dismiss') return { status: 503, body: { code: 'unavailable', message: 'Firestore did not answer' } }
      return null
    })
    const { SetupCard } = await load()
    render(<SetupCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Hide' }))
    expect(await screen.findByText(/could not be hidden/)).toBeTruthy()
    expect(screen.getByText('Set up SwarmCloud')).toBeTruthy()
  })
})

describe('Work › Setup and a dismissed checklist', () => {
  it('still shows every step, and Show on Overview posts dismissed: false', async () => {
    const calls = serve((m, url) => {
      if (m === 'GET' && url === '/v1/onboarding') return { status: 200, body: doc({ dismissed: true, dismissed_at: new Date().toISOString() }) }
      if (m === 'POST' && url === '/v1/onboarding/dismiss') return { status: 200, body: { ...DISMISSED, dismissed: false, dismissed_at: null } }
      return null
    })
    const { OnboardingScreen } = await load()
    render(<OnboardingScreen />)
    await screen.findByRole('list', { name: 'Setup steps' })
    expect(document.querySelectorAll('li.ob-step')).toHaveLength(7)
    fireEvent.click(screen.getByRole('button', { name: 'Show on Overview' }))
    await waitFor(() =>
      expect(calls.filter((c) => c.method === 'POST')).toEqual([expect.objectContaining({ url: '/v1/onboarding/dismiss', body: { dismissed: false } })]),
    )
  })
})
