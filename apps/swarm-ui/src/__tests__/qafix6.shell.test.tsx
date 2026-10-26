/**
 * THE SHELL AND ITS SHARED MARKS, FROM CONSOLE VISUAL QA RUN 6
 * (QAFIX6-SHELL, part of #1680; run wf_31aca00c977e4d37a935, 2026-10-23, the
 * fixture build of main at d3cdbd62, 1440 and 390 px, light and dark). Each
 * `describe` is one item of the lane, held by the property the review found
 * false, or, for an item already fixed on main, by the property that fix set:
 *
 *   a  6:capacity-001..004, 6:admin-005  A page's end under the Reads bar at
 *      390. The frame keeps the scroller and the dock in two grid rows.
 *   b  6343768697  The 'partial' and 'none' marks drew a dashed bracket off
 *      the right side and a doubled left edge: one even 1px border now.
 *   c  6343761666, 6:admin-007, 6343790327  The admin phone header cut the
 *      workspace to 'Bo…', and named it 'Bogdan' where the desktop switcher
 *      says 'Your workspace'.
 *   d  6343760052  An unticked checkbox or radio in dark is a 3:1 ring.
 *   e  6298219747  HelpCard's '?' takes the app's 2px focus ring.
 *   f  6298219379  A pinned HelpCard is a named dialog.
 *   g  6314116505  The shared refresh keeps focus through its read.
 *   h  6314111042  At 390 the Reads facts are parted by a drawn '·'.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TENANT_HEADER } from '../fetch'
import { HelpCard } from '../HelpCard'
import { HELP } from '../help'
import { Screen } from '../Shell'
import type { Result } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted, resolveColour, tokensIn } from './marks'
import { contrast } from './spaceprobe'

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 5000 } as const

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
  try {
    window.localStorage.clear()
    window.sessionStorage.clear()
  } catch {
    /* storage a test broke is already put back */
  }
})

/** Markup parsed into a host the cascade can read. */
function fragment(html: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

/** A declared length in px: `px` as is, `em` at the 14px body size the phone bar sets its title in. */
function px(v: string | null): number {
  if (v === null || v === 'auto' || v === 'none') return 0
  const m = /^(-?[\d.]+)(px|em)?$/.exec(v.trim())
  if (m === null) throw new Error(`not a plain length: ${v}`)
  return Number(m[1]) * (m[2] === 'em' ? 14 : 1)
}

describe('a: the page scrolls above the Reads bar at 390, never under it', () => {
  it('keeps the scroller and the dock in two rows of the content column', () => {
    const scroller = build('div.sk-app > div.sk-main > div.ctl-scroll', hosts)
    const main = scroller.parentElement!
    const dock = document.createElement('div')
    dock.className = 'ctl-dock'
    main.appendChild(dock)
    for (const env of [PHONE, WIDE]) {
      // MUTATION: `.sk-main { display: block }` (or the rows dropped) and the
      // scroller is the page's height, running on under the dock.
      expect(painted(main, 'display', env)).toBe('grid')
      expect(painted(main, 'grid-template-rows', env)).toBe('minmax(0, 1fr) auto')
      expect(painted(main, 'min-height', env)).toBe('0')
      // The scroller shrinks to its row and scrolls inside it.
      expect(painted(scroller, 'min-height', env)).toBe('0')
      expect(painted(scroller, 'overflow', env)).toBe('auto')
      // MUTATION: a phone rule taking the dock out of flow (`position: fixed`)
      // lays it over the scroller's last row.
      expect(['static', null]).toContain(painted(dock, 'position', env))
    }
  })
})

describe('b: the partial and none marks are closed pills with one even border', () => {
  const MARKS = ['i.ctl-mark.is-partial', 'i.ctl-mark.is-zero'] as const
  const SIDES = ['top', 'right', 'bottom', 'left'] as const

  for (const sel of MARKS) {
    it(`${sel} sets one 1px border shorthand, the same on all four sides`, () => {
      const mark = build(sel, hosts)
      for (const env of [WIDE, PHONE]) {
        for (const side of SIDES) {
          // MUTATION: put back `border-right-style: dashed; border-right-width: 2px`
          // on `.is-partial`, or `border-left-width: var(--track-axis)` on
          // `.is-zero`, and that side's winner is the longhand, not the one shorthand.
          const won = painted(
            mark,
            [`border-${side}-width`, `border-${side}-style`, `border-${side}-color`, `border-${side}`, 'border-width', 'border-style', 'border'],
            env,
          )
          expect(won, `${sel} ${side} at ${env.width}`).toBe('1px solid currentColor')
        }
        // The padding is even too: no side widened to sit beside a thicker edge.
        expect(painted(mark, ['padding-left', 'padding-right', 'padding'], env)).toBe('0 6px')
        // MUTATION: draw the dash as a `::before`/`::after` strip off the box.
        expect(painted(mark, 'content', env, 'before')).toBeNull()
        expect(painted(mark, 'content', env, 'after')).toBeNull()
      }
    })
  }

  it('keeps the squared dashed side on Accounts only (owner, 6287748161)', () => {
    const acct = build('div.acct-page i.ctl-mark.is-partial', hosts)
    expect(painted(acct, 'border-top-right-radius', WIDE)).toBe('0')
    expect(painted(acct, 'background-image', WIDE)).toContain('repeating-linear-gradient')
    const elsewhere = build('div.ap-linked i.ctl-mark.is-partial', hosts)
    expect(painted(elsewhere, ['border-top-right-radius', 'border-radius'], WIDE)).toBe('var(--ctl-radius-sm)')
  })
})

describe('c: the admin phone header names the workspace as the desktop does, and keeps it', () => {
  /** A phone below 760px: `matchMedia` answers the phone query, never the tablet one. */
  function phone(): void {
    vi.stubGlobal('matchMedia', (query: string) => ({
      matches: query.includes('max-width: 759px') || query.includes('560'),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }))
  }

  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
  const OWN = { tenant_id: 'u-bogdan', display_name: 'Your workspace (u-bogdan)', kind: 'user' as const }
  const ENG = { tenant_id: 'eng', display_name: 'eng@example.com', kind: 'group' as const }

  it('draws the desktop switcher’s own label in the phone pill', async () => {
    phone()
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const chosen = new Headers(init?.headers).get(TENANT_HEADER)
      if (url.includes('/v1/tenants/mine')) return json([OWN, ENG])
      if (url.includes('/v1/tenants/me')) {
        const id = chosen ?? OWN.tenant_id
        return json({
          // The tenant document's name for a personal workspace is the
          // person's (fixtureMe's 'Bogdan'): the shell never prints it for one.
          tenant: { tenant_id: id, display_name: id === OWN.tenant_id ? 'Bogdan' : 'Engineering' },
          principal: { email: 'bogdan@example.com', domain: 'example.com', groups: [], is_admin: true },
          environment: 'dev',
          environment_declared: true,
        })
      }
      return json({ pools: [] })
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell, OWN_WORKSPACE_LABEL } = await import('../Spine')
    render(
      <SkyShell section="admin" tab="runner-sizes" title="Runner sizes" go={vi.fn()} foot={null} keepOnSwitch={false}>
        <p>screen</p>
      </SkyShell>,
    )
    const chip = await waitFor(() => {
      const c = document.querySelector<HTMLButtonElement>('header.sk-pbar > button.sk-tchip')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)
    // MUTATION: name the pill from `who.tenant.display_name` again and it says
    // 'Bogdan' where the desktop switcher says 'Your workspace'.
    await waitFor(() => expect(chip.querySelector('.sk-tcn')?.textContent).toBe(OWN_WORKSPACE_LABEL), WAIT)
    // The header that holds the chip is marked, so the title can give way first.
    expect(chip.parentElement?.classList.contains('has-chip')).toBe(true)
  })

  it('lets the workspace give way before the title, and sets no fixed-width children past 390px', () => {
    const bar = build('div.sk-app > header.sk-pbar.has-chip', hosts)
    const title = document.createElement('b')
    bar.appendChild(title)
    const chip = document.createElement('button')
    chip.className = 'sk-tchip'
    const name = document.createElement('span')
    name.className = 'sk-tcn'
    chip.appendChild(name)
    bar.appendChild(chip)
    // The chip holds the short key now, so the title keeps a floor beside
    // it as beside the plain label (visual QA VQ-001, part of #2391): 8em at
    // one size step down, two whole lines of the longest page name
    // (PHONE-BAR-LABEL). MUTATION: drop the floor beside the chip again and
    // the page name goes to 'O…' at 390.
    expect(painted(title, 'min-width', PHONE)).toBe('8em')
    // The member's bar (a plain label, no chip) keeps the title's floor.
    const plain = build('div.sk-app > header.sk-pbar > b', hosts)
    expect(painted(plain, 'min-width', PHONE)).toBe('5.5em')
    // The chip and its name still give way, and before the title (5b:QA-017, VQ-001).
    expect(painted(chip, 'min-width', PHONE)).toBe('0')
    expect(painted(chip, 'flex-shrink', PHONE)).toBe('100')
    expect(painted(name, 'text-overflow', PHONE)).toBe('ellipsis')
    // Every child's declared floor, with the bar's padding and gaps, inside the glass.
    const kids = [
      build('div.sk-app > header.sk-pbar.has-chip > button.sk-ibtn', hosts),
      build('div.sk-app > header.sk-pbar.has-chip > span.sk-pill.is-mini', hosts),
      build('div.sk-app > header.sk-pbar.has-chip > span.sk-pbell', hosts),
      build('div.sk-app > header.sk-pbar.has-chip > button.sk-top', hosts),
      title,
      chip,
    ]
    const floors = kids.map((k) => Math.max(px(painted(k, 'width', PHONE)), px(painted(k, 'min-width', PHONE))))
    const gap = px(painted(bar, ['column-gap', 'gap'], PHONE))
    // Seven children (these six and the 22px mark; the admin tag is in the drawer since VQ-001), so six gaps, and 12px of padding each side.
    const total = floors.reduce((a, b) => a + b, 0) + 22 + gap * kids.length + 2 * 12
    expect(total, `floors ${floors.join(', ')}`).toBeLessThanOrEqual(390)
  })
})

describe('d: an unticked checkbox or radio is a 3:1 edge in dark', () => {
  it('rings every unticked settings control in --ctl-bd, at 3:1 or more on the card', () => {
    const host = fragment(
      '<label class="jb-option"><input type="checkbox"></label><label class="jb-option"><input type="radio"></label>',
    )
    for (const input of host.querySelectorAll('input')) {
      for (const theme of THEMES) {
        const env: CascadeEnv = { width: 1440, theme }
        expect(painted(input, 'appearance', env)).toBe('none')
        const edge = painted(input, ['border', 'border-color'], env)!
        // MUTATION: draw the ring in `--line` and it measures about 1.4:1.
        expect(edge).toContain('var(--ctl-bd)')
        const ratio = contrast(tokensIn(edge, theme)[0]!.rgba, resolveColour('var(--surface)', theme))
        expect(ratio, `${input.type} ${theme}`).toBeGreaterThanOrEqual(3)
      }
    }
  })
})

describe('e: the help glyph takes the app’s 2px focus ring', () => {
  it('draws a 2px solid ring on a focused `?`', () => {
    const glyph = build('button.help-glyph', hosts)
    // MUTATION: drop `.help-glyph:focus-visible` and the browser's 1px auto ring shows.
    expect(painted(glyph, 'outline', { width: 1440, states: ['focus-visible'] })).toBe('2px solid var(--info)')
  })
})

describe('f: a pinned help card is named by its title', () => {
  it('has an accessible name once pinned', () => {
    const title = HELP['api-reads'].title
    render(<HelpCard topic="api-reads" />)
    fireEvent.click(screen.getByRole('button', { name: `Help: ${title}` }))
    // MUTATION: drop `aria-labelledby` from the card and the dialog is unnamed.
    expect(screen.getByRole('dialog', { name: title })).toBeDefined()
  })
})

describe('g: the shared refresh keeps focus through its read', () => {
  it('is still the focused element once the read it started has answered', async () => {
    let n = 0
    const load = vi.fn(async (): Promise<Result<number[]>> => {
      n += 1
      return { status: 'ok', data: Array.from({ length: n }, (_, i) => i), fetchedAt: Date.now() }
    })
    render(
      <Screen<number[]> title="Agents" load={load}>
        {(d) => <span>{d.length} rows</span>}
      </Screen>,
    )
    await screen.findByText('1 rows', undefined, WAIT)
    const refresh = document.querySelector<HTMLButtonElement>('.c-refresh')!
    refresh.focus()
    expect(document.activeElement).toBe(refresh)
    await act(async () => {
      fireEvent.click(refresh)
    })
    await screen.findByText('2 rows', undefined, WAIT)
    // MUTATION: key the control on its state and the answered read draws a
    // new button, so focus falls to <body>.
    expect(document.activeElement).toBe(refresh)
  })
})

describe('h: the Reads facts are parted by a drawn dot at 390', () => {
  it('draws a `·` before each wrapped fact on a phone', () => {
    const fact = build('div.ctl-dock-facts > span.ctl-dock-fact', hosts)
    // MUTATION: drop the phone rule's `::before` and the facts run together.
    expect(painted(fact, 'content', PHONE, 'before')).toBe("'·'")
  })
})
