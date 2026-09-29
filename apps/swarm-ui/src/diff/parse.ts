// `git diff --binary` OUTPUT, READ INTO FILES, HUNKS AND NUMBERED LINES (#310).
//
// WHY A HAND-WRITTEN PARSER rather than a package. The console adds no npm
// dependency for a view it can own in one file, and the parser is where the
// viewer's honesty lives: a line number it gets wrong is a line number the
// reader acts on. So each rule below says which piece of git's output it reads.
//
// WHAT IT NEVER DOES: throw. The patch is text an agent produced, and a viewer
// that crashes on it takes the page with it. Every malformed shape comes back
// as `{ ok: false, error }` with a `kind` a caller can switch on and the
// 1-based line it was found on. A value that is not a string is `not-text` at
// line 0, because it has no lines.
//
// WHAT IT READS, and where each piece of a file's identity comes from, in the
// order it is trusted:
//
//   paths    `rename from` / `copy from` (and `to`), then `--- ` / `+++ `, then
//            the `diff --git` line. The last is the only source that is
//            ambiguous -- `a/x b/y b/z` has two readings -- so it is used only
//            when nothing else names the file (a mode change, a pure rename, a
//            binary). Quoted paths are C-unescaped, octal bytes as UTF-8.
//   status   `new file mode` or `--- /dev/null` is added; `deleted file mode`
//            or `+++ /dev/null` is deleted; `rename`/`copy` headers; else
//            modified. A mode change is not a status: it is `oldMode` and
//            `newMode`, and a file can be renamed AND change mode.
//   binary   `Binary files ... differ` and `GIT binary patch`. The base85 body
//            of the latter is skipped to the next `diff --git`, never parsed:
//            its lines can begin with `-` and `+`.
//
// CRLF. A patch whose EVERY line ends in CRLF was converted in transit, so the
// CR is a line ending and is dropped. In any other patch a CR is part of the
// content -- a CRLF file inside an LF patch -- and a content line keeps it,
// exactly as given; only header lines, which git never writes with a CR, drop
// a trailing one.

export type LineKind = 'context' | 'add' | 'del'

export interface DiffLine {
  kind: LineKind
  /** The line's content, without its one-character prefix. */
  text: string
  oldNo: number | null
  newNo: number | null
  /** Followed by `\ No newline at end of file`. */
  noNewlineAtEnd: boolean
}

export interface DiffHunk {
  /** The `@@ ... @@` line as given. */
  header: string
  oldStart: number
  oldLines: number
  newStart: number
  newLines: number
  /** The function context git prints after the second `@@`, or ''. */
  section: string
  lines: DiffLine[]
}

export type FileStatus = 'modified' | 'added' | 'deleted' | 'renamed' | 'copied'

export interface DiffFile {
  /** null for an added file. */
  oldPath: string | null
  /** null for a deleted file. */
  newPath: string | null
  /** The path to show: the new one, or the old one for a deletion. */
  path: string
  status: FileStatus
  /** `similarity index N%`, for a rename or a copy. */
  similarity: number | null
  oldMode: string | null
  newMode: string | null
  binary: boolean
  hunks: DiffHunk[]
  additions: number
  deletions: number
}

export type DiffErrorKind =
  | 'not-text'
  | 'no-file-header'
  | 'bad-file-header'
  | 'bad-path'
  | 'bad-hunk-header'
  | 'truncated-hunk'
  | 'unexpected-line'

export interface DiffParseError {
  kind: DiffErrorKind
  /** 1-based line of the input; 0 when the input has no lines. */
  line: number
  message: string
}

export type ParseResult = { ok: true; files: DiffFile[] } | { ok: false; error: DiffParseError }

class Malformed extends Error {
  constructor(
    readonly kind: DiffErrorKind,
    readonly line: number,
    message: string,
  ) {
    super(message)
  }
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/
const NO_NEWLINE = '\\ No newline at end of file'
const DEV_NULL = '/dev/null'

/**
 * Read a C-quoted path as git writes one: `"a/caf\303\251 \"q\""`. Returns the
 * decoded text and the index just past the closing quote, or throws Malformed.
 */
function readQuoted(s: string, from: number, lineNo: number): { value: string; end: number } {
  const bytes: number[] = []
  const enc = new TextEncoder()
  const SIMPLE: Record<string, number> = { a: 7, b: 8, t: 9, n: 10, v: 11, f: 12, r: 13, '"': 34, '\\': 92 }
  let i = from + 1
  while (i < s.length) {
    const c = s[i]!
    if (c === '"') {
      return { value: new TextDecoder('utf-8').decode(new Uint8Array(bytes)), end: i + 1 }
    }
    if (c === '\\') {
      const n = s[i + 1]
      if (n === undefined) break
      if (n in SIMPLE) {
        bytes.push(SIMPLE[n]!)
        i += 2
        continue
      }
      const oct = /^[0-7]{3}/.exec(s.slice(i + 1, i + 4))
      if (oct) {
        bytes.push(parseInt(oct[0], 8) & 0xff)
        i += 4
        continue
      }
      throw new Malformed('bad-path', lineNo, `unknown escape \\${n} in a quoted path`)
    }
    for (const b of enc.encode(c)) bytes.push(b)
    i += 1
  }
  throw new Malformed('bad-path', lineNo, 'a quoted path is not closed')
}

function stripPrefix(p: string, prefix: 'a/' | 'b/'): string {
  return p.startsWith(prefix) ? p.slice(2) : p
}

/** The path on a `--- ` / `+++ ` line: quoted, or up to a tab (git's trailing tab, or a timestamp). */
function markerPath(rest: string, lineNo: number): string {
  if (rest.startsWith('"')) {
    const q = readQuoted(rest, 0, lineNo)
    return q.value
  }
  const tab = rest.indexOf('\t')
  const p = tab === -1 ? rest : rest.slice(0, tab)
  if (p === '') throw new Malformed('bad-file-header', lineNo, 'a ---/+++ line names no path')
  return p
}

/** The two paths on a `diff --git` line, prefixes still on. */
function gitLinePaths(rest: string, lineNo: number): [string, string] {
  if (rest === '') throw new Malformed('bad-file-header', lineNo, 'diff --git names no paths')
  let a: string
  let after: string
  if (rest.startsWith('"')) {
    const q = readQuoted(rest, 0, lineNo)
    a = q.value
    after = rest.slice(q.end)
    if (!after.startsWith(' ')) throw new Malformed('bad-file-header', lineNo, 'diff --git names one path')
    after = after.slice(1)
  } else {
    const quotedB = rest.indexOf(' "')
    if (quotedB !== -1 && rest.endsWith('"')) {
      a = rest.slice(0, quotedB)
      after = rest.slice(quotedB + 1)
    } else {
      // Unquoted on both sides. When the paths are the same -- the common case,
      // and the only one where this line is the sole source -- the line is
      // `a/P b/P` and splits exactly in the middle, spaces in P or not.
      const mid = (rest.length - 1) / 2
      if (Number.isInteger(mid) && rest[mid] === ' ' && rest.slice(2, mid) === rest.slice(mid + 3)) {
        return [rest.slice(0, mid), rest.slice(mid + 1)]
      }
      const cut = rest.indexOf(' b/')
      if (cut === -1) {
        const sp = rest.indexOf(' ')
        if (sp === -1) throw new Malformed('bad-file-header', lineNo, 'diff --git names one path')
        return [rest.slice(0, sp), rest.slice(sp + 1)]
      }
      return [rest.slice(0, cut), rest.slice(cut + 1)]
    }
  }
  const b = after.startsWith('"') ? readQuoted(after, 0, lineNo).value : after
  if (b === '') throw new Malformed('bad-file-header', lineNo, 'diff --git names one path')
  return [a, b]
}

interface Building {
  gitA: string | null
  gitB: string | null
  minus: string | null
  plus: string | null
  from: string | null
  to: string | null
  kind: 'rename' | 'copy' | null
  added: boolean
  deleted: boolean
  similarity: number | null
  oldMode: string | null
  newMode: string | null
  binary: boolean
  hunks: DiffHunk[]
  sawMinus: boolean
}

function fresh(): Building {
  return {
    gitA: null,
    gitB: null,
    minus: null,
    plus: null,
    from: null,
    to: null,
    kind: null,
    added: false,
    deleted: false,
    similarity: null,
    oldMode: null,
    newMode: null,
    binary: false,
    hunks: [],
    sawMinus: false,
  }
}

function finish(b: Building, lineNo: number): DiffFile {
  // git always writes the a/ and b/ prefixes. A plain unified diff may or may
  // not, so there they come off only when both sides carry them.
  const strip = b.gitA !== null || (b.minus?.startsWith('a/') === true && b.plus?.startsWith('b/') === true)
  const side = (p: string | null, prefix: 'a/' | 'b/'): string | null =>
    p === null || p === DEV_NULL || !strip ? p : stripPrefix(p, prefix)
  const minus = side(b.minus, 'a/')
  const plus = side(b.plus, 'b/')
  const added = b.added || minus === DEV_NULL
  const deleted = b.deleted || plus === DEV_NULL
  let oldPath: string | null = b.from ?? (minus !== DEV_NULL ? minus : null) ?? (b.gitA !== null ? stripPrefix(b.gitA, 'a/') : null)
  let newPath: string | null = b.to ?? (plus !== DEV_NULL ? plus : null) ?? (b.gitB !== null ? stripPrefix(b.gitB, 'b/') : null)
  if (added) oldPath = null
  if (deleted) newPath = null
  const path = newPath ?? oldPath
  if (path === null) throw new Malformed('bad-file-header', lineNo, 'a file header names no path')

  const status: FileStatus = added
    ? 'added'
    : deleted
      ? 'deleted'
      : b.kind === 'rename'
        ? 'renamed'
        : b.kind === 'copy'
          ? 'copied'
          : 'modified'
  let additions = 0
  let deletions = 0
  for (const h of b.hunks) {
    for (const l of h.lines) {
      if (l.kind === 'add') additions += 1
      else if (l.kind === 'del') deletions += 1
    }
  }
  return {
    oldPath,
    newPath,
    path,
    status,
    similarity: b.similarity,
    oldMode: b.oldMode,
    newMode: b.newMode,
    binary: b.binary,
    hunks: b.hunks,
    additions,
    deletions,
  }
}

function splitLines(text: string): string[] {
  const lines = text.split('\n')
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()
  // Every line terminated by CRLF, and at least one line: the CR is transport.
  const allCrlf = lines.length > 0 && text.endsWith('\r\n') && lines.every((l) => l.endsWith('\r'))
  return allCrlf ? lines.map((l) => l.slice(0, -1)) : lines
}

/** A header line never carries a CR of its own. */
function hdr(line: string): string {
  return line.endsWith('\r') ? line.slice(0, -1) : line
}

function parse(text: string): DiffFile[] {
  const lines = splitLines(text)
  const files: DiffFile[] = []
  let cur: Building | null = null
  let curStart = 0
  let firstText = 0
  let i = 0

  const close = (): void => {
    if (cur !== null) files.push(finish(cur, curStart))
    cur = null
  }

  const startsPlain = (at: number): boolean =>
    hdr(lines[at] ?? '').startsWith('--- ') && hdr(lines[at + 1] ?? '').startsWith('+++ ')

  while (i < lines.length) {
    const raw = lines[i]!
    const line = hdr(raw)
    const lineNo = i + 1

    if (line.startsWith('diff --git')) {
      close()
      if (line !== 'diff --git' && !line.startsWith('diff --git ')) {
        throw new Malformed('bad-file-header', lineNo, 'not a diff --git line')
      }
      const [a, b] = gitLinePaths(line.slice('diff --git '.length), lineNo)
      cur = fresh()
      cur.gitA = a
      cur.gitB = b
      curStart = lineNo
      i += 1
      continue
    }

    if (cur === null) {
      if (startsPlain(i)) {
        cur = fresh()
        curStart = lineNo
        continue
      }
      if (line.startsWith('@@')) throw new Malformed('no-file-header', lineNo, 'a hunk appears before any file header')
      if (firstText === 0 && line.trim() !== '') firstText = lineNo
      i += 1
      continue
    }

    const f: Building = cur

    // A format-patch signature: everything after it is the version line.
    if (line === '-- ') break

    if (line.startsWith('@@')) {
      if (f.gitA !== null && f.minus === null && f.plus === null) {
        throw new Malformed('bad-file-header', lineNo, 'a hunk with no ---/+++ header')
      }
      i = readHunk(lines, i, f)
      continue
    }

    if (line.startsWith('--- ') && f.hunks.length === 0 && !f.sawMinus) {
      f.minus = markerPath(line.slice(4), lineNo)
      f.sawMinus = true
      i += 1
      continue
    }
    if (line.startsWith('+++ ') && f.hunks.length === 0) {
      if (!f.sawMinus) throw new Malformed('bad-file-header', lineNo, 'a +++ line with no --- line before it')
      f.plus = markerPath(line.slice(4), lineNo)
      i += 1
      continue
    }

    // A plain unified diff's next file starts with ---/+++ and no diff --git.
    if (f.gitA === null && f.hunks.length > 0 && startsPlain(i)) {
      close()
      continue
    }

    if (f.hunks.length === 0 && !f.sawMinus) {
      const m = /^(old mode|new mode|deleted file mode|new file mode) (\d+)$/.exec(line)
      if (m) {
        if (m[1] === 'old mode') f.oldMode = m[2]!
        else if (m[1] === 'new mode') f.newMode = m[2]!
        else if (m[1] === 'deleted file mode') {
          f.deleted = true
          f.oldMode = m[2]!
        } else {
          f.added = true
          f.newMode = m[2]!
        }
        i += 1
        continue
      }
      const ren = /^(rename|copy) (from|to) (.*)$/.exec(line)
      if (ren) {
        const p = ren[3]!.startsWith('"') ? readQuoted(ren[3]!, 0, lineNo).value : ren[3]!
        f.kind = ren[1] as 'rename' | 'copy'
        if (ren[2] === 'from') f.from = p
        else f.to = p
        i += 1
        continue
      }
      const sim = /^(?:similarity|dissimilarity) index (\d+)%$/.exec(line)
      if (sim) {
        f.similarity = line.startsWith('similarity') ? Number(sim[1]) : 100 - Number(sim[1])
        i += 1
        continue
      }
      if (line.startsWith('index ')) {
        const mode = /^index [0-9a-f]+\.\.[0-9a-f]+ (\d+)$/.exec(line)
        if (mode && f.oldMode === null && f.newMode === null) {
          f.oldMode = mode[1]!
          f.newMode = mode[1]!
        }
        i += 1
        continue
      }
      if (/^Binary files .* differ$/.test(line)) {
        f.binary = true
        if (/^Binary files \/dev\/null and /.test(line)) f.added = true
        if (/ and \/dev\/null differ$/.test(line)) f.deleted = true
        i += 1
        continue
      }
      if (line === 'GIT binary patch') {
        f.binary = true
        i += 1
        while (i < lines.length && !hdr(lines[i]!).startsWith('diff --git ')) i += 1
        continue
      }
    }

    if (line.trim() === '') {
      i += 1
      continue
    }
    throw new Malformed('unexpected-line', lineNo, `unexpected line: ${JSON.stringify(line.slice(0, 80))}`)
  }
  close()

  if (files.length === 0 && firstText !== 0) {
    throw new Malformed('no-file-header', firstText, 'no "diff --git" or ---/+++ file header was found')
  }
  return files
}

/** Read one hunk starting at `at`; returns the index of the first line after it. */
function readHunk(lines: string[], at: number, f: Building): number {
  const headerLine = hdr(lines[at]!)
  const m = HUNK.exec(headerLine)
  if (!m) throw new Malformed('bad-hunk-header', at + 1, `not a hunk header: ${JSON.stringify(headerLine.slice(0, 80))}`)
  const oldStart = Number(m[1])
  const oldLines = m[2] === undefined ? 1 : Number(m[2])
  const newStart = Number(m[3])
  const newLines = m[4] === undefined ? 1 : Number(m[4])
  const hunk: DiffHunk = {
    header: headerLine,
    oldStart,
    oldLines,
    newStart,
    newLines,
    section: m[5] ?? '',
    lines: [],
  }
  let oldLeft = oldLines
  let newLeft = newLines
  let oldNo = oldStart
  let newNo = newStart
  let i = at + 1
  while (oldLeft > 0 || newLeft > 0) {
    if (i >= lines.length) {
      throw new Malformed('truncated-hunk', at + 1, `the hunk ends with ${oldLeft} old and ${newLeft} new lines missing`)
    }
    const raw = lines[i]!
    const c = raw[0]
    if (raw === '' || c === ' ') {
      if (oldLeft === 0 || newLeft === 0) {
        throw new Malformed('truncated-hunk', at + 1, 'a context line after one side of the hunk is complete')
      }
      hunk.lines.push({ kind: 'context', text: raw.slice(1), oldNo, newNo, noNewlineAtEnd: false })
      oldNo += 1
      newNo += 1
      oldLeft -= 1
      newLeft -= 1
    } else if (c === '-') {
      if (oldLeft === 0) throw new Malformed('truncated-hunk', at + 1, 'more removed lines than the header counts')
      hunk.lines.push({ kind: 'del', text: raw.slice(1), oldNo, newNo: null, noNewlineAtEnd: false })
      oldNo += 1
      oldLeft -= 1
    } else if (c === '+') {
      if (newLeft === 0) throw new Malformed('truncated-hunk', at + 1, 'more added lines than the header counts')
      hunk.lines.push({ kind: 'add', text: raw.slice(1), oldNo: null, newNo, noNewlineAtEnd: false })
      newNo += 1
      newLeft -= 1
    } else if (hdr(raw) === NO_NEWLINE) {
      const last = hunk.lines[hunk.lines.length - 1]
      if (last) last.noNewlineAtEnd = true
    } else if (raw.startsWith('diff --git') || raw.startsWith('@@')) {
      throw new Malformed('truncated-hunk', at + 1, `the hunk ends with ${oldLeft} old and ${newLeft} new lines missing`)
    } else {
      throw new Malformed('unexpected-line', i + 1, `not a diff line: ${JSON.stringify(raw.slice(0, 80))}`)
    }
    i += 1
  }
  // The marker for the hunk's final line comes after the counts run out.
  if (i < lines.length && hdr(lines[i]!) === NO_NEWLINE) {
    const last = hunk.lines[hunk.lines.length - 1]
    if (last) last.noNewlineAtEnd = true
    i += 1
  }
  f.hunks.push(hunk)
  return i
}

/** Read `git diff` / `git diff --binary` output. Never throws. */
export function parseUnifiedDiff(text: string): ParseResult {
  if (typeof text !== 'string') {
    return { ok: false, error: { kind: 'not-text', line: 0, message: 'the patch is not text' } }
  }
  try {
    return { ok: true, files: parse(text) }
  } catch (e) {
    if (e instanceof Malformed) return { ok: false, error: { kind: e.kind, line: e.line, message: e.message } }
    // Nothing above throws anything else; if something does, it is still a
    // parse that failed, not a page that crashed.
    return { ok: false, error: { kind: 'unexpected-line', line: 0, message: String(e) } }
  }
}
