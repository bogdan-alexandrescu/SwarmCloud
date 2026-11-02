// VOICE IT › TALK MOUNTS THE TALK PANE (docs/design/voice-it.md §3.10, §10,
// phase one; the owner's pick of 2026-11-01, "a follow-up of this lane").
//
//   * for the person Voice it is open to, the page under its head is the Talk
//     pane: its start control and its captions region, and none of the section
//     lane's placeholder ("No session yet");
//   * for anyone else it is the Gate's refusal, and no Talk pane;
//   * swarm-voice is not deployed here yet, so a start is answered 503 by
//     swarm-api (swarm_api/voice/sessions.py `unavailable`). The pane shows the
//     server's sentence as served, releases the microphone and claims nothing
//     is listening. A 403 (`not_allowed`) reads the same way with its own.
//
// MUTATIONS: draw the placeholder in place of <TalkPane />; drop the Gate;
// prefix the server's sentence with the pane's own words; leave the mic track
// running after a refused start.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { classifyFailure } from '../fetch'
import type { Me } from '../types'
import { installMedia, settle, type FakeTrack } from './voice.fakes'

const api = vi.hoisted(() => ({ loadMe: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  ...api,
}))

const fetchMock = vi.hoisted(() => ({ write: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  write: fetchMock.write,
}))

const { VoiceTalkScreen } = await import('../voice/Section')

/** swarm-api's own sentences (swarm_api/voice/sessions.py `unavailable`, `not_allowed`). */
const UNAVAILABLE =
  'Voice it is unavailable: swarm-voice is not deployed here (SWARM_VOICE_URL is unset). Nothing was started.'
const NOT_ALLOWED =
  'Voice it is open only to the people this deployment names (VOICE_IT_PEOPLE, else the platform owner). Nothing was started.'

function me(admin: boolean): Me {
  return {
    tenant: {
      tenant_id: 'eng', kind: 'group', principal: 'eng@example.com', display_name: null,
      created_at: '2026-09-01T00:00:00Z', max_active: 10, capacity_units: 20, monthly_budget_usd: null,
      enabled: true, credentials: [], service_account: null, gcs_prefix: null, namespace: null,
    },
    principal: { email: 'a@example.com', domain: 'example.com', groups: [], is_admin: admin },
    environment: 'dev',
    environment_declared: true,
  }
}

function signedIn(admin: boolean): void {
  api.loadMe.mockResolvedValue({ status: 'ok', data: me(admin), fetchedAt: Date.now() })
}

/** swarm-api refusing the start, as `write` hands the refusal back after classifying it. */
function refuseStarts(status: number, code: string, message: string): void {
  fetchMock.write.mockImplementation(async (target: { url: string }) => {
    if (target.url === '/v1/voice/sessions') {
      return { status: 'error', error: { ...classifyFailure(status, { code, message }, undefined), method: 'POST' } }
    }
    return { status: 'ok', data: null, fetchedAt: Date.now() }
  })
}

describe('Voice it › Talk', () => {
  let mic: FakeTrack
  beforeEach(() => {
    ;({ mic } = installMedia({ audioContext: true }))
  })
  afterEach(() => {
    api.loadMe.mockReset()
    fetchMock.write.mockReset()
  })

  it('draws the Talk pane, its start control and its captions, for the person Voice it is open to', async () => {
    signedIn(true)
    render(<VoiceTalkScreen />)
    expect(await screen.findByRole('button', { name: 'Start talking' })).toBeTruthy()
    expect(screen.getByRole('heading', { level: 1, name: 'Talk' })).toBeTruthy()
    expect(screen.getByRole('log', { name: 'Captions' })).toBeTruthy()
    expect(screen.getByRole('status').textContent).toMatch(/Not connected/)
    expect(screen.queryByText(/No session yet/)).toBeNull()
    expect(screen.queryByText(/not switched on yet/i)).toBeNull()
    expect(screen.queryByText(/Voice it is not open to you/)).toBeNull()
  })

  it("draws the Gate's refusal, and no Talk pane, for anyone else", async () => {
    signedIn(false)
    render(<VoiceTalkScreen />)
    expect(await screen.findByText('Voice it is not open to you')).toBeTruthy()
    expect(screen.getByRole('heading', { level: 1, name: 'Talk' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Start talking' })).toBeNull()
    expect(screen.queryByRole('log', { name: 'Captions' })).toBeNull()
  })

  it("shows swarm-api's 503 sentence as served, and leaves no live track", async () => {
    signedIn(true)
    refuseStarts(503, 'voice_unavailable', UNAVAILABLE)
    render(<VoiceTalkScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Start talking' }))
    await settle(act)
    expect(screen.getByRole('alert').textContent).toBe(UNAVAILABLE)
    expect(mic.stop).toHaveBeenCalled()
    expect(mic.readyState).toBe('ended')
    expect(screen.getByRole('status').textContent).toMatch(/Not connected/)
    expect(screen.queryByText(/^Live/)).toBeNull()
    expect(screen.getByRole('button', { name: 'Start talking' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Mute' }).hasAttribute('disabled')).toBe(true)
  })

  it("shows swarm-api's 403 sentence as served, and leaves no live track", async () => {
    signedIn(true)
    refuseStarts(403, 'forbidden', NOT_ALLOWED)
    render(<VoiceTalkScreen />)
    fireEvent.click(await screen.findByRole('button', { name: 'Start talking' }))
    await settle(act)
    expect(screen.getByRole('alert').textContent).toBe(NOT_ALLOWED)
    expect(mic.readyState).toBe('ended')
    expect(screen.getByRole('status').textContent).toMatch(/Not connected/)
  })
})
