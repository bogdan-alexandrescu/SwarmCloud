// ADMIN › SETUP'S CONSOLE FINISH: A CHECKLIST BESIDE A WORKING PANEL (part
// of #2688; the owner's pick of 2026-11-02).
//
// WHAT EACH CASE HOLDS, each drawn from a record moment of the Day One story
// (consolefinish/moments.ts, the fixture build's records) through the live
// normaliser (`normSetupState`), at the fixture clock:
//   * the checklist's eight lines, in order, with their marks and the count
//     "N of 8", every one from the record;
//   * the panel follows the next step the person can act on, and stays put
//     once they pick another, until "Follow the next step again";
//   * the status line's states: waiting, applying, verify running with the
//     time left, not running with when it was last seen, verify failed with
//     its served cause, and ready with its ready line;
//   * a time the record lacks reads "not recorded", never now or zero, and a
//     usual time the route does not serve is never a constant;
//   * one person: "Your workspace", and no team step or invites;
//   * Hand over is offered only once the smoke agent has succeeded, and Send
//     is locked until verify passes;
//   * NO EXPLANATORY PROSE ON THE SURFACE (the owner's standing rule, 2026-11-05):
//     what the panel follows, what verify prints at the end, which checks the
//     console runs, how the add-ons are turned on and where every mark comes
//     from are tooltips or an (i), never a sentence on the page, and "Follow
//     the next step again" is a real button, drawn only when it does something.
//
// Stubbed fetch only (every read answers 404); no network, no credentials.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

/** The text a reader sees: tooltips and hidden descriptions left out. */
function shown(el: Element | null): string {
  if (el === null) return ''
  const copy = el.cloneNode(true) as Element
  for (const off of Array.from(copy.querySelectorAll('[role="tooltip"], [hidden]'))) off.remove()
  return visible(copy)
}

/** The tooltip of the host around `el`. */
const tipOf = (el: Element | null) => visible(el?.closest('.c-tip-host')?.querySelector('[role="tooltip"]') ?? null)

type Json = Record<string, unknown>

const NOW = Date.parse('2026-10-19T12:00:00Z')

async function load() {
  serve(() => null)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AdminSetupPage } = await import('../AdminSetup')
  const moments = await import('../consolefinish/moments')
  const { normSetupState } = await import('../api')
  const stateOf = (m: Parameters<typeof moments.momentSetupState>[0], edit?: (raw: Json) => Json) => {
    const raw = moments.momentSetupState(m, NOW)
    return normSetupState(edit === undefined ? raw : edit(raw))
  }
  return { AdminSetupPage, stateOf }
}

async function draw(m: Parameters<Awaited<ReturnType<typeof load>>['stateOf']>[0], edit?: (raw: Json) => Json) {
  const { AdminSetupPage, stateOf } = await load()
  const r = render(<AdminSetupPage state={stateOf(m, edit)} now={NOW} />)
  return { ...r, AdminSetupPage, stateOf }
}

const list = () => screen.getByRole('navigation', { name: 'Setup steps' })
const item = (key: string) => list().querySelector(`[data-step="${key}"]`) as HTMLElement
const keys = () => [...list().querySelectorAll('[data-step]')].map((li) => li.getAttribute('data-step'))
const status = () => visible(screen.getByRole('status', { name: 'What swarm install is doing' }))
const panel = () => document.querySelector('.cfc-panel') as HTMLElement
const kicker = () => panel().querySelector('.cfc-kicker')
const follows = 'The panel follows the next step you can act on.'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
})

describe('the console finish: the checklist', () => {
  it('draws eight lines in order, each mark from the record, and counts N of 8', async () => {
    await draw('accounts-waiting')
    // MUTATION: fold the smoke agent back into the first team (seven lines) -> fails.
    expect(keys()).toEqual(['cloud', 'sign-in-client', 'github', 'team', 'accounts', 'smoke', 'verify', 'send'])
    for (const k of ['cloud', 'sign-in-client', 'github', 'team', 'accounts']) expect(item(k).className).toContain('is-done')
    // MUTATION: tick the smoke agent while it runs -> fails.
    expect(item('smoke').className).toContain('is-in_progress')
    expect(item('verify').className).toContain('is-todo')
    expect(item('send').className).toContain('is-todo')
    expect(visible(item('send'))).toContain('locked until verify passes')
    expect(status()).toContain('5 of 8 done')
    // Who acts on the running smoke agent: the console, and the line says so.
    expect(visible(item('smoke'))).toContain('The console')
    // Lea's unused link stays listed under its step, with the time left from the served expiry.
    const subs = within(item('accounts')).getByRole('list', { name: /waiting on others/ })
    expect(visible(subs)).toContain('A teammate · lea@example.com')
    expect(visible(subs)).toContain('since 09:00')
    expect(visible(subs)).toContain('21 h left on the link')
  })

  it('arrives with one of eight done and the panel on the sign-in client', async () => {
    await draw('arrive')
    expect(item('cloud').className).toContain('is-done')
    for (const k of ['sign-in-client', 'github', 'team', 'accounts', 'smoke', 'verify', 'send']) expect(item(k).className).not.toContain('is-done')
    expect(status()).toContain('1 of 8 done')
    expect(shown(kicker())).toBe('Step 2 of 8')
    expect(tipOf(kicker()?.querySelector('.asm-explained') ?? null)).toBe(follows)
    // The step's own controls are today's: the client id field and the hidden secret.
    expect(within(panel()).getByLabelText(/Client ID/i)).toBeTruthy()
    const secret = within(panel()).getByLabelText(/Client secret/i) as HTMLInputElement
    expect(secret.type).toBe('password')
  })
})

describe('the console finish: the panel', () => {
  it('follows the next step the person can act on, past the steps others hold', async () => {
    await draw('signin-saved')
    // swarm install applies the client; the panel is already on the GitHub App.
    expect(item('sign-in-client').className).toContain('is-in_progress')
    expect(visible(item('sign-in-client'))).toContain('swarm install')
    // MUTATION: follow the first step not done rather than the first the person can act on -> fails.
    expect(panel().getAttribute('aria-label')).toBe('Step 3: GitHub App')
    expect(within(panel()).getByRole('button', { name: /Create the GitHub App/ })).toBeTruthy()
    cleanup()
    await draw('github-installed')
    // The self-check waits on pass 3 and nothing else is the person's yet: the panel stays on the first step not done.
    expect(item('github').className).toContain('is-in_progress')
    expect(panel().getAttribute('aria-label')).toBe('Step 3: GitHub App')
    expect(visible(item('team'))).toContain('after the GitHub App')
  })

  it('stays on a picked step as the record moves, until Follow the next step again', async () => {
    const { rerender, AdminSetupPage, stateOf } = await draw('github-installed')
    // MUTATION: draw the button while the panel already follows -> it does nothing, and this fails.
    expect(screen.queryByRole('button', { name: 'Follow the next step again' })).toBeNull()
    fireEvent.click(within(item('cloud')).getByRole('button'))
    expect(panel().getAttribute('aria-label')).toBe('Step 1: Cloud')
    expect(shown(kicker())).toBe('Step 1 of 8 · picked by you')
    // A real button in the theme's style, not a text link.
    const follow = screen.getByRole('button', { name: 'Follow the next step again' })
    expect(follow.className).toContain('c-btn')
    // MUTATION: re-follow on every read -> the next read moves the panel and this fails.
    rerender(<AdminSetupPage state={stateOf('accounts-waiting')} now={NOW} />)
    expect(panel().getAttribute('aria-label')).toBe('Step 1: Cloud')
    fireEvent.click(follow)
    expect(panel().getAttribute('aria-label')).toBe('Step 6: Smoke agent')
    expect(shown(kicker())).toBe('Step 6 of 8')
    expect(tipOf(kicker()?.querySelector('.asm-explained') ?? null)).toBe(follows)
    expect(screen.queryByRole('button', { name: 'Follow the next step again' })).toBeNull()
  })

  it('keeps every explanation behind a tooltip or an (i), each still in the page', async () => {
    await draw('verify-running')
    const page = document.querySelector('.cfc-finish') as HTMLElement
    const said: [string, string][] = [
      ['It prints its ready line when every check passes.', 'swarm install is running verify · check 3 of 8'],
      ['The console task and workflow below are two of them.', '2 of 8 checks passed'],
    ]
    for (const [tip, label] of said) {
      // MUTATION: print the sentence beside its label again.
      expect(shown(page)).not.toContain(tip)
      const host = [...page.querySelectorAll('.asm-explained')].find((el) => shown(el).startsWith(label)) ?? null
      expect(host, label).not.toBeNull()
      expect(tipOf(host)).toBe(tip)
    }
    const abouts: [string, string][] = [
      ['About Terminal', 'Every mark comes from the install record, read every 5 s; nothing here is ticked by hand.'],
      ['About Add-ons', 'Each is turned on from the terminal later; the panel lists them.'],
    ]
    for (const [name, tip] of abouts) {
      expect(shown(page)).not.toContain(tip)
      expect(tipOf(screen.getByRole('button', { name }))).toBe(tip)
    }
    // The must-stay facts stay on the surface.
    expect(shown(page)).toContain('Add-ons: optional, off.')
  })

  it('offers Hand over only once the smoke agent has succeeded, and Send only once verify passed', async () => {
    await draw('accounts-waiting')
    fireEvent.click(within(item('verify')).getByRole('button'))
    // MUTATION: offer the hand-over before the smoke succeeded -> fails.
    expect(within(panel()).queryByRole('button', { name: /Hand over to swarm install/ })).toBeNull()
    fireEvent.click(within(item('send')).getByRole('button'))
    expect(visible(panel())).toContain('Locked until verify passes')
    cleanup()
    await draw('terminal-closed')
    // The smoke succeeded: the hand-over is the next thing the person can do.
    expect(panel().getAttribute('aria-label')).toBe('Step 7: Verify')
    expect(within(panel()).getByRole('button', { name: /Hand over to swarm install/ })).toBeTruthy()
    cleanup()
    await draw('ready')
    fireEvent.click(within(item('send')).getByRole('button'))
    expect(visible(panel())).toContain('claude plugin install sc@swarmcloud --config deployment_url=https://console.example.com')
  })
})

describe('the console finish: the status line', () => {
  it('says swarm install is waiting, seen from its lease', async () => {
    await draw('arrive')
    expect(status()).toContain('swarm install is waiting on this page')
    expect(status()).toContain('Seen 20 s ago')
  })

  it('says what swarm install is applying', async () => {
    await draw('signin-saved')
    expect(status()).toContain('swarm install is working: adds the client to the sign-in proxy')
  })

  it('says verify is running, at which check, with the time left', async () => {
    await draw('verify-running')
    expect(status()).toContain('swarm install is running verify · check 3 of 8')
    // MUTATION: draw a fixed "about 18 min" in place of the served expected end -> fails.
    expect(status()).toContain('About 9 min left')
  })

  it('says swarm install is not running, and when it was last seen', async () => {
    await draw('terminal-closed')
    expect(status()).toContain('swarm install is not running · last seen 14 min ago')
    expect(status()).toContain('run swarm install again')
  })

  it('draws verify’s served cause when it failed, and no cause it was not served', async () => {
    await draw('verify-failed')
    expect(status()).toContain('verify failed at check 6 of 8, the live-browser check')
    expect(visible(panel())).toContain('Google’s sign-in answered 403 for the test account')
    cleanup()
    await draw('verify-failed', (raw) => {
      const engine = raw.engine as Json
      return { ...raw, engine: { ...engine, verify: { ...(engine.verify as Json), detail: undefined } } }
    })
    // MUTATION: draw a constant cause when the route serves none -> fails.
    expect(visible(panel())).not.toContain('403')
    expect(visible(panel())).toContain('failed')
  })

  it('reads ready with the ready line once swarm install finished', async () => {
    await draw('ready')
    expect(status()).toContain('READY: SwarmCloud answers at https://console.example.com')
    expect(status()).toContain('8 of 8 done')
  })
})

describe('the console finish: what the record does not hold', () => {
  it('reads since: not recorded for the smoke agent’s start when the record has none', async () => {
    await draw('accounts-waiting')
    // The moment's smoke has no started_at.
    // MUTATION: draw now, or 0 s, for a missing time -> fails.
    expect(visible(item('smoke'))).toContain('since: not recorded')
    expect(visible(item('smoke'))).not.toMatch(/since \d\d:\d\d/)
    cleanup()
    await draw('accounts-waiting', (raw) => {
      const teams = raw.teams as Json[]
      return { ...raw, teams: teams.map((t) => ({ ...t, smoke: { ...(t.smoke as Json), started_at: new Date(NOW - 4 * 60_000).toISOString() } })) }
    })
    expect(visible(item('smoke'))).toContain('since 11:56 · 4 min')
  })

  it('never derives a link’s start from its expiry, and never states a usual time the route does not serve', async () => {
    await draw('accounts-waiting', (raw) => {
      const accounts = raw.accounts as Record<string, Json>
      const { typical_s: _unserved, ...rest } = raw
      return { ...rest, accounts: Object.fromEntries(Object.entries(accounts).map(([k, a]) => [k, { ...a, link_made_at: undefined }])) }
    })
    const subs = within(item('accounts')).getByRole('list', { name: /waiting on others/ })
    // MUTATION: read the link's start as its expiry less 24 h -> "since 09:00" and this fails.
    expect(visible(subs)).toContain('since: not recorded')
    expect(visible(subs)).not.toContain('since 09:00')
    // MUTATION: keep the page's own "usually about 3 min" -> fails.
    fireEvent.click(within(item('smoke')).getByRole('button'))
    const waits = within(panel()).getByRole('region', { name: 'Waiting on' })
    expect(visible(waits)).toContain('usual time not recorded')
    expect(visible(waits)).not.toMatch(/usually/)
  })
})

describe('the console finish: one person', () => {
  it('names the step Your workspace, and draws no team step and no invites', async () => {
    await draw('one-person')
    expect(keys()).toHaveLength(8)
    expect(visible(item('team'))).toContain('Your workspace')
    expect(item('team').className).toContain('is-done')
    expect(visible(list())).not.toContain('First team')
    expect(visible(list())).toContain('no team step and no invites')
    // MUTATION: list a Google Groups wait for one person -> fails.
    expect(list().querySelector('.cfc-subs')).toBeNull()
    // The installer's own account is next, and the panel is on it.
    expect(panel().getAttribute('aria-label')).toBe('Step 5: Claude accounts')
  })
})

describe('the fixture build’s moments', () => {
  it('are read only in the fixture build, never in a live one', async () => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const live = await import('../consolefinish/moments')
    // MUTATION: read ?moment in a live build -> fails.
    expect(live.fixtureMoment('?moment=arrive')).toBeNull()
    vi.unstubAllEnvs()
    vi.resetModules()
    const fixtures = await import('../consolefinish/moments')
    expect(fixtures.fixtureMoment('?moment=arrive')).toBe('arrive')
    expect(fixtures.fixtureMoment('?moment=nowhere')).toBeNull()
  })

  it('read stage 9 done once the console steps are finished, as a finished install does', async () => {
    const { stateOf } = await load()
    expect(stateOf('ready').engine?.stages[9]?.state).toBe('done')
    vi.unstubAllEnvs()
    document.cookie = 'swarm_qa_persona=platform-admin; path=/'
    try {
      vi.resetModules()
      const api = await import('../api')
      expect(api.USE_FIXTURES).toBe(true)
      const got = await api.loadSetup()
      if (got.status !== 'ok') throw new Error(`the fixture setup read answered ${got.status}`)
      expect(got.data.finished).not.toBeNull()
      // MUTATION: leave the finished demo's stage 9 at switches_on (pass 3 only) -> fails.
      expect(got.data.engine?.stages[9]?.state).toBe('done')
    } finally {
      document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
    }
  })
})

describe('the console finish on a phone', () => {
  it('stacks the checklist and the panel in one column, the panel first', async () => {
    const { readFileSync } = await import('node:fs')
    const { join } = await import('node:path')
    const css = readFileSync(join(__dirname, '..', 'styles', 'console-finish.css'), 'utf8')
    const phone = [...css.matchAll(/@media \(max-width: 900px\) \{([\s\S]*?)\n\}/g)].map((m) => m[1]).join('\n')
    // MUTATION: drop either rule -> the 390-wide grid keeps two columns and
    // the panel computes 0px wide, so no step's controls can be reached.
    expect(phone).toMatch(/\.cfc-grid \{ grid-template-columns: minmax\(0, 1fr\); \}/)
    expect(phone).toMatch(/\.cfc-grid \.cfc-right \{ order: -1; \}/)
  })
})
