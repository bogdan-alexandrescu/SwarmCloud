// #310 THROUGH AGENT › DETAILS: the Code card's `Show the diff` opens the task's
// `swarm-work.patch` in the in-house viewer, and the issue's "how would you
// know it worked" holds THERE, not only on a bare <DiffView>.
//
// The viewer's own behaviour is pinned in `__tests__/diff/*`; `code.card` only
// checks that a region appears. This file drives the whole path a reviewer
// takes -- the card, the artifact read by the patch's manifest name, the
// viewer inside the drawer -- and asserts on what is drawn:
//
//   * every file listed with its own +/- counts, unified and split;
//   * find counts a string across files and moves the open file;
//   * n / p and j / k move between files and hunks;
//   * a binary file, a rename and a deleted file each named as such;
//   * the patch is drawn AS SERVED: a masked value stays the mask, markup in
//     a line is text, never an element.
//
// CONTROL: the mask is asserted present character for character and the
// markup case asserts no <img> exists, so either goes red if the viewer
// rewrote the served text or parsed it as HTML.
//
// MUTATION, run once by hand: search only the first file in `findMatches`
// (find.ts), or drop `backLabel="Code"` from `AgPatchDiff` -- the counts and
// find cases go red.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun } from '../api'
import type { ArtifactContent, AttemptRow, GitSummary, Task } from '../types'
import { attempt, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflow: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const MASK = '***REDACTED***'

/**
 * Five files, in patch order: a modified file with two hunks (holding the mask
 * and a line of markup), a rename, a deleted file, a binary file and an added
 * one.
 */
const PATCH = [
  'diff --git a/src/app/config.ts b/src/app/config.ts',
  'index 1111111..2222222 100644',
  '--- a/src/app/config.ts',
  '+++ b/src/app/config.ts',
  '@@ -1,3 +1,3 @@',
  ' export const region = 1',
  '-export const forge = "old"',
  `+export const forge = "${MASK}"`,
  ' export const zone = 2',
  '@@ -20,2 +20,3 @@ export function boot() {',
  '   boot()',
  '+  // <img src=x onerror=alert(1)>',
  '   return region',
  'diff --git a/src/app/old.ts b/src/app/new.ts',
  'similarity index 80%',
  'rename from src/app/old.ts',
  'rename to src/app/new.ts',
  'index 3333333..4444444 100644',
  '--- a/src/app/old.ts',
  '+++ b/src/app/new.ts',
  '@@ -1,2 +1,2 @@',
  "-export const name = 'old'",
  "+export const name = 'new'",
  ' export default name',
  'diff --git a/src/app/gone.ts b/src/app/gone.ts',
  'deleted file mode 100644',
  'index 5555555..0000000',
  '--- a/src/app/gone.ts',
  '+++ /dev/null',
  '@@ -1,2 +0,0 @@',
  '-export const gone = 1',
  '-export default gone',
  'diff --git a/assets/logo.png b/assets/logo.png',
  'index 6666666..7777777 100644',
  'Binary files a/assets/logo.png and b/assets/logo.png differ',
  'diff --git a/docs/notes.md b/docs/notes.md',
  'new file mode 100644',
  'index 0000000..8888888',
  '--- /dev/null',
  '+++ b/docs/notes.md',
  '@@ -0,0 +1,2 @@',
  '+# Notes',
  '+export nothing here',
  '',
].join('\n')

const REF = { name: 'swarm-work.patch', bytes: PATCH.length, uri: 'gs://swarm-artifacts/tenants/eng/tasks/t/attempts/att_1/artifacts/swarm-work.patch' }

function served(): ArtifactContent {
  return {
    task_id: 'tsk_diff',
    tenant_id: 'eng',
    attempt_id: 'att_1',
    artifact: REF,
    status: 'ok',
    detail: null,
    key: 'k',
    uri: REF.uri,
    content: PATCH,
    total_bytes: PATCH.length,
    offset: 0,
    returned_bytes: PATCH.length,
    next_offset: null,
    truncated: false,
    redacted: true,
    redaction_count: 1,
    redaction: { applied_at_read_time: true, rules: 9 },
  }
}

function run(t: Task, attempts: AttemptRow[] = [attempt(1)]): AgentRun {
  return {
    task: t,
    events: [],
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: null,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

function finished(): Task {
  const git: GitSummary = { base: 'abc', published: false, publish_reason: 'collect', strategy: 'collect', patch: 'swarm-work.patch' }
  return runTask({ state: 'SUCCEEDED', repository_url: 'https://github.com/example/swarm', result_summary: { git, artifacts: [REF] } })
}

/** Render the agent, press `Show the diff`, and wait for the viewer's lines. */
async function openDiff(): Promise<void> {
  render(<Run run={run(finished())} />)
  fireEvent.click(screen.getByRole('button', { name: 'Show the diff' }))
  await waitFor(() => expect(screen.getByRole('region', { name: 'Diff lines' })).toBeTruthy())
}

function scroller(): HTMLElement {
  return screen.getByRole('region', { name: 'Diff lines' })
}

function openFile(): string | null {
  return scroller().getAttribute('data-open-file')
}

function listRow(path: string): HTMLElement {
  const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
  const row = Array.from(nav.querySelectorAll<HTMLElement>('button.diff-file')).find((b) => b.getAttribute('data-path') === path)
  if (!row) throw new Error(`no list row for ${path}`)
  return row
}

let store: Map<string, string>

beforeEach(() => {
  store = new Map()
  vi.stubGlobal('localStorage', {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  })
  vi.stubGlobal('innerWidth', 1280)
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadArtifactContent.mockResolvedValue({ status: 'ok', data: served(), fetchedAt: Date.now() })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('Agent › Details opens swarm-work.patch in the in-app diff viewer', () => {
  it('reads the patch by its manifest name and lists every file with its own counts', async () => {
    await openDiff()
    expect(api.loadArtifactContent).toHaveBeenCalledWith(expect.any(String), 'swarm-work.patch')
    const counts = (path: string) => listRow(path).textContent ?? ''
    expect(counts('src/app/config.ts')).toMatch(/\+2\s*−1/)
    expect(counts('src/app/new.ts')).toMatch(/\+1\s*−1/)
    expect(counts('src/app/gone.ts')).toMatch(/\+0\s*−2/)
    expect(counts('docs/notes.md')).toMatch(/\+2\s*−0/)
    expect(listRow('assets/logo.png')).toBeTruthy()
    // The bar names the patch and goes back to the Code card, not to a list
    // of artifacts that is not on screen.
    expect(screen.getByRole('button', { name: /Code/ })).toBeTruthy()
  })

  it('switches between unified and split', async () => {
    await openDiff()
    expect(scroller().querySelector('[data-diff-row="line"]')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Split' }))
    expect(screen.getByRole('button', { name: 'Split' }).getAttribute('aria-pressed')).toBe('true')
    expect(scroller().querySelector('[data-diff-row="pair"]')).not.toBeNull()
    expect(scroller().querySelector('[data-diff-row="line"]')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Unified' }))
    expect(scroller().querySelector('[data-diff-row="pair"]')).toBeNull()
  })

  it('finds a string across files with a counter, moving the open file', async () => {
    await openDiff()
    const box = screen.getByRole('searchbox', { name: 'Find in diff' })
    fireEvent.change(box, { target: { value: 'export' } })
    const count = () => screen.getByTestId('diff-find-count').textContent
    // config.ts holds 4 (region, both forge lines, zone), the rename 3, the
    // deletion 2, notes.md 1 -- and not the hunk header's `export function
    // boot`, which is not a line of the patch.
    expect(count()).toBe('1 of 10')
    expect(openFile()).toBe('src/app/config.ts')
    for (let i = 0; i < 4; i++) fireEvent.keyDown(box, { key: 'Enter' })
    // The fifth match is the rename's first line: find crossed a file.
    expect(count()).toBe('5 of 10')
    expect(openFile()).toBe('src/app/new.ts')
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(count()).toBe('4 of 10')
    expect(openFile()).toBe('src/app/config.ts')
  })

  it('moves between files with n / p and between hunks with j / k, inside the drawer', async () => {
    await openDiff()
    const first = openFile()
    expect(fireEvent.keyDown(scroller(), { key: 'n' })).toBe(false)
    const second = openFile()
    expect(second).not.toBe(first)
    fireEvent.keyDown(scroller(), { key: 'p' })
    expect(openFile()).toBe(first)
    // config.ts has two hunks: j consumes the key and k comes back.
    expect(openFile()).toBe('src/app/config.ts')
    expect(fireEvent.keyDown(scroller(), { key: 'j' })).toBe(false)
    expect(fireEvent.keyDown(scroller(), { key: 'k' })).toBe(false)
  })

  it('names a binary file, a rename and a deleted file as such', async () => {
    await openDiff()
    fireEvent.click(listRow('assets/logo.png'))
    expect(scroller().textContent).toMatch(/binary/i)
    fireEvent.click(listRow('src/app/new.ts'))
    expect(scroller().textContent).toMatch(/renamed/i)
    expect(scroller().textContent).toMatch(/80%/)
    fireEvent.click(listRow('src/app/gone.ts'))
    expect(scroller().textContent).toMatch(/deleted/i)
  })

  it('draws the patch as served: the mask stays the mask, markup stays text', async () => {
    await openDiff()
    const drawn = scroller().textContent ?? ''
    expect(drawn).toContain(MASK)
    expect(drawn).toContain('<img src=x onerror=alert(1)>')
    expect(document.querySelector('img')).toBeNull()
    // The masked count the serve path measured is shown beside the patch.
    expect(document.querySelector('.art-masked')?.textContent).toBe('1')
  })
})
