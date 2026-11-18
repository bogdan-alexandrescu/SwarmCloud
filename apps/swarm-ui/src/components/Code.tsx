/**
 * CODE AND DIFF BLOCKS (components.html A, "Code and diff blocks"): 12px DM
 * Mono, line numbers in a gutter, added and removed lines tinted AND marked
 * with + and −, so a diff reads without colour.
 *
 * The full per-file diff viewer stays `diff/DiffView.tsx`, which the
 * inventory found already consistent; these are the small blocks a card or a
 * dialog shows.
 */
import type { ReactNode } from 'react'

export function CodeBlock({ title, lang, text, firstLine = 1, action }: { title: string; lang?: string; text: string; firstLine?: number; action?: ReactNode }) {
  const lines = text.replace(/\n$/, '').split('\n')
  return (
    <figure className="c-code">
      <figcaption className="c-codeh">
        <b>{title}</b>
        {lang !== undefined && <span>{lang}</span>}
        {action}
      </figcaption>
      <pre>
        {lines.map((l, i) => (
          <span key={i} className="c-dl">
            <span className="c-ln">{firstLine + i}</span>
            {l}
          </span>
        ))}
      </pre>
    </figure>
  )
}

export interface DiffLine {
  kind: 'add' | 'del' | 'ctx' | 'hunk'
  text: string
  /** The line number on the side it belongs to; none on a hunk header. */
  n?: number
}

/** Parse a unified diff's hunks into lines (the file header lines are dropped). */
export function parseDiff(patch: string): DiffLine[] {
  const out: DiffLine[] = []
  let a = 0
  let b = 0
  for (const raw of patch.replace(/\n$/, '').split('\n')) {
    if (raw.startsWith('+++') || raw.startsWith('---') || raw.startsWith('diff ') || raw.startsWith('index ')) continue
    const h = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/.exec(raw)
    if (h !== null) {
      a = Number(h[1])
      b = Number(h[2])
      out.push({ kind: 'hunk', text: raw })
    } else if (raw.startsWith('+')) {
      out.push({ kind: 'add', text: raw.slice(1), n: b++ })
    } else if (raw.startsWith('-')) {
      out.push({ kind: 'del', text: raw.slice(1), n: a++ })
    } else {
      out.push({ kind: 'ctx', text: raw.startsWith(' ') ? raw.slice(1) : raw, n: b++ })
      a++
    }
  }
  return out
}

export function DiffBlock({ file, patch, action }: { file: string; patch: string; action?: ReactNode }) {
  const lines = parseDiff(patch)
  const added = lines.filter((l) => l.kind === 'add').length
  const removed = lines.filter((l) => l.kind === 'del').length
  return (
    <figure className="c-code" data-diff>
      <figcaption className="c-codeh">
        <b>{file}</b>
        <span className="c-ds">
          <span className="is-add">+{added}</span> <span className="is-del">&minus;{removed}</span>
        </span>
        {action}
      </figcaption>
      <pre>
        {lines.map((l, i) =>
          l.kind === 'hunk' ? (
            <span key={i} className="c-hk">
              {l.text}
            </span>
          ) : (
            <span key={i} className={`c-dl${l.kind === 'add' ? ' is-add' : l.kind === 'del' ? ' is-del' : ''}`} data-line={l.kind}>
              <span className="c-ln">{l.n}</span>
              {l.kind === 'add' ? '+ ' : l.kind === 'del' ? '− ' : '  '}
              {l.text}
            </span>
          ),
        )}
      </pre>
    </figure>
  )
}
