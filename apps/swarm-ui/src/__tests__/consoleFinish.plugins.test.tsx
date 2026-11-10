// ADMIN › SETUP'S OPTIONAL PLUGIN STEP: A NINTH LINE (managed-plugins.md
// §15.1; the owner's decision of 2026-11-06 and pick of 2026-11-10, "a ninth
// line").
//
// WHAT EACH CASE HOLDS, each drawn from a record moment of the Day One story
// (consolefinish/moments.ts) with the route's `steps.plugins` written in,
// through the live normaliser (`normSetupState`), at the fixture clock:
//   * each state draws its line and its panel: to do (Add a plugin and Skip),
//     skipped (Add one anyway), done (the plugins added, each a link);
//   * the line is last, after Send to your team, tagged Optional, and what a
//     plugin is sits behind its (i), never on the surface;
//   * Skip posts `POST /v1/admin/setup/plugins/skip` and draws the skipped
//     line; Add one anyway sends `DELETE` of the same route;
//   * the count stays "N of 8" in every plugin state, and the panel says
//     "Step 9 · optional", never "of 9";
//   * `nextLineKey` never picks the optional line, so the panel never moves
//     there by itself;
//   * on a narrow screen, picking a line scrolls its panel into view.
//
// Stubbed fetch only; no network, no credentials.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>
type Handler = Parameters<typeof serve>[0]

const NOW = Date.parse('2026-10-19T12:00:00Z')

/** `steps.plugins` as swarm-api's `plugins_step_view` answers it, in each state. */
function pluginsStep(state: 'todo' | 'skipped' | 'done'): Json {
  const added =
    state === 'done'
      ? [
          { install_id: 'pi-7c2e41', name: 'lint-guard', display_name: 'Lint guard', scope: 'platform', tenant_id: null },
          { install_id: 'pi-93ab08', name: 'changelog-writer', display_name: 'Changelog writer', scope: 'workspace', tenant_id: 'engineering' },
        ]
      : []
  const skipped = state === 'skipped' ? { skipped_at: '2026-10-19T11:57:00Z', skipped_by: 'ana@example.com' } : null
  return { needed: false, optional: true, state, count: added.length, added, skipped }
}

/** The moment's record with `steps.plugins` served, the way the live route serves it beside the other steps. */
const withPlugins = (state: 'todo' | 'skipped' | 'done') => (raw: Json): Json => ({ ...raw, steps: { ...((raw.steps as Json | null) ?? {}), plugins: pluginsStep(state) } })

async function draw(m: string, plug: 'todo' | 'skipped' | 'done' | null, handler: Handler = () => null) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { AdminSetupPage } = await import('../AdminSetup')
  const moments = await import('../consolefinish/moments')
  const { normSetupState } = await import('../api')
  const raw = moments.momentSetupState(m as Parameters<typeof moments.momentSetupState>[0], NOW)
  const state = normSetupState(plug === null ? raw : withPlugins(plug)(raw))
  const r = render(<AdminSetupPage state={state} now={NOW} />)
  return { ...r, calls }
}

/** The text a reader sees: tooltips and hidden descriptions left out. */
function shown(el: Element | null): string {
  if (el === null) return ''
  const copy = el.cloneNode(true) as Element
  for (const off of Array.from(copy.querySelectorAll('[role="tooltip"], [hidden]'))) off.remove()
  return visible(copy)
}

const tipOf = (el: Element | null) => visible(el?.closest('.c-tip-host')?.querySelector('[role="tooltip"]') ?? null)
const list = () => screen.getByRole('navigation', { name: 'Setup steps' })
const item = (key: string) => list().querySelector(`[data-step="${key}"]`) as HTMLElement
const keys = () => [...list().querySelectorAll('[data-step]')].map((li) => li.getAttribute('data-step'))
const status = () => visible(screen.getByRole('status', { name: 'What swarm install is doing' }))
const panel = () => document.querySelector('.cfc-panel') as HTMLElement
const kicker = () => shown(panel().querySelector('.cfc-kicker'))
const pickPlugins = () => fireEvent.click(within(item('plugins')).getAllByRole('button')[0] as HTMLElement)

const TIP = 'A plugin adds skills, commands or hooks to agent steps. One added to the platform catalogue serves only the workspaces that allow it. Optional: nothing waits on it, and you can add one any time from Admin › Plugins.'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('the plugin step: its line', () => {
  it('is a ninth line, last after Send, tagged Optional, with what a plugin is behind its (i)', async () => {
    await draw('verify-running', 'todo')
    // MUTATION: place the line before Send, or leave it out -> fails.
    expect(keys()).toEqual(['cloud', 'sign-in-client', 'github', 'team', 'accounts', 'smoke', 'verify', 'send', 'plugins'])
    expect(item('plugins').className).toContain('is-todo')
    expect(item('plugins').className).toContain('is-optional')
    expect(shown(item('plugins'))).toContain('9. Add a plugin')
    expect(within(item('plugins')).getByText('Optional').className).toContain('c-tag')
    // No words while to do: the panel holds the count (the console's prose ratchet).
    expect(shown(item('plugins'))).toBe('9. Add a pluginOptional')
    // MUTATION: print what a plugin is on the surface -> fails.
    expect(shown(document.body)).not.toContain('A plugin adds skills')
    expect(tipOf(screen.getByRole('button', { name: 'About Add a plugin' }))).toBe(TIP)
  })

  it('draws no ninth line from a route that serves no plugin step', async () => {
    await draw('verify-running', null)
    expect(keys()).not.toContain('plugins')
  })
})

describe('the plugin step: each state’s panel', () => {
  it('to do: Add a plugin, a link to Admin › Plugins › Add, and Skip, a real button', async () => {
    await draw('verify-running', 'todo')
    pickPlugins()
    expect(panel().getAttribute('aria-label')).toBe('Step 9: Add a plugin')
    // MUTATION: say "Step 9 of 9" -> fails.
    expect(kicker()).toBe('Step 9 · optional')
    const add = within(panel()).getByRole('link', { name: 'Add a plugin' })
    expect(add.getAttribute('href')).toBe('/admin/plugins/add')
    expect(add.className).toContain('c-btn')
    expect(within(panel()).getByRole('button', { name: 'Skip' }).className).toContain('c-btn')
    expect(shown(panel())).toContain('gstack')
    expect(shown(panel())).toContain('security-audit')
    expect(within(panel()).queryByRole('button', { name: 'Add one anyway' })).toBeNull()
  })

  it('skipped: the line reads skipped, and the panel offers Add one anyway and no Skip', async () => {
    await draw('verify-running', 'skipped')
    expect(item('plugins').className).toContain('is-skipped')
    expect(shown(item('plugins'))).toContain('skipped')
    pickPlugins()
    expect(kicker()).toBe('Step 9 · optional')
    expect(within(panel()).getByRole('button', { name: 'Add one anyway' }).className).toContain('c-btn')
    expect(within(panel()).queryByRole('button', { name: 'Skip' })).toBeNull()
    expect(shown(panel())).toContain('Skipped 11:57 by ana@example.com')
  })

  it('done: the line is ticked and the panel lists the plugins added, each a link to its page', async () => {
    await draw('verify-running', 'done')
    expect(item('plugins').className).toContain('is-done')
    expect(shown(item('plugins'))).toContain('2 added: Lint guard, Changelog writer')
    pickPlugins()
    const added = within(panel()).getByRole('list', { name: 'Plugins added' })
    expect(within(added).getByRole('link', { name: 'Lint guard' }).getAttribute('href')).toBe('/admin/plugins/pi-7c2e41')
    expect(within(added).getByRole('link', { name: 'Changelog writer' }).getAttribute('href')).toBe('/admin/plugins/pi-93ab08')
    expect(within(panel()).getByRole('link', { name: 'Add another' }).getAttribute('href')).toBe('/admin/plugins/add')
    expect(within(panel()).queryByRole('button', { name: 'Skip' })).toBeNull()
  })
})

describe('the plugin step: Skip and Add one anyway', () => {
  it('Skip posts the skip route and draws the skipped line', async () => {
    const { calls } = await draw('verify-running', 'todo', (m, url) =>
      m === 'POST' && url === '/v1/admin/setup/plugins/skip' ? { status: 200, body: { plugins: pluginsStep('skipped'), skipped: { skipped_at: '2026-10-19T11:57:00Z', skipped_by: 'ana@example.com' } } } : null,
    )
    pickPlugins()
    fireEvent.click(within(panel()).getByRole('button', { name: 'Skip' }))
    // MUTATION: draw the skip without asking the route -> no POST, and this fails.
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url === '/v1/admin/setup/plugins/skip')).toBe(true))
    await waitFor(() => expect(item('plugins').className).toContain('is-skipped'))
    expect(within(panel()).getByRole('button', { name: 'Add one anyway' })).toBeTruthy()
  })

  it('Add one anyway sends DELETE of the skip route, and the line is to do again', async () => {
    const { calls } = await draw('verify-running', 'skipped', (m, url) => (m === 'DELETE' && url === '/v1/admin/setup/plugins/skip' ? { status: 200, body: { plugins: pluginsStep('todo'), removed: true } } : null))
    pickPlugins()
    fireEvent.click(within(panel()).getByRole('button', { name: 'Add one anyway' }))
    await waitFor(() => expect(calls.some((c) => c.method === 'DELETE' && c.url === '/v1/admin/setup/plugins/skip')).toBe(true))
    await waitFor(() => expect(item('plugins').className).not.toContain('is-skipped'))
    const add = within(panel()).getByRole('link', { name: 'Add a plugin' })
    // The pressed button is gone: focus lands on Add a plugin, where it was.
    expect(document.activeElement).toBe(add)
  })
})

describe('the plugin step: Skip holds across the re-read', () => {
  it('stays skipped past a poll sent before the write, and once the re-read agrees', async () => {
    let rerender: (state: unknown) => void = () => {}
    let reads = 0
    const { calls, ...r } = await draw('verify-running', 'todo', (m, url) =>
      m === 'POST' && url === '/v1/admin/setup/plugins/skip' ? { status: 200, body: { plugins: pluginsStep('skipped') } } : null,
    )
    const { AdminSetupPage } = await import('../AdminSetup')
    const moments = await import('../consolefinish/moments')
    const { normSetupState } = await import('../api')
    const read = (plug: 'todo' | 'skipped') => normSetupState(withPlugins(plug)(moments.momentSetupState('verify-running', NOW)))
    rerender = (state) => r.rerender(<AdminSetupPage state={state as ReturnType<typeof read>} now={NOW} onChanged={() => (reads += 1)} />)
    rerender(read('todo'))
    pickPlugins()
    fireEvent.click(within(panel()).getByRole('button', { name: 'Skip' }))
    await waitFor(() => expect(item('plugins').className).toContain('is-skipped'))
    expect(reads).toBe(1)
    // A poll sent before the POST lands after it, still to do.
    // MUTATION: drop the held answer whenever the read changes -> the line is to do again, and this fails.
    rerender(read('todo'))
    expect(item('plugins').className).toContain('is-skipped')
    // The re-read sent after the POST agrees, and the read takes over.
    rerender(read('skipped'))
    expect(item('plugins').className).toContain('is-skipped')
    expect(within(panel()).getByRole('button', { name: 'Add one anyway' })).toBeTruthy()
    expect(calls.filter((c) => c.method === 'POST').length).toBe(1)
  })

  it('in the fixture build, the setup read serves the skip and the undo', async () => {
    document.cookie = 'swarm_qa_persona=platform-admin; path=/'
    try {
      vi.resetModules()
      const api = await import('../api')
      expect(api.USE_FIXTURES).toBe(true)
      const state = async () => {
        const got = await api.loadSetup()
        return got.status === 'ok' ? got.data.steps?.plugins?.state : got.status
      }
      expect(await state()).toBe('todo')
      // MUTATION: answer the skip without the fixture holding it -> the re-read serves to do, and this fails.
      expect((await api.skipSetupPlugins()).status).toBe('ok')
      expect(await state()).toBe('skipped')
      expect((await api.undoSetupPluginsSkip()).status).toBe('ok')
      expect(await state()).toBe('todo')
    } finally {
      document.cookie = 'swarm_qa_persona=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'
    }
  })
})

describe('the plugin step never counts and is never followed', () => {
  for (const plug of ['todo', 'skipped', 'done'] as const) {
    it(`keeps the count at 8 of 8 with the plugin step ${plug}`, async () => {
      await draw('ready', plug)
      expect(keys().at(-1)).toBe('plugins')
      // MUTATION: count the optional line -> "8 of 9" or "9 of 9", and this fails.
      expect(status()).toContain('8 of 8 done')
      expect(status()).not.toContain('of 9')
      cleanup()
      await draw('accounts-waiting', plug)
      expect(status()).toContain('5 of 8 done')
      expect(kicker()).toMatch(/ of 8$/)
    })
  }

  it('nextLineKey passes over the optional line, even when it is the only one not done', async () => {
    vi.resetModules()
    const { nextLineKey, pluginsLine, finishLines } = await import('../consolefinish/ChecklistFinish')
    const moments = await import('../consolefinish/moments')
    const { normSetupState } = await import('../api')
    const state = normSetupState(withPlugins('todo')(moments.momentSetupState('ready', NOW)))
    const ls = finishLines(state, NOW)
    expect(ls.map((l) => l.key).at(-1)).toBe('plugins')
    // MUTATION: drop the optional filter -> the plugin line is the first not done, and this fails.
    expect(nextLineKey(ls)).toBe('send')
    const only = [{ ...pluginsLine({ state: 'todo', count: 0, added: [], skipped: null }), actor: 'you' as const }]
    expect(nextLineKey(only)).toBe('send')
  })

  it('never moves the panel to the plugin step by itself', async () => {
    await draw('ready', 'todo')
    expect(item('plugins').className).toContain('is-todo')
    expect(panel().getAttribute('aria-label')).not.toBe('Step 9: Add a plugin')
  })
})

describe('the plugin step on a phone', () => {
  it('scrolls the panel into view when a line is picked on a narrow screen', async () => {
    const scroll = vi.fn()
    Object.defineProperty(window.HTMLElement.prototype, 'scrollIntoView', { configurable: true, writable: true, value: scroll })
    vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({ matches: q === '(max-width: 900px)', media: q, onchange: null, addEventListener: () => {}, removeEventListener: () => {}, addListener: () => {}, removeListener: () => {}, dispatchEvent: () => false }) as MediaQueryList)
    await draw('verify-running', 'todo')
    await act(async () => pickPlugins())
    // MUTATION: drop the scroll -> on a phone the panel sits above the list, out of view, and this fails.
    expect(scroll).toHaveBeenCalled()
    expect(scroll.mock.contexts[0]).toBe(document.querySelector('.cfc-right'))
  })
})

describe('the plugin step: the setup read', () => {
  it('reads steps.plugins through the live normaliser, and none from a route that serves none', async () => {
    vi.resetModules()
    const { normPluginsStep, normSetupState } = await import('../api')
    expect(normPluginsStep(pluginsStep('done'))?.added.map((p) => p.display_name)).toEqual(['Lint guard', 'Changelog writer'])
    expect(normPluginsStep(null)).toBeNull()
    expect(normSetupState({ steps: { plugins: pluginsStep('skipped') } }).steps?.plugins?.skipped?.skipped_by).toBe('ana@example.com')
  })
})
