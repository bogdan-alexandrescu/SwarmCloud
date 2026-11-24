// ISSUE 81, THE ADMIN AND VOICE PAGES: NO EXPLANATORY RUN ON THE SURFACE,
// EVERY SENTENCE STILL IN THE DOCUMENT (the owner's standing rule, 2026-11-05).
//
// Admin's pages and Voice's, measured as the platform admin, each drew a
// sentence or two of explanation as a visible run (proseruns.ts: nine words or
// more, or two sentences or more). Each is now a short line, or a short fact
// list, on the surface, and the sentence it was sits behind an (i)
// (`[data-help-description]`), word for word. A must-stay disclosure -- what
// a QA identity grant lets a workspace do, that the grants could not be read,
// what a count costs in reads, that no secret value is read -- stays visible
// as ONE short line.
//
// What is left on these pages as a long run is data the console only shows:
// a saved reason an admin typed, the doctor's own findings (src/api.ts's
// fixture), and the test harness's own fetch guard drawn as an error on the
// two Voice pages that read the network (DATA below).
//
// BREAK IT: put any listed sentence back on the surface, or drop it from its
// tip -- the case for its page turns red.

import { act, render, screen, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { PLATFORM_TICKS } from '../PlatformCounts'
import { AdminRoleRefusal } from '../WorkspaceRefusal'
import { helpIn, longRuns, shownIn } from './proseruns'
import { screenLoaded } from './screenLoaded'

/** What each render left mounted, unmounted after each case. */
const mounted: Array<() => void> = []
function unmountAll(): void {
  while (mounted.length > 0) mounted.pop()!()
}

const collapse = (s: string | null | undefined) => (s ?? '').replace(/\s+/g, ' ').trim()

/** What a reader sees: the document's text with the help copy taken out. */
function surface(root: Element = document.body): string {
  return shownIn(root)
}

/** The help copy: every mark's tip, as text. */
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

/** The long runs that are data the console only shows, not its own words. */
const DATA = [
  // A saved reason: the admin's own words (RunnerSizes' p.rs-version-why).
  'large agents hit the memory limit on monorepo builds',
  // The test harness's fetch guard, drawn as the error of a read it refused.
  'a test reached the network: the UI suite is offline by contract.',
]

/** The page's long runs that are not DATA: the console's own long words. */
function ownLongRuns(): string[] {
  return longRuns(document.body).filter((r) => !DATA.some((d) => r.includes(d)))
}

// ---------------------------------------------------------------------------
// The App's pages, on the fixture build, as the platform admin
// ---------------------------------------------------------------------------

async function settle(): Promise<void> {
  for (let i = 0; i < 4; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(700)
    })
  }
}

async function page(open: string): Promise<void> {
  vi.resetModules()
  const { App } = await import('../App')
  const { FIXTURE_CLOCK } = await import('../api')
  localStorage.clear()
  sessionStorage.clear()
  vi.setSystemTime(new Date(FIXTURE_CLOCK))
  window.history.replaceState(null, '', '/')
  window.location.hash = `#${open}`
  mounted.push(render(<App />).unmount)
  await screenLoaded()
  await settle()
}

describe('issue 81: the admin and voice pages, on the fixture build, as the platform admin', () => {
  let zone: string | undefined
  beforeAll(() => {
    zone = process.env.TZ
    process.env.TZ = 'UTC'
    document.cookie = 'swarm_qa_persona=platform-admin; path=/'
    vi.useFakeTimers({ toFake: ['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', 'Date'] })
  })
  afterAll(() => {
    vi.useRealTimers()
    document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
    if (zone === undefined) delete process.env.TZ
    else process.env.TZ = zone
    window.location.hash = ''
    window.history.replaceState(null, '', '/')
    localStorage.clear()
  })
  afterEach(() => unmountAll())

  it('admin/limits: where the rest of global’s units are is a short fact list', async () => {
    await page('admin/limits')
    noLongRun('These pools add up to')
    stays('2 outside any class')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/runner-sizes: the version, the recent use and the hints are short facts, the sentences behind their (i)', async () => {
    await page('admin/runner-sizes')
    moved('A large agent takes 4 units and the pool’s ceiling is 2.')
    moved('These Cloud Run profiles do not follow this size: mock, generic, claude-code, codex.')
    moved('No pattern in these readings asks for a change.')
    stays('In effect: version 2')
    stays('saved by ops@example.com')
    noLongRun('Last saved by')
    noLongRun('Last 7 days')
    stays('42 attempts')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/tenants: the QA grant and its could-not-read are one short line each; budget’s why is its (i)', async () => {
    await page('admin/tenants')
    moved(
      'A workspace granted here may ask for a browser agent signed in to this console as the QA account. Grant it only with a reason; a revoke takes effect at the agent’s next credential renewal.',
    )
    stays('Granted workspaces’ agents may sign in as QA.')
    moved(
      'QA identity grants cannot be read from this deployment: its swarm-api is a release from before the QA identity, and has none of its routes. Deploy the current release of swarm-api to grant it; until then no workspace can name it.',
    )
    stays('QA identity grants could not be read here.')
    moved('Tenants have no budget column: no budget can be set here.')
    stays('No budget can be set here.')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/counts: each platform tick has a short name, its sentence behind an (i) in its row; the read cost stays', async () => {
    await page('admin/counts')
    moved('A snapshot, read by the same Run: 2 × 12 reads')
    stays('A snapshot, read by the same Run')
    stays('One exact count per state, 12 reads.')
    noLongRun('not run yet')
    moved(
      'The platform’s own machinery. No tenant manages these; they are set in Terraform, and listed here so every recurring thing is in one place.',
    )
    stays('The platform’s own machinery')
    const rows = within(screen.getByTestId('as-platform-ticks'))
      .getAllByRole('row')
      .filter((r) => r.dataset.tick !== undefined)
    expect(rows.map((r) => r.dataset.tick)).toEqual(PLATFORM_TICKS.map((t) => t.name))
    for (const t of PLATFORM_TICKS) {
      const row = rows.find((r) => r.dataset.tick === t.name)!
      // The third cell: What it does.
      const cell = within(row).getAllByRole('cell')[2]
      // MUTATION: draw `t.does` in the cell again and the row has a long run.
      expect(longRuns(row), `${t.name} draws a long run`).toEqual([])
      expect(collapse(shownIn(cell)), `${t.name} draws no short name`).toBe(t.title)
      expect(helpIn(row), `${t.name}'s sentence is behind no (i) in its row`).toContain(t.does)
    }
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/platform-secrets: the lead is short facts and “No value is read.” stays', async () => {
    await page('admin/platform-secrets')
    moved('It is in the catalogue but nothing was stored.')
    moved('One other is mid-rotation, which is expected for 24 h.')
    // Behind the disclosure's own tooltip, not an (i): the banner it sits in
    // is dark, and an (i)'s border there is under the spacing sweep's floor.
    moved('Read from Secret Manager metadata. No value, length or version content is read, and nothing on this page can set one.')
    stays('No value is read.')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/setup: the add-ons line and the console checks are short, their sentences behind an (i)', async () => {
    await page('admin/setup')
    moved('Off by default; core runs without them. Enabling one is a terminal step: it needs a Terraform switch')
    stays('Off by default; core runs without them')
    moved('swarm-api starts them on this page’s next read, as the first team')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/people: the workspace roles card’s hint is behind its (i); the oldest request is a fact', async () => {
    await page('admin/people')
    moved('Each group workspace’s default role, its admins and the people with a role of their own.')
    moved('a personal workspace’s only admin is its person.')
    noLongRun('The oldest')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('admin/workspaces: the page’s sub line is short, the rest behind its (i)', async () => {
    await page('admin/workspaces')
    moved('Every group workspace; you are admin in each as a platform admin. A workspace’s name opens its page and selects it.')
    stays('Every group workspace; you are admin in each')
    stays('you are its fixed admin')
    noLongRun('its settings are under Profile')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)

  it('voice/talk: the waiting card says what to do in a few words, the rest behind its (i)', async () => {
    await page('voice/talk')
    moved('Say which one, or answer in your own words; Voice it writes it here.')
    expect(ownLongRuns()).toEqual([])
  }, 60_000)
})

describe('issue 81: a platform page refused to a member', () => {
  afterEach(() => unmountAll())

  it('says “not a platform admin” and whose workspace it is as short facts', () => {
    const { container, unmount } = render(<AdminRoleRefusal page="Pool limits" role="admin" workspace="Alex" personal />)
    mounted.push(unmount)
    expect(longRuns(container)).toEqual([])
    expect(surface(container)).toContain('You are not a platform admin · Alex is your own workspace')
  })
})
