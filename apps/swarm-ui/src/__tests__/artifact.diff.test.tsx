// #104: A DIFF IS DRAWN AS A DIFF.
//
// Images already render inline in Artifacts; a patch rendered as a grey `<pre>`
// is the one output a reviewer most wants to read and the one this viewer read
// worst. A diff is recognised by NAME (`.patch`, `.diff` -- `swarm-work.patch`,
// `change.diff`) or by CONTENT (`diff --git` at the top, or `@@` hunks), and is
// drawn line by line with a +/- glyph gutter, so an added line is told from a
// removed one without reading its colour. Long lines scroll by default and the
// scroll is said on the glass; wrapping is a toggle. A window that is not the
// whole file says `partial` beside the diff.
//
// MUTATION: return 'text' for `.patch` in `artifactKind`, drop the glyph from
// the gutter, or default the wrap toggle to on -- each goes red below.

import { describe, expect, it } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/react'

import { ArtifactViewer, diffStat } from '../ArtifactViewer'
import type { Result } from '../fetch'
import { artifactKind, looksLikeDiff, type ArtifactContent } from '../types'

const DIFF = [
  'diff --git a/src/a.ts b/src/a.ts',
  'index 111..222 100644',
  '--- a/src/a.ts',
  '+++ b/src/a.ts',
  '@@ -1,3 +1,4 @@ export function a() {',
  ' const keep = 1',
  '-const gone = 2',
  '+const added = 2',
  '+const more = 3',
  'diff --git a/b.md b/b.md',
  '--- a/b.md',
  '+++ b/b.md',
  '@@ -1 +1 @@',
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

async function view(data: ArtifactContent, name = data.artifact.name ?? 'x'): Promise<HTMLElement> {
  const got: Result<ArtifactContent> = { status: 'ok', data, fetchedAt: Date.now() }
  const load = () => Promise.resolve(got)
  const { container } = render(
    <ArtifactViewer taskId="tsk_diff" artifact={{ name, bytes: 1, uri: 'gs://b/x' }} onClose={() => {}} load={load} />,
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

describe('the viewer draws a diff line by line', () => {
  it('with a +/- glyph gutter and styled hunk headers', async () => {
    const el = await view(content())
    const diff = el.querySelector('.art-diff')
    expect(diff, 'a .patch rendered as plain text').not.toBeNull()
    const add = [...diff!.querySelectorAll('.art-diff-line.is-add')]
    const del = [...diff!.querySelectorAll('.art-diff-line.is-del')]
    expect(add).toHaveLength(3)
    expect(del).toHaveLength(1)
    // NEVER HUE ALONE: the glyph is text in the gutter.
    for (const l of add) expect(l.querySelector('.art-diff-glyph')?.textContent).toBe('+')
    for (const l of del) expect(l.querySelector('.art-diff-glyph')?.textContent).toBe('−')
    const hunks = [...diff!.querySelectorAll('.art-diff-line.is-hunk')]
    expect(hunks).toHaveLength(2)
    expect(hunks[0]!.textContent).toContain('@@ -1,3 +1,4 @@')
    // `+++ b/src/a.ts` is a file header, not an added line.
    const meta = [...diff!.querySelectorAll('.art-diff-line.is-meta')].map((l) => l.textContent)
    expect(meta.some((t) => t?.includes('+++ b/src/a.ts'))).toBe(true)
  })

  it('by content when the name says nothing', async () => {
    const el = await view(content({ artifact: { name: 'output.txt', bytes: 1, uri: null } }), 'output.txt')
    expect(el.querySelector('.art-diff')).not.toBeNull()
  })

  it('leaves ordinary text alone', async () => {
    const el = await view(
      content({ artifact: { name: 'notes.txt', bytes: 5, uri: null }, content: 'hello', total_bytes: 5, returned_bytes: 5 }),
      'notes.txt',
    )
    expect(el.querySelector('.art-diff')).toBeNull()
    expect(el.querySelector('pre.art-text')?.textContent).toBe('hello')
  })

  it('does not wrap by default, says it scrolls, and wraps on the toggle', async () => {
    const el = await view(content())
    const diff = el.querySelector('.art-diff')!
    const toggle = el.querySelector<HTMLButtonElement>('button.art-diff-wrap')
    expect(toggle, 'no wrap toggle').not.toBeNull()
    expect(toggle!.getAttribute('aria-pressed')).toBe('false')
    expect(diff.classList.contains('is-wrapped')).toBe(false)
    expect(el.querySelector('.art-diff-cue')?.textContent).toMatch(/scroll/)
    fireEvent.click(toggle!)
    expect(toggle!.getAttribute('aria-pressed')).toBe('true')
    expect(el.querySelector('.art-diff')!.classList.contains('is-wrapped')).toBe(true)
    expect(el.querySelector('.art-diff-cue')).toBeNull()
  })

  it('says partial when the window is not the whole file', async () => {
    const el = await view(content({ truncated: true, next_offset: 400, total_bytes: 9000 }))
    const bar = el.querySelector('.art-diff-bar')
    expect(bar?.querySelector('.ctl-mark.is-partial'), 'a cut diff drew no partial mark').not.toBeNull()
    expect(bar?.textContent).toMatch(/partial/)
    const whole = await view(content())
    expect(whole.querySelector('.art-diff-bar .ctl-mark.is-partial')).toBeNull()
  })
})
