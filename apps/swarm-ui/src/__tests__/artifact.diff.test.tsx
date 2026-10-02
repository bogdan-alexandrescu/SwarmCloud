// #104: A DIFF IS DRAWN AS A DIFF -- in the #310 viewer (owner decision
// 2026-10-01).
//
// A diff is recognised by NAME (`.patch`, `.diff` -- `swarm-work.patch`,
// `change.diff`) or by CONTENT (`diff --git` at the top, or `@@` hunks), and
// opens in `src/diff/DiffView.tsx`: a file list on the left and the selected
// file on the right, under a bar with `‹ Artifacts`, the name, size and
// attempt, Unified/Split, Copy patch and Download. ArtifactViewer draws no
// diff of its own any more. A window that is not the whole file says
// `partial` in the bar and draws only the files wholly inside it.
//
// MUTATION: return 'text' for `.patch` in `artifactKind`, bring back a local
// diff renderer in ArtifactViewer.tsx, or drop the partial mark -- each goes
// red below.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, waitFor, within } from '@testing-library/react'

import VIEWER_SOURCE from '../ArtifactViewer.tsx?raw'
import STYLES from '../styles.css?raw'
import { ArtifactViewer, diffStat, patchWindow } from '../ArtifactViewer'
import type { Result } from '../fetch'
import { artifactKind, looksLikeDiff, type ArtifactContent } from '../types'

const DIFF = [
  'diff --git a/src/a.ts b/src/a.ts',
  'index 111..222 100644',
  '--- a/src/a.ts',
  '+++ b/src/a.ts',
  '@@ -1,2 +1,3 @@ export function a() {',
  ' const keep = 1',
  '-const gone = 2',
  '+const added = 2',
  '+const more = 3',
  'diff --git a/b.md b/b.md',
  '--- a/b.md',
  '+++ b/b.md',
  '@@ -0,0 +1 @@',
  '+line',
  '',
].join('\n')

function content(over: Partial<ArtifactContent> = {}): ArtifactContent {
  return {
    task_id: 'tsk_diff',
    tenant_id: 'acme',
    attempt_id: 'att_1',
    artifact: { name: 'swarm-work.patch', bytes: DIFF.length, uri: 'gs://b/swarm-work.patch' },
    status: 'ok',
    detail: null,
    key: 'k',
    uri: 'gs://b/swarm-work.patch',
    content: DIFF,
    total_bytes: DIFF.length,
    offset: 0,
    returned_bytes: DIFF.length,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 9 },
    ...over,
  }
}

async function view(data: ArtifactContent, name = data.artifact.name ?? 'x', onClose = () => {}): Promise<HTMLElement> {
  const got: Result<ArtifactContent> = { status: 'ok', data, fetchedAt: Date.now() }
  const load = () => Promise.resolve(got)
  const { container } = render(
    <ArtifactViewer taskId="tsk_diff" artifact={{ name, bytes: 1, uri: 'gs://b/x' }} onClose={onClose} load={load} />,
  )
  await waitFor(() => expect(container.querySelector('.art-prov')).not.toBeNull())
  return container as HTMLElement
}

describe('a diff is recognised', () => {
  it('by name: .patch and .diff', () => {
    expect(artifactKind('swarm-work.patch')).toBe('diff')
    expect(artifactKind('change.diff')).toBe('diff')
    expect(artifactKind('CHANGE.DIFF')).toBe('diff')
    expect(artifactKind('notes.md')).toBe('markdown')
    expect(artifactKind('out.txt')).toBe('text')
  })

  it('by content: `diff --git` at the top, or a hunk header', () => {
    expect(looksLikeDiff(DIFF)).toBe(true)
    expect(looksLikeDiff('--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n')).toBe(true)
    expect(looksLikeDiff('just some text\nwith @@ in it\n')).toBe(false)
    expect(looksLikeDiff('')).toBe(false)
  })

  it('counts files, insertions and deletions, never the +++/--- headers', () => {
    expect(diffStat(DIFF)).toEqual({ files: 2, insertions: 3, deletions: 1 })
  })
})

describe('an artifact diff opens in the #310 viewer', () => {
  it('with a file list and the first file open', async () => {
    const el = await view(content())
    const diff = el.querySelector('section.diff')
    expect(diff, 'a .patch was not opened in the diff viewer').not.toBeNull()
    const rows = [...diff!.querySelectorAll('nav button.diff-file')].map((b) => b.getAttribute('data-path'))
    expect(rows).toEqual(['src/a.ts', 'b.md'])
    expect(diff!.querySelector('[data-testid="diff-files-head"]')?.textContent).toBe('2 files +3 −1')
    const scroller = diff!.querySelector('.diff-scroll')!
    expect(scroller.getAttribute('data-open-file')).toBe('src/a.ts')
    const add = [...scroller.querySelectorAll('.diff-line.is-add')]
    const del = [...scroller.querySelectorAll('.diff-line.is-del')]
    expect(add).toHaveLength(2)
    expect(del).toHaveLength(1)
    // NEVER HUE ALONE: the sign is text in its own column.
    for (const l of add) expect(l.querySelector('.diff-sign')?.textContent).toBe('+')
    for (const l of del) expect(l.querySelector('.diff-sign')?.textContent).toBe('-')
    expect(scroller.querySelector('[data-diff-row="hunk"]')?.textContent).toContain('@@ -1,2 +1,3 @@')
  })

  it('under its own bar: back, name, size, attempt, Copy patch, Download -- and no second head', async () => {
    const onClose = vi.fn()
    const el = await view(content(), undefined, onClose)
    expect(el.querySelector('.art-head'), 'the generic head is drawn above the patch bar').toBeNull()
    const bar = el.querySelector<HTMLElement>('.diff-bar')!
    expect(bar.textContent).toContain('swarm-work.patch')
    expect(bar.textContent).toContain(`${DIFF.length} B`)
    expect(bar.textContent).toContain('attempt att_1')
    expect(within(bar).getByRole('button', { name: 'Copy patch' })).toBeDefined()
    expect(within(bar).getByRole('button', { name: 'Download' })).toBeDefined()
    fireEvent.click(within(bar).getByRole('button', { name: '‹ Artifacts' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    // The provenance strip still says how much this is and what was masked.
    expect(el.querySelector('.diff .art-prov')).not.toBeNull()
  })

  it('by content when the name says nothing', async () => {
    const el = await view(content({ artifact: { name: 'output.txt', bytes: 1, uri: null } }), 'output.txt')
    expect(el.querySelector('section.diff nav')).not.toBeNull()
  })

  it('leaves ordinary text alone', async () => {
    const el = await view(
      content({ artifact: { name: 'notes.txt', bytes: 5, uri: null }, content: 'hello', total_bytes: 5, returned_bytes: 5 }),
      'notes.txt',
    )
    expect(el.querySelector('.diff')).toBeNull()
    expect(el.querySelector('.art-head')).not.toBeNull()
    expect(el.querySelector('pre.art-text')?.textContent).toBe('hello')
  })

  it('says partial when the window is not the whole file, and draws its whole files only', async () => {
    const cut = DIFF + 'diff --git a/c.ts b/c.ts\n--- a/c.ts\n+++ b/c.ts\n@@ -1,9 +1,9 @@\n-cut off\n'
    const el = await view(content({ content: cut, truncated: true, next_offset: 400, total_bytes: 9000 }))
    const bar = el.querySelector('.diff-bar')
    expect(bar?.querySelector('.ctl-mark.is-partial'), 'a cut diff drew no partial mark').not.toBeNull()
    expect(bar?.textContent).toMatch(/partial/)
    expect([...el.querySelectorAll('nav button.diff-file')].map((b) => b.getAttribute('data-path'))).toEqual([
      'src/a.ts',
      'b.md',
    ])
    expect(el.querySelector('.diff-error')).toBeNull()
    const whole = await view(content())
    expect(whole.querySelector('.diff-bar .ctl-mark.is-partial')).toBeNull()
  })

  it('reads a window by the server offsets: the first, a middle and the last page', () => {
    const head = '+tail of a file cut at the front\n'
    const tail = 'diff --git a/z/cut.ts b/z/cut.ts\n--- a/z/cut.ts\n+++ b/z/cut.ts\n@@ -1,5 +1,5 @@\n-z\n'
    // The first page: from the top, its last file cut.
    expect(patchWindow(DIFF + tail, { offset: 0, next_offset: 900, truncated: true })).toEqual({
      patch: DIFF,
      before: false,
      after: 'z/cut.ts',
    })
    // A middle page: starts inside a file, ends inside another.
    expect(patchWindow(head + DIFF + tail, { offset: 900, next_offset: 1800, truncated: true })).toEqual({
      patch: DIFF,
      before: true,
      after: 'z/cut.ts',
    })
    // The last page: starts inside a file and runs to the end, so its last file is whole.
    expect(patchWindow(head + DIFF, { offset: 900, next_offset: null, truncated: false })).toEqual({
      patch: DIFF,
      before: true,
      after: null,
    })
    // One file bigger than the window, and a window that is the middle of one.
    expect(patchWindow(tail, { offset: 0, next_offset: 900, truncated: true })).toEqual({
      patch: '',
      before: false,
      after: 'z/cut.ts',
    })
    expect(patchWindow(head, { offset: 900, next_offset: 1800, truncated: true })).toEqual({
      patch: '',
      before: true,
      after: null,
    })
    // A plain (not git) diff names its cut file by its `+++` line.
    expect(patchWindow('--- a/p.txt\n+++ b/p.txt\n@@ -1,9 +1,9 @@\n-x\n', { offset: 0, next_offset: 50, truncated: true }).after).toBe(
      'p.txt',
    )
  })
})

// THE MAJOR OF THE #452 REVIEW: a patch bigger than the 512 KiB window. A
// window with no whole file drew a parse error, a later page with no header
// drew "no diff --git header", a file across a page edge vanished from both,
// and Copy and Download handed out a part of the patch named as the whole.
// MUTATION: draw `content` instead of the window's whole files; drop the
// `empty` notice; copy or download the window as `Copy patch` / a blob -- each
// goes red below.
describe('a patch bigger than one window', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const BIG = 'diff --git a/src/big.ts b/src/big.ts\n--- a/src/big.ts\n+++ b/src/big.ts\n@@ -1,400 +1,400 @@\n-old\n+new\n'

  it('one file over the window: no parse error, the cut file named, and the window copied as a window', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const el = await view(content({ content: BIG, truncated: true, next_offset: BIG.length, total_bytes: 900_000 }))
    expect(el.querySelector('.diff-error'), 'a window with no whole file drew a parse error').toBeNull()
    expect(el.querySelector('.diff-empty')?.textContent).toMatch(/No file lies wholly inside this window/)
    expect(el.querySelector('.diff-cut')?.textContent).toContain('src/big.ts cut by the window, page on.')
    const bar = el.querySelector<HTMLElement>('.diff-bar')!
    expect(within(bar).queryByRole('button', { name: 'Copy patch' }), 'a window is offered as the patch').toBeNull()
    fireEvent.click(within(bar).getByRole('button', { name: 'Copy window' }))
    expect(writeText).toHaveBeenCalledWith(BIG)
    // A caller's own loader has no route to the whole object: refused, with the reason.
    const dl = within(bar).getByRole('button', { name: 'Download' }) as HTMLButtonElement
    expect(dl.disabled).toBe(true)
    expect(dl.title).toMatch(/window of the patch/)
  })

  it('a window that starts mid-file says so and draws the files after its first header', async () => {
    const mid = '+tail of src/a.ts\n' + DIFF
    const el = await view(content({ content: mid, offset: 4096, next_offset: null, truncated: false, total_bytes: 9000 }))
    expect(el.querySelector('.diff-error')).toBeNull()
    expect([...el.querySelectorAll('nav button.diff-file')].map((b) => b.getAttribute('data-path'))).toEqual([
      'src/a.ts',
      'b.md',
    ])
    expect(el.querySelector('.diff-cut')?.textContent).toMatch(/starts inside a file the page before it cut/)
  })

  it('a middle page with no header at all is a notice, not an error', async () => {
    const el = await view(content({ content: '+line\n+line\n', offset: 4096, next_offset: 8192, truncated: true, total_bytes: 900_000 }))
    expect(el.querySelector('.diff-error')).toBeNull()
    expect(el.querySelector('.diff-empty')?.textContent).toMatch(/No file lies wholly inside this window/)
  })
})

describe('a window of an artifact downloads the whole object', () => {
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it('through the artifact raw route, never a blob of the window', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const cut = DIFF + 'diff --git a/c.ts b/c.ts\n--- a/c.ts\n+++ b/c.ts\n@@ -1,9 +1,9 @@\n-cut off\n'
    const body = content({ content: cut, truncated: true, next_offset: 400, total_bytes: 9000, artifact: { name: 'change.diff', bytes: 9000, uri: null } })
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })),
    )
    const live = await import('../ArtifactViewer')
    const { container } = render(
      <live.ArtifactViewer taskId="tsk_diff" artifact={{ name: 'change.diff', bytes: 9000, uri: 'gs://b/change.diff' }} onClose={() => {}} />,
    )
    const a = await waitFor(() => {
      const el = container.querySelector<HTMLAnchorElement>('.diff-bar a.diff-btn[download]')
      expect(el, 'a partial patch still downloads a blob of its window').not.toBeNull()
      return el!
    })
    expect(a.textContent).toBe('Download')
    expect(a.getAttribute('href')).toBe(
      `/v1/tasks/tsk_diff/artifacts/raw?${new URLSearchParams({ name: 'change.diff', disposition: 'attachment' }).toString()}`,
    )
    expect(a.getAttribute('download')).toBe('change.diff')
    expect(container.querySelector('.diff-cut')?.textContent).toContain('c.ts cut by the window, page on.')
  })
})

describe('the back button says where it goes', () => {
  it('is `‹ Artifacts` by default and the caller’s words elsewhere', async () => {
    const el = await view(content())
    expect(el.querySelector('.diff-back')?.textContent).toBe('‹ Artifacts')
    const got: Result<ArtifactContent> = { status: 'ok', data: content(), fetchedAt: Date.now() }
    const { container } = render(
      <ArtifactViewer
        taskId="tsk_diff"
        artifact={{ name: 'swarm-work.patch', bytes: 1, uri: 'gs://b/x' }}
        onClose={() => {}}
        load={() => Promise.resolve(got)}
        backLabel="Close"
      />,
    )
    await waitFor(() => expect(container.querySelector('.diff-back')?.textContent).toBe('Close'))
  })
})

describe('ArtifactViewer holds no diff renderer of its own', () => {
  it('imports the #310 viewer and draws no art-diff rows', () => {
    expect(VIEWER_SOURCE).toContain("from './diff/DiffView'")
    expect(VIEWER_SOURCE).not.toMatch(/function DiffView\b/)
    expect(VIEWER_SOURCE).not.toMatch(/art-diff/)
  })

  it('and the stylesheet has no art-diff rules left', () => {
    expect(STYLES).not.toMatch(/\.art-diff/)
  })
})
