// ISSUE 81, THE CAPACITY AND CONFIG PAGES: NO EXPLANATORY RUN ON THE SURFACE,
// EVERY SENTENCE STILL IN THE DOCUMENT (the owner's standing rule, 2026-11-05).
//
// Capacity's pages and Config's (with Settings › Profile) each drew a sentence
// or two of explanation as a visible run (proseruns.ts: nine words or more, or
// two sentences or more). Each is now a short line, or nothing, on the
// surface, and the sentence it was sits behind a mark (`[data-help-description]`:
// an (i)'s tip, or an access rule's fold), word for word. A must-stay
// disclosure -- Jev's price not set, an unlisted repository refused, what a
// pushed workflow runs with, the per-agent budget, a setting the API does not
// serve, a secret this workspace could not read -- stays visible as ONE short
// line.
//
// The App's pages are mounted at their addresses on the fixture build, as the
// prose sweep mounts them (proseapp.tsx); the states the fixture does not
// draw are mounted from their components.
//
// BREAK IT: put any listed sentence back on the surface, or drop it from its
// tip -- the case for its page turns red.

import { act, render, screen } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { longRuns } from './proseruns'
import { serve } from './repofixture'
import { screenLoaded } from './screenLoaded'

/** What each render left mounted, unmounted after each case and between a case's mounts. */
const mounted: Array<() => void> = []
function unmountAll(): void {
  while (mounted.length > 0) mounted.pop()!()
}

const collapse = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim()

/** What a reader sees: the document's text with the help copy taken out. */
function surface(root: Element = document.body): string {
  const clone = root.cloneNode(true) as Element
  for (const el of [...clone.querySelectorAll('[data-help-description]')]) el.remove()
  return collapse(clone.textContent)
}

/** The help copy: every mark's tip and fold, as text. */
function behind(root: Element = document.body): string[] {
  return [...root.querySelectorAll('[data-help-description]')].map((el) => collapse(el.textContent))
}

/** No visible long run contains `fragment`. */
function noLongRun(fragment: string): void {
  const hits = longRuns(document.body).filter((r) => r.includes(fragment))
  expect(hits, `a long run still shows "${fragment}"`).toEqual([])
}

/** `sentence` is no longer on the surface, and is word for word behind a mark. */
function moved(sentence: string): void {
  noLongRun(sentence.slice(0, 24))
  expect(surface(), `"${sentence}" is still on the surface`).not.toContain(sentence)
  expect(behind().some((t) => t.includes(sentence)), `"${sentence}" is behind no mark`).toBe(true)
}

/** `line` is on the surface, and is no long run. */
function stays(line: string): void {
  expect(surface(), `"${line}" left the surface`).toContain(line)
  expect(longRuns(document.body).filter((r) => r.includes(line)), `"${line}" is part of a long run`).toEqual([])
}

/** Each access rule config/github draws: its id, its head, and its visible long runs. */
function accessRules(): Array<{ rule: string; head: string; runs: string[] }> {
  return [...document.querySelectorAll<HTMLElement>('[data-rule]')].map((r) => ({
    rule: r.dataset.rule ?? '',
    head: collapse(r.querySelector('b')?.textContent),
    runs: longRuns(r),
  }))
}

/** Every `.pk-lead` drawn is short: no long run of its own. */
function leadsShort(): void {
  for (const lead of document.querySelectorAll<HTMLElement>('.pk-lead')) {
    expect(longRuns(lead), `a pk-lead draws a long run: ${collapse(lead.textContent)}`).toEqual([])
  }
}

// ---------------------------------------------------------------------------
// The App's pages, on the fixture build
// ---------------------------------------------------------------------------

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700)
    })
  }
}

function openAt(open: string): void {
  if (open.startsWith('/')) {
    window.location.hash = ''
    window.history.replaceState(null, '', open)
  } else {
    window.history.replaceState(null, '', '/')
    window.location.hash = `#${open}`
  }
}

async function page(open: string): Promise<void> {
  vi.resetModules()
  const { App } = await import('../App')
  const { FIXTURE_CLOCK } = await import('../api')
  localStorage.clear()
  sessionStorage.clear()
  vi.setSystemTime(new Date(FIXTURE_CLOCK))
  openAt(open)
  mounted.push(render(<App />).unmount)
  await screenLoaded()
  await settle()
}

describe('issue 81: the capacity and config pages, on the fixture build', () => {
  let zone: string | undefined
  beforeAll(() => {
    zone = process.env.TZ
    process.env.TZ = 'UTC'
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    if (zone === undefined) delete process.env.TZ
    else process.env.TZ = zone
    window.location.hash = ''
    window.history.replaceState(null, '', '/')
    localStorage.clear()
  })
  afterEach(() => unmountAll())

  it('capacity/profiles: the matrix note is a short label, its sentences behind its (i)', async () => {
    await page('capacity/profiles')
    moved('Each cell is the units free in that pool, leased/ceiling under it.')
    stays('How to read this')
    leadsShort()
  }, 60_000)

  it('capacity/jev-usage: "rate not set" stays as one line, why the dollars are blank behind its (i)', async () => {
    await page('capacity/jev-usage')
    moved('Dollars stay blank until a platform admin sets Jev’s per-token rate.')
    stays('Jev’s price: rate not set.')
  }, 60_000)

  it('capacity/catalogue: what sets a runtime apart is a short fact', async () => {
    await page('capacity/catalogue')
    noLongRun('accepting any one of its credentials')
    noLongRun('the only runtime')
    expect(surface()).toContain('sole runtime accepting any one of its credentials')
  }, 60_000)

  it('capacity/account-health: one subscription twice is named, the rest behind its (i); skipped here is one line', async () => {
    await page('capacity/account-health')
    noLongRun('One subscription twice')
    expect(surface()).toContain('One subscription twice')
    expect(behind().some((t) => t.endsWith('. The pool hands out only the other'))).toBe(true)
    expect(surface()).not.toContain('The pool hands out only the other')
    stays('Skipped here: its secret could not be read')
  }, 60_000)

  it('config/github: each access rule draws its head, and no body on the surface', async () => {
    await page('config/github')
    const rules = accessRules()
    expect(rules.map((r) => r.rule)).toEqual(['D9', 'D8', 'D6'])
    for (const r of rules) {
      expect(r.head, `rule ${r.rule} draws no head`).not.toBe('')
      expect(r.runs, `rule ${r.rule} draws a body`).toEqual([])
    }
    stays('Read means read.')
    stays('No workflow changes.')
    stays('Verifying only reads.')
    moved('A repository granted Read is cloned and never pushed')
    moved('The SwarmCloud App asks GitHub for contents, pull requests, issues and checks (read).')
    moved('Clone reads the upload-pack advertisement')
    leadsShort()
  }, 60_000)

  it('config/repositories: the lead, the reach foot and the workflow-files note are short, each sentence behind its (i)', async () => {
    await page('config/repositories')
    moved('3 repositories chosen, every one registered. Choose more on GitHub opens GitHub access › Choose repositories.')
    stays('Repositories · nothing left.')
    moved('An agent for a repository not on this list is refused at submission.')
    stays('Unlisted repositories are refused at submission.')
    moved("An agent's workflow on a pushed branch runs in this repository with its secrets before any merge hold. Merges of workflow changes always wait for an owner.")
    stays('Workflow files run with secrets before any merge.')
    leadsShort()
  }, 60_000)

  it('config/secrets: the Jev backend lead is one short line, its sentence behind its (i)', async () => {
    await page('config/secrets')
    moved('Where Jev, the browser’s judgment model, sends what it is asked to judge for agents in this workspace.')
    stays('Where Jev sends what it judges.')
    leadsShort()
  }, 60_000)

  it('config/workspace: one short line for the per-agent budget and one for a setting the API does not serve', async () => {
    await page('config/workspace')
    moved('The most one agent may spend on a step in this workspace. At 80% of its budget an agent is told to finish and publish.')
    stays('An agent stops at its budget.')
    moved('A QA run’s read-only flows run in batches, one live-browser step each.')
    stays('The batch size a QA run uses.')
    noLongRun('A whole number')
    moved('The Per-agent budget setting is not served by this API yet. Nothing here is a zero: it has not been read.')
    stays('Not read, so not a zero.')
    leadsShort()
  }, 60_000)

  it('config/mcp-servers, config/plugins and settings/profile: every pk-lead drawn is short', async () => {
    for (const at of ['config/mcp-servers', 'config/plugins', '/settings/profile']) {
      await page(at)
      leadsShort()
      unmountAll()
    }
  }, 120_000)
})

// ---------------------------------------------------------------------------
// The states the fixture does not draw, mounted from their components
// ---------------------------------------------------------------------------

describe('issue 81: the config states behind a click or a missing route', () => {
  afterEach(() => unmountAll())

  it('a route the API does not serve: one short could-not-check line, the sentences behind its (i)', async () => {
    const { UrNotServed } = await import('../RepositoriesParts')
    mounted.push(render(<UrNotServed route="GET /v1/plugins" what="the plugin catalogue" />).unmount)
    moved('The plugin catalogue is not served by this API yet. Nothing here is a zero: it has not been read.')
    stays('Not read, so not a zero.')
  })

  it('one subscription twice, in the table: the name on the surface, what matched behind its (i)', async () => {
    const { whyOf } = await import('../AccountsTable')
    const a = {
      account_id: 'eng:copy', state: 'AVAILABLE', can_take_new_agent: false,
      blocked_by: [{ duplicate_of: 'eng:primary', reason: 'registered on the same organisation as eng:primary' }],
      duplicate_of: 'eng:primary',
    }
    mounted.push(render(<p>{whyOf(a as never, 'eng', Date.now(), new Map([['eng:primary', 'primary']]))}</p>).unmount)
    moved('registered on the same organisation as primary. The pool hands out only the other')
    stays('One subscription twice')
  })
})

// ---------------------------------------------------------------------------
// The join page, live against a stubbed fetch: no link, and a link's ask form
// ---------------------------------------------------------------------------

describe('issue 81: the join page leads are short, each sentence behind its (i)', () => {
  // A made-up code of a join link's shape, built at runtime.
  const CODE = ['jc', 'Q4', 'x'.repeat(8), '-_', 'k'.repeat(8)].join('')
  afterEach(() => {
    unmountAll()
    vi.unstubAllEnvs()
  })
  async function join(code: string | null): Promise<void> {
    serve((m, url) => {
      if (m === 'GET' && url === '/v1/join-requests/mine') return { status: 200, body: { requests: [] } }
      if (m === 'POST' && url === '/v1/join/lookup') return { status: 200, body: { display_name: 'Example Labs' } }
      return null
    })
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { JoinWorkspaceScreen } = await import('../JoinWorkspace')
    mounted.push(render(<JoinWorkspaceScreen code={code} />).unmount)
  }

  it('with no link: one short line, the three sentences behind its (i)', async () => {
    await join(null)
    await screen.findByText('You have not asked to join a workspace.', undefined, { timeout: 5000 })
    moved('To ask to join a team’s workspace, open the join link one of its admins shared with you')
    moved('An admin who has not shared one turns it on in Settings › People and roles.')
    stays('Open the join link an admin shared.')
    leadsShort()
  })

  it('with a link: whose it is and who sees the request stay, the rest behind its (i)', async () => {
    await join(CODE)
    await screen.findByText(/This link is Example Labs/, undefined, { timeout: 5000 })
    moved('Ask to join it, and one of its admins approves or denies your request.')
    moved('Until one approves it you cannot see anything in Example Labs.')
    stays('Its admins see your email and note.')
    leadsShort()
  })
})
