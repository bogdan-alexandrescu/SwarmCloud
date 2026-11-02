// QAFIX4-WORKFLOWS (part of #1680; visual QA run 4, wf_ecd935541ced4a539540,
// the fixture build of main at fabe4d6c, 1440 and 390 px, light and dark):
//
//   c28 (owner decision, 2026-10-22, box comment 6314117148)  at 1440 the
//          Workflows list's Cost cell read three lines, '$0.426' / '3 of 4' /
//          'reported'. From 1280 the column is 136px (128 until QAFIX5
//          work-a-010) and the coverage is one
//          line under the figure; below 1280 it keeps 100px and V163's wrap.
//   N17 (task-009)  the Changes empty state's heading was announced as the
//          chip's whole sentence: `Mark` is `role="img"` named by `say`, and
//          inside the h3 it made the sentence the heading's name, so the
//          visible 'not measured' was in no name at all.
//   N22 (flow-workflow-graph-light-05, dark-05)  the graph's key read
//          'order / staged / declared' and the zoom note '<fields> not
//          drawn': the meaning was only in titles and an accessible name.
//
// Every assertion names the mutation that turns it red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'

import type { Task, Workflow, WorkflowStep } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { task as runTask } from './runfixture'
import { cellStyle, fixedColumns, lengthPx, textPx } from './tablefit'

vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, loadArtifactContent: vi.fn(), loadAttempts: vi.fn() }
})

const { AgentChanges } = await import('../WorkflowChanges')
const { WorkflowCard } = await import('../Workflows')
const { Mark } = await import('../primitives')

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

function html(markup: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = markup
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

// ---------------------------------------------------------------------------
// c28: the Cost column from 1280
// ---------------------------------------------------------------------------

/** The list's head and one row with a partial cost, as Workflows.tsx draws them at 900 and up (Shape and Runners hidden below 1680). */
function list(): HTMLElement {
  const cols = ['state', 'workflow', 'done', 'cost', 'owner', 'submitted', 'duration']
  return html(
    '<div class="wfl"><div class="table-wrap is-scroll wfl-scroll"><table class="wfl-table"><thead><tr>' +
      cols.map((c) => `<th data-col="${c}">${c}</th>`).join('') +
      '</tr></thead><tbody><tr class="wfl-row">' +
      cols
        .map((c) =>
          c === 'cost'
            ? '<td class="num" data-col="cost"><span class="wf-spend">$0.426<span class="wf-spend-cov">3 of 4 reported</span></span></td>'
            : `<td data-col="${c}">x</td>`,
        )
        .join('') +
      '</tr></tbody></table></div></div>',
  )
}

/** The list's box: the viewport less the 84px spine, the 236px nav panel open, and two 32px gutters. */
const listAt = (viewport: number) => viewport - 84 - 236 - 64

describe('c28: from 1280 the Cost column holds "3 of 4 reported" on one line', () => {
  it('is 136px at 1440 and the coverage line does not wrap, and that is room enough for it', () => {
    const host = list()
    const env: CascadeEnv = { width: 1440 }
    const th = host.querySelector('th[data-col="cost"]')!
    const td = host.querySelector('td[data-col="cost"]')!
    const cov = host.querySelector('.wf-spend-cov')!
    // MUTATION: drop the 1280 block's `th[data-col="cost"] { width: 136px }`
    // and the head is 100px again. (128px until QAFIX5 work-a-010: see the
    // measurement below.)
    expect(painted(th, 'width', env)).toBe('136px')
    // Still its own line under the figure.
    expect(painted(cov, 'display', env)).toBe('block')
    // One line through width, not through nowrap: the coverage still wraps at
    // its spaces, and '3 of 4 reported' simply has room enough not to.
    // MUTATION: set the width under 128px (or the cell's padding over 10px):
    // the 15 characters of 12px mono no longer fit the content box, and the
    // coverage wraps to '3 of 4' / 'reported' again.
    // MEASURED ON THE COVERAGE, in the DM Mono `.wf-spend` draws it in: on the
    // cell it was measured in the sans face, 100.8px, and 128px passed with
    // no slack at all (QAFIX5 work-a-010, qafix5.workflows.test.tsx).
    const st = cellStyle(td, env)
    const room = (lengthPx(painted(th, 'width', env), 0) ?? 0) - st.pl - st.pr
    expect(room).toBeGreaterThanOrEqual(textPx('3 of 4 reported', cov, env))
  })

  it('never clips a longer coverage at 1440: "100 of 120 reported" wraps instead', () => {
    const host = list()
    const env: CascadeEnv = { width: 1440 }
    const th = host.querySelector('th[data-col="cost"]')!
    const td = host.querySelector('td[data-col="cost"]')!
    const cov = host.querySelector('.wf-spend-cov')!
    const st = cellStyle(td, env)
    const room = (lengthPx(painted(th, 'width', env), 0) ?? 0) - st.pl - st.pr
    // The premise: a long coverage is wider than the box, and the cell clips
    // what overflows it. A 12-step workflow's (R10) fits since the note is in
    // the body face (visual QA pass 2 ovag2-15, #2694), so the premise is a
    // three-figure count's.
    expect(textPx('100 of 120 reported', cov, env)).toBeGreaterThan(room)
    expect(painted(td, 'text-overflow', env)).toBe('clip')
    // MUTATION: put `.wf-spend-cov { white-space: nowrap }` in the 1280 block:
    // the coverage stays one 122px line in a 116px box and the clip takes
    // "reported", the word V163 and Q9 say is never cut.
    expect(painted(cov, 'white-space', env)).toBe('normal')
    // Wrapped at its spaces, every word fits: nothing of it is cut.
    // MUTATION: narrow the 1280 head under the widest word ('reported', 58px,
    // plus the cell's 20px of padding) and a word itself is clipped.
    for (const word of '100 of 120 reported'.split(' ')) {
      expect(textPx(word, cov, env)).toBeLessThanOrEqual(room)
    }
  })

  it('keeps 100px and V163’s wrap at 1100', () => {
    const host = list()
    const env: CascadeEnv = { width: 1100 }
    // MUTATION: put the 136px outside the 1280 query (in the 900 block) and
    // the narrow layout loses 36px it does not have.
    expect(painted(host.querySelector('th[data-col="cost"]')!, 'width', env)).toBe('100px')
    // MUTATION: put a nowrap on the coverage in the 900 block: '3 of 4
    // reported' is 108px in a 80px content box, and the cell's clip takes
    // "reported".
    expect(painted(host.querySelector('.wf-spend-cov')!, 'white-space', env)).toBe('normal')
  })

  it('leaves the name at least the 168px D8 and R10 hold, at 1280 with the nav open', () => {
    const host = list()
    const env: CascadeEnv = { width: 1280 }
    const { cols } = fixedColumns(host.querySelector('table')!, listAt(1280), env)
    const name = cols.find((c) => c.col === 'workflow')!
    // MUTATION: widen Cost (or any fixed column) by more than the 28px the
    // name has over its floor here: 896px less 700px of fixed columns is 196.
    expect(name.end - name.start).toBeGreaterThanOrEqual(168)
    expect(cols.find((c) => c.col === 'cost')!.end - cols.find((c) => c.col === 'cost')!.start).toBe(136)
  })
})

// ---------------------------------------------------------------------------
// N17: the Changes empty state's heading
// ---------------------------------------------------------------------------

describe('N17: the Changes empty state’s heading is named by what it shows', () => {
  const SAY = /The patch is written when an attempt ends/

  it('names the heading by its visible text, the chip’s word in it, and describes it by the sentence', () => {
    render(<AgentChanges task={runTask({ id: 'task_run', state: 'RUNNING', attempt_count: 1, result_summary: null })} attempts={[]} file={null} onFile={() => {}} />)
    // MUTATION: put `<Mark kind={mark} say={say} />` back in `State`: the
    // heading's name is the sentence plus the heading, and this finds nothing.
    const h3 = screen.getByRole('heading', { level: 3, name: 'not measured nothing written yet · running' })
    // The name is what a sighted reader sees, word for word.
    expect(h3.textContent!.replace(/\s+/g, ' ').trim()).toBe('not measured nothing written yet · running')
    // MUTATION: drop the chip's `aria-label`/word and the chip has no name
    // holding 'not measured'.
    const chip = within(h3).getByRole('img', { name: 'not measured' })
    expect(chip.classList.contains('ctl-mark')).toBe(true)
    expect(chip.classList.contains('is-absent')).toBe(true)
    // MUTATION: drop `aria-describedby` from the h3 and the sentence is in no
    // description.
    const by = h3.getAttribute('aria-describedby')
    expect(by).not.toBeNull()
    expect(document.getElementById(by!)?.textContent).toMatch(SAY)
    // MUTATION: drop the chip's `title`: the sighted reader loses the sentence.
    expect(chip.getAttribute('title')).toMatch(SAY)
  })

  it('draws, for every kind a state uses, the word `Mark` draws', () => {
    // A state draws its own chip because `Mark` names it by the sentence;
    // the words must not drift from Mark's.
    const kinds = ['zero', 'absent', 'unread', 'partial'] as const
    const shown = new Map<string, string>()
    for (const kind of kinds) {
      const { container, unmount } = render(<Mark kind={kind} say="s" />)
      shown.set(kind, container.textContent ?? '')
      unmount()
    }
    // A measured zero (no changes), a running agent (not measured): two of
    // the four, through `State` itself.
    const zero: Task = runTask({ id: 'task_zero', state: 'SUCCEEDED', result_summary: { git: { base: 'abc', commits: [], dirty: [] } } as never })
    render(<AgentChanges task={zero} attempts={[]} file={null} onFile={() => {}} />)
    // MUTATION: change STATE_WORD.zero in WorkflowChanges.tsx.
    expect(document.querySelector('.chg-state > h3 > .ctl-mark.is-zero')!.textContent).toBe(shown.get('zero'))
    cleanup()
    render(<AgentChanges task={runTask({ id: 'task_run', state: 'RUNNING', attempt_count: 1, result_summary: null })} attempts={[]} file={null} onFile={() => {}} />)
    // MUTATION: change STATE_WORD.absent in WorkflowChanges.tsx.
    expect(document.querySelector('.chg-state > h3 > .ctl-mark.is-absent')!.textContent).toBe(shown.get('absent'))
  })
})

// ---------------------------------------------------------------------------
// N22: the graph's key and the zoom note in plain words
// ---------------------------------------------------------------------------

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function workflow(steps: WorkflowStep[]): Workflow {
  const at = new Date(Date.now() - 600_000).toISOString()
  return {
    workflow_id: 'wf_qafix4',
    state: 'RUNNING',
    tenant_id: 'u-bogdan',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts: { unstarted: steps.length },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: at,
    updated_at: at,
    submitted_by: 'alice@example.com',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  } as Workflow
}

function graph(zoom: 'auto' | 'names' | 'details') {
  const w = workflow([step('plan', []), step('build', ['plan'], { input_from: { plan: 'plan.md' } }), step('ship', ['build'])])
  return render(
    <WorkflowCard
      workflow={w}
      taskById={new Map()}
      zoom={zoom}
      usage={{ kind: 'ready', usage: null }}
      openStages={{}}
      onToggleStage={() => {}}
      reload={() => {}}
    />,
  )
}

describe('N22: the graph says what its edges mean and why figures are hidden, in words on screen', () => {
  it('reads the edge kinds as what they mean, the sentences kept whole in the titles', () => {
    graph('auto')
    const key = screen.getByRole('list', { name: 'Edge key' })
    const items = within(key).getAllByRole('listitem')
    // MUTATION: put back 'order' / 'staged' / 'declared' in EDGE_KEY.
    expect(items.map((li) => li.querySelector('.wf-key-word')?.textContent)).toEqual(['runs after', 'file passed on', 'file expected, not arrived'])
    // The titles stay whole sentences naming the kind.
    expect(items[0]!.getAttribute('title')).toMatch(/^Thin: an order dependency\. .+\.$/)
    expect(items[1]!.getAttribute('title')).toMatch(/^Heavy, solid: a file staged\. .+\.$/)
    expect(items[2]!.getAttribute('title')).toMatch(/^Heavy, dashed: a file declared and not yet reported staged\. .+\.$/)
  })

  it('says the zoom hid the fields and how to see them', () => {
    graph('names')
    const note = document.querySelector<HTMLElement>('.wf-zoom .wf-zoom-note')!
    // MUTATION: put back `{list} not drawn` in ZoomNotice.
    expect(note.textContent).toBe('profile, duration and figures hidden at this zoom. Choose Figures, or open a step, to see them.')
    // The name and title stay the whole sentence.
    expect(note.getAttribute('aria-label')).toMatch(/^This canvas is drawn at the Names zoom, which you chose, .+ to see them\.$/)
    expect(note.getAttribute('title')).toBe(note.getAttribute('aria-label'))
  })

  it('lets the longer note shrink to its row and wrap', () => {
    const note = html('<div class="wf-zoom"><span class="wf-caveat wf-zoom-note">n</span></div>').querySelector('.wf-zoom-note')!
    for (const width of [390, 1440]) {
      // MUTATION: drop the `.wf-zoom > .wf-caveat.wf-zoom-note` rule: the
      // strip's `flex: none` runs the note past a 390px column.
      expect(painted(note, ['flex', 'flex-shrink'], { width }), `${width}`).toBe('0 1 auto')
    }
  })
})
