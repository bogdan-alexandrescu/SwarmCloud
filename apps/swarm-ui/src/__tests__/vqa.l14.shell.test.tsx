// VISUAL QA, LANE VQA-L14 (2026-10-11, part of #1038): the shell chrome, the
// failure panel and its copy. Each case asserts the PROPERTY the finding was
// about -- the rule the cascade picks, the class a panel carries, the words
// on screen, the tab stops -- not the markup's shape.
//
//   V007  A 404's copy names no "task", and a 404 on a read offers no Try again.
//   V008  The failure panel's body is 13px nested and --t-lead as a page;
//         Try again sits 12px under the panel's last line.
//   V009  `Top` rides in the phone header and never shrinks there.
//   V010  A write's refusal is not a read's failure (the central half).
//   V027  The phone header's menu keeps the spine's hover fill in light.
//   V050  The dark palette declares `color-scheme: dark`, the light ones light.
//   V054  The principal's name and email are each one line, cut.
//   V063  The screen skeleton is a card of uneven rows, not six equal bars.
//   V064  The page head is a control tall in every state, actions or none.
//   V074  The theme switch is one Tab stop, moved by the arrow keys.
//   V083  Collapsed, a click on the section you are in opens its pages.
//   V092  A screen read once says `read 0 s ago`, not a bare `0 s`; on a phone the
//         refresh's 44px target is not clipped or counted as content.
//   V129  Send-panel values end at the right edge; mono is the meta size.
//   V134  A merge option's description is lighter than its title on every card.
//   V149  The phone header's tenant is sans.
//   (V150 is in shell.v2.test.tsx, beside the panel counts it changes.)
//
// BREAK IT: put "This task does not exist" back in `errorReassurance` -- V007
// fails; drop `.state.is-nested p` -- V008 fails; drop `color-scheme: dark`
// from `:root` -- V050 fails; give every theme button `tabIndex={0}` -- V074
// fails; restore the panel-expanding click -- V083 fails.

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { errorReassurance, type ApiError, type Result } from '../fetch'
import { FailedPanel, Screen, SkeletonRows } from '../Shell'
import { SkyShell } from '../Spine'
import { ThemeToggle } from '../ThemeToggle'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'

const PHONE: CascadeEnv = { width: 400 }
const WIDE: CascadeEnv = { width: 1440 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  document.documentElement.removeAttribute('data-theme')
  try {
    window.localStorage.clear()
  } catch {
    /* storage may be unavailable */
  }
})

const missing: ApiError = { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'account acct-x not found' }
const broken: ApiError = { kind: 'server_error', httpStatus: 500, code: null, message: 'boom' }

// ---------------------------------------------------------------------------
// The failure panel
// ---------------------------------------------------------------------------

describe('V007: a 404 says what a 404 can tell, about any kind of thing', () => {
  it('names no task, keeps the two-reasons caveat, and offers no Try again', () => {
    expect(errorReassurance(missing)).not.toMatch(/task/i)
    expect(errorReassurance(missing)).toContain('belongs to another tenant')
    render(<FailedPanel error={missing} onRetry={() => undefined} />)
    const panel = document.querySelector('.state.failed')!
    expect(panel.textContent).toContain('account acct-x not found')
    expect(screen.queryByRole('button', { name: 'Try again' }), 'a retry that can only 404 again').toBeNull()
  })

  it('keeps Try again for a read that failed rather than answered', () => {
    render(<FailedPanel error={broken} onRetry={() => undefined} />)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })
})

describe('V008: the failure panel speaks at the size of what holds it', () => {
  it('is the 13px scale nested, the lead size as a page, and the HTTP line stays micro', () => {
    const nested = build('div.state.failed.is-nested > p', hosts)
    expect(painted(nested, 'font-size', WIDE)).toBe('var(--t-meta)')
    expect(painted(nested, 'font-size', PHONE)).toBe('var(--t-meta)')
    const page = build('div.state.failed.is-page > p', hosts)
    expect(painted(page, 'font-size', WIDE)).toBe('var(--t-lead)')
    const http = build('div.state.failed.is-nested > p.checked-at', hosts)
    expect(painted(http, ['font-size', 'font'], WIDE)).toContain('var(--t-micro)')
  })

  it('puts Try again 12px under the panel, not ~4px', () => {
    const btn = build('div.state.failed.is-page > button.c-btn', hosts)
    expect(painted(btn, 'margin-top', WIDE)).toBe('12px')
  })

  it('is nested by default and a page under a Screen whose read failed', async () => {
    render(<FailedPanel error={broken} onRetry={() => undefined} />)
    expect(document.querySelector('.state.failed')!.classList.contains('is-nested')).toBe(true)
    cleanup()
    const failed: Result<{ n: number }> = { status: 'error', error: broken }
    render(
      <Screen<{ n: number }> title="Pools" load={async () => failed}>
        {() => null}
      </Screen>,
    )
    const panel = await screen.findByText('boom')
    expect(panel.closest('.state.failed')!.classList.contains('is-page')).toBe(true)
  })
})

describe('V010: a write\'s failure is told from a read\'s by its method, not by its caller', () => {
  it('a refused write is not a failed read and offers no resend', () => {
    const refused: ApiError = { kind: 'invalid', httpStatus: 422, code: null, message: 'label is bad', method: 'POST' }
    render(<FailedPanel error={refused} onRetry={() => undefined} />)
    const panel = document.querySelector('.state.failed')!
    expect(panel.textContent).not.toContain('failure to read the platform')
    expect(panel.querySelector('.ctl-mark.is-unread')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The phone header
// ---------------------------------------------------------------------------

describe('V009: `Top` rides in the phone header', () => {
  it('never shrinks there, is hidden on a desktop, and has a 44px target', () => {
    const top = build('header.sk-pbar > button.sk-top', hosts)
    expect(painted(top, 'display', PHONE)).toBe('inline-flex')
    expect(painted(top, 'flex', PHONE)).toBe('none')
    expect(painted(top, 'display', WIDE)).toBe('none')
    expect(painted(top, 'height', PHONE, 'after')).toBe('44px')
  })
})

describe('V027: the phone menu keeps a dark hover in the light theme', () => {
  it('fills with the spine hover, not the near-white panel hover', () => {
    const menu = build('div.sk-app > header.sk-pbar > button.sk-ibtn', hosts)
    for (const theme of ['light', 'dark'] as const) {
      const env: CascadeEnv = { width: 400, theme, states: ['hover'] }
      expect(painted(menu, ['background', 'background-color'], env), theme).toBe('#194b89')
      expect(painted(menu, 'color', env), theme).toBe('#fff')
    }
  })
})

describe('V149: the phone header names the tenant in sans', () => {
  it('draws `.sk-tn` in the sans face', () => {
    const tn = build('header.sk-pbar > span.sk-tn', hosts)
    const font = painted(tn, ['font', 'font-family'], PHONE) ?? ''
    expect(font).toContain('var(--font)')
    expect(font).not.toContain('var(--mono)')
  })
})

// ---------------------------------------------------------------------------
// The theme
// ---------------------------------------------------------------------------

describe('V050: native controls follow the palette', () => {
  it('says dark on the base palette and light on both light ones', () => {
    const root = document.documentElement
    expect(painted(root, 'color-scheme', { width: 1440, theme: 'dark' })).toBe('dark')
    expect(painted(root, 'color-scheme', { width: 1440, theme: 'light' })).toBe('light')
    // Dark chosen on a light OS: the light media block excludes it.
    root.setAttribute('data-theme', 'dark')
    expect(painted(root, 'color-scheme', { width: 1440, theme: 'light' })).toBe('dark')
    // Light chosen on a dark OS.
    root.setAttribute('data-theme', 'light')
    expect(painted(root, 'color-scheme', { width: 1440, theme: 'dark' })).toBe('light')
  })
})

describe('V074: the theme switch is one Tab stop', () => {
  it('puts only the chosen choice in the Tab order, and the arrows move the choice and the focus', () => {
    render(<ThemeToggle />)
    const radios = screen.getAllByRole('radio')
    expect(radios).toHaveLength(3)
    const stops = radios.filter((r) => r.tabIndex === 0)
    expect(stops, 'more than one Tab stop').toHaveLength(1)
    expect(stops[0]!.getAttribute('aria-checked')).toBe('true')
    const from = radios.indexOf(stops[0]!)
    act(() => stops[0]!.focus())
    fireEvent.keyDown(stops[0]!, { key: 'ArrowRight' })
    const next = radios[(from + 1) % 3]!
    expect(next.getAttribute('aria-checked')).toBe('true')
    expect(next.tabIndex).toBe(0)
    expect(document.activeElement).toBe(next)
    expect(radios.filter((r) => r.tabIndex === 0)).toHaveLength(1)
    fireEvent.keyDown(next, { key: 'Home' })
    expect(radios[0]!.getAttribute('aria-checked')).toBe('true')
    expect(document.activeElement).toBe(radios[0])
  })
})

// ---------------------------------------------------------------------------
// The panel
// ---------------------------------------------------------------------------

describe('V054: the principal stays inside the panel', () => {
  it('cuts the name and the email on one line each, and the admin tag keeps its width', () => {
    const name = build('nav.sk-panel > div.sk-pfoot > span.sk-pfrow > b', hosts)
    expect(painted(name, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(name, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(name, 'min-width', WIDE)).toBe('0')
    const email = build('nav.sk-panel > div.sk-pfoot > small', hosts)
    expect(painted(email, 'white-space', WIDE), 'the email wraps under its own ellipsis').toBe('nowrap')
    expect(painted(email, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(build('div.sk-pfoot > span.sk-pfrow > span.sk-adm', hosts), 'flex', WIDE)).toBe('none')
  })
})

describe('V083: collapsed, the section you are in opens its pages', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    window.localStorage.setItem('swarm.shell.collapsed', '1')
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  // Capacity, not Admin, since NAV-ADMIN-ROLE (#1633): the fixture caller
  // administers nothing, so it is shown no Admin item to click.
  it('opens the Capacity flyout on a click, without expanding, and the flyout can expand', () => {
    const go = vi.fn()
    const { container: c } = render(
      <SkyShell section="capacity" tab="pools" title="Pools" go={go} foot={null}>
        <button type="button">page</button>
      </SkyShell>,
    )
    expect(c.querySelector('.sk-app.is-collapsed')).not.toBeNull()
    fireEvent.click(c.querySelector<HTMLElement>('.sk-spine [data-sec="capacity"]')!)
    expect(c.querySelector('.sk-app.is-collapsed'), 'the click expanded the panel').not.toBeNull()
    const fly = c.querySelector('.sk-flyout')
    expect(fly, 'no way to the other Capacity pages').not.toBeNull()
    // API reads is Capacity's last page since NAV-FOOT (#1672).
    expect([...fly!.querySelectorAll('a')].map((a) => a.textContent)).toEqual(['Pools', 'Runtimes', 'Holders', 'Accounts', 'API reads'])
    expect(go).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Open the panel' }))
    expect(c.querySelector('.sk-app.is-collapsed')).toBeNull()
    expect(c.querySelector('.sk-flyout')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The page
// ---------------------------------------------------------------------------

describe('V063: the screen skeleton is a card, in the shape the data lands into', () => {
  it('draws one card with a head and uneven rows at the table row height', () => {
    const { container } = render(<SkeletonRows />)
    const card = container.querySelector('.ctl-card')
    expect(card, 'no card frame').not.toBeNull()
    expect(card!.querySelector('.ctl-card-head .skeleton')).not.toBeNull()
    const bars = [...card!.querySelectorAll<HTMLElement>('.ctl-skel-row > .skeleton')]
    expect(bars).toHaveLength(6)
    const widths = new Set(bars.map((b) => b.style.width))
    expect(widths.size, 'every bar is the same width').toBeGreaterThan(3)
    for (const b of bars) expect(b.style.width).not.toBe('100%')
    expect(painted(build('div.ctl-skel-row', hosts), 'height', WIDE)).toBe('38px')
  })
})

describe('V064: the page head is a control tall in every state', () => {
  it('holds the title row at the control height, wide and on a phone', () => {
    const head = build('div.c-phead', hosts)
    for (const env of [WIDE, PHONE]) expect(painted(head, 'min-height', env)).toBe('var(--h-ctl)')
  })
})

describe('V092: a screen read once says its age is a read age', () => {
  it('reads `read 0 s ago`, not a bare `0 s`, when the screen passes no pollMs', async () => {
    // MUTATION: let `RefreshControl` say the bare age when it has no cadence.
    render(
      <Screen<{ n: number }> title="Runtimes" load={async () => ({ status: 'ok', data: { n: 1 }, fetchedAt: Date.now() })}>
        {() => <p>rows</p>}
      </Screen>,
    )
    await screen.findByText('rows')
    const refresh = document.querySelector<HTMLButtonElement>('.c-phead .c-acts .c-refresh')!
    expect(refresh.textContent).toMatch(/^⟳ read once · \d+ s ago$/)
    expect(refresh.getAttribute('title')).toMatch(/^read once · \d+ s ago$/)
  })
})

describe('V092: the refresh on a phone', () => {
  it('neither clips its 44px target nor is narrower than it; the desktop keeps its ellipsis', () => {
    const refresh = build('div.c-phead > div.c-acts > button.c-refresh', hosts)
    expect(painted(refresh, 'overflow', PHONE)).toBe('visible')
    expect(painted(refresh, 'min-width', PHONE)).toBe('44px')
    expect(painted(refresh, 'overflow', WIDE)).toBe('hidden')
  })
})

// ---------------------------------------------------------------------------
// Submit's panels
// ---------------------------------------------------------------------------

describe('V129: the send panel\'s facts', () => {
  it('end every value at the right edge, key at the left, and set mono at the meta size', () => {
    const fact = build('div.sbf-send > ul.ctl-facts > li.ctl-fact', hosts)
    expect(painted(fact, 'text-align', WIDE)).toBe('right')
    expect(painted(build('div.sbf-send > ul.ctl-facts > li.ctl-fact > b', hosts), 'text-align', WIDE)).toBe('left')
    const code = build('div.sbf-send > ul.ctl-facts > li.ctl-fact > code', hosts)
    expect(painted(code, 'font-size', WIDE)).toBe('var(--t-meta)')
    expect(painted(code, 'font-family', WIDE)).toBe('var(--mono)')
  })
})

describe('V134: a merge option\'s description is lighter than its title', () => {
  it('is one weight and one size on every card, below the title\'s', () => {
    const title = build('label.dsp-option > span.dsp-option-body > span.dsp-option-head > b', hosts)
    expect(painted(title, 'font-weight', WIDE)).toBe('600')
    const opens = build('label.dsp-option > span.dsp-option-body > span.dsp-count', hosts)
    const none = build('label.dsp-option > span.dsp-option-body > span.dsp-count.is-none', hosts)
    const font = painted(opens, ['font', 'font-weight'], WIDE) ?? ''
    expect(font).toMatch(/^400 var\(--t-meta\)/)
    // Collect differs by ink only: it inherits the same font.
    expect(painted(none, ['font', 'font-weight'], WIDE)).toBe(font)
    expect(painted(none, 'color', WIDE)).toBe('var(--text-dim)')
    expect(painted(opens, 'color', WIDE)).toBe('var(--text)')
  })
})
