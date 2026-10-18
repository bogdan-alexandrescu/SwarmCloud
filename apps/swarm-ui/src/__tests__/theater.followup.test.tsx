// LANE THEATER-FOLLOWUP: THE THEATER WORKS AT PHONE WIDTH, ITS CONTROLS ARE
// REAL AND STYLED, FOCUS STAYS INSIDE IT, A REPLAY SAYS IT IS A REPLAY, AND
// THE HAND-OFF CHECKLIST USES NEUTRAL WORDING (part of #1030).
//
// Measured in the live-browser QA pass p12 (owner, 2026-10-13 ~04:00 PDT, in
// gstack on task_4defa61c5eea4a5c87f5). One block per finding:
//
//   QA12-02 at 390 px the screen row collapsed to a 0x0 canvas.
//   QA12-03 the bottom bar's Take control ask must sit inside the viewport,
//           at 1440x900 and at 390x844.
//   QA12-08/24 the top bar's device chips were always disabled while the
//           right column's switched devices: ONE set, the working one.
//   QA12-10 aria-modal, but Tab walked out to the page's nav.
//   QA12-13 a replay said nothing on the stage, the strip and the pill still
//           read live, and the recordings list was read once.
//   QA12-15 `.ctl-btn` had no CSS at all.
//   QA12-16 opening by ⤢ or `T` did not put `?theater=1` in the address.
//   QA12-23 "check the strip" was never ticked, even with the strip green.
//   QA12-24 the device outline ignored rotate; OTHER LIVE rows had no id.
//   QA12-05 while you hold control, the top flag reads "You are in control".
//   QA12-21 one m:ss countdown; in control, the idle timer says so.
//   Owner decision "Neutral wording": steps that fit any hand-off, the
//   agent's own reason as the headline, password masking as a fact.
//
// MUTATIONS, one per block: drop the 55vh floor from the phone media block;
// drop the phone `.th-bottom .live-ask` rule; put the top chips back; drop the
// root's Tab handling; drop the replay tag, the pill's REPLAY or the
// recordings interval; drop the `.ctl-btn` rules; drop the URL sync; pass
// `stripOk={false}`; drop `is-landscape` or the short id; drop the in-control
// flag or the idle label; put "Sign in." back as a step.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { useEffect, useRef, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Task } from '../types'

vi.stubEnv('VITE_LIVE', '1')
const { TheaterButton, TheaterHost, RECORDINGS_POLL_MS } = await import('../Theater')
type TheaterSession = import('../Theater').TheaterSession
type TheaterHandoff = import('../Theater').TheaterHandoff
const { browserTabPath } = await import('../LiveThumb')

const TENANT = 'eng'
const TASK = 'task_8cf26eb6e1f94e85adf8'
const ATTEMPT = 'att_fafe0d6de50c4ff39de9'
const FIXTURES = join(__dirname, '..', '..', '..', '..', 'tests', 'fixtures', 'live')
const STYLES = join(__dirname, '..', 'styles')

function fixture<T>(file: string): T {
  return JSON.parse(readFileSync(join(FIXTURES, file), 'utf8')) as T
}

function css(file: string): string {
  return readFileSync(join(STYLES, file), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/\s+/g, ' ')
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })
}

interface Call {
  url: string
  method: string
}

function stubFetch(answer: (url: string, method: string, n: number) => Response): Call[] {
  const calls: Call[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ url, method })
      return answer(url, method, calls.filter((c) => c.url.split('?')[0] === url.split('?')[0]).length)
    }),
  )
  return calls
}

function row(id: string, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: TENANT,
    state: 'RUNNING',
    runner_profile: 'claude-code-browser',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 5,
    created_at: '2026-10-12T10:00:00Z',
    updated_at: '2026-10-12T10:01:00Z',
    started_at: '2026-10-12T10:00:30Z',
    completed_at: null,
    submitted_by: 'alice@saga.xyz',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    ...over,
  } as Task
}

const PAGE = {
  tasks: [row(TASK, { step_id: 'this-one' }), row('task_plain_browser_000001', { step_id: 'plain-browser' })],
  next_page_token: null,
}

const OPEN = fixture<{ session: LiveSession }>('session.open.json').session
const REPORTED = fixture<{ session: LiveSession }>('session.reported.json').session
const RECORDINGS = fixture<{ segments: Array<Record<string, unknown>> }>('recordings.json')

/** The listing with one more minute recorded, as the relay closes a segment every minute. */
const RECORDINGS_LATER = {
  ...RECORDINGS,
  segments: [
    ...RECORDINGS.segments,
    {
      ...RECORDINGS.segments[1],
      seq: 2,
      object: `tenants/${TENANT}/tasks/${TASK}/attempts/${ATTEMPT}/live/rec-0000000000002.live.webm`,
      started_at: '2026-10-11T23:43:00+00:00',
    },
  ],
}

function answer(url: string, method: string, n: number): Response {
  if (url.startsWith('/v1/tasks?')) return json(PAGE)
  if (url.includes('/live/actions')) return json({ attempt_id: 'att_x', actions: [] })
  if (url.startsWith(`/v1/tasks/${TASK}/live/recordings`)) return json(n > 1 ? RECORDINGS_LATER : RECORDINGS)
  if (url.startsWith(`/v1/tasks/${TASK}/live/control`) && method === 'DELETE') return json(OPEN)
  return json({ code: 'not_found', message: 'Not found.' }, 404)
}

/** The tab's device switcher, as far as the theater sees it: a group of chips it marks with outlines. */
function DeviceSlot() {
  return (
    <span className="live-chips" role="group" aria-label="Device">
      <button type="button" className="live-chip" aria-pressed="false">
        Desktop
      </button>
      <button type="button" className="live-chip" aria-pressed="true">
        iPhone 15
      </button>
    </span>
  )
}

function Tab(props: {
  browser?: LiveSession | null
  handoff?: TheaterHandoff | null
  initiallyOpen?: boolean
  strip?: boolean
}) {
  const screenRef = useRef<HTMLDivElement>(null)
  const [node, setNode] = useState<HTMLElement | null>(null)
  useEffect(() => setNode(screenRef.current), [])
  const session: TheaterSession = {
    taskId: TASK,
    tenantId: TENANT,
    attemptId: '',
    sessionId: OPEN.session_id,
    screen: node,
    live: true,
    browser: props.browser === undefined ? OPEN : props.browser,
  }
  return (
    <TheaterHost
      session={session}
      handoff={props.handoff ?? null}
      device={<DeviceSlot />}
      strip={props.strip === false ? undefined : <div data-testid="the-strip">https://github.com</div>}
      control={<button type="button">Take control</button>}
      initiallyOpen={props.initiallyOpen ?? true}
    >
      <nav>
        <a href="/agents">Agents</a>
      </nav>
      <TheaterButton />
      <div ref={screenRef} data-live-screen="" />
    </TheaterHost>
  )
}

async function flush(): Promise<void> {
  for (let i = 0; i < 4; i += 1) {
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
  }
}

const dialog = (): HTMLElement => screen.getByRole('dialog')
const head = (): HTMLElement => dialog().querySelector('header') as HTMLElement

beforeEach(() => {
  window.history.replaceState(null, '', '/')
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

/** The body of the first `@media (max-width: 640px)` block of a stylesheet. */
function phoneBlock(sheet: string): string {
  const at = sheet.indexOf('@media (max-width: 640px)')
  expect(at, 'theater.css has no phone block').toBeGreaterThan(-1)
  let depth = 0
  const open = sheet.indexOf('{', at)
  for (let i = open; i < sheet.length; i += 1) {
    if (sheet[i] === '{') depth += 1
    if (sheet[i] === '}') depth -= 1
    if (depth === 0) return sheet.slice(open + 1, i)
  }
  throw new Error('unclosed phone block')
}

function rule(sheet: string, selector: string): string | null {
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const m = new RegExp(`(?:^|[}\\s])${esc} \\{([^}]*)\\}`).exec(sheet)
  return m ? m[1] ?? null : null
}

function px(value: string | undefined, vw: number, vh: number): number {
  if (value === undefined) return NaN
  const v = value.trim()
  const min = /^min\((.+),(.+)\)$/.exec(v)
  const calc = /^calc\((\S+) - (\S+)\)$/.exec(v)
  if (calc) return px(calc[1], vw, vh) - px(calc[2], vw, vh)
  if (min) return Math.min(px(min[1], vw, vh), px(min[2], vw, vh))
  if (v.endsWith('vw')) return (parseFloat(v) * vw) / 100
  if (v.endsWith('vh')) return (parseFloat(v) * vh) / 100
  if (v.endsWith('px')) return parseFloat(v)
  if (v === '0') return 0
  return NaN
}

function decl(body: string | null, prop: string): string | undefined {
  if (body === null) return undefined
  const m = new RegExp(`(?:^|;)\\s*${prop}:\\s*([^;]+)`).exec(body)
  return m?.[1]?.trim()
}

describe('QA12-02: at phone width the screen has room', () => {
  it('the centre row has a floor of at least half the viewport, so the canvas is never 0x0', () => {
    const phone = phoneBlock(css('theater.css'))
    const rows = decl(rule(phone, '.th-root'), 'grid-template-rows')
    expect(rows, 'the phone block sets no rows').toBeDefined()
    const centre = /^auto (minmax\(([^,]+),[^)]+\)|\S+)/.exec(rows!)
    expect(centre, rows).not.toBeNull()
    const floor = centre![2] ?? centre![1]
    expect(px(floor, 390, 844)).toBeGreaterThanOrEqual(844 * 0.5)
    expect(px(decl(rule(phone, '.th-center'), 'min-height'), 390, 844)).toBeGreaterThanOrEqual(844 * 0.5)
  })
})

describe('QA12-03: the bottom bar’s Take control ask is inside the viewport', () => {
  const sheet = css('theater.css')
  const ask = css('live.css')

  it('at 1440x900: it opens upward from the bar, right-aligned to the control at the bar’s right end', () => {
    const [vw, vh] = [1440, 900]
    const desk = rule(sheet, '.th-bottom .live-ask')
    expect(decl(desk, 'top')).toBe('auto')
    expect(decl(desk, 'bottom')).toBe('100%')
    const width = px(decl(rule(ask, '.live-ask'), 'width'), vw, vh)
    expect(decl(rule(ask, '.live-ask'), 'right')).toBe('0')
    // The control sits after `.th-spacer`, at the bar's right padding (16px).
    const right = vw - 16
    expect(right - width).toBeGreaterThanOrEqual(0)
    expect(right).toBeLessThanOrEqual(vw)
    // Upward from a bar ~50px tall at the bottom: the ask's cap leaves it on screen.
    const cap = px(decl(desk, 'max-height'), vw, vh)
    expect(cap).toBeLessThanOrEqual(vh - 50)
    expect(decl(desk, 'overflow-y') ?? decl(desk, 'overflow')).toBe('auto')
  })

  it('at 390x844: pinned to the viewport, not to a control the bar may have wrapped to its left edge', () => {
    const [vw, vh] = [390, 844]
    const phone = rule(phoneBlock(sheet), '.th-bottom .live-ask')
    expect(phone, 'no phone rule for the bar’s ask').not.toBeNull()
    expect(decl(phone, 'position')).toBe('fixed')
    const left = px(decl(phone, 'left'), vw, vh)
    const right = px(decl(phone, 'right'), vw, vh)
    const bottom = px(decl(phone, 'bottom'), vw, vh)
    expect(left).toBeGreaterThanOrEqual(0)
    expect(right).toBeGreaterThanOrEqual(0)
    expect(bottom).toBeGreaterThanOrEqual(0)
    expect(decl(phone, 'top')).toBe('auto')
    expect(decl(phone, 'width')).toBe('auto')
    expect(vw - left - right).toBeGreaterThan(200)
    expect(px(decl(phone, 'max-height'), vw, vh) + bottom).toBeLessThanOrEqual(vh)
  })
})

describe('QA12-08 / QA12-24: one set of device chips, the working one', () => {
  it('the top bar draws no chips of its own; the DEVICE column holds the tab’s switcher', async () => {
    stubFetch(answer)
    render(<Tab browser={REPORTED} />)
    await flush()
    const groups = within(dialog()).getAllByRole('group', { name: 'Device' })
    expect(groups.length).toBe(1)
    expect(screen.getByRole('region', { name: 'DEVICE' }).contains(groups[0] ?? null)).toBe(true)
    expect(within(head()).queryByRole('button', { name: /rotate|Desktop|iPhone/ })).toBeNull()
    expect(dialog().querySelector('[title="Switching the device is not served yet."]')).toBeNull()
  })
})

describe('QA12-10: focus stays inside the theater, and goes back on close', () => {
  it('Tab from the last control wraps to the first, Shift+Tab from the first to the last', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    const root = dialog()
    const focusable = [...root.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled])')].filter(
      (el) => el.closest('[hidden]') === null,
    )
    const first = focusable[0]!
    const last = focusable[focusable.length - 1]!
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
    // From the root itself (where the theater puts focus on open), Shift+Tab stays in.
    root.focus()
    fireEvent.keyDown(root, { key: 'Tab', shiftKey: true })
    expect(root.contains(document.activeElement)).toBe(true)
  })

  it('focus that lands outside (a click behind, a script) is pulled back in', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    const nav = screen.getByRole('link', { name: 'Agents' })
    act(() => nav.focus())
    expect(dialog().contains(document.activeElement)).toBe(true)
  })

  it('Esc closes and focus returns to ⤢ Theater', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    fireEvent.keyDown(dialog(), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Open theater (T)' }))
  })
})

describe('QA12-13: a replay says it is a replay', () => {
  it('the stage reads "Replay · <time>", the pill reads REPLAY, the live strip is labelled as live; live ● undoes all three', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    expect(screen.queryByTestId('theater-replay-tag')).toBeNull()
    expect(screen.getByTestId('theater-live-pill').textContent).toMatch(/^LIVE/)

    fireEvent.click(screen.getByRole('button', { name: '◀◀ replay' }))
    await flush()
    const tag = screen.getByTestId('theater-replay-tag')
    expect(tag.textContent).toMatch(/^Replay · \S/)
    expect(tag.textContent).not.toContain('unknown')
    expect(screen.getByTestId('theater-live-pill').textContent).toMatch(/^REPLAY/)
    const strip = screen.getByTestId('the-strip').closest('.th-strip') as HTMLElement
    expect(strip.classList.contains('is-replay')).toBe(true)
    expect(strip.textContent).toMatch(/live now, not the replay/i)

    fireEvent.click(screen.getByRole('button', { name: 'live ●' }))
    expect(screen.queryByTestId('theater-replay-tag')).toBeNull()
    expect(screen.getByTestId('theater-live-pill').textContent).toMatch(/^LIVE/)
    expect(strip.classList.contains('is-replay')).toBe(false)
  })

  it('the recordings list is read again while the theater is open, without stopping a replay', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
    const calls = stubFetch(answer)
    render(<Tab />)
    await flush()
    const reads = () => calls.filter((c) => c.url.startsWith(`/v1/tasks/${TASK}/live/recordings`)).length
    expect(reads()).toBe(1)
    expect(screen.getByTestId('theater-scrubber').querySelectorAll('[data-segment]').length).toBe(2)
    fireEvent.click(screen.getByRole('button', { name: '◀◀ replay' }))
    await flush()

    act(() => {
      vi.advanceTimersByTime(RECORDINGS_POLL_MS)
    })
    await flush()
    expect(reads()).toBe(2)
    expect(screen.getByTestId('theater-scrubber').querySelectorAll('[data-segment]').length).toBe(3)
    expect(screen.getByTestId('theater-replay')).toBeTruthy()
  })
})

describe('QA12-15: the theater’s buttons are styled', () => {
  const sheet = css('theater.css')
  const styles = readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')

  it('.ctl-btn has a rule: border, ground, ink, height, and disabled, pressed and focus states', () => {
    const base = rule(sheet, '.ctl-btn')
    expect(base, '.ctl-btn has no CSS').not.toBeNull()
    for (const prop of ['border', 'background', 'color', 'height', 'border-radius', 'cursor']) {
      expect(decl(base, prop), prop).toBeDefined()
    }
    expect(sheet).toMatch(/\.ctl-btn:disabled[^{]*\{[^}]*not-allowed/)
    expect(sheet).toMatch(/\.ctl-btn\[aria-pressed='true'\][^{]*\{[^}]*background/)
    expect(sheet).toMatch(/\.ctl-btn:focus-visible[^{]*\{[^}]*outline/)
  })

  it('in both themes: every colour token the rules use is defined for dark (:root) and light', () => {
    const body = [...sheet.matchAll(/\.ctl-btn[^{]*\{([^}]*)\}/g)].map((m) => m[1] ?? '').join(';')
    const tokens = [...new Set([...body.matchAll(/var\((--[\w-]+)\)/g)].map((m) => m[1] ?? ''))].filter(
      (t) => !/^--(h|r|t|lh|font)(-|$)/.test(t),
    )
    expect(tokens.length).toBeGreaterThan(2)
    const light = [...styles.matchAll(/:root\[data-theme='light'\]\s*\{([^}]*)\}/g)].map((m) => m[1]).join('\n')
    for (const t of tokens) {
      expect(styles, `${t} is never defined`).toMatch(new RegExp(`${t}:`))
      expect(light, `${t} has no light value`).toMatch(new RegExp(`${t}:`))
    }
  })
})

describe('QA12-16: the address follows the theater', () => {
  it('⤢ and T put ?theater=1 on the Browser tab’s address; Esc takes it off; the rest of the query stays', async () => {
    stubFetch(answer)
    window.history.replaceState(null, '', `${browserTabPath(TASK)}?wf=wf_1`)
    render(<Tab initiallyOpen={false} />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Open theater (T)' }))
    await flush()
    let q = new URLSearchParams(window.location.search)
    expect(q.get('theater')).toBe('1')
    expect(q.get('wf')).toBe('wf_1')
    expect(window.location.pathname).toBe(browserTabPath(TASK))

    fireEvent.keyDown(dialog(), { key: 'Escape' })
    q = new URLSearchParams(window.location.search)
    expect(q.get('theater')).toBeNull()
    expect(q.get('wf')).toBe('wf_1')

    fireEvent.keyDown(document.body, { key: 't' })
    await flush()
    expect(screen.getByRole('dialog')).toBeTruthy()
    expect(new URLSearchParams(window.location.search).get('theater')).toBe('1')
  })

  it('a page that is not a Browser tab’s address is left alone', async () => {
    stubFetch(answer)
    window.history.replaceState(null, '', '/workflows/wf_1')
    render(<Tab />)
    await flush()
    expect(window.location.search).toBe('')
  })
})

describe('the hand-off checklist (QA12-23, QA12-05, QA12-21, neutral wording)', () => {
  const wait = (over: Partial<TheaterHandoff> = {}): TheaterHandoff => ({
    reason: 'Approve the deploy in the vendor console',
    expect: 'github.com',
    deadline: new Date(Date.now() + 272_000).toISOString(),
    ...over,
  })
  const checklist = (): HTMLElement => screen.getByRole('region', { name: 'Hand-off checklist' })
  const stripStep = (): HTMLElement =>
    [...checklist().querySelectorAll<HTMLElement>('li')].find((li) => /strip/i.test(li.textContent ?? '')) as HTMLElement

  it('QA12-23: "check the strip" is ticked when the strip’s verdict is the expected site', async () => {
    stubFetch(answer)
    render(<Tab browser={REPORTED} handoff={wait()} />)
    await flush()
    expect(stripStep().dataset.done).toBe('true')
  })

  it('QA12-23: and not ticked on another site, or with no address reported', async () => {
    stubFetch(answer)
    const other = { ...REPORTED, origin: { ...REPORTED.origin!, host: 'github.com.evil.test', registrable_domain: 'evil.test' } }
    const { unmount } = render(<Tab browser={other} handoff={wait()} />)
    await flush()
    expect(stripStep().dataset.done).toBe('false')
    unmount()
    render(<Tab browser={OPEN} handoff={wait()} />)
    await flush()
    expect(stripStep().dataset.done).toBe('false')
  })

  it('neutral wording: the agent’s reason is the headline, no step says sign in, masking is a fact', async () => {
    stubFetch(answer)
    render(<Tab handoff={wait()} />)
    await flush()
    const list = checklist()
    expect(list.querySelector('.th-handoff-reason')?.textContent).toBe('Approve the deploy in the vendor console')
    const steps = [...list.querySelectorAll('li')].map((li) => li.textContent ?? '')
    expect(steps.length).toBe(4)
    for (const s of steps) expect(s).not.toMatch(/sign[ -]?in|password/i)
    expect(steps[0]).toMatch(/asked for your help/i)
    expect(list.textContent).not.toMatch(/signed-in/i)
    const notes = [...list.querySelectorAll('p')].map((p) => p.textContent ?? '')
    expect(notes.some((n) => /passwords .*masked/i.test(n))).toBe(true)
  })

  it('QA12-05 / QA12-21: while you hold control the flag reads "You are in control" and the timer is the idle one', async () => {
    stubFetch(answer)
    const mine = { ...OPEN, controller: { email: OPEN.you.email, since: '2026-10-12T10:05:00Z' } }
    render(<Tab browser={mine} handoff={wait()} />)
    await flush()
    expect(head().textContent).toContain('You are in control')
    expect(head().textContent).not.toContain('Needs you')
    expect(checklist().querySelector('time')?.textContent).toMatch(/^returns to the agent after \d+:\d\d idle$/i)
  })

  it('QA12-21: not in control, the flag is "Needs you" and the countdown is m:ss left', async () => {
    stubFetch(answer)
    render(<Tab handoff={wait()} />)
    await flush()
    expect(head().textContent).toContain('Needs you')
    expect(checklist().querySelector('time')?.textContent).toMatch(/^\d+:\d\d left$/)
  })
})

describe('QA12-24: the outline follows rotate, and OTHER LIVE names the task', () => {
  it('a landscape device draws a landscape outline, in the DEVICE line and on the chosen chip', async () => {
    stubFetch(answer)
    const landscape = { ...REPORTED, device: { preset: 'iphone-15', orientation: 'landscape' as const, touch: true } }
    render(<Tab browser={landscape} />)
    await flush()
    const device = screen.getByRole('region', { name: 'DEVICE' })
    const outline = device.querySelector<HTMLElement>('.th-device-now [data-device-outline]')!
    expect(outline.dataset.orientation).toBe('landscape')
    expect(outline.classList.contains('is-landscape')).toBe(true)
    const chosen = within(device).getByRole('button', { name: 'iPhone 15' })
    expect(chosen.dataset.orientation).toBe('landscape')
    const sheet = css('theater.css')
    expect(sheet).toMatch(/\.th-outline\.is-phone\.is-landscape \{[^}]*width: 16px; height: 9px/)
    expect(sheet).toMatch(/\.live-chip\[data-outline="phone"\]\[data-orientation="landscape"\]::before \{[^}]*width: 16px; height: 9px/)
  })

  it('each OTHER LIVE row carries the task’s short id beside its name', async () => {
    stubFetch(answer)
    render(<Tab />)
    await flush()
    const rows = within(screen.getByRole('region', { name: 'OTHER LIVE' })).getAllByRole('listitem')
    expect(rows.length).toBe(1)
    expect(rows[0]!.textContent).toContain('plain-browser')
    expect(rows[0]!.querySelector('.th-agent-id')?.textContent).toBe('plain_br')
  })
})
