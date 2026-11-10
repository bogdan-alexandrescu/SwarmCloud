// MOCK-HOOK FILE
/**
 * DIRECTION A, "line": Admin › Setup's optional plugin step as a ninth line
 * of the checklist, after Send to your team (managed-plugins.md §15.1,
 * lane 21's mock-up). Drawn only in the fixture build at `?sp=line`; the
 * plugin step's fixture state is `?plug=todo|skipped|done` (todo by default).
 *
 * The line's state is the route's `steps.plugins` (`plugins_step_view`):
 * done once a person added an active install, skipped when a platform admin
 * skipped it and none exists, else todo. It is never needed: no line's
 * state, no readiness and not `nextLineKey` reads it.
 */
import { useEffect, useRef, useState } from 'react'
import { USE_FIXTURES } from '../api'
import { ADMIN_PLUGIN_ADD_PATH, adminPluginPath } from '../AdminPlugins'
import { Button, ButtonLink, Chip } from '../components'

/** One install a person added (`added_plugins`). */
export interface AddedPlugin {
  install_id: string
  name: string
  display_name: string
  scope: 'platform' | 'workspace'
  tenant_id: string | null
}

/** `steps.plugins`, as `plugins_step_view` answers it, plus the bundled names the catalogue lists. */
export interface PluginStepView {
  state: 'todo' | 'skipped' | 'done'
  count: number
  added: AddedPlugin[]
  skipped: { skipped_at: string; skipped_by: string } | null
  /** The bundled installs (§1.4): there without anyone adding them, never counted. */
  bundled: string[]
}

export type PlugFixture = 'todo' | 'skipped' | 'done'

/** The address as the page opened: read once, at load, because the app takes its query off the address bar. */
const OPENED = typeof window === 'undefined' ? '' : window.location.search

/** The direction is drawn: the fixture build at `?sp=line`. */
export function pluginLineOn(search: string = OPENED): boolean {
  return USE_FIXTURES && new URLSearchParams(search).get('sp') === 'line'
}

export function plugFixture(search: string = OPENED): PlugFixture {
  const p = new URLSearchParams(search).get('plug')
  return p === 'skipped' || p === 'done' ? p : 'todo'
}

const BUNDLED = ['gstack', 'security-audit']

const ADDED: AddedPlugin[] = [
  { install_id: 'pi-7c2e41', name: 'lint-guard', display_name: 'Lint guard', scope: 'platform', tenant_id: null },
  { install_id: 'pi-93ab08', name: 'changelog-writer', display_name: 'Changelog writer', scope: 'workspace', tenant_id: 'eng' },
]

/** The fixture's `steps.plugins` in each state, its times counted from `nowMs`. */
export function fixturePluginStep(kind: PlugFixture, nowMs: number): PluginStepView {
  if (kind === 'done') return { state: 'done', count: ADDED.length, added: ADDED, skipped: null, bundled: BUNDLED }
  if (kind === 'skipped') return { state: 'skipped', count: 0, added: [], skipped: { skipped_at: new Date(nowMs - 3 * 60_000).toISOString(), skipped_by: 'ana@example.com' }, bundled: BUNDLED }
  return { state: 'todo', count: 0, added: [], skipped: null, bundled: BUNDLED }
}

/**
 * The step's view: the fixture's, then what Skip (`POST /v1/admin/setup/plugins/skip`)
 * and Add one anyway (`DELETE` of the same route) answer, until the next read.
 */
export function usePluginStep(served: PluginStepView | null | undefined, nowMs: number): { view: PluginStepView; undone: boolean; skip: () => void; undo: () => void } {
  const [view, setView] = useState(() => served ?? fixturePluginStep(plugFixture(), nowMs))
  /** Add one anyway was just pressed: the line flashes once and focus lands on Add a plugin, where the pressed button was. */
  const [undone, setUndone] = useState(false)
  useEffect(() => {
    if (!undone) return
    const t = window.setTimeout(() => setUndone(false), UNDONE_MS)
    return () => window.clearTimeout(t)
  }, [undone])
  return {
    view,
    undone,
    skip: () => {
      setUndone(false)
      setView(fixturePluginStep('skipped', nowMs))
    },
    undo: () => {
      setView(fixturePluginStep('todo', nowMs))
      setUndone(true)
    },
  }
}

/** How long the line's flash after Add one anyway lasts (the `cfp-undone` keyframes run as long). */
const UNDONE_MS = 2400

/** "gstack and security-audit". */
export function andWords(names: string[]): string {
  return names.length <= 1 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** What a plugin is and where it is done later: behind the line's (i), never on the surface. */
export const PLUGIN_TIP =
  'A plugin adds skills, commands or hooks to agent steps. One added to the platform catalogue serves only the workspaces that allow it. Optional: nothing waits on it, and you can add one any time from Admin › Plugins.'

/** The few words under the line's name. */
export function pluginNow(v: PluginStepView): string {
  if (v.state === 'done') return `${v.count} added: ${v.added.map((p) => p.display_name).join(', ')}`
  if (v.state === 'skipped') return `skipped · ${v.bundled.join(', ')} installed`
  return `${v.bundled.join(', ')} installed · 0 added`
}

/** The panel: what is there already, the count, and the actions for this state. */
export function PluginBody({ view, undone, onSkip, onUndo, clock }: { view: PluginStepView; undone: boolean; onSkip: () => void; onUndo: () => void; clock: (t: number) => string }) {
  const skippedAt = view.skipped === null ? null : Date.parse(view.skipped.skipped_at)
  // The pressed button is gone once the skip is undone: focus moves to the step's first action, not to the page.
  const acts = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (undone) acts.current?.querySelector<HTMLElement>('a, button')?.focus()
  }, [undone])
  return (
    <>
      <div className="cfp-facts">
        <div className="cfp-fact">
          <span className="cfp-k">Installed</span>
          <span className="cfp-chips">
            {view.bundled.map((b) => (
              <Chip key={b}>{b}</Chip>
            ))}
            <span className="cfp-tag">bundled</span>
          </span>
        </div>
        <div className="cfp-fact">
          <span className="cfp-k">Added</span>
          <b className="cfp-v">{view.count}</b>
        </div>
      </div>
      {view.state === 'done' && (
        <ul className="cfp-added" aria-label="Plugins added">
          {view.added.map((p) => (
            <li key={p.install_id}>
              <a href={adminPluginPath(p.install_id)}>{p.display_name}</a>
              <span className="cfp-scope">{p.scope === 'platform' ? 'platform catalogue' : `${p.tenant_id ?? 'a'} workspace`}</span>
            </li>
          ))}
        </ul>
      )}
      {view.state === 'skipped' && skippedAt !== null && !Number.isNaN(skippedAt) && (
        <p className="cfp-skipped">
          Skipped {clock(skippedAt)} by {view.skipped?.skipped_by}
        </p>
      )}
      <div className="cfc-act" ref={acts}>
        {view.state === 'skipped' ? (
          <Button size="sm" kind="secondary" onClick={onUndo}>
            Add one anyway
          </Button>
        ) : (
          <>
            <ButtonLink size="sm" kind={view.state === 'done' ? 'secondary' : 'primary'} href={ADMIN_PLUGIN_ADD_PATH}>
              {view.state === 'done' ? 'Add another' : 'Add a plugin'}
            </ButtonLink>
            {view.state === 'todo' && (
              <Button size="sm" kind="secondary" onClick={onSkip}>
                Skip
              </Button>
            )}
          </>
        )}
      </div>
    </>
  )
}
