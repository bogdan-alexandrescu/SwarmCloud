// ADMIN › ACCOUNTS: ADD AN OPENAI API KEY FOR VOICE IT (the owner, 4 November
// 2026: a Live session runs on an OpenAI developer API key, never a ChatGPT
// plan; 6 November 2026: the key goes in here, in ONE step, never through a
// link page that talks about Claude).
//
// What this file pins:
//   * the field is a platform admin's: a reader who is not one sees none;
//   * Add mints `POST /v1/admin/setup/links` with kind `voice-api-key`, the
//     label (VOICE_KEY_LABEL unless edited behind the pencil) and the admin
//     THEMSELF as the owner, then at once redeems that link's token with the
//     typed key through SetupLink.tsx's `redeemSetupLink`, and never navigates;
//   * after Add the key is in no input: a tick row names the label, and a
//     refusal shows `linkRefusal`'s line;
//   * an `openai-api` account's windows read "not measured", never 0 and
//     never "no reading yet", and its provider reads "OpenAI API key";
//   * the pool section and the field render in both themes, at 1440 and 390,
//     before and after Add.
//
// BREAK IT: drop the `is_admin` check -- a member sees the field; mint with
// another owner -- the body check fails; keep the key after a refusal -- the
// field still holds it; drop `notMeasured` -- the window reads "no reading yet".

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ApiError, Result } from '../fetch'
import type { Account } from '../types'
import type { PoolAccount } from '../accountsPool'

const api = vi.hoisted(() => ({
  loadMe: vi.fn(),
  mintSetupLink: vi.fn(),
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountModelClasses: vi.fn(),
  setAccountName: vi.fn(),
  setAccountState: vi.fn(),
  setAccountMaxAgents: vi.fn(),
  loadAccountHealth: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => api)

const link = vi.hoisted(() => ({ redeemSetupLink: vi.fn() }))
vi.mock('../SetupLink', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../SetupLink')>()),
  redeemSetupLink: link.redeemSetupLink,
}))

const { VoiceKeyLink, VoiceItPool, VOICE_KEY_BUTTON, VOICE_KEY_FIELD, VOICE_KEY_LABEL } = await import('../Accounts')
const { linkRefusal } = await import('../SetupLink')
const { ageOf, whyOf } = await import('../AccountsTable')

const WAIT = { timeout: 5000 } as const
const ADMIN = 'owner@example.com'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

const KEY_ACCOUNT = {
  account_id: 'eng:openai-key',
  owner_tenant: 'eng',
  label: 'openai-key',
  provider: 'openai-api',
  state: 'AVAILABLE',
  reason: '',
  lend_to: [],
  assigned: 0,
  observed_at: null,
  stale: false,
  unreadable_by: [],
  unreadable_now: [],
  last_assigned_at: null,
  windows: {},
  measured: false,
  max_agents: 1,
  can_take_new_agent: true,
  blocked_by: [],
  next_eligible_at: null,
  pools: ['voice-it'],
  voice_it_people: [ADMIN],
  voice_it_holder: null,
} as unknown as PoolAccount

function setWidth(width: number): void {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width })
  vi.stubGlobal('matchMedia', (q: string) => ({
    matches: q === '(max-width: 560px)' ? width <= 560 : false,
    media: q,
    addEventListener() {},
    removeEventListener() {},
  }))
}

beforeEach(() => {
  setWidth(1440)
  api.loadMe.mockReset()
  api.loadMe.mockResolvedValue(ok({ principal: { email: ADMIN, is_admin: true } }))
  api.mintSetupLink.mockReset()
  link.redeemSetupLink.mockReset()
  history.replaceState(null, '', '/capacity/accounts')
})

/** A fresh random hex string, so no credential-shaped literal is in this file. */
function hex(n: number): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => b.toString(16).padStart(2, '0')).join('')
}

function minted(label: string, token: string) {
  return ok({ kind: 'voice-api-key', label, owner: ADMIN, url: null, path: `/admin/setup/link/${token}`, expires_at: new Date(Date.now() + 600_000).toISOString() })
}

/** Types `key` into the field and presses Add. */
async function add(key: string): Promise<HTMLInputElement> {
  const field = (await screen.findByLabelText(VOICE_KEY_FIELD, {}, WAIT)) as HTMLInputElement
  fireEvent.change(field, { target: { value: key } })
  fireEvent.click(screen.getByRole('button', { name: 'Add' }))
  return field
}

/** No input on the page holds `key`, and nothing stored or in the address does. */
function keyGone(key: string): void {
  for (const input of Array.from(document.querySelectorAll('input'))) expect(input.value).not.toContain(key)
  expect(window.location.href).not.toContain(key)
  expect(JSON.stringify({ ...localStorage })).not.toContain(key)
  expect(JSON.stringify({ ...sessionStorage })).not.toContain(key)
  expect(document.body.textContent).not.toContain(key)
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.documentElement.removeAttribute('data-theme')
})

describe('Add an OpenAI API key for Voice it', () => {
  it('is not offered to a reader who is not a platform admin', async () => {
    api.loadMe.mockResolvedValue(ok({ principal: { email: 'member@example.com', is_admin: false } }))
    render(<VoiceKeyLink />)
    await waitFor(() => expect(api.loadMe).toHaveBeenCalled())
    await new Promise((r) => setTimeout(r, 0))
    expect(screen.queryByLabelText(VOICE_KEY_FIELD)).toBeNull()
    expect(screen.queryByRole('region', { name: VOICE_KEY_BUTTON })).toBeNull()
  })

  it('takes the key in one masked field, with no link hop', async () => {
    render(<VoiceKeyLink />)
    const field = (await screen.findByLabelText(VOICE_KEY_FIELD, {}, WAIT)) as HTMLInputElement
    expect(field.type).toBe('password')
    expect(field.getAttribute('autocomplete')).toBe('off')
    expect(field.getAttribute('spellcheck')).toBe('false')
    // The label is not a visible form field: it sits behind the pencil.
    expect(screen.queryByLabelText('Account name in the Voice it pool')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Make the link' })).toBeNull()
    expect(screen.queryByRole('link')).toBeNull()
    // The explanation is the (i)'s tooltip, never a visible sentence.
    expect(screen.getByRole('button', { name: /^About / })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('mints a voice-api-key link for the admin themself and redeems it at once with the typed key', async () => {
    const token = hex(24)
    const key = hex(20)
    api.mintSetupLink.mockResolvedValue(minted(VOICE_KEY_LABEL, token))
    link.redeemSetupLink.mockResolvedValue(ok({ kind: 'voice-api-key', label: VOICE_KEY_LABEL, account_id: `eng:${VOICE_KEY_LABEL}` }))
    const before = window.location.href
    const open = vi.spyOn(window, 'open')
    render(<VoiceKeyLink />)
    await add(key)
    const tick = await screen.findByRole('status', {}, WAIT)
    expect(api.mintSetupLink).toHaveBeenCalledTimes(1)
    expect(api.mintSetupLink).toHaveBeenCalledWith({ kind: 'voice-api-key', label: VOICE_KEY_LABEL, owner: ADMIN })
    expect(link.redeemSetupLink).toHaveBeenCalledTimes(1)
    expect(link.redeemSetupLink).toHaveBeenCalledWith(token, key)
    expect(api.mintSetupLink.mock.invocationCallOrder[0] ?? Infinity).toBeLessThan(link.redeemSetupLink.mock.invocationCallOrder[0] ?? -Infinity)
    expect(window.location.href).toBe(before)
    expect(open).not.toHaveBeenCalled()
    expect(tick.textContent).toContain(VOICE_KEY_LABEL)
    expect(tick.textContent).toContain('Voice it pool')
    expect(within(tick).getByRole('button', { name: /^About / }).getAttribute('aria-describedby')).toBeTruthy()
    expect(tick.textContent).toContain('never shown again')
    keyGone(key)
    open.mockRestore()
  })

  it('sends a label edited behind the pencil', async () => {
    const token = hex(24)
    const key = hex(20)
    api.mintSetupLink.mockResolvedValue(minted('voice-1', token))
    link.redeemSetupLink.mockResolvedValue(ok({ kind: 'voice-api-key', label: 'voice-1', account_id: 'eng:voice-1' }))
    render(<VoiceKeyLink />)
    fireEvent.click(await screen.findByRole('button', { name: /^Change the account name/ }, WAIT))
    const name = screen.getByLabelText('Account name in the Voice it pool') as HTMLInputElement
    expect(name.value).toBe(VOICE_KEY_LABEL)
    fireEvent.change(name, { target: { value: 'voice-1' } })
    await add(key)
    const tick = await screen.findByRole('status', {}, WAIT)
    expect(api.mintSetupLink).toHaveBeenCalledWith({ kind: 'voice-api-key', label: 'voice-1', owner: ADMIN })
    expect(link.redeemSetupLink).toHaveBeenCalledWith(token, key)
    expect(tick.textContent).toContain('voice-1')
    keyGone(key)
  })

  it('shows a refused redeem as its one line and keeps no key', async () => {
    const token = hex(24)
    const key = hex(20)
    const refusal = { httpStatus: 404, code: 'not_found', message: 'no such link' } as unknown as ApiError
    api.mintSetupLink.mockResolvedValue(minted(VOICE_KEY_LABEL, token))
    link.redeemSetupLink.mockResolvedValue({ status: 'error', error: refusal, fetchedAt: Date.now() })
    render(<VoiceKeyLink />)
    const field = await add(key)
    const alert = await screen.findByRole('alert', {}, WAIT)
    expect(link.redeemSetupLink).toHaveBeenCalledWith(token, key)
    expect(alert.textContent).toBe(linkRefusal(refusal).line)
    expect(screen.queryByRole('status')).toBeNull()
    expect(field.value).toBe('')
    keyGone(key)
  })

  it('refuses a label the pool would refuse before asking', async () => {
    render(<VoiceKeyLink />)
    fireEvent.click(await screen.findByRole('button', { name: /^Change the account name/ }, WAIT))
    fireEvent.change(screen.getByLabelText('Account name in the Voice it pool'), { target: { value: '-bad' } })
    fireEvent.change(screen.getByLabelText(VOICE_KEY_FIELD), { target: { value: hex(20) } })
    expect((screen.getByRole('button', { name: 'Add' }) as HTMLButtonElement).disabled).toBe(true)
    expect(api.mintSetupLink).not.toHaveBeenCalled()
  })
})

describe('an OpenAI API key account in the Voice it pool', () => {
  it('reads its windows as not measured, never 0, and names its provider', () => {
    render(<VoiceItPool accounts={[KEY_ACCOUNT] as Account[]} now={Date.now()} phone={false} />)
    const section = screen.getByRole('region', { name: 'Voice it pool' })
    const cells = section.querySelectorAll('[data-measured="false"]')
    expect(cells.length).toBe(2)
    for (const cell of Array.from(cells)) {
      expect(cell.textContent).toContain('not measured')
      expect(cell.textContent).not.toMatch(/\b0%|no reading yet/)
    }
    expect(section.textContent).toContain('OpenAI API key')
  })

  it('says not measured in its reading age and its verdict, never no reading yet', () => {
    expect(ageOf(KEY_ACCOUNT, Date.now())).toBe('not measured')
    const { container } = render(<>{whyOf(KEY_ACCOUNT, 'eng', Date.now())}</>)
    expect(container.textContent).toContain('Not measured: OpenAI gives an API key no usage window')
    expect(container.textContent).not.toContain('it takes one agent until it is')
  })

  for (const theme of ['light', 'dark'] as const) {
    for (const width of [1440, 390] as const) {
      it(`renders before and after Add under the ${theme} theme at ${width} wide`, async () => {
        setWidth(width)
        document.documentElement.setAttribute('data-theme', theme)
        const key = hex(20)
        api.mintSetupLink.mockResolvedValue(minted(VOICE_KEY_LABEL, hex(24)))
        link.redeemSetupLink.mockResolvedValue(ok({ kind: 'voice-api-key', label: VOICE_KEY_LABEL, account_id: `eng:${VOICE_KEY_LABEL}` }))
        render(
          <>
            <VoiceItPool accounts={[KEY_ACCOUNT] as Account[]} now={Date.now()} phone={width <= 560} />
            <VoiceKeyLink />
          </>,
        )
        const section = screen.getByRole('region', { name: 'Voice it pool' })
        expect(within(section).getAllByText(/not measured/).length).toBe(2)
        if (width <= 560) expect(section.querySelectorAll('article.acct-card').length).toBe(1)
        else expect(within(section).getByRole('table')).toBeTruthy()
        const block = await screen.findByRole('region', { name: VOICE_KEY_BUTTON }, WAIT)
        expect(within(block).getByLabelText(VOICE_KEY_FIELD)).toBeTruthy()
        await add(key)
        expect((await within(block).findByRole('status', {}, WAIT)).textContent).toContain(VOICE_KEY_LABEL)
        keyGone(key)
      })
    }
  }
})
