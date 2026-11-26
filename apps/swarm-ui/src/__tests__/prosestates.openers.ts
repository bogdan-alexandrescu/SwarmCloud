// THE OPENERS (issue 439, review round 2): every opener a screen file draws,
// named so a state can say which one its click opens, and read back from
// React's tree after the click so that claim is checked, not trusted.
//
// WHY. The cover test counted states that NAME a file, not what they open: a
// file with two dialogs and two states that both open the first passed, and a
// runtime audit found thirteen openers no state ever opened (a TypedConfirm in
// RunnerSizes.tsx, a fold in Artifacts.tsx, a dialog in People.tsx, ...).
// Now each opener has an id, `<file> <kind>#<n>` (`RunnerSizes.tsx
// <TypedConfirm>#1`: the file's first TypedConfirm), counted in source order
// per kind, so adding a fold of another kind renumbers nothing. A state lists
// the ids its click opens in `opens` (prosestates.opens.ts); prosestates.tsx's
// judge() fails a state whose click did not open each one it lists, and the
// cover test (prose.states.test.tsx) fails an id no state lists.
//
// HOW AN OPENER IS FOUND AT RUN TIME. The test build compiles JSX with
// esbuild's jsxDev, so each fiber React 18 mounts carries `_debugSource`
// (the file and line of the JSX that drew it). An opener id's line is the
// line of its element's `<`; after the click the mounted tree is walked and
// an opener counts as opened when it is open now and was not before the
// click: a `<details>` whose `open` is set, a `role="dialog"`/`role="menu"`
// element that is mounted, or an overlay component (Dialog, Menu,
// TypedConfirm, ...) that renders an open dialog, menu or fold.

/** Every screen file under src, its source by its path from src (`components/Overlay.tsx`). */
export const SOURCES: Readonly<Record<string, string>> = Object.fromEntries(
  Object.entries(import.meta.glob<string>('../**/*.tsx', { query: '?raw', import: 'default', eager: true }))
    // The glob also answers this directory's own files as `./x.tsx`: tests, not screens.
    .filter(([path]) => path.startsWith('../') && !path.startsWith('../__tests__/'))
    .map(([path, raw]) => [path.slice('../'.length), raw] as const)
    .filter(([path]) => !/\.test\.tsx$/.test(path) && !/(^|\/)generated[/.]/.test(path)),
)

/** `line` cut at its `//` comment; a `//` after a colon or a quote (`https://`, `'//'`) is code. */
function lineCode(line: string): string {
  let at = line.indexOf('//')
  while (at !== -1) {
    const before = at === 0 ? ' ' : line.charAt(at - 1)
    if (!`:'"\`\\`.includes(before)) return line.slice(0, at)
    at = line.indexOf('//', at + 2)
  }
  return line
}

/** `src` with its comments blanked and every line kept where it was, so a match's line is the source's. */
export function blankComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '))
    .split('\n')
    .map(lineCode)
    .join('\n')
}

export interface Opener {
  /** `<file> <kind>#<n>`: what a state's `opens` lists. */
  id: string
  file: string
  /** `details`, `role=dialog`, `role=menu`, or the overlay component's name. */
  kind: string
  /** The 1-based line of the opener element's `<`: what React's `_debugSource.lineNumber` says. */
  line: number
}

/**
 * The openers `src` draws, in source order: each `<details>`, each
 * `role="dialog"` or `role="menu"` written as a JSX attribute (not inside a
 * selector string such as `'[role="dialog"]'`), and each overlay component
 * it renders but Tooltip, which opens on hover and focus and holds a tip, not
 * a state.
 */
export function scanOpeners(file: string, src: string, overlays: readonly string[]): Opener[] {
  const code = blankComments(src)
  const tags = overlays.filter((name) => name !== 'Tooltip').map((name) => `<${name}\\b`)
  const re = new RegExp(['<details\\b', '(?<=\\s)role="(?:dialog|menu)"', ...tags].join('|'), 'g')
  const seen = new Map<string, number>()
  const out: Opener[] = []
  for (const m of code.matchAll(re)) {
    const at = m.index ?? 0
    const kind = m[0].startsWith('role=') ? m[0].replace(/"/g, '') : m[0].startsWith('<details') ? 'details' : m[0].slice(1)
    // A role attribute belongs to the element whose `<` comes last before it.
    let start = at
    if (m[0].startsWith('role=')) for (const t of code.slice(0, at).matchAll(/<[A-Za-z]/g)) start = t.index ?? start
    const n = (seen.get(kind) ?? 0) + 1
    seen.set(kind, n)
    const label = kind.startsWith('role=') || kind === 'details' ? kind : `<${kind}>`
    out.push({ id: `${file} ${label}#${n}`, file, kind, line: code.slice(0, Math.max(start, 0)).split('\n').length })
  }
  return out
}

/** Every opener of every screen file, scanned with `overlays`. */
export function allOpeners(overlays: readonly string[], sources: Readonly<Record<string, string>> = SOURCES): Opener[] {
  return Object.keys(sources)
    .sort((a, b) => a.localeCompare(b))
    .flatMap((f) => scanOpeners(f, sources[f] ?? '', overlays))
}

// ---------------------------------------------------------------- run time

interface Fiber {
  child: Fiber | null
  sibling: Fiber | null
  stateNode: unknown
  _debugSource?: { fileName: string; lineNumber: number } | null
}

/** The current root fibers of every React root under the body. */
function roots(): Fiber[] {
  const out: Fiber[] = []
  for (const el of document.body.querySelectorAll<HTMLElement>('*')) {
    const key = Object.keys(el).find((k) => k.startsWith('__reactContainer$'))
    if (key === undefined) continue
    const host = (el as unknown as Record<string, { stateNode?: { current?: Fiber } }>)[key]
    const current = host?.stateNode?.current
    if (current !== undefined) out.push(current)
  }
  return out
}

/** Whether `el` is an open dialog, menu or fold. */
function openHost(el: Element): boolean {
  if (el instanceof HTMLDetailsElement) return el.open
  // A popover drawn hidden until its chip is pressed (voice/Roster.tsx) is not open.
  if (el.closest('[hidden]') !== null) return false
  const role = el.getAttribute('role')
  return role === 'dialog' || role === 'menu' || role === 'alertdialog' || el.classList.contains('c-menu')
}

/** The host elements `fiber` draws, itself included when it is one. */
function hosts(fiber: Fiber): Element[] {
  const out: Element[] = []
  const walk = (f: Fiber | null): void => {
    for (let at = f; at !== null; at = at.sibling) {
      if (at.stateNode instanceof Element) out.push(at.stateNode)
      walk(at.child)
    }
  }
  if (fiber.stateNode instanceof Element) out.push(fiber.stateNode)
  walk(fiber.child)
  return out
}

/** The src-relative path of a `_debugSource.fileName`, or null outside src. */
function fromSrc(fileName: string): string | null {
  const at = fileName.lastIndexOf('/src/')
  return at === -1 ? null : fileName.slice(at + '/src/'.length)
}

/** The ids of the openers open in the document now, read from React's mounted tree. */
export function openNow(overlays: readonly string[]): Set<string> {
  const byLine = new Map<string, Opener[]>()
  const ids = new Set<string>()
  const visit = (f: Fiber | null): void => {
    for (let at = f; at !== null; at = at.sibling) {
      const where = at._debugSource
      const file = where == null ? null : fromSrc(where.fileName)
      if (where != null && file !== null && SOURCES[file] !== undefined) {
        if (!byLine.has(file)) for (const o of scanOpeners(file, SOURCES[file] ?? '', overlays)) byLine.set(`${file}:${o.line}`, [...(byLine.get(`${file}:${o.line}`) ?? []), o])
        for (const o of byLine.get(`${file}:${where.lineNumber}`) ?? []) {
          const open =
            o.kind === 'details' || o.kind.startsWith('role=')
              ? at.stateNode instanceof Element && openHost(at.stateNode)
              : hosts(at).some(openHost)
          if (open) ids.add(o.id)
        }
        if (!byLine.has(file)) byLine.set(file, [])
      }
      visit(at.child)
    }
  }
  for (const root of roots()) visit(root)
  return ids
}
