// THE PARSER, CASE BY CASE (#310 step 1a).
//
// `parseUnifiedDiff` is the only thing between a string somebody's agent wrote
// and the screen, so each case it claims to read is asserted here on the
// numbers it produces -- paths, statuses, line numbers -- and every malformed
// shape is asserted to come back as a typed error rather than a throw. The
// multi-file case is a REAL `git diff --binary` (see fixture.ts), because a
// hand-written patch only proves the parser agrees with whoever wrote it.

import { describe, expect, it } from 'vitest'

import { parseUnifiedDiff, type DiffFile, type ParseResult } from '../../diff/parse'
import { REAL_GIT_DIFF } from './fixture'

function files(r: ParseResult): DiffFile[] {
  if (!r.ok) throw new Error(`expected a parse, got ${r.error.kind} at line ${r.error.line}: ${r.error.message}`)
  return r.files
}

function one(text: string): DiffFile {
  const fs = files(parseUnifiedDiff(text))
  expect(fs).toHaveLength(1)
  return fs[0]!
}

describe('parseUnifiedDiff: a real multi-file git diff', () => {
  const fs = files(parseUnifiedDiff(REAL_GIT_DIFF))
  const byPath = (p: string): DiffFile => {
    const f = fs.find((x) => x.path === p)
    if (!f) throw new Error(`no file ${p}; parsed ${fs.map((x) => x.path).join(', ')}`)
    return f
  }

  it('reads every file, in order', () => {
    expect(fs.map((f) => f.path)).toEqual([
      'added file.txt',
      'blob.bin',
      'copied.txt',
      'crlf.txt',
      'deleted.txt',
      'new name.txt',
      'nonl.txt',
      'run.sh',
      'src.txt',
      'tab\there.txt',
    ])
  })

  it('reads an added file whose path has a space, dropping the tab git appends', () => {
    const f = byPath('added file.txt')
    expect(f.status).toBe('added')
    expect(f.oldPath).toBeNull()
    expect(f.newPath).toBe('added file.txt')
    expect(f.newMode).toBe('100644')
    expect([f.additions, f.deletions]).toEqual([2, 0])
    expect(f.hunks[0]!.lines.map((l) => [l.kind, l.oldNo, l.newNo, l.text])).toEqual([
      ['add', null, 1, 'brand'],
      ['add', null, 2, 'new'],
    ])
  })

  it('marks a GIT binary patch as binary and parses none of it as text', () => {
    const f = byPath('blob.bin')
    expect(f.binary).toBe(true)
    expect(f.hunks).toEqual([])
    expect([f.additions, f.deletions]).toEqual([0, 0])
  })

  it('reads a copy with its similarity and both paths', () => {
    const f = byPath('copied.txt')
    expect(f.status).toBe('copied')
    expect(f.similarity).toBe(90)
    expect(f.oldPath).toBe('copysrc.txt')
    expect(f.newPath).toBe('copied.txt')
    expect(f.hunks[0]!.section).toBe('gamma')
    expect([f.additions, f.deletions]).toEqual([1, 0])
    expect(f.hunks[0]!.lines.at(-1)).toMatchObject({ kind: 'add', oldNo: null, newNo: 7, text: 'eta' })
  })

  it('keeps a carriage return that belongs to the content of an LF patch', () => {
    const f = byPath('crlf.txt')
    expect(f.hunks[0]!.lines.map((l) => l.text)).toEqual(['crlf one\r', 'crlf two\r', 'crlf TWO\r'])
  })

  it('reads a deleted file', () => {
    const f = byPath('deleted.txt')
    expect(f.status).toBe('deleted')
    expect(f.newPath).toBeNull()
    expect(f.oldPath).toBe('deleted.txt')
    expect([f.additions, f.deletions]).toEqual([0, 2])
    expect(f.hunks[0]!.lines.map((l) => [l.oldNo, l.newNo])).toEqual([[1, null], [2, null]])
  })

  it('reads a rename with its similarity, and numbers both sides from the hunk header', () => {
    const f = byPath('new name.txt')
    expect(f.status).toBe('renamed')
    expect(f.similarity).toBe(87)
    expect(f.oldPath).toBe('old name.txt')
    const h = f.hunks[0]!
    expect([h.oldStart, h.oldLines, h.newStart, h.newLines, h.section]).toEqual([2, 7, 2, 7, 'one'])
    expect(h.lines.map((l) => [l.kind, l.oldNo, l.newNo])).toEqual([
      ['context', 2, 2],
      ['context', 3, 3],
      ['context', 4, 4],
      ['del', 5, null],
      ['add', null, 5],
      ['context', 6, 6],
      ['context', 7, 7],
      ['context', 8, 8],
    ])
  })

  it('attaches "No newline at end of file" to the line it follows, on each side', () => {
    const f = byPath('nonl.txt')
    expect(f.hunks[0]!.lines.map((l) => [l.kind, l.text, l.noNewlineAtEnd])).toEqual([
      ['del', 'no newline', true],
      ['add', 'no newline', false],
      ['add', 'now with more', true],
    ])
  })

  it('reads a mode change that carries no hunks', () => {
    const f = byPath('run.sh')
    expect(f.status).toBe('modified')
    expect([f.oldMode, f.newMode]).toEqual(['100644', '100755'])
    expect(f.hunks).toEqual([])
  })

  it('reads two hunks in one file with their own numbering', () => {
    const f = byPath('src.txt')
    expect(f.hunks.map((h) => [h.oldStart, h.newStart, h.lines.length])).toEqual([
      [1, 1, 6],
      [15, 15, 7],
    ])
    expect(f.hunks[1]!.lines[3]).toMatchObject({ kind: 'del', oldNo: 18, text: 'line18' })
    expect([f.additions, f.deletions]).toEqual([2, 2])
  })

  it('decodes a C-quoted path', () => {
    const f = byPath('tab\there.txt')
    expect(f.oldPath).toBe('tab\there.txt')
    expect(f.hunks[0]!.lines.map((l) => [l.kind, l.text, l.noNewlineAtEnd])).toEqual([
      ['del', 'tab', false],
      ['add', 'x', true],
    ])
  })
})

describe('parseUnifiedDiff: the cases a real fixture above does not reach', () => {
  it('reads "Binary files ... differ" as binary', () => {
    const f = one(
      'diff --git a/img.png b/img.png\n' +
        'index 1111111..2222222 100644\n' +
        'Binary files a/img.png and b/img.png differ\n',
    )
    expect(f.binary).toBe(true)
    expect(f.hunks).toEqual([])
  })

  it('reads a binary file that was added, from /dev/null', () => {
    const f = one(
      'diff --git a/new.bin b/new.bin\n' +
        'new file mode 100644\n' +
        'index 0000000..2222222\n' +
        'Binary files /dev/null and b/new.bin differ\n',
    )
    expect([f.binary, f.status, f.oldPath, f.newPath]).toEqual([true, 'added', null, 'new.bin'])
  })

  it('does not read a GIT binary patch body as diff lines, even when it looks like one', () => {
    // A base85 line can start with `-` or `+`; it must never become a hunk line.
    const fs = files(
      parseUnifiedDiff(
        'diff --git a/a.bin b/a.bin\n' +
          'index 1..2 100644\n' +
          'GIT binary patch\n' +
          'delta 5\n' +
          '-cmZ?\n' +
          '\n' +
          'delta 5\n' +
          '+cmZ!\n' +
          '\n' +
          'diff --git a/b.txt b/b.txt\n' +
          '--- a/b.txt\n' +
          '+++ b/b.txt\n' +
          '@@ -1 +1 @@\n' +
          '-x\n' +
          '+y\n',
      ),
    )
    expect(fs.map((f) => [f.path, f.binary, f.hunks.length])).toEqual([
      ['a.bin', true, 0],
      ['b.txt', false, 1],
    ])
  })

  it('reads a rename with no content change, which has no --- or +++ line', () => {
    const f = one(
      'diff --git a/dir one/a b.txt b/dir two/a b.txt\n' +
        'similarity index 100%\n' +
        'rename from dir one/a b.txt\n' +
        'rename to dir two/a b.txt\n',
    )
    expect([f.status, f.similarity, f.oldPath, f.newPath, f.hunks.length]).toEqual([
      'renamed',
      100,
      'dir one/a b.txt',
      'dir two/a b.txt',
      0,
    ])
  })

  it('reads a path with spaces from the diff --git line when nothing else names it', () => {
    const f = one('diff --git a/my file.sh b/my file.sh\nold mode 100644\nnew mode 100755\n')
    expect(f.path).toBe('my file.sh')
    expect([f.oldMode, f.newMode]).toEqual(['100644', '100755'])
  })

  it('decodes octal UTF-8 escapes in a quoted path', () => {
    // git writes a non-ASCII byte as a three-digit octal escape: é is \303\251.
    const f = one(
      'diff --git "a/caf\\303\\251 \\"q\\".txt" "b/caf\\303\\251 \\"q\\".txt"\n' +
        '--- "a/caf\\303\\251 \\"q\\".txt"\n' +
        '+++ "b/caf\\303\\251 \\"q\\".txt"\n' +
        '@@ -1 +1 @@\n' +
        '-a\n' +
        '+b\n',
    )
    expect(f.path).toBe('café "q".txt')
  })

  it('reads a patch whose every line ends in CRLF as if it were LF', () => {
    const f = one(
      ['diff --git a/w.txt b/w.txt', '--- a/w.txt', '+++ b/w.txt', '@@ -1,2 +1,2 @@', ' keep', '-old', '+new', ''].join(
        '\r\n',
      ),
    )
    expect(f.path).toBe('w.txt')
    expect(f.hunks[0]!.lines.map((l) => [l.kind, l.text])).toEqual([
      ['context', 'keep'],
      ['del', 'old'],
      ['add', 'new'],
    ])
  })

  it('reads a context line whose leading space was stripped by an editor', () => {
    const f = one('diff --git a/e b/e\n--- a/e\n+++ b/e\n@@ -1,3 +1,3 @@\n a\n\n-b\n+c\n')
    expect(f.hunks[0]!.lines.map((l) => [l.kind, l.text, l.oldNo, l.newNo])).toEqual([
      ['context', 'a', 1, 1],
      ['context', '', 2, 2],
      ['del', 'b', 3, null],
      ['add', 'c', null, 3],
    ])
  })

  it('reads a plain unified diff with no git header', () => {
    const f = one('--- a/p.txt\t2026-09-29 10:00:00\n+++ b/p.txt\t2026-09-29 10:01:00\n@@ -1 +1 @@\n-a\n+b\n')
    expect([f.path, f.status, f.additions, f.deletions]).toEqual(['p.txt', 'modified', 1, 1])
  })

  it('ignores a preamble before the first file, and a format-patch signature after the last', () => {
    const f = one(
      'From abc Mon Sep 17 00:00:00 2001\nSubject: [PATCH] x\n\n---\n a | 2 +-\n\n' +
        'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n-- \n2.39.5\n\n',
    )
    expect([f.path, f.additions, f.deletions]).toEqual(['a', 1, 1])
  })

  it('returns no files, and no error, for an empty patch', () => {
    expect(parseUnifiedDiff('')).toEqual({ ok: true, files: [] })
    expect(parseUnifiedDiff('\n\n')).toEqual({ ok: true, files: [] })
  })
})

describe('parseUnifiedDiff: malformed input is a typed error, never a throw', () => {
  const cases: ReadonlyArray<readonly [string, unknown, string, number]> = [
    ['text with no file header', 'hello\nworld\n', 'no-file-header', 1],
    ['a hunk header that does not parse', 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -x +1 @@\n', 'bad-hunk-header', 4],
    ['a hunk that stops before its counts', 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,3 +1,3 @@\n a\n', 'truncated-hunk', 4],
    [
      'a hunk cut off by the next file',
      'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@\n a\ndiff --git a/b b/b\n',
      'truncated-hunk',
      4,
    ],
    ['a line in a hunk with no valid prefix', 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@\n a\n*b\n', 'unexpected-line', 6],
    ['a hunk before any file header', '@@ -1 +1 @@\n-a\n+b\n', 'no-file-header', 1],
    ['a +++ with no ---', 'diff --git a/a b/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\n', 'bad-file-header', 2],
    ['an unterminated quoted path', 'diff --git "a/x b/x\n', 'bad-path', 1],
    ['garbage after a finished hunk', 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1 +1 @@\n-a\n+b\nwhat\n', 'unexpected-line', 7],
    ['a diff --git line with no paths', 'diff --git\n', 'bad-file-header', 1],
    ['a value that is not a string', 42, 'not-text', 0],
    ['null', null, 'not-text', 0],
  ]

  it.each(cases)('%s', (_name, input, kind, line) => {
    let r: ParseResult | undefined
    expect(() => {
      r = parseUnifiedDiff(input as string)
    }).not.toThrow()
    expect(r!.ok).toBe(false)
    if (r!.ok) return
    expect(r!.error.kind).toBe(kind)
    expect(r!.error.line).toBe(line)
    expect(r!.error.message.length).toBeGreaterThan(0)
  })

  it('never throws on a fuzzed corruption of the real fixture', () => {
    // Deterministic: a fixed-seed generator, so a failure reproduces.
    let seed = 310
    const rand = (n: number): number => {
      seed = (seed * 1103515245 + 12345) % 2147483648
      return seed % n
    }
    const lines = REAL_GIT_DIFF.split('\n')
    for (let i = 0; i < 400; i++) {
      const copy = [...lines]
      const at = rand(copy.length)
      const op = rand(3)
      if (op === 0) copy.splice(at, 1)
      else if (op === 1) copy[at] = (copy[at] ?? '').slice(0, rand(12))
      else copy.splice(at, 0, copy[rand(copy.length)] ?? '')
      expect(() => parseUnifiedDiff(copy.join('\n'))).not.toThrow()
    }
  })
})
