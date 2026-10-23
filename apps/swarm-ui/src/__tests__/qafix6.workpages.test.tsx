// VISUAL QA RUN 6 LANE QAFIX6-WORKPAGES (part of #1680): RUNS, REPOSITORIES
// AND ACCESS.
//
//   a (6:work-a-010)  Runs' Plan approval cell printed `plan_approval` as
//        served, so run_4c1e09d2 (Planned, no workflow) and run_19ab77e0
//        (Running, workflow wf_8b21d0e4) both read "required". It says
//        whether the approval is still pending or was given.
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

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

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

  it("the Planned run with no workflow reads 'awaiting approval', the Running one 'approved', an automatic one 'not required'", async () => {
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
    // MUTATION: print `{r.plan_approval}` in the cell again (both required runs read "required").
    expect(cells.map(visible)).toEqual(['awaiting approval', 'approved', 'not required'])
  })

  it("the fixture build's own two runs read 'awaiting approval' and 'approved'", async () => {
    vi.stubEnv('VITE_LIVE', '')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={null} go={vi.fn()} />)
    const cells = await waitFor(() => {
      const c = [...container.querySelectorAll<HTMLElement>('.rn-row > td[data-label="Plan approval"]')]
      expect(c).toHaveLength(2)
      return c
    }, WAIT)
    // MUTATION: print `{r.plan_approval}` in the cell again (both fixture runs read "required").
    expect(cells.map(visible)).toEqual(['awaiting approval', 'approved'])
  })

  it("a run that ended before its plan was approved reads 'not given'", async () => {
    const { planApprovalWord } = await import('../Runs')
    // MUTATION: return 'awaiting approval' for every unapproved run that is not Planning.
    for (const state of ['FAILED', 'CANCELLED', 'NOT_READY']) {
      expect(planApprovalWord(issueRun({ state, terminal: state !== 'NOT_READY' }) as never), state).toBe('not given')
    }
  })

  it('at 1440 the word is drawn at the micro size and wraps inside its 86px column', async () => {
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
    // 'awaiting approval' is about 107px at --t-meta, nowrap, in about 74px of
    // content: it ran over the Workflow cell.
    // MUTATION: drop `.rn-row > td[data-label='Plan approval']` from repositories.css.
    expect(painted(cell, 'white-space', WIDE)).toBe('normal')
    expect(painted(cell, 'font-size', WIDE)).toMatch(/--t-micro|^12px$/)
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
    expect(visible(cards[2]!.querySelector('h2'))).toBe(`example-org/${LONG}`)
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
    expect(painted(cards[2]!.querySelector('.ur-cbadge')!, ['justify-content'], WIDE) ?? 'flex-start').toMatch(/^(flex-)?start|normal$/)
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

  it('the actions wrap as one group under the title from the left edge, Refresh on the switch\'s row', async () => {
    await mountAccess()
    const acts = document.querySelector<HTMLElement>('.ac-page > .c-phead > .c-acts')!
    expect(acts, 'the page head draws its actions').not.toBeNull()
    const refresh = acts.querySelector('.c-refresh')!
    const sw = screen.getByRole('radiogroup', { name: 'Whose access' })
    const group = sw.closest('.ur-acts')!
    // Refresh and the switch share one container: the head's actions, Refresh
    // and the switch's group its two items, Refresh first.
    expect(refresh.parentElement).toBe(acts)
    expect(group.parentElement).toBe(acts)
    expect(acts.firstElementChild).toBe(refresh)
    // MUTATION: drop the phone rule `.ac-page > .c-phead > .c-acts { ... }` from repositories.css.
    expect(painted(acts, ['flex', 'flex-basis'], PHONE) ?? '', 'the actions take the row under the title').toMatch(/100%/)
    expect(painted(acts, ['margin-left', 'margin'], PHONE)).toBe('0')
    expect(painted(acts, 'justify-content', PHONE), 'the group is aligned to the left edge').toBe('flex-start')
    // A group sized to its content wraps whole onto the next line and leaves
    // Refresh alone; from no width it stays on Refresh's line and wraps inside.
    // MUTATION: drop `.ac-page > .c-phead > .c-acts > .ur-acts { flex: 1 1 0 }`.
    expect(painted(group, ['flex', 'flex-basis'], PHONE) ?? '').toMatch(/(^|\s)0(px|%)?$/)
    expect(painted(group, ['flex-wrap', 'flex-flow'], PHONE) ?? '').toMatch(/wrap/)
  })

  it('at 1440 the head keeps its actions right, on one row', async () => {
    await mountAccess()
    const acts = document.querySelector<HTMLElement>('.ac-page > .c-phead > .c-acts')!
    // MUTATION: move the phone rule for the head's actions out of the phone block.
    expect(painted(acts, 'justify-content', WIDE)).toBe('flex-end')
    expect(painted(acts, ['margin-left', 'margin'], WIDE)).toBe('auto')
  })
})
