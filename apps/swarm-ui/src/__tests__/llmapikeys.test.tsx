// CAPACITY › LLM PROVIDERS › API KEYS, AS CARDS (lane LP-U3;
// docs/design/llm-providers.md §4 and §6.3; the owner's ask of 11 November
// 2026: the cards mock-up, and type that is not "super small").
//
// WHAT EACH CASE HOLDS:
//   * the cards group by vendor and draw each key's name, state, live holders
//     over max agents, this month's spend over its cap and its lends, off the
//     key list api.ts's fixture path serves (`loadApiKeys` is the real one);
//   * every control (Pause, Drain, Resume, Delete, Add key, Send a link)
//     calls its api.ts function once, with the right arguments; Delete only
//     after the label is typed back (§4.4);
//   * a control that fails shows its error on the card and leaves the card as
//     it was, with no re-read;
//   * no keys is a short line plus Add key;
//   * no text on the page is below the body step, the key's name is at the
//     lead step, read off the shipped sheets with `cascade` (cssgate.ts);
//   * the prose guard: no sentence on the surface, and the page's words at
//     or under the count measured when the cards landed.
//
// Placeholders only; the key typed into the add form is built at runtime.

import { randomBytes } from 'node:crypto'

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ACCOUNTS_RANKED_CSS from '../styles/accounts-ranked.css?raw'
import COMPONENTS_CSS from '../styles/components.css?raw'
import LP_CARDS_CSS from '../styles/lp-cards.css?raw'
import { cascade, flatRules } from './cssgate'

const OK = { status: 'ok', fetchedAt: 0, data: { ok: true } } as const
const FAIL = {
  status: 'error',
  error: { kind: 'validation', httpStatus: 422, code: null, message: 'the broker refused the state change' },
} as const

// The read stays api.ts's own (its fixture path in this build); the writes are
// spies, so the fixture's keys never move and each call is counted.
const spies = vi.hoisted(() => ({
  setAccountState: vi.fn(),
  removeAccount: vi.fn(),
  addApiKey: vi.fn(),
  mintSetupLink: vi.fn(),
  loadApiKeys: vi.fn(),
  loadMe: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  spies.loadApiKeys.mockImplementation(actual.loadApiKeys)
  return { ...actual, ...spies, USE_FIXTURES: true }
})

const realLoad = (await vi.importActual<typeof import('../api')>('../api')).loadApiKeys
const { LlmProvidersScreen } = await import('../LlmProviders')

const WAIT = { timeout: 5000 } as const
const OWNER = 'owner@example.test'

beforeEach(() => {
  spies.loadApiKeys.mockImplementation(realLoad)
  for (const k of ['setAccountState', 'removeAccount', 'addApiKey'] as const) spies[k].mockResolvedValue(OK)
  spies.loadMe.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { principal: { email: OWNER, is_admin: true } } })
  spies.mintSetupLink.mockResolvedValue({
    status: 'ok',
    fetchedAt: 0,
    data: { kind: 'api-key', label: 'holder-key', owner: 'holder@example.test', url: 'https://console.example.test/setup/link#one', path: '/setup/link', expires_at: null },
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function draw(): Promise<HTMLElement> {
  render(<LlmProvidersScreen sub="api-keys" />)
  await screen.findByRole('region', { name: 'Anthropic' }, WAIT)
  return document.querySelector('.lpc-page') as HTMLElement
}

function card(name: string): HTMLElement {
  return screen.getByRole('article', { name: `API key ${name}` })
}

describe('API Keys: the cards', () => {
  it('groups the keys by vendor, Anthropic then OpenAI', async () => {
    await draw()
    // MUTATION: drop groupKeys' vendor order and OpenAI comes first.
    expect([...document.querySelectorAll('.lpc-group')].map((g) => g.getAttribute('aria-label'))).toEqual(['Anthropic', 'OpenAI'])
    const anthropic = screen.getByRole('region', { name: 'Anthropic' })
    expect(within(anthropic).getAllByRole('article').map((a) => a.getAttribute('aria-label'))).toEqual([
      'API key night-key',
      'API key spare-key',
      'API key Team key',
    ])
    expect(within(screen.getByRole('region', { name: 'OpenAI' })).getAllByRole('article').map((a) => a.getAttribute('aria-label'))).toEqual(['API key codex-key'])
  })

  it("draws a key's name, state, holders over max agents, spend over its cap and its lends", async () => {
    await draw()
    const team = card('Team key')
    expect(within(team).getByRole('heading', { level: 3 }).textContent).toBe('Team key')
    expect(team.querySelector('.lpc-mono')?.textContent).toBe('team-key')
    expect(team.querySelector('.lpc-state')?.textContent).toBe('Available')
    expect(team.querySelector('.lpc-figs')?.textContent).toContain('$212.50')
    expect(team.querySelector('.lpc-figs')?.textContent).toContain('of $400.00')
    expect(within(team).getByText('Agents').parentElement?.querySelector('.lpc-big')?.textContent).toBe('3/8')
    const lend = within(team).getByRole('list', { name: 'Lends of Team key' })
    expect([...lend.querySelectorAll('li')].map((li) => [...li.children].map((c) => c.textContent))).toEqual([['smoke', '≤ 2 agents', '$18.25 of $50.00']])
    // A key has no five-hour or seven-day window: none is drawn.
    expect(team.textContent).not.toMatch(/5-hour|7-day|five-hour|seven-day/i)
    expect(card('night-key').querySelector('.lpc-state')?.textContent).toBe('Paused')
    expect(card('spare-key').querySelector('.lpc-state')?.textContent).toBe('Out of credit')
    // No cap is said as such, never a bar of an invented cap.
    expect(card('codex-key').querySelector('.lpc-figs')?.textContent).toContain('no cap')
    expect(card('codex-key').querySelector('.lpc-tag')?.textContent).toBe('Voice it')
  })

  it("says a spend the broker did not serve as unknown, never $0", async () => {
    spies.loadApiKeys.mockImplementation(async () => {
      const r = await realLoad()
      if (r.status !== 'ok') return r
      return { ...r, data: { ...r.data, keys: r.data.keys.map((k) => ({ ...k, month_spend_usd: undefined })) } }
    })
    await draw()
    expect(card('Team key').querySelector('.lpc-big')?.textContent).toBe('unknown')
  })
})

describe('API Keys: every control calls its api.ts function once', () => {
  it('Pause asks for a reason, then calls setAccountState', async () => {
    await draw()
    fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Pause' }))
    fireEvent.change(within(card('Team key')).getByRole('textbox', { name: 'Reason' }), { target: { value: 'rotating soon' } })
    await act(async () => fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Pause key' })))
    expect(spies.setAccountState).toHaveBeenCalledTimes(1)
    expect(spies.setAccountState).toHaveBeenCalledWith('u-owner:team-key', 'PAUSED', 'rotating soon')
  })

  it('Drain asks for a reason, then calls setAccountState', async () => {
    await draw()
    fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Drain' }))
    fireEvent.change(within(card('Team key')).getByRole('textbox', { name: 'Reason' }), { target: { value: 'moving off' } })
    await act(async () => fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Drain key' })))
    expect(spies.setAccountState).toHaveBeenCalledTimes(1)
    expect(spies.setAccountState).toHaveBeenCalledWith('u-owner:team-key', 'DRAINING', 'moving off')
  })

  it('Resume on a paused key and on one out of credit calls setAccountState with AVAILABLE', async () => {
    await draw()
    await act(async () => fireEvent.click(within(card('night-key')).getByRole('button', { name: 'Resume' })))
    expect(spies.setAccountState).toHaveBeenCalledTimes(1)
    expect(spies.setAccountState).toHaveBeenLastCalledWith('u-owner:night-key', 'AVAILABLE', '')
    await act(async () => fireEvent.click(within(card('spare-key')).getByRole('button', { name: 'Resume' })))
    expect(spies.setAccountState).toHaveBeenCalledTimes(2)
    expect(spies.setAccountState).toHaveBeenLastCalledWith('u-owner:spare-key', 'AVAILABLE', '')
  })

  it('Delete is the typed confirmation: the label typed back, then removeAccount once', async () => {
    await draw()
    fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete API key Team key' })
    expect(within(dialog).getByRole('link', { name: 'Anthropic' }).getAttribute('target')).toBe('_blank')
    const go = within(dialog).getByRole('button', { name: 'Delete key' }) as HTMLButtonElement
    // MUTATION: drop the typed match and the key is one press from gone.
    expect(go.disabled).toBe(true)
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type team-key to delete' }), { target: { value: 'team-ke' } })
    expect(go.disabled).toBe(true)
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type team-key to delete' }), { target: { value: 'team-key' } })
    expect(go.disabled).toBe(false)
    await act(async () => fireEvent.click(go))
    expect(spies.removeAccount).toHaveBeenCalledTimes(1)
    expect(spies.removeAccount).toHaveBeenCalledWith('u-owner:team-key')
  })

  it("Add key sends the vendor, the label, the reader's email and the key to addApiKey once", async () => {
    await draw()
    fireEvent.click(screen.getByRole('button', { name: 'Add key' }))
    const form = screen.getByRole('form', { name: 'Add key' })
    fireEvent.change(within(form).getByRole('textbox', { name: 'Label' }), { target: { value: 'build-key' } })
    const value = `placeholder-${randomBytes(8).toString('hex')}`
    fireEvent.change(within(form).getByLabelText('API key'), { target: { value } })
    const submit = within(form).getByRole('button', { name: 'Add key' }) as HTMLButtonElement
    await waitFor(() => expect(submit.disabled).toBe(false))
    await act(async () => fireEvent.click(submit))
    expect(spies.addApiKey).toHaveBeenCalledTimes(1)
    expect(spies.addApiKey).toHaveBeenCalledWith({ provider: 'anthropic-api', label: 'build-key', owner: OWNER, value })
    expect(spies.mintSetupLink).not.toHaveBeenCalled()
  })

  it("Send a link mints an api-key link for the holder's email, once, and shows it", async () => {
    await draw()
    fireEvent.click(screen.getByRole('button', { name: 'Add key' }))
    const form = screen.getByRole('form', { name: 'Add key' })
    fireEvent.click(within(form).getByRole('button', { name: 'OpenAI' }))
    fireEvent.change(within(form).getByRole('textbox', { name: 'Label' }), { target: { value: 'holder-key' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Send a link' }))
    fireEvent.change(within(form).getByRole('textbox', { name: "Holder's email" }), { target: { value: 'holder@example.test' } })
    await act(async () => fireEvent.click(within(form).getByRole('button', { name: 'Make link' })))
    expect(spies.mintSetupLink).toHaveBeenCalledTimes(1)
    expect(spies.mintSetupLink).toHaveBeenCalledWith({ kind: 'api-key', label: 'holder-key', owner: 'holder@example.test', provider: 'openai-api' })
    expect((within(form).getByRole('textbox', { name: 'One-time link' }) as HTMLInputElement).value).toBe('https://console.example.test/setup/link#one')
    expect(spies.addApiKey).not.toHaveBeenCalled()
  })
})

describe('API Keys: a control that fails', () => {
  it('shows its error on the card and leaves the card as it was', async () => {
    await draw()
    const reads = spies.loadApiKeys.mock.calls.length
    spies.setAccountState.mockResolvedValue(FAIL)
    fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Pause' }))
    fireEvent.change(within(card('Team key')).getByRole('textbox', { name: 'Reason' }), { target: { value: 'rotating soon' } })
    await act(async () => fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Pause key' })))
    expect(within(card('Team key')).getByRole('alert').textContent).toBe('the broker refused the state change')
    // MUTATION: re-read or patch the state on a failure and this reads Paused.
    expect(card('Team key').querySelector('.lpc-state')?.textContent).toBe('Available')
    expect(spies.loadApiKeys.mock.calls.length).toBe(reads)
    // The other cards say nothing.
    expect(within(card('night-key')).queryByRole('alert')).toBeNull()
  })

  it('a failed delete says why and keeps the key', async () => {
    await draw()
    spies.removeAccount.mockResolvedValue({ ...FAIL, error: { ...FAIL.error, message: 'secret not yet destroyed' } })
    fireEvent.click(within(card('spare-key')).getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog', { name: 'Delete API key spare-key' })
    fireEvent.change(within(dialog).getByRole('textbox', { name: 'Type spare-key to delete' }), { target: { value: 'spare-key' } })
    await act(async () => fireEvent.click(within(dialog).getByRole('button', { name: 'Delete key' })))
    expect(within(card('spare-key')).getByRole('alert').textContent).toBe('secret not yet destroyed')
    expect(card('spare-key').querySelector('.lpc-state')?.textContent).toBe('Out of credit')
  })
})

describe('API Keys: no keys', () => {
  it('is a short line plus Add key', async () => {
    spies.loadApiKeys.mockResolvedValue({ status: 'ok', fetchedAt: 0, data: { keys: [], tenant: 'u-owner', readAt: 0 } })
    render(<LlmProvidersScreen sub="api-keys" />)
    const empty = await screen.findByRole('region', { name: 'No API keys' }, WAIT)
    const h = within(empty).getByRole('heading', { level: 2 })
    // The line is the heading's own text; its (i) carries the rest.
    expect([...h.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join('').trim()).toBe('No API keys')
    expect(visibleWords(empty).join(' ')).toBe('No API keys i Add key')
    const add = within(empty).getByRole('button', { name: 'Add key' }) as HTMLButtonElement
    expect(add.disabled).toBe(false)
    fireEvent.click(add)
    expect(screen.getByRole('form', { name: 'Add key' })).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// Readable type: no text below the body step
// ---------------------------------------------------------------------------

const SHEETS = `${COMPONENTS_CSS}\n${ACCOUNTS_RANKED_CSS}\n${LP_CARDS_CSS}`
const AT_LEAST_BODY = new Set(['body', 'lead', 'title', 'figure'])

/** The type step an element's text is drawn at: its own winning size, or the nearest ancestor's (inheritance). */
function stepOf(el: Element, width: number): string | null {
  for (let e: Element | null = el; e !== null; e = e.parentElement) {
    const w = cascade(SHEETS, e, ['font-size', 'font'], { width }).winner
    if (w !== null) return /var\(--t-([a-z]+)\)/.exec(w.value)?.[1] ?? `raw: ${w.value}`
  }
  return null
}

function textElements(root: HTMLElement): Element[] {
  return [root, ...root.querySelectorAll('*')].filter(
    (el) => el.closest('[hidden]') === null && [...el.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim() !== ''),
  )
}

describe('API Keys: readable type', () => {
  it.each([1440, 390])('draws no text below the body step at %ipx, panels open', async (width) => {
    const page = await draw()
    fireEvent.click(screen.getByRole('button', { name: 'Add key' }))
    fireEvent.click(within(card('Team key')).getByRole('button', { name: 'Delete' }))
    fireEvent.click(within(card('night-key')).getByRole('button', { name: 'Resume' }))
    fireEvent.click(within(card('codex-key')).getByRole('button', { name: 'Pause' }))
    const small = textElements(page)
      .map((el) => ({ el, step: stepOf(el, width) }))
      .filter(({ step }) => step === null || !AT_LEAST_BODY.has(step))
      .map(({ el, step }) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${el.textContent?.trim().slice(0, 30)}" at ${step ?? 'no size'}`)
    // MUTATION: drop `.lpc-page .c-btn` and every button reads at --t-meta.
    expect(small, small.join('\n')).toEqual([])
    expect(textElements(page).length).toBeGreaterThan(40)
  })

  it("draws a key's name at the lead step and its spend at the title step", async () => {
    await draw()
    const team = card('Team key')
    expect(stepOf(team.querySelector('h3')!, 1440)).toBe('lead')
    expect(stepOf(team.querySelector('.lpc-big')!, 1440)).toBe('title')
  })
})

describe('API Keys: light, dark and the phone', () => {
  it('names only console tokens for colour, so light and dark both follow', () => {
    // MUTATION: give the vendor heading a literal #d97757 and this fails.
    const literal = flatRules(LP_CARDS_CSS).flatMap((r) =>
      [...r.body.matchAll(/#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla)\(/g)].map((m) => `${r.selector}: ${m[0]}`),
    )
    expect(literal).toEqual([])
  })

  it('at 390 wide the cards are one column and their figures stack', async () => {
    const page = await draw()
    const grid = page.querySelector('.lpc-grid')!
    expect(cascade(SHEETS, grid, 'grid-template-columns', { width: 390 }).winner?.value).toMatch(/minmax\(min\(100%/)
    const figs = page.querySelector('.lpc-figs')!
    expect(cascade(SHEETS, figs, 'grid-template-columns', { width: 390 }).winner?.value).toBe('minmax(0, 1fr)')
    expect(cascade(SHEETS, figs, 'grid-template-columns', { width: 1440 }).winner?.value).toBe('minmax(0, 2fr) minmax(0, 1fr)')
  })
})

// ---------------------------------------------------------------------------
// The prose guard
// ---------------------------------------------------------------------------

/** The words a reader can see on the page: text nodes outside hidden nodes and tooltips. */
function visibleWords(root: HTMLElement): string[] {
  const words: string[] = []
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  for (let n = walk.nextNode(); n !== null; n = walk.nextNode()) {
    const p = n.parentElement
    if (p === null || p.closest('[hidden], [role="tooltip"], style, script') !== null) continue
    words.push(...(n.textContent ?? '').split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)))
  }
  return words
}

/**
 * The page's word count on the fixture when the cards landed (11 November
 * 2026): four key cards, their figures, one lend and the controls. A ceiling,
 * not a target; it may only go down.
 */
const WORD_CEILING = 72

describe('API Keys: the prose guard', () => {
  it('shows no sentence on the surface: every run of text is a label or a figure', async () => {
    const page = await draw()
    const long = [...page.querySelectorAll('*')]
      .filter((el) => el.closest('[hidden], [role="tooltip"]') === null)
      .flatMap((el) => [...el.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => (n.textContent ?? '').trim()))
      .filter((t) => t.split(/\s+/).length > 4)
    expect(long).toEqual([])
  })

  it(`keeps the page at or under ${WORD_CEILING} words`, async () => {
    const page = await draw()
    const words = visibleWords(page)
    expect(words.length, words.join(' ')).toBeLessThanOrEqual(WORD_CEILING)
  })
})
