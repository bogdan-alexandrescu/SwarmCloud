// LANE BSTATUS-STRIP (part of #1030): THE BROWSER TAB AND THE THEATER SAY
// WHICH BROWSER TOOL THE AGENT USES, WHETHER ITS TRAFFIC LEAVES THROUGH THE
// RESIDENTIAL PROXY, AND WHETHER JEV IS ON.
//
// Owner decision 2026-10-14 (~08:00 PDT): "Picker on Submit, Auto by
// default", and the Browser tab and theater strip show the active tool, the
// egress state and Jev's, with hover detail. The live read gains a `browser`
// object (the shared shape in the brief); an older worker serves none, and
// then the strip draws exactly what it drew before.
//
//   1. `browserStatusOf` takes the shape, and refuses anything else (absent,
//      null, malformed) as null -- never a half-drawn badge;
//   2. the badges' words: the tool (Auto: <last used>), Residential · US /
//      Direct / Proxy failing / Proxy off, Jev only when enabled;
//   3. each badge's hover/focus card: what it means, the tool's last action
//      and when, how to change it;
//   4. the regular Browser tab draws them in its strip, after the control
//      state, and an absent object draws nothing new and logs no error;
//   5. the theater draws the same strip above its stage, not over it;
//   6. both themes and a phone: the badges' colours are theme tokens only,
//      and the row wraps on its own instead of pushing the URL or the
//      control state off their line.
//
// MUTATIONS: read `.egress.state` as "ok" by default (2's failing row turns
// red); show [Proxy off] for a direct pod (2's direct row turns red); drop
// the `jev.enabled` check (2's disabled-Jev row turns red); return a partial
// object from `browserStatusOf` (1's malformed rows turn red).

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveSession } from '../api'
import type { Task } from '../types'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  mintLiveTicket: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadLiveRecordings: vi.fn(),
  loadLiveActions: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

vi.mock('@novnc/novnc/core/rfb.js', () => ({
  default: class {
    addEventListener() {}
    disconnect() {}
  },
}))

const { LiveBrowserPane } = await import('../LiveBrowser')
const { BrowserBadges, browserStatusOf } = await import('../BrowserBadges')
type BrowserStatus = NonNullable<ReturnType<typeof browserStatusOf>>

const ID = 'task_0b5c1d2e3f40516273a4'
const ME = ['ada', 'example.com'].join('@')
const NOW = Date.parse('2026-10-14T16:00:00Z')

function agent(): Task {
  return runTask({ id: ID, state: 'RUNNING', runner_profile: 'claude-code-browser', resource_class: 'browser' })
}

function status(over: Partial<BrowserStatus> = {}): BrowserStatus {
  return {
    tool_selected: 'auto',
    tools_available: ['playwright', 'dev-browser', 'jev'],
    tool_last_used: { name: 'playwright', at: new Date(NOW - 120_000).toISOString() },
    jev: { enabled: true, reason: null, key_scope: 'user' },
    egress: { mode: 'residential', country: 'us', state: 'ok', last_error: null },
    ...over,
  }
}

function session(browser?: unknown, over: Partial<LiveSession> = {}): LiveSession {
  const s: LiveSession = {
    session_id: 'live_att_1a2b3c4d5e6f70819203',
    state: 'open',
    closed_reason: null,
    origin: { origin: 'https://github.com', host: 'github.com', registrable_domain: 'github.com', scheme: 'https:' },
    human_wait: null,
    controller: null,
    open_to_members: false,
    device: null,
    presets: [{ name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false }],
    you: { email: ME, can_control: true, can_preempt: true, can_open: true, can_switch_device: false },
    ...over,
  }
  return browser === undefined ? s : ({ ...s, browser } as LiveSession)
}

function pane(s: LiveSession, theater = false, browser?: unknown) {
  return (
    <LiveBrowserPane
      task={agent()}
      live={{ status: 'ok', liveBrowser: true, session: s, ...(browser === undefined ? {} : { browser }) }}
      reload={() => {}}
      theater={theater}
    />
  )
}

const badges = (root: ParentNode = document) => root.querySelector<HTMLElement>('[data-testid="browser-badges"]')
const badge = (kind: string, root: ParentNode = document) =>
  root.querySelector<HTMLElement>(`[data-testid="browser-badge-${kind}"]`)

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
  vi.setSystemTime(NOW)
  api.mintLiveTicket.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveRecordings.mockReturnValue(new Promise(() => {}))
  api.loadLiveActions.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
  vi.restoreAllMocks()
  document.documentElement.removeAttribute('data-theme')
})

describe('1. browserStatusOf takes the shape and nothing else', () => {
  it('takes the shared shape whole', () => {
    expect(browserStatusOf(status())).toEqual(status())
  })

  it('takes a pod that never used a tool and has no proxy', () => {
    const s = status({
      tool_last_used: null,
      jev: { enabled: false, reason: 'no_key', key_scope: null },
      egress: { mode: 'direct', country: null, state: 'off', last_error: null },
    })
    expect(browserStatusOf(s)).toEqual(s)
  })

  it.each([
    ['absent (an older worker)', undefined],
    ['null', null],
    ['a string', 'playwright'],
    ['an unknown tool', { ...status(), tool_selected: 'selenium' }],
    ['no egress', { ...status(), egress: undefined }],
    ['an unknown egress mode', { ...status(), egress: { ...status().egress, mode: 'vpn' } }],
    ['no jev', { ...status(), jev: null }],
    ['tools_available not a list', { ...status(), tools_available: 'playwright' }],
  ])('refuses %s as null', (_why, raw) => {
    expect(browserStatusOf(raw)).toBeNull()
  })

  it('drops an unknown tool from tools_available rather than refusing the whole object', () => {
    expect(browserStatusOf({ ...status(), tools_available: ['playwright', 'selenium'] })?.tools_available).toEqual([
      'playwright',
    ])
  })
})

describe('2. the badges say', () => {
  const words = (s: BrowserStatus) => {
    const view = render(<BrowserBadges status={s} />)
    const out = Array.from(view.container.querySelectorAll('[data-testid^="browser-badge-"]')).map((b) =>
      b.querySelector('.bb-label')?.textContent,
    )
    view.unmount()
    return out
  }

  it('Auto with the tool it last used, Residential · US, and Jev', () => {
    expect(words(status())).toEqual(['Auto: Playwright', 'Residential · US', 'Jev'])
  })

  it('Auto alone before any tool was used', () => {
    expect(words(status({ tool_last_used: null }))[0]).toBe('Auto')
  })

  it.each([
    ['playwright', 'Playwright'],
    ['dev-browser', 'dev-browser'],
    ['jev', 'Jev'],
  ] as const)('the chosen tool by name when one was picked (%s)', (tool, label) => {
    expect(words(status({ tool_selected: tool }))[0]).toBe(label)
  })

  it('Residential without a country when none is pinned', () => {
    expect(words(status({ egress: { mode: 'residential', country: null, state: 'ok', last_error: null } }))[1]).toBe(
      'Residential',
    )
  })

  it('Direct for a pod that leaves through the NAT, never Proxy off', () => {
    const w = words(status({ egress: { mode: 'direct', country: null, state: 'off', last_error: null } }))
    expect(w[1]).toBe('Direct')
    expect(w).not.toContain('Proxy off')
  })

  it('Proxy failing, amber, when the residential proxy is failing', () => {
    render(<BrowserBadges status={status({ egress: { mode: 'residential', country: 'us', state: 'failing', last_error: 'proxy refused the connection' } })} />)
    const b = badge('egress')!
    expect(b.querySelector('.bb-label')?.textContent).toBe('Proxy failing')
    expect(b.classList.contains('is-warn')).toBe(true)
  })

  it('Proxy off when residential was expected and the proxy is off', () => {
    render(<BrowserBadges status={status({ egress: { mode: 'residential', country: 'us', state: 'off', last_error: null } })} />)
    expect(badge('egress')!.querySelector('.bb-label')?.textContent).toBe('Proxy off')
    expect(badge('egress')!.classList.contains('is-warn')).toBe(true)
  })

  it('no Jev badge when Jev is not enabled', () => {
    expect(words(status({ jev: { enabled: false, reason: 'not_selected', key_scope: null } }))).toEqual([
      'Auto: Playwright',
      'Residential · US',
    ])
  })
})

describe('3. each badge explains itself on hover and on focus', () => {
  const card = (b: HTMLElement) => document.getElementById(b.getAttribute('aria-describedby') ?? '')!

  it('the tool: what Auto means, the last action and when, and where it is chosen', () => {
    render(<BrowserBadges status={status()} />)
    const b = badge('tool')!
    expect(b.tabIndex).toBe(0)
    expect(card(b).hidden).toBe(true)
    fireEvent.mouseEnter(b)
    act(() => {
      vi.advanceTimersByTime(200)
    })
    const c = card(b)
    expect(c.hidden).toBe(false)
    expect(c.getAttribute('role')).toBe('tooltip')
    expect(c.textContent).toMatch(/Auto/)
    expect(c.textContent).toMatch(/Last browser action: Playwright, 2m ago/)
    expect(c.textContent).toMatch(/Browser tool on Submit/)
    fireEvent.mouseLeave(b)
    expect(card(b).hidden).toBe(true)
  })

  it('opens at once on focus and closes on Escape', () => {
    render(<BrowserBadges status={status()} />)
    const b = badge('egress')!
    fireEvent.focus(b)
    expect(card(b).hidden).toBe(false)
    fireEvent.keyDown(b, { key: 'Escape' })
    expect(card(b).hidden).toBe(true)
  })

  it('the failing proxy shows its last error', () => {
    render(<BrowserBadges status={status({ egress: { mode: 'residential', country: 'us', state: 'failing', last_error: 'proxy refused the connection' } })} />)
    const b = badge('egress')!
    fireEvent.focus(b)
    expect(card(b).textContent).toMatch(/proxy refused the connection/)
  })

  it('Jev says it sends page text to TypeSafe, whose key it uses, and where keys live', () => {
    render(<BrowserBadges status={status({ tool_last_used: { name: 'jev', at: new Date(NOW - 30_000).toISOString() } })} />)
    const b = badge('jev')!
    fireEvent.focus(b)
    const c = card(b).textContent ?? ''
    expect(c).toMatch(/sends page text to TypeSafe/)
    expect(c).toMatch(/your own TypeSafe key/)
    // Settings › API keys moved to Config › Secrets (SSC-UI, part of #2158).
    expect(c).toMatch(/Config › Secrets/)
    expect(c).toMatch(/Last browser action: Jev, 30s ago/)
  })

  it('a tool that never acted says so', () => {
    render(<BrowserBadges status={status({ tool_selected: 'dev-browser', tool_last_used: null })} />)
    const b = badge('tool')!
    fireEvent.focus(b)
    expect(card(b).textContent).toMatch(/No browser action yet/)
  })

  it('Jev picked but not enabled says why on the tool badge', () => {
    render(<BrowserBadges status={status({ tool_selected: 'jev', jev: { enabled: false, reason: 'no_key', key_scope: null } })} />)
    const b = badge('tool')!
    fireEvent.focus(b)
    expect(card(b).textContent).toMatch(/no TypeSafe key/)
    expect(badge('jev')).toBeNull()
  })
})

describe('4. the Browser tab', () => {
  it('draws the badges in the strip, after the control state', () => {
    render(pane(session(status())))
    const strip = document.querySelector<HTMLElement>('.live-strip')!
    const b = badges(strip)!
    expect(b).not.toBeNull()
    const who = strip.querySelector('.live-strip-who')!
    expect(who.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(strip.querySelector('.live-origin')?.textContent).toMatch(/github\.com/)
    expect(within(strip).getByText('Nobody is in control: the agent is driving.')).not.toBeNull()
  })

  it('reads the object from the top of the live read as well as from the session', () => {
    render(pane(session(), false, status({ tool_selected: 'dev-browser' })))
    expect(badge('tool')!.querySelector('.bb-label')?.textContent).toBe('dev-browser')
  })

  it.each([
    ['absent', undefined],
    ['null', null],
    ['malformed', { tool_selected: 'auto' }],
  ])('draws nothing new and logs no error when the object is %s', (_why, raw) => {
    const errors = vi.spyOn(console, 'error')
    render(pane(session(raw)))
    expect(document.querySelector('.live-strip')).not.toBeNull()
    expect(badges()).toBeNull()
    expect(errors).not.toHaveBeenCalled()
  })

  it('keeps the hand-off banner and the strip warning beside the badges', () => {
    const s = session(status(), {
      human_wait: {
        reason: 'sign in',
        expect: 'accounts.google.com',
        requested_at: new Date(NOW - 60_000).toISOString(),
        deadline: new Date(NOW + 600_000).toISOString(),
      },
    })
    render(pane(s))
    const strip = document.querySelector<HTMLElement>('.live-strip')!
    expect(strip.querySelector('.live-strip-say')?.textContent).toMatch(/the agent said it would ask you to sign in/)
    expect(badges(strip)).not.toBeNull()
  })
})

describe('5. the theater', () => {
  it('draws the same strip, with the badges, above its stage', () => {
    render(pane(session(status({ egress: { mode: 'residential', country: 'us', state: 'failing', last_error: 'proxy timed out' } })), true))
    const theater = screen.getByRole('dialog', { name: /^Theater/ })
    const strip = theater.querySelector<HTMLElement>('.th-strip')!
    expect(strip).not.toBeNull()
    expect(badge('egress', strip)!.querySelector('.bb-label')?.textContent).toBe('Proxy failing')
    const stage = theater.querySelector('.th-stage')!
    // A sibling before the stage in the column, not a layer over it.
    expect(strip.parentElement).toBe(stage.parentElement)
    expect(strip.compareDocumentPosition(stage) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('draws nothing new in the theater for an older worker', () => {
    render(pane(session(), true))
    const theater = screen.getByRole('dialog', { name: /^Theater/ })
    expect(theater.querySelector('.th-strip')).not.toBeNull()
    expect(badges(theater)).toBeNull()
  })
})

describe('6. both themes, and a phone', () => {
  const css = readFileSync(join(__dirname, '..', 'styles', 'live.css'), 'utf8')
  const rules = css
    .split('}')
    .filter((r) => /\.bb-/.test(r.split('{')[0] ?? ''))
    .map((r) => r.split('{')[1] ?? '')

  it.each(['light', 'dark'])('renders the same badges under the %s theme', (theme) => {
    document.documentElement.setAttribute('data-theme', theme)
    render(<BrowserBadges status={status()} />)
    expect(badges()?.querySelectorAll('[data-testid^="browser-badge-"]').length).toBe(3)
  })

  it('colours the badges with theme tokens only, never a literal colour', () => {
    expect(rules.length).toBeGreaterThan(3)
    for (const body of rules) {
      expect(body).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(/)
    }
  })

  it('wraps the row on its own and right-aligns it, so the URL and the control state keep their line', () => {
    const row = css.match(/\.bb-row\s*\{([^}]*)\}/)?.[1] ?? ''
    expect(row).toMatch(/flex-wrap:\s*wrap/)
    expect(row).toMatch(/justify-content:\s*flex-end/)
    expect(row).toMatch(/min-width:\s*0/)
    const label = css.match(/\.bb-badge\s*\{([^}]*)\}/)?.[1] ?? ''
    expect(label).toMatch(/white-space:\s*nowrap/)
  })

  it('at 390px the card is held inside the screen', () => {
    const tip = css.match(/\.bb-card\s*\{([^}]*)\}/)?.[1] ?? ''
    expect(tip).toMatch(/max-width:\s*min\(/)
  })
})
