/**
 * QAFIX4-RUNS, THE RUNS LIST (part of #1680, visual QA run 4,
 * wf_ecd935541ced4a539540, the fixture build of main at fabe4d6c, 2026-10-22):
 *
 *   N20 (run-4 work-a-009)  at 1440 the Issue column cut
 *           'example-org/agent-swarm-infra#512' to 'example-org/agent-swarm-inf…#512'
 *           on both rows, in both themes. The columns were sized to fill a
 *           1056px card with no pixel over, and the card is 1041px since the
 *           scroller holds its 15px scrollbar gutter on every tab (styles.css
 *           `.ctl-scroll`, visual QA run 3): Issue -- what is left -- lost
 *           exactly those 15px.
 *   box comment 6298216578 (run-2 work-a-011, still in run 4)  at 390 the
 *           state filter's 3×2 grid notched every inner join: each cell kept
 *           the app's button radius (`:where(.app button)`), so its hairlines
 *           curved away at their ends and it read as six loose buttons.
 *
 * Every assertion names the mutation that turns it red.
 */
import { render, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { CascadeEnv } from './cssgate'
import { build, painted, TABLES } from './marks'
import { resolveVars } from './spaceprobe'
import { cellStyle, fixedColumns, lengthPx, textPx } from './tablefit'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
/** A classic (non-overlay) scrollbar's width in desktop Chrome, which the QA runs drive. */
const SCROLLBAR_PX = 15

const OWNER = 'example-org'
const REPO = 'agent-swarm-infra'
// 34 characters: the longest reference D11 sized the column for.
const REF_34 = 'bogdan-alexandrescu/SwarmCloud#454'
const RUN_ID = 'run_' + 'b2'.repeat(10)
const WF = 'wf_' + 'a1'.repeat(10)

function run(id: string, over: Record<string, unknown> = {}) {
  return {
    id, tenant_id: 'eng', state: 'RUNNING', terminal: false,
    issue: { ref: `${OWNER}/${REPO}#512`, owner: OWNER, repo: REPO, number: 512, url: `https://github.com/${OWNER}/${REPO}/issues/512` },
    plan_approval: 'required', workflow_id: WF, created_by: 'operator@example.com',
    created_at: '2026-10-22T03:00:00Z', updated_at: '2026-10-22T03:10:00Z',
    issue_read: { title: 'Fix the list', state: 'open' },
    ...over,
  }
}

const hosts: HTMLElement[] = []

/** The list, mounted inside `.app` as main.tsx mounts it, so the app's button floor applies. */
async function mountList(runs: unknown[]) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/v1/runs?view=summary') {
      return new Response(JSON.stringify({ runs, next_page_token: null, tenant_id: 'eng' }), { status: 200, headers: JSON_HEADERS })
    }
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const app = document.createElement('div')
  app.className = 'app'
  document.body.appendChild(app)
  hosts.push(app)
  const utils = render(<RunsScreen view={null} go={vi.fn()} />, { container: app })
  await waitFor(() => expect(utils.container.querySelector('.rn-row')).not.toBeNull(), WAIT)
  return utils
}

afterEach(() => {
  vi.unstubAllEnvs()
  for (const h of hosts.splice(0)) h.remove()
})

/** px of one length the cascade gives, through the token table. */
const px = (v: string | null) => lengthPx(v, 0)

/**
 * The width the list is laid out in at 1440, read off the shipped frame: the
 * window less the spine, the panel, the page's gutter on both sides and the
 * scroller's held scrollbar gutter.
 */
function listWidthAt1440(): number {
  const spine = px(painted(build('.sk-spine', hosts), 'width', WIDE))
  const panel = px(painted(build('.sk-panel', hosts), 'width', WIDE))
  const gutter = px(painted(document.documentElement, '--ctl-gutter', WIDE))
  expect(spine, '.sk-spine has no width').not.toBeNull()
  expect(panel, '.sk-panel has no width').not.toBeNull()
  expect(gutter, '--ctl-gutter is not a length at 1440').not.toBeNull()
  const held = painted(build('.ctl-scroll', hosts), 'scrollbar-gutter', WIDE) === 'stable' ? SCROLLBAR_PX : 0
  return 1440 - spine! - panel! - 2 * gutter! - held
}

describe('N20 (run-4 work-a-009): at 1440 the Issue column holds a 34-character reference whole', () => {
  it('measures the list at 1041px, the 1056px card less the held scrollbar gutter', () => {
    // MUTATION: drop `scrollbar-gutter: stable` from `.ctl-scroll` and this
    // is 1056 -- the card the columns were first sized for.
    expect(listWidthAt1440()).toBe(1041)
  })

  it('fits the reference in Issue, with Run, State, Workflow and By still holding what they hold', async () => {
    const { container } = await mountList([
      run(RUN_ID, { issue: { ref: REF_34, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454, url: null } }),
    ])
    const table = container.querySelector<HTMLTableElement>('.rn-table')!
    const { width, cols } = fixedColumns(table, listWidthAt1440(), WIDE)
    // MUTATION: give the sized columns more than the list has and the fixed
    // table runs past its card, behind a sideways scroll.
    expect(cols[cols.length - 1]!.end, 'the table is wider than the list').toBeLessThanOrEqual(width)
    const row = container.querySelector('.rn-row')!
    const room = (col: string, label: string) => {
      const box = cols.find((c) => c.col === col)!
      const st = cellStyle(row.querySelector(`td[data-label="${label}"]`)!, WIDE)
      return box.end - box.start - st.pl - st.pr
    }
    const ref = row.querySelector('td[data-label="Issue"] .rn-refcut')!
    expect(ref.textContent).toBe(REF_34)
    // MUTATION: put the cells' 8px a side back (`.rn-row > td { padding: 4px 8px }`),
    // or the six sized columns' widths, and Issue is 15px short again:
    // 'bogdan-alexandrescu/SwarmClo…#454', as run 4 saw its reference.
    expect(room('issue', 'Issue'), 'the issue reference is cut').toBeGreaterThanOrEqual(textPx(REF_34, ref, WIDE))
    // MUTATION: find Issue's room by narrowing Run, Workflow or By below its
    // id or address (D11, R11, QA-081) and one of these is red.
    expect(room('run', 'Run'), 'the run id is cut').toBeGreaterThanOrEqual(textPx(RUN_ID, row.querySelector('td[data-label="Run"] a')!, WIDE))
    expect(room('workflow', 'Workflow'), 'the workflow id is cut').toBeGreaterThanOrEqual(textPx(WF, row.querySelector('td[data-label="Workflow"] .rn-cut')!, WIDE))
    const by = row.querySelector('td[data-label="By"] .rn-cut')!
    expect(by.textContent).toBe('operator@example.com')
    expect(room('by', 'By'), 'the address is cut').toBeGreaterThanOrEqual(textPx('operator@example.com', by, WIDE))
    // The State pill's longest word, its glyph, gap and padding (components.css `.c-pill`).
    // MUTATION: take Issue's room from State below the pill and this is red.
    const pill = row.querySelector<HTMLElement>('td[data-label="State"] .c-pill')!
    const pad = (painted(pill, 'padding', WIDE) ?? '').split(/\s+/).map((p) => px(p) ?? 0)
    const glyph = px(painted(pill.querySelector('svg')!, 'width', WIDE)) ?? 0
    const gap = px(painted(pill, 'gap', WIDE)) ?? 0
    const pillPx = (pad[1] ?? 0) + (pad[3] ?? pad[1] ?? 0) + glyph + gap + textPx('Cancelled', pill, WIDE)
    expect(room('state', 'State'), 'the state pill is cut').toBeGreaterThanOrEqual(pillPx)
  })

  it('keeps the head cells on the body cells\' padding, so a header starts over its column\'s values', async () => {
    const { container } = await mountList([run(RUN_ID)])
    const th = container.querySelector('.rn-table th[data-col="issue"]')!
    const td = container.querySelector('.rn-row > td[data-label="Issue"]')!
    // MUTATION: narrow the cells' padding and not the head's (or the other
    // way round) and the headers sit 2px off their values.
    expect(cellStyle(th, WIDE).pl).toBe(cellStyle(td, WIDE).pl)
    expect(cellStyle(th, WIDE).pr).toBe(cellStyle(td, WIDE).pr)
  })
})

describe('box comment 6298216578 (run-2 work-a-011): at 390 the state filter is one segmented control', () => {
  it('squares every inner corner, keeps the control\'s outer radius and draws each join once', async () => {
    const { container } = await mountList([run('run_' + 'c9'.repeat(10))])
    const seg = within(container).getByRole('group', { name: 'Which runs' })
    const cells = [...seg.querySelectorAll<HTMLElement>(':scope > button')]
    expect(cells).toHaveLength(6)
    const CORNERS = ['top-left', 'top-right', 'bottom-right', 'bottom-left'] as const
    const corner = (b: HTMLElement, c: (typeof CORNERS)[number]) => painted(b, ['border-radius', `border-${c}-radius`], PHONE)
    cells.forEach((b, i) => {
      for (const c of CORNERS) {
        // A cell's corner is square: an inner one meets a neighbour, and an
        // outer one is drawn by the control, which clips the cell to it.
        // MUTATION: drop `.rn-filters > .c-seg > button { border-radius: 0 }`
        // and every cell takes `:where(.app button)`'s radius: the hairlines
        // curve off at their ends and each join is notched.
        expect(px(corner(b, c)), `${b.textContent} ${c}`).toBe(0)
      }
      // No two neighbours draw two lines: a cell draws its right and top
      // joins only, never a left or bottom one the next cell also draws.
      // MUTATION: give a cell its left or bottom hairline and that join is
      // two lines, 2px where the rest are 1.
      const left = painted(b, ['border', 'border-left', 'border-left-width'], PHONE)
      const bottom = painted(b, ['border', 'border-bottom', 'border-bottom-width'], PHONE)
      expect(left, `${b.textContent} left`).toBe('0')
      expect(bottom, `${b.textContent} bottom`).toBe('0')
      // MUTATION: drop the `:nth-child(3n)` rule and a row's last cell
      // doubles the control's right border.
      const right = painted(b, ['border', 'border-right', 'border-right-width'], PHONE)
      if (i % 3 === 2) expect(right, `${b.textContent} right`).toBe('0')
      else expect(right, `${b.textContent} right`).toMatch(/^1px solid/)
      // MUTATION: keep the shared phone `border-top` on the first row and
      // it doubles the control's top border.
      const top = painted(b, ['border', 'border-top', 'border-top-width'], PHONE)
      if (i < 3) expect(top, `${b.textContent} top`).toBe('0')
      else expect(top, `${b.textContent} top`).toMatch(/^1px solid/)
    })
    // The outer corners are the control's own, rounded, and the square
    // corner cells are clipped to them.
    // MUTATION: square the control too (`border-radius: 0` on `.rn-filters > .c-seg`)
    // and the filter loses the rounded outline every segmented control has.
    const outer = painted(seg, ['border-radius'], PHONE)
    expect(outer).toBe(painted(build('.c-seg', hosts), 'border-radius', WIDE))
    expect(Number.parseFloat(resolveVars(outer ?? '0', TABLES.dark)), `the control's radius is ${outer}`).toBeGreaterThan(0)
    // MUTATION: let the control overflow and the square corner cells' fill
    // pokes past its rounded corners.
    expect(painted(seg, 'overflow', PHONE)).toBe('hidden')
    // One outline around the whole control.
    expect(painted(seg, ['border'], PHONE)).toMatch(/^1px solid/)
  })
})
