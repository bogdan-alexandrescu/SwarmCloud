// Console-source checks moved out of two Python files (part of #72):
// tests/unit/control_plane/test_runtimes_screen.py and
// tests/unit/control_plane/test_account_listing_is_honest.py. Each compares
// console source with nothing in Python, so it belongs here; issue 73 owns
// reviewing them. Every `it` is named after the Python test it replaces so a
// reviewer can pair them, and every check fails when the slice it reads is
// missing or empty (a check that reads nothing must not pass).
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'
import { SECTIONS } from '../App'
import catalogue from '../generated/constants/runtimes.json'

const SRC = join(__dirname, '..')
const ROUTE = '/v1/runtimes'

function src(name: string): string {
  const text = readFileSync(join(SRC, name), 'utf8')
  expect(text.trim().length, `${name} is empty; this test would check nothing`).toBeGreaterThan(0)
  return text
}

const API = src('api.ts')
const APP = src('App.tsx')

/** The exported loader in api.ts whose body performs `path`; found, not named. */
function loaderReading(path: string): string {
  const hits: string[] = []
  for (const chunk of API.split('\nexport ')) {
    if (!chunk.includes(`'${path}'`)) continue
    const m = /^async function (\w+)/.exec(chunk)
    if (m?.[1]) hits.push(m[1])
  }
  expect(
    hits.length,
    `expected exactly one exported loader in api.ts to read ${path}, found ${JSON.stringify(hits)}. ` +
      'Two readers of one route drift; none means nothing renders it.',
  ).toBe(1)
  return hits[0] as string
}

/** Every top-level *.tsx in src that mentions the loader, other than App.tsx. */
function screensReading(loader: string): Array<[string, string]> {
  const files = readdirSync(SRC).filter((f) => f.endsWith('.tsx') && f !== 'App.tsx').sort((a, b) => a.localeCompare(b))
  expect(files.length, 'no *.tsx files found in src; this check would read nothing').toBeGreaterThan(0)
  return files.map((f): [string, string] => [f, src(f)]).filter(([, text]) => text.includes(loader))
}

function screenFile(): [string, string] {
  const hits = screensReading(loaderReading(ROUTE))
  expect(hits.length, `expected one screen reading ${ROUTE}, found ${JSON.stringify(hits.map((h) => h[0]))}`).toBe(1)
  return hits[0] as [string, string]
}

/** One top-level component's source, on its own, with a vacuity guard. */
function component(source: string, name: string): string {
  const marker = `function ${name}(`
  expect(
    source.includes(marker),
    `${marker} is not in the screen. If it was renamed, rename it here too -- a check that cannot find its subject must fail, not pass.`,
  ).toBe(true)
  const start = source.indexOf(marker)
  const end = source.indexOf('\n}\n', start)
  expect(end, `${name} has no closing brace at column 0`).toBeGreaterThan(start)
  const body = source.slice(start, end)
  expect(body.length, `${name} was located and is too small to be it`).toBeGreaterThan(200)
  return body
}

/** The body of a top-level function in `source`, from its marker to the next closing brace at column 0. */
function functionBody(source: string, marker: string): string {
  expect(source.includes(marker), `${marker} is not in the source; this check would read nothing`).toBe(true)
  const start = source.indexOf(marker)
  const end = source.indexOf('\n}\n', start)
  expect(end, `${marker} has no closing brace at column 0`).toBeGreaterThan(start)
  const body = source.slice(start, end)
  expect(body.trim().length, `${marker} was located and is empty`).toBeGreaterThan(0)
  return body
}

/** Top-level field names of one exported TS interface, in order. */
function interfaceFields(source: string, name: string, file: string): string[] {
  const marker = `export interface ${name} {`
  expect(source.includes(marker), `${marker} is not in ${file}; this check is vacuous`).toBe(true)
  const body = source.slice(source.indexOf(marker))
  const end = body.indexOf('\n}\n')
  expect(end, `interface ${name} in ${file} has no closing brace`).toBeGreaterThan(0)
  const fields = Array.from(body.slice(0, end).matchAll(/^ {2}(\w+)\??:/gm), (m) => m[1] as string)
  expect(fields.length, `no fields parsed out of interface ${name}; the check is vacuous`).toBeGreaterThan(0)
  return fields
}

/** The source of the development runtimes fixture, on its own. */
function fixtureBlock(): string {
  const marker = 'const FIXTURE_RUNTIMES'
  expect(API.includes(marker), `${marker} is not in api.ts; this check would read nothing`).toBe(true)
  const start = API.indexOf(marker)
  const end = API.indexOf('\n}\n', start)
  expect(end, 'the fixture block has no closing brace at column 0').toBeGreaterThan(start)
  const block = API.slice(start, end)
  expect(block.length, 'the fixture block was located and is too small to be it').toBeGreaterThan(400)
  return block
}

describe('runtimes screen (was tests/unit/control_plane/test_runtimes_screen.py)', () => {
  it('test_the_runtimes_route_is_read_by_a_screen_the_router_reaches', () => {
    const loader = loaderReading(ROUTE)
    const screens = screensReading(loader)
    expect(screens.length, `api.ts exports ${loader}() and no screen imports it. ${ROUTE} would be read by nothing.`).toBeGreaterThan(0)

    const exported: string[] = []
    for (const [, text] of screens) {
      for (const m of text.matchAll(/export function (\w+Screen)\b/g)) exported.push(m[1] as string)
    }
    expect(exported.length, 'the screen reading this route exports no *Screen component').toBeGreaterThan(0)

    const rendered = exported.filter((n) => APP.includes(`<${n} `) || APP.includes(`<${n}/>`) || APP.includes(`<${n} />`))
    expect(
      rendered.length,
      `App.tsx renders none of ${JSON.stringify(exported)}. The screen exists and no address reaches it, which is the state this route was already in.`,
    ).toBeGreaterThan(0)
    for (const name of rendered) {
      // Static import, or React.lazy: `const X = lazy(() => import('./Mod').then((m) => ({ default: m.X })))`.
      const lazily = new RegExp(
        `const ${name} = lazy\\(\\(\\) => import\\('\\./\\w+'\\)\\.then\\(\\(m\\) => \\(\\{ default: m\\.${name} \\}\\)\\)\\)`,
      ).test(APP)
      expect(APP.includes(`import { ${name} }`) || lazily, `${name} is rendered in App.tsx and not imported`).toBe(true)
    }
  })

  it('test_the_screen_is_reachable_by_a_hash_that_resolves_to_it', () => {
    const loader = loaderReading(ROUTE)
    const names = screensReading(loader).flatMap(([, text]) =>
      Array.from(text.matchAll(/export function (\w+Screen)\b/g), (m) => m[1]),
    )
    expect(names.length, 'no *Screen component found in the screen reading this route').toBeGreaterThan(0)
    const screen = names[0]

    const found = new RegExp(`case '([\\w-]+)/([\\w-]+)':\\s*\\n\\s*return <${screen} ?/>`).exec(APP)
    expect(found, `App.tsx has no \`case '<section>/<tab>'\` returning <${screen} />`).not.toBeNull()
    const [, sectionId, tabId] = found as RegExpExecArray

    // SECTIONS is App.tsx's own export: the value the nav is built from, not a re-parse of its text.
    expect(SECTIONS.length, 'SECTIONS is empty; this check would read nothing').toBeGreaterThan(0)
    const section = SECTIONS.find((s) => s.id === sectionId)
    expect(
      section,
      `SectionBody routes '${sectionId}/${tabId}' and SECTIONS declares no section with id '${sectionId}'. Nothing in the nav leads there. Declared: ${JSON.stringify(SECTIONS.map((s) => s.id).sort((a, b) => a.localeCompare(b)))}`,
    ).toBeDefined()
    const tabs = (section as (typeof SECTIONS)[number]).tabs.map((t) => t.id)
    expect(
      tabs.includes(tabId as string),
      `SECTIONS declares section '${sectionId}' and no tab '${tabId}' in it (it has ${JSON.stringify(tabs)}); the section would open on a pane that renders the 'No such pane' state.`,
    ).toBe(true)
  })

  it('test_the_screen_reads_every_field_the_route_publishes', () => {
    // The field list comes from the GENERATED file, not the hand-written types.ts.
    const [file, text] = screenFile()
    const fields = interfaceFields(src('generated/api-types.ts'), 'Runtime', 'generated/api-types.ts')
    const unread = fields.filter((f) => !new RegExp(`\\.${f}\\b`).test(text))
    expect(
      unread,
      `${file} never reads ${JSON.stringify(unread)}, which /v1/runtimes publishes. A field served and not shown is the route's gap moved one layer up.`,
    ).toEqual([])
  })

  it('test_the_screen_never_coalesces_a_failed_read_to_a_number', () => {
    const [file, text] = screenFile()
    const lines = text.split('\n')
    expect(lines.length, `${file} is empty; this check would read nothing`).toBeGreaterThan(1)
    const offenders = lines
      .map((line, i) => ({ n: i + 1, line }))
      .filter(({ line }) => /(\?\?|\|\|)\s*0\b/.test(line))
      .map(({ n, line }) => `${n}: ${line.trim()}`)
    expect(offenders, `${file} coalesces a possibly-absent value to 0`).toEqual([])
  })

  it('test_interchangeable_credentials_are_distinguished_where_they_are_rendered', () => {
    const [, text] = screenFile()
    const credential = component(text, 'Credential')
    expect(
      credential.includes('secrets_any_of'),
      'the credential panel does not branch on secrets_any_of, so a runtime that takes any ONE of its credentials is rendered as needing all of them',
    ).toBe(true)
  })

  it('test_the_always_present_glyph_explains_the_card_and_the_footer_keeps_the_route', () => {
    const [, text] = screenFile()
    const topology = component(text, 'Topology')
    const glyphs = Array.from(topology.matchAll(/<HelpCard topic="([a-z0-9-]+)"/g), (m) => m[1])
    expect(
      glyphs,
      "the catalogue's always-present `?` must open the topic that explains `Sets it apart` and `disabled`",
    ).toEqual(['what-sets-it-apart-is-arithmetic'])
    const footer = /RUNTIME_TOPICS: readonly TopicId\[\] = \[([^\]]*)\]/.exec(text)
    expect(footer, 'the footer index was not found').not.toBeNull()
    expect(
      ((footer as RegExpExecArray)[1] ?? '').includes("'catalogue-from-route'"),
      'catalogue-from-route left the footer when the glyph moved off it',
    ).toBe(true)
    const card = component(text, 'RuntimeCard')
    const from = card.indexOf('className="ctl-card-head"')
    const to = card.indexOf('className="ctl-card-body"')
    expect(from, 'the card head marker was not found').toBeGreaterThanOrEqual(0)
    expect(to, 'the card body marker was not found').toBeGreaterThan(from)
    const head = card.slice(from, to)
    expect(
      head.includes('resolved_backend'),
      'the card head still prints the resolved backend beside the name; the `runs on` row is where it is read',
    ).toBe(false)
    expect(text.includes('href="#capacity/pools"'), '`Pools` in the Backends caption is not a link').toBe(true)
  })

  // The frozen catalogue's names come from the GENERATED constants
  // (./generated/constants/runtimes.json, read from swarm_common.profiles and
  // drift-checked), so these two compare console source with the catalogue
  // without reading any Python.
  const wordIn = (text: string, name: string) =>
    new RegExp(`\\b${name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`).test(text)

  it('test_the_screen_restates_no_value_from_the_frozen_catalogue', () => {
    const frozen = [...catalogue.runner_profiles, ...catalogue.resource_classes, ...catalogue.backends]
    expect(frozen.length, 'the generated catalogue is empty; this check is vacuous').toBeGreaterThan(0)
    const [path, text] = screenFile()
    const named = frozen.filter((n) => wordIn(text, n)).sort((a, b) => a.localeCompare(b))
    expect(named, `${path} names frozen catalogue entries; every value must come from the response`).toEqual([])
    const built = [...new Set(Array.from(text.matchAll(/\b(cpu|memory_gib|disk_gib|units)\s*:\s*[\d.]/g), (m) => m[1]))]
    expect(built, `${path} constructs a sizing record; sizes are served, never assembled here`).toEqual([])
  })

  it('test_the_development_fixture_is_not_a_copy_of_the_frozen_catalogue', () => {
    const block = fixtureBlock()
    const named = [...catalogue.runner_profiles, ...catalogue.resource_classes].filter((n) => wordIn(block, n)).sort((a, b) => a.localeCompare(b))
    expect(named, 'the runtimes fixture names real catalogue entries, a copy of the frozen sizes in TypeScript').toEqual([])
  })

  it('test_the_fixture_exercises_the_branches_live_data_never_would (api.ts half)', () => {
    const block = fixtureBlock()
    const declared = Array.from(
      block.matchAll(/backend: '([^']+)', resolved_backend: '([^']+)'/g),
      (m) => [m[1], m[2]] as const,
    )
    expect(declared.length, 'no backend pairs parsed out of the fixture; this check is vacuous').toBeGreaterThan(0)
    expect(
      declared.some(([d, r]) => d !== r),
      'no fixture runtime declares one backend and resolves to another, so the declared/resolved split is drawn by nothing in development',
    ).toBe(true)
    expect(
      new Set(declared.map(([, r]) => r)).size,
      'every fixture runtime resolves to the same backend, so the topology panel never groups in development',
    ).toBeGreaterThan(1)
    expect(
      block.includes('provider: null'),
      "no fixture runtime is provider-free, so the 'none needed' credential branch is never seen",
    ).toBe(true)
    expect(
      block.includes('secrets_any_of: true') && block.includes('secrets_any_of: false'),
      "the fixture does not cover both credential shapes; 'any one of' versus 'all of' is the distinction this panel exists to make",
    ).toBe(true)
  })
})

describe('account listing (was tests/unit/control_plane/test_account_listing_is_honest.py, section 4)', () => {
  it('test_the_accounts_screen_marks_a_row_the_pool_is_skipping', () => {
    const text = src('Accounts.tsx')
    expect(text.includes('unreadableFor('), 'the row must ask whether the pool is skipping it').toBe(true)
    expect(text.includes('neverAssigned('), 'never assigned is not recently assigned').toBe(true)
    expect(text.includes('unreadable_document_count'), 'the shortfall has to reach the reader').toBe(true)
  })

  it('test_the_overview_headroom_excludes_an_account_the_pool_is_skipping', () => {
    const body = functionBody(src('Overview.tsx'), 'function accountHeadroom(')
    expect(body.includes('unreadableFor(')).toBe(true)
  })

  it('test_the_dev_fixture_carries_what_the_route_serves', () => {
    expect(API.includes('unreadable_now:')).toBe(true)
    expect(API.includes('unreadable_document_count:')).toBe(true)
    expect(API.includes('last_assigned_at:')).toBe(true)
  })
})
