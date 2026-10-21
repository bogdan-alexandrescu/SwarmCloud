// QAFIX3-WORKFLOWS, THE CHANGES TAB (part of #1680; visual QA run 3,
// wf_d598fa7bffdb41fc9d76, 2026-10-21, the fixture build of main at 51d99c53):
//
//   task-023 (every size)  the Changes tab's state chip sat flush against its
//          heading: `.ctl-mark` is a bordered pill, and the one space between
//          it and "no changes · nothing differed from the clone" read as no
//          gap at all -- the defect V141 (Overview) and V210 (Runs) fixed in
//          their own empty states. The mark now stands off its heading by
//          `--ctl-s1`, as theirs do.
//   dark-004 (the dark part)  in the files x steps matrix a step column the
//          filter leaves out was dimmed by `opacity: .45` on its whole head
//          and every cell, badges included. A `.ctl-mark` is 12px type and a
//          1px border in `--text-dim`/`--text-faint`/`--warn`; at 45% over
//          dark `--surface-2` the faint one measures about 2:1. The badges
//          now keep their ink; the step's name and its cells still recede.
//
// Every assertion names the mutation that turns it red.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { task as runTask } from './runfixture'

vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, loadArtifactContent: vi.fn(), loadAttempts: vi.fn() }
})

const { AgentChanges } = await import('../WorkflowChanges')

const ENVS: CascadeEnv[] = [
  { width: 1440, theme: 'light' },
  { width: 1440, theme: 'dark' },
  { width: 390, theme: 'light' },
  { width: 390, theme: 'dark' },
]
const at = (env: CascadeEnv) => `${env.width} ${env.theme}`

const hosts: HTMLElement[] = []
afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
})

describe('task-023: the Changes tab’s chip stands off its heading', () => {
  it('spaces the state mark from the heading words at every size', () => {
    const t = runTask({ id: 'task_nochange', state: 'SUCCEEDED', result_summary: { git: { base: 'abc', commits: [], dirty: [] } } as never })
    render(<AgentChanges task={t} attempts={[]} file={null} onFile={() => {}} />)
    const h3 = document.querySelector<HTMLElement>('.chg-state > h3')!
    expect(h3.textContent).toMatch(/no changes · nothing differed from the clone/)
    const mark = h3.querySelector<HTMLElement>(':scope > .ctl-mark')!
    expect(mark).not.toBeNull()
    for (const env of ENVS) {
      // MUTATION: drop the `.chg-state > h3 > .ctl-mark` rule and this is
      // null: the pill and one space, flush against the first word.
      expect(painted(mark, ['margin-right', 'margin'], env), at(env)).toBe('var(--ctl-s1)')
    }
  })
})

/** The product of `opacity` from `el` up to the table: what the reader sees of it. */
function seenOpacity(el: Element, env: CascadeEnv): number {
  let o = 1
  for (let at: Element | null = el; at !== null && !at.classList.contains('chg-mx-card'); at = at.parentElement) {
    const v = painted(at, 'opacity', env)
    if (v !== null) o *= Number.parseFloat(v)
  }
  return o
}

describe('dark-004: a dimmed column keeps its badges legible', () => {
  // The matrix's markup as WorkflowChanges.tsx `ChangesMatrix` draws a column
  // the step filter leaves out: its head (name, then the badge line) and a cell.
  const head = 'div.chg-mx-card table.chg-mx-table thead tr th.chg-mx-step.is-dim'
  it('does not fade the badge line of a column the filter leaves out', () => {
    for (const env of ENVS) {
      const mark = build(`${head} span.chg-mx-say i.ctl-mark.is-absent`, hosts)
      // MUTATION: put back `.chg-mx-table .is-dim { opacity: .45 }` on the
      // whole head and this is 0.45: the faint badge at ~2:1 in dark.
      expect(seenOpacity(mark, env), `badge at ${at(env)}`).toBe(1)
    }
  })

  it('still dims the column’s step name and its cells', () => {
    for (const env of ENVS) {
      const name = build(`${head} span.mono`, hosts)
      const cell = build('div.chg-mx-card table.chg-mx-table tbody tr td.is-dim span.chg-mx-cell', hosts)
      // MUTATION: drop the dimming altogether and these are 1: a filtered
      // matrix that no longer shows which columns the filter left out.
      expect(seenOpacity(name, env), `name at ${at(env)}`).toBeLessThan(1)
      expect(seenOpacity(cell, env), `cell at ${at(env)}`).toBeLessThan(1)
      // The control: a shown column is not dimmed at all.
      const shown = build('div.chg-mx-card table.chg-mx-table thead tr th.chg-mx-step span.mono', hosts)
      expect(seenOpacity(shown, env), `shown name at ${at(env)}`).toBe(1)
    }
  })
})
