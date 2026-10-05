// THE DIFF VIEWER'S FILE LIST AND ONE OPEN FILE (owner decision 2026-10-01).
//
// A patch is read a file at a time: a list column on the left, grouped by
// directory in patch order, and only the selected file's diff on the right.
// Viewed dots live in the component and nowhere else; the open file is never
// in the address; split is offered only from 700px of pane width; under it the
// list is a picker.
//
// MUTATION: draw every file in the scroller, keep `viewed` in localStorage,
// write the open file into `location.hash`, or offer Split under 700px --
// each goes red below.

import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DiffView } from '../../diff/DiffView'
import { barCells } from '../../diff/rows'

/** Four files in two directories, in this patch order: M, A, D, R. */
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

function nav(): HTMLElement {
  return screen.getByRole('navigation', { name: 'Files in this diff' })
}

function listRows(): HTMLElement[] {
  return Array.from(nav().querySelectorAll<HTMLElement>('button.diff-file'))
}

function row(path: string): HTMLElement {
  const r = listRows().find((b) => b.getAttribute('data-path') === path)
  if (!r) throw new Error(`no list row for ${path}`)
  return r
}

function scroller(): HTMLElement {
  return screen.getByRole('region', { name: 'Diff lines' })
}

/** The paths the diff column draws rows for. */
function drawnPaths(): string[] {
  const paths = Array.from(scroller().querySelectorAll<HTMLElement>('[data-diff-row]')).map(
    (r) => r.getAttribute('data-path') ?? '',
  )
  return [...new Set(paths)]
}

function viewed(path: string): boolean {
  return row(path).getAttribute('data-viewed') === 'true'
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

describe('the diff file list', () => {
  it('draws a 4-file patch as 4 rows in directory groups, with the right counts, and opens file 1', () => {
    render(<DiffView patch={FOUR} />)
    expect(screen.getByTestId('diff-files-head').textContent).toBe('4 files +6 −4')

    const groups = Array.from(nav().querySelectorAll<HTMLElement>('.diff-dir'))
    expect(groups.map((g) => g.getAttribute('aria-label'))).toEqual(['src/app', 'docs'])
    const inGroup = (g: HTMLElement): string[] =>
      Array.from(g.querySelectorAll('button.diff-file')).map((b) => b.getAttribute('data-path') ?? '')
    // Patch order inside each directory.
    expect(inGroup(groups[0]!)).toEqual(['src/app/one.ts', 'src/app/two.ts', 'src/app/new.ts'])
    expect(inGroup(groups[1]!)).toEqual(['docs/guide.md'])
    expect(listRows()).toHaveLength(4)

    const facts = (path: string): string[] =>
      ['.diff-kind', '.diff-fname', '.diff-plus', '.diff-minus'].map((sel) => row(path).querySelector(sel)?.textContent ?? '')
    expect(facts('src/app/one.ts')).toEqual(['M', 'one.ts', '+2', '−1'])
    expect(facts('docs/guide.md')).toEqual(['A', 'guide.md', '+3', '−0'])
    expect(facts('src/app/two.ts')).toEqual(['D', 'two.ts', '+0', '−2'])
    expect(facts('src/app/new.ts')).toEqual(['R', 'new.ts', '+1', '−1'])

    // Five cells each, split in proportion.
    const cells = (path: string): string[] =>
      Array.from(row(path).querySelectorAll('.diff-cell')).map((c) => c.className.replace('diff-cell ', ''))
    expect(cells('src/app/one.ts')).toEqual(['is-add', 'is-add', 'is-add', 'is-del', 'is-del'])
    expect(cells('docs/guide.md')).toEqual(Array(5).fill('is-add'))
    expect(cells('src/app/two.ts')).toEqual(Array(5).fill('is-del'))

    // File 1 is open, and is the selected row.
    expect(drawnPaths()).toEqual(['src/app/one.ts'])
    expect(row('src/app/one.ts').getAttribute('aria-current')).toBe('true')
    expect(row('src/app/one.ts').classList.contains('is-on')).toBe(true)
    expect(listRows().filter((r) => r.classList.contains('is-on'))).toHaveLength(1)
  })

  it('opens another file on a click, and marks the one before it viewed', () => {
    render(<DiffView patch={FOUR} />)
    expect(viewed('src/app/one.ts')).toBe(true)
    expect(viewed('docs/guide.md')).toBe(false)
    fireEvent.click(row('docs/guide.md'))
    expect(drawnPaths()).toEqual(['docs/guide.md'])
    expect(row('docs/guide.md').getAttribute('aria-current')).toBe('true')
    expect(row('src/app/one.ts').getAttribute('aria-current')).toBeNull()
    expect(viewed('src/app/one.ts')).toBe(true)
    expect(viewed('docs/guide.md')).toBe(true)
    expect(viewed('src/app/two.ts')).toBe(false)
  })

  it('opens the next file in list order on n, and marks the one before it viewed', () => {
    render(<DiffView patch={FOUR} />)
    expect(fireEvent.keyDown(scroller(), { key: 'n' })).toBe(false)
    // The list's order: src/app's three files, then docs.
    expect(drawnPaths()).toEqual(['src/app/two.ts'])
    expect(viewed('src/app/one.ts')).toBe(true)
    expect(viewed('src/app/two.ts')).toBe(true)
    fireEvent.keyDown(scroller(), { key: 'n' })
    fireEvent.keyDown(scroller(), { key: 'n' })
    expect(drawnPaths()).toEqual(['docs/guide.md'])
    // Past the last file, n stays.
    fireEvent.keyDown(scroller(), { key: 'n' })
    expect(drawnPaths()).toEqual(['docs/guide.md'])
  })

  it('clears the viewed dots on a remount and stores nothing', () => {
    const first = render(<DiffView patch={FOUR} />)
    fireEvent.click(row('docs/guide.md'))
    fireEvent.click(row('src/app/new.ts'))
    expect(listRows().filter((r) => r.getAttribute('data-viewed') === 'true')).toHaveLength(3)
    first.unmount()
    render(<DiffView patch={FOUR} />)
    expect(listRows().filter((r) => r.getAttribute('data-viewed') === 'true').map((r) => r.getAttribute('data-path'))).toEqual([
      'src/app/one.ts',
    ])
    // Only the Unified/Split choice is ever stored, and it was never made here.
    expect([...store.keys()]).toEqual([])
  })

  it('never puts the open file in the address', () => {
    const at = '/agents/live/tsk_1/artifacts/change.diff'
    window.history.replaceState(null, '', at)
    const before = window.location.href
    render(<DiffView patch={FOUR} />)
    fireEvent.click(row('docs/guide.md'))
    fireEvent.keyDown(scroller(), { key: 'p' })
    expect(drawnPaths()).toEqual(['src/app/new.ts'])
    expect(window.location.href).toBe(before)
    expect(window.location.pathname).toBe(at)
    expect(window.location.hash).toBe('')
    expect(window.location.search).toBe('')
    // And no link in the viewer would put it there either.
    expect(document.querySelectorAll('.diff a[href]')).toHaveLength(0)
  })

  it('filters by path', () => {
    render(<DiffView patch={FOUR} />)
    // `/` is find in the diff (find.test.tsx); the filter is reached by Tab or a click.
    const box = screen.getByRole('searchbox', { name: 'Filter files by path' })
    fireEvent.change(box, { target: { value: 'DOCS' } })
    expect(listRows().map((r) => r.getAttribute('data-path'))).toEqual(['docs/guide.md'])
    expect(nav().textContent).toContain('1 of 4 shown')
    // The header still counts the patch, not the filter.
    expect(screen.getByTestId('diff-files-head').textContent).toBe('4 files +6 −4')
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(drawnPaths()).toEqual(['docs/guide.md'])
  })
})

describe('the diff bar', () => {
  it('carries back, name, size, attempt, the layout toggle, Copy patch and Download', () => {
    const back = vi.fn()
    render(<DiffView patch={FOUR} name="change.diff" size="1 KiB" attempt="att_7" onBack={back} />)
    const bar = document.querySelector<HTMLElement>('.diff-bar')!
    expect(bar.textContent).toContain('change.diff')
    expect(bar.textContent).toContain('1 KiB')
    expect(bar.textContent).toContain('attempt att_7')
    fireEvent.click(within(bar).getByRole('button', { name: '‹ Artifacts' }))
    expect(back).toHaveBeenCalledTimes(1)
    expect(within(bar).getByRole('button', { name: 'Unified' })).toBeDefined()
    expect(within(bar).getByRole('button', { name: 'Split' })).toBeDefined()
    expect(within(bar).getByRole('button', { name: 'Copy patch' })).toBeDefined()
    expect(within(bar).getByRole('button', { name: 'Download' })).toBeDefined()
    // No whole-patch reading mode. Expand all (owner decision 2026-10-05)
    // unfolds collapsed files; it still draws only the open one.
    expect(within(bar).queryByRole('button', { name: /whole|all files/i })).toBeNull()
    fireEvent.click(within(bar).getByRole('button', { name: 'Collapse all' }))
    fireEvent.click(within(bar).getByRole('button', { name: 'Expand all' }))
    expect(drawnPaths()).toEqual(['src/app/one.ts'])
  })

  it('copies the whole patch', async () => {
    const writeText = vi.fn(() => Promise.resolve())
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<DiffView patch={FOUR} />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy patch' }))
    expect(writeText).toHaveBeenCalledWith(FOUR)
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeDefined()
  })
})

describe('diff split and the phone picker', () => {
  it('offers split only from 700px of pane width', () => {
    vi.stubGlobal('innerWidth', 699)
    const narrow = render(<DiffView patch={FOUR} />)
    expect(screen.queryByRole('button', { name: 'Split' })).toBeNull()
    narrow.unmount()
    vi.stubGlobal('innerWidth', 700)
    render(<DiffView patch={FOUR} />)
    expect(screen.getByRole('button', { name: 'Split' })).toBeDefined()
  })

  it('measures the PANE, not the window: a 650px pane in a 1280px window offers no split', () => {
    // jsdom lays nothing out, so every clientWidth is 0 and the viewer would
    // fall back to the window. Give the pane a width of its own.
    const width = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 650 })
    try {
      vi.stubGlobal('innerWidth', 1280)
      render(<DiffView patch={FOUR} />)
      expect(screen.queryByRole('button', { name: 'Split' }), 'split was offered by the window width').toBeNull()
      expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 1 of 4')
    } finally {
      if (width) Object.defineProperty(HTMLElement.prototype, 'clientWidth', width)
    }
  })

  it('turns the list into a picker under 700px, with previous and next, always unified', () => {
    store.set('swarm.diff.view', 'split')
    vi.stubGlobal('innerWidth', 600)
    render(<DiffView patch={FOUR} />)
    expect(screen.queryByRole('navigation', { name: 'Files in this diff' })).toBeNull()
    const picker = screen.getByRole('group', { name: 'File' })
    expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 1 of 4')
    expect(picker.textContent).toContain('src/app/one.ts')
    expect(picker.textContent).toContain('+2')
    expect(picker.textContent).toContain('−1')
    const prev = within(picker).getByRole('button', { name: 'Previous file' }) as HTMLButtonElement
    expect(prev.disabled).toBe(true)
    fireEvent.click(within(picker).getByRole('button', { name: 'Next file' }))
    expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 2 of 4')
    expect(drawnPaths()).toEqual(['src/app/two.ts'])
    expect(scroller().querySelectorAll('[data-diff-row="pair"]')).toHaveLength(0)
    fireEvent.click(within(picker).getByRole('button', { name: 'Previous file' }))
    expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 1 of 4')
  })
})

describe('the diff +/- bar cells', () => {
  it('splits five cells in proportion and keeps one for a side that changed at all', () => {
    expect(barCells(0, 0)).toEqual(Array(5).fill('none'))
    expect(barCells(1000, 1)).toEqual(['add', 'add', 'add', 'add', 'del'])
    expect(barCells(1, 1000)).toEqual(['add', 'del', 'del', 'del', 'del'])
    expect(barCells(3, 3).filter((c) => c === 'add').length + barCells(3, 3).filter((c) => c === 'del').length).toBe(5)
  })
})
