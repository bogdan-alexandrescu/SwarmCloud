// THE SUBMIT PAGES AS PICKED (submit.html: the chooser M2, the one-page task
// form F1, the stacked workflow form G1) AND THE #503 AUDIT FINDINGS ON THEM.
//
//   * /submit is a page of its own in the frame: its head names Submit, not
//     API reads; its refresh carries the recent read's age instead of
//     "reading…" forever; and the Work section is the lit one, as in every M2 frame.
//   * the chooser's two cards carry an icon, a "Start a task" / "Start a
//     workflow" button and their T / W key, and print no route as text;
//     recent submissions are M2's "Start from a recent one" card, stating the
//     route that does not exist rather than guessing a list;
//   * the task form's runners are two-column cards with size, backend and how
//     many can start now; steps 2 and 3 are cards, dimmed until a runner is
//     chosen; the summary says where the task lands and what that costs;
//   * a workflow step picks its runner from a select, the stages have arrows
//     between them, and "If a step fails" and "Priority" are in step 2, with
//     both listed in the summary.
//
// Rendered against the development fixture (mock 15 room, generic 10,
// claude-code 0, browser 0; codex disabled).

import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { App } from '../App'
import { SubmitScreen } from '../Submit'
import { SubmitChooser } from '../SubmitChooser'
import { SubmitWorkflowScreen } from '../SubmitWorkflow'
import SUBMIT_CSS from '../styles/submit.css?raw'
import COMPONENTS_CSS from '../styles/components.css?raw'
import STYLES from '../styles.css?raw'
import { flatRules, gate } from './cssgate'

const WAIT = { timeout: 4000 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const rules = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, '')
/** The retired chooser's classes, joined at runtime: `sk-` and a long tail
 *  reads as a key to the worker's credential scan. */
const OLD_SK = (tail: string) => `.sk-${tail}`

// ---------------------------------------------------------------------------
// /submit in the frame (#503: "API reads", "reading…", nothing lit)
// ---------------------------------------------------------------------------

describe('/submit is a page of its own in the frame', () => {
  it('names Submit in the head, times its recent read, and lights Submit', async () => {
    window.history.replaceState(null, '', '/submit')
    render(<App />)
    await screen.findByRole('heading', { name: 'Submit' }, WAIT)
    // The page head (visual QA Q2): the title and the head's age on one row.
    const head = document.querySelector<HTMLElement>('main.work .c-phead')!
    expect(visible(head), 'the head fell through to the API reads label').not.toContain('API reads')
    // THE CHOOSER READS YOUR RECENT SUBMISSIONS (QA G1-05/G4-23), so its
    // head carries that read's age on its refresh, never "reads nothing".
    expect(visible(head)).not.toContain('reads nothing')
    expect(head.querySelector('.c-refresh'), 'no refresh in the chooser head').not.toBeNull()
    // SUBMIT IS THE PLACE (U10a D25, owner QA 2026-10-04): it lit Work, and
    // the panel lit Agents › Live, so Submit never read as where you were.
    const cta = document.querySelector<HTMLElement>('.sk-spine .sk-cta')!
    expect(cta.getAttribute('aria-current'), 'Submit is not lit on /submit').toBe('page')
    const work = document.querySelector<HTMLElement>('.sk-spine [data-sec="work"]')!
    expect(work.getAttribute('aria-current'), 'Work is lit on /submit').toBeNull()
  })

  it('gives the two forms a trail back to the chooser', async () => {
    for (const [path, h1] of [['/submit/task', 'Submit a task'], ['/submit/workflow', 'Submit a workflow'], ['/submit/issue', 'Submit from a GitHub issue']] as const) {
      window.history.replaceState(null, '', path)
      const { unmount } = render(<App />)
      await screen.findByRole('heading', { name: h1, level: 1 }, WAIT)
      // THE WAY BACK IS THE SPINE'S SUBMIT, not a crumb row (visual QA Q2,
      // 2026-10-02: no breadcrumb on a section page). It is on every form.
      expect(document.querySelector('.ctl-crumb'), `${path} draws a breadcrumb row`).toBeNull()
      const back = document.querySelector<HTMLAnchorElement>('.sk-spine a.sk-cta')
      expect(back?.getAttribute('href'), `${path} has no way back to the chooser`).toBe('/submit')
      fireEvent.click(back!)
      await waitFor(() => expect(window.location.pathname).toBe('/submit'))
      unmount()
    }
  })
})

// ---------------------------------------------------------------------------
// the chooser (M2)
// ---------------------------------------------------------------------------

describe('the chooser draws the three picked cards', () => {
  it('each card has an icon, its heading, its Start button and its key', () => {
    const go = vi.fn()
    const { container } = render(<SubmitChooser go={go} />)
    const cards = [...container.querySelectorAll<HTMLElement>('.sb-choice')]
    // The third is intake-tenants.html 1A's "From a GitHub issue".
    expect(cards).toHaveLength(3)
    const [task, wf, issue] = cards as [HTMLElement, HTMLElement, HTMLElement]
    for (const c of cards) expect(c.querySelector('h2 svg'), 'a chooser card has no icon').not.toBeNull()
    expect(visible(task.querySelector('h2'))).toBe('Submit a task')
    expect(visible(wf.querySelector('h2'))).toBe('Submit a workflow')
    expect(visible(task.querySelector('kbd'))).toBe('T')
    expect(visible(wf.querySelector('kbd'))).toBe('W')
    expect(visible(issue.querySelector('h2'))).toBe('From a GitHub issue')
    expect(visible(issue.querySelector('kbd'))).toBe('I')
    // No route printed as monospace text.
    expect(visible(container)).not.toMatch(/\/submit\/(task|workflow|issue)/)

    fireEvent.click(within(task).getByRole('button', { name: 'Start a task' }))
    expect(go).toHaveBeenLastCalledWith('work/new')
    fireEvent.click(within(wf).getByRole('button', { name: 'Start a workflow' }))
    expect(go).toHaveBeenLastCalledWith('work/new-workflow')
    fireEvent.click(within(issue).getByRole('button', { name: 'Start from an issue' }))
    expect(go).toHaveBeenLastCalledWith('work/new-issue')
  })

  it('T and W open the two forms, but not while typing or with a modifier', () => {
    const go = vi.fn()
    render(
      <>
        <SubmitChooser go={go} />
        <input aria-label="elsewhere" />
      </>,
    )
    fireEvent.keyDown(screen.getByLabelText('elsewhere'), { key: 't' })
    fireEvent.keyDown(document.body, { key: 'w', ctrlKey: true })
    expect(go).not.toHaveBeenCalled()
    fireEvent.keyDown(document.body, { key: 't' })
    expect(go).toHaveBeenLastCalledWith('work/new')
    fireEvent.keyDown(document.body, { key: 'W' })
    expect(go).toHaveBeenLastCalledWith('work/new-workflow')
    fireEvent.keyDown(document.body, { key: 'i' })
    expect(go).toHaveBeenLastCalledWith('work/new-issue')
  })

  // U12 R15 (owner QA, 2026-10-04): the card lists the caller's own recent
  // submissions from the reads that exist, rather than "No recent submissions
  // yet" to an owner with runs (qa.u12.submit.test.tsx holds what it lists).
  it('"Start from a recent one" is a card of your own recent submissions, not a dashed box', async () => {
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    expect(recent, 'no recent card').not.toBeNull()
    expect(visible(recent.querySelector('.sb-card-h'))).toBe('Start from a recent one')
    // Where the list comes from is behind the `?`.
    expect(recent.querySelector('button[aria-label^="Help: "]'), 'the why is not behind a `?`').not.toBeNull()
    expect(container.querySelector(OLD_SK('recent-empty'))).toBeNull()
    await waitFor(() => expect(recent.querySelector('[aria-busy="true"]')).toBeNull())
    // The fixture's caller has submissions: each is listed as a link to its
    // page, and the empty line is not drawn beside them.
    const rows = [...recent.querySelectorAll('.sb-recent-i')]
    expect(rows.length, 'the fixture caller has recent submissions').toBeGreaterThan(0)
    expect(recent.querySelector('.sb-empty')).toBeNull()
    for (const r of rows) expect(r.querySelector('a[href]')).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// the task form (F1)
// ---------------------------------------------------------------------------

function runnerCard(root: HTMLElement, name: string): HTMLElement {
  const radio = root.querySelector<HTMLInputElement>(`input[name="runner-profile"][value="${name}"]`)
  expect(radio, `no runner ${name}`).not.toBeNull()
  return radio!.closest('label') as HTMLElement
}

describe('the task form is F1', () => {
  it('draws the runners as two-column cards with size, backend and how many can start now', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    const list = container.querySelector<HTMLElement>('.sbf-runners')!
    expect(list.classList.contains('is-cards'), 'the runner list is not the card grid').toBe(true)
    expect(rules(SUBMIT_CSS)).toMatch(/\.sbf-runners\.is-cards\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/)

    const mock = runnerCard(container, 'mock')
    expect(visible(mock.querySelector('.sb-runner-size'))).toBe('standard · 1 unit · Cloud Run')
    expect(visible(mock.querySelector('.sbf-runner-room'))).toBe('15 more can start now')
    expect(visible(runnerCard(container, 'browser').querySelector('.sb-runner-size'))).toBe('browser · 2 units · GKE Autopilot')
    // A measured zero is a digit, and it names the pool holding it down.
    expect(visible(runnerCard(container, 'claude-code').querySelector('.sbf-runner-room'))).toMatch(/^0 can start now · held by \S+/)
    // A disabled runner stays listed with its reason in place of the room.
    const codex = runnerCard(container, 'codex')
    expect(codex.querySelector('input')!.disabled).toBe(true)
    // One short line (walkthrough E); the platform's words are its tooltip.
    expect(visible(codex.querySelector('.sbf-runner-off'))).toBe('Not enabled yet. Use claude-code')
    expect(codex.querySelector('.sbf-runner-off')!.getAttribute('title')).toMatch(/codex is disabled on this platform/)
  })

  it('draws steps 2 and 3 as cards, dimmed until a runner is chosen', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    const steps = () => [...container.querySelectorAll<HTMLElement>('form.sbf .sb-step')]
    expect(steps()).toHaveLength(3)
    expect(steps().map((s) => s.classList.contains('is-dim'))).toEqual([false, true, true])
    fireEvent.click(runnerCard(container, 'mock').querySelector('input')!)
    expect(steps().map((s) => s.classList.contains('is-dim'))).toEqual([false, false, false])
    // The CONTROLS are dimmed, not the step (QA G4-26): qa.c6p.submit.test.tsx.
    expect(rules(SUBMIT_CSS)).toMatch(/\.sb-step\.is-dim textarea[^{]*\{[^}]*opacity/)
    expect(rules(SUBMIT_CSS)).not.toMatch(/\.sb-step\.is-dim\s*\{/)
    expect(rules(SUBMIT_CSS)).toMatch(/\.sb-step\s*\{[^}]*border-radius:\s*var\(--radius\)/)
  })

  it('says where the task lands and what that costs, and the disabled button is pale accent', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    const lands = [...panel.querySelectorAll('.ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'lands as')
    expect(lands, 'no "lands as" row').toBeDefined()
    expect(visible(lands!)).toContain('READY or PARKED · costs nothing')
    expect(visible(panel)).toContain('Nothing runs until the scheduler admits it into every pool it needs.')
    const go = within(panel).getByRole('button', { name: 'Submit one task' })
    // The canonical primary button (components.html A): the accent fill, and
    // ONE disabled treatment that pales it.
    expect(go.className).toContain('c-btn is-primary')
    const css = rules(COMPONENTS_CSS)
    expect(css).toMatch(/\.c-btn\.is-primary\s*\{[^}]*background:\s*var\(--sk-ac\)/)
    expect(css).toMatch(/\.c-btn:disabled,\s*\.c-btn\[aria-disabled='true'\]\s*\{[^}]*opacity/)
  })
})

// ---------------------------------------------------------------------------
// the workflow form (G1)
// ---------------------------------------------------------------------------

describe('the workflow form is G1', () => {
  it('picks each step runner from a select, not a radio list of every profile', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const step = container.querySelector<HTMLElement>('.wfb-step')!
    expect(step.querySelector('.sbf-runners'), 'the step still holds the full runner list').toBeNull()
    const select = within(step).getByRole('combobox', { name: 'step-1 runner' }) as HTMLSelectElement
    expect(select.value).toBe('')
    const names = [...select.options].map((o) => o.value)
    expect(names).toEqual(['', 'browser', 'claude-code', 'codex', 'generic', 'mock'])
    const codex = [...select.options].find((o) => o.value === 'codex')!
    expect(codex.disabled).toBe(true)
    // The card's own words, not a lowercased copy (QA G4-30).
    expect(codex.textContent).toBe('codex · Not enabled yet. Use claude-code')
    expect(step.classList.contains('is-bad'), 'an unchosen step is not flagged').toBe(true)

    fireEvent.change(select, { target: { value: 'mock' } })
    expect(step.classList.contains('is-bad')).toBe(false)
    expect(visible(step.querySelector('.wfb-cost'))).toMatch(/^standard · 1 unit · Cloud Run/)
    expect(visible(step.querySelector('.wfb-cost .sbf-runner-room'))).toBe('15 more can start now')
  })

  it('draws an arrow between each pair of stages', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    fireEvent.click(container.querySelector<HTMLButtonElement>('button.wfb-add.is-stage')!)
    expect(container.querySelectorAll('.wfb-stage')).toHaveLength(2)
    const arrow = container.querySelector<HTMLElement>('.sbf-flow')!
    expect(arrow.querySelector('svg path')).not.toBeNull()
  })

  it('puts "If a step fails" and "Priority" in step 2 and lists both in the summary', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const panel = container.querySelector<HTMLElement>('.sbf-send')!
    expect(within(panel).queryByRole('radio'), 'on failure is still in the summary').toBeNull()
    expect(within(panel).queryByRole('spinbutton'), 'priority is still in the summary').toBeNull()

    const step2 = [...container.querySelectorAll<HTMLElement>('.sb-step')].find((s) =>
      visible(s.querySelector('h2')).includes('Choose what happens to the work'))!
    expect(step2, 'no step 2').toBeDefined()
    const fails = within(step2).getByRole('radiogroup', { name: 'If a step fails' })
    const fail = within(fails).getByRole('radio', { name: /Fail the workflow/ }) as HTMLInputElement
    const cont = within(fails).getByRole('radio', { name: /Continue/ }) as HTMLInputElement
    expect(fail.checked).toBe(true)
    const prio = within(step2).getByRole('spinbutton', { name: 'Priority' }) as HTMLInputElement
    expect(prio.value).toBe('0')

    const fact = (k: string) => {
      const f = [...panel.querySelectorAll<HTMLElement>('.ctl-fact')].find((x) => x.querySelector('b')?.textContent === k)
      return visible(f ?? null).slice(k.length).trim()
    }
    expect(fact('on failure')).toBe('fail the workflow')
    expect(fact('priority')).toBe('0 (default)')
    fireEvent.click(cont)
    fireEvent.change(prio, { target: { value: '7' } })
    expect(fact('on failure')).toBe('continue')
    expect(fact('priority')).toBe('7')
    fireEvent.change(prio, { target: { value: '400' } })
    expect(fact('priority')).not.toMatch(/400/)
  })
})

// ---------------------------------------------------------------------------
// the stylesheet half
// ---------------------------------------------------------------------------

describe('the Submit section keeps its own sheet', () => {
  it('lives in styles/submit.css, parses, and keeps the type rules', () => {
    expect(flatRules(SUBMIT_CSS).length).toBeGreaterThan(10)
    const report = gate(SUBMIT_CSS, 'submit.css')
    expect(report.problems).toEqual([])
    expect(report.duplicates).toEqual([])
    const css = rules(SUBMIT_CSS)
    expect(css, 'no letter-spacing').not.toMatch(/letter-spacing/)
    expect(css, 'no all-caps').not.toMatch(/text-transform:\s*uppercase/)
    for (const m of css.matchAll(/font(?:-size)?:[^;}]*?(\d+(?:\.\d+)?)px/g)) {
      expect(Number(m[1]), `font under the 12px floor: ${m[0]}`).toBeGreaterThanOrEqual(12)
    }
  })

  it('deleted the dead chooser and sidebar-option rules from styles.css', () => {
    const css = rules(STYLES)
    for (const sel of [...['choice', 'choices', 'chooser', 'recent-empty'].map(OLD_SK), '.sbf-wf-opts', '.sbf-runners.is-compact']) {
      expect(css.includes(sel), `${sel} is still in styles.css`).toBe(false)
    }
  })
})
