// PER-FILE COLLAPSE AND EXPAND (#310, owner decision 2026-10-05): the open
// file's header is a button that toggles its body, with aria-expanded; a file
// of more than LARGE_FILE_LINES changed lines starts collapsed and says why;
// Collapse all / Expand all at the top; find, j and k expand a collapsed file
// when they reach a match or a hunk inside it; and a 50,000-line file, once
// expanded, is still drawn as a window of rows.
//
// MUTATION: start every file open, drop aria-expanded, leave a collapsed file
// collapsed when find or j lands in it, or build every row of an expanded big
// file -- each goes red below.

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DiffView } from '../../diff/DiffView'
import { parseUnifiedDiff } from '../../diff/parse'
import { buildRows, LARGE_FILE_LINES, startsCollapsed } from '../../diff/rows'

/** One file of `lines` changed lines: the first half removed, the second added. */
function filePatch(path: string, lines: number, marker = ''): string[] {
  const out = [`diff --git a/${path} b/${path}`, `--- a/${path}`, `+++ b/${path}`, `@@ -1,${lines / 2} +1,${lines / 2} @@`]
  for (let i = 0; i < lines / 2; i++) out.push(`-old ${path}:${i}`)
  for (let i = 0; i < lines / 2; i++) out.push(`+new ${path}:${i}${i === lines / 2 - 1 ? marker : ''}`)
  return out
}

/** small.txt (10 changed lines) then large.txt (1,000, its last line holding `needle`). */
const MIXED = [...filePatch('small.txt', 10), ...filePatch('large.txt', 1000, ' needle')].join('\n') + '\n'

function scroller(): HTMLElement {
  return screen.getByRole('region', { name: 'Diff lines' })
}

function rows(kind?: string): HTMLElement[] {
  const sel = kind ? `[data-diff-row="${kind}"]` : '[data-diff-row]'
  return Array.from(scroller().querySelectorAll<HTMLElement>(sel))
}

function header(): HTMLElement {
  const [h] = rows('file')
  if (!h) throw new Error('no file header drawn')
  return h
}

function body(): HTMLElement[] {
  return rows().filter((r) => r.getAttribute('data-diff-row') !== 'file')
}

function openFile(path: string): void {
  const nav = screen.getByRole('navigation', { name: 'Files in this diff' })
  const row = Array.from(nav.querySelectorAll<HTMLElement>('button.diff-file')).find(
    (b) => b.getAttribute('data-path') === path,
  )
  if (!row) throw new Error(`no list row for ${path}`)
  fireEvent.click(row)
}

beforeEach(() => {
  const store = new Map<string, string>()
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

describe('which files start collapsed', () => {
  it('collapses a file of more than LARGE_FILE_LINES changed lines, and no smaller one', () => {
    const [atLimit, over] = (
      parseUnifiedDiff([...filePatch('a.txt', LARGE_FILE_LINES), ...filePatch('b.txt', LARGE_FILE_LINES + 2)].join('\n') + '\n') as {
        ok: true
        files: Parameters<typeof startsCollapsed>[0][]
      }
    ).files
    expect(startsCollapsed(atLimit!)).toBe(false)
    expect(startsCollapsed(over!)).toBe(true)
  })

  it('builds only the header row for a collapsed file', () => {
    const parsed = parseUnifiedDiff(MIXED)
    if (!parsed.ok) throw new Error('fixture did not parse')
    const input = { files: parsed.files, file: 1, mode: 'unified' as const, canExpand: false, context: new Map(), opened: new Set<string>() }
    expect(buildRows({ ...input, collapsed: true })).toEqual([{ t: 'file', file: 1 }])
    expect(buildRows({ ...input, collapsed: false }).length).toBeGreaterThan(1000)
  })

  it('opens a small file expanded', () => {
    render(<DiffView patch={MIXED} />)
    expect(header().getAttribute('data-path')).toBe('small.txt')
    expect(header().tagName).toBe('BUTTON')
    expect(header().getAttribute('aria-expanded')).toBe('true')
    expect(header().textContent).not.toContain('collapsed')
    expect(rows('line')).toHaveLength(10)
  })

  it('opens a large file collapsed, and says why', () => {
    render(<DiffView patch={MIXED} />)
    openFile('large.txt')
    expect(header().getAttribute('data-path')).toBe('large.txt')
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(header().textContent).toContain('collapsed: large')
    expect(body()).toEqual([])
    expect(Number(scroller().getAttribute('data-total-rows'))).toBe(1)
  })
})

describe('the header toggles its file', () => {
  it('expands and collapses on a click', () => {
    render(<DiffView patch={MIXED} />)
    openFile('large.txt')
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('true')
    expect(header().textContent).not.toContain('collapsed')
    expect(rows('line').length).toBeGreaterThan(10)
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(body()).toEqual([])
    openFile('small.txt')
    fireEvent.click(header())
    expect(header().getAttribute('aria-expanded')).toBe('false')
    // Collapsed by the reader, not for size: it says collapsed, not why.
    expect(header().textContent).toContain('collapsed')
    expect(header().textContent).not.toContain('collapsed: large')
    expect(body()).toEqual([])
  })

  it('remembers each file\'s state while another is open', () => {
    render(<DiffView patch={MIXED} />)
    openFile('large.txt')
    fireEvent.click(header())
    openFile('small.txt')
    openFile('large.txt')
    expect(header().getAttribute('aria-expanded')).toBe('true')
  })
})

describe('Collapse all and Expand all', () => {
  it('collapses and expands every file', () => {
    render(<DiffView patch={MIXED} />)
    const collapseAll = screen.getByRole('button', { name: 'Collapse all' })
    const expandAll = screen.getByRole('button', { name: 'Expand all' })
    fireEvent.click(collapseAll)
    expect(header().getAttribute('aria-expanded')).toBe('false')
    expect(body()).toEqual([])
    expect((collapseAll as HTMLButtonElement).disabled).toBe(true)
    openFile('large.txt')
    expect(header().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(expandAll)
    expect(header().getAttribute('aria-expanded')).toBe('true')
    expect((expandAll as HTMLButtonElement).disabled).toBe(true)
    openFile('small.txt')
    expect(header().getAttribute('aria-expanded')).toBe('true')
    expect(rows('line')).toHaveLength(10)
  })

  it('is offered at phone width too', () => {
    vi.stubGlobal('innerWidth', 600)
    render(<DiffView patch={MIXED} />)
    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }))
    expect(header().getAttribute('aria-expanded')).toBe('false')
  })
})

describe('reaching into a collapsed file', () => {
  it('find expands the collapsed file its match is in, and draws the match', () => {
    render(<DiffView patch={MIXED} />)
    fireEvent.change(screen.getByRole('searchbox', { name: 'Find in diff' }), { target: { value: 'needle' } })
    expect(screen.getByTestId('diff-find-count').textContent).toBe('1 of 1')
    expect(scroller().getAttribute('data-open-file')).toBe('large.txt')
    expect(Number(scroller().getAttribute('data-total-rows'))).toBeGreaterThan(1000)
    // The match is the file's last line, so the window moved down to it...
    expect(scroller().querySelector('mark.diff-hit.is-current')?.textContent).toBe('needle')
    // ...and back at the top, the header says the file is open.
    fireEvent.scroll(scroller(), { target: { scrollTop: 0 } })
    expect(header().getAttribute('aria-expanded')).toBe('true')
  })

  it('j expands a collapsed open file and moves to its first hunk', () => {
    render(<DiffView patch={MIXED} />)
    openFile('large.txt')
    expect(fireEvent.keyDown(scroller(), { key: 'j' })).toBe(false)
    expect(header().getAttribute('aria-expanded')).toBe('true')
    expect(scroller().scrollTop).toBeGreaterThan(0)
    expect(rows('hunk')).toHaveLength(1)
  })

  it('k expands a collapsed open file too', () => {
    render(<DiffView patch={MIXED} />)
    openFile('large.txt')
    expect(fireEvent.keyDown(scroller(), { key: 'k' })).toBe(false)
    expect(header().getAttribute('aria-expanded')).toBe('true')
  })

  it('n and p still move between files, collapsed or not', () => {
    render(<DiffView patch={MIXED} />)
    fireEvent.keyDown(scroller(), { key: 'n' })
    expect(scroller().getAttribute('data-open-file')).toBe('large.txt')
    fireEvent.keyDown(scroller(), { key: 'p' })
    expect(scroller().getAttribute('data-open-file')).toBe('small.txt')
  })
})

describe('a big file expanded', () => {
  it('is still a window of rows, not 50,000 of them', () => {
    render(<DiffView patch={filePatch('huge.txt', 52_000).join('\n') + '\n'} />)
    expect(header().getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }))
    expect(Number(scroller().getAttribute('data-total-rows'))).toBeGreaterThan(50_000)
    const n = rows().length
    expect(n).toBeGreaterThan(10)
    expect(n).toBeLessThan(300)
  })
})
