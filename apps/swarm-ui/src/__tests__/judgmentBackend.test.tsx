// SETTINGS › API KEYS › JEV BACKEND (lane JUDGE-SELECT, owner decisions
// 2026-10-14). Each workspace chooses where Jev's judgments go:
//
//   * Internal Jev -- the default, "stays inside Saga";
//   * Internal Laya -- also inside Saga;
//   * TypeSafe -- "sends page text to TypeSafe; needs a key".
//
// The radio reads GET /v1/judgment (the selected workspace) and writes
// PUT /v1/workspaces/{id}/judgment-backend. An option the platform cannot
// serve (no tailnet egress, no TypeSafe key) is disabled with the API's
// reason; a member who is neither the workspace's owner nor an admin sees
// the choice and cannot change it.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const LABELS: Record<string, string> = {
  'internal-jev': 'Jev (Internal)',
  'internal-laya': 'Jev (Internal Laya)',
  typesafe: 'Jev (TypeSafe)',
}

const NO_EGRESS = 'the tailnet egress to the decision API is not enabled on this platform'
const NO_KEY = 'no TypeSafe key resolves for you in this workspace'

function judgment(opts: { backend?: string; internal?: boolean; key?: boolean; canEdit?: boolean } = {}): Json {
  const backend = opts.backend ?? 'internal-jev'
  const internal = opts.internal ?? true
  const key = opts.key ?? false
  const option = (b: string) => {
    const available = b === 'typesafe' ? key : internal
    return { backend: b, label: LABELS[b], available, reason: available ? null : b === 'typesafe' ? NO_KEY : NO_EGRESS }
  }
  const options = ['internal-jev', 'internal-laya', 'typesafe'].map(option)
  const chosen = options.find((o) => o.backend === backend)!
  return {
    tenant_id: 'eng',
    judgment_backend: backend,
    can_edit: opts.canEdit ?? true,
    jev: { ...chosen, default: 'internal-jev', set_at: null, set_by: null, options },
  }
}

function api(start: Json, write?: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null) {
  let current = start
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/judgment')) return { status: 200, body: current }
    if (m === 'GET' && url.startsWith('/v1/tenants/me')) {
      return { status: 200, body: { tenant: { tenant_id: 'eng', kind: 'group' }, principal: { email: 'alice@example.com', is_admin: true } } }
    }
    if (m === 'GET' && url.startsWith('/v1/provider-keys/typesafe')) return { status: 404, body: { detail: 'Not Found' } }
    if (m !== 'GET' && write) return write(m, url, body)
    return null
  })
  return {
    calls,
    set: (j: Json) => {
      current = j
    },
  }
}

async function load() {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  return import('../JudgmentBackend')
}

const group = () => screen.findByRole('radiogroup', { name: /jev backend/i })
const radio = (name: RegExp) => screen.getByRole('radio', { name }) as HTMLInputElement

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('the Jev backend radio', () => {
  it('defaults to Internal Jev and says where each option sends page text', async () => {
    api(judgment())
    const { JevBackendCard } = await load()
    render(<JevBackendCard />)
    const g = await group()
    expect(radio(/internal jev/i).checked).toBe(true)
    expect(radio(/internal laya/i).checked).toBe(false)
    expect(radio(/typesafe/i).checked).toBe(false)
    const text = visible(g)
    expect(text).toMatch(/internal jev.*default/i)
    expect(text).toMatch(/stays inside saga/i)
    expect(text).toMatch(/sends page text to typesafe; needs a key/i)
  })

  it('disables an option the platform cannot serve, with the reason', async () => {
    api(judgment({ internal: false, key: false, backend: 'internal-jev' }))
    const { JevBackendCard } = await load()
    render(<JevBackendCard />)
    await group()
    for (const name of [/internal jev/i, /internal laya/i, /typesafe/i]) {
      expect(radio(name).disabled).toBe(true)
    }
    expect(visible(document.body)).toContain(NO_EGRESS)
    expect(visible(document.body)).toContain(NO_KEY)
    // The chosen backend cannot run: the card says so rather than pretending.
    expect(visible(document.body)).toMatch(/jev is unavailable/i)
  })

  it('enables TypeSafe once a key resolves', async () => {
    api(judgment({ key: true }))
    const { JevBackendCard } = await load()
    render(<JevBackendCard />)
    await group()
    expect(radio(/typesafe/i).disabled).toBe(false)
    expect(radio(/internal laya/i).disabled).toBe(false)
  })

  it('saves a choice with one PUT to the workspace and shows the new state', async () => {
    const seen: unknown[] = []
    const server = api(judgment(), (m, url, body) => {
      if (m === 'PUT' && url === '/v1/workspaces/eng/judgment-backend') {
        seen.push(body)
        server.set(judgment({ backend: 'internal-laya' }))
        return { status: 200, body: judgment({ backend: 'internal-laya' }) }
      }
      return null
    })
    const { JevBackendCard } = await load()
    render(<JevBackendCard />)
    await group()
    fireEvent.click(radio(/internal laya/i))
    await waitFor(() => expect(seen).toEqual([{ judgment_backend: 'internal-laya' }]))
    await waitFor(() => expect(radio(/internal laya/i).checked).toBe(true))
    expect(await screen.findByRole('status')).toBeTruthy()
    expect(visible(screen.getByRole('status'))).toMatch(/saved/i)
  })

  it('reverts and says why when the write is refused', async () => {
    api(judgment({ key: true }), (m) =>
      m === 'PUT' ? { status: 403, body: { code: 'FORBIDDEN', message: 'only the owner or an admin' } } : null,
    )
    const { JevBackendCard } = await load()
    render(<JevBackendCard />)
    await group()
    fireEvent.click(radio(/typesafe/i))
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/not changed/i)
    await waitFor(() => expect(radio(/internal jev/i).checked).toBe(true))
  })

  it('is read-only for a member who is neither the owner nor an admin', async () => {
    const { calls } = api(judgment({ canEdit: false, key: true }))
    const { JevBackendCard } = await load()
    render(<JevBackendCard />)
    const g = await group()
    for (const r of within(g).getAllByRole('radio')) expect((r as HTMLInputElement).disabled).toBe(true)
    expect(visible(document.body)).toMatch(/only the workspace.s owner or a platform admin/i)
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })
})

describe('on Settings › API keys', () => {
  it('is drawn above the TypeSafe key', async () => {
    api(judgment())
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ProviderKeysScreen } = await import('../ProviderKeys')
    render(<ProviderKeysScreen link={null} />)
    expect(await group()).toBeTruthy()
  })
})
