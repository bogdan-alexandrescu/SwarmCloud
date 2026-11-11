// VOICE IT › PREFERENCES: WHICH CHATGPT PLAN THE NEXT SESSION USES (the owner,
// 2026-11-02: ChatGPT plan accounts are reserved for Voice it in a separate
// Voice it pool).
//
//   * one line from `GET /v1/voice/pool`: the account's name (or label), or
//     why there is none, with a link to LLM Providers (Plans);
//   * no ChatGPT sign-in: no button, no field, and no call to the removed
//     `/v1/voice/chatgpt*` routes, read or write;
//   * a refusal is said in swarm-api's words.
//
// MUTATIONS: print the label when a name is served; draw the account line for
// `account: null`; read `/v1/voice/chatgpt`; put a "Sign in with ChatGPT"
// button back.

import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
import { CAPACITY_ACCOUNTS_HREF, VOICE_POOL, VOICE_PREFERENCES, VoicePreferences } from '../voice/Preferences'

function ok(data: unknown) {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function servePool(data: unknown): void {
  fetchMock.read.mockImplementation(async (target: { url: string }) => {
    expect(target.url).toBe(VOICE_POOL)
    return ok(data)
  })
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 5; i += 1) await Promise.resolve()
  })
}

function readUrls(): string[] {
  return fetchMock.read.mock.calls.map(([t]) => (t as { url: string }).url)
}

function noSignIn(): void {
  expect(screen.queryByRole('button', { name: /sign in/i })).toBeNull()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(document.body.textContent ?? '').not.toMatch(/sign in with chatgpt|sign-in/i)
  for (const url of readUrls()) expect(url).not.toContain('/v1/voice/chatgpt')
  expect(fetchMock.write).not.toHaveBeenCalled()
}

describe('Voice it › Preferences, the Voice it pool line', () => {
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
    apiMock.loadMe.mockReset()
  })

  it("names the account the next session will use, by its name, and links to Capacity", async () => {
    servePool({ pool: 'voice-it', account: { label: 'plan-a', display_name: 'Team plan A', provider: 'openai' }, reason: '' })
    render(<VoicePreferences />)
    await settle()

    expect(screen.getByText("Your next session will use the Voice it pool's ChatGPT plan: Team plan A.")).toBeTruthy()
    expect(screen.getByRole('link', { name: /LLM Providers/ }).getAttribute('href')).toBe(CAPACITY_ACCOUNTS_HREF)
    expect(readUrls()).toEqual([VOICE_POOL])
    noSignIn()
  })

  it('falls back to the label when the account has no name', async () => {
    servePool({ pool: 'voice-it', account: { label: 'plan-a', display_name: null, provider: 'openai' }, reason: '' })
    render(<VoicePreferences />)
    await settle()
    expect(screen.getByText("Your next session will use the Voice it pool's ChatGPT plan: plan-a.")).toBeTruthy()
  })

  it('says when no Voice it pool account is free', async () => {
    servePool({ pool: 'voice-it', account: null, reason: 'no_voice_account_available' })
    render(<VoicePreferences />)
    await settle()
    expect(screen.getByText('No Voice it pool account is free right now.')).toBeTruthy()
    expect(screen.queryByText(/Your next session will use/)).toBeNull()
    noSignIn()
  })

  it('says when the person is not in the Voice it pool', async () => {
    servePool({ pool: 'voice-it', account: null, reason: 'not_in_voice_pool' })
    render(<VoicePreferences />)
    await settle()
    expect(screen.getByText('You are not in the Voice it pool.')).toBeTruthy()
    expect(screen.getByRole('link', { name: /LLM Providers/ })).toBeTruthy()
  })

  it("says a refusal in swarm-api's words", async () => {
    fetchMock.read.mockResolvedValue({ status: 'error', error: { kind: 'http', status: 503, message: 'the account pool is unavailable' } })
    render(<VoicePreferences />)
    await settle()
    expect(screen.getByText('Could not read the Voice it pool')).toBeTruthy()
    expect(screen.getByText('the account pool is unavailable')).toBeTruthy()
  })

  it('is what the Preferences screen draws, with no ChatGPT sign-in anywhere on it', async () => {
    apiMock.loadMe.mockResolvedValue(ok({ principal: { is_admin: true } }))
    fetchMock.read.mockImplementation(async (target: { url: string }) => {
      if (target.url === VOICE_POOL) return ok({ pool: 'voice-it', account: null, reason: 'no_voice_account_available' })
      if (target.url === VOICE_PREFERENCES) return { status: 'error', error: { kind: 'http', status: 503, message: 'down' } }
      throw new Error(`unexpected read ${target.url}`)
    })
    render(<VoicePreferencesScreen />)
    await settle()

    expect(screen.getByRole('heading', { name: 'ChatGPT plan' })).toBeTruthy()
    expect(screen.getByText('No Voice it pool account is free right now.')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'ChatGPT sign-in' })).toBeNull()
    noSignIn()
  })
})
