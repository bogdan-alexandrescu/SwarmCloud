// THE INTAKE ROUTES (intake-tenants.html 1A, picked 2026-10-02): the issue
// form at /submit/issue and Work › Runs at /runs and /runs/<id>.
//
// A run's page is `work/runs?run=<id>` inside the router, as one workflow is
// `work/workflows?wf=<id>`, and `/runs/<id>` in the address bar. Without the
// Runs branch in `fromAddress`, a Work tail that matches no tab is read as a
// TASK id, so `/runs/run_x` would open an agent inspector for "runs/run_x".

import { describe, expect, it } from 'vitest'

import { SECTIONS, canonical, fromAddress } from '../App'
import { addressToPath, pathToAddress } from '../paths'

const work = () => SECTIONS.find((s) => s.id === 'work')!

describe('the Work section carries Runs and the issue form', () => {
  it('lists Runs after Workflows and the issue form after the workflow form', () => {
    const ids = work().tabs.map((t) => t.id)
    expect(ids).toContain('runs')
    expect(ids.indexOf('runs')).toBe(ids.indexOf('workflows') + 1)
    expect(ids).toContain('new-issue')
    expect(ids.indexOf('new-issue')).toBe(ids.indexOf('new-workflow') + 1)
    expect(work().tabs.find((t) => t.id === 'runs')!.label).toBe('Runs')
    expect(work().tabs.find((t) => t.id === 'new-issue')!.label).toBe('Submit from a GitHub issue')
  })
})

describe('paths', () => {
  it('spells the list, one run and the issue form as the mock-up does', () => {
    expect(addressToPath('work/runs')).toBe('/runs')
    expect(addressToPath('work/runs?run=run_4c1e09d2')).toBe('/runs/run_4c1e09d2')
    expect(addressToPath('work/new-issue')).toBe('/submit/issue')
  })

  it('reads them back to the same addresses', () => {
    expect(pathToAddress('/runs')?.address).toBe('work/runs')
    expect(pathToAddress('/runs/run_4c1e09d2')?.address).toBe('work/runs?run=run_4c1e09d2')
    expect(pathToAddress('/runs/run_4c1e09d2/')?.address).toBe('work/runs?run=run_4c1e09d2')
    expect(pathToAddress('/submit/issue')?.address).toBe('work/new-issue')
  })
})

describe('the router', () => {
  it('opens one run on the Runs tab, never as an agent', () => {
    const r = fromAddress('work/runs?run=run_4c1e09d2')
    expect(r.sectionId).toBe('work')
    expect(r.tab).toBe('runs')
    expect(r.taskId).toBeNull()
    expect(r.view).toBe('run=run_4c1e09d2')
    expect(canonical(r)).toBe('work/runs?run=run_4c1e09d2')
  })

  it('opens the list and the issue form', () => {
    expect(canonical(fromAddress('work/runs'))).toBe('work/runs')
    const f = fromAddress('work/new-issue')
    expect([f.sectionId, f.tab, f.taskId]).toEqual(['work', 'new-issue', null])
  })
})
