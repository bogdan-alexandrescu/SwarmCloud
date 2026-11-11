// THE USER GUIDE AT /guide (Guide.tsx; source docs/user-guide/*.md).
//
// What this holds:
//   * the console's copy (guidePages.ts) is the Markdown, byte for byte, so a
//     page edited in docs/ and not synced fails here with the command to run;
//   * every console path the guide links still opens a page, and every `/sc`
//     verb and `/sc:` command it names still exists in the plugin -- a renamed
//     page or a dropped verb fails here, naming the guide page that says it;
//   * the pages render, link to each other in place, and search their titles;
//   * `/guide` and `/guide/<page>` are a route of their own, lit as Help.
//
// BREAK IT: rename `/config/github` in paths.ts, or drop the `inputs` row from
// plugin/skills/sc/SKILL.md, and "every console path" or "every /sc verb" fails.
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { serve } from './repofixture'
import { GUIDE_SOURCES } from '../guidePages'
import { GuideScreen, guideHref, guidePages, guideSlugFrom, parseMarkdown } from '../Guide'
import { pathToAddress } from '../paths'

const REPO = join(__dirname, '..', '..', '..', '..')
const GUIDE_DIR = join(REPO, 'docs', 'user-guide')
const SYNC = 'node apps/swarm-ui/guide-sync.mjs'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

function markdownFiles(): string[] {
  return readdirSync(GUIDE_DIR)
    .filter((n) => n.endsWith('.md'))
    .sort()
}

/** Every link target in every page, with the page it is on. */
function links(): { page: string; href: string }[] {
  const out: { page: string; href: string }[] = []
  for (const [page, md] of Object.entries(GUIDE_SOURCES)) {
    for (const m of md.matchAll(/\]\(([^)\s]+)\)/g)) out.push({ page, href: m[1] ?? '' })
  }
  return out
}

/** Every code span (and fenced line) that starts with a slash command. */
function commands(): { page: string; text: string }[] {
  const out: { page: string; text: string }[] = []
  for (const [page, md] of Object.entries(GUIDE_SOURCES)) {
    for (const m of md.matchAll(/`([^`\n]+)`/g)) out.push({ page, text: (m[1] ?? '').trim() })
    for (const line of md.split('\n')) if (/^\/\S/.test(line.trim())) out.push({ page, text: line.trim() })
  }
  return out.filter((c) => c.text.startsWith('/sc'))
}

/** The /sc verbs: the first word of each row of "## The verb"'s table, and the views it names. */
function scVerbs(): Set<string> {
  const skill = readFileSync(join(REPO, 'plugin', 'skills', 'sc', 'SKILL.md'), 'utf8')
  const start = skill.indexOf('## The verb')
  const table = skill.slice(start, skill.indexOf('\n## ', start + 1))
  const verbs = new Set<string>([''])
  for (const row of table.split('\n').filter((l) => l.startsWith('| ') && !l.startsWith('|--'))) {
    const cell = row.split('|')[1] ?? ''
    for (const m of cell.matchAll(/`([a-z-]+)/g)) verbs.add(m[1] ?? '')
  }
  return verbs
}

describe('the console carries the guide as written', () => {
  it('is the Markdown under docs/user-guide, byte for byte', () => {
    const files = markdownFiles()
    expect(files.length, 'docs/user-guide holds no page').toBeGreaterThan(5)
    expect(Object.keys(GUIDE_SOURCES).sort(), `guidePages.ts lists other pages than docs/user-guide: run ${SYNC}`).toEqual(
      files.map((n) => n.slice(0, -3)),
    )
    for (const n of files) {
      expect(GUIDE_SOURCES[n.slice(0, -3)], `${n} differs from guidePages.ts: run ${SYNC}`).toBe(readFileSync(join(GUIDE_DIR, n), 'utf8'))
    }
  })

  it('has an index that links every page, each page titled', () => {
    const pages = guidePages()
    expect(pages[0]?.slug).toBe('index')
    const index = GUIDE_SOURCES.index ?? ''
    for (const p of pages) {
      expect(p.title, `${p.slug}.md has no "# " title`).not.toBe('')
      if (p.slug !== 'index') expect(index, `index.md does not link ${p.slug}.md`).toContain(`](${p.slug}.md)`)
    }
  })

  it('links only pages that exist', () => {
    for (const { page, href } of links().filter((l) => /\.md(#|$)/.test(l.href))) {
      const slug = href.replace(/\.md(#.*)?$/, '')
      expect(GUIDE_SOURCES[slug], `${page}.md links ${href}, which is not a guide page`).toBeDefined()
    }
  })
})

describe('everything the guide names exists on main', () => {
  it('every console path the guide links opens a console page', () => {
    const paths = links().filter((l) => l.href.startsWith('/'))
    expect(paths.length, 'the guide links no console page: the scan read nothing').toBeGreaterThan(20)
    for (const { page, href } of paths) {
      const path = href.split('#')[0] ?? ''
      expect(pathToAddress(path), `${page}.md links ${href}, which no console page answers`).not.toBeNull()
    }
  })

  it('every /sc verb the guide names is a row of the sc skill’s verb table', () => {
    const verbs = scVerbs()
    expect(verbs.has('inputs') && verbs.has('run') && verbs.has('check'), 'the verb table was not read').toBe(true)
    const named = commands().filter((c) => /^\/sc(\s|$)/.test(c.text))
    expect(named.length, 'the guide names no /sc verb: the scan read nothing').toBeGreaterThan(10)
    for (const { page, text } of named) {
      const verb = text.split(/\s+/)[1] ?? ''
      expect(verbs.has(verb), `${page}.md names \`${text}\`, and /sc has no verb ${verb}`).toBe(true)
    }
  })

  it('every /sc: command the guide names is a plugin command or its workflow', () => {
    const named = commands().filter((c) => c.text.startsWith('/sc:'))
    expect(named.length).toBeGreaterThan(2)
    const workflow = readFileSync(join(REPO, 'plugin', 'workflows', 'run.js'), 'utf8')
    for (const { page, text } of named) {
      const name = /^\/sc:([a-z-]+)/.exec(text)?.[1] ?? ''
      const known = existsSync(join(REPO, 'plugin', 'commands', `${name}.md`)) || workflow.includes(`name: '${name}'`)
      expect(known, `${page}.md names ${text}, which the plugin does not have`).toBe(true)
    }
  })

  it('every sc CLI command the guide names is one sc has', () => {
    const cli = readFileSync(join(REPO, 'apps', 'swarm-mcp', 'swarm_mcp', 'sc.py'), 'utf8')
    const named = Object.entries(GUIDE_SOURCES).flatMap(([page, md]) =>
      Array.from(md.matchAll(/uv run sc ([a-z-]+)/g), (m) => ({ page, cmd: m[1] ?? '' })),
    )
    expect(named.length).toBeGreaterThan(0)
    for (const { page, cmd } of named) {
      expect(new RegExp(`add_parser\\(\\s*"${cmd}"`).test(cli), `${page}.md names \`sc ${cmd}\`, which sc.py does not define`).toBe(true)
    }
  })

  it('names no real person, address or domain: only example ones', () => {
    for (const [page, md] of Object.entries(GUIDE_SOURCES)) {
      for (const m of md.matchAll(/[\w.+-]+@([\w-]+(?:\.[\w-]+)+)/g)) {
        expect(m[1], `${page}.md: ${m[0]}`).toMatch(/(^|\.)example\.com$/)
      }
      for (const m of md.matchAll(/https?:\/\/([^/\s)`"]+)(\/[^\s)`"]*)?/g)) {
        const host = m[1] ?? ''
        const ok = /(^|\.)example\.com$/.test(host) || (host === 'github.com' && (m[2] ?? '').startsWith('/example-org/'))
        expect(ok, `${page}.md: ${m[0]}`).toBe(true)
      }
    }
  })
})

describe('the reader', () => {
  it('reads headings, nested numbered steps, tables and fenced code', () => {
    const blocks = parseMarkdown(
      ['# Title', '', '1. One', '   You should see it.', '2. Two:', '   1. inner', '', '| A | B |', '|---|---|', '| a | b |', '', '```json', '{"x": 1}', '```'].join(
        '\n',
      ),
    )
    expect(blocks.map((b) => b.kind)).toEqual(['heading', 'list', 'table', 'code'])
    const list = blocks[1]
    expect(list?.kind === 'list' && list.items.length).toBe(2)
    expect(list?.kind === 'list' && list.items[1]?.map((b) => b.kind)).toEqual(['para', 'list'])
    expect(blocks[3]).toEqual({ kind: 'code', text: '{"x": 1}' })
  })

  it('sends a sibling page to its /guide path and leaves a console path as written', () => {
    expect(guideHref('quick-start.md')).toBe('/guide/quick-start')
    expect(guideHref('index.md#everyday-work')).toBe('/guide#everyday-work')
    expect(guideHref('/config/github')).toBe('/config/github')
    expect(guideSlugFrom('/guide')).toBe('')
    expect(guideSlugFrom('/guide/quick-start')).toBe('quick-start')
    expect(guideSlugFrom('/guides')).toBeNull()
  })
})

describe('/guide in the console', () => {
  it('draws the index, opens a page in place, and searches page titles', () => {
    window.history.replaceState(null, '', '/guide')
    const popped = vi.fn()
    window.addEventListener('popstate', popped)
    render(<GuideScreen slug="" />)
    expect(screen.getByRole('heading', { level: 1, name: 'SwarmCloud user guide' })).toBeTruthy()
    fireEvent.click(screen.getByRole('link', { name: 'From nothing to a running workflow in 15 minutes' }))
    window.removeEventListener('popstate', popped)
    expect(window.location.pathname).toBe('/guide/quick-start')
    expect(popped).toHaveBeenCalledTimes(1)

    fireEvent.change(screen.getByRole('searchbox', { name: "Search the guide's page titles" }), { target: { value: 'trouble' } })
    const found = screen.getByRole('region', { name: 'Pages found' })
    expect(Array.from(found.querySelectorAll('a'), (a) => a.textContent)).toEqual(['Troubleshooting'])
  })

  it('says so for a page it does not have', () => {
    render(<GuideScreen slug="no-such-page" />)
    expect(screen.getByRole('heading', { name: 'No guide page is called no-such-page' })).toBeTruthy()
  })

  it('is a route of its own, written back as its own path', async () => {
    window.history.replaceState(null, '', '/guide/quick-start')
    const { fromLocation, canonical } = await import('../App')
    const r = fromLocation()
    expect(r).toMatchObject({ sectionId: 'guide', tab: 'quick-start', taskId: null })
    expect(canonical(r)).toBe('guide')
  })

  it('opens inside the console at /guide, with Help lit', async () => {
    serve(() => null)
    window.history.replaceState(null, '', '/guide/troubleshooting')
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { App } = await import('../App')
    render(<App />)
    await screen.findByRole('heading', { level: 1, name: 'Troubleshooting' }, { timeout: 5000 })
    // Help, the guide's short form, is lit; no Help group is marked as the page shown.
    const help = Array.from(document.querySelectorAll('.ctl-nav-util a.sk-ri')).find((a) => a.textContent?.trim() === 'Help')
    expect(help?.className).toContain('is-on')
    expect(document.querySelectorAll('.sk-pk[aria-current="page"]')).toHaveLength(0)
    expect(window.location.pathname).toBe('/guide/troubleshooting')
  })
})
