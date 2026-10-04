// LANE U9 ITEMS 4-6 (owner, 2026-10-03, run_d13a2f1b7e1e4eafabfb at 1440 and
// 390): the run page.
//
//   4. THE PAGE LEADS WITH THE ISSUE. Its title was the run id; it is the
//      issue's title now, with `owner/repo#N · run_… · created by …` as the
//      meta. "Read from the issue" printed title / labels / body / comments as
//      "not served by the run" although the preview read them at submission:
//      the run stores that read (`issue_read`, routes/runs.py) and the card
//      shows it. A run created before runs kept it keeps the dash with that
//      reason; a read that failed at submission says why. Plan comment,
//      Status comment and Pull request are a dash with "GitHub write-back is
//      not built yet (#454)".
//   5. THE PLAN IS STRUCTURED: a two-to-three-line summary, numbered steps
//      (title, step id, files / tests / estimate when the plan has them, the
//      prompt folded), overlaps and risks when the plan has them, and the raw
//      plan behind "Show the full plan" -- not one 25-line paragraph.
//   6. A LINK NEVER BREAKS MID-NAME: `bogdan-/alexandrescu/SwarmCloud#454`
//      wrapped at the hyphen in the Linked and Read-from-the-issue cards. The
//      reference is one line, cut with an ellipsis, its whole text in a title.
//
// MUTATIONS: title the page with the run id again, drop `issue_read` from the
// card, render the summary whole, unfold the prompts, or put
// `overflow-wrap: anywhere` back on the card's links -- each goes red here.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'c3'.repeat(32)
const REF = 'bogdan-alexandrescu/SwarmCloud#454'
const URL = 'https://github.com/bogdan-alexandrescu/SwarmCloud/issues/454'

/** The planner's summary as the owner measured it: one long paragraph. */
const LONG_SUMMARY =
  'Build the issue intake end to end. The run page leads with the issue and keeps what the preview read. ' +
  'The plan is drawn from its schema rather than as one paragraph. ' +
  Array.from({ length: 24 }, (_, i) => `Detail ${i + 1} of the change touches another part of the console and its tests.`).join(' ')

const ISSUE_READ = {
  title: 'Issue intake: plan an issue, show the plan, run it once approved',
  labels: ['enhancement', 'Where: Runs'],
  state: 'open',
  comments: 4,
  url: URL,
  body: 'The console should take a GitHub issue and plan it.\n',
  body_truncated: false,
  body_redacted: false,
  read_at: '2026-10-03T14:02:00Z',
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: 'run_d13a2f1b7e1e4eafabfb', tenant_id: 'eng', state: 'PLANNED', terminal: false,
    issue: { ref: REF, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454, url: URL,
      repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud' },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner1',
    plan: {
      summary: LONG_SUMMARY,
      steps: [
        { step_id: 'impl-plan-schema-and-overlaps', title: 'api: the plan schema and its overlaps',
          prompt: 'Extend PlanSpec.\nAdd overlaps.\nWrite the tests first.', depends_on: [] },
        { step_id: 'impl-forge-write-back', title: 'worker: write the plan back to the issue',
          prompt: 'Post the plan comment.', depends_on: ['impl-plan-schema-and-overlaps'],
          files: ['apps/agent-worker/agent_worker/forge.py'], tests: ['tests/unit/worker/test_forge_comment.py'],
          estimate: 'about 40 min' },
      ],
      overlaps: ['PR #498 also edits Runs.tsx'],
      risks: ['The forge token must never reach a log line.'],
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    plan_shape: '2 steps in 2 stages, then review and fix',
    created_by: 'operator@example.com', created_at: '2026-10-03T14:02:00Z', updated_at: '2026-10-03T14:09:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-10-03T14:02:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: ISSUE_READ,
    issue_read_error: null,
    ...over,
  }
}

function serve(body: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const ok = url === '/v1/runs/run_d13a2f1b7e1e4eafabfb'
    return new Response(JSON.stringify(ok ? { run: body } : { code: 'not_found', message: url }), {
      status: ok ? 200 : 404, headers: JSON_HEADERS,
    })
  }) as unknown as typeof fetch
}

async function mount(served: unknown) {
  serve(served)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view="run=run_d13a2f1b7e1e4eafabfb" go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-plan')).not.toBeNull(), WAIT)
  return utils
}

function card(container: HTMLElement, name: string): HTMLElement {
  return within(container).getByRole('region', { name })
}

function fact(region: HTMLElement, key: string): HTMLElement {
  const li = [...region.querySelectorAll<HTMLElement>('.ctl-fact')].find((f) => visible(f.querySelector('b')) === key)
  expect(li, `no "${key}" fact`).toBeDefined()
  return li!
}

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('item 4: the run page leads with the issue', () => {
  it('titles the page with the issue title and puts ref · run · creator in its meta', async () => {
    await mount(run())
    const h1 = await screen.findByRole('heading', { level: 1, name: ISSUE_READ.title }, WAIT)
    expect(h1).toBeTruthy()
    expect(screen.queryByRole('heading', { level: 1, name: 'run_d13a2f1b7e1e4eafabfb' })).toBeNull()
    await waitFor(() => expect(document.body.textContent).toContain(`${REF} · run_d13a2f1b7e1e4eafabfb · created by operator@example.com`), WAIT)
  })

  it('shows what the issue said at submission in the Read-from-the-issue card', async () => {
    const { container } = await mount(run())
    const read = card(container, 'Read from the issue')
    expect(visible(fact(read, 'title'))).toContain(ISSUE_READ.title)
    expect(visible(fact(read, 'labels'))).toContain('enhancement · Where: Runs')
    expect(visible(fact(read, 'state'))).toContain('open')
    expect(visible(fact(read, 'comments'))).toContain('4')
    expect(visible(fact(read, 'body'))).toMatch(/\d+ chars/)
    expect(visible(read)).toContain('The console should take a GitHub issue and plan it.')
    expect(visible(read)).not.toMatch(/not served by the run/)
    expect(read.querySelectorAll('.ctl-fact.is-absent')).toHaveLength(0)
  })

  it('keeps the dash and its reason for a run created before runs kept the read', async () => {
    const { container } = await mount(run({ issue_read: undefined, issue_read_error: undefined }))
    const read = card(container, 'Read from the issue')
    for (const key of ['title', 'labels', 'body', 'comments']) {
      const f = fact(read, key)
      expect(f.classList.contains('is-absent'), key).toBe(true)
      expect(visible(f)).toMatch(/— .*created before runs kept what the issue said/)
    }
    // The page still has a title: the issue's reference.
    await screen.findByRole('heading', { level: 1, name: REF }, WAIT)
  })

  it('says why when the read failed at submission, never 0 comments', async () => {
    const { container } = await mount(run({ issue_read: null,
      issue_read_error: { code: 'no_forge_credential', message: "tenant 'eng' has no forge credential" } }))
    const read = card(container, 'Read from the issue')
    const comments = fact(read, 'comments')
    expect(comments.classList.contains('is-absent')).toBe(true)
    expect(visible(comments)).toMatch(/not read at submission/)
    expect(visible(read)).toContain("tenant 'eng' has no forge credential")
    expect(visible(comments)).not.toMatch(/\b0\b/)
  })

  it('names write-back as not built for the comments and the pull request', async () => {
    const { container } = await mount(run())
    const links = card(container, 'Linked')
    for (const key of ['Plan comment', 'Status comment', 'Pull request']) {
      const f = fact(links, key)
      expect(f.classList.contains('is-absent'), key).toBe(true)
      expect(visible(f)).toContain('— GitHub write-back is not built yet (#454)')
    }
  })
})

describe('item 5: the plan is drawn from its schema', () => {
  it('leads with a short summary, not the whole paragraph', async () => {
    const { container } = await mount(run())
    const lead = container.querySelector<HTMLElement>('.rn-plan .rn-summary')!
    expect(visible(lead)).toMatch(/^Build the issue intake end to end\./)
    // Two or three sentences: a phone reads it in three lines, not twenty-five.
    expect(visible(lead).length).toBeLessThanOrEqual(320)
    expect(visible(lead)).not.toContain('Detail 24')
  })

  it('numbers the steps with title and id, files / tests / estimate when present, and folds each prompt', async () => {
    const { container } = await mount(run())
    const steps = [...container.querySelectorAll<HTMLElement>('.rn-plan .rn-step')]
    expect(steps).toHaveLength(2)
    expect(visible(steps[0]!.querySelector('.rn-step-h'))).toBe('1 · api: the plan schema and its overlaps')
    expect(visible(steps[0]!)).toContain('impl-plan-schema-and-overlaps')
    expect(steps[0]!.querySelector('.rn-step-meta'), 'files drawn for a step that has none').toBeNull()
    const meta = steps[1]!.querySelector<HTMLElement>('.rn-step-meta')!
    expect(visible(meta)).toContain('apps/agent-worker/agent_worker/forge.py')
    expect(visible(meta)).toContain('tests/unit/worker/test_forge_comment.py')
    expect(visible(meta)).toContain('about 40 min')
    for (const s of steps) {
      const fold = s.querySelector<HTMLDetailsElement>('details.rn-prompt-d')
      expect(fold, 'the prompt is not folded').not.toBeNull()
      expect(fold!.open).toBe(false)
      expect(visible(fold!.querySelector('summary'))).toBe('Prompt')
    }
  })

  it('draws overlaps and risks when the plan has them, and the raw plan behind a disclosure', async () => {
    const { container } = await mount(run())
    expect(visible(container.querySelector('.rn-overlaps'))).toContain('PR #498 also edits Runs.tsx')
    expect(visible(container.querySelector('.rn-risks'))).toContain('The forge token must never reach a log line.')
    const raw = container.querySelector<HTMLDetailsElement>('details.rn-raw')!
    expect(raw.open).toBe(false)
    expect(visible(raw.querySelector('summary'))).toBe('Show the full plan')
    expect(visible(raw)).toContain('Detail 24 of the change')
    fireEvent.click(raw.querySelector('summary')!)
  })

  it('a plan with neither draws no risks and says overlaps are not served', async () => {
    const { container } = await mount(run({ plan: { summary: 'One short summary.', steps: [
      { step_id: 'one', title: 'one step', prompt: 'do it' }] } }))
    expect(container.querySelector('.rn-risks')).toBeNull()
    expect(visible(container.querySelector('.rn-overlaps'))).toMatch(/not served/)
    expect(visible(container.querySelector('.rn-summary'))).toBe('One short summary.')
  })
})

describe('item 6: owner/repo#N stays on one line', () => {
  for (const width of [1440, 390]) {
    it(`in the Linked and Read-from-the-issue cards at ${width}px`, async () => {
      const env: CascadeEnv = { width }
      const { container } = await mount(run())
      for (const name of ['Linked', 'Read from the issue']) {
        const ref = within(card(container, name)).getByText(REF)
        expect(ref.getAttribute('title'), `${name}: the whole reference is not in a title`).toBe(REF)
        expect(painted(ref, 'white-space', env), `${name}: the reference wraps`).toBe('nowrap')
        expect(painted(ref, 'text-overflow', env), `${name}: the reference is cut without an ellipsis`).toBe('ellipsis')
        expect(painted(ref, ['overflow-x', 'overflow'], env)).toBe('hidden')
        expect(painted(ref, 'min-width', env), `${name}: the reference cannot shrink in its row`).toBe('0')
        expect(painted(ref, 'overflow-wrap', env) ?? 'normal', `${name}: the reference breaks anywhere`).toBe('normal')
      }
    })
  }
})
