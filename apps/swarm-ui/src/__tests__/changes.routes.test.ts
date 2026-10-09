// THE CHANGES TAB'S ADDRESSES (docs/design/diff-viewer.md §2 variant 2, lane
// DIFF2a): `/agents/<tab>/<id>/changes`, with the open file as one more
// encoded segment; `/workflows/<id>/changes`; `/runs/<id>/changes`.
//
// Each is a path a person copies, so each must survive the round trip
// path -> address -> route -> canonical address -> path unchanged. The two
// failures this guards are the quiet ones: `/runs/<id>/changes` read as a run
// called `<id>/changes` (the Runs branch joined every segment into the id),
// and `/workflows/<id>/changes` dropped to the Graph by a tab list that did not
// know the name.
//
// MUTATIONS: drop `changes` from `PANE_SEGMENTS` (App.tsx) -- the agent cases
// go red, the pane reads as part of the task id; drop `RUN_PANES` from the
// Runs branch of `pathToAddress` -- the run id carries `/changes`; drop
// `changes` from `WORKFLOW_TABS` -- the workflow lands on its Graph.

import { describe, expect, it } from 'vitest'

import { canonical, fromAddress } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { parseWorkflowQuery, workflowHref, workflowQueryString } from '../workflowlist'

const ID = 'task_0123456789abcdef0123'

/** path -> route -> path, the way the router writes the bar back (App `pathFor`, list query aside). */
function roundTrip(path: string, agentTab: 'live' | 'recent' = 'recent'): string {
  const p = pathToAddress(path)
  expect(p, `${path} is not a path this console serves`).not.toBeNull()
  return addressToPath(canonical(fromAddress(p!.address)), agentTab)
}

describe('an agent’s Changes tab', () => {
  it('is a pane of the agent, not part of its id', () => {
    const p = pathToAddress(`/agents/recent/${ID}/changes`)!
    expect(p.address).toBe(`work/task/${ID}/changes`)
    expect(p.agentTab).toBe('recent')
    const r = fromAddress(p.address)
    expect([r.sectionId, r.tab, r.taskId, r.taskPane]).toEqual(['work', 'running', ID, 'changes'])
    expect(r.file ?? null).toBeNull()
    expect(roundTrip(`/agents/recent/${ID}/changes`)).toBe(`/agents/recent/${ID}/changes`)
  })

  it('names the open file as one encoded segment, and gives it back decoded', () => {
    const path = `/agents/recent/${ID}/changes/${encodeURIComponent('src/app/config.ts')}`
    expect(path).toContain('/changes/src%2Fapp%2Fconfig.ts')
    const r = fromAddress(pathToAddress(path)!.address)
    expect(r.taskId).toBe(ID)
    expect(r.taskPane).toBe('changes')
    expect(r.file).toBe('src/app/config.ts')
    expect(roundTrip(path)).toBe(path)
  })

  it('takes a file named like a pane as a file, and leaves the Artifacts output alone', () => {
    const r = fromAddress(`work/task/${ID}/changes/logs`)
    expect([r.taskId, r.taskPane, r.file]).toEqual([ID, 'changes', 'logs'])
    const a = fromAddress(`work/task/${ID}/artifacts/changes`)
    expect([a.taskId, a.taskPane, a.artifact, a.file ?? null]).toEqual([ID, 'artifacts', 'changes', null])
  })

  it('drops the file from every pane but Changes', () => {
    expect(canonical({ ...fromAddress(`work/task/${ID}/logs`), file: 'a.ts' })).toBe(`work/task/${ID}/logs`)
  })
})

describe('a workflow’s Changes tab', () => {
  it('is `/workflows/<id>/changes`, both ways', () => {
    const p = pathToAddress('/workflows/wf_broker/changes')!
    expect(p.address).toBe('work/workflows?wf=wf_broker&tab=changes')
    expect(parseWorkflowQuery(fromAddress(p.address).view).tab).toBe('changes')
    expect(roundTrip('/workflows/wf_broker/changes')).toBe('/workflows/wf_broker/changes')
  })

  it('is written by the workflow’s own query and href', () => {
    const q = parseWorkflowQuery('wf=wf_broker&tab=changes&state=failed')
    expect(workflowQueryString(q)).toBe('wf=wf_broker&tab=changes&state=failed')
    expect(workflowHref('wf_broker', q, 'changes')).toBe('/workflows/wf_broker/changes?state=failed')
  })
})

describe('an issue run’s Changes tab', () => {
  it('is `/runs/<id>/changes`, both ways, and the run’s id stays its id', () => {
    const p = pathToAddress('/runs/run_4c1e09d2/changes')!
    expect(p.address).toBe('work/runs?run=run_4c1e09d2&tab=changes')
    const r = fromAddress(p.address)
    expect([r.tab, r.taskId]).toEqual(['runs', null])
    expect(new URLSearchParams(r.view ?? '').get('run')).toBe('run_4c1e09d2')
    expect(roundTrip('/runs/run_4c1e09d2/changes')).toBe('/runs/run_4c1e09d2/changes')
  })

  it('leaves the run’s page where it was', () => {
    expect(pathToAddress('/runs/run_4c1e09d2')?.address).toBe('work/runs?run=run_4c1e09d2')
    expect(roundTrip('/runs/run_4c1e09d2')).toBe('/runs/run_4c1e09d2')
    // A tab this console does not know is not written into the path.
    expect(addressToPath('work/runs?run=run_4c1e09d2&tab=bogus')).toBe('/runs/run_4c1e09d2')
  })
})
