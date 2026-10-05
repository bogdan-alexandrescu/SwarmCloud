// THE DIFF VIEWER, AS RENDERED (#310 step 1b-1d; a file list and one file
// since the owner decision of 2026-10-01).
//
// Every claim the viewer makes is asserted on the DOM it produces: which rows
// exist, what their gutters say, what a control's accessible name is, where
// the scroller was sent. jsdom has no layout, so the virtual window is sized
// from the viewer's own fallback viewport, and "scrolled to" is read off the
// scroller's `scrollTop`, which jsdom stores as written.

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DiffView } from '../../diff/DiffView'
import { VIEW_KEY } from '../../diff/storage'
import { REAL_GIT_DIFF } from './fixture'

/** `files` files of `lines` changed lines each: a context line, then alternating -/+. */
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

function rows(kind?: string): HTMLElement[] {
  const sel = kind ? `[data-diff-row="${kind}"]` : '[data-diff-row]'
  return Array.from(scroller().querySelectorAll<HTMLElement>(sel))
}

function fileRow(path: string): HTMLElement | undefined {
  return rows('file').find((r) => r.getAttribute('data-path') === path)
}

/** Open a file from the list, by its path. */
function openFile(path: string): void {
  const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
  const row = Array.from(nav.querySelectorAll<HTMLElement>('button.diff-file')).find(
    (b) => b.getAttribute('data-path') === path,
  )
  if (!row) throw new Error(`no list row for ${path}`)
  fireEvent.click(row)
}

function linesOf(path: string): HTMLElement[] {
  return rows().filter((r) => r.getAttribute('data-path') === path && r.getAttribute('data-diff-row') !== 'file')
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

describe('the file list', () => {
  it('lists every file with its added and removed counts', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
    const links = Array.from(nav.querySelectorAll<HTMLElement>('button.diff-file'))
    expect(links).toHaveLength(10)
    const src = links.find((b) => b.getAttribute('data-path') === 'src.txt')!
    expect(src.textContent).toContain('+2')
    expect(src.textContent).toContain('−2')
    const del = links.find((b) => b.getAttribute('data-path') === 'deleted.txt')!
    expect(del.textContent).toContain('+0')
    expect(del.textContent).toContain('−2')
  })

  it('draws only the open file, and a click on a row opens another', () => {
    render(<DiffView patch={bigPatch(5, 2000)} />)
    expect(fileRow('big0.txt')).toBeDefined()
    expect(fileRow('big3.txt')).toBeUndefined()
    openFile('big3.txt')
    expect(fileRow('big3.txt')).toBeDefined()
    expect(fileRow('big0.txt')).toBeUndefined()
    expect(rows().every((r) => r.getAttribute('data-path') === 'big3.txt')).toBe(true)
  })

  it('letters each row with its change kind', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    const letter = (path: string): string =>
      Array.from(document.querySelectorAll('button.diff-file'))
        .find((b) => b.getAttribute('data-path') === path)
        ?.querySelector('.diff-kind')?.textContent ?? ''
    expect(letter('added file.txt')).toBe('A')
    expect(letter('deleted.txt')).toBe('D')
    expect(letter('new name.txt')).toBe('R')
    expect(letter('copied.txt')).toBe('C')
    expect(letter('src.txt')).toBe('M')
    expect(letter('run.sh')).toBe('M')
  })
})

describe('the unified view', () => {
  it('draws hunk headers and both line-number gutters', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    openFile('src.txt')
    const hunks = rows('hunk').filter((r) => r.getAttribute('data-path') === 'src.txt')
    expect(hunks.map((h) => h.textContent)).toEqual(['@@ -1,5 +1,5 @@', '@@ -15,6 +15,6 @@ line14'])
    const lines = linesOf('src.txt').filter((r) => r.getAttribute('data-diff-row') === 'line')
    const cells = (r: HTMLElement): string[] =>
      Array.from(r.querySelectorAll('.diff-no, .diff-sign, .diff-text')).map((c) => c.textContent ?? '')
    expect(cells(lines[0]!)).toEqual(['1', '1', ' ', 'line1'])
    expect(cells(lines[1]!)).toEqual(['2', '', '-', 'line2'])
    expect(cells(lines[2]!)).toEqual(['', '2', '+', 'LINE TWO'])
    expect(lines[1]!.classList.contains('is-del')).toBe(true)
    expect(lines[2]!.classList.contains('is-add')).toBe(true)
  })

  it('marks a missing final newline and a content carriage return visibly', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    openFile('nonl.txt')
    expect(linesOf('nonl.txt').filter((r) => r.querySelector('.diff-nonl'))).toHaveLength(2)
    openFile('crlf.txt')
    const cr = linesOf('crlf.txt').filter((r) => r.querySelector('.diff-cr'))
    expect(cr).toHaveLength(3)
  })
})

describe('the split view', () => {
  it('pairs removed and added lines side by side, and remembers the choice', () => {
    const { unmount } = render(<DiffView patch={REAL_GIT_DIFF} />)
    openFile('src.txt')
    const split = screen.getByRole('button', { name: 'Split' })
    expect(split.getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(split)
    expect(split.getAttribute('aria-pressed')).toBe('true')
    expect(store.get(VIEW_KEY)).toBe('split')
    expect(rows('line')).toEqual([])
    const pairs = rows('pair').filter((r) => r.getAttribute('data-path') === 'src.txt')
    const halves = (r: HTMLElement): string[] =>
      Array.from(r.querySelectorAll('.diff-half')).map((h) =>
        Array.from(h.querySelectorAll('.diff-no, .diff-text'))
          .map((c) => c.textContent)
          .join('|'),
      )
    expect(halves(pairs[1]!)).toEqual(['2|line2', '2|LINE TWO'])
    unmount()
    render(<DiffView patch={REAL_GIT_DIFF} />)
    expect(screen.getByRole('button', { name: 'Split' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('still works when localStorage throws on every call', () => {
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('denied')
      },
      setItem: () => {
        throw new Error('denied')
      },
    })
    render(<DiffView patch={REAL_GIT_DIFF} />)
    openFile('src.txt')
    fireEvent.click(screen.getByRole('button', { name: 'Split' }))
    expect(rows('pair').length).toBeGreaterThan(0)
  })

  it('is always unified below 700px, whatever was remembered, and offers no split', () => {
    store.set(VIEW_KEY, 'split')
    vi.stubGlobal('innerWidth', 600)
    render(<DiffView patch={REAL_GIT_DIFF} />)
    expect(rows('pair')).toEqual([])
    expect(rows('line').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Split' })).toBeNull()
  })

  it('returns to split when the window grows past 700px', () => {
    store.set(VIEW_KEY, 'split')
    vi.stubGlobal('innerWidth', 600)
    render(<DiffView patch={REAL_GIT_DIFF} />)
    expect(rows('pair')).toEqual([])
    vi.stubGlobal('innerWidth', 1200)
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })
    expect(rows('pair').length).toBeGreaterThan(0)
  })
})

describe('context between hunks', () => {
  it('says context is not available, and invents no line, without getFile', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    openFile('src.txt')
    const gaps = rows('gap').filter((r) => r.getAttribute('data-path') === 'src.txt')
    expect(gaps).toHaveLength(1)
    expect(gaps[0]!.textContent).toContain('context not available')
    expect(gaps[0]!.querySelector('button')).toBeNull()
    expect(rows('context')).toEqual([])
    expect(scroller().textContent).not.toContain('line6')
  })

  it('expands the hidden lines from getFile, numbered on both sides', async () => {
    const body = Array.from({ length: 20 }, (_, i) => `line${i + 1}`)
    body[1] = 'LINE TWO'
    body[17] = 'LINE EIGHTEEN'
    const getFile = vi.fn(async () => body.join('\n') + '\n')
    render(<DiffView patch={REAL_GIT_DIFF} getFile={getFile} />)
    openFile('src.txt')
    const button = screen.getByRole('button', { name: 'Expand 9 hidden lines in src.txt' })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(getFile).toHaveBeenCalledWith('src.txt', 'new')
    const ctx = rows('context').filter((r) => r.getAttribute('data-path') === 'src.txt')
    expect(ctx.map((r) => r.querySelector('.diff-text')?.textContent)).toEqual(
      ['line6', 'line7', 'line8', 'line9', 'line10', 'line11', 'line12', 'line13', 'line14'],
    )
    const nos = Array.from(ctx[0]!.querySelectorAll('.diff-no')).map((c) => c.textContent)
    expect(nos).toEqual(['6', '6'])
  })

  it('refuses a file that does not match the patch rather than showing its lines', async () => {
    const getFile = vi.fn(async () => Array.from({ length: 20 }, (_, i) => `other${i}`).join('\n'))
    render(<DiffView patch={REAL_GIT_DIFF} getFile={getFile} />)
    openFile('src.txt')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Expand 9 hidden lines in src.txt' }))
    })
    expect(rows('context')).toEqual([])
    const gap = rows('gap').find((r) => r.getAttribute('data-path') === 'src.txt')!
    expect(gap.textContent).toContain('does not match this patch')
  })

  it('says the context was not read when getFile fails', async () => {
    const getFile = vi.fn(async () => {
      throw new Error('404')
    })
    render(<DiffView patch={REAL_GIT_DIFF} getFile={getFile} />)
    openFile('src.txt')
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Expand 9 hidden lines in src.txt' }))
    })
    expect(rows('context')).toEqual([])
    const gap = rows('gap').find((r) => r.getAttribute('data-path') === 'src.txt')!
    expect(gap.textContent).toContain('context not read')
  })
})

describe('badges', () => {
  it('names binary, renamed, copied, mode-change, added and deleted files', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    const badges = (path: string): string[] => {
      openFile(path)
      return Array.from(fileRow(path)!.querySelectorAll('.diff-badge')).map((b) => b.textContent ?? '')
    }
    expect(badges('blob.bin')).toEqual(['binary'])
    const binaryBody = linesOf('blob.bin')
    expect(binaryBody).toHaveLength(1)
    expect(binaryBody[0]!.textContent).toBe('binary file not shown')
    expect(badges('new name.txt')).toEqual(['renamed 87%'])
    expect(fileRow('new name.txt')!.textContent).toContain('old name.txt')
    expect(badges('copied.txt')).toEqual(['copied 90%'])
    expect(badges('run.sh')).toEqual(['mode 100644 → 100755'])
    expect(badges('deleted.txt')).toEqual(['deleted'])
    expect(badges('added file.txt')).toEqual(['added'])
    expect(badges('src.txt')).toEqual([])
  })
})

describe('the keyboard', () => {
  it('moves by file with n and p, and by hunk with j and k, consuming each key', () => {
    render(<DiffView patch={bigPatch(4, 400)} />)
    const s = scroller()
    const open = (): string | null => s.getAttribute('data-open-file')
    expect(open()).toBe('big0.txt')
    // `fireEvent` returns false when the handler called preventDefault.
    expect(fireEvent.keyDown(s, { key: 'n' })).toBe(false)
    expect(open()).toBe('big1.txt')
    fireEvent.keyDown(s, { key: 'n' })
    expect(open()).toBe('big2.txt')
    expect(fireEvent.keyDown(s, { key: 'p' })).toBe(false)
    expect(open()).toBe('big1.txt')
    expect(s.scrollTop).toBe(0)
  })

  it('moves between the open file\'s hunks with j and k', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    openFile('src.txt')
    const s = scroller()
    expect(fireEvent.keyDown(s, { key: 'j' })).toBe(false)
    const h1 = s.scrollTop
    expect(h1).toBeGreaterThan(0)
    fireEvent.keyDown(s, { key: 'j' })
    const h2 = s.scrollTop
    expect(h2).toBeGreaterThan(h1)
    expect(fireEvent.keyDown(s, { key: 'k' })).toBe(false)
    expect(s.scrollTop).toBe(h1)
  })

  it('focuses find in the diff with /', () => {
    render(<DiffView patch={REAL_GIT_DIFF} />)
    expect(fireEvent.keyDown(scroller(), { key: '/' })).toBe(false)
    expect(document.activeElement).toBe(screen.getByRole('searchbox', { name: 'Find in diff' }))
  })

  it('ignores its keys while an input has focus', () => {
    render(<DiffView patch={bigPatch(4, 400)} />)
    const box = screen.getByRole('searchbox', { name: 'Filter files by path' })
    box.focus()
    fireEvent.keyDown(box, { key: 'n' })
    fireEvent.keyDown(box, { key: 'j' })
    expect(scroller().getAttribute('data-open-file')).toBe('big0.txt')
    expect(scroller().scrollTop).toBe(0)
  })

  it('ignores its keys with a modifier held', () => {
    render(<DiffView patch={bigPatch(4, 400)} />)
    fireEvent.keyDown(scroller(), { key: 'n', ctrlKey: true })
    fireEvent.keyDown(scroller(), { key: 'n', metaKey: true })
    expect(scroller().getAttribute('data-open-file')).toBe('big0.txt')
  })

  it('gives every control an accessible name and a keyboard route', () => {
    render(<DiffView patch={REAL_GIT_DIFF} getFile={async () => ''} />)
    const controls = Array.from(document.querySelectorAll<HTMLElement>('button, input, [tabindex]'))
    expect(controls.length).toBeGreaterThan(15)
    for (const c of controls) {
      const name = (c.getAttribute('aria-label') ?? c.textContent ?? '').trim()
      expect(name, c.outerHTML.slice(0, 120)).not.toBe('')
      expect(c.tabIndex, c.outerHTML.slice(0, 120)).toBeGreaterThanOrEqual(0)
    }
  })
})

describe('big diffs', () => {
  it('renders a 50,000-line patch as a window of rows, not 50,000 of them', () => {
    const patch = bigPatch(1, 52_000)
    expect(patch.split('\n').length).toBeGreaterThan(50_000)
    render(<DiffView patch={patch} />)
    // A file this size opens collapsed (owner decision 2026-10-05); expanded, it still windows.
    expect(rows()).toHaveLength(1)
    fireEvent.click(rows('file')[0]!)
    const n = rows().length
    expect(n).toBeGreaterThan(10)
    expect(n).toBeLessThan(300)
    // The spacers carry the height of what is not drawn.
    expect(Number(scroller().getAttribute('data-total-rows'))).toBeGreaterThan(50_000)
  })

  it('draws the rows under the scroll position, and not the ones above it', () => {
    render(<DiffView patch={bigPatch(1, 50_000)} />)
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }))
    const s = scroller()
    fireEvent.scroll(s, { target: { scrollTop: 22 * 30_000 } })
    const texts = rows('line').map((r) => r.querySelector('.diff-text')?.textContent ?? '')
    expect(texts.some((t) => t.startsWith('new 0:'))).toBe(true)
    expect(texts).not.toContain('old 0:0')
    expect(rows().length).toBeLessThan(300)
  })
})

describe('text is shown exactly as given', () => {
  it('renders markup in a patch as text, never as HTML', () => {
    const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>'
    render(<DiffView patch={`diff --git a/x.html b/x.html\n--- a/x.html\n+++ b/x.html\n@@ -0,0 +1 @@\n+${evil}\n`} />)
    expect(scroller().querySelector('img, script')).toBeNull()
    expect(rows('line')[0]!.querySelector('.diff-text')!.textContent).toBe(evil)
  })

  it('reports a malformed patch as an alert and draws no rows', () => {
    render(<DiffView patch={'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,3 +1,3 @@\n a\n'} />)
    const alert = screen.getByRole('alert')
    expect(alert.textContent).toContain('patch not read')
    expect(alert.textContent).toContain('line 4')
    expect(document.querySelectorAll('[data-diff-row]')).toHaveLength(0)
  })

  it('says a patch with no files has none', () => {
    render(<DiffView patch="" />)
    expect(screen.getByText('no files in this patch')).toBeDefined()
  })
})
