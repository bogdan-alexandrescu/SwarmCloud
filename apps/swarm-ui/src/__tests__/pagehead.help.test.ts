// EVERY PAGE HEAD HAS A `?`, OR SAYS WHY IT DOES NOT (lane QAFIX-PAGEHEAD;
// visual QA finding QA-066, part of #1680).
//
// `PageHead` draws its `help` topic's `?` after the title, and with none it
// draws the `?` its section lends it (`SectionHelp`). A Settings page belongs
// to no section, so a Settings page head with no `help=` drew no `?` at all:
// four of the five pages QA-066 found were exactly that.
//
// This walks every `<PageHead` written in src/ and holds each one to having a
// `help=` prop, or to an entry on the allowlist below, which says why. A new
// page head with neither fails here, and so does an allowlist entry whose head
// is gone or has gained a topic (the list cannot rot into covering nothing).
//
// MUTATION: drop `help="mcp-servers"` from McpServers.tsx's PageHead, or add a
// `<PageHead title="New page" />` anywhere in src/ without a topic.

import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = join(__dirname, '..')

/**
 * The heads that draw no topic of their own, keyed `<file> <title as written>`.
 *
 * A SECTION PAGE: `PageHead` draws its section's question (`SectionHelp`) in
 * the `?` slot when it is given no `help`, so the page has its `?` already.
 */
const SECTION_PAGE = 'a section page: PageHead draws its section’s question (SectionHelp)'
const ALLOWED: Readonly<Record<string, string>> = {
  'Access.tsx "Access"': SECTION_PAGE,
  'Activity.tsx "Timeline"': SECTION_PAGE,
  'App.tsx {REFERENCE_LABEL}': SECTION_PAGE,
  'GitTokens.tsx "Git tokens"': SECTION_PAGE,
  'GitTokens.tsx "Permissions"': SECTION_PAGE,
  'Onboarding.tsx "Setup"': SECTION_PAGE,
  'Overview.tsx "Overview"': SECTION_PAGE,
  'People.tsx "People"': SECTION_PAGE,
  'PlatformCounts.tsx "Platform counts"': SECTION_PAGE,
  'Repositories.tsx "Repositories"': SECTION_PAGE,
  'RepositoriesDetail.tsx {title}': SECTION_PAGE,
  'RepositoriesDetail.tsx {repoName(r)}': SECTION_PAGE,
  'RepositoriesRegister.tsx "Register repository"': SECTION_PAGE,
  'Schedules.tsx "Schedules"': SECTION_PAGE,
  'TimelineLanes.tsx "Timeline"': SECTION_PAGE,
  'WorkspacesYouAdmin.tsx "Workspaces you admin"': SECTION_PAGE,
  // Submit is no section, but App.tsx lends it `SUBMIT_HELP`'s question.
  'SubmitChooser.tsx "Submit"': 'Submit’s question, lent by App.tsx (SUBMIT_HELP)',
  // The Help page is the place every topic lives; a `?` on it would open itself.
  'HelpSection.tsx {q === \'\' ? group.title : \'Help\'}': 'the Help page itself',
  // SETTINGS PAGES WITH NO TOPIC YET. QA-066 named five pages, and these two
  // already draw their refresh in the head; their `?` is the open half of the
  // finding, said in the PR rather than invented here.
  'ProviderKeys.tsx "API keys"': 'a Settings page with no topic yet (outside QA-066’s five)',
  'Profile.tsx "Profile"': 'a Settings page with no topic yet (outside QA-066’s five)',
}

/** Comments out, so a `PageHead` named in prose is not a head. */
function code(text: string): string {
  return text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '')
}

/** Every `<PageHead …>` opening tag in a source, whole: `{…}` props may hold a `>`. */
function heads(text: string): string[] {
  const found: string[] = []
  const src = code(text)
  for (const m of src.matchAll(/<PageHead\b/g)) {
    let depth = 0
    let i = m.index! + m[0].length
    for (; i < src.length; i++) {
      const ch = src[i]
      if (ch === '{') depth++
      else if (ch === '}') depth--
      else if (ch === '>' && depth === 0) break
    }
    found.push(src.slice(m.index!, i + 1))
  }
  return found
}

/** The `title` prop as written: `"Access"` or `{REFERENCE_LABEL}`. */
function titleOf(tag: string): string {
  const s = /\btitle=("[^"]*")/.exec(tag)
  if (s) return s[1]!
  const at = tag.search(/\btitle=\{/)
  if (at < 0) return '(no title)'
  let depth = 0
  const start = tag.indexOf('{', at)
  for (let i = start; i < tag.length; i++) {
    if (tag[i] === '{') depth++
    else if (tag[i] === '}' && --depth === 0) return tag.slice(start, i + 1)
  }
  return '(unparsed title)'
}

function walk(): { key: string; helped: boolean }[] {
  const out: { key: string; helped: boolean }[] = []
  for (const f of readdirSync(SRC)) {
    if (!f.endsWith('.tsx')) continue
    for (const tag of heads(readFileSync(join(SRC, f), 'utf8'))) {
      out.push({ key: `${f} ${titleOf(tag)}`, helped: /\bhelp=/.test(tag) })
    }
  }
  return out
}

describe('every PageHead has a help topic, or is on the commented allowlist (QA-066)', () => {
  it('finds the heads it is about, so an empty walk cannot pass', () => {
    const all = walk()
    // 26 heads on main on 2026-10-20; Shell.tsx's `Screen` head passes its own `help` through.
    expect(all.length).toBeGreaterThanOrEqual(25)
    expect(all.some((h) => h.key === 'Shell.tsx {title}' && h.helped)).toBe(true)
  })

  it('a head with no help= is on the allowlist', () => {
    const missing = walk()
      .filter((h) => !h.helped && ALLOWED[h.key] === undefined)
      .map((h) => h.key)
    expect(missing, 'a PageHead with no help= and no allowlist entry saying why').toEqual([])
  })

  it('every allowlist entry is still a head without a topic', () => {
    const bare = new Set(walk().filter((h) => !h.helped).map((h) => h.key))
    const stale = Object.keys(ALLOWED).filter((k) => !bare.has(k))
    expect(stale, 'an allowlist entry whose head is gone or now has a topic').toEqual([])
  })

  it('the five QA-066 pages each name their topic', () => {
    // MUTATION: drop `help=` from any one of the four heads below.
    const helped = new Set(walk().filter((h) => h.helped).map((h) => h.key))
    for (const key of [
      'WorkspacePeople.tsx "People and roles"',
      'McpServers.tsx "MCP servers"',
      'CodeIntelligence.tsx "Workspace"',
      'GitHubConnect.tsx "Connect GitHub"',
    ]) {
      expect(helped.has(key), `${key} has no help=`).toBe(true)
    }
  })
})
