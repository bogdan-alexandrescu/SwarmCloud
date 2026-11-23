// EACH ROUTE'S SCREEN LOADS WITH ITS ROUTE (issue 195, after issue 79).
//
// A screen declared lazy in App.tsx still lands in the main chunk when an
// eager module imports it statically for a helper (`guidePath`,
// `useRecentName`, `needsALook` ...): Vite keeps the whole module where the
// static import is and the lazy() splits nothing. So the helpers live in
// helper-only modules (guidePaths.ts, workspacePaths.ts ...) and this reads
// the shell's import lines to keep it that way.
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..')

function source(file: string): string {
  return readFileSync(join(SRC, file), 'utf8')
}

/** The modules a file imports statically, `import type` lines left out: they leave nothing in the bundle. */
function staticImports(file: string): string[] {
  const out: string[] = []
  for (const m of source(file).matchAll(/^\s*(import|export)\s+(?!type\b)(?:[^'";]*?\sfrom\s+)?['"]([^'"]+)['"]/gm)) out.push(m[2])
  return out
}

/** Every screen App.tsx loads with its route, and the module that holds it. */
const LAZY_SCREENS: readonly [string, string][] = [
  ['WorkspaceSettingsScreen', './WorkspaceSettings'],
  ['ConfigPluginsScreen', './ConfigPlugins'],
  ['AdminPluginsScreen', './AdminPlugins'],
  ['HelpScreen', './HelpSection'],
  ['WorkspacePage', './WorkspacePage'],
  ['JoinWorkspaceScreen', './JoinWorkspace'],
  ['GuideScreen', './Guide'],
  ['LlmProvidersScreen', './LlmProviders'],
  ['SchedulesScreen', './Schedules'],
  ['WorkflowsScreen', './Workflows'],
  ['CapacityScreen', './Capacity'],
  ['HomeScreen', './Home'],
  ['ApprovalsScreen', './Approvals'],
  ['ProfileScreen', './Profile'],
  ['AdminSettingsScreen', './AdminSettings'],
  ['VoiceTalkScreen', './voice/Section'],
]

/** The shell: what every route loads before its screen. */
const SHELL = ['App.tsx', 'Spine.tsx', 'Shell.tsx']

describe('the shell imports no lazy screen', () => {
  for (const file of SHELL) {
    it(`${file} imports none of the lazy screens' modules statically`, () => {
      const screens = new Set(LAZY_SCREENS.map(([, m]) => m))
      expect(staticImports(file).filter((m) => screens.has(m))).toEqual([])
    })
  }

  it("App.tsx takes GitHubConnect only for the callback, which must mount before the router rewrites the query", () => {
    const line = /^import\s+\{([^}]*)\}\s+from\s+'\.\/GitHubConnect'/m.exec(source('App.tsx'))
    expect(line?.[1].trim()).toBe('GitHubCallbackScreen')
  })

  it('Schedules imports neither Approvals nor Profile, both lazy screens', () => {
    expect(staticImports('Schedules.tsx').filter((m) => m === './Approvals' || m === './Profile')).toEqual([])
  })
})

describe('each screen is declared with lazy()', () => {
  for (const [name, mod] of LAZY_SCREENS) {
    it(`${name} from ${mod}`, () => {
      const decl = `const ${name} = lazy(() => import('${mod}').then((m) => ({ default: m.${name} })))`
      expect(source('App.tsx')).toContain(decl)
    })
  }
})
