// VOICE IT › PREFERENCES: THE CHATGPT SIGN-IN (the owner's pick, 2026-11-02:
// "Sign-in button, then go live").
//
//   * not stored: "Not signed in" and "Sign in with ChatGPT";
//   * stored: "Signed in", since when, and "Sign in again" -- never a value;
//   * the control starts a sign-in through swarm-api, opens ChatGPT's page,
//     says where the sign-in ends, and sends the pasted address back with the
//     sign-in's state; the stored state follows, and the field is emptied;
//   * a refusal is said in swarm-api's words and nothing is drawn as stored.
//
// MUTATIONS: draw the stored line for `stored: false`; drop `state` from the
// exchange body; leave the pasted address in the field after saving; render
// a field of the status answer other than `stored` and `stored_at`.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

const apiMock = vi.hoisted(() => ({ loadMe: vi.fn() }))

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  loadMe: apiMock.loadMe,
}))

import { VoicePreferencesScreen } from '../voice/Section'
import { VOICE_CHATGPT, VOICE_CHATGPT_AUTHORIZE, VOICE_CHATGPT_EXCHANGE, VoicePreferences } from '../voice/Preferences'

const STATE = 'st-0001'
const AUTHORIZE = `https://auth.openai.com/oauth/authorize?state=${STATE}`
const INSTRUCTIONS =
  'Sign in to ChatGPT in the tab that opens. When it is done, that tab goes to an address starting ' +
  'http://localhost:1455/auth/callback and says the page cannot be reached: that is expected. Copy the ' +
  'whole address from the address bar and paste it here.'

function ok(data: unknown) {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function serveStatus(data: unknown): void {
  fetchMock.read.mockImplementation(async (target: { url: string }) => {
    expect(target.url).toBe(VOICE_CHATGPT)
    return ok(data)
  })
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve()
  })
}

describe('Voice it › Preferences, the ChatGPT sign-in', () => {
  let open: ReturnType<typeof vi.fn>
  beforeEach(() => {
    open = vi.fn()
    vi.stubGlobal('open', open)
  })
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
    apiMock.loadMe.mockReset()
    vi.unstubAllGlobals()
  })

  it('draws the not-stored state with "Sign in with ChatGPT"', async () => {
    serveStatus({ stored: false, stored_at: null })
    render(<VoicePreferences />)
    await settle()

    expect(screen.getByText('Not signed in')).toBeTruthy()
    expect(screen.queryByText('Signed in')).toBeNull()
    expect(screen.getByRole('button', { name: 'Sign in with ChatGPT' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Sign in again' })).toBeNull()
  })

  it('draws the stored state, since when, with "Sign in again" and no value', async () => {
    const leaked = `rt-${Math.random().toString(36).slice(2)}`
    serveStatus({ stored: true, stored_at: '2026-11-02T03:04:05+00:00', refresh_token: leaked })
    const { container } = render(<VoicePreferences />)
    await settle()

    expect(screen.getByText('Signed in')).toBeTruthy()
    expect(screen.getByText(/A ChatGPT sign-in is stored, since Nov 2/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign in again' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Sign in with ChatGPT' })).toBeNull()
    expect(container.innerHTML).not.toContain(leaked)
  })

  it('runs the sign-in: opens the page, says where it ends, sends the address with its state', async () => {
    serveStatus({ stored: false, stored_at: null })
    fetchMock.write.mockImplementation(async (target: { url: string }) => {
      if (target.url === VOICE_CHATGPT_AUTHORIZE) {
        return ok({ authorize_url: AUTHORIZE, state: STATE, redirect_uri: 'http://localhost:1455/auth/callback', expires_in_seconds: 900, instructions: INSTRUCTIONS })
      }
      if (target.url === VOICE_CHATGPT_EXCHANGE) return ok({ stored: true, stored_at: '2026-11-02T03:04:05+00:00' })
      throw new Error(`unexpected write ${target.url}`)
    })
    render(<VoicePreferences />)
    await settle()

    fireEvent.click(screen.getByRole('button', { name: 'Sign in with ChatGPT' }))
    await settle()

    expect(open).toHaveBeenCalledWith(AUTHORIZE, '_blank', 'noopener,noreferrer')
    expect(screen.getByText(INSTRUCTIONS)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Open the ChatGPT sign-in' }).getAttribute('href')).toBe(AUTHORIZE)

    const pasted = `http://localhost:1455/auth/callback?code=ac-${Math.random().toString(36).slice(2)}&state=${STATE}`
    const field = screen.getByLabelText('The address the sign-in tab ended on')
    fireEvent.change(field, { target: { value: pasted } })
    fireEvent.click(screen.getByRole('button', { name: 'Save sign-in' }))
    await settle()

    const exchange = fetchMock.write.mock.calls.find(([t]) => (t as { url: string }).url === VOICE_CHATGPT_EXCHANGE)
    expect(exchange?.[1]).toBe('POST')
    expect(exchange?.[2]).toEqual({ state: STATE, callback: pasted })
    expect(screen.getByText('Signed in')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign in again' })).toBeTruthy()
    expect(screen.queryByLabelText('The address the sign-in tab ended on')).toBeNull()
    expect(document.body.innerHTML).not.toContain(pasted)
  })

  it("says a refusal in swarm-api's words and draws nothing as stored", async () => {
    serveStatus({ stored: false, stored_at: null })
    fetchMock.write.mockImplementation(async (target: { url: string }) => {
      if (target.url === VOICE_CHATGPT_AUTHORIZE) return ok({ authorize_url: AUTHORIZE, state: STATE, instructions: INSTRUCTIONS })
      return { status: 'error', error: { kind: 'http', status: 422, message: 'this sign-in has expired or was already used; press Sign in with ChatGPT to start again' } }
    })
    render(<VoicePreferences />)
    await settle()
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with ChatGPT' }))
    await settle()
    fireEvent.change(screen.getByLabelText('The address the sign-in tab ended on'), { target: { value: 'code-1234#st-0001' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save sign-in' }))
    await settle()

    expect(screen.getByText('The ChatGPT sign-in did not finish')).toBeTruthy()
    expect(screen.getByText(/this sign-in has expired or was already used/)).toBeTruthy()
    expect(screen.queryByText('Signed in')).toBeNull()
    expect((screen.getByLabelText('The address the sign-in tab ended on') as HTMLInputElement).value).toBe('')
  })

  it('is what the Preferences screen draws for the person Voice it is open to', async () => {
    apiMock.loadMe.mockResolvedValue(ok({ principal: { is_admin: true } }))
    serveStatus({ stored: false, stored_at: null })
    render(<VoicePreferencesScreen />)
    await settle()

    expect(screen.getByRole('heading', { name: 'ChatGPT sign-in' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign in with ChatGPT' })).toBeTruthy()
  })
})
