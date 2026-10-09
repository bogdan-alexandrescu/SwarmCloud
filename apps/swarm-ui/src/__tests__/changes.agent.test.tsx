// THE AGENT'S CHANGES TAB (docs/design/diff-viewer.md §2 variant 2, owner
// decision 2026-10-08; lane DIFF2a).
//
// The shipped viewer at the pane's full height, beside Details and Logs, with:
//
//   * a count that is the file count (`git.files`), a measured 0 for an agent
//     that changed nothing, and a DASH with its reason otherwise;
//   * the list folded to its strip on open -- in memory, never written to the
//     device -- and given back its width on leaving, unless the reader moved it;
//   * the open file in the address both ways: the route opens it, and each
//     file the reader opens is reported for the route to write;
//   * the states, none of which reads as zero: withheld (no read of the patch
//     at all), discarded over the cap, nothing written yet, a failed read
//     with Retry, and a measured zero.
//
// MUTATIONS, one per block: count a summary with no `git.files` as 0; write
// the fold with `remember` true (or never undo it); drop `initialFile` from
// `PatchChanges`; read the patch before `publishRefusals` is consulted, or
// filter refusals on the latest attempt only; draw `patch_omitted` as no
// changes.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TaskPane } from '../App'
import type { Result } from '../fetch'
import type { ArtifactContent, GitSummary, Task, TaskPage } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadArtifactContent: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { resetListSnap, setListSnap } = await import('../listSnap')
const { AgentChanges, changesCount } = await import('../WorkflowChanges')

const ID = 'task_0123456789abcdef0123'
const PATCH_NAME = 'swarm-work.patch'

const PATCH = [
  'diff --git a/src/app/config.ts b/src/app/config.ts',
  'index 1111111..2222222 100644',
  '--- a/src/app/config.ts',
  '+++ b/src/app/config.ts',
  '@@ -1,2 +1,2 @@',
  ' export const region = 1',
  '-export const zone = 1',
  '+export const zone = 2',
  'diff --git a/docs/notes.md b/docs/notes.md',
  'new file mode 100644',
  'index 0000000..8888888',
  '--- /dev/null',
  '+++ b/docs/notes.md',
  '@@ -0,0 +1,1 @@',
  '+# Notes',
  '',
].join('\n')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function served(over: Partial<ArtifactContent> = {}): ArtifactContent {
  return {
    task_id: ID,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    artifact: { name: PATCH_NAME, bytes: PATCH.length, uri: null },
    status: 'ok',
    detail: null,
    key: 'k',
    uri: null,
    content: PATCH,
    total_bytes: PATCH.length,
    offset: 0,
    returned_bytes: PATCH.length,
    next_offset: null,
    truncated: false,
    redacted: true,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
    ...over,
  }
}

function agent(git: GitSummary | null, over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'SUCCEEDED',
    attempt_count: 1,
    result_summary: git === null ? null : { git, artifacts: [{ name: PATCH_NAME, bytes: PATCH.length, uri: 'gs://b/p' }] },
    ...over,
  })
}

const WITH_PATCH: GitSummary = { base: '9c4e0b2abcdef0', patch: PATCH_NAME, pull_request: { number: 553, url: 'https://github.com/example/swarm/pull/553', state: 'open', created: true } }

/** The split under a router of its own: `.../changes/<file>` is the pane and the file. */
function Routed({ start, file: first = null, onGo }: { start: TaskPane; file?: string | null; onGo?: (to: string) => void }) {
  const [at, setAt] = useState<{ pane: TaskPane; file: string | null }>({ pane: start, file: first })
  const go = (to: string) => {
    onGo?.(to)
    const rest = to.split(`work/task/${ID}`)[1] ?? ''
    const [, pane = 'detail', file] = rest.split('/')
    setAt({ pane: (pane || 'detail') as TaskPane, file: file === undefined ? null : decodeURIComponent(file) })
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={at.pane} artifact={null} file={at.file} closeTo="work/running/recent" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

function tab(name: string): HTMLElement {
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  const t = within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === name)
  expect(t, `no ${name} tab`).toBeTruthy()
  return t!
}

function openFile(): string | null {
  return screen.getByRole('region', { name: 'Diff lines' }).getAttribute('data-open-file')
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  vi.stubGlobal('innerWidth', 1280)
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
  api.loadArtifactContent.mockResolvedValue(ok(served()))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  delete document.documentElement.dataset.agentList
})

describe('the tab: beside Logs, counted by the file count, a dash where nobody counted', () => {
  it('sits after Logs and counts git.files', async () => {
    const files = [
      { path: 'a.ts', old_path: null, status: 'M' as const, insertions: 1, deletions: 0, binary: false },
      { path: 'b.ts', old_path: null, status: 'A' as const, insertions: 2, deletions: 0, binary: false },
    ]
    api.loadTask.mockResolvedValue(ok(agent({ ...WITH_PATCH, files })))
    render(<Routed start="attempts" />)
    await waitFor(() => expect(tab('Changes').querySelector('.c-tabs em')?.textContent).toBe('2'))
    const names = [...document.querySelectorAll('.c-tabs[role="tablist"] .c-tab-label')].map((t) => t.textContent)
    expect(names.slice(0, 3)).toEqual(['Details', 'Logs', 'Changes'])
  })

  it('is a dash with its reason, never 0, for a patch whose files the worker did not count', () => {
    const c = changesCount(agent(WITH_PATCH))
    expect(c.count).toBeNull()
    expect(c.say).toMatch(/git\.files/)
    expect(changesCount(agent(null, { state: 'RUNNING' })).count).toBeNull()
    expect(changesCount(agent({ base: 'abc', patch_omitted: true, patch_bytes: 18_402_113 })).count).toBeNull()
  })

  it('is a measured 0 for an agent that changed nothing, and says a capped list is capped', () => {
    expect(changesCount(agent({ base: 'abc', commits: [], dirty: [] }))).toEqual({ count: 0, say: null })
    const files = Array.from({ length: 3 }, (_, i) => ({ path: `f${i}`, old_path: null, status: 'M' as const, insertions: 1, deletions: 1, binary: false }))
    expect(changesCount(agent({ ...WITH_PATCH, files, files_truncated: true })).count).toBe('3+')
  })
})

describe('opening it folds the list to the strip: remembered, not forced', () => {
  it('folds on open without writing the device’s width, and gives the width back on leaving', async () => {
    api.loadTask.mockResolvedValue(ok(agent(WITH_PATCH)))
    setListSnap('half')
    render(<Routed start="detail" />)
    expect(document.documentElement.dataset.agentList).toBe('half')
    fireEvent.click(tab('Changes'))
    await waitFor(() => expect(document.documentElement.dataset.agentList).toBe('strip'))
    expect(localStorage.getItem('swarm.agents.list'), 'the fold was written to the device').toBe('half')
    fireEvent.click(tab('Details'))
    await waitFor(() => expect(document.documentElement.dataset.agentList).toBe('half'))
  })

  it('keeps the width a reader chose while on Changes', async () => {
    api.loadTask.mockResolvedValue(ok(agent(WITH_PATCH)))
    render(<Routed start="changes" />)
    await waitFor(() => expect(document.documentElement.dataset.agentList).toBe('strip'))
    fireEvent.keyDown(screen.getByRole('separator', { name: 'Resize the agent list' }), { key: 'ArrowRight' })
    expect(document.documentElement.dataset.agentList).toBe('list')
    fireEvent.click(tab('Logs'))
    expect(document.documentElement.dataset.agentList).toBe('list')
  })
})

describe('the patch, at full height, with the open file in the address', () => {
  it('reads the patch by its manifest name and opens the file the route names', async () => {
    api.loadTask.mockResolvedValue(ok(agent(WITH_PATCH)))
    render(<Routed start="changes" file="docs/notes.md" />)
    await waitFor(() => expect(openFile()).toBe('docs/notes.md'))
    expect(api.loadArtifactContent).toHaveBeenCalledWith(ID, PATCH_NAME)
    expect(document.querySelector('.ag-split-pane.is-changes .diff.is-full'), 'the viewer is not full height').not.toBeNull()
    // The summary line: the base, the patch, and the PR in the viewer's bar.
    const sum = document.querySelector('.chg-sum')!
    expect(sum.textContent).toMatch(/base 9c4e0b2abc → work tree/)
    expect(sum.textContent).toMatch(/swarm-work\.patch/)
    expect(document.querySelector<HTMLAnchorElement>('a.diff-pr')?.getAttribute('href')).toBe('https://github.com/example/swarm/pull/553')
  })

  it('writes each file the reader opens into the address', async () => {
    api.loadTask.mockResolvedValue(ok(agent(WITH_PATCH)))
    const onGo = vi.fn()
    render(<Routed start="changes" onGo={onGo} />)
    await waitFor(() => expect(openFile()).toBe('src/app/config.ts'))
    const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
    const row = [...nav.querySelectorAll<HTMLElement>('button.diff-file')].find((b) => b.getAttribute('data-path') === 'docs/notes.md')!
    fireEvent.click(row)
    expect(onGo).toHaveBeenLastCalledWith(`work/task/${ID}/changes/${encodeURIComponent('docs/notes.md')}`)
    await waitFor(() => expect(openFile()).toBe('docs/notes.md'))
  })

  it('is reached from the Code card’s `Changes ›`', async () => {
    api.loadTask.mockResolvedValue(ok(agent({ ...WITH_PATCH, published: false, publish_reason: 'collect', strategy: 'collect' })))
    api.loadAgentRun.mockResolvedValue(
      ok({ task: agent({ ...WITH_PATCH, published: false, publish_reason: 'collect', strategy: 'collect' }), events: [], eventsDetail: null, attempts: [attempt(1)], attemptsDetail: null, classes: null, classesDetail: null, classesRouteMissing: false }),
    )
    const onGo = vi.fn()
    render(<Routed start="detail" onGo={onGo} />)
    fireEvent.click(await screen.findByRole('link', { name: 'Changes ›' }))
    expect(onGo).toHaveBeenLastCalledWith(`work/task/${ID}/changes`)
    await waitFor(() => expect(tab('Changes').getAttribute('aria-selected')).toBe('true'))
  })
})

describe('the states: none of them reads as no changes', () => {
  const credential = (file: string) => `the final tree adds a credential in ${file} (rule R1, line 3); remove it`

  it('withholds the patch on a credential refusal in ANY attempt, and never reads it', () => {
    const attempts = [attempt(1, { error: credential('config/test.env') }), attempt(2)]
    render(<AgentChanges task={agent(WITH_PATCH, { attempt_count: 2 })} attempts={attempts} file={null} onFile={() => {}} />)
    expect(screen.getByRole('status').textContent).toMatch(/withheld · a file failed the credential scan/)
    expect(screen.getByRole('status').textContent).toMatch(/config\/test\.env/)
    expect(api.loadArtifactContent, 'the refused patch was read').not.toHaveBeenCalled()
    expect(document.querySelector('.diff')).toBeNull()
  })

  it('waits for the attempts before it reads the patch', async () => {
    let answer: (r: Result<{ attempts: ReturnType<typeof attempt>[] }>) => void = () => {}
    api.loadAttempts.mockReturnValue(new Promise((r) => (answer = r)))
    api.loadTask.mockResolvedValue(ok(agent(WITH_PATCH)))
    render(<Routed start="changes" />)
    await waitFor(() => expect(api.loadAttempts).toHaveBeenCalled())
    expect(api.loadArtifactContent).not.toHaveBeenCalled()
    answer(ok({ attempts: [attempt(1, { error: credential('a.env') })] }))
    await waitFor(() => expect(screen.getByText(/withheld/)).toBeTruthy())
    expect(api.loadArtifactContent).not.toHaveBeenCalled()
  })

  it('says a patch over the cap was discarded, at its size', () => {
    render(<AgentChanges task={agent({ base: 'abc', patch_omitted: true, patch_bytes: 18_402_113 })} attempts={[]} file={null} onFile={() => {}} />)
    expect(screen.getByRole('status').textContent).toMatch(/patch discarded at 18[,.]?402[,.]?113 bytes/)
  })

  it('says a running agent has written nothing yet', () => {
    render(<AgentChanges task={agent(null, { state: 'RUNNING' })} attempts={[]} file={null} onFile={() => {}} />)
    expect(screen.getByRole('status').textContent).toMatch(/nothing written yet · running/i)
  })

  it('draws a measured zero for an agent that changed nothing', () => {
    render(<AgentChanges task={agent({ base: 'abc', commits: [], dirty: [] })} attempts={[]} file={null} onFile={() => {}} />)
    expect(document.querySelector('.chg-state h3')?.textContent).toMatch(/no changes · nothing differed from the clone/)
    expect(document.querySelector('.chg-state .ctl-mark.is-zero')).not.toBeNull()
  })

  it('says a failed read is not read, with its status, and reads again on Retry', async () => {
    api.loadArtifactContent.mockResolvedValueOnce({ status: 'error', error: { kind: 'server_error', httpStatus: 503, code: null, message: 'unavailable' } })
    render(<AgentChanges task={agent(WITH_PATCH)} attempts={[]} file={null} onFile={() => {}} />)
    await waitFor(() => expect(screen.getByRole('status').textContent).toMatch(/patch not read · 503/))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Diff lines' })).toBeTruthy())
    expect(api.loadArtifactContent).toHaveBeenCalledTimes(2)
  })

  it('reads a diff a workflow step handed on, and says it was handed on', async () => {
    const t = runTask({
      id: ID,
      state: 'SUCCEEDED',
      workflow_id: 'wf_a',
      step_id: 'implement',
      result_summary: { git: { base: 'abc', commits: [], dirty: [] }, artifacts: [{ name: 'change.diff', bytes: 10, uri: 'gs://b/c' }] },
    })
    render(<AgentChanges task={t} attempts={[]} file={null} onFile={() => {}} />)
    await waitFor(() => expect(screen.getByRole('region', { name: 'Diff lines' })).toBeTruthy())
    expect(api.loadArtifactContent).toHaveBeenCalledWith(ID, 'change.diff')
    expect(document.querySelector('.chg-sum')?.textContent).toMatch(/handed on, not committed/)
    expect(changesCount(t).count, 'a handed-on diff counted as no changes').toBeNull()
  })
})
