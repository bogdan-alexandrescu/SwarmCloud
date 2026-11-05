// LOGS › TRANSCRIPT IS A TRACE ON THE AGENT PAGE (owner pick 2026-11-05, part
// of #2688): a numbered timeline of every recorded event beside the picked one
// in full. One describe per state the picked frames draw: the finished coding
// lane at rest, a failure opened with an edit kept open, the browser lane, a
// failed run jumped to its error, a live run, and the narrow pane (1280 with
// the list back, and a phone). MUTATIONS are named at each assertion.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import { listSnap, resetListSnap, setListSnap } from '../listSnap'
import type { Task, TaskLogs, TaskTranscript } from '../types'
import { task as runTask } from './runfixture'
import { CWD, call, codingRecords, init, progress, reply, result, say, stepsOf, transcriptOf } from './tracefixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadArtifactListing: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentLogs } = await import('../AgentLogs')

const ID = 'task_trace'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function agent(over: Partial<Task> = {}): Task {
  return runTask({ id: ID, state: 'SUCCEEDED', runner_profile: 'claude-code', attempt_count: 1, ...over })
}

/** Drawn in the agent page's Logs pane, where the Transcript view is the trace. */
function inPane(task: Task) {
  return render(
    <div className="ag-split-pane is-logs">
      <AgentLogs task={task} />
    </div>,
  )
}

function serve(t: TaskTranscript) {
  api.loadTranscript.mockResolvedValue(ok(t))
}

const rows = () => [...document.querySelectorAll<HTMLButtonElement>('.trc-row')]
const picked = () => document.querySelector<HTMLElement>('.trc-row[aria-current="true"]')
const detail = () => document.querySelector<HTMLElement>('.trc-right')!

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState(null, '', `/work/task/${ID}/logs`)
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTaskLogs.mockResolvedValue(
    ok({
      task_id: ID,
      tenant_id: 'eng',
      attempt_id: 'att_1',
      attempt: { status: 'latest', known: true, generation: 1, created_at: new Date().toISOString(), completed_at: new Date().toISOString(), exit_code: 0 },
      read_at: new Date().toISOString(),
      streams: [],
      prefix: `tenants/eng/tasks/${ID}/attempts/att_1/`,
      redaction: { applied_at_read_time: true, rules: 12 },
    } as TaskLogs),
  )
  api.loadArtifactListing.mockResolvedValue(ok({ task_id: ID, artifacts: [], artifacts_skipped: [], artifact_bytes: 0, complete: true }))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the finished coding lane at rest (tr-b-coding-rest)', () => {
  beforeEach(() => serve(transcriptOf(stepsOf(codingRecords()))))

  it('reads the transcript with its records, and draws one numbered row per event', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    // Mutation: a read without include_raw has no cwd, no progress seconds, no patch.
    expect(api.loadTranscript).toHaveBeenCalledWith(ID, expect.objectContaining({ includeRaw: true }))
    expect(rows().map((r) => r.querySelector('.trc-n')!.textContent)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11'])
    // The progress records fold into their call as a count, with the lower-bound seconds.
    const test = rows()[4]!
    expect(test.querySelector('.trc-prog')!.textContent).toBe('2')
    expect(test.querySelector('.trc-dur')!.textContent).toBe('≥12s')
    expect(test.querySelector('.trc-sub')!.textContent).toBe('1 failed · 11 passed')
    expect(test.querySelector('.trc-st')!.getAttribute('aria-label')).toBe('error')
    // A call with no progress record shows no duration.
    expect(rows()[2]!.querySelector('.trc-dur')!.textContent).toBe('')
    // The old stream view is not drawn.
    expect(document.querySelector('.ag-logs-body')).toBeNull()
  })

  it('opens on the last message, and the result row points back to it instead of drawing the answer twice', async () => {
    inPane(agent())
    await waitFor(() => expect(picked()?.textContent).toContain('Done'))
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('10')
    expect(within(detail()).getAllByText(/drops a stale generation/)).toHaveLength(1)
    fireEvent.click(rows()[10]!)
    // Mutation: drawing the result's text here would show the answer a second time.
    const back = within(detail()).getByRole('button', { name: 'same as #10, the last message ↑' })
    expect(within(detail()).queryByText(/drops a stale generation/)).toBeNull()
    expect(within(detail()).getAllByText('2m 4s').length).toBeGreaterThan(0)
    fireEvent.click(back)
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('10')
  })

  it('shows the working directory once, in a tooltip, and never in a command', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    const home = document.querySelector<HTMLElement>('.trc-cwd')!
    expect(home.textContent!.trim()).toBe('repo')
    expect(home.getAttribute('title')).toContain(CWD)
    fireEvent.click(rows()[4]!)
    const cmd = detail().querySelector('.trc-cmd')!
    expect(cmd.textContent).toBe('uv run pytest tests/unit/test_reconciler.py -q')
    expect(document.querySelector('.trc-list')!.textContent).not.toContain(CWD)
  })

  it('counts each filter chip, and a chip shows only its rows', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    const chip = (name: RegExp) => screen.getByRole('button', { name })
    expect(chip(/^All/).textContent).toBe('All 11')
    expect(chip(/^Messages/).textContent).toBe('Messages 2')
    expect(chip(/^Tools/).textContent).toBe('Tools 5')
    expect(chip(/^Edits/).textContent).toBe('Edits 2')
    expect(chip(/^Errors/).textContent).toBe('Errors 1')
    fireEvent.click(chip(/^Edits/))
    expect(rows().map((r) => r.querySelector('.trc-l')!.textContent)).toEqual(['Edit reconciler.py', 'Write pr-body.md'])
  })

  it('marks the rows a search matches, and Enter steps through them', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    const box = screen.getByRole('searchbox', { name: 'Search this window' })
    fireEvent.change(box, { target: { value: 'pytest' } })
    const hits = rows().filter((r) => r.classList.contains('is-hit'))
    expect(hits.map((r) => r.querySelector('.trc-n')!.textContent)).toEqual(['5', '6'])
    expect(rows().filter((r) => r.classList.contains('is-miss'))).toHaveLength(9)
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('5')
    expect(document.querySelector('.trc-count')!.textContent).toBe('1/2')
    expect(detail().querySelector('mark')!.textContent).toBe('pytest')
  })

  it('steps with j and k', async () => {
    inPane(agent())
    await waitFor(() => expect(picked()).not.toBeNull())
    fireEvent.keyDown(window, { key: 'k' })
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('9')
    fireEvent.keyDown(window, { key: 'j' })
    fireEvent.keyDown(window, { key: 'j' })
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('11')
  })

  it('shows an event it does not recognise as recorded', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    fireEvent.click(rows()[7]!)
    expect(rows()[7]!.textContent).toContain('auth_status')
    expect(detail().querySelector('.trc-raw')!.textContent).toContain('"isAuthenticating": false')
  })

  it('keeps the stream switch, and the other streams are drawn as before', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    fireEvent.click(screen.getByRole('button', { name: /Agent stderr/ }))
    await waitFor(() => expect(document.querySelector('.ag-logs-body')).not.toBeNull())
    expect(document.querySelector('.trc')).toBeNull()
  })
})

describe('a failure opened, an edit kept open (tr-b-coding-open)', () => {
  beforeEach(() => serve(transcriptOf(stepsOf(codingRecords()))))

  it('draws an edit as a diff with its line numbers and a button to the Changes tab', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    fireEvent.click(rows()[3]!)
    const diff = detail().querySelector('.trc-diff')!
    expect([...diff.querySelectorAll('tr.is-del td.trc-no')].map((td) => td.textContent)).toEqual(['11', ''])
    expect([...diff.querySelectorAll('tr.is-add td.trc-no')].map((td) => td.textContent)).toEqual(['', '11'])
    const link = within(detail()).getByRole('link', { name: 'Open in Changes ›' })
    expect(link.getAttribute('href')).toBe(`/work/task/${ID}/changes/${encodeURIComponent('apps/reconciler.py')}`)
    fireEvent.click(link)
    expect(window.location.pathname).toBe(`/work/task/${ID}/changes/${encodeURIComponent('apps/reconciler.py')}`)
  })

  it('a file written outside the repository has no Changes button', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    fireEvent.click(rows()[8]!)
    expect(within(detail()).queryByRole('link', { name: 'Open in Changes ›' })).toBeNull()
    expect(detail().querySelector('.trc-diff-path')!.getAttribute('title')).toContain('outside the repository')
  })

  it('shows the whole command and the whole output, with the error highlighted, and keeps an event open below', async () => {
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    fireEvent.click(rows()[3]!)
    fireEvent.click(within(detail()).getByRole('button', { name: 'Keep open' }))
    fireEvent.click(rows()[4]!)
    const card = detail().querySelector('.trc-card.is-error')!
    expect([...card.querySelectorAll('h4')].map((h) => h.textContent)).toEqual(['Command', 'Output · error'])
    expect(card.querySelector('.trc-ln.is-stack')!.textContent).toContain('>       assert gen == 2')
    expect(card.querySelector('.trc-pre:not(.trc-cmd)')!.textContent).toContain('1 failed, 11 passed in 0.31s')
    // Mutation: dropping the kept slot leaves only the picked card.
    expect(detail().querySelectorAll('.trc-slot')).toHaveLength(2)
    expect(detail().querySelector('.trc-slot.is-kept .trc-diff')).not.toBeNull()
  })
})

describe('the browser lane (tr-b-browser)', () => {
  const records = () => [
    init(),
    call('b1', 'mcp__playwright__browser_navigate', { url: 'https://example.com' }),
    reply('b1', 'Navigated.\nPage URL: https://example.com'),
    call('b2', 'mcp__playwright__browser_take_screenshot', {}),
    reply('b2', `Took the screenshot: [page](${CWD.replace('/work/repo', '')}/artifacts/screenshots/page-1.png)\nPage URL: https://example.com`),
    call('b3', 'mcp__playwright__browser_take_screenshot', {}),
    reply('b3', `[page](${CWD.replace('/work/repo', '')}/artifacts/screenshots/page-2.png)`),
    call('b4', 'mcp__playwright__browser_console_messages', {}),
    reply('b4', '[ERROR] Failed to load resource\n[WARNING] slow script\n[LOG] ready'),
    say('Checked the page.'),
    result('Checked the page.'),
  ]

  it('draws the screenshot the result names from the run’s artifacts, and says when it is not there', async () => {
    serve(transcriptOf(stepsOf(records())))
    api.loadArtifactListing.mockResolvedValue(ok({ task_id: ID, artifacts: [{ name: 'screenshots/page-1.png' }], artifacts_skipped: [], artifact_bytes: 10, complete: true }))
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(7))
    expect(rows()[1]!.textContent).toContain('navigate https://example.com')
    expect(rows()[4]!.querySelector('.trc-sub')!.textContent).toBe('1 error · 1 warning')
    fireEvent.click(rows()[2]!)
    const img = await waitFor(() => {
      const found = detail().querySelector<HTMLImageElement>('.trc-shot img')
      expect(found).not.toBeNull()
      return found!
    })
    expect(img.getAttribute('src')).toContain('name=screenshots%2Fpage-1.png')
    fireEvent.click(rows()[3]!)
    expect(detail().textContent).toContain('screenshots/page-2.png is not in this run’s artifacts.')
    fireEvent.click(rows()[4]!)
    expect([...detail().querySelectorAll('.trc-ln.is-err')].map((l) => l.textContent!.trim())).toEqual(['[ERROR] Failed to load resource'])
    expect(detail().querySelector('.trc-ln.is-warn')!.textContent).toContain('[WARNING] slow script')
  })
})

describe('a failed run, jumped to its error (tr-b-failed)', () => {
  it('jumps to the first error, then the next, and shows the task’s own error above the panes', async () => {
    const recs = [
      init(),
      call('t1', 'Bash', { command: 'uv run pytest tests/unit -q' }),
      reply('t1', 'E   boom\n1 failed in 0.1s', true),
      call('t2', 'Bash', { command: 'git push' }),
      reply('t2', 'fatal: denied', true),
      say('Could not push.'),
      result('Could not push.', { subtype: 'error_during_execution', is_error: true }),
    ]
    serve(transcriptOf(stepsOf(recs)))
    inPane(agent({ state: 'FAILED', last_error: 'exit 1: the agent failed\nmore' }))
    const jump = await screen.findByRole('button', { name: 'Jump to error · 3' })
    fireEvent.click(jump)
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('2')
    fireEvent.click(jump)
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('3')
    fireEvent.keyDown(window, { key: 'e' })
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('5')
    expect(rows()[4]!.classList.contains('is-error')).toBe(true)
    const note = document.querySelector<HTMLElement>('.trc-taskerr')!
    expect(note.textContent).toContain('exit 1: the agent failed')
    expect(note.textContent).not.toContain('more')
  })

  it('has no error button to press when nothing is marked an error', async () => {
    serve(transcriptOf(stepsOf([init(), say('fine'), result('fine')])))
    inPane(agent())
    const none = await screen.findByRole('button', { name: 'No errors' })
    expect((none as HTMLButtonElement).disabled).toBe(true)
  })
})

describe('a live run (tr-b-live)', () => {
  it('appends new events and keeps the newest picked until the reader picks another', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const first = [init(), say('Starting.'), call('t1', 'Bash', { command: 'uv run pytest tests/unit/test_a.py' }), progress('t1', 6)]
    const live = (recs: Record<string, unknown>[]) => transcriptOf(stepsOf(recs), { complete: false, stream: { ...transcriptOf([]).stream, source: 'live' } })
    serve(live(first))
    inPane(agent({ state: 'RUNNING', completed_at: null }))
    await waitFor(() => expect(rows()).toHaveLength(3))
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('3')
    expect(rows()[2]!.querySelector('.trc-st')!.getAttribute('aria-label')).toBe('no result yet')
    expect(rows()[2]!.querySelector('.trc-dur')!.textContent).toBe('≥6s')
    expect(detail().textContent).toContain('no result in this read yet')
    expect(screen.getByRole('button', { name: 'Following' }).getAttribute('aria-pressed')).toBe('true')

    serve(live([...first, reply('t1', '3 passed in 6.2s'), say('Tests pass.')]))
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    await waitFor(() => expect(rows()).toHaveLength(4))
    // Mutation: following off at rest would leave #3 picked.
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('4')

    fireEvent.click(rows()[1]!)
    serve(live([...first, reply('t1', '3 passed in 6.2s'), say('Tests pass.'), say('Opening the PR.')]))
    await act(() => vi.advanceTimersByTimeAsync(5_000))
    await waitFor(() => expect(rows()).toHaveLength(5))
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('2')
    const follow = screen.getByRole('button', { name: '↓ 3 newer · Follow' })
    fireEvent.click(follow)
    expect(picked()!.querySelector('.trc-n')!.textContent).toBe('5')
  })
})

describe('a narrow pane: 1280 with the list back, and a phone (tr-b-coding-rest-1280, -390)', () => {
  let width = 390
  const real = globalThis.ResizeObserver
  beforeEach(() => {
    globalThis.ResizeObserver = class {
      constructor(private fn: () => void) {}
      observe() {
        this.fn()
      }
      unobserve() {}
      disconnect() {}
    } as unknown as typeof ResizeObserver
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      const w = this.classList.contains('trc') ? width : 0
      return { x: 0, y: 0, top: 0, left: 0, bottom: 0, right: w, width: w, height: 0, toJSON: () => ({}) } as DOMRect
    })
    serve(transcriptOf(stepsOf(codingRecords())))
  })
  afterEach(() => {
    globalThis.ResizeObserver = real
    vi.restoreAllMocks()
  })

  it('shows the timeline alone, short chip names, and a tapped row full screen with a way back', async () => {
    width = 390
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    const root = document.querySelector<HTMLElement>('.trc')!
    expect(root.classList.contains('is-phone')).toBe(true)
    expect(screen.getByRole('button', { name: /^Msgs/ }).textContent).toBe('Msgs 2')
    expect(root.classList.contains('is-detail')).toBe(false)
    fireEvent.click(rows()[4]!)
    expect(root.classList.contains('is-detail')).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '← Events' }))
    expect(root.classList.contains('is-detail')).toBe(false)
  })

  it('draws both panes side by side when there is room', async () => {
    width = 1100
    inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    expect(document.querySelector('.trc')!.classList.contains('is-side')).toBe(true)
    expect(screen.queryByRole('button', { name: '← Events' })).toBeNull()
  })
})

describe('beside the Agents list', () => {
  it('folds the list to its strip while the trace is drawn, and opens it again to where it was', async () => {
    resetListSnap()
    setListSnap('half', false)
    serve(transcriptOf(stepsOf(codingRecords())))
    const view = inPane(agent())
    await waitFor(() => expect(rows()).toHaveLength(11))
    // Mutation: no fold leaves the list at half the width beside two panes.
    expect(listSnap()).toBe('strip')
    view.unmount()
    expect(listSnap()).toBe('half')
    resetListSnap()
  })
})

describe('outside the agent’s Logs pane', () => {
  it('the log beside the live browser keeps the stream view', async () => {
    serve(transcriptOf(stepsOf(codingRecords())))
    render(<AgentLogs task={agent()} />)
    await waitFor(() => expect(document.querySelector('.ag-logs-body')).not.toBeNull())
    expect(document.querySelector('.trc')).toBeNull()
    expect(api.loadTranscript).not.toHaveBeenCalledWith(ID, expect.objectContaining({ includeRaw: true }))
  })
})
