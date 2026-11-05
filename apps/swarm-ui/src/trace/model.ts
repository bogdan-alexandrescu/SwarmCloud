// THE TRANSCRIPT AS A TRACE (owner pick, 2026-11-05, part of #2688): the
// transcript's steps as one timeline of events, each a row. Everything here is
// read from the steps `GET /v1/tasks/{id}/transcript` serves (`map_event` in
// swarm_api/transcript.py); a field the record does not carry is left out,
// never worked out.

import { useSyncExternalStore } from 'react'

import type { TranscriptStep } from '../types'

export type EvKind = 'message' | 'tool' | 'edit' | 'test' | 'browser' | 'thinking' | 'start' | 'end' | 'system' | 'other'
export type EvStatus = 'ok' | 'error' | 'running' | null

export interface Ev {
  key: string
  n: number
  kind: EvKind
  label: string
  /** A short second part of the label: a file's folder, a test count, a tool's target. */
  sub: string | null
  status: EvStatus
  /** Seconds from the CLI's `tool_progress.elapsed_time_seconds` (a lower bound), or the result's `duration_ms`. */
  secs: { value: number; from: 'progress' | 'result' } | null
  progress: number
  main: TranscriptStep
  call: TranscriptStep | null
  result: TranscriptStep | null
  progressSteps: TranscriptStep[]
  /** Every string a search looks through. */
  hay: string
}

export interface Trace {
  events: Ev[]
  cwd: string | null
  root: string | null
  model: string | null
}

function rawOf(s: TranscriptStep): Record<string, unknown> | null {
  if (s.raw === null) return null
  try {
    const v: unknown = JSON.parse(s.raw)
    return v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : null
  } catch {
    return null
  }
}
export { rawOf }

export function inputOf(s: TranscriptStep | null): Record<string, unknown> {
  if (s?.tool?.input == null) return {}
  try {
    const v: unknown = JSON.parse(s.tool.input)
    return v !== null && typeof v === 'object' ? (v as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

/** The attempt's paths, dropped from commands and file names; shown once, in a tooltip. */
export function shorten(text: string, cwd: string | null, root: string | null): string {
  let out = text
  if (cwd) out = out.split(`cd ${cwd} && `).join('').split(`cd ${cwd}; `).join('').split(`${cwd}/`).join('')
  if (root) out = out.split(root).join('')
  return out
}

const TEST = /\b(pytest|vitest|related-tests\.sh|make (test|smoke)|npm (run )?test|playwright test)\b/
const EDIT_TOOLS = new Set(['Edit', 'MultiEdit', 'Write', 'NotebookEdit'])

export function toolShort(name: string): string {
  return name.replace(/^mcp__[^_]+(?:_[^_]+)*?__/, '')
}

function testSummary(out: string): string | null {
  const failed = /(\d+) failed/.exec(out)?.[1]
  const passed = /(\d+) passed/.exec(out)?.[1]
  if (failed === undefined && passed === undefined) return null
  return [failed && `${failed} failed`, passed && `${passed} passed`].filter(Boolean).join(' · ')
}

export function patchOf(ev: Ev): { lines: string[]; oldStart: number | null; newStart: number | null }[] | null {
  const raw = ev.result ? rawOf(ev.result) : null
  const tur = raw?.tool_use_result as Record<string, unknown> | undefined
  const sp = tur?.structuredPatch
  if (Array.isArray(sp) && sp.length > 0) {
    return sp.map((h: Record<string, unknown>) => ({
      lines: (h.lines as string[]) ?? [],
      oldStart: typeof h.oldStart === 'number' ? h.oldStart : null,
      newStart: typeof h.newStart === 'number' ? h.newStart : null,
    }))
  }
  const inp = inputOf(ev.call)
  if (typeof inp.old_string === 'string' && typeof inp.new_string === 'string') {
    return [{ lines: [...inp.old_string.split('\n').map((l) => `-${l}`), ...inp.new_string.split('\n').map((l) => `+${l}`)], oldStart: null, newStart: null }]
  }
  if (typeof inp.content === 'string') return [{ lines: inp.content.replace(/\n$/, '').split('\n').map((l) => `+${l}`), oldStart: null, newStart: 1 }]
  return null
}

function firstLine(t: string): string {
  const line = t.split('\n').find((l) => l.trim() !== '') ?? ''
  // Strip only markdown markers (a leading heading/quote marker, backtick pairs,
  // emphasis around words); a _ or # inside a word or path is the agent's text.
  return line
    .replace(/^\s*(?:#{1,6}\s+|>\s*)/, '')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/(\*\*|__)(?=\S)(.+?)(?<=\S)\1/g, '$2')
    .replace(/(^|[^\w*])([*_])(?=\S)([^*_]+?)(?<=\S)\2(?![\w*])/g, '$1$3')
    .trim()
}

export function buildTrace(steps: readonly TranscriptStep[]): Trace {
  const events: Ev[] = []
  const byTool = new Map<string, Ev>()
  let cwd: string | null = null
  let model: string | null = null
  const orphanProgress: TranscriptStep[] = []
  const push = (e: Omit<Ev, 'n' | 'progress' | 'progressSteps' | 'hay' | 'call' | 'result' | 'secs'> & Partial<Ev>) => {
    const ev: Ev = { n: events.length + 1, progress: 0, progressSteps: [], hay: '', call: null, result: null, secs: null, ...e }
    events.push(ev)
    return ev
  }
  const flushOrphans = () => {
    if (orphanProgress.length === 0) return
    const s = orphanProgress.splice(0)
    push({ key: s[0]!.id, kind: 'system', label: `${s.length} progress event${s.length === 1 ? '' : 's'}`, sub: null, status: null, main: s[0]!, progressSteps: s, progress: s.length })
  }
  for (const s of steps) {
    const meta = s.meta ?? {}
    if (s.kind === 'other' && meta.raw_type === 'tool_progress') {
      const raw = rawOf(s)
      const ev = typeof raw?.tool_use_id === 'string' ? byTool.get(raw.tool_use_id) : undefined
      if (ev === undefined) {
        orphanProgress.push(s)
        continue
      }
      ev.progress += 1
      ev.progressSteps.push(s)
      const el = raw?.elapsed_time_seconds
      if (typeof el === 'number' && (ev.secs === null || el > ev.secs.value)) ev.secs = { value: el, from: 'progress' }
      continue
    }
    flushOrphans()
    if (s.kind === 'init') {
      const raw = rawOf(s)
      cwd = typeof raw?.cwd === 'string' ? raw.cwd : null
      model = typeof meta.model === 'string' ? meta.model : null
      const tools = typeof meta.tool_count === 'number' ? `${meta.tool_count} tools` : null
      push({ key: s.id, kind: 'start', label: 'start', sub: [model, tools].filter(Boolean).join(' · ') || null, status: null, main: s })
    } else if (s.kind === 'text') {
      push({ key: s.id, kind: 'message', label: firstLine(s.text ?? '') || '(empty message)', sub: s.role === 'user' ? 'user' : null, status: null, main: s })
    } else if (s.kind === 'thinking') {
      const t = (s.text ?? '').trim()
      push({ key: s.id, kind: 'thinking', label: t === '' ? 'thinking' : firstLine(t), sub: t === '' ? 'text not recorded' : null, status: null, main: s })
    } else if (s.kind === 'tool_call') {
      const ev = push({ key: s.id, kind: 'tool', label: s.tool?.name ?? 'tool', sub: null, status: 'running', main: s, call: s })
      if (s.tool?.id) byTool.set(s.tool.id, ev)
    } else if (s.kind === 'tool_result') {
      const id = s.tool_result?.tool_use_id
      const ev = id ? byTool.get(id) : undefined
      if (ev !== undefined && ev.result === null) {
        ev.result = s
        ev.status = s.tool_result?.is_error === true ? 'error' : 'ok'
      } else {
        push({ key: s.id, kind: 'tool', label: 'tool result', sub: 'its call is not in this read', status: s.tool_result?.is_error === true ? 'error' : null, main: s, result: s })
      }
    } else if (s.kind === 'result') {
      const ok = meta.is_error !== true
      push({ key: s.id, kind: 'end', label: `result · ${String(meta.subtype ?? 'no subtype')}`, sub: typeof meta.num_turns === 'number' ? `${meta.num_turns} turns` : null, status: ok ? 'ok' : 'error', main: s, secs: typeof meta.duration_ms === 'number' ? { value: meta.duration_ms / 1000, from: 'result' } : null })
    } else if (s.kind === 'rate_limit') {
      const st = meta['rate_limit_info.status'] ?? meta.status
      const kind = meta['rate_limit_info.rateLimitType']
      const use = meta['rate_limit_info.utilization']
      push({ key: s.id, kind: 'system', label: `rate limit${st ? ` · ${String(st)}` : ''}`, sub: [kind, typeof use === 'number' ? `${Math.round(use * 100)}% used` : null].filter(Boolean).join(' · ') || null, status: null, main: s })
    } else if (s.kind === 'error') {
      push({ key: s.id, kind: 'system', label: firstLine(s.text ?? '') || 'error', sub: null, status: 'error', main: s })
    } else if (s.kind === 'usage') {
      push({ key: s.id, kind: 'system', label: 'usage', sub: s.text, status: null, main: s })
    } else if (s.kind === 'system') {
      push({ key: s.id, kind: 'system', label: `system · ${String(meta.subtype ?? 'no subtype')}`, sub: null, status: null, main: s })
    } else {
      push({ key: s.id, kind: 'other', label: String(meta.raw_type ?? s.kind), sub: 'as recorded', status: null, main: s })
    }
  }
  flushOrphans()
  const root = cwd !== null && /^\/workspace\/[^/]+\//.test(cwd) ? /^\/workspace\/[^/]+\//.exec(cwd)![0] : null
  // Second pass: name each tool call now that the working directory is known.
  for (const ev of events) {
    if (ev.call === null) continue
    const name = ev.call.tool?.name ?? 'tool'
    const inp = inputOf(ev.call)
    const out = ev.result?.tool_result?.content ?? ''
    if (name === 'Bash') {
      const cmd = shorten(String(inp.command ?? ''), cwd, root)
      ev.label = cmd
      if (TEST.test(cmd)) {
        ev.kind = 'test'
        ev.sub = ev.result ? testSummary(out) : null
      }
    } else if (EDIT_TOOLS.has(name)) {
      ev.kind = 'edit'
      const path = shorten(String(inp.file_path ?? inp.notebook_path ?? ''), cwd, root)
      ev.label = `${name} ${path.split('/').pop()}`
      const patch = patchOf(ev)
      const add = patch?.reduce((a, h) => a + h.lines.filter((l) => l.startsWith('+')).length, 0) ?? 0
      const del = patch?.reduce((a, h) => a + h.lines.filter((l) => l.startsWith('-')).length, 0) ?? 0
      ev.sub = `+${add} −${del}`
    } else if (/(^|__)browser_/.test(name)) {
      ev.kind = 'browser'
      const short = toolShort(name).replace(/^browser_/, '')
      const target = inp.url ?? inp.element ?? inp.text ?? null
      ev.label = target ? `${short} ${String(target)}` : short
      const page = /Page URL: (\S+)/.exec(out)?.[1]
      if (short === 'console_messages') {
        const errs = (out.match(/^\[ERROR\]/gm) ?? []).length
        const warns = (out.match(/^\[WARNING\]/gm) ?? []).length
        ev.sub = `${errs} error${errs === 1 ? '' : 's'} · ${warns} warning${warns === 1 ? '' : 's'}`
      } else if (short === 'take_screenshot') ev.sub = page ?? null
    } else if (name === 'Read') {
      const path = shorten(String(inp.file_path ?? ''), cwd, root)
      ev.label = `Read ${path.split('/').pop()}`
      ev.sub = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : null
    } else {
      const first = Object.values(inp).find((v) => typeof v === 'string') as string | undefined
      ev.label = `${toolShort(name)}${first ? ` ${shorten(first, cwd, root)}` : ''}`
    }
  }
  for (const ev of events) {
    ev.hay = [ev.label, ev.sub ?? '', ev.main.text ?? '', shorten(ev.call?.tool?.input ?? '', cwd, root), shorten(ev.result?.tool_result?.content ?? '', cwd, root)].join('\n').toLowerCase()
  }
  return { events, cwd, root, model }
}

export type Filter = 'all' | 'messages' | 'tools' | 'edits' | 'errors'
export function inFilter(ev: Ev, f: Filter): boolean {
  if (f === 'all') return true
  if (f === 'messages') return ev.kind === 'message'
  if (f === 'tools') return ev.kind === 'tool' || ev.kind === 'test' || ev.kind === 'browser' || ev.kind === 'edit'
  if (f === 'edits') return ev.kind === 'edit'
  return ev.status === 'error'
}

/** A duration as the console writes one: `12 s`, `4 m 12 s`. */
export function dur(secs: number): string {
  // The agent head's own format ('2m 4s'), so the two times on one screen read alike.
  if (secs < 60) return `${secs < 10 ? Math.round(secs * 10) / 10 : Math.round(secs)}s`
  const m = Math.floor(secs / 60)
  const s = Math.round(secs - m * 60)
  return s === 0 ? `${m}m` : `${m}m ${s}s`
}

// THE AGENT LIST FOLDS AWAY WHILE A TRANSCRIPT IS READ, and comes back in one
// click (the Inspector's strip) or `[`. Folded only while a trace is on screen:
// `mounted` counts the traces drawn, so leaving the Transcript view brings the
// list back without the trace having to say so.
let wide = true
let mounted = 0
const subs = new Set<() => void>()
const tell = () => subs.forEach((f) => f())
export function setTraceWide(next: boolean): void {
  wide = next
  tell()
}
/** Called by a drawn trace; the returned function is its unmount. A new trace opens full width. */
export function traceMounted(): () => void {
  mounted += 1
  wide = true
  tell()
  return () => {
    mounted -= 1
    tell()
  }
}
/** Whether the trace asked for the room: the list is folded only while one is drawn. */
export function useTraceWide(): boolean {
  return useSyncExternalStore(
    (f) => {
      subs.add(f)
      return () => subs.delete(f)
    },
    () => wide && mounted > 0,
  )
}
