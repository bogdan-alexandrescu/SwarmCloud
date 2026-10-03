// PHONE ROWS AND TABLES KEEP THE FIELDS THAT TELL ROWS APART (#109), AND
// OVERVIEW NAMES A RUNNING AGENT BY ITS STEP (#94).
//
//   #94   Overview's Running rows showed `claude-code` over the task id, four
//         times for four steps of one workflow. The step is the name now; a
//         standalone task is named by its id (`agentName`), the profile is on
//         the sub-line (O1's "Agent · profile"), and the whole id is the
//         link's title.
//   #109  At 390 the workflow list scrolls sideways and its done, failed and
//         age columns sit off the right edge: `.wfl-phone` carries
//         `9/30 · 4✕ · 2h ago` under the name. The step Table drops runner and
//         inputs, keeps the step column held (`.is-scroll`, ≤899), and fades
//         its right edge with a mask while columns are off that edge. The
//         board-level Rows/Graph switch went with #432, so a workflow page has
//         ONE view switch and the list ONE state switch -- pinned here so a
//         third does not come back.
//
// MUTATIONS, one per block: name a RunningRow by `runner_profile` again; drop
// the failed count or the age from `phoneSummary`; delete the ≤560 runner /
// inputs rule; draw the fade unconditionally or never; add a `ctl-seg` to
// Workflows.tsx.

import STYLES from '../styles.css?raw'
import WORKFLOWS_SRC from '../Workflows.tsx?raw'
import WORKFLOW_VIEWS_SRC from '../WorkflowViews.tsx?raw'
import { describe, expect, it } from 'vitest'
import { act, fireEvent, render } from '@testing-library/react'

import { RunningRow } from '../Overview'
import { useMoreRight } from '../WorkflowViews'
import { phoneSummary } from '../workflowlist'
import type { Workflow } from '../types'
import { cascade, type CascadeEnv } from './cssgate'
import { task } from './runfixture'

const PHONE: CascadeEnv = { width: 390 }
const WIDE: CascadeEnv = { width: 1440 }

function display(el: Element, env: CascadeEnv): string | null {
  const r = cascade(STYLES, el, 'display', env)
  expect(r.unsupported).toEqual([])
  return r.winner?.value ?? null
}

describe('#94: an Overview running agent is named by its step', () => {
  function row(over: Parameters<typeof task>[0]) {
    const { container } = render(
      <table>
        <tbody>
          <RunningRow task={task({ state: 'RUNNING', ...over })} />
        </tbody>
      </table>,
    )
    return container.querySelector('th')!
  }

  it('reads the step, with the profile on the sub-line and the whole id as the title', () => {
    const th = row({ id: 'task_9c0ade75fd0c4f1c9a6e', runner_profile: 'codex', workflow_id: 'wf_one', step_id: 'scan-04' })
    const name = th.querySelector('a.ov-name')!
    expect(name.textContent).toBe('scan-04')
    // The whole name and the whole id (walkthrough C: the name is clamped to two lines).
    expect(name.getAttribute('title')).toBe('scan-04 · task_9c0ade75fd0c4f1c9a6e')
    expect(th.querySelector('.ov-sub')?.textContent).toBe('codex · wf_one')
  })

  it('names a standalone agent by what it is and its short id, with its profile under it', () => {
    // Walkthrough G (2026-10-03): `agentName`, as the list, the Timeline and the crumb name it.
    const th = row({ id: 'task_alone', runner_profile: 'claude-code', workflow_id: null, step_id: null })
    expect(th.querySelector('a.ov-name')?.textContent).toBe('claude-code task · alone')
    expect(th.querySelector('.ov-sub')?.textContent).toBe('claude-code')
  })
})

function wf(over: Partial<Workflow>): Pick<Workflow, 'steps' | 'rollup' | 'created_at'> {
  return {
    steps: Array.from({ length: 30 }, (_, i) => ({ step_id: `scan-${i}` })) as unknown as Workflow['steps'],
    created_at: '2026-10-02T10:00:00.000Z',
    rollup: {
      state: 'FAILED',
      complete: true,
      reason: '',
      counts: { SUCCEEDED: 9, FAILED: 4, CANCELLED: 17 },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: 30,
    },
    ...over,
  }
}

const NOW = Date.parse('2026-10-02T12:00:00.000Z')

describe('#109: a phone workflow row says how it ended and when', () => {
  it('reads done, failed and age in one line', () => {
    expect(phoneSummary(wf({}), NOW)).toBe('9/30 · 4✕ · 2h ago')
  })

  it('draws no failed count when nothing failed, never `0✕`', () => {
    const w = wf({})
    w.rollup = { ...w.rollup!, counts: { SUCCEEDED: 30 } }
    expect(phoneSummary(w, NOW)).toBe('30/30 · 2h ago')
  })

  it('keeps the step count alone over a partial census', () => {
    const w = wf({})
    w.rollup = { ...w.rollup!, complete: false }
    expect(phoneSummary(w, NOW)).toBe('30 steps · 2h ago')
  })

  it('is drawn at 390 and not at 1440, where the cells say it', () => {
    const el = document.createElement('span')
    el.className = 'wfl-phone'
    expect(display(el, PHONE)).toBe('block')
    expect(display(el, WIDE)).toBe('none')
  })
})

describe('#109: the phone step Table keeps the columns that compare rows', () => {
  function cell(col: string): HTMLElement {
    const wrap = document.createElement('div')
    wrap.className = 'ctl-table wf-table is-scroll'
    wrap.innerHTML = `<table><tbody><tr><td data-col="step">s</td><td data-col="${col}">x</td></tr></tbody></table>`
    return wrap.querySelector(`[data-col="${col}"]`)!
  }

  it('drops runner and inputs at 390 and keeps them at 1440', () => {
    for (const col of ['runner', 'inputs']) {
      expect(display(cell(col), PHONE)).toBe('none')
      expect(display(cell(col), WIDE)).not.toBe('none')
    }
    for (const col of ['state', 'why', 'waited', 'ran', 'cost']) {
      expect(display(cell(col), PHONE)).not.toBe('none')
    }
  })

  it('holds the step column in view at 390', () => {
    const step = cell('runner').parentElement!.querySelector('[data-col="step"]')!
    expect(cascade(STYLES, step, 'position', PHONE).winner?.value).toBe('sticky')
  })

  it('fades the right edge with a mask only while columns are off it', () => {
    const el = document.createElement('div')
    el.className = 'ctl-table wf-table is-scroll has-more'
    expect(cascade(STYLES, el, 'mask-image', PHONE).winner?.value).toMatch(/^linear-gradient\(to left/)
    el.className = 'ctl-table wf-table is-scroll'
    expect(cascade(STYLES, el, 'mask-image', PHONE).winner).toBeNull()
    expect(cascade(STYLES, el, 'background-attachment', PHONE).winner).toBeNull()
  })

  it('sets `has-more` from the scroller, and clears it at the right edge', () => {
    function Probe() {
      const m = useMoreRight<HTMLDivElement>()
      return <div ref={m.ref} data-testid="s" className={m.on ? 'has-more' : ''} />
    }
    const { getByTestId } = render(<Probe />)
    const s = getByTestId('s')
    Object.defineProperty(s, 'clientWidth', { value: 390, configurable: true })
    Object.defineProperty(s, 'scrollWidth', { value: 900, configurable: true })
    act(() => {
      fireEvent.scroll(s)
    })
    expect(s.classList.contains('has-more')).toBe(true)
    Object.defineProperty(s, 'scrollLeft', { value: 510, configurable: true })
    act(() => {
      fireEvent.scroll(s)
    })
    expect(s.classList.contains('has-more')).toBe(false)
  })
})

describe('#109: one mode switch per phone screen', () => {
  it('has no board-level Rows/Graph switch beside the per-workflow view switch', () => {
    const segs = [...`${WORKFLOWS_SRC}\n${WORKFLOW_VIEWS_SRC}`.matchAll(/<Segmented\s+className="([\w-]+)"/g)].map((m) => m[1])
    // The list's state filter, the page's Graph/Table/Timeline, and the
    // canvas zoom (Graph only, and only when there is something to zoom).
    expect(segs.sort()).toEqual(['wf-view-seg', 'wf-zoom-seg', 'wfl-seg'])
  })
})
