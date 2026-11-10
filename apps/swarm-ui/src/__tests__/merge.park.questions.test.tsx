// A MERGE STEP HELD BY THE OWNER'S QUESTIONS IS DRAWN AS THOSE QUESTIONS, NOT
// AS A BROWSER SIGN-IN (MERGE-PARK-QUESTIONS, #2313).
//
// owner, 2026-10-25 ~07:38Z, on task_7ee2d99642c84dc0b3b2 (GA-MOVE-VIEWS'
// merge, held by 2 open questions): "why a merge step in a workflow needs to
// use a browser agent and also needs user input to move forward?" The merge
// (runner profile `merge`: git only, no browser) parks PARKED(HUMAN_REQUIRED)
// with `park_phase: owner_questions` in its `blocked_by` entry
// (agent_worker.lifecycle._park_for_owner_questions), and the console drew
// the live-browser hand-off for it: the yellow banner, I'm ready (which only
// re-ran the merge into the same park, 07:32:52Z on task_ad66d2b402a847ca8b7d),
// and the Browser tab. The answer that clears the last named question returns
// it to READY by itself (QUESTION-WAKE), so what the page owes the owner is
// the questions and the answer control.
//
// MUTATIONS: drop the owner-question exclusion from `handoffOf`; drop the
// `held` guard on the Browser tab in AgentSplit; render `HeldQuestionsCard`
// nowhere; answer on the merge task instead of the asking one.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, TaskQuestions } from '../api'
import type { Result } from '../fetch'
import type { Task, Waiting } from '../types'
import { cascade, splitTop, type CascadeEnv } from './cssgate'
import { at, ev, task as runTask } from './runfixture'
import { ALL_CSS } from './sheets'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadAttempts: vi.fn(),
  loadChildren: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadLiveSession: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadQuestions: vi.fn(),
  answerQuestion: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api, USE_FIXTURES: false }
})

const { AgentSplit } = await import('../AgentSplit')
const { Run } = await import('../AgentDetail')
const { handoffOf, needsYou, ownerQuestionsHold } = await import('../Handoff')
const { fromAddress } = await import('../App')
const { resetListSnap } = await import('../listSnap')
const { waitingShort } = await import('../waitingShort')
const { HeldQuestionsCard } = await import('../QuestionsCard')

const ID = 'task_7ee2d99642c84dc0b3b2'
const IMPL = 'task_a1b2c3d4e5f6a7b8c9d0'
const FIX = 'task_f0e1d2c3b4a5968778695'
const NOW = Date.now()
const iso = (ms: number) => new Date(ms).toISOString()
const SPLIT = { timeout: 12_000 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const HELD = [
  { task_id: IMPL, step_id: 'implement', question_index: 0, question: 'Keep the old route as a redirect?' },
  { task_id: FIX, step_id: 'fix', question_index: 1, question: 'Rename the flag now or in a follow-up?' },
]

const SUMMARY = 'Waiting for answers to 2 owner questions before merging (implement: 1, fix: 1)'

function waiting(over: Partial<Waiting> = {}): Waiting {
  return {
    reason: 'HUMAN_REQUIRED',
    summary: SUMMARY,
    since: iso(NOW - 9 * 60_000),
    next_check_at: null,
    resumes_when: 'the last named question is answered (it resumes on its own)',
    detail: {
      park_phase: 'owner_questions',
      questions: HELD,
      unreadable: 0,
      who_may_answer: { submitted_by: 'owner@example.com', admin: true, text: 'owner@example.com, who launched workflow wf_7548, or an admin' },
    },
    history: [],
    history_read: true,
    read_at: iso(NOW),
    ...over,
  }
}

/** The merge step, parked on the owner's questions as the worker writes it. */
function mergeHeld(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'merge',
    workflow_id: 'wf_7548cc6f627f42769919',
    step_id: 'merge',
    submitted_by: 'owner@example.com',
    started_at: at(1),
    completed_at: null,
    result_summary: null,
    next_eligible_at: iso(NOW - 9 * 60_000),
    blocked_by: [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { code: 'owner_questions_open', questions: HELD, unreadable: 0 },
      } as unknown as NonNullable<Task['blocked_by']>[number],
    ],
    waiting: waiting(),
    ...over,
  })
}

/** A browser agent's hand-off that nobody took in time: the real sign-in park. */
function handOff(): Task {
  return runTask({
    id: ID,
    state: 'PARKED',
    park_reason: 'HUMAN_REQUIRED',
    runner_profile: 'claude-code-browser',
    resource_class: 'browser',
    started_at: at(1),
    completed_at: null,
    result_summary: null,
    next_eligible_at: iso(NOW - 60_000),
    blocked_by: [{ reason: 'HUMAN_REQUIRED' } as unknown as NonNullable<Task['blocked_by']>[number]],
    waiting: waiting({
      summary: 'Waiting for a person: sign in on its console page, then say it is ready',
      resumes_when: "a member of the workspace calls human-ready (the console's Ready button)",
      detail: {
        who_may_answer: { tenant_id: 'acme', text: 'any member of workspace acme' },
        expires_at: null,
        answer_route: `POST /v1/tasks/${ID}/human-ready`,
        cause: 'hold_expired',
      },
    }),
  })
}

function asked(taskId: string, n: number): TaskQuestions {
  return {
    task_id: taskId,
    count: n,
    status: 'ok',
    redacted: false,
    redaction_count: 0,
    may_answer: true,
    questions: Array.from({ length: n }, (_, i) => ({
      question: `${taskId} question ${i}`,
      options: [
        { label: 'Yes', description: `yes to ${i}` },
        { label: 'No', description: `no to ${i}` },
      ],
      recommended: 'Yes',
      context: null,
      answer: null,
    })),
  }
}

type Pane = 'detail' | 'logs' | 'browser'

function Routed({ start = 'detail', onGo }: { start?: Pane; onGo: (to: string) => void }) {
  const [p, setP] = useState<Pane>(start)
  return (
    <div className="app has-inspector">
      <AgentSplit
        taskId={ID}
        pane={p}
        theater={false}
        artifact={null}
        closeTo="work/waiting"
        go={(to) => {
          onGo(to)
          setP(fromAddress(to).taskPane as Pane)
        }}
        base={`work/task/${ID}`}
      />
    </div>
  )
}

const tabLabels = () =>
  within(screen.getByRole('tablist', { name: 'Agent panes' }))
    .getAllByRole('tab')
    .map((t) => t.querySelector('.c-tab-label')?.textContent ?? t.textContent ?? '')

function run(t: Task): AgentRun {
  return {
    task: t,
    events: [ev('submitted', at(-1), null)],
    eventsDetail: null,
    attempts: [],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskEventsPage.mockReturnValue(new Promise(() => {}))
  api.loadLiveSession.mockReturnValue(new Promise(() => {}))
  api.loadQuestions.mockImplementation(async (id: string) => ok(asked(id, 2)))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('the hold is told apart from a hand-off', () => {
  it('reads the owner-question hold off blocked_by, and only off a HUMAN_REQUIRED park', () => {
    expect(ownerQuestionsHold(mergeHeld())?.questions.map((q) => [q.task_id, q.question_index])).toEqual([
      [IMPL, 0],
      [FIX, 1],
    ])
    expect(ownerQuestionsHold(handOff())).toBeNull()
    expect(ownerQuestionsHold(mergeHeld({ state: 'READY', park_reason: null }))).toBeNull()
  })

  it('is no hand-off: no banner, no I am ready, no "needs you" browser row', () => {
    expect(handoffOf(mergeHeld())).toBeNull()
    expect(needsYou(mergeHeld())).toBe(false)
    expect(handoffOf(handOff())?.phase).toBe('parked')
  })

  it('says it waits for answers in the short line, never "sign in"', () => {
    expect(waitingShort({ reason: 'HUMAN_REQUIRED', summary: SUMMARY })).toBe('answers: 2 owner questions')
    expect(waitingShort({ reason: 'HUMAN_REQUIRED', summary: 'Waiting for a person: sign in on its console page, then say it is ready' })).toBe(
      'needs a person to sign in',
    )
  })
})

describe("the merge step's page", () => {
  it('draws no Browser tab, no hand-off banner and no I am ready', async () => {
    api.loadTask.mockResolvedValue(ok(mergeHeld()))
    render(<Routed onGo={() => {}} />)
    await waitFor(() => expect(tabLabels()).toContain('Details'), SPLIT)
    // The read has landed: the head names the step.
    await waitFor(() => expect(document.querySelector('.ag-head')?.textContent ?? '').toContain('merge'), SPLIT)
    expect(tabLabels()).not.toContain('Browser')
    expect(document.querySelector('.ho-strip')).toBeNull()
    expect(screen.queryByRole('button', { name: /I.m ready/ })).toBeNull()
  })

  it('sends a Browser address (the bell links one) to Details, and draws no Browser tab there', async () => {
    api.loadTask.mockResolvedValue(ok(mergeHeld()))
    const went: string[] = []
    render(<Routed start="browser" onGo={(to) => went.push(to)} />)
    await waitFor(() => expect(went).toContain(`work/task/${ID}`), SPLIT)
    await waitFor(() => expect(tabLabels()).not.toContain('Browser'), SPLIT)
  })

  it('a real hand-off park keeps the banner, I am ready and the Browser tab', async () => {
    api.loadTask.mockResolvedValue(ok(handOff()))
    render(<Routed start="browser" onGo={() => {}} />)
    await waitFor(() => expect(tabLabels()).toContain('Browser'), SPLIT)
    api.loadTask.mockResolvedValue(ok(handOff()))
    const other = render(<Routed onGo={() => {}} />)
    await within(other.container).findByRole('button', { name: /I.m ready/ }, SPLIT)
    expect(other.container.querySelector('.ho-strip')).not.toBeNull()
  })
})

describe('the questions are drawn inline, with the answer control', () => {
  async function draw(t: Task): Promise<HTMLElement> {
    const { container } = render(<Run run={run(t)} />)
    await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull())
    return container
  }

  it('lists each held question with its options, links its asking task, and answers on that task', async () => {
    api.answerQuestion.mockImplementation(async (id: string) => ok(asked(id, 2)))
    const c = await draw(mergeHeld())
    const card = await within(c).findByRole('region', { name: 'Questions holding this merge' })
    await waitFor(() => expect(within(card).getAllByRole('radiogroup')).toHaveLength(2))
    // Only the named index of each asking task, in the asking task's words.
    expect(card.textContent).toContain(`${IMPL} question 0`)
    expect(card.textContent).not.toContain(`${IMPL} question 1`)
    expect(card.textContent).toContain(`${FIX} question 1`)
    expect(card.textContent).not.toContain(`${FIX} question 0`)
    const links = within(card).getAllByRole('link').map((a) => a.getAttribute('href') ?? '')
    expect(links.some((h) => h.includes(IMPL))).toBe(true)
    expect(links.some((h) => h.includes(FIX))).toBe(true)
    expect(c.textContent).not.toMatch(/sign in/i)
    expect(within(c).queryByRole('button', { name: /I.m ready/ })).toBeNull()
    // The answer goes to the ASKING task, at its own index.
    const fix = within(card).getByRole('radiogroup', { name: /fix question 2/i })
    fireEvent.click(within(fix).getByRole('radio', { name: /^No/ }))
    await act(async () => {
      fireEvent.click(within(fix.closest('li')!).getByRole('button', { name: 'Answer' }))
    })
    expect(api.answerQuestion).toHaveBeenCalledWith(FIX, { question_index: 1, choice: 'No', text: null })
  })

  it('says how it resumes, in the wait panel, without a human-ready route', async () => {
    const c = await draw(mergeHeld())
    const panel = c.querySelector('.dt-top')!
    expect(panel.textContent).toContain(SUMMARY)
    expect(panel.textContent).not.toContain('human-ready')
  })

  it('offers the re-run only for a hold the answer route cannot read whole', async () => {
    const c = await draw(mergeHeld())
    const card = await within(c).findByRole('region', { name: 'Questions holding this merge' })
    expect(within(card).queryByRole('button', { name: 'Re-run the merge' })).toBeNull()
    const blocked = [
      {
        reason: 'HUMAN_REQUIRED',
        park_phase: 'owner_questions',
        owner_questions: { code: 'owner_questions_open', questions: HELD.slice(0, 1), unreadable: 1 },
      } as unknown as NonNullable<Task['blocked_by']>[number],
    ]
    const u = await draw(mergeHeld({ id: 'task_unreadable0000000000', blocked_by: blocked }))
    const held = (await within(u).findAllByRole('region', { name: 'Questions holding this merge' })).at(-1)!
    // MUTATION: drop `UnreadableHold` -- an unreadable hold has no way back but this.
    expect(within(held).getByRole('button', { name: 'Re-run the merge' })).toBeTruthy()
    expect(held.textContent).toContain('could not be read')
    // 10 November 2026: a member's ready is recorded and the merge goes on past
    // the entries. MUTATION: keep "no answer can resume this merge by itself"
    // and the person is not told the ready lets it go on.
    expect(held.textContent).toContain('a ready from a member lets the merge go on past it.')
    const about = within(held).getByRole('button', { name: 'About going on past entries the merge cannot read' })
    expect(about.closest('.c-tip-host')?.querySelector('[role="tooltip"]')?.textContent).toContain('A newer review or a new head')
    // The status line agrees with the ready: no answer resumes it on its own.
    // MUTATION: keep "the last answer resumes it on its own" for every hold.
    const status = within(held).getByRole('status')
    expect(status.textContent).not.toContain('resumes it on its own')
    expect(status.textContent).not.toContain('no Ready')
    const readable = within(card).getByRole('status')
    expect(readable.textContent).toContain('the last answer resumes it on its own')
  })

  // THE (i)'S TIP STAYS INSIDE THE INSPECTOR WHEREVER THE SENTENCE WRAPS IT
  // (re-review, 10 November 2026). The inspector (.drawer.ag-split) clips
  // sideways, and a tip pinned right of its icon lost 160 of its 240px past
  // the inspector's LEFT edge at 1440x900, because the sentence wrapped the (i)
  // to the start of a line. A check that the CSS rule exists passed meanwhile.
  //
  // jsdom lays nothing out, so the boxes below are the ones the shipped page
  // measured in Chromium on the agent page (fixture merge park, 2 unreadable
  // entries): the inspector and the sentence. What is NOT assumed is how the
  // tip hangs: the element it is positioned from, its left/right and its
  // max-width come from the cascade of the shipped sheets over the rendered
  // card, at that width. The tip's box is then laid out as a browser does an
  // absolutely positioned box, with the (i) at either end of a line.
  // MUTATIONS: `.qc-unreadable .c-tip { left: auto; right: 0 }` alone (out at
  // the left), no rule at all (out at the right), the host left positioned.
  describe('the tip behind the (i)', () => {
    const GEOMETRY = [
      { name: 'desktop 1440x900', width: 1440, height: 900, inspector: { left: 1061, right: 1393 }, sentence: { left: 1078, right: 1363 } },
      { name: 'phone 390x844', width: 390, height: 844, inspector: { left: 8, right: 382 }, sentence: { left: 25, right: 367 } },
    ]
    const ICON = 28

    function value(el: Element, prop: string, env: CascadeEnv): string | null {
      const r = cascade(ALL_CSS, el, prop, env)
      expect(r.unsupported).toEqual([])
      return r.winner?.value ?? null
    }

    /** A length the tip's rules use: px, a percentage of the containing block, or min() of those. */
    function length(v: string, of: number): number {
      const min = /^min\((.*)\)$/.exec(v)
      if (min) return Math.min(...splitTop(min[1]).map((part) => length(part.trim(), of)))
      const px = /^(-?[\d.]+)px$/.exec(v)
      if (px) return Number(px[1])
      const pct = /^(-?[\d.]+)%$/.exec(v)
      if (pct) return (Number(pct[1]) / 100) * of
      if (v === '0') return 0
      throw new Error(`a length this test does not read: ${v}`)
    }

    for (const g of GEOMETRY) {
      it(`opens fully inside the inspector at ${g.name}, with the (i) at either end of a line`, async () => {
        const blocked = [
          {
            reason: 'HUMAN_REQUIRED',
            park_phase: 'owner_questions',
            owner_questions: { code: 'owner_questions_open', questions: HELD.slice(0, 1), unreadable: 2 },
          } as unknown as NonNullable<Task['blocked_by']>[number],
        ]
        const c = await draw(mergeHeld({ id: 'task_unreadable0000000002', blocked_by: blocked }))
        const block = c.querySelector<HTMLElement>('.qc-unreadable')!
        const about = within(block).getByRole('button', { name: 'About going on past entries the merge cannot read' })
        const tip = about.closest('.c-tip-host')!.querySelector<HTMLElement>('[role="tooltip"]')!
        const env: CascadeEnv = { width: g.width, height: g.height, states: ['hover', 'focus-visible'] }

        // The containing block: the nearest ancestor the cascade positions.
        let from: Element | null = tip.parentElement
        while (from !== null && (value(from, 'position', env) ?? 'static') === 'static') from = from.parentElement
        expect(from, 'the tip is positioned from nothing the card draws').not.toBeNull()
        expect(value(tip, 'position', env)).toBe('absolute')
        const left = value(tip, 'left', env) ?? 'auto'
        const right = value(tip, 'right', env) ?? 'auto'
        const maxWidth = value(tip, 'max-width', env) ?? 'none'

        for (const at of ['start', 'end'] as const) {
          const icon =
            at === 'start'
              ? { left: g.sentence.left, right: g.sentence.left + ICON }
              : { left: g.sentence.right - ICON, right: g.sentence.right }
          // The host wraps the icon alone; the sentence and its block span the line.
          const cb = from!.classList.contains('c-tip-host') ? icon : g.sentence
          expect(
            from === about.closest('.c-tip-host') || from === block || from === block.querySelector(':scope > p'),
            `the tip hangs from <${from!.tagName.toLowerCase()} class="${from!.className}">, which this test has no box for`,
          ).toBe(true)
          const cbWidth = cb.right - cb.left
          // The tip's text is far longer than one line, so it is as wide as it may be.
          expect(maxWidth, 'an unbounded tip is as wide as its text').not.toBe('none')
          const w = length(maxWidth, cbWidth)
          const x = left !== 'auto' ? cb.left + length(left, cbWidth) : right !== 'auto' ? cb.right - length(right, cbWidth) - w : icon.left
          const box = { left: Math.round(x), right: Math.round(x + w) }
          expect(box.left, `${g.name}, (i) at the ${at} of a line: tip ${box.left}..${box.right}, inspector ${g.inspector.left}..${g.inspector.right}`).toBeGreaterThanOrEqual(g.inspector.left)
          expect(box.right, `${g.name}, (i) at the ${at} of a line: tip ${box.left}..${box.right}, inspector ${g.inspector.left}..${g.inspector.right}`).toBeLessThanOrEqual(g.inspector.right)
          // Wide enough to read: the full 240px wherever the sentence allows it.
          expect(w).toBe(Math.min(240, cbWidth))
        }
      })
    }
  })

  it("keeps the agent page's held-questions id, and takes a page's own id for each mount", async () => {
    // MUTATION: drop the card's `id` prop, or its `held-questions` default.
    const c = await draw(mergeHeld())
    const card = await within(c).findByRole('region', { name: 'Questions holding this merge' })
    expect(card.id).toBe('held-questions')
    const { container } = render(
      <>
        <HeldQuestionsCard task={mergeHeld()} id="questions-merge-a-held" />
        <HeldQuestionsCard task={mergeHeld({ id: 'task_secondmerge000000000' })} id="questions-merge-b-held" />
      </>,
    )
    expect([...container.querySelectorAll('.qc-held')].map((e) => e.id)).toEqual(['questions-merge-a-held', 'questions-merge-b-held'])
  })

  it('draws no held-questions card for a hand-off park', async () => {
    const c = await draw(handOff())
    expect(within(c).queryByRole('region', { name: 'Questions holding this merge' })).toBeNull()
  })
})
