// FIND IN THE DIFF (#310): a string searched across EVERY file of the patch,
// not only the open one, with an `n of N` counter, next and previous that
// switch the open file, Enter / Shift+Enter in the box, `/` to reach it, and
// the matches marked in the rows the window draws.
//
// MUTATION: search only the open file, count only the drawn rows, leave the
// open file where it is when the next match lives in another one, drop the
// marks, or send `/` back to the path filter -- each goes red below.

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DiffView } from '../../diff/DiffView'
import { findMatches, firstMatchFrom, hitSegments } from '../../diff/find'
import { parseUnifiedDiff } from '../../diff/parse'
import { groupByDirectory, listOrder } from '../../diff/rows'

/** Four files in two directories, in this patch order: M, A, D, R. List order: one, two, new, guide. */
const FOUR = [
  'diff --git a/src/app/one.ts b/src/app/one.ts',
  'index 1111111..2222222 100644',
  '--- a/src/app/one.ts',
  '+++ b/src/app/one.ts',
  '@@ -1,3 +1,4 @@',
  ' const a = 1',
  '-const b = 2',
  '+const b = 3',
  '+const c = 4',
  ' export { a }',
  'diff --git a/docs/guide.md b/docs/guide.md',
  'new file mode 100644',
  'index 0000000..3333333',
  '--- /dev/null',
  '+++ b/docs/guide.md',
  '@@ -0,0 +1,3 @@',
  '+# Guide',
  '+',
  '+Read me.',
  'diff --git a/src/app/two.ts b/src/app/two.ts',
  'deleted file mode 100644',
  'index 4444444..0000000',
  '--- a/src/app/two.ts',
  '+++ /dev/null',
  '@@ -1,2 +0,0 @@',
  '-const two = 2',
  '-export { two }',
  'diff --git a/src/app/old.ts b/src/app/new.ts',
  'similarity index 80%',
  'rename from src/app/old.ts',
  'rename to src/app/new.ts',
  'index 5555555..6666666 100644',
  '--- a/src/app/old.ts',
  '+++ b/src/app/new.ts',
  '@@ -1,2 +1,2 @@',
  "-export const name = 'old'",
  "+export const name = 'new'",
  ' export default name',
  '',
].join('\n')

/** `files` files of `lines` changed lines each, alternating -/+ halves. */
function bigPatch(files: number, lines: number): string {
  const out: string[] = []
  for (let f = 0; f < files; f++) {
    out.push(`diff --git a/big${f}.txt b/big${f}.txt`, `--- a/big${f}.txt`, `+++ b/big${f}.txt`)
    out.push(`@@ -1,${lines / 2} +1,${lines / 2} @@`)
    for (let i = 0; i < lines / 2; i++) out.push(`-old ${f}:${i}`)
    for (let i = 0; i < lines / 2; i++) out.push(`+new ${f}:${i}`)
  }
  return out.join('\n') + '\n'
}

function scroller(): HTMLElement {
  return screen.getByRole('region', { name: 'Diff lines' })
}

function findBox(): HTMLInputElement {
  return screen.getByRole('searchbox', { name: 'Find in diff' }) as HTMLInputElement
}

function count(): string {
  return screen.getByTestId('diff-find-count').textContent ?? ''
}

function openFile(): string | null {
  return scroller().getAttribute('data-open-file')
}

function marks(): HTMLElement[] {
  return Array.from(scroller().querySelectorAll<HTMLElement>('mark.diff-hit'))
}

function find(q: string): void {
  fireEvent.change(findBox(), { target: { value: q } })
}

function openListRow(path: string): void {
  const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
  const row = Array.from(nav.querySelectorAll<HTMLElement>('button.diff-file')).find(
    (b) => b.getAttribute('data-path') === path,
  )
  if (!row) throw new Error(`no list row for ${path}`)
  fireEvent.click(row)
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
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('findMatches, the model', () => {
  const parsed = parseUnifiedDiff(FOUR)
  if (!parsed.ok) throw new Error('fixture did not parse')
  const files = parsed.files
  const order = listOrder(groupByDirectory(files))
  const pathOf = (fi: number): string => files[fi]!.path

  it('finds a string in every file, in list order, case-insensitively', () => {
    const m = findMatches(files, order, 'EXPORT')
    expect(m.map((x) => pathOf(x.file))).toEqual([
      'src/app/one.ts',
      'src/app/two.ts',
      'src/app/new.ts',
      'src/app/new.ts',
      'src/app/new.ts',
    ])
    // Offsets are into the line's text as given.
    const first = m[0]!
    const text = files[first.file]!.hunks[first.hunk]!.lines[first.line]!.text
    expect(text.slice(first.start, first.end)).toBe('export')
  })

  it('finds every occurrence on a line, and treats the query as text, not a pattern', () => {
    expect(findMatches(files, order, 'e').filter((x) => pathOf(x.file) === 'docs/guide.md')).toHaveLength(3)
    expect(findMatches(files, order, '.')).toHaveLength(1)
    expect(findMatches(files, order, '{ a }')).toHaveLength(1)
  })

  it('finds nothing for an empty query or a string the patch does not hold', () => {
    expect(findMatches(files, order, '')).toEqual([])
    expect(findMatches(files, order, 'zebra')).toEqual([])
  })

  it('starts from the open file, or the first file after it, wrapping', () => {
    const m = findMatches(files, order, 'const')
    const two = files.findIndex((f) => f.path === 'src/app/two.ts')
    const guide = files.findIndex((f) => f.path === 'docs/guide.md')
    expect(pathOf(m[firstMatchFrom(m, order, two)]!.file)).toBe('src/app/two.ts')
    // guide.md is last in the list and holds no `const`: the next is the first.
    expect(firstMatchFrom(m, order, guide)).toBe(0)
    expect(firstMatchFrom([], order, guide)).toBe(-1)
  })

  it('cuts a line into plain and marked pieces without changing a character', () => {
    const pieces = hitSegments('a const b const', [
      { start: 2, end: 7, index: 4 },
      { start: 10, end: 15, index: 5 },
    ])
    expect(pieces.map((p) => p.text).join('')).toBe('a const b const')
    expect(pieces.map((p) => p.index)).toEqual([null, 4, null, 5])
  })
})

describe('find in the diff, as drawn', () => {
  it('counts the matches of every file, not only the open one, as n of N', () => {
    render(<DiffView patch={FOUR} />)
    expect(openFile()).toBe('src/app/one.ts')
    find('export')
    // one.ts holds 1, two.ts 1, new.ts 3.
    expect(count()).toBe('1 of 5')
    expect(openFile()).toBe('src/app/one.ts')
  })

  it('moves with Enter and Shift+Enter, switching the open file, and wraps', () => {
    render(<DiffView patch={FOUR} />)
    find('export')
    const box = findBox()
    expect(fireEvent.keyDown(box, { key: 'Enter' })).toBe(false)
    expect(count()).toBe('2 of 5')
    expect(openFile()).toBe('src/app/two.ts')
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(count()).toBe('3 of 5')
    expect(openFile()).toBe('src/app/new.ts')
    expect(fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })).toBe(false)
    expect(count()).toBe('2 of 5')
    expect(openFile()).toBe('src/app/two.ts')
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(count()).toBe('5 of 5')
    expect(openFile()).toBe('src/app/new.ts')
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(count()).toBe('1 of 5')
    expect(openFile()).toBe('src/app/one.ts')
  })

  it('moves with the next and previous buttons too', () => {
    render(<DiffView patch={FOUR} />)
    find('const')
    fireEvent.click(screen.getByRole('button', { name: 'Next match' }))
    expect(count()).toBe('2 of 7')
    fireEvent.click(screen.getByRole('button', { name: 'Previous match' }))
    fireEvent.click(screen.getByRole('button', { name: 'Previous match' }))
    expect(count()).toBe('7 of 7')
    expect(openFile()).toBe('src/app/new.ts')
  })

  it('starts at the open file rather than the top of the patch', () => {
    render(<DiffView patch={FOUR} />)
    openListRow('src/app/two.ts')
    find('const')
    expect(count()).toBe('5 of 7')
    expect(openFile()).toBe('src/app/two.ts')
  })

  it('marks every match in the drawn rows, one of them current, without changing the text', () => {
    render(<DiffView patch={FOUR} />)
    find('CONST')
    const m = marks()
    expect(m).toHaveLength(4)
    // The mark holds the patch's characters, not the query's.
    expect(m.every((x) => x.textContent === 'const')).toBe(true)
    expect(m.filter((x) => x.classList.contains('is-current'))).toHaveLength(1)
    const texts = Array.from(scroller().querySelectorAll('[data-diff-row="line"] .diff-text')).map((t) => t.textContent)
    expect(texts).toEqual(['const a = 1', 'const b = 2', 'const b = 3', 'const c = 4', 'export { a }'])
    fireEvent.keyDown(findBox(), { key: 'Enter' })
    const current = marks().filter((x) => x.classList.contains('is-current'))
    expect(current).toHaveLength(1)
    expect(current[0]!.closest('[data-diff-row]')!.querySelector('.diff-text')!.textContent).toBe('const b = 2')
  })

  it('marks matches in the split view as well', () => {
    render(<DiffView patch={FOUR} />)
    fireEvent.click(screen.getByRole('button', { name: 'Split' }))
    find('const b')
    expect(count()).toBe('1 of 2')
    expect(marks()).toHaveLength(2)
    expect(Array.from(scroller().querySelectorAll('[data-diff-row="pair"] mark.diff-hit'))).toHaveLength(2)
  })

  it('says so when nothing matches, and Escape clears the find', () => {
    render(<DiffView patch={FOUR} />)
    find('zebra')
    expect(count()).toBe('no matches')
    expect(screen.getByRole('button', { name: 'Next match' })).toHaveProperty('disabled', true)
    find('const')
    expect(marks().length).toBeGreaterThan(0)
    expect(fireEvent.keyDown(findBox(), { key: 'Escape' })).toBe(false)
    expect(findBox().value).toBe('')
    expect(count()).toBe('')
    expect(marks()).toHaveLength(0)
  })

  it('keeps a clearing Escape inside the viewer, so the drawer around it stays open', () => {
    const outer = vi.fn()
    render(
      <div onKeyDown={(e) => outer(e.key)}>
        <DiffView patch={FOUR} />
      </div>,
    )
    find('const')
    fireEvent.keyDown(findBox(), { key: 'Escape' })
    expect(findBox().value).toBe('')
    expect(outer).not.toHaveBeenCalled()
    // An empty find box lets Escape through, so the drawer can still close.
    fireEvent.keyDown(findBox(), { key: 'Escape' })
    expect(outer).toHaveBeenCalledTimes(1)

    const filter = screen.getByRole('searchbox', { name: 'Filter files by path' }) as HTMLInputElement
    fireEvent.change(filter, { target: { value: 'app' } })
    fireEvent.keyDown(filter, { key: 'Escape' })
    expect(filter.value).toBe('')
    expect(outer).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(filter, { key: 'Escape' })
    expect(outer).toHaveBeenCalledTimes(2)
  })

  it('is reached with /, and the path filter no longer takes it', () => {
    render(<DiffView patch={FOUR} />)
    expect(fireEvent.keyDown(scroller(), { key: '/' })).toBe(false)
    expect(document.activeElement).toBe(findBox())
    expect(findBox().getAttribute('aria-keyshortcuts')).toBe('/')
    expect(screen.getByRole('searchbox', { name: 'Filter files by path' }).getAttribute('aria-keyshortcuts')).toBeNull()
  })

  it('finds across files at phone width, where there is no path filter', () => {
    vi.stubGlobal('innerWidth', 600)
    render(<DiffView patch={FOUR} />)
    expect(fireEvent.keyDown(scroller(), { key: '/' })).toBe(false)
    expect(document.activeElement).toBe(findBox())
    find('guide')
    expect(count()).toBe('1 of 1')
    expect(openFile()).toBe('docs/guide.md')
    expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 4 of 4')
  })

  it('jumps to a match deep in another file of a big patch and draws it, still as a window', () => {
    render(<DiffView patch={bigPatch(3, 20_000)} />)
    find('new 2:9999')
    expect(count()).toBe('1 of 1')
    expect(openFile()).toBe('big2.txt')
    expect(scroller().scrollTop).toBeGreaterThan(22 * 19_000)
    const drawn = scroller().querySelectorAll('[data-diff-row]')
    expect(drawn.length).toBeLessThan(300)
    const m = marks()
    expect(m).toHaveLength(1)
    expect(m[0]!.classList.contains('is-current')).toBe(true)
    expect(m[0]!.closest('[data-diff-row]')!.querySelector('.diff-text')!.textContent).toBe('new 2:9999')
  })
})
