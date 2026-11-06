// THE PLUGIN INTERVIEW AS A FULL PAGE (direction B's plan screens, the owner's
// pick of 4 November 2026; part of #2688). One task's questions as
// `GET /v1/tasks/{id}/questions` serves them, with its `waiting` block: the
// pending question with its options side by side, the recommended one marked
// and picked, each option's design drawn in a sandboxed iframe when the
// question served one; the rail of answered questions with who answered and
// where; the bar with the time left and the answer, sent through
// `POST /v1/tasks/{id}/answers` as `console`. The proposal is its own page,
// decided on the plan's digest with the run page's calls.
//
// MUTATIONS, one per block: give the iframe `allow-scripts` or
// `allow-same-origin`, or draw the design as live markup; invent a design for
// an option that served none; count an answer with no `via` as the
// console's; say a time left the wait did not serve; send the answer without
// `via: 'console'` or to another question; approve without the digest; drop
// the route or the links to it.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { digest, issueRun, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  answerQuestion: vi.fn(),
  approvePlan: vi.fn(),
  editPlan: vi.fn(),
  rejectPlan: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const page = await import('../InterviewPage')
const { InterviewView, ProposalView, interviewPageOf, interviewAddress, proposalAddress, plainLabel, viaWord, answerTally } = page
type InterviewQuestions = import('../InterviewPage').InterviewQuestions
const { addressToPath, pathToAddress } = await import('../paths')

const NOW = Date.parse('2026-10-19T12:00:00Z')
const ASKED = '2026-10-19T11:22:00Z'
const ENDS = '2026-10-20T11:22:00Z'

const design = (word: string) => `<!doctype html><html><body><h1 id="d-${word}">${word}</h1><script>window.parent.document.title='x'</script></body></html>`

function questions(over: Partial<InterviewQuestions> = {}): InterviewQuestions {
  return {
    task_id: 't_design', count: 3, status: 'ok', redacted: false, redaction_count: 0, may_answer: true, may_answer_because: null,
    questions: [
      { question: 'Which checkout layout?', options: [{ label: 'One page', description: 'All on one page' }], recommended: null, context: null,
        answer: { question_index: 0, question: 'Which checkout layout?', choice: 'One page', text: null, by: 'ana.moreau@example.com', at: '2026-10-19T10:00:00Z', via: 'console', session: null, history: [] } },
      { question: 'Show the price breakdown?', options: [{ label: 'Yes', description: null }, { label: 'No', description: null }], recommended: 'Yes', context: null,
        answer: { question_index: 1, question: 'Show the price breakdown?', choice: 'Yes', text: null, by: 'kenji.sato@example.com', at: '2026-10-19T10:30:00Z', via: null, session: null, history: [] } },
      { question: 'Where does the timer sit?', context: 'The hold on a room lasts 10 minutes.', recommended: 'Summary (Recommended)',
        options: [
          { label: 'Summary (Recommended)', description: 'In the order summary', preview: design('summary') },
          { label: 'Header', description: 'In the page header', preview: design('header') },
          { label: 'Footer', description: 'Pinned at the foot' },
        ],
        answer: null, blocking: true },
    ],
    waiting: { open: true, blocking: [2], unanswered: [2], asked_at: ASKED, expires_at: ENDS, issue_comment_url: null, skill: 'design-html' },
    ...over,
  }
}

const designTask = runTask({
  id: 't_design', state: 'PARKED', workflow_id: 'wf_checkout', step_id: 'design', repository_url: 'https://github.com/example-org/roombook',
  metadata: { work_contract: { capabilities: { plugins: { gstack: { mode: 'designer', skills: ['design-html', 'design-review'] } } } } },
})

function draw(q: InterviewQuestions = questions(), go = vi.fn()) {
  render(<InterviewView taskId="t_design" go={go} read={{ questions: q, task: designTask, catalogue: [{ name: 'gstack', version: '1.92.55.0', runner_profiles: [], modes: {} }] }} />)
  return go
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  for (const f of Object.values(api)) f.mockReset()
})
afterEach(() => {
  vi.useRealTimers()
})

describe('addresses', () => {
  it('rides on the Runs tab: /runs?interview=<task> and /runs?proposal=<run>, both ways', () => {
    expect(addressToPath(interviewAddress('t_design'))).toBe('/runs?interview=t_design')
    expect(addressToPath(proposalAddress('run_1'))).toBe('/runs?proposal=run_1')
    expect(pathToAddress('/runs', '?interview=t_design')?.address).toBe('work/runs?interview=t_design')
    expect(interviewPageOf('interview=t_design')).toEqual({ kind: 'interview', taskId: 't_design' })
    expect(interviewPageOf('proposal=run_1')).toEqual({ kind: 'proposal', runId: 'run_1' })
    expect(interviewPageOf('run=run_1')).toBeNull()
    expect(interviewPageOf(null)).toBeNull()
  })
})

describe('the pending question', () => {
  it('asks the wait’s unanswered question, its options side by side, the recommended one marked and picked', () => {
    draw()
    expect(screen.getByRole('heading', { name: 'Where does the timer sit?' })).toBeTruthy()
    expect(screen.getByText('Question 3')).toBeTruthy()
    expect(screen.getByText('design-html').tagName).toBe('CODE')
    const group = screen.getByRole('radiogroup', { name: 'Answers to question 3' })
    const cards = within(group).getAllByRole('listitem')
    expect(cards.map((c) => c.querySelector('.iv-opt-k')?.textContent)).toEqual(['A', 'B', 'C'])
    expect(within(cards[0]!).getByText('Recommended')).toBeTruthy()
    expect(within(cards[0]!).getByText('Summary')).toBeTruthy()
    expect((within(cards[0]!).getByRole('radio') as HTMLInputElement).checked).toBe(true)
    expect(screen.getByRole('button', { name: 'Answer A' })).toBeTruthy()
    fireEvent.click(within(cards[1]!).getByRole('radio'))
    expect(screen.getByRole('button', { name: 'Answer B' })).toBeTruthy()
  })

  it('draws each served design in an iframe with an empty sandbox, never as live markup', () => {
    draw()
    const frames = [...document.querySelectorAll('iframe')]
    expect(frames).toHaveLength(2)
    for (const f of frames) {
      expect(f.getAttribute('sandbox')).toBe('')
      expect(f.getAttribute('srcdoc')).toContain('<h1')
    }
    // The design's own elements are not in the console's document.
    expect(document.getElementById('d-summary')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    // An option that served no design says so: nothing is drawn in its place.
    const footer = screen.getAllByRole('listitem').find((li) => li.textContent?.includes('Footer'))!
    expect(footer.querySelector('iframe')).toBeNull()
    expect(footer.textContent).toContain('No design came with this option.')
  })

  it('gives every design frame a sandbox attribute that is exactly empty, and its HTML as srcdoc, never src', () => {
    draw()
    const frames = [...document.querySelectorAll('iframe')]
    expect(frames).toHaveLength(2)
    for (const f of frames) {
      expect(f.hasAttribute('sandbox')).toBe(true)
      expect(f.getAttribute('sandbox')).toBe('')
      expect(f.hasAttribute('srcdoc')).toBe(true)
      expect(f.hasAttribute('src')).toBe(false)
    }
  })

  it('runs no script a design carries', () => {
    const ran = window as unknown as { __ivDesignRan?: boolean }
    delete ran.__ivDesignRan
    const q = questions()
    const hostile = '<!doctype html><html><body><p id="d-hostile">x</p><script>window.__ivDesignRan = true; window.parent.__ivDesignRan = true; top.__ivDesignRan = true</script><img src="x" onerror="parent.__ivDesignRan = true"></body></html>'
    q.questions[2] = { ...q.questions[2]!, options: [{ label: 'Summary (Recommended)', description: null, preview: hostile }, { label: 'Header', description: null }] }
    draw(q)
    const frame = document.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.getAttribute('srcdoc')).toContain('<script>')
    expect(ran.__ivDesignRan).toBeUndefined()
    expect(document.getElementById('d-hostile')).toBeNull()
    expect(document.querySelector('script, img[onerror]')).toBeNull()
  })

  it('keeps the reason a design was dropped behind an (i), and draws no frame for it', () => {
    const q = questions()
    const reason = 'the preview is larger than the 8192 characters one option may show'
    q.questions[2] = { ...q.questions[2]!, options: [
      { label: 'Summary (Recommended)', description: null, preview: design('summary') },
      { label: 'Header', description: null, preview: null, preview_dropped: reason },
    ] }
    draw(q)
    const header = screen.getAllByRole('listitem').find((li) => li.textContent?.includes('Header'))!
    expect(header.querySelector('iframe')).toBeNull()
    const tip = within(header).getByRole('button', { name: 'About the design' })
    const box = document.getElementById(tip.getAttribute('aria-describedby')!)!
    expect(box.hidden).toBe(true)
    expect(box.textContent).toBe(reason)
    fireEvent.click(tip)
    expect(box.hidden).toBe(false)
    // An option that served no design keeps today's card, with no (i).
    const summary = screen.getAllByRole('listitem').find((li) => li.textContent?.includes('Summary'))!
    expect(summary.querySelector('iframe')).not.toBeNull()
    expect(within(summary).queryByRole('button', { name: 'About the design' })).toBeNull()
  })

  it('enlarges a design to full size, still sandboxed', () => {
    draw()
    fireEvent.click(screen.getAllByRole('button', { name: 'Enlarge' })[1]!)
    const dialog = screen.getByRole('dialog', { name: 'Design: Header' })
    expect(dialog.querySelector('iframe')!.getAttribute('sandbox')).toBe('')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('says the time left of the wait, and that the first answer stands', () => {
    draw()
    const bar = document.querySelector('.iv-dbar')!
    expect(bar.textContent).toContain('23 h 22 min left of 24 h')
    expect(bar.textContent).toContain('Never answered for anyone. The first answer stands.')
  })

  it('says a clock the wait did not serve is not known, never a number', () => {
    draw(questions({ waiting: { open: true, blocking: [2], unanswered: [2], asked_at: null, expires_at: null, issue_comment_url: null, skill: null } }))
    const bar = document.querySelector('.iv-dbar')!
    expect(bar.textContent).toContain('time left not known')
    expect(bar.textContent).not.toMatch(/\d+ h|\d+ min/)
    expect(bar.textContent).not.toContain('The first answer stands')
    expect(screen.getByText(/skill that asked: not known/)).toBeTruthy()
  })

  it('offers no answer to a viewer who may not answer, and says why', () => {
    draw(questions({ may_answer: false, may_answer_because: 'Only the submitter or an admin answers this.' }))
    expect((screen.getByRole('button', { name: 'Answer A' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Use the recommended answer' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText('Only the submitter or an admin answers this.')).toBeTruthy()
  })
})

describe('the rail', () => {
  it('lists each answered question with who answered and where, and counts the places', () => {
    draw()
    const rail = screen.getByRole('navigation', { name: 'Questions answered so far' })
    expect(rail.textContent).toContain('2 answered')
    expect(rail.textContent).toContain('how many more: not known')
    const items = within(rail).getAllByRole('listitem')
    expect(items[0]!.textContent).toContain('ana.moreau@example.com')
    expect(items[0]!.textContent).toContain('Console')
    // An answer the API stored with no `via` is not counted as the console's.
    expect(items[1]!.textContent).toContain('where: not recorded')
    expect(items[2]!.getAttribute('aria-current')).toBe('step')
    expect(items[2]!.textContent).toContain('Waiting for an answer')
    const head = within(rail).getByLabelText('Where they were answered')
    expect(head.textContent).toBe('Console 1where: not recorded 1')
  })

  it('shows an answer given by voice as Voice it', () => {
    const q = questions()
    q.questions[1] = { ...q.questions[1]!, answer: { ...q.questions[1]!.answer!, via: 'voice-it' } }
    draw(q)
    const rail = screen.getByRole('navigation', { name: 'Questions answered so far' })
    const items = within(rail).getAllByRole('listitem')
    expect(items[1]!.querySelector('.iv-via')!.textContent).toBe('Voice it')
    expect(items[1]!.querySelector('.iv-via')!.className).toContain('is-voice-it')
    expect(within(rail).getByLabelText('Where they were answered').textContent).toBe('Console 1Voice it 1')
  })

  it('words every place an answer can come from', () => {
    expect(viaWord('console')).toBe('Console')
    expect(viaWord('claude-code')).toBe('Claude Code')
    expect(viaWord('voice-it')).toBe('Voice it')
    expect(viaWord('github-comment')).toBe('GitHub')
    expect(viaWord(null)).toBe('where: not recorded')
    expect(plainLabel('Summary (Recommended)')).toBe('Summary')
  })
})

describe('answering', () => {
  function answeredRead(choice: string | null, text: string | null): InterviewQuestions {
    const q = questions()
    q.questions[2] = { ...q.questions[2]!, answer: { question_index: 2, question: 'Where does the timer sit?', choice, text, by: 'ana.moreau@example.com', at: '2026-10-19T12:00:00Z', via: 'console', session: null, history: [] } }
    return { ...q, waiting: { ...q.waiting!, open: false, unanswered: [] } }
  }

  it('sends the recommended answer to that question as the console, then shows it chosen', async () => {
    api.answerQuestion.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: answeredRead('Summary (Recommended)', null) })
    draw()
    fireEvent.click(screen.getByRole('button', { name: 'Use the recommended answer' }))
    await waitFor(() => expect(screen.getByText('Question 3 answered')).toBeTruthy())
    expect(api.answerQuestion).toHaveBeenCalledWith('t_design', { question_index: 2, choice: 'Summary (Recommended)', text: null, via: 'console' })
    expect(screen.getByText('Answered: queued to resume')).toBeTruthy()
    expect(screen.getByText(/the recommended answer/)).toBeTruthy()
    expect(screen.getByText('B and C were not chosen')).toBeTruthy()
    expect(document.querySelector('.iv-dbar')!.textContent).toContain('A second answer to question 3 is refused: the first one stands.')
    const chosen = document.querySelector('.iv-done-shot iframe')!
    expect(chosen.getAttribute('sandbox')).toBe('')
    const rail = screen.getByRole('navigation', { name: 'Questions answered so far' })
    expect(rail.textContent).toContain('3 answered')
    expect(rail.querySelector('.is-just')!.textContent).toContain('just now')
  })

  it('sends an answer in the person’s own words', async () => {
    api.answerQuestion.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: answeredRead(null, 'Only in the last 5 minutes') })
    draw()
    fireEvent.click(screen.getByRole('button', { name: 'Answer in your own words' }))
    const send = screen.getByRole('button', { name: 'Send my answer' }) as HTMLButtonElement
    expect(send.disabled).toBe(true)
    fireEvent.change(screen.getByRole('textbox'), { target: { value: ' Only in the last 5 minutes ' } })
    fireEvent.click(send)
    await waitFor(() => expect(screen.getByText('Question 3 answered')).toBeTruthy())
    expect(api.answerQuestion).toHaveBeenCalledWith('t_design', { question_index: 2, choice: null, text: 'Only in the last 5 minutes', via: 'console' })
  })

  it('says why an answer was not stored, and keeps the question open', async () => {
    api.answerQuestion.mockResolvedValue({ status: 'error', error: { kind: 'conflict', httpStatus: 409, code: 'already_answered', message: 'This question was already answered.' } })
    draw()
    fireEvent.click(screen.getByRole('button', { name: 'Answer A' }))
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('This question was already answered.'))
    expect(screen.getByRole('radiogroup')).toBeTruthy()
  })

  it('goes back to Needs you after answering', async () => {
    api.answerQuestion.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: answeredRead('Header', null) })
    const go = draw()
    fireEvent.click(screen.getAllByRole('radio')[1]!)
    fireEvent.click(screen.getByRole('button', { name: 'Answer B' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Back to Needs you' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Back to Needs you' }))
    expect(go).toHaveBeenCalledWith('work/running')
  })
})

describe('the step facts', () => {
  it('names the step, its repository and its plugin with the catalogue’s pinned version, mode and skills', () => {
    draw()
    const facts = screen.getByLabelText('The step')
    expect(facts.textContent).toContain('design')
    expect(facts.textContent).toContain('example-org/roombook')
    expect(facts.textContent).toContain('gstack 1.92.55.0')
    expect(facts.textContent).toContain('Designer')
    expect(facts.textContent).toContain('design-html · design-review')
    expect(facts.textContent).toContain('Parked: holds no machine')
  })
})

describe('the proposal page', () => {
  const run = issueRun({ planner_task_id: 't_plan', plan: { summary: 'Move the timer.', steps: [{ step_id: 'ui', title: 'ui: the timer', prompt: 'Draw it.' }], requirements: ['The timer shows'], estimate: '2 hours' } })
  const planQs = questions()

  it('shows the plan, the interview tallied by who and where, and that the design doc is not known here', () => {
    render(<ProposalView read={{ run: run as never, questions: planQs }} go={vi.fn()} reread={vi.fn()} />)
    expect(screen.getByText('Move the timer.')).toBeTruthy()
    expect(screen.getByText('Plan waiting for approval')).toBeTruthy()
    const rail = screen.getByRole('complementary', { name: 'The interview' })
    expect(rail.textContent).toContain('3 questions asked')
    expect(rail.textContent).toContain('2 of 3 answered.')
    expect(screen.getByText(/Not known here: the run serves its plan/)).toBeTruthy()
    expect(answerTally(planQs)).toEqual([
      { by: 'ana.moreau@example.com', via: 'Console', n: 1 },
      { by: 'kenji.sato@example.com', via: 'where: not recorded', n: 1 },
    ])
    expect(within(rail).getByRole('link', { name: 'The questions and answers' }).getAttribute('href')).toBe('/runs?interview=t_plan')
  })

  it('approves against the plan’s digest, and says what it did', async () => {
    api.approvePlan.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: { run: { ...run, state: 'APPROVED', approved_by: 'operator@example.com' } } })
    render(<ProposalView read={{ run: run as never, questions: planQs }} go={vi.fn()} reread={vi.fn()} />)
    expect(document.querySelector('.iv-dbar')!.textContent).toContain('Revision 1')
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(screen.getByRole('status').textContent).toContain('Approved.'))
    expect(api.approvePlan).toHaveBeenCalledWith(run.id, digest('a1'))
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull()
  })

  it('reads the run again when the plan changed first, deciding nothing', async () => {
    api.approvePlan.mockResolvedValue({ status: 'error', error: { kind: 'conflict', httpStatus: 409, code: 'plan_changed', message: 'changed' } })
    const reread = vi.fn()
    render(<ProposalView read={{ run: run as never, questions: planQs }} go={vi.fn()} reread={reread} />)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(reread).toHaveBeenCalled())
  })

  it('rejects with a reason on the digest, through the run page’s form', async () => {
    api.rejectPlan.mockResolvedValue({ status: 'ok', fetchedAt: NOW, data: { run: { ...run, state: 'REJECTED' } } })
    render(<ProposalView read={{ run: run as never, questions: planQs }} go={vi.fn()} reread={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Not this sprint' } })
    const form = document.querySelector('form.rn-reject') as HTMLFormElement
    fireEvent.submit(form)
    await waitFor(() => expect(api.rejectPlan).toHaveBeenCalledWith(run.id, digest('a1'), 'Not this sprint'))
  })

  it('opens the run page’s plan editor on Edit', () => {
    render(<ProposalView read={{ run: run as never, questions: planQs }} go={vi.fn()} reread={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(document.querySelector('.iv-plan-steps')).toBeNull()
    expect((screen.getAllByRole('textbox')[0] as HTMLTextAreaElement).value).toBe('Move the timer.')
  })

  it('says a run approved automatically, with no buttons', () => {
    const auto = { ...run, state: 'APPROVED', plan_approval: 'auto', approved_by: 'auto-approval' }
    render(<ProposalView read={{ run: auto as never, questions: null }} go={vi.fn()} reread={vi.fn()} />)
    expect(screen.getByText('Approved automatically')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Approve' })).toBeNull()
    expect(screen.getByText('its questions could not be read')).toBeTruthy()
  })
})

describe('the links to it', () => {
  it('the run’s page links its interview and its proposal', async () => {
    vi.useRealTimers()
    const r = issueRun()
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url === `/v1/runs/${r.id}`) return new Response(JSON.stringify({ run: r }), { status: 200, headers: { 'content-type': 'application/json' } })
      return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: { 'content-type': 'application/json' } })
    }) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { RunsScreen } = await import('../Runs')
    const { container } = render(<RunsScreen view={`run=${r.id}`} go={vi.fn()} />)
    await waitFor(() => expect(container.querySelector('section[aria-label="Linked"]')).not.toBeNull(), { timeout: 4000 })
    const linked = within(container).getByRole('region', { name: 'Linked' })
    // The run as this build serves it: its planner task and its id name the two pages.
    expect(within(linked).getByRole('link', { name: 'Open the interview' }).getAttribute('href')).toMatch(/^\/runs\?interview=task_planner\w*$/)
    expect(within(linked).getByRole('link', { name: 'Open the proposal' }).getAttribute('href')).toMatch(/^\/runs\?proposal=run_\w+$/)
    vi.unstubAllEnvs()
  })
})
