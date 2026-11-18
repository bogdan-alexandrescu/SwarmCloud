// THE VIEWER AS THE CHANGES TAB NEEDS IT (lane DIFF1, design
// docs/design/diff-viewer.md §5; owner decisions of 2026-10-08, §3).
//
// One describe per piece of the lane: full height; `initialFile` and
// `onFileChange`; the pull-request link; the `from step` rows of variant 5;
// the phone's picker and `All files` sheet; and the lazily loaded syntax
// highlighter, which must reach the DOM as text nodes and spans and never
// through an HTML setter.
//
// MUTATION: drop `is-full`, ignore `initialFile`, call `onFileChange` on
// mount or not at all, link a `javascript:` PR address, draw a gap between
// two steps' hunks, stack every file at phone width, or set a line's colour
// through `dangerouslySetInnerHTML` -- each goes red below.

import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DiffView } from '../../diff/DiffView'
import { loadHighlighter } from '../../diff/useHighlighter'

/** Three files, in patch order: a TypeScript file with two hunks, a Python file, a text file. */
const THREE = [
  'diff --git a/src/app/one.ts b/src/app/one.ts',
  '--- a/src/app/one.ts',
  '+++ b/src/app/one.ts',
  '@@ -1,3 +1,3 @@',
  ' const a = 1',
  '-const b = "two"',
  '+const b = "three"',
  ' export { a }',
  '@@ -20,2 +20,3 @@',
  ' // tail',
  '+return 42',
  ' }',
  'diff --git a/tools/run.py b/tools/run.py',
  '--- a/tools/run.py',
  '+++ b/tools/run.py',
  '@@ -1,2 +1,2 @@',
  '-def old(): pass',
  '+def new(): return None',
  ' # done',
  'diff --git a/notes.txt b/notes.txt',
  '--- a/notes.txt',
  '+++ b/notes.txt',
  '@@ -1 +1 @@',
  '-if this were code',
  '+it would not be coloured',
  '',
].join('\n')

function scroller(): HTMLElement {
  return screen.getByRole('region', { name: 'Diff lines' })
}

function rows(kind?: string): HTMLElement[] {
  const sel = kind ? `[data-diff-row="${kind}"]` : '[data-diff-row]'
  return Array.from(scroller().querySelectorAll<HTMLElement>(sel))
}

/** The paths of every row drawn in the scroller: ONE path when one file is drawn. */
function drawnPaths(): string[] {
  return [...new Set(rows().map((r) => r.getAttribute('data-path') ?? ''))]
}

function listRow(path: string): HTMLElement {
  const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
  const row = nav.querySelector<HTMLElement>(`button.diff-file[data-path="${path}"]`)
  if (!row) throw new Error(`no list row for ${path}`)
  return row
}

/** `matchMedia` answering the console's phone query (560px) as `phone`. */
function stubPhone(phone: boolean): void {
  vi.stubGlobal('innerWidth', phone ? 390 : 1280)
  vi.stubGlobal(
    'matchMedia',
    (query: string) =>
      ({
        matches: phone && query.includes('max-width: 560px'),
        media: query,
        addEventListener: () => {},
        removeEventListener: () => {},
      }) as unknown as MediaQueryList,
  )
}

beforeEach(() => {
  vi.stubGlobal('localStorage', { getItem: () => null, setItem: () => {}, removeItem: () => {} })
  vi.stubGlobal('innerWidth', 1280)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('1 · full height', () => {
  it('marks the viewer to fill its pane, with the file list left and the file right at desktop width', () => {
    const { container } = render(<DiffView patch={THREE} fullHeight />)
    const section = container.querySelector('section.diff')!
    expect(section.classList.contains('is-full')).toBe(true)
    const body = section.querySelector('.diff-body')!
    // List first, file second: the grid's left and right columns.
    expect(body.children[0]!.matches('nav.diff-files')).toBe(true)
    expect(body.children[1]!.matches('.diff-main')).toBe(true)
    expect(drawnPaths()).toEqual(['src/app/one.ts'])
  })

  it('is not full height unless asked', () => {
    const { container } = render(<DiffView patch={THREE} />)
    expect(container.querySelector('section.diff')!.classList.contains('is-full')).toBe(false)
  })
})

describe('2 · initialFile and onFileChange', () => {
  it('opens the file initialFile names, and marks it in the list', () => {
    render(<DiffView patch={THREE} initialFile="tools/run.py" />)
    expect(drawnPaths()).toEqual(['tools/run.py'])
    expect(scroller().getAttribute('data-open-file')).toBe('tools/run.py')
    expect(listRow('tools/run.py').getAttribute('aria-current')).toBe('true')
  })

  it('opens the first file for a path the patch does not hold', () => {
    render(<DiffView patch={THREE} initialFile="no/such/file.ts" />)
    expect(drawnPaths()).toEqual(['src/app/one.ts'])
  })

  it('fires onFileChange with the path of each file the reader opens, and not on mount', () => {
    const onFileChange = vi.fn()
    render(<DiffView patch={THREE} initialFile="src/app/one.ts" onFileChange={onFileChange} />)
    expect(onFileChange).not.toHaveBeenCalled()
    fireEvent.click(listRow('notes.txt'))
    expect(onFileChange).toHaveBeenLastCalledWith('notes.txt')
    // p walks the list as drawn: directory groups, the root's files last.
    fireEvent.keyDown(scroller(), { key: 'p' })
    expect(onFileChange).toHaveBeenLastCalledWith('tools/run.py')
    expect(onFileChange).toHaveBeenCalledTimes(2)
    // Opening the file already open is not a change.
    fireEvent.click(listRow('tools/run.py'))
    expect(onFileChange).toHaveBeenCalledTimes(2)
  })

  it('follows a new initialFile (the route moved) without echoing it back', () => {
    const onFileChange = vi.fn()
    const { rerender } = render(<DiffView patch={THREE} initialFile="src/app/one.ts" onFileChange={onFileChange} />)
    rerender(<DiffView patch={THREE} initialFile="notes.txt" onFileChange={onFileChange} />)
    expect(drawnPaths()).toEqual(['notes.txt'])
    expect(onFileChange).not.toHaveBeenCalled()
  })

  it('does not pull the reader back when the route ignores onFileChange', () => {
    const { rerender } = render(<DiffView patch={THREE} initialFile="src/app/one.ts" />)
    fireEvent.click(listRow('tools/run.py'))
    rerender(<DiffView patch={THREE} initialFile="src/app/one.ts" />)
    expect(drawnPaths()).toEqual(['tools/run.py'])
  })
})

describe('3 · the pull-request link in the bar', () => {
  it('is drawn only when given', () => {
    const { container } = render(<DiffView patch={THREE} />)
    expect(container.querySelector('.diff-bar a')).toBeNull()
  })

  it('links the PR, naming its number, in a new tab', () => {
    render(<DiffView patch={THREE} pullRequest={{ url: 'https://github.com/o/r/pull/909' }} />)
    const a = screen.getByRole('link', { name: /PR #909/ })
    expect(a.getAttribute('href')).toBe('https://github.com/o/r/pull/909')
    expect(a.getAttribute('target')).toBe('_blank')
    expect(a.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('takes the caller\'s label', () => {
    render(<DiffView patch={THREE} pullRequest={{ url: 'https://example.test/x', label: 'Integrated PR' }} />)
    expect(screen.getByRole('link', { name: /Integrated PR/ })).toBeDefined()
  })

  it('never links an address that is not http(s)', () => {
    const { container } = render(<DiffView patch={THREE} pullRequest={{ url: 'javascript:alert(1)' }} />)
    expect(container.querySelector('a')).toBeNull()
  })
})

describe('4 · the per-step source header (variant 5)', () => {
  /** One file, three hunks: the first two from `implement`, the third from `fix`. */
  const STEPPED = [
    'diff --git a/src/app/one.ts b/src/app/one.ts',
    '--- a/src/app/one.ts',
    '+++ b/src/app/one.ts',
    '@@ -1,1 +1,1 @@',
    '-a',
    '+b',
    '@@ -10,1 +10,1 @@',
    '-c',
    '+d',
    '@@ -5,1 +5,1 @@',
    '-e',
    '+f',
    '',
  ].join('\n')
  const STEP = ['implement', 'implement', 'fix']
  const stepOf = (path: string, hunk: number): string | null => (path === 'src/app/one.ts' ? STEP[hunk]! : null)

  it('names the step above each step\'s run of hunks', () => {
    render(<DiffView patch={STEPPED} stepOf={stepOf} />)
    const sources = rows('source')
    expect(sources.map((r) => r.getAttribute('data-step'))).toEqual(['implement', 'fix'])
    expect(sources[0]!.textContent).toBe('from step implement')
    // Order: source, hunk, lines, hunk, lines, source, hunk, lines.
    const kinds = rows().map((r) => r.getAttribute('data-diff-row'))
    expect(kinds.filter((k) => k === 'source' || k === 'hunk')).toEqual(['source', 'hunk', 'hunk', 'source', 'hunk'])
  })

  it('offers no unchanged lines between two steps\' hunks, whose line numbers count different files', () => {
    render(<DiffView patch={STEPPED} stepOf={stepOf} getFile={() => 'x\n'} />)
    const kinds = rows().map((r) => r.getAttribute('data-diff-row'))
    const fix = kinds.lastIndexOf('source')
    expect(kinds[fix - 1]).not.toBe('gap')
    // Within one step, the gap between its hunks is still offered.
    expect(kinds.slice(0, fix)).toContain('gap')
  })

  it('draws no source row without stepOf, or for a hunk it names no step for', () => {
    render(<DiffView patch={STEPPED} stepOf={() => null} />)
    expect(rows('source')).toEqual([])
  })

  it('lands j on the step row above a hunk, so the step is on screen with it', () => {
    render(<DiffView patch={STEPPED} stepOf={stepOf} />)
    const s = scroller()
    fireEvent.keyDown(s, { key: 'j' })
    fireEvent.keyDown(s, { key: 'j' })
    // The second j passes the second `implement` hunk; the third lands on `fix`'s row.
    fireEvent.keyDown(s, { key: 'j' })
    expect(s.scrollTop).toBe(offsetOf(rows('source')[1]!))
  })
})

/** A drawn row's offset: the sum of the heights of the rows and spacer above it. */
function offsetOf(row: HTMLElement): number {
  let y = 0
  for (const el of Array.from(row.parentElement!.children) as HTMLElement[]) {
    if (el === row) return y
    y += Number.parseFloat(el.style.height || '0')
  }
  throw new Error('row not found')
}

describe('5 · the phone: one file, a picker, and the All files sheet', () => {
  it('draws exactly one file, with a picker naming it and no list column', () => {
    stubPhone(true)
    render(<DiffView patch={THREE} />)
    expect(drawnPaths()).toEqual(['src/app/one.ts'])
    expect(screen.queryByRole('navigation', { name: 'Files in this diff' })).toBeNull()
    const picker = screen.getByRole('group', { name: 'File' })
    expect(within(picker).getByTestId('diff-picker-pos').textContent).toBe('file 1 of 3')
    expect(picker.textContent).toContain('src/app/one.ts')
    expect(screen.queryByRole('button', { name: 'Split' })).toBeNull()
  })

  it('is the phone at the console\'s 560px query even in a pane wide enough for split', () => {
    stubPhone(true)
    vi.stubGlobal('innerWidth', 900)
    render(<DiffView patch={THREE} />)
    expect(screen.getByRole('group', { name: 'File' })).toBeDefined()
    expect(screen.queryByRole('button', { name: 'Split' })).toBeNull()
  })

  it('moves one file at a time with previous and next', () => {
    stubPhone(true)
    const onFileChange = vi.fn()
    render(<DiffView patch={THREE} onFileChange={onFileChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Next file' }))
    expect(drawnPaths()).toEqual(['tools/run.py'])
    expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 2 of 3')
    expect(onFileChange).toHaveBeenLastCalledWith('tools/run.py')
  })

  it('lists every file with its counts in the All files sheet, and opens the one chosen', () => {
    stubPhone(true)
    render(<DiffView patch={THREE} />)
    const all = screen.getByRole('button', { name: 'All files' })
    expect(all.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(all)
    const sheet = screen.getByRole('dialog', { name: 'All files' })
    expect(within(sheet).getByTestId('diff-sheet-head').textContent).toBe('3 files +4 −3')
    const items = Array.from(sheet.querySelectorAll<HTMLElement>('button.diff-file'))
    expect(items.map((b) => b.getAttribute('data-path'))).toEqual(['src/app/one.ts', 'tools/run.py', 'notes.txt'])
    expect(items.map((b) => b.getAttribute('aria-label'))).toEqual([
      'src/app/one.ts, modified, 2 added, 1 removed, viewed',
      'tools/run.py, modified, 1 added, 1 removed',
      'notes.txt, modified, 1 added, 1 removed',
    ])
    // The whole path, since the sheet has no directory groups.
    expect(items[1]!.querySelector('.diff-fname')!.textContent).toBe('tools/run.py')
    fireEvent.click(items[2]!)
    expect(screen.queryByRole('dialog', { name: 'All files' })).toBeNull()
    expect(drawnPaths()).toEqual(['notes.txt'])
    expect(screen.getByTestId('diff-picker-pos').textContent).toBe('file 3 of 3')
  })

  it('closes the sheet on Escape without moving, and keeps the Escape from the drawer around it', () => {
    stubPhone(true)
    const outer = vi.fn()
    render(
      <div onKeyDown={outer}>
        <DiffView patch={THREE} />
      </div>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'All files' }))
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'All files' }), { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: 'All files' })).toBeNull()
    expect(outer).not.toHaveBeenCalled()
    expect(drawnPaths()).toEqual(['src/app/one.ts'])
  })
})

describe('6 · the syntax highlighter', () => {
  it('colours a known language once the lazily loaded lexer arrives, as spans around text nodes', async () => {
    render(<DiffView patch={THREE} />)
    await waitFor(() => expect(scroller().querySelector('.diff-tk')).not.toBeNull())
    const added = rows('line').find((r) => r.textContent?.includes('"three"'))!
    const text = added.querySelector('.diff-text')!
    expect(text.textContent).toBe('const b = "three"')
    expect(text.querySelector('.diff-tk.is-kw')!.textContent).toBe('const')
    expect(text.querySelector('.diff-tk.is-str')!.textContent).toBe('"three"')
    // Only spans and text nodes: no element the patch could have named.
    for (const el of Array.from(text.querySelectorAll('*'))) expect(el.tagName).toBe('SPAN')
  })

  it('never sets innerHTML on the way, and draws markup in a coloured file as text', async () => {
    const set = vi.spyOn(Element.prototype, 'innerHTML', 'set')
    const evil = '<img src=x onerror="window.__pwned=1">'
    const patch = `diff --git a/x.ts b/x.ts\n--- a/x.ts\n+++ b/x.ts\n@@ -0,0 +1 @@\n+const s = '${evil}'\n`
    render(<DiffView patch={patch} />)
    await waitFor(() => expect(scroller().querySelector('.diff-tk.is-str')).not.toBeNull())
    expect(scroller().querySelector('img, script')).toBeNull()
    expect(rows('line')[0]!.querySelector('.diff-text')!.textContent).toBe(`const s = '${evil}'`)
    expect(set).not.toHaveBeenCalled()
  })

  it('keeps find marks and colour together without changing a character', async () => {
    render(<DiffView patch={THREE} />)
    await waitFor(() => expect(scroller().querySelector('.diff-tk')).not.toBeNull())
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find in diff' }), { target: { value: 'nst b = "th' } })
    const added = rows('line').find((r) => r.textContent?.includes('"three"'))!
    expect(added.querySelector('.diff-text')!.textContent).toBe('const b = "three"')
    const mark = added.querySelector('mark.diff-hit')!
    expect(mark.textContent).toBe('nst b = "th')
    // The match crosses a keyword, plain text and a string: each piece keeps its colour.
    expect(mark.querySelector('.diff-tk.is-kw')!.textContent).toBe('nst')
    expect(mark.querySelector('.diff-tk.is-str')!.textContent).toBe('"th')
  })

  it('leaves an unknown language as plain text', async () => {
    await loadHighlighter()
    render(<DiffView patch={THREE} initialFile="notes.txt" />)
    // Loaded already, so a known language would be coloured on this render.
    expect(drawnPaths()).toEqual(['notes.txt'])
    expect(scroller().querySelector('.diff-tk')).toBeNull()
    expect(rows('line')[0]!.querySelector('.diff-text')!.childNodes).toHaveLength(1)
  })

  it('leaves a file over the size cap as plain text, even expanded', async () => {
    await loadHighlighter()
    const n = 500
    const big = [
      'diff --git a/big.ts b/big.ts',
      '--- a/big.ts',
      '+++ b/big.ts',
      `@@ -0,0 +1,${n} @@`,
      ...Array.from({ length: n }, (_, i) => `+const v${i} = ${i}`),
      '',
    ].join('\n')
    render(<DiffView patch={big} />)
    fireEvent.click(rows('file')[0]!)
    expect(rows('line').length).toBeGreaterThan(10)
    expect(scroller().querySelector('.diff-tk')).toBeNull()
  })

  it('colours the first file once the lexer has loaded, with no other change to the rows', async () => {
    await act(async () => {
      await loadHighlighter()
    })
    render(<DiffView patch={THREE} />)
    expect(scroller().querySelector('.diff-tk')).not.toBeNull()
    expect(rows('line').map((r) => r.querySelector('.diff-text')!.textContent)).toEqual([
      'const a = 1',
      'const b = "two"',
      'const b = "three"',
      'export { a }',
      '// tail',
      'return 42',
      '}',
    ])
  })
})
