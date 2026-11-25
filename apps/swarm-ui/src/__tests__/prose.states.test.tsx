// The states guard's own rules (prosestates.tsx), on hand-built states, and
// THE COVER TEST: every screen file that draws an opener is named by a state,
// or by issue 81's state tests; there is no pending allowance.

import { useState, type ReactElement } from 'react'
import { fireEvent } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import * as Overlay from '../components/Overlay'
import { InfoTip } from '../InfoTip'
import { visitMount, type ProseState, type Theme } from './prosestates'
import { ADMIN } from './prosestates.admin'
import { AGENT } from './prosestates.agent'
import { CONFIG } from './prosestates.config'
import { OVERVIEW } from './prosestates.overview'
import { SHELL } from './prosestates.shell'
import { WORK } from './prosestates.work'
import { WORKFLOWS } from './prosestates.workflows'

const REGISTRY: readonly ProseState[] = [...SHELL, ...AGENT, ...WORKFLOWS, ...WORK, ...OVERVIEW, ...CONFIG, ...ADMIN]

/**
 * The screen files issue 81's state tests (prose81.*.test.tsx,
 * accounts.prose81.test.tsx) already open and hold to the rule: they are
 * covered there, not here.
 */
const NAMED_BY_81: readonly string[] = [
  'Accounts.tsx',
  'AccountSheet.tsx',
  'AccountsTable.tsx',
  'AccountsVerdict.tsx',
  'Secrets.tsx',
  'SecretsActions.tsx',
  'McpServers.tsx',
  'QaTargets.tsx',
  'Repositories.tsx',
  'RepositoriesParts.tsx',
  'IssueSubmit.tsx',
  'JoinWorkspace.tsx',
  'PlatformCounts.tsx',
  'WorkspaceRefusal.tsx',
]

// ---------------------------------------------------------------- the rules

const LONG = 'This panel lists every read the page made and how long each one took to come back.'
const LINE8 = 'Writes to the live cluster in this workspace'
const LINE9 = 'Writes to the live cluster in this workspace right now'

function Shown({ children }: Readonly<{ children: ReactElement }>): ReactElement {
  return <div className="ps-box">{children}</div>
}

/** A box that opens on its button's click, with `body` in it. */
function Opens({ body }: Readonly<{ body: () => ReactElement }>): ReactElement {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="ps-open" onClick={() => setOpen(true)}>
        Open
      </button>
      {open && <Shown>{body()}</Shown>}
    </>
  )
}

function fixture(over: Partial<ProseState> & { body: () => ReactElement; act?: () => void }): ProseState {
  const { body, act, ...rest } = over
  return {
    key: 'fixture/box',
    group: 'fixture',
    files: [],
    shows: '.ps-box',
    ...rest,
    mount: () => <Opens body={body} />,
    act: act ?? (() => fireEvent.click(document.querySelector('.ps-open') as HTMLElement)),
  } as ProseState
}

/** The theme the document is drawn in now, as a screen that reads it would. */
const themeNow = (): string | null => document.documentElement.getAttribute('data-theme')

describe('the states guard holds a state to the rule', () => {
  beforeAll(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
  })
  const both = async (s: ProseState): Promise<void> => {
    for (const theme of ['light', 'dark'] as const satisfies readonly Theme[]) await visitMount(s, theme)
  }

  it('fails a state with a long run, and the message quotes the run', async () => {
    const s = fixture({ body: () => <p>{LONG}</p> })
    await expect(both(s)).rejects.toThrow(LONG)
  })

  it('fails a state whose click opened nothing', async () => {
    const s = fixture({ body: () => <p>Pool a full</p>, act: () => undefined })
    await expect(both(s)).rejects.toThrow(/the click opened nothing/)
  })

  it('fails a state already drawn before its click', async () => {
    const s = fixture({ body: () => <p>Pool a full now</p>, shows: '.ps-open' })
    await expect(both(s)).rejects.toThrow(/before the click/)
  })

  it('fails a sentence visible in only one theme', async () => {
    const s = fixture({ body: () => <p>{themeNow() === 'dark' ? LONG : 'Pool a is full'}</p> })
    await expect(visitMount(s, 'light')).resolves.toBeDefined()
    await expect(both(s)).rejects.toThrow(/\(dark\) shows 1 long runs/)
  })

  it('passes a long run that is the state data', async () => {
    const s = fixture({ body: () => <pre>{LONG}</pre>, data: ['how long each one took'] })
    await expect(both(s)).resolves.toBeUndefined()
  })

  it('passes a must-stay line of 8 words and fails one of 9', async () => {
    await expect(both(fixture({ body: () => <p>{LINE8}</p>, lines: [LINE8] }))).resolves.toBeUndefined()
    await expect(both(fixture({ body: () => <p>{LINE9}</p>, lines: [LINE9] }))).rejects.toThrow(LINE9)
  })

  it('holds a moved sentence behind its (i), and fails it on the surface', async () => {
    const tipped = (): ReactElement => (
      <p>
        Reads this tab made <InfoTip about="reads" tip={LONG} />
      </p>
    )
    await expect(both(fixture({ body: tipped, moved: [LONG], lines: ['Reads this tab made'] }))).resolves.toBeUndefined()
    const shortOnSurface = 'Every read is listed here.'
    await expect(both(fixture({ body: () => <p>{shortOnSurface} Pool a</p>, moved: [shortOnSurface] }))).rejects.toThrow(/is on the surface/)
  })

  it('fails an arrowed text link', async () => {
    const s = fixture({ body: () => <a href="/reads">Every read, in a table →</a> })
    await expect(both(s)).rejects.toThrow(/text link with an arrow/)
  })
})

// ---------------------------------------------------------------- the cover

/** Every screen file under src, its source by its path from src (`components/Overlay.tsx`). */
const SOURCES: Readonly<Record<string, string>> = Object.fromEntries(
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

/** `src` with its block, JSX and line comments taken out: a comment that mentions `aria-expanded` draws nothing. */
export function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .map(lineCode)
    .join('\n')
}

/** The overlay components components/Overlay.tsx exports: what its module says, not a list kept here. */
const OVERLAYS: readonly string[] = Object.entries(Overlay)
  .filter(([name, v]) => /^[A-Z]/.test(name) && typeof v === 'function')
  .map(([name]) => name)

/** Whether `src` draws something a click opens. */
export function drawsOpener(src: string, overlays: readonly string[]): boolean {
  const code = stripComments(src)
  if (/role="dialog"|role="menu"|aria-expanded|<details\b/.test(code)) return true
  return overlays.some((name) => new RegExp(`<${name}\\b`).test(code))
}

/** The opener files nothing accounts for: no state names them and issue 81 did not name them. */
export function uncovered(openers: readonly string[], states: readonly ProseState[], named81: readonly string[]): string[] {
  const named = new Set(states.flatMap((s) => s.files))
  return openers.filter((f) => !named.has(f) && !named81.includes(f))
}

describe('the cover: every screen file that draws an opener is visited', () => {
  const openers = Object.keys(SOURCES)
    .filter((f) => drawsOpener(SOURCES[f] ?? '', OVERLAYS))
    .sort((a, b) => a.localeCompare(b))
  const named = new Set(REGISTRY.flatMap((s) => s.files))

  it('reads the tree and finds the overlays', () => {
    expect(Object.keys(SOURCES).length, 'the glob read no screen files').toBeGreaterThan(100)
    expect(OVERLAYS).toEqual(expect.arrayContaining(['Dialog', 'Menu', 'TypedConfirm']))
    expect(openers).toEqual(expect.arrayContaining(['App.tsx', 'Spine.tsx', 'Notifications.tsx', 'Dock.tsx', 'components/Overlay.tsx']))
    const covered = openers.filter((f) => named.has(f))
    const by81 = openers.filter((f) => NAMED_BY_81.includes(f))
    console.log(
      `PROSE STATES COVER: ${openers.length} files draw an opener; ${covered.length} named by a state, ` +
        `${by81.length} named by issue 81; ${REGISTRY.length} states`,
    )
  })

  it('names every opener file by a state or by issue 81', () => {
    const left = uncovered(openers, REGISTRY, NAMED_BY_81)
    expect(left, `register a state for each (prosestates.<group>.tsx):\n${left.join('\n')}`).toEqual([])
  })

  it('fails a file when the one state that names it is taken out', () => {
    const bell = REGISTRY.filter((s) => !s.files.includes('Notifications.tsx'))
    expect(uncovered(openers, bell, NAMED_BY_81)).toEqual(['Notifications.tsx'])
  })

  it('has no pending allowance: a new opener file no state names fails by name', () => {
    const extra: Readonly<Record<string, string>> = { ...SOURCES, 'NewSheet.tsx': '<Dialog title="x" onClose={() => undefined}>y</Dialog>' }
    const all = Object.keys(extra).filter((f) => drawsOpener(extra[f] ?? '', OVERLAYS))
    expect(uncovered(all, REGISTRY, NAMED_BY_81)).toEqual(['NewSheet.tsx'])
  })

  it("names only files that exist", () => {
    const missing = [...named].filter((f) => SOURCES[f] === undefined)
    expect(missing, 'a state names a screen file that is not under src').toEqual([])
    const lost81 = NAMED_BY_81.filter((f) => SOURCES[f] === undefined)
    expect(lost81, 'NAMED_BY_81 names a file that is gone').toEqual([])
  })
})
