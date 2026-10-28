// AN AGENT'S BROWSER TAB, THE THREE FOLLOW-UPS (#2423, BT-015..017).
//
// The Claude-in-Chrome walk of 2026-10-26 measured the console's frame around
// the Browser tab at 1440x757, 1024x625 and 390x844, in both themes. Three of
// its findings sat in the shell and the agent list rather than in the tab:
//
//   BT-015  A Recent workflow whose state this page load has not read draws
//           no glyph, and its `.sk-st` was 0px tall: the state was there for
//           a screen reader only. It now has a stand-in, a 12x12 dashed ring
//           whose title is the `last seen …` word.
//   BT-016  The frame clipped on short screens: at 1440x757 the panel's
//           pages scrolled by 12px, and at 1024x625 the collapsed spine
//           (780px of items) put Help and Profile below the fold. A screen
//           800px tall or less packs the spine and the panel rows tighter.
//           At 390 the page title keeps its floor (VQ-001/PHONE-BAR-LABEL,
//           held here as a guard).
//   BT-017  A list row's sub-line was cut with no title, so the owner's name
//           was lost; a failure reason clamped to two lines had no ellipsis;
//           and at 1024 the agent opened as an overlay on the list with no
//           backdrop, cutting list text mid-word.
//
// EVERYTHING VISUAL IS READ THROUGH THE CASCADE (`painted`, marks.ts), as
// browser-tab.logs.test.tsx does: jsdom applies no `@media` and lays nothing
// out, so the heights below are sums of what the sheet declares.

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, build, painted, resolveColour } from './marks'
import { task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(async () => ({ status: 'error', error: { kind: 'unreachable', httpStatus: null, code: null, message: 'no' } })),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const Spine = await import('../Spine')
const { AgentsScreen, TaskRow } = await import('../Agents')

/** The three windows the walk measured, each in both themes. */
const WINDOWS: readonly { width: number; height: number }[] = [
  { width: 1440, height: 757 },
  { width: 1024, height: 625 },
  { width: 390, height: 844 },
]
const ENVS: CascadeEnv[] = WINDOWS.flatMap((w) => THEMES.map((theme) => ({ ...w, theme })))
const WAIT = { timeout: 5000 }

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  window.localStorage.clear()
  window.history.replaceState(null, '', '/')
  vi.clearAllMocks()
})

/** A px length the cascade chose, as a number; `0` and `none` read as 0. */
function px(el: Element, prop: string, env: CascadeEnv, pseudo: string | null = null): number {
  const v = painted(el, prop, env, pseudo)
  if (v === null || v === '0' || v === 'none' || v === 'auto') return 0
  const m = /^(-?[\d.]+)px$/.exec(v.trim())
  if (m === null) throw new Error(`${prop} is ${JSON.stringify(v)}, not a px length`)
  return Number(m[1])
}

/** The two block-axis lengths of a `padding`/`margin` shorthand, or its longhands. */
function block(el: Element, box: 'padding' | 'margin', env: CascadeEnv): number {
  const top = painted(el, `${box}-top`, env)
  const bottom = painted(el, `${box}-bottom`, env)
  const short = painted(el, box, env)
  const sides = (short ?? '0').trim().split(/\s+/)
  const fromShort = (i: 0 | 2): string => sides[i] ?? sides[0]!
  const len = (v: string): number => (v === '0' ? 0 : Number(/^([\d.]+)px$/.exec(v)?.[1] ?? NaN))
  // The cascade answers per property; a longhand declared after the shorthand
  // in a narrower query is the one a browser would use, so prefer it.
  return len(top ?? fromShort(0)) + len(bottom ?? fromShort(2))
}

/** The row gap of a flex column: `row-gap`, else the first value of `gap`. */
function gapOf(el: Element, env: CascadeEnv): number {
  const v = painted(el, 'row-gap', env) ?? painted(el, 'gap', env)
  if (v === null || v.trim() === '0') return 0
  const m = /^([\d.]+)px/.exec(v.trim())
  if (m === null) throw new Error(`gap is ${JSON.stringify(v)}, not a px length`)
  return Number(m[1])
}

// ---------------------------------------------------------------------------
// BT-015
// ---------------------------------------------------------------------------

describe('BT-015: a Recent workflow whose mark is withheld still shows a stand-in', () => {
  it('draws a 12x12 ring titled with the "last seen" word, at every window and theme', () => {
    window.localStorage.setItem(Spine.RECENT_WORKFLOWS_KEY, JSON.stringify([{ id: 'wf_bt015', state: 'RUNNING', name: 'stale-lane' }]))
    expect(Spine.workflowReadThisLoad('wf_bt015')).toBe(false)
    render(
      <Spine.SkyShell section="work" tab="workflows" title="Workflows" go={vi.fn()} foot={null}>
        {null}
      </Spine.SkyShell>,
    )
    const group = screen.getByRole('group', { name: 'Recent workflows' })
    const row = [...group.querySelectorAll<HTMLElement>('a')].find((a) => a.textContent?.includes('stale-lane'))!
    const mark = row.querySelector<HTMLElement>('.sk-st')!
    // Still not drawn as a state: no glyph, and the word says why.
    expect(mark.querySelector('svg')).toBeNull()
    expect(mark.getAttribute('data-mark')).toBe('stale')
    expect(mark.getAttribute('title')).toMatch(/^last seen running; not re-read/)
    for (const env of ENVS) {
      const at = `at ${env.width}x${env.height} ${env.theme}`
      // MUTATION: drop the `.is-withheld::before` ring (styles.css) and the
      // mark is 0px tall again: the walk's `span.sk-st.is-neu height 0`.
      expect(painted(mark, 'content', env, 'before'), at).not.toBeNull()
      expect(px(mark, 'height', env, 'before'), at).toBe(12)
      expect(px(mark, 'width', env, 'before'), at).toBe(12)
      expect(painted(mark, 'border-style', env, 'before') ?? painted(mark, 'border', env, 'before'), at).toMatch(/dashed/)
    }
  })
})

// ---------------------------------------------------------------------------
// BT-016
// ---------------------------------------------------------------------------

/**
 * THE COLLAPSED SPINE'S HEIGHT, from what the sheet declares: the longest
 * spine there is (a platform admin, panel collapsed, so the tenant tile, the
 * expand control and the compact Sign out are all on it). Each `.sk-ri` is
 * its padding, a 20px icon (22px for Profile's avatar), its gap and one
 * 12px x 1.45 label line. The bell is 32px (notifications.css draws a 32px
 * control) and the compact Sign out 30px (6px padding, 16px icon, 1px border).
 */
function spineHeight(env: CascadeEnv): number {
  const spine = build('div.sk-app.is-collapsed nav.sk-spine', hosts)
  const sib = (sel: string) => {
    const el = document.createElement(sel.split('.')[0] || 'span')
    el.className = sel.split('.').slice(1).join(' ')
    spine.appendChild(el)
    return el
  }
  const hive = sib('a.sk-hive')
  const hiveSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  hive.appendChild(hiveSvg)
  const tile = sib('button.sk-ttile')
  const ri = sib('a.sk-ri')
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  icon.setAttribute('class', 'sk-ic')
  ri.appendChild(icon)
  const rsep = sib('span.sk-rsep')
  const expand = sib('button.sk-ibtn.sk-expand')
  const out = sib('button.sk-out')

  const label = 12 * 1.45
  const item = (iconH: number) => block(ri, 'padding', env) + iconH + gapOf(ri, env) + label
  const hiveH = block(hive, 'padding', env) + (px(hiveSvg, 'height', env) || 44)
  const items = 8 * item(px(icon, 'height', env)) + item(22)
  const children = 16
  return (
    block(spine, 'padding', env) +
    hiveH +
    px(tile, 'height', env) + block(tile, 'margin', env) +
    items +
    1 + block(rsep, 'margin', env) +
    px(expand, 'height', env) + block(expand, 'margin', env) +
    32 +
    30 + block(out, 'margin', env) +
    (children - 1) * gapOf(spine, env)
  )
}

describe('BT-016: the console frame fits a short screen', () => {
  it('the collapsed spine fits 1024x625 with Help and Profile on screen', () => {
    for (const theme of THEMES) {
      const env: CascadeEnv = { width: 1024, height: 625, theme }
      const h = spineHeight(env)
      // MUTATION: drop the `max-height: 800px` spine block (styles.css) and
      // the sum is the walk's ~780px against a 625px window.
      expect(h, `spine ${h.toFixed(1)}px at 1024x625 ${theme}`).toBeLessThanOrEqual(625)
    }
    // A tall window keeps the spine as it was drawn: the block is for short screens only.
    expect(spineHeight({ width: 1440, height: 900 })).toBeGreaterThan(700)
  })

  it('every spine item keeps a 24px target when packed', () => {
    const ri = build('nav.sk-spine a.sk-ri', hosts)
    const env: CascadeEnv = { width: 1024, height: 625 }
    expect(block(ri, 'padding', env) + 20 + 12 * 1.45).toBeGreaterThanOrEqual(24)
  })

  it('the Work panel gives back its 12px at 1440x757', () => {
    const { container } = render(
      <Spine.SkyShell section="work" tab="running" agentTab="live" title="Agents" go={vi.fn()} foot={null}>
        {null}
      </Spine.SkyShell>,
    )
    const rows = container.querySelectorAll('.sk-panel a.sk-pk, .sk-panel button.sk-pk').length
    expect(rows, 'the Work panel drew no rows').toBeGreaterThan(2)
    const row = container.querySelector('.sk-panel .sk-pk')!
    const panel = container.querySelector('nav.sk-panel')!
    for (const theme of THEMES) {
      const tall: CascadeEnv = { width: 1440, height: 900, theme }
      const short: CascadeEnv = { width: 1440, height: 757, theme }
      const saved = rows * (block(row, 'padding', tall) - block(row, 'padding', short)) + (block(panel, 'padding', tall) - block(panel, 'padding', short))
      // MUTATION: drop the panel half of the short-screen block and nothing
      // is given back: the panel's page scroller 435 of 447px, Timeline under the meter.
      expect(saved, `${theme}: ${rows} rows`).toBeGreaterThanOrEqual(12)
    }
  })

  it('at 390 the page title keeps a floor beside the controls (guard: VQ-001, PHONE-BAR-LABEL)', () => {
    for (const theme of THEMES) {
      const env: CascadeEnv = { width: 390, height: 844, theme }
      const plain = build('div.sk-app header.sk-pbar b', hosts)
      const chip = build('div.sk-app header.sk-pbar.has-chip b', hosts)
      // The walk's `header.sk-pbar > b 17 of 46 px` ('A..') was a title with no floor.
      expect(painted(plain, 'min-width', env)).toBe('5.5em')
      expect(painted(chip, 'min-width', env)).toBe('min-content')
      expect(painted(plain, 'white-space', env)).toBe('normal')
    }
  })
})

// ---------------------------------------------------------------------------
// BT-017
// ---------------------------------------------------------------------------

const NOW = Date.parse('2026-09-22T11:00:00.000Z')

function row(t: Task): HTMLElement {
  const { container } = render(<TaskRow task={t} now={NOW} onOpen={() => {}} classes={null} />)
  return container.querySelector<HTMLElement>('.row.is-compact')!
}

describe('BT-017: the agent list beside the Browser tab says what it cuts', () => {
  it('a row’s sub-line carries the whole line, the owner’s full name included, as its title', () => {
    const owner = 'bartholomew.long-surname@acme.test'
    const r = row(
      runTask({
        id: 'task_bt017live',
        state: 'RUNNING',
        runner_profile: 'claude-code-browser',
        model: 'claude-opus-5-5',
        submitted_by: owner,
        started_at: '2026-09-22T10:30:00.000Z',
      }),
    )
    const sub = r.querySelector<HTMLElement>('span.cr-sub')!
    // MUTATION: drop `title={subSaid}` from `.cr-sub` (Agents.tsx) and the
    // cut owner has no hover: the walk's `span.cr-sub … no title`.
    const title = sub.getAttribute('title')
    expect(title, 'span.cr-sub has no title').not.toBeNull()
    expect(title).toContain('claude-code-browser · claude-opus-5-5')
    expect(title).toContain(owner)
    // The row's state in words, then the try as the line prints it
    // (`attempt … of 3` until the attempts are read, QA-026); every live row
    // says its state since visual QA pass 1, overview-agents-11.
    expect(title).toMatch(/^running · attempt \S+ of 3 · /)
  })

  it('a clamped failure reason ends in an ellipsis, at every window and theme', () => {
    const r = row(
      runTask({
        id: 'task_bt017fail',
        state: 'FAILED',
        submitted_by: 'ada@acme.test',
        started_at: '2026-09-22T10:00:00.000Z',
        completed_at: '2026-09-22T10:20:00.000Z',
        last_error: 'GATE_NOT_YET, not ready: '.repeat(12),
      }),
    )
    const why = r.querySelector<HTMLElement>('.why')!
    expect(why.className).toMatch(/\bis-bad\b/)
    expect(why.getAttribute('title')).not.toBeNull()
    for (const env of ENVS) {
      const at = `at ${env.width}x${env.height} ${env.theme}`
      expect(painted(why, '-webkit-line-clamp', env), at).toBe('2')
      // MUTATION: drop `text-overflow: ellipsis` from the clamp (agents.css)
      // and the walk's `span.why.is-warn.is-bad 42 of 63 px, cut` is back.
      expect(painted(why, 'text-overflow', env), at).toBe('ellipsis')
    }
  })

  it('an agent open over the list at 1024 draws a scrim behind it, and none where it sits beside the list', async () => {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: { tasks: [runTask({ id: 'task_bt017open', state: 'RUNNING', started_at: '2026-09-22T10:30:00.000Z' })], tenant_id: 'acme' },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    const { container } = render(<AgentsScreen onOpen={() => {}} taskId="task_bt017open" />)
    await waitFor(() => expect(container.querySelector('.row.is-compact')).not.toBeNull(), WAIT)
    // MUTATION: drop the scrim from AgentsScreen (Agents.tsx) and nothing
    // separates the overlaid agent from the list it cuts mid-word.
    const scrim = container.querySelector<HTMLElement>('.ag-scrim')
    expect(scrim, 'no scrim with an agent open').not.toBeNull()
    expect(scrim!.getAttribute('aria-hidden')).toBe('true')
    for (const theme of THEMES) {
      const over: CascadeEnv = { width: 1024, height: 625, theme }
      expect(painted(scrim!, 'display', over), `1024 ${theme}`).toBe('block')
      expect(painted(scrim!, 'position', over)).toBe('fixed')
      const fill = resolveColour(painted(scrim!, 'background', over) ?? 'transparent', theme)
      expect(fill.a, `1024 ${theme}: the scrim is see-through`).toBeGreaterThan(0.2)
      // Under the agent (`.drawer`, z-index 40) and the spine (`.sk-side`, 10).
      expect(Number(painted(scrim!, 'z-index', over))).toBeLessThan(10)
      // Beside the list (1100 up) and full-screen on a phone, there is nothing to cover.
      expect(painted(scrim!, 'display', { width: 1440, height: 757, theme }), `1440 ${theme}`).toBe('none')
      expect(painted(scrim!, 'display', { width: 390, height: 844, theme }), `390 ${theme}`).toBe('none')
    }
  })

  it('no agent open, no scrim', async () => {
    api.loadTasks.mockResolvedValue({
      status: 'ok',
      data: { tasks: [runTask({ id: 'task_bt017none', state: 'RUNNING' })], tenant_id: 'acme' },
      fetchedAt: Date.now(),
    } satisfies Result<TaskPage>)
    const { container } = render(<AgentsScreen onOpen={() => {}} />)
    await waitFor(() => expect(container.querySelector('.row.is-compact')).not.toBeNull(), WAIT)
    expect(container.querySelector('.ag-scrim')).toBeNull()
  })
})
