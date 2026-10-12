// The theater's timeline shows what happened, not every protocol call (lane LB-R2, part of #1030).
//
// MEASURED 2026-10-12 08:05-08:20 PDT (re-QA after release f1c242c8): the
// Timeline listed every raw CDP method the agent's browser sent --
// "Browser.getVersion", "Target.setAutoAttach", "Runtime.enable",
// "Page.enable", "Runtime.runIfWaitingForDebugger" xn,
// "Target.detachFromTarget" -- about 40 lines in the first seconds, burying
// "You took control", the device changes and the refusals LB-V3 added.
// This file holds what the column shows by default: navigations with their
// URL (no query), what people did, hand-offs, refusals in words, input as
// counts, and every other method folded into one "N protocol calls" group per
// burst, opened by "Show protocol".

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TheaterTimeline, foldActions, type LiveAction } from '../TheaterTimeline'
import { ALL_CSS } from './sheets'

function serve(actions: LiveAction[]): void {
  globalThis.fetch = vi.fn(
    async () =>
      new Response(JSON.stringify({ attempt_id: 'att_fold', actions }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
  ) as unknown as typeof fetch
}

afterEach(() => {
  vi.restoreAllMocks()
})

const T0 = Date.parse('2026-10-12T15:05:00Z')
const at = (s: number) => new Date(T0 + s * 1000).toISOString()
const cdp = (s: number, method: string, over: Partial<LiveAction> = {}): LiveAction => ({ at: at(s), kind: 'cdp', method, ...over })
const ctl = (s: number, event: string, over: Partial<LiveAction> = {}): LiveAction => ({ at: at(s), kind: 'control', event, ...over })

/** The opening burst QA measured: a browser attaching, before anything happened. */
const STARTUP: LiveAction[] = [
  cdp(0, 'Browser.getVersion'),
  cdp(0, 'Target.setAutoAttach'),
  cdp(0, 'Target.setDiscoverTargets'),
  cdp(0, 'Runtime.enable'),
  cdp(0, 'Page.enable'),
  ...Array.from({ length: 30 }, (_, i) => cdp(1, i % 2 ? 'Runtime.runIfWaitingForDebugger' : 'Network.enable')),
  cdp(1, 'Target.detachFromTarget'),
]

const SESSION: LiveAction[] = [
  ...STARTUP,
  cdp(2, 'Page.navigate', { url: 'https://github.com/login?return_to=%2Fsettings#top' }),
  cdp(2, 'Page.getFrameTree'),
  cdp(3, 'Input.dispatchMouseEvent'),
  cdp(3, 'Runtime.evaluate'),
  cdp(3, 'Input.dispatchMouseEvent'),
  cdp(3, 'Input.dispatchMouseEvent'),
  cdp(4, 'Input.insertText', { length: 7 }),
  cdp(4, 'Input.insertText', { length: 5 }),
  cdp(4, 'Input.dispatchKeyEvent'),
  ctl(30, 'control_taken', { by: 'alice@saga.xyz', you: true }),
  ctl(31, 'device_changed', { by: 'alice@saga.xyz', you: true, preset: 'iphone-15', orientation: 'portrait', touch: true }),
  cdp(40, 'Page.navigate', { url: 'https://example.test/elsewhere', outcome: 'refused', cause: 'method_policy' }),
  ctl(50, 'handoff_resolved', { outcome: 'returned' }),
]

function lines(): { what: string; kind: string }[] {
  const list = screen.getByRole('list', { name: 'Agent actions' })
  return [...list.children].map((li) => ({
    what: li.querySelector('.th-act-what')!.textContent ?? '',
    kind: li.className,
  }))
}

describe('the timeline shows what happened', () => {
  it('folds the 36-call startup burst into one collapsed group', async () => {
    serve(STARTUP)
    render(<TheaterTimeline taskId="task_live" live={false} />)
    await screen.findByRole('list', { name: 'Agent actions' })
    expect(lines().map((l) => l.what)).toEqual(['36 protocol calls'])
    // Collapsed: no method name is on screen until a person asks for it.
    expect(screen.getByRole('list', { name: 'Agent actions' }).textContent).not.toContain('Runtime.enable')
  })

  it('draws a session as navigations, people, refusals and input counts, with protocol folded per burst', async () => {
    serve(SESSION)
    render(<TheaterTimeline taskId="task_live" live={false} />)
    await screen.findByRole('list', { name: 'Agent actions' })
    expect(lines().map((l) => l.what)).toEqual([
      '36 protocol calls',
      'The agent went to',
      // One burst: the frame-tree read came before the first click, so its
      // group comes first; each is one line however the two interleave.
      '2 protocol calls',
      'Agent input: 12 characters typed, 1 key event, 3 mouse events',
      'You took control',
      'You switched the device to iPhone 15, portrait, touch on',
      'A navigation was refused: the live gate does not let the agent do that',
      'Hand-off resolved: control is back with the agent',
    ])
    const list = screen.getByRole('list', { name: 'Agent actions' })
    // The navigation's URL, its query string and fragment stripped.
    expect(list.textContent).toContain('https://github.com/login')
    expect(list.textContent).not.toContain('return_to')
    expect(list.textContent).not.toContain('#top')
    expect(list.querySelectorAll('.is-refused')).toHaveLength(1)
  })

  it('"Show protocol" opens every group, naming each method once with its repeat count', async () => {
    serve(SESSION)
    render(<TheaterTimeline taskId="task_live" live={false} />)
    await screen.findByRole('list', { name: 'Agent actions' })
    const toggle = screen.getByRole('button', { name: 'Show protocol' })
    expect(toggle.getAttribute('aria-pressed')).toBe('false')
    const groups = () => [...document.querySelectorAll('details.th-proto')] as HTMLDetailsElement[]
    expect(groups()).toHaveLength(2)
    expect(groups().every((d) => !d.open)).toBe(true)

    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: 'Hide protocol' }).getAttribute('aria-pressed')).toBe('true')
    expect(groups().every((d) => d.open)).toBe(true)
    const first = [...groups()[0]!.querySelectorAll('li')].map((li) => li.textContent)
    expect(first).toContain('Runtime.runIfWaitingForDebugger ×15')
    expect(first).toContain('Browser.getVersion')
    expect(first).toContain('Target.detachFromTarget')
    expect(groups()[1]!.textContent).toContain('Page.getFrameTree')

    fireEvent.click(screen.getByRole('button', { name: 'Hide protocol' }))
    expect(groups().every((d) => !d.open)).toBe(true)
  })

  it('a timeline with no protocol call offers no protocol toggle', async () => {
    serve([ctl(0, 'control_taken', { you: true })])
    render(<TheaterTimeline taskId="task_live" live={false} />)
    await screen.findByRole('list', { name: 'Agent actions' })
    expect(screen.queryByRole('button', { name: /protocol/ })).toBeNull()
  })
})

describe('foldActions', () => {
  it('starts a new burst after a quiet gap, so two minutes apart are two groups', () => {
    const folded = foldActions([cdp(0, 'Runtime.enable'), cdp(1, 'Page.enable'), cdp(120, 'Runtime.evaluate')])
    expect(folded.map((e) => (e.type === 'protocol' ? e.calls.length : e.type))).toEqual([2, 1])
  })

  it('keeps every refused method, whatever its name, as its own line', () => {
    const folded = foldActions([cdp(0, 'Runtime.evaluate', { outcome: 'refused', cause: 'not_its_session' }), cdp(0, 'Runtime.enable')])
    expect(folded.map((e) => e.type)).toEqual(['action', 'protocol'])
  })

  it('counts typing by characters and the pointer by events, never by method lines', () => {
    const folded = foldActions([
      cdp(0, 'Input.insertText', { length: 3 }),
      cdp(0, 'Input.dispatchTouchEvent'),
      cdp(0, 'Input.dispatchTouchEvent'),
      cdp(0, 'Input.dispatchKeyEvent'),
      cdp(0, 'Input.dispatchKeyEvent'),
    ])
    expect(folded).toHaveLength(1)
    expect(folded[0]).toMatchObject({ type: 'input', typed: 3, keys: 2, touches: 2, mouse: 0 })
  })

  it('a new page the agent opened is shown; a reload is shown', () => {
    const folded = foldActions([
      cdp(0, 'Target.createTarget', { url: 'https://github.com/' }),
      cdp(0, 'Page.reload'),
    ])
    expect(folded.map((e) => e.type)).toEqual(['action', 'action'])
  })
})

describe('readable at a 280 px column', () => {
  it('wraps a URL and a method name instead of cutting either off with an ellipsis', () => {
    for (const sel of ['.th-act-url', '.th-proto-calls']) {
      const rule = new RegExp(`${sel.replace('.', '\\.')}[^{]*\\{([^}]*)\\}`).exec(ALL_CSS)
      expect(rule, `${sel} has a rule`).not.toBeNull()
      expect(rule![1]).not.toMatch(/text-overflow:\s*ellipsis/)
      expect(rule![1]).not.toMatch(/white-space:\s*nowrap/)
      expect(rule![1]).toMatch(/overflow-wrap:\s*anywhere/)
    }
  })

  it('breaks a method name only at its dot', async () => {
    serve(STARTUP)
    render(<TheaterTimeline taskId="task_live" live={false} />)
    await screen.findByRole('list', { name: 'Agent actions' })
    fireEvent.click(screen.getByRole('button', { name: 'Show protocol' }))
    const item = [...document.querySelectorAll('.th-proto-calls li')].find((li) => li.textContent === 'Browser.getVersion')!
    expect(item.innerHTML).toContain('Browser.<wbr>getVersion')
  })
})
