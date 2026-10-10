// VARIANT 3: THE REVIEW'S FINDINGS BESIDE THEIR LINES (docs/design/diff-viewer.md
// §2 variant 3 and §5 "later · findings"; the worker half is PR 943).
//
// The gated step's `result_summary.verdict_gate` carries the findings as text,
// `finding_locations` ({finding, file, line?, side?}) and `reviewed_patches`
// ({task_id, filename, sha256}: what the review's worker measured as it staged
// each patch). The Changes matrix pins a finding beside its line ONLY when the
// patch it shows hashes to that digest; otherwise it is listed, not pinned.
//
// MUTATIONS, one per block: drop the digest comparison in
// `wfreview.placeFinding` (pin on any digest) and the "earlier patch" test goes
// red; pin a finding with a file and no line on the file's first line and the
// "not placed" test goes red; key a pin by line number alone, ignoring
// `side`, and the side test goes red (the mark lands in the new gutter of the
// added line); render the mark as a <span> or drop the list's buttons and the
// keyboard test goes red; drop the named-first sort and the sort test goes red.

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createHash } from 'node:crypto'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { ArtifactContent, GitSummary, Task, Workflow, WorkflowStep } from '../types'
import { attempt, task as baseTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadArtifactContent: vi.fn(),
  loadWorkflow: vi.fn(),
  loadWorkflowUsage: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { WorkflowChangesTab, patchDigest } = await import('../WorkflowChanges')
const { placeFinding, reviewFindings } = await import('../wfreview')
const { DiffView, placePins } = await import('../diff/DiffView')
const { parseUnifiedDiff } = await import('../diff/parse')

// ---------------------------------------------------------------------------
// Fixtures: implement changes two files, review reads implement's patch, fix
// is gated on the review and records what it read.
// ---------------------------------------------------------------------------

const PATCH = 'swarm-work.patch'

/** One file's section: `hunks` are [line, removed, added], one line each side. */
function section(path: string, hunks: [number, string, string][]): string {
  const out = [`diff --git a/${path} b/${path}`, `--- a/${path}`, `+++ b/${path}`]
  for (const [at, del, add] of hunks) out.push(`@@ -${at},1 +${at},1 @@`, `-${del}`, `+${add}`)
  return out.join('\n') + '\n'
}

const IMPLEMENT = section('src/app/one.ts', [[1, 'alpha', 'beta'], [10, 'gamma', 'delta']]) + section('src/app/two.ts', [[3, 'x', 'y']])
const FIX = section('src/app/one.ts', [[5, 'e', 'f']])
const sha = (text: string) => createHash('sha256').update(text, 'utf8').digest('hex')

type FileRow = NonNullable<GitSummary['files']>[number]
const fileRow = (path: string): FileRow => ({ path, old_path: null, status: 'M', insertions: 1, deletions: 1, binary: false })

function stepTask(id: string, git: GitSummary, extra: Record<string, unknown> = {}): Task {
  return baseTask({
    id,
    state: 'SUCCEEDED',
    workflow_id: 'wf_one',
    result_summary: { git, artifacts: [{ name: PATCH, bytes: 100, uri: 'gs://b/p' }], ...extra },
  })
}

function step(step_id: string, task_id: string): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id }
}

const STEPS = [step('implement', 't_impl'), step('review', 't_rev'), step('fix', 't_fix')]
const WF = {
  workflow_id: 'wf_one',
  state: 'SUCCEEDED',
  tenant_id: 'eng',
  stored_state: 'SUCCEEDED',
  created_at: '2026-10-10T10:00:00Z',
  updated_at: '2026-10-10T11:00:00Z',
  submitted_by: 'operator@example.com',
  priority: 0,
  on_step_failure: 'FAIL_WORKFLOW',
  cancel_requested: false,
  steps: STEPS,
} as Workflow

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function served(taskId: string, content: string, over: Partial<ArtifactContent> = {}): ArtifactContent {
  return {
    task_id: taskId,
    tenant_id: 'eng',
    attempt_id: 'att_1',
    artifact: { name: PATCH, bytes: content.length, uri: null },
    status: 'ok',
    detail: null,
    key: 'k',
    uri: null,
    content,
    total_bytes: content.length,
    offset: 0,
    returned_bytes: content.length,
    next_offset: null,
    truncated: false,
    redacted: true,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
    invalid_utf8_bytes: 0,
    ...over,
  }
}

const PATCHES: Record<string, string> = { t_impl: IMPLEMENT, t_fix: FIX }

interface Gate {
  findings: unknown[]
  locations?: Record<string, unknown>[]
  digest?: string | null
}

/** The three tasks, the fix step carrying the gate it read. */
function tasks({ findings, locations, digest = sha(IMPLEMENT) }: Gate): Task[] {
  const gate: Record<string, unknown> = { task_id: 't_rev', file: 'verdict.json', verdict: 'NOT_YET', verdict_in: ['NOT_YET'], agent_ran: true, findings, findings_dropped: 0 }
  if (locations !== undefined) gate['finding_locations'] = locations
  if (digest !== null) gate['reviewed_patches'] = [{ task_id: 't_impl', filename: PATCH, sha256: digest }]
  return [
    stepTask('t_impl', { base: 'abc', patch: PATCH, files: [fileRow('src/app/one.ts'), fileRow('src/app/two.ts')] }),
    stepTask('t_rev', { base: 'abc', commits: [], dirty: [], files: [] }),
    stepTask('t_fix', { base: 'def', patch: PATCH, files: [fileRow('src/app/one.ts')] }, { verdict_gate: gate }),
  ]
}

function Tab({ ts, onAt }: { ts: Task[]; onAt?: (at: { step: string | null; file: string | null }) => void }) {
  const [at, setAt] = useState<{ step: string | null; file: string | null }>({ step: null, file: null })
  return (
    <WorkflowChangesTab
      workflow={WF}
      taskById={new Map(ts.map((t) => [t.id, t]))}
      at={at}
      onAt={(next) => {
        onAt?.(next)
        setAt(next)
      }}
    />
  )
}

function findingsPanel(): HTMLElement {
  return screen.getByRole('region', { name: 'Review findings' })
}

function marks(): HTMLButtonElement[] {
  return [...document.querySelectorAll<HTMLButtonElement>('button.diff-pin')]
}

/** The diff row a mark is drawn in, and which of its gutters (0 = old, 1 = new in unified). */
function markPlace(mark: HTMLElement): { text: string; gutter: number } {
  const row = mark.closest<HTMLElement>('.diff-row')!
  const cell = mark.closest<HTMLElement>('.diff-no')!
  const gutters = [...row.querySelectorAll('.diff-no')]
  return { text: row.querySelector('.diff-text')!.textContent ?? '', gutter: gutters.indexOf(cell) }
}

beforeEach(() => {
  vi.stubGlobal('innerWidth', 1280)
  api.loadAttempts.mockResolvedValue(ok({ attempts: [attempt(1)] }))
  api.loadArtifactContent.mockImplementation(async (id: string) => ok(served(id, PATCHES[id] ?? '')))
  api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------

describe('the digest check', () => {
  it('pins a finding beside its line when the patch shown is the patch the review read', async () => {
    const ts = tasks({ findings: ['the loop never ends'], locations: [{ finding: 0, file: 'src/app/one.ts', line: 10, side: 'new' }] })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(marks()).toHaveLength(1))
    const mark = marks()[0]!
    expect(mark.getAttribute('aria-label')).toBe('Not graded finding: the loop never ends')
    expect(mark.getAttribute('title')).toBe('Not graded finding: the loop never ends')
    // On the added line `delta`, in the new-side gutter, in implement's hunk.
    const place = markPlace(mark)
    expect(place.text).toBe('delta')
    expect(place.gutter).toBe(1)
    expect(findingsPanel().textContent).toMatch(/1 pinned/)
    expect(within(findingsPanel()).getByRole('button', { name: /pinned · implement · src\/app\/one\.ts · new 10/ })).toBeTruthy()
  })

  it('pins nothing and says "from an earlier patch" when the digest differs', async () => {
    const ts = tasks({
      findings: ['the loop never ends'],
      locations: [{ finding: 0, file: 'src/app/one.ts', line: 10, side: 'new' }],
      digest: sha('a patch implement wrote before a re-run\n'),
    })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(findingsPanel().textContent).toMatch(/from an earlier patch: the review read a patch implement has since replaced/))
    // The open file's hunks are drawn, and not one mark is on them.
    await waitFor(() => expect(screen.getByRole('region', { name: 'Diff lines' })).toBeTruthy())
    expect(marks()).toHaveLength(0)
    expect(findingsPanel().textContent).toMatch(/0 pinned/)
    expect(findingsPanel().textContent).toMatch(/1 from an earlier patch/)
  })

  it('does not compare a patch it cannot hash as stored: masked text pins nothing', async () => {
    api.loadArtifactContent.mockImplementation(async (id: string) => ok(served(id, PATCHES[id] ?? '', { redaction_count: 1 })))
    const ts = tasks({ findings: ['the loop never ends'], locations: [{ finding: 0, file: 'src/app/one.ts', line: 10, side: 'new' }] })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(findingsPanel().textContent).toMatch(/not compared/))
    expect(marks()).toHaveLength(0)
  })

  it('measures the digest only from the whole, unmasked, UTF-8 object', async () => {
    expect(await patchDigest(served('t', IMPLEMENT))).toBe(sha(IMPLEMENT))
    expect(await patchDigest(served('t', IMPLEMENT, { next_offset: 10 }))).toBeNull()
    expect(await patchDigest(served('t', IMPLEMENT, { invalid_utf8_bytes: 2 }))).toBeNull()
    expect(await patchDigest(served('t', IMPLEMENT, { invalid_utf8_bytes: undefined }))).toBeNull()
  })
})

describe('not placed, never guessed', () => {
  it('lists a finding without a location as not placed, and one with a file and no line too', async () => {
    const ts = tasks({
      findings: ['add a test for the empty case', 'this file needs a header'],
      locations: [{ finding: 1, file: 'src/app/one.ts' }],
    })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(screen.getByRole('region', { name: 'Diff lines' })).toBeTruthy())
    const items = [...findingsPanel().querySelectorAll('li')]
    expect(items[0]!.textContent).toMatch(/add a test for the empty case.*not placed: the review named no file/)
    expect(items[1]!.textContent).toMatch(/this file needs a header.*src\/app\/one\.ts · not placed: the review named the file and no line in it/)
    expect(findingsPanel().textContent).toMatch(/2 not placed/)
    expect(marks()).toHaveLength(0)
  })

  it('places nothing when the worker recorded no digest of the patch the review read', async () => {
    const ts = tasks({ findings: ['the loop never ends'], locations: [{ finding: 0, file: 'src/app/one.ts', line: 10, side: 'new' }], digest: null })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(findingsPanel().textContent).toMatch(/not placed: no digest of the patch the review read was recorded/))
    expect(marks()).toHaveLength(0)
  })

  it('lists a pin whose line is in no hunk as not pinned, in the viewer itself', () => {
    const pins = [{ id: 'p', path: 'src/app/one.ts', side: 'new' as const, line: 500, severity: 'Major', tone: 'warn' as const, summary: 'far away' }]
    render(<DiffView patch={IMPLEMENT} pins={pins} />)
    const list = screen.getByRole('navigation', { name: 'Review findings on this patch' })
    expect(list.textContent).toMatch(/0 of 1 finding pinned/)
    expect(list.textContent).toMatch(/src\/app\/one\.ts · new 500 · Major · far away · not pinned: no hunk of this patch holds that line/)
    expect(marks()).toHaveLength(0)
  })
})

describe('the side', () => {
  it('pins an `old` finding on the removed line, in the old-side gutter', async () => {
    const ts = tasks({ findings: ['this removal drops the guard'], locations: [{ finding: 0, file: 'src/app/one.ts', line: 10, side: 'old' }] })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(marks()).toHaveLength(1))
    const place = markPlace(marks()[0]!)
    expect(place.text).toBe('gamma')
    expect(place.gutter).toBe(0)
    expect(marks()[0]!.getAttribute('data-pin-side')).toBe('old')
  })

  it('places by side and step, never by line number alone', () => {
    const files = (() => {
      const r = parseUnifiedDiff(IMPLEMENT)
      if (!r.ok) throw new Error('fixture does not parse')
      return r.files
    })()
    const pin = (side: 'old' | 'new', step?: string) => ({ id: `${side}${step ?? ''}`, path: 'src/app/one.ts', side, line: 10, step, severity: 'Minor', tone: 'info' as const, summary: 's' })
    const at = placePins(files, [pin('old'), pin('new'), pin('new', 'fix')], (_p, _h) => 'implement')
    // Hunk 1 is `-gamma +delta`: line 0 is the removal, line 1 the addition.
    expect(at.get('old')).toEqual({ file: 0, hunk: 1, line: 0 })
    expect(at.get('new')).toEqual({ file: 0, hunk: 1, line: 1 })
    // Named for another step's hunks: not this line.
    expect(at.get('newfix')).toBeNull()
  })
})

describe('keyboard access', () => {
  it('makes each mark a button whose focus says the finding, and lists every pin as a button that reaches it', async () => {
    const ts = tasks({
      findings: [{ summary: 'the loop never ends', severity: 'blocker' }, 'rename this'],
      locations: [
        { finding: 0, file: 'src/app/one.ts', line: 10, side: 'new' },
        { finding: 1, file: 'src/app/one.ts', line: 1, side: 'old' },
      ],
    })
    render(<Tab ts={ts} />)
    await waitFor(() => expect(marks()).toHaveLength(2))
    const note = screen.getByTestId('diff-pin-note')
    expect(note.textContent).toBe('')
    const blocker = marks().find((m) => m.getAttribute('aria-label') === 'Blocker finding: the loop never ends')!
    expect(blocker.tagName).toBe('BUTTON')
    expect(blocker.tabIndex).toBe(0)
    fireEvent.focus(blocker)
    expect(note.textContent).toBe('Blocker · new line 10: the loop never ends')

    const list = screen.getByRole('navigation', { name: 'Review findings on this patch' })
    expect(list.textContent).toMatch(/2 of 2 findings pinned/)
    const go = within(list).getByRole('button', { name: /src\/app\/one\.ts · old 1 · Not graded · rename this/ })
    fireEvent.click(go)
    expect(go.getAttribute('aria-pressed')).toBe('true')
    expect(note.textContent).toBe('Not graded · old line 1: rename this')
  })

  it('opens a pinned finding’s file from the findings panel', async () => {
    const onAt = vi.fn()
    const ts = tasks({ findings: ['two is wrong'], locations: [{ finding: 0, file: 'src/app/two.ts', line: 3, side: 'new' }] })
    render(<Tab ts={ts} onAt={onAt} />)
    const pinned = await within(await screen.findByRole('region', { name: 'Review findings' })).findByRole('button', { name: /pinned · implement · src\/app\/two\.ts · new 3/ })
    fireEvent.click(pinned)
    expect(onAt).toHaveBeenLastCalledWith({ step: null, file: 'src/app/two.ts' })
    await waitFor(() => expect(marks()).toHaveLength(1))
    expect(markPlace(marks()[0]!).text).toBe('y')
  })
})

describe('files named by the review sort first', () => {
  it('puts a named file above the rest and tags it, with no score', async () => {
    const ts = tasks({ findings: ['two is wrong'], locations: [{ finding: 0, file: 'src/app/two.ts', line: 3, side: 'new' }] })
    render(<Tab ts={ts} />)
    const table = await screen.findByRole('table', { name: 'Files by step' })
    const rows = [...table.querySelectorAll('tbody tr')]
    expect(rows.map((r) => r.getAttribute('data-file'))).toEqual(['src/app/two.ts', 'src/app/one.ts'])
    expect(rows[0]!.textContent).toMatch(/named by the review/)
    expect(rows[1]!.textContent).not.toMatch(/named by the review/)
  })
})

describe('the pure readers', () => {
  it('reads each finding once per review, with its location, and refuses a malformed digest', () => {
    const ts = tasks({ findings: ['a', 'b'], locations: [{ finding: 1, file: 'src/app/one.ts', line: 10, side: 'sideways' }], digest: 'not-a-digest' })
    const read = reviewFindings([...ts, ts[2]!])
    expect(read.map((f) => [f.id, f.file, f.line, f.side, f.patches.length])).toEqual([
      ['t_rev:0', null, null, null, 0],
      ['t_rev:1', 'src/app/one.ts', null, null, 0],
    ])
    expect(placeFinding(read[1]!, []).kind).toBe('unplaced')
  })
})
