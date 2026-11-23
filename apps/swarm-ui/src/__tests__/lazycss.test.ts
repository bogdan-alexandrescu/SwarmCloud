// A LAZY SCREEN'S CLASSES ARE STYLED ON A COLD LOAD (issue 195's rendered review).
//
// Before the screens split, styles.css and every screen's sheet went out as
// ONE stylesheet, so a class defined in Guide's sheet styled the Help page
// too. A lazy screen's sheet now loads only with that screen's chunk: a class
// one lazy screen uses but only ANOTHER lazy screen's sheet defines is drawn
// unstyled until the other screen has been visited (the Help page's
// `help-guide` line lived in guide.css; `sb-note`, used by Schedules, the admin
// counts page and the voice Talk page, lived only in submit.css).
//
// What this reads, from the source alone: the modules main.tsx reaches through
// static imports are the shell, and every sheet they import is always loaded.
// Each `import('./X')` names a lazy chunk: its modules are what X reaches
// statically beyond the shell, and its sheets are the ones those modules
// import. A class a chunk's modules write in a string must then be defined in
// a sheet the chunk or the shell loads whenever it is defined in some other
// chunk's sheet. The shell itself is held to the same rule: a class it uses
// may not live only in a lazy chunk's sheet.
//
// BREAK IT: move `.help-guide` from styles/help.css back to styles/guide.css,
// or `.sb-note` from styles/components.css to styles/submit.css -- the test
// names the chunk, the class and the sheet it was found in.
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { blankComments } from './cssgate'

const SRC = join(__dirname, '..')

function read(file: string): string {
  return readFileSync(join(SRC, file), 'utf8')
}

/** Every module and sheet under src/, tests left out. */
function sources(dir = SRC): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    if (statSync(full).isDirectory()) {
      if (name === '__tests__' || name === 'node_modules') continue
      out.push(...sources(full))
    } else if (/\.(tsx?|css)$/.test(name) && !/\.test\.tsx?$/.test(name)) {
      out.push(relative(SRC, full))
    }
  }
  return out
}

/** A relative import's file under src/ (a module or a sheet), or null for a package. */
function resolved(from: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null
  const base = resolve(dirname(join(SRC, from)), spec)
  for (const f of [base, `${base}.ts`, `${base}.tsx`, join(base, 'index.ts'), join(base, 'index.tsx')]) {
    if (/\.(tsx?|css)$/.test(f) && existsSync(f) && statSync(f).isFile()) return relative(SRC, f)
  }
  return null
}

const STATIC = /^\s*(?:import|export)\s+(?!type\b)(?:[^'";]*?\sfrom\s+)?['"]([^'"]+)['"]/gm
const DYNAMIC = /\bimport\(\s*['"]([^'"]+)['"]\s*\)/g

function staticImports(file: string): string[] {
  if (file.endsWith('.css')) return []
  const out: string[] = []
  for (const m of read(file).matchAll(STATIC)) {
    const r = resolved(file, m[1]!)
    if (r !== null) out.push(r)
  }
  return out
}

/** What a module reaches through static imports, itself included. */
function closure(entry: string): Set<string> {
  const seen = new Set([entry])
  const todo = [entry]
  while (todo.length > 0) {
    for (const next of staticImports(todo.pop()!)) {
      if (seen.has(next)) continue
      seen.add(next)
      todo.push(next)
    }
  }
  return seen
}

/**
 * Each selector a sheet writes, as the class names it needs: `.vtel-bars .b`
 * needs both, so a module that writes only `b` is not styled by it.
 * Comments, strings and urls are blanked first.
 */
function selectors(sheet: string): string[][] {
  const css = blankComments(read(sheet))
    .replace(/url\([^)]*\)/g, 'url()')
    .replace(/"[^"\n]*"|'[^'\n]*'/g, '""')
  const out: string[][] = []
  // A rule's prelude is the text before its `{`, back to the previous `}`, `{` or `;`.
  for (const m of css.matchAll(/([^{};]*)\{/g)) {
    const prelude = m[1]!
    if (/^\s*@/.test(prelude)) continue
    for (const one of prelude.split(',')) {
      const classes = [...one.replace(/:not\([^)]*\)/g, '').matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((c) => c[1]!)
      if (classes.length > 0) out.push(classes)
    }
  }
  return out
}

/** The words a module writes in its string and template literals: every class it can name. */
function usedWords(module: string): Set<string> {
  const code = read(module)
  const out = new Set<string>()
  for (const m of code.matchAll(/'([^'\\\n]*)'|"([^"\\\n]*)"|`([^`\\]*)`/g)) {
    const text = (m[1] ?? m[2] ?? m[3] ?? '').replace(/\$\{[^}]*\}\S*/g, ' ')
    for (const w of text.split(/\s+/)) if (/^-?[_a-zA-Z][\w-]*$/.test(w)) out.add(w)
  }
  return out
}

const ALL = sources()
const SHELL = closure('main.tsx')
const SHELL_SHEETS = [...SHELL].filter((f) => f.endsWith('.css'))

/** Every lazy chunk: each `import('./X')` that leaves the shell, with the modules and sheets it adds. */
const CHUNKS: { entry: string; modules: string[]; sheets: string[] }[] = (() => {
  const entries = new Set<string>()
  for (const file of ALL) {
    if (file.endsWith('.css')) continue
    for (const m of read(file).matchAll(DYNAMIC)) {
      const r = resolved(file, m[1]!)
      if (r !== null && !SHELL.has(r)) entries.add(r)
    }
  }
  return [...entries].sort().map((entry) => {
    const own = [...closure(entry)].filter((f) => !SHELL.has(f))
    return { entry, modules: own.filter((f) => !f.endsWith('.css')), sheets: own.filter((f) => f.endsWith('.css')) }
  })
})()

const SELECTORS = new Map(ALL.filter((f) => f.endsWith('.css')).map((f) => [f, selectors(f)] as const))

/**
 * Words that are a module's data, not a class, though a sheet elsewhere has a
 * class of the same name: route keys and slugs, and a corner's name.
 */
const NOT_CLASSES = new Set([
  'guide', // App.tsx `GUIDE`, the guide route's slug; Onboarding.tsx's link to it
  'mcp-servers', // the MCP servers page's key in Spine.tsx, help.ts and ConfigSetup.tsx
  'tl', // voice/Floating.tsx `Corner`, the top-left corner
])

const WORDS = new Map<string, Set<string>>()
function words(module: string): Set<string> {
  if (!WORDS.has(module)) WORDS.set(module, usedWords(module))
  return WORDS.get(module)!
}
const SHELL_MODULES = [...SHELL].filter((f) => !f.endsWith('.css'))
const SHELL_WORDS = new Set(SHELL_MODULES.flatMap((m) => [...words(m)]))

/**
 * Each class `modules` write that no `loaded` sheet defines but a selector in a
 * lazy chunk's sheet outside them does, every class of that selector being
 * one the chunk or the shell writes.
 */
function stranded(modules: string[], loaded: string[]): string[] {
  const have = new Set<string>()
  for (const s of loaded) for (const sel of SELECTORS.get(s)!) for (const c of sel) have.add(c)
  const reach = new Set([...SHELL_WORDS, ...modules.flatMap((m) => [...words(m)])])
  const elsewhere = [...new Set(CHUNKS.flatMap((c) => c.sheets))].filter((s) => !loaded.includes(s))
  const out = new Set<string>()
  for (const module of modules) {
    for (const word of words(module)) {
      if (have.has(word) || NOT_CLASSES.has(word)) continue
      const owners = elsewhere.filter((s) => SELECTORS.get(s)!.some((sel) => sel.includes(word) && sel.every((c) => reach.has(c))))
      if (owners.length > 0) out.add(`${module}: .${word} is defined only in ${owners.join(', ')}`)
    }
  }
  return [...out].sort()
}

describe('every class a screen uses is styled on a cold load of that screen', () => {
  it('reads the shell and finds the lazy chunks', () => {
    expect(SHELL.has('App.tsx')).toBe(true)
    expect(SHELL_SHEETS).toContain('styles.css')
    expect(CHUNKS.map((c) => c.entry)).toEqual(expect.arrayContaining(['HelpSection.tsx', 'Guide.tsx', 'Submit.tsx', 'Schedules.tsx']))
  })

  it('the shell uses no class that only a lazy screen\'s sheet defines', () => {
    expect(stranded(SHELL_MODULES, SHELL_SHEETS)).toEqual([])
  })

  for (const chunk of CHUNKS) {
    it(`${chunk.entry} uses no class that only another lazy screen's sheet defines`, () => {
      expect(stranded(chunk.modules, [...SHELL_SHEETS, ...chunk.sheets])).toEqual([])
    })
  }
})
