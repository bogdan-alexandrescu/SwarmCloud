// WHICH LEXER A FILE GETS, decided before the lexer is loaded (owner decision
// 2026-10-08, design §3 question 3). This module is in the viewer's own chunk
// and is a lookup table only: the lexer itself (`highlight.ts`) is a dynamic
// import, fetched the first time a file with a known language is shown. A file
// this table does not name is drawn as plain text and costs no download.
//
// A FILE OVER THE VIEWER'S SIZE CAP IS PLAIN TEXT. The cap is the one the
// viewer already has: more than LARGE_FILE_LINES changed lines (rows.ts), the
// size at which a file opens collapsed. Past it a file is read by its hunks,
// and colour is not worth the tokenising. A LINE over HIGHLIGHT_MAX_LINE
// characters is plain too: a minified bundle's one line is not code anyone
// reads by colour, and lexing it on every scroll would be.

import type { DiffFile } from './parse'
import { startsCollapsed } from './rows'

export type Lang = 'c' | 'ts' | 'go' | 'rust' | 'py' | 'sh' | 'yaml' | 'json' | 'css' | 'hcl'

/** A line longer than this many characters is drawn as plain text. */
export const HIGHLIGHT_MAX_LINE = 2000

const BY_EXTENSION: Readonly<Record<string, Lang>> = {
  ts: 'ts',
  tsx: 'ts',
  mts: 'ts',
  cts: 'ts',
  js: 'ts',
  jsx: 'ts',
  mjs: 'ts',
  cjs: 'ts',
  go: 'go',
  rs: 'rust',
  c: 'c',
  h: 'c',
  cc: 'c',
  cpp: 'c',
  hpp: 'c',
  java: 'c',
  kt: 'c',
  swift: 'c',
  py: 'py',
  pyi: 'py',
  sh: 'sh',
  bash: 'sh',
  zsh: 'sh',
  yml: 'yaml',
  yaml: 'yaml',
  json: 'json',
  jsonc: 'json',
  css: 'css',
  tf: 'hcl',
  tfvars: 'hcl',
  hcl: 'hcl',
}

const BY_NAME: Readonly<Record<string, Lang>> = {
  Makefile: 'sh',
  Dockerfile: 'sh',
  '.envrc': 'sh',
}

/** The lexer a path gets, or null for plain text. */
export function languageOf(path: string): Lang | null {
  const base = path.slice(path.lastIndexOf('/') + 1)
  const named = BY_NAME[base]
  if (named !== undefined) return named
  const dot = base.lastIndexOf('.')
  if (dot <= 0) return null
  return BY_EXTENSION[base.slice(dot + 1).toLowerCase()] ?? null
}

/**
 * The lexer a FILE OF THE PATCH gets: null for a binary file, for a file over
 * the size cap (`startsCollapsed`), and for an unknown language.
 */
export function highlightLanguage(f: DiffFile): Lang | null {
  if (f.binary || startsCollapsed(f)) return null
  return languageOf(f.path)
}
