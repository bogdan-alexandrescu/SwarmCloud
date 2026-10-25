// SETTINGS › WORKSPACE › CODE INTELLIGENCE (lane CTXQ-UI;
// docs/design/context-quality.md §4.7, §5.4). One switch per workspace:
// whether Jev, the judgment model, is ever sent excerpts of this workspace's
// code. It is OFF by default, and the card says plainly what leaves the
// platform when it is on -- excerpts, after the path deny, the credential
// drop and redaction, to the workspace's judgment backend (read from
// GET /v1/judgment, so the card names where they go).
//
// The switch reads GET /v1/code-intelligence (the selected workspace) and
// writes PUT /v1/workspaces/{id}/code-intelligence, which merges the one
// field into workspace_settings/{tenant_id}. Only the workspace's owner or a
// platform admin (`can_edit`) sees it enabled; the API is the gate.
//
// Fixtures only; no network.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { COMPANY_NAME } from '../../test/companyName'

type Json = Record<string, unknown>

function codeIntel(opts: { jev?: boolean; canEdit?: boolean; served?: boolean } = {}): Json {
  return {
    tenant_id: 'eng',
    can_edit: opts.canEdit ?? true,
    // `served: false` is an answer that carries no `jev` at all: the switch
    // must read OFF, since an absent field is a workspace that never turned it on.
    ...(opts.served === false ? {} : { jev: opts.jev ?? false }),
    context: 'inherit',
    context_ab: 'inherit',
    defaults: { jev: false, context: 'on', context_ab: 'ab' },
    updated_at: null,
    updated_by: null,
    repositories: [],
  }
}

function judgment(backend = 'internal-jev'): Json {
  const labels: Record<string, string> = { 'internal-jev': 'Jev (Internal)', 'internal-laya': 'Jev (Internal Laya)', typesafe: 'Jev (TypeSafe)' }
  const options = Object.keys(labels).map((b) => ({ backend: b, label: labels[b], available: true, reason: null }))
  return {
    tenant_id: 'eng',
    can_edit: true,
    jev: { ...options.find((o) => o.backend === backend)!, default: 'internal-jev', set_at: null, set_by: null, options },
  }
}

function api(start: Json, opts: { backend?: string; write?: (m: string, url: string, body: unknown) => { status: number; body: unknown } | null } = {}) {
  let current = start
  const calls = serve((m, url, body) => {
    if (m === 'GET' && url.startsWith('/v1/code-intelligence')) return { status: 200, body: current }
    if (m === 'GET' && url.startsWith('/v1/judgment')) return { status: 200, body: judgment(opts.backend) }
    if (m !== 'GET' && opts.write) return opts.write(m, url, body)
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
  return import('../CodeIntelligence')
}

const toggle = async () => (await screen.findByRole('switch', { name: /send code to jev/i })) as HTMLInputElement

afterEach(() => {
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('the Code intelligence switch', () => {
  it('is off by default when the workspace has never set it', async () => {
    api(codeIntel({ served: false }))
    const { CodeIntelligenceCard } = await load()
    render(<CodeIntelligenceCard />)
    const t = await toggle()
    expect(t.checked).toBe(false)
    expect(visible(document.body)).toMatch(/off \(default\)/i)
  })

  it('says what leaves the platform when it is on, and where it goes', async () => {
    api(codeIntel())
    const { CodeIntelligenceCard } = await load()
    render(<CodeIntelligenceCard />)
    await toggle()
    const text = visible(document.body)
    // What is sent, and what never is (§5.4).
    expect(text).toMatch(/excerpts of this workspace.s code/i)
    expect(text).toMatch(/never sent/i)
    expect(text).toMatch(/\.env/)
    expect(text).toMatch(/credential/i)
    expect(text).toMatch(/transcript/i)
    // Where it goes: the workspace's judgment backend, by name.
    await waitFor(() => expect(visible(document.body)).toMatch(/Internal Jev.*stays inside your network/i))
    // The company is not named on the card (#1647).
    expect(document.body.textContent).not.toMatch(COMPANY_NAME)
    // Local context is a separate switch and sends nothing.
    expect(text).toMatch(/repository context.*nothing leaves the platform/i)
  })

  it('names TypeSafe as leaving your network when that is the judgment backend', async () => {
    api(codeIntel(), { backend: 'typesafe' })
    const { CodeIntelligenceCard } = await load()
    render(<CodeIntelligenceCard />)
    await toggle()
    await waitFor(() => expect(visible(document.body)).toMatch(/TypeSafe.*outside your network/i))
    expect(document.body.textContent).not.toMatch(COMPANY_NAME)
  })

  it('is enabled for the owner or an admin, and turning it on is one PUT to the workspace', async () => {
    const seen: unknown[] = []
    const server = api(codeIntel(), {
      write: (m, url, body) => {
        if (m === 'PUT' && url === '/v1/workspaces/eng/code-intelligence') {
          seen.push(body)
          server.set(codeIntel({ jev: true }))
          return { status: 200, body: codeIntel({ jev: true }) }
        }
        return null
      },
    })
    const { CodeIntelligenceCard } = await load()
    render(<CodeIntelligenceCard />)
    const t = await toggle()
    expect(t.disabled).toBe(false)
    fireEvent.click(t)
    await waitFor(() => expect(seen).toEqual([{ jev: true }]))
    await waitFor(async () => expect((await toggle()).checked).toBe(true))
    expect(visible(await screen.findByRole('status'))).toMatch(/saved/i)
  })

  it('reverts and says why when the write is refused', async () => {
    api(codeIntel(), { write: (m) => (m === 'PUT' ? { status: 403, body: { code: 'FORBIDDEN', message: 'only the owner or an admin' } } : null) })
    const { CodeIntelligenceCard } = await load()
    render(<CodeIntelligenceCard />)
    fireEvent.click(await toggle())
    const alert = await screen.findByRole('alert')
    expect(visible(alert)).toMatch(/not changed/i)
    await waitFor(async () => expect((await toggle()).checked).toBe(false))
  })

  it('is read-only for a member who is neither the owner nor an admin', async () => {
    const { calls } = api(codeIntel({ canEdit: false, jev: true }))
    const { CodeIntelligenceCard } = await load()
    render(<CodeIntelligenceCard />)
    const t = await toggle()
    expect(t.disabled).toBe(true)
    expect(t.checked).toBe(true)
    expect(visible(document.body)).toMatch(/only the workspace.s owner or a platform admin/i)
    fireEvent.click(t)
    expect(calls.filter((c) => c.method !== 'GET')).toEqual([])
  })
})

describe('Config › Workspace Settings', () => {
  it('draws the Code intelligence card', async () => {
    api(codeIntel())
    await load()
    // The page and its parts are WorkspaceSettings.tsx's since WS-SETTINGS-PAGE.
    const { WorkspaceSettingsScreen } = await import('../WorkspaceSettings')
    render(<WorkspaceSettingsScreen />)
    expect(await toggle()).toBeTruthy()
    // Config › Workspace Settings since CONFIG-NAV (part of #2157).
    expect(visible(document.querySelector('h1'))).toBe('Workspace Settings')
  })
})
