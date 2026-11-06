// VISUAL QA RUN 6 LANE QAFIX6-WORKPAGES (part of #1680): RUNS, REPOSITORIES
// AND ACCESS.
//
//   a (6:work-a-010)  Runs' Plan approval cell printed `plan_approval` as
//        served, so run_4c1e09d2 (Planned, no workflow) and run_19ab77e0
//        (Running, workflow wf_8b21d0e4) both read "required". It says
//        whether the approval is still pending or was given.
//        QAFIX6-WORKPAGES-2: in words that fit the 86px column at the body
//        size on one line -- 'awaiting' / 'approved', and a dash titled as
//        automatic where none is needed ('not required' and 'none needed'
//        are both wider than the column's 74px of content at 13px).
//   b (6:work-a-009, box 6343779974)  At 1440 the third Repositories card's
//        long name pushed its 'current' badge onto a line of its own, and the
//        cards placed Schedule differently. The badge has one slot, the row
//        under the name, and Schedule is a line of its own on every card.
//   c (6:work-b-003, box 6343780777)  At 390 Access's head put Refresh alone,
//        right-aligned, on a row between the title and the switch. The head's
//        actions wrap as one group under the title from the left edge, with
//        Refresh on the switch's row, as Setup's head does.
//
// jsdom lays nothing out, so c reads what the real sheets resolve to at 390
// (`painted`, every sheet in the bundle's order, phone rules applied).
//
// Fixtures only; no network. Nothing here is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { HOUR, ago, coverageOf, repo, serve, sha, visible } from './repofixture'
import { issueRun } from './runfixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { lengthPx, nowrap, paddingX, textPx } from './tablefit'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', window.location.pathname)
})

describe('a: Runs says whether a plan approval is pending or given (6:work-a-010)', () => {
  // The two runs of the fixture build (api.ts `fixtureIssueRuns`), and one
  // whose approval is automatic.
  const RUNS = [
    issueRun({ id: 'run_4c1e09d2' }),
    issueRun({
      id: 'run_19ab77e0', state: 'RUNNING', workflow_id: 'wf_8b21d0e4',
      approved_by: 'operator@example.com', approved_at: ago(30 * 60_000),
    }),
    issueRun({ id: 'run_auto0001', plan_approval: 'auto' }),
  ]

  it("the Planned run with no workflow reads 'awaiting', the Running one 'approved', an automatic one 'automatic', titled as such", async () => {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/runs')
      ? { status: 200, body: { runs: RUNS, next_page_token: null, tenant_id: 'eng' } }
      : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    const cells = await waitFor(() => {
      const c = [...container.querySelectorAll<HTMLElement>('.rn-row > td[data-label="Plan approval"]')]
      expect(c).toHaveLength(3)
      return c
    }, WAIT)
    // MUTATION: print `{r.plan_approval}` in the cell again (both required runs
    // read "required"), or put back 'awaiting approval' / 'not required'
    // (QAFIX6-WORKPAGES-2: neither fits the column at the body size).
    // QA1-FIX-24 (chrome-C13): an automatic approval is a word, not a dash.
    expect(cells.map(visible)).toEqual(['awaiting', 'approved', 'automatic'])
    // MUTATION: drop the cell's `title` and the dash says nothing about why.
    expect(cells[2]!.getAttribute('title')).toBe("This run's plan is approved automatically.")
    expect(cells[0]!.getAttribute('title')).toBe("This run's plan needs a person's approval.")
  })

  it("the fixture build's own two runs read 'awaiting' and 'approved'", async () => {
    vi.stubEnv('VITE_LIVE', '')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    const cells = await waitFor(() => {
      const c = [...container.querySelectorAll<HTMLElement>('.rn-row > td[data-label="Plan approval"]')]
      expect(c).toHaveLength(2)
      return c
    }, WAIT)
    // MUTATION: print `{r.plan_approval}` in the cell again (both fixture runs
    // read "required"), or put back 'awaiting approval'.
    expect(cells.map(visible)).toEqual(['awaiting', 'approved'])
  })

  it("a run that ended before its plan was approved reads 'not given'", async () => {
    const { planApprovalWord } = await import('../Runs')
    // MUTATION: return 'awaiting' for every unapproved run that is not Planning.
    for (const state of ['FAILED', 'CANCELLED', 'NOT_READY']) {
      expect(planApprovalWord(issueRun({ state, terminal: state !== 'NOT_READY' }) as never), state).toBe('not given')
    }
  })

  it("at 1440 the fixture's two words sit on one line at the body size and fit the column", async () => {
    vi.stubEnv('VITE_LIVE', '')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    const cells = await waitFor(() => {
      const c = [...container.querySelectorAll<HTMLElement>('.rn-row > td[data-label="Plan approval"]')]
      expect(c).toHaveLength(2)
      return c
    }, WAIT)
    const th = container.querySelector<HTMLElement>('.rn-table th[data-col="approval"]')!
    const column = lengthPx(painted(th, 'width', WIDE), 0)
    expect(column, 'the Plan approval column has no width').not.toBeNull()
    for (const cell of cells) {
      const word = visible(cell)
      // MUTATION: put back `.rn-row > td[data-label='Plan approval'] { white-space: normal;
      // font-size: var(--t-micro) }` in repositories.css and the word wraps at the micro size.
      expect(nowrap(cell, WIDE), `${word} wraps`).toBe(true)
      expect(painted(cell, 'font-size', WIDE), word).toMatch(/--t-meta|^13px$/)
      const [pl, pr] = paddingX(cell, WIDE)
      // MUTATION: put back 'awaiting approval' (17 characters, about 124px
      // conservatively at 13px) and it runs over the Workflow cell.
      expect(column! - pl - pr, `${word} is wider than its column`).toBeGreaterThanOrEqual(textPx(word, cell, WIDE))
    }
  })

  it('at 1440 every word the cell can say fits the column at the body size', async () => {
    const { planApprovalWord } = await import('../Runs')
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/runs')
      ? { status: 200, body: { runs: RUNS.slice(0, 1), next_page_token: null, tenant_id: 'eng' } }
      : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    const cell = await waitFor(() => {
      const c = container.querySelector<HTMLElement>('.rn-row > td[data-label="Plan approval"]')
      expect(c).not.toBeNull()
      return c!
    }, WAIT)
    const th = container.querySelector<HTMLElement>('.rn-table th[data-col="approval"]')!
    const [pl, pr] = paddingX(cell, WIDE)
    const room = lengthPx(painted(th, 'width', WIDE), 0)! - pl - pr
    const words = [
      issueRun({ plan_approval: 'auto' }),
      issueRun({ state: 'REJECTED', terminal: true }),
      RUNS[1]!,
      issueRun(),
      issueRun({ state: 'PLANNING', plan: null }),
      issueRun({ state: 'FAILED', terminal: true }),
    ].map((r) => planApprovalWord(r as never))
    expect(new Set(words).size, words.join(', ')).toBe(6)
    for (const word of words) {
      // MUTATION: say 'not required' (or 'none needed') for an automatic
      // approval, or 'awaiting a plan' for a Planning run, and that word is
      // wider than the column's 74px.
      expect(room, `'${word}' is wider than its column`).toBeGreaterThanOrEqual(textPx(word, cell, WIDE))
    }
  })
})

describe('b: the Repositories cards line up (6:work-a-009)', () => {
  const LONG = 'example-platform-infrastructure-terraform-modules'
  const THREE = [
    repo(),
    repo(
      { repo_id: 'repo_1111111111111111', repo: 'example-web' },
      { current_sha: sha('9f8e7d6'), head_sha: sha('5c4b3a2'), behind_by: 3, coverage: coverageOf(40, 26, 310, 2), interval_hours: 12, last_indexed_at: ago(3 * HOUR) },
    ),
    repo({ repo_id: 'repo_2222222222222222', repo: LONG }, { interval_hours: 168, on_change: 'off' }),
  ]

  /** A card's rows in order: each child's first class, a meta line by the word it opens with. */
  function rowsOf(card: Element): string[] {
    return [...card.children].map((el) => {
      const cls = el.classList[0] ?? el.tagName.toLowerCase()
      return el.classList.contains('ur-cmeta') ? `${cls}:${visible(el.firstElementChild).split(' ')[0]}` : cls
    })
  }

  it('every card has the badge in the same slot under the name and the same rows in the same order, Schedule on its own line', async () => {
    serve((m, url) => (m === 'GET' && url === '/v1/repositories' ? { status: 200, body: { repositories: THREE } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RepositoriesScreen } = await import('../Repositories')
    render(<RepositoriesScreen view={null} go={vi.fn()} />)
    const cards = await waitFor(() => {
      const c = [...document.querySelectorAll<HTMLElement>('.ur-cards > .ur-card')]
      expect(c).toHaveLength(3)
      return c
    }, WAIT)
    // The cards sort by full name (config2-09), so the long name is found by
    // its name, not by the slot it was served in.
    const long = cards.find((c) => visible(c.querySelector('h2')) === `example-org/${LONG}`)
    expect(long, 'the long-named card is drawn').toBeDefined()
    for (const card of cards) {
      const head = card.querySelector('.c-card-h')!
      // MUTATION: pass the pill back as the Card's `action` (it rides the head's row and wraps under a long name).
      expect(head.querySelector('.c-pill'), 'the badge is not in the head beside the name').toBeNull()
      const badge = card.querySelector('[data-fresh]')!
      // MUTATION: move the `.ur-cbadge` row below the Default branch line.
      expect(badge.parentElement, 'the badge sits in the row right under the name').toBe(head.nextElementSibling)
      expect(badge.parentElement!.firstElementChild, 'the badge opens its row, from the left edge').toBe(badge)
      const schedule = [...card.querySelectorAll('.ur-cmeta')].find((l) => visible(l).includes('Schedule'))!
      // MUTATION: put `{lastIndexed(r)}` back in the Schedule line.
      expect(schedule.children, 'Schedule is a line of its own').toHaveLength(1)
    }
    const first = rowsOf(cards[0]!)
    expect(first).toContain('ur-cmeta:Schedule')
    for (const card of cards.slice(1)) expect(rowsOf(card)).toEqual(first)
    // The badge's row starts from the left edge at every width.
    // MUTATION: give `.ur-cbadge` `justify-content: flex-end`.
    expect(painted(long!.querySelector('.ur-cbadge')!, ['justify-content'], WIDE) ?? 'flex-start').toMatch(/^(flex-)?start|normal$/)
  })
})

describe("c: Access's head at 390 (6:work-b-003)", () => {
  async function mountAccess() {
    serve(() => null)
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { AccessScreen } = await import('../Access')
    return render(<AccessScreen />)
  }

  // GA-MOVE-VIEWS (part of #2157): the Mine / Members / Browser logins switch
  // left the head for People and roles and Secrets, so Refresh is its one action.
  // The phone rule that gave the switch's group a row of its own
  // (`.ac-page > .c-phead > .c-acts`, repositories.css) is not asserted here:
  // with Refresh alone it is dead, and its removal is asked in GA-MOVE-VIEWS'
  // questions.json, outside that lane's territory.
  it('the head\'s actions are Refresh alone', async () => {
    await mountAccess()
    const acts = document.querySelector<HTMLElement>('.ac-page > .c-phead > .c-acts')!
    expect(acts, 'the page head draws its actions').not.toBeNull()
    const refresh = acts.querySelector('.c-refresh')!
    expect(refresh.parentElement).toBe(acts)
    expect(acts.firstElementChild).toBe(refresh)
    // MUTATION: put the UrRadio "Whose access" back in Access.tsx's PageHead.
    expect(screen.queryByRole('radiogroup', { name: 'Whose access' })).toBeNull()
    expect(acts.querySelector('.ur-acts')).toBeNull()
  })

  it('at 1440 the head keeps its actions right, on one row', async () => {
    await mountAccess()
    const acts = document.querySelector<HTMLElement>('.ac-page > .c-phead > .c-acts')!
    // MUTATION: move the phone rule for the head's actions out of the phone block.
    expect(painted(acts, 'justify-content', WIDE)).toBe('flex-end')
    expect(painted(acts, ['margin-left', 'margin'], WIDE)).toBe('auto')
  })
})
