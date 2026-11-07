// VOICE IT › SETTINGS: the four cards over swarm-api's settings routes
// (routes/voice_settings.py), on the live path (USE_FIXTURES off) against a
// stubbed `fetch`.
//
//   * the cards draw the catalogue and the settings as served: every choice
//     is a catalogue name, so a model the catalogue lists appears and one it
//     does not list never does;
//   * a save sends the version the page read, and a 409 draws the conflict
//     line with who saved first;
//   * the OpenAI key shows its status only;
//   * nothing is wider than a 390 phone.
//
// BREAK IT: hard-code a model list in Settings.tsx, send `version + 1` or no
// version, drop the conflict band, draw `openai_key` as served, or put
// `white-space: nowrap` back on a tick.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { randomBytes } from 'node:crypto'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SHEET from '../styles/voice-settings.css?raw'

const CATALOGUE = {
  engines: [
    { name: 'own', label: 'Own speech', uses_machine: true },
    { name: 'gpt-live', label: 'GPT-Live', uses_machine: false },
  ],
  machines: [
    { name: 'g2-standard-4', vcpu: 4, memory_gb: 16, gpu: 'nvidia-l4', gpus: 1, usd_per_hour: 0.71, price_read_at: '2026-11-05', price_source: 'figure' },
    { name: 'g2-standard-8', vcpu: 8, memory_gb: 32, gpu: 'nvidia-l4', gpus: 1, usd_per_hour: 0.85, price_read_at: '2026-11-05', price_source: 'list price' },
  ],
  stt_models: [{ name: 'qwen3-asr-0.6b', hf: 'Qwen/Qwen3-ASR-0.6B', revision: 'r', files: [], disk_gb: 2 }],
  tts_models: [{ name: 'kokoro-82m', hf: 'hexgrad/Kokoro-82M', revision: 'r', files: [], voices: ['af_heart', 'bf_emma'], default_voice: 'af_heart' }],
  conversation_models: [
    { name: 'claude-haiku-5-5', label: 'Claude Haiku 5.5 (fastest)', model: 'claude-haiku-5-5', provider: 'anthropic' },
    { name: 'claude-sonnet-5', label: 'Claude Sonnet 5', model: 'claude-sonnet-5', provider: 'anthropic' },
  ],
  planner_models: [
    { name: 'fable', label: 'Fable class (planning)', model: 'claude-fable-5-1' },
    { name: 'opus', label: 'Opus class (coding)', model: 'claude-opus-5-5' },
  ],
  idle: [
    { name: '2m', seconds: 120 },
    { name: '5m', seconds: 300 },
  ],
  zones: [{ name: 'us-central1-a', machine: true }],
  defaults: {},
  read_at: '2026-11-05',
}

/** A key-shaped value, built at run time: it must never reach the page. */
const SECRET = `sk-${randomBytes(12).toString('hex')}`

function settings(version: number, over: Record<string, unknown> = {}) {
  return {
    settings: {
      engine: 'own', machine: 'g2-standard-4', stt_model: 'qwen3-asr-0.6b', tts_model: 'kokoro-82m', tts_voice: 'af_heart',
      conversation_model: 'claude-haiku-5-5', planner_model: 'fable', idle: '5m', zone: 'us-central1-a',
    },
    version,
    updated_by: 'owner@example.com',
    updated_at: '2026-11-04T22:10:00Z',
    applied: { machine: 'g2-standard-8', stt_model: 'qwen3-asr-0.6b', tts_model: 'kokoro-82m', idle: '5m', zone: 'us-central1-a', image: 'img', at: '2026-11-05T06:12:00Z' },
    pending: ['machine'],
    openai_key: { set: true, by: 'owner@example.com', at: '2026-11-03T09:00:00Z', label: 'voice-openai', link: { open: false, expires_at: null }, key: SECRET, value: SECRET },
    zones: ['us-central1-a'],
    ...over,
  }
}

interface Call {
  method: string
  url: string
  body: unknown
}

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

/** A `fetch` answering the three routes; `patch` decides each write's answer. */
function serve(read: () => unknown, patch: (body: Record<string, unknown>) => Response = () => json(200, read())): Call[] {
  const calls: Call[] = []
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = init?.method ?? 'GET'
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : null
    calls.push({ method, url, body })
    if (url.includes('/v1/voice/settings/catalogue')) return json(200, CATALOGUE)
    if (url.includes('/v1/voice/settings') && method === 'PATCH') return patch(body as Record<string, unknown>)
    if (url.includes('/v1/voice/settings')) return json(200, read())
    return json(404, { code: 'not_found', message: 'no such route' })
  }) as unknown as typeof fetch
  return calls
}

/** The live path, not the development fixtures: USE_FIXTURES is decided at import. */
async function page(canEdit = true) {
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { VoiceSettings } = await import('../voice/Settings')
  const view = render(<VoiceSettings canEdit={canEdit} />)
  await screen.findByRole('heading', { name: 'Engine' })
  return view
}

const card = (id: string): HTMLElement => document.querySelector<HTMLElement>(`[data-card="${id}"]`)!

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('Voice it › Settings draws the catalogue and the settings', () => {
  it('draws the four cards, every choice from the catalogue', async () => {
    serve(() => settings(7))
    await page()
    expect(['engine', 'machine', 'models', 'behaviour'].map((id) => card(id) !== null)).toEqual([true, true, true, true])

    const engine = within(card('engine'))
    expect(engine.getByRole('radio', { name: 'Own speech' }).getAttribute('aria-checked')).toBe('true')
    expect(engine.getByRole('radio', { name: 'GPT-Live' }).getAttribute('aria-checked')).toBe('false')
    const fast = engine.getByRole('combobox', { name: 'Fast model' }) as HTMLSelectElement
    expect([...fast.options].map((o) => o.value)).toEqual(['claude-haiku-5-5', 'claude-sonnet-5'])
    expect(fast.value).toBe('claude-haiku-5-5')
    const planner = engine.getByRole('combobox', { name: 'Planner model' }) as HTMLSelectElement
    expect([...planner.options].map((o) => o.value)).toEqual(['fable', 'opus'])
    expect(planner.selectedOptions[0]!.textContent).toBe('Fable class (planning) · claude-fable-5-1')
    // No model the catalogue does not list, the frames' older Haiku included.
    expect(document.body.textContent).not.toMatch(/haiku-4|Haiku 4/)

    const machine = within(card('machine'))
    expect(machine.getAllByRole('radio').map((r) => r.textContent)).toEqual([
      expect.stringContaining('g2-standard-4'),
      expect.stringContaining('g2-standard-8'),
    ])
    expect(machine.getByText('us-central1-a')).toBeTruthy()
    expect(within(card('behaviour')).getByRole('radio', { name: '5 min' }).getAttribute('aria-checked')).toBe('true')
    expect(within(card('models')).getByRole('radio', { name: 'bf_emma' })).toBeTruthy()
  })

  it('ticks a pending machine field "Next start" and an applied one "Applied"', async () => {
    serve(() => settings(7))
    await page()
    const machine = within(card('machine'))
    expect(machine.getByText('Next start').closest('.vset-tick')!.getAttribute('title')).toBe('Applied now: g2-standard-8')
    expect(within(card('behaviour')).getByText('Applied')).toBeTruthy()
    expect(within(card('models')).queryByText('Next start')).toBeNull()
  })

  it('is read only without the edit right: values, no controls', async () => {
    serve(() => settings(7))
    await page(false)
    expect(screen.getByText('Read only')).toBeTruthy()
    expect(screen.queryAllByRole('radio')).toHaveLength(0)
    expect(screen.queryAllByRole('combobox')).toHaveLength(0)
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    expect(within(card('engine')).getByText('Claude Haiku 5.5 (fastest)')).toBeTruthy()
  })
})

describe('saving', () => {
  it('sends the version the page read with the changed names only', async () => {
    const calls = serve(() => settings(7))
    await page()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
    fireEvent.click(within(card('machine')).getByRole('radio', { name: /g2-standard-8/ }))
    fireEvent.change(within(card('engine')).getByRole('combobox', { name: 'Fast model' }), { target: { value: 'claude-sonnet-5' } })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    const writes = calls.filter((c) => c.method === 'PATCH')
    expect(writes).toHaveLength(1)
    expect(writes[0]!.body).toEqual({ version: 7, machine: 'g2-standard-8', conversation_model: 'claude-sonnet-5' })
  })

  it('shows who saved first on a 409, and saves over it with the version read again', async () => {
    let version = 7
    const calls = serve(
      () => settings(version, { updated_by: 'alex@example.com', updated_at: '2026-11-06T06:41:00Z' }),
      (body) => {
        if (body.version !== version) return json(409, { code: 'conflict', message: 'the settings changed since they were read; read them again', detail: { field: 'version', version } })
        version += 1
        return json(200, settings(version))
      },
    )
    await page()
    version = 8 // someone else saved after the page read version 7
    fireEvent.click(within(card('behaviour')).getByRole('radio', { name: '2 min' }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    const band = within(card('behaviour')).getByRole('alert')
    expect(band.textContent).toContain('Saved first by alex@example.com')
    expect(band.textContent).toContain('v8')
    // The other cards carry no band: the change was the idle stop's.
    expect(within(card('machine')).queryByRole('alert')).toBeNull()

    await act(async () => {
      fireEvent.click(within(band).getByRole('button', { name: 'Save mine over it' }))
    })
    expect(calls.filter((c) => c.method === 'PATCH').map((c) => c.body)).toEqual([
      { version: 7, idle: '2m' },
      { version: 8, idle: '2m' },
    ])
    expect(within(card('behaviour')).queryByRole('alert')).toBeNull()
  })

  it('names the refused field on its card on a 422', async () => {
    serve(
      () => settings(7),
      () => json(422, { code: 'validation_failed', message: 'machine takes a name from the catalogue, never free text', detail: { field: 'machine' } }),
    )
    await page()
    fireEvent.click(within(card('machine')).getByRole('radio', { name: /g2-standard-8/ }))
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    })
    expect(within(card('machine')).getByRole('alert').textContent).toContain('Refused')
  })
})

describe('the OpenAI key', () => {
  it('shows whether it is set, by whom and when, and nothing else of the answer', async () => {
    serve(() => settings(7))
    await page()
    const key = document.querySelector<HTMLElement>('[data-key-status]')!
    expect(key?.getAttribute('data-key-status')).toBe('set')
    expect(key.textContent).toContain('Set · owner@example.com')
    expect(key.querySelector('input, textarea')).toBeNull()
    expect(document.body.innerHTML).not.toContain(SECRET)
  })

  it('says "Not set" when the pool holds no key', async () => {
    serve(() => settings(7, { openai_key: { set: false, by: null, at: null, label: null, link: { open: false, expires_at: null } } }))
    await page()
    expect(document.querySelector('[data-key-status]')!.getAttribute('data-key-status')).toBe('unset')
    expect(screen.getByText('Not set')).toBeTruthy()
  })
})

/** The rules inside `@media (max-width: 720px)`, braces balanced. */
function phoneBlock(): string {
  const at = SHEET.indexOf('@media (max-width: 720px)')
  expect(at, 'the sheet has no phone block').toBeGreaterThanOrEqual(0)
  const open = SHEET.indexOf('{', at)
  let depth = 0
  for (let i = open; i < SHEET.length; i += 1) {
    if (SHEET[i] === '{') depth += 1
    if (SHEET[i] === '}') depth -= 1
    if (depth === 0) return SHEET.slice(open + 1, i)
  }
  throw new Error('the phone block never closes')
}

/** Each rule's declarations, keyed by selector, outside any at-rule. */
function rules(css: string): [string, string][] {
  const out: [string, string][] = []
  for (const m of css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}@]+)\{([^{}]*)\}/g)) out.push([m[1]!.trim(), m[2]!])
  return out
}

const px = (decl: string): number => [...decl.matchAll(/(\d+(?:\.\d+)?)px/g)].reduce((n, m) => n + Number(m[1]), 0)

describe('nothing is wider than a 390 phone', () => {
  it('stacks the cards and each row under 720px', () => {
    const phone = rules(phoneBlock())
    expect(phone.find(([s]) => s === '.vset-grid')?.[1]).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\);/)
    expect(phone.find(([s]) => s === '.vset-row')?.[1]).toMatch(/grid-template-columns:\s*minmax\(0, 1fr\);/)
  })

  it('lets every control shrink, and no tick refuses to wrap', () => {
    const all = rules(SHEET)
    const decl = (sel: string) => all.find(([s]) => s === sel)?.[1] ?? ''
    for (const sel of ['.vset', '.vset-card', '.vset-select', '.vset-tick', '.vset-seg']) {
      expect(decl(sel), `${sel} may not shrink`).toMatch(/max-width:\s*100%|min-width:\s*0/)
    }
    expect(decl('.vset-select')).toMatch(/max-width:\s*100%/)
    expect(decl('.vset-tick')).not.toMatch(/nowrap/)
    expect(decl('.vset-mach')).toMatch(/box-sizing:\s*border-box/)
  })

  it('fixes no track or width a phone card cannot hold', () => {
    // A 390 screen less the shell's gutters and the card's own border and
    // padding leaves well over 300px for a card's content; no fixed width or
    // grid's fixed tracks in this sheet may claim more.
    for (const [sel, body] of rules(SHEET)) {
      for (const d of body.split(';')) {
        const [prop] = d.split(':')
        if (!/^\s*(width|min-width|flex-basis|grid-template-columns)\s*$/.test(prop ?? '')) continue
        expect(px(d), `${sel} { ${d.trim()} }`).toBeLessThanOrEqual(300)
      }
    }
  })

  it('draws no inline width in pixels', async () => {
    serve(() => settings(7))
    await page()
    for (const el of document.querySelectorAll<HTMLElement>('.vset [style]')) {
      expect(el.style.width, el.className).not.toMatch(/px/)
    }
  })
})
