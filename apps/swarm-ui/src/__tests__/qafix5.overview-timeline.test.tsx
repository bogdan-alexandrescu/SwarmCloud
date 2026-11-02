/**
 * VISUAL QA RUN 5b, LANE QAFIX5-OVERVIEW-TIMELINE (part of #1680; run
 * wf_025ae735511546d99eff, the fixture build of main at 58cd0c1e, 1440 and
 * 390 px, light and dark).
 *
 *   5b:wg-dark-D1 (#1680 comment 6343766999) Overview's failed-agents card's
 *       prominent link, 'Agents · FAILED →', opened the Agents list, and the
 *       only way to the failed workflow was the bare id inside the card's
 *       sentence. The card now carries a 'Workflow <id> →' link beside the
 *       list's, drawn alike, and each link's accessible name starts with
 *       where it goes.
 *   5b:QA-014 (#1680 comment 6343760837) Timeline said 'Decided counts cover
 *       Sep 12, 00:00 → now' over a ledger read generated on Sep 25, with the
 *       clock at Oct 19. 'now' was the read's own `generated_at`, never
 *       checked against the reader's clock. The fixture's canned date is why
 *       the two were apart; the words are why the page claimed a span it did
 *       not read. A read older than `LEDGER_NOW_MS` ends at its instant.
 *   r3:work-a-005 (#1680 comment 6314122402; 5b:QA-015) A bar still running
 *       at 'now' was laid flush against the chart's right edge, with no right
 *       border and a fill fading to transparent, so it read as cut off; a
 *       merged pill whose newest attempt runs (a073aff5's '3') read as half
 *       clipped. Bars and end marks now stop `TRACK_END_PX` short of the
 *       edge, and a running bar keeps its rounded end, dashed, on a fill that
 *       never fades out. Owner pick c00 (one counted pill) is kept.
 *
 * Each assertion names the mutation that turns it red.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowRead } from '../api'
import { deriveChecks, type CheckInputs, type Problem } from '../checks'
import { ledgerFixture } from '../outcomes.fixture'
import { instantLabel, ledgerRange } from '../outcomes'
import type { AttemptsPage, Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task } from './runfixture'
import { byIdVia } from './workflowsById'

const api = vi.hoisted(() => ({
  loadAttemptsPage: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadTasks: vi.fn(),
  loadTask: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowsById: vi.fn(),
  loadOutcomes: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { CheckCard, toPath } = await import('../Overview')
const { TimelineLanesScreen, laneLayout, PILL_COUNT_MIN_PX, TRACK_END_PX } = await import('../TimelineLanes')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const MIN = 60_000
const DAY = 24 * 60 * MIN

const ok = <T,>(data: T): Result<T> => ({ status: 'ok', data, fetchedAt: Date.now() })
const text = (el: Element | null | undefined): string => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

afterEach(() => {
  cleanup()
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------
// 5b:wg-dark-D1: the failed-agents card's links
// ---------------------------------------------------------------------------

const WF_A = 'wf_5e5ad3b6f7da4299a839'
const WF_B = 'wf_0b1c2d3e4f5a6b7c8d9e'
const FAILED_LIST = toPath('#work/running/recent/failed')

function failuresProblem(tasks: Task[]): Problem {
  const now = Date.now()
  const empty = { status: 'empty', fetchedAt: now } as const
  const c = {
    capacity: empty,
    tasks: ok({ tasks, next_page_token: null, tenant_id: 'eng' } as TaskPage),
    leases: empty,
    providers: empty,
    accounts: empty,
    stats: empty,
    workflows: empty,
  } as unknown as CheckInputs
  const check = deriveChecks(c, now).find((x) => x.label === 'Failures')!
  expect(check.status).toBe('found')
  return check.status === 'found' ? check.problems[0]! : (undefined as never)
}

const ago = (m: number): string => new Date(Date.now() - m * MIN).toISOString()

describe('5b:wg-dark-D1: the failed-agents card reaches the failed workflow as plainly as the list', () => {
  const ONE = [
    task({ id: 'tsk_f1', state: 'FAILED', workflow_id: WF_A, completed_at: ago(2), last_error: 'exit status 1' }),
    task({ id: 'tsk_f2', state: 'FAILED', workflow_id: WF_A, completed_at: ago(4) }),
  ]

  it('draws a link named for the workflow beside the list’s link, not only the id in the sentence', () => {
    const { container } = render(<CheckCard problem={failuresProblem(ONE)} />)
    // MUTATION: leave the workflow reachable only through the id inside the ids line.
    const wf = screen.getByRole('link', { name: `Workflow ${WF_A}` })
    expect(wf.getAttribute('href')).toBe(`/workflows/${WF_A}`)
    // MUTATION: put the workflow's link back inside the sentence (the ids line).
    expect(wf.closest('.ov-att-ids')).toBeNull()
    // It sits with the list's link, in the card's links column.
    const acts = container.querySelector('.ov-att-acts')
    // MUTATION: draw the workflow link anywhere but beside 'Agents · FAILED →'.
    expect(acts).not.toBeNull()
    expect(within(acts as HTMLElement).getAllByRole('link').map((a) => a.getAttribute('href'))).toEqual([`/workflows/${WF_A}`, FAILED_LIST])
    // MUTATION: drop the arrow, so it no longer reads as a way out of the card.
    expect(text(wf)).toBe(`Workflow ${WF_A} →`)
  })

  it('names each link by where it goes', () => {
    const p = failuresProblem(ONE)
    render(<CheckCard problem={p} />)
    // MUTATION: name the list's link by the card's headline again ('2 failed agents…').
    const list = screen.getByRole('link', { name: /^Agents · failed\b/ })
    expect(list.getAttribute('href')).toBe(FAILED_LIST)
    // The card's words stay in the list link's name, after its destination.
    // MUTATION: drop the headline from the list link's name.
    expect(list.getAttribute('aria-label')).toBe(`Agents · failed: ${p.headline}. ${p.detail}`)
    // No link on the card is named by the headline alone any more.
    // MUTATION: keep `aria-label={headline. detail}` on the list link.
    expect(screen.queryAllByRole('link', { name: /^2 failed agents/ })).toHaveLength(0)
  })

  it('draws one workflow link per workflow the headline names, in its order', () => {
    const TWO = [...ONE, task({ id: 'tsk_f3', state: 'FAILED', workflow_id: WF_B, completed_at: ago(6) })]
    const { container } = render(<CheckCard problem={failuresProblem(TWO)} />)
    const acts = container.querySelector('.ov-att-acts')!
    // MUTATION: link only the first workflow from the links column.
    expect(within(acts as HTMLElement).getAllByRole('link').map((a) => text(a))).toEqual([
      `Workflow ${WF_A} →`,
      `Workflow ${WF_B} →`,
      'Agents · failed →',
    ])
  })

  it('paints the workflow link as the list’s link is painted, and lets it cover the card', () => {
    const { container } = render(<CheckCard problem={failuresProblem(ONE)} />)
    const wf = container.querySelector(`.ov-att-acts a[href="/workflows/${WF_A}"]`)!
    const list = container.querySelector(`.ov-att-acts a[href="${FAILED_LIST}"]`)!
    // Drawn by the list link's own rule (`.ov-att-go`: its accent, its size,
    // its arrow). The resolver cannot evaluate `color` over the whole sheet,
    // so the shared class is what holds 'alike' here.
    // MUTATION: draw the workflow link with a class of its own only, in the sentence's dim ink.
    expect(wf.classList.contains('ov-att-go')).toBe(true)
    expect(painted(wf, 'white-space', WIDE)).toBe(painted(list, 'white-space', WIDE))
    // THE WORKFLOW'S LINK COVERS THE CARD (BGD's decision of 2026-10-23,
    // #1680 box 6343766999): a click on the card's empty space opens the
    // failed workflow. It reversed "only the list's link covers the card".
    // MUTATION: leave the cover on the list's link, so a click on the card opens the list.
    expect(painted(wf, 'content', WIDE, 'after')).not.toBeNull()
    expect(painted(wf, 'position', WIDE, 'after')).toBe('absolute')
    // MUTATION: keep `position: relative` on the cover's link, so its cover covers only itself.
    expect(painted(wf, 'position', WIDE)).not.toBe('relative')
    // The list's link is a plain link above the cover.
    // MUTATION: let the cover rule reach the list's link too.
    expect(painted(list, 'content', WIDE, 'after') ?? 'none').toBe('none')
    // MUTATION: drop `position: relative; z-index: 1` from it, so the workflow's cover swallows its clicks.
    expect(painted(list, 'position', WIDE)).toBe('relative')
    expect(painted(list, 'z-index', WIDE)).toBe('1')
    // MUTATION: let the column shrink, so the links wrap under each other's words.
    expect(painted(acts(container), 'flex-shrink', WIDE)).toBe('0')
    // ON A PHONE THE CARD IS ONE LINE (visual QA VQ-002, 2026-10-26): the
    // cover's link stays, its words stepped aside visually and its arrow at
    // the right edge, so a tap anywhere still opens the failed workflow; the
    // other links are the desktop's.
    // MUTATION: hide the cover's link on a phone, so a tap on the card opens nothing.
    expect(painted(wf, 'display', PHONE)).not.toBe('none')
    expect(painted(wf, 'content', PHONE, 'after')).not.toBeNull()
    // MUTATION: drop `display: none` from the split card's list link in the 560px block.
    expect(painted(list, 'display', PHONE)).toBe('none')
    expect(painted(acts(container), 'display', PHONE)).not.toBe('none')
  })
})

function acts(container: HTMLElement): Element {
  return container.querySelector('.ov-att-acts')!
}

// ---------------------------------------------------------------------------
// 5b:QA-014: 'now' only when the read is now
// ---------------------------------------------------------------------------

describe('5b:QA-014: the decided counts’ range says “now” only for a read that is', () => {
  it('ends an old read at its own instant, and a fresh one at now', () => {
    const at = Date.parse('2026-10-19T12:00:00Z')
    const d = ledgerFixture()
    // The fixture's read was generated on Sep 25 and runs to its generation.
    expect(Math.abs(Date.parse(d.until) - Date.parse(d.generated_at))).toBeLessThan(2_000)
    // MUTATION: print 'now' whenever `until` equals `generated_at`, as before.
    expect(ledgerRange(d, at)).toBe(`${instantLabel(d.since, d.tz)} → ${instantLabel(d.until, d.tz)}`)
    // Read a minute ago, it is still now.
    // MUTATION: never print 'now'.
    expect(ledgerRange(d, Date.parse(d.generated_at) + MIN)).toBe(`${instantLabel(d.since, d.tz)} → now`)
  })

  describe('drawn on Timeline', () => {
    beforeEach(() => {
      api.loadWorkflowsById.mockImplementation(byIdVia(api.loadWorkflow))
      api.loadAttemptsPage.mockResolvedValue(
        ok({ tenant_id: 'eng', read_at: new Date().toISOString(), next_page_token: null, coverage: { scope: 'page' }, attempts: [] } as unknown as AttemptsPage),
      )
      api.loadTaskEventsPage.mockResolvedValue(ok({ events: [], next_page_token: null }))
      api.loadTasks.mockResolvedValue(ok({ tasks: [] } as unknown as TaskPage))
      api.loadTask.mockResolvedValue({ status: 'error', error: { kind: 'server', httpStatus: 404, code: null, message: 'gone' } })
      api.loadWorkflow.mockResolvedValue(ok({ workflow: { workflow_id: 'wf_none', state: 'RUNNING', steps: [] }, tasks: [] } as unknown as WorkflowRead))
      api.loadOutcomes.mockResolvedValue(ok(ledgerFixture()))
    })
    afterEach(() => {
      vi.clearAllMocks()
    })

    it('prints the canned Sep 25 read’s end, not now, in the summary', async () => {
      const d = ledgerFixture()
      // Lanes zoomed to the read's own window: since QA2-FIX-05 (runs2-04) a
      // read over another window than the lanes' prints no range of its own.
      const q = new URLSearchParams({ since: new Date(d.since).toISOString(), until: new Date(d.until).toISOString() }).toString()
      render(<TimelineLanesScreen view={q} onView={() => {}} />)
      const range = await waitFor(() => {
        const el = document.querySelector('.tl-strip-range')
        expect(el).not.toBeNull()
        return el!
      })
      // MUTATION: call `ledgerRange` with the read's own clock rather than the page's.
      expect(text(range)).toBe(`${instantLabel(d.since, d.tz)} → ${instantLabel(d.until, d.tz)}`)
      expect(text(range)).not.toMatch(/now/)
    })
  })
})

// ---------------------------------------------------------------------------
// r3:work-a-005: a bar running at 'now' is not cut at the chart's edge
// ---------------------------------------------------------------------------

describe('r3:work-a-005: nothing running at now is cut at the chart’s right edge', () => {
  it('stops a running bar and a running merged pill short of the track’s edge, at 1440 and 390', () => {
    for (const trackPx of [1100, 358]) {
      const at = (msBeforeNow: number) => 100 - (msBeforeNow / DAY) * 100
      const placed = laneLayout(
        [
          { from: at(30 * MIN), to: at(14 * MIN) },
          // a073aff5's shape: three attempts merged, the newest running to now.
          { from: at(9 * MIN), to: 100, min: PILL_COUNT_MIN_PX },
        ],
        [],
        trackPx,
      )
      const pill = placed.bars[1]!
      const right = ((pill.left + pill.width) / 100) * trackPx
      // MUTATION: drop TRACK_END_PX from laneLayout's starting edge, so the pill is flush with the edge.
      expect(right, `${trackPx}`).toBeLessThanOrEqual(trackPx - TRACK_END_PX + 1e-9)
      // MUTATION: set TRACK_END_PX to 0.
      expect(TRACK_END_PX).toBeGreaterThanOrEqual(3)
      // Still laid out at its counted floor, so its '3' has room.
      // MUTATION: lay the pill out at a lone pill's floor.
      expect((pill.width / 100) * trackPx, `${trackPx}`).toBeGreaterThanOrEqual(PILL_COUNT_MIN_PX - 1e-9)
    }
  })

  it('stops an end mark short of the edge too', () => {
    const trackPx = 1100
    const placed = laneLayout([], [{ pct: 100, end: true }], trackPx)
    // An end mark is 18 px wide and drawn from 2 px after its left.
    // MUTATION: start the end marks' edge at the track's width again.
    expect((placed.marks[0]! / 100) * trackPx + 2 + 18).toBeLessThanOrEqual(trackPx - TRACK_END_PX + 1e-9)
  })

  it('draws a running bar with its end, never a fill fading into the edge', () => {
    const host = document.createElement('div')
    host.className = 'tl-trk'
    const bar = document.createElement('span')
    bar.className = 'tl-seg is-hold is-open'
    host.appendChild(bar)
    document.body.appendChild(host)
    // MUTATION: put back `border-right: 0` on `.tl-seg.is-hold.is-open`.
    expect(painted(bar, ['border-right', 'border-right-style'], WIDE)).toMatch(/dashed/)
    // MUTATION: put back the square right end, `border-radius: 6px 0 0 6px`.
    expect(painted(bar, ['border-radius', 'border-top-right-radius'], WIDE)).toBe('6px')
    // MUTATION: put back the fill's fade to `transparent`.
    const fill = painted(bar, ['background', 'background-image'], WIDE) ?? ''
    expect(fill).not.toMatch(/transparent/)
    // The merged pill takes the same end: it is the newest attempt's style.
    const pill = document.createElement('button')
    pill.className = 'tl-seg is-hold is-merged is-open'
    host.appendChild(pill)
    // MUTATION: give the merged pill a rule of its own that squares its end again.
    expect(painted(pill, ['border-radius', 'border-top-right-radius'], WIDE)).toBe('6px')
    expect(painted(pill, ['border-right', 'border-right-style'], WIDE)).toMatch(/dashed/)
  })
})
