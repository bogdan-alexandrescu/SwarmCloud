/**
 * A WORKFLOW STEP'S PLUGIN, CHOSEN IN A GUIDED SHEET (docs/design/agent-plugins.md;
 * the owner picked direction B, 4 November 2026).
 *
 * The step card carries one segmented chip -- plugin, mode, skills -- or "+ Add
 * a plugin". Choosing opens a side sheet in three numbered parts: 1 the plugin
 * (only the catalogue plugins this workspace allows can be picked, and each
 * says why it can or cannot be), 2 the mode (each with what it means; a mode
 * that does not run yet is refused in the platform's words), 3 the skills the
 * mode offers. The live chip at the sheet's foot is what the step will carry.
 *
 * NOTHING TYPED (invariant 10): plugins, modes and skills are radios and ticks
 * over the names `GET /v1/plugins` serves. No control takes a path, command,
 * URL or version; the version is shown, pinned in the image.
 *
 * WHERE THE PICK GOES (the owner's pick on PR #3264, 5 November 2026): the
 * step's own `plugins` field, `{name: {mode, skills}}` (swarm_api.schemas.
 * StepPluginCreate), never a work contract the console writes. swarm-api
 * checks it and carries it in the step's signed work contract
 * (`swarm_api.plugins.with_step_plugins`). The refusals `stepPluginProblem`
 * predicts are `swarm_api.plugins.check_step`'s own words, in its order.
 */

import { useCallback, useEffect, useId, useRef, useState } from 'react'

import { USE_FIXTURES } from './api'
import { Button, Chip, Dash } from './components'
import { read, route, type Result } from './fetch'
import './styles/pluginsheet.css'

/* --------------------------------------------------------------------------
   WHAT swarm-api SERVES (swarm_api.plugins.view / catalogue_view)
   -------------------------------------------------------------------------- */

export interface PluginModeOffer {
  skills: string[]
  available: boolean
  /** Present only when `available` is false: why the mode does not run yet. */
  reason?: string
}

export interface PluginCatalogueEntry {
  name: string
  version: string
  runner_profiles: string[]
  modes: Record<string, PluginModeOffer>
}

/** `GET /v1/plugins`: the selected workspace's signed setting and the catalogue. */
export interface PluginsView {
  tenant_id: string
  plugins: { allowed: string[] }
  defaults?: { plugins: { allowed: string[] } }
  set_at: string | null
  set_by: string | null
  can_edit: boolean
  catalogue: PluginCatalogueEntry[]
}

/** One step's pick: a catalogue plugin, a mode and skills, by name. */
export interface StepPlugin {
  name: string
  mode: string
  skills: string[]
}

/** The words a mode reads as, and its one-line meaning (agent-plugins.md, "The question bridge"). */
export const MODE_WORDS: Readonly<Record<string, string>> = {
  unattended: 'Unattended', planner: 'Planner', designer: 'Designer',
}
export const MODE_MEANS: Readonly<Record<string, string>> = {
  unattended: 'answers what has a recommended answer itself; anything else waits for a person',
  planner: 'interviews a person, then proposes a plan for approval',
  designer: 'interviews a person, then proposes a design for approval',
}
const MODE_ORDER = ['unattended', 'planner', 'designer']

const modeWord = (m: string) => MODE_WORDS[m] ?? m

/** The served modes in the platform's order, then any the console has no word for. */
function modesOf(entry: PluginCatalogueEntry): string[] {
  const served = Object.keys(entry.modes)
  return [...MODE_ORDER.filter((m) => served.includes(m)), ...served.filter((m) => !MODE_ORDER.includes(m)).sort()]
}

/* --------------------------------------------------------------------------
   check_step's refusals, in its words and its order
   -------------------------------------------------------------------------- */

export const refusalWords = {
  unknown: (name: string) => `'${name}' is not in the plugin catalogue`,
  profile: (entry: PluginCatalogueEntry, profile: string) =>
    `${entry.name} runs only on the profiles ${entry.runner_profiles.join(', ')}, not ${profile}`,
  mode: (name: string, mode: string, reason: string) => `${name} in ${mode} mode ${reason}`,
  skills: (name: string, mode: string, extra: string[]) => `${name} in ${mode} mode does not offer ${extra.join(', ')}`,
  notAllowed: (name: string) =>
    `this workspace has not allowed ${name}: its owner or admin allows it in the workspace's plugins setting`,
}

/** Why swarm-api would refuse this pick on a step on `profile`, or null. */
export function stepPluginProblem(view: PluginsView, pick: StepPlugin, profile: string): string | null {
  const entry = view.catalogue.find((e) => e.name === pick.name)
  if (entry === undefined) return refusalWords.unknown(pick.name)
  if (!entry.runner_profiles.includes(profile)) return refusalWords.profile(entry, profile)
  const offer = entry.modes[pick.mode]
  if (offer === undefined || !offer.available) {
    return refusalWords.mode(pick.name, pick.mode, offer?.reason ?? 'is not a mode this plugin runs')
  }
  const extra = pick.skills.filter((s) => !offer.skills.includes(s)).sort()
  if (extra.length > 0) return refusalWords.skills(pick.name, pick.mode, extra)
  if (pick.skills.length === 0) return `${pick.name} names at least one skill its mode offers`
  if (!view.plugins.allowed.includes(pick.name)) return refusalWords.notAllowed(pick.name)
  return null
}

/** Whether any catalogue plugin runs on `profile`. */
export function profileLoadsPlugins(view: PluginsView, profile: string): boolean {
  return view.catalogue.some((e) => e.runner_profiles.includes(profile))
}

/* --------------------------------------------------------------------------
   WHAT THE STEP SENDS (swarm_api.schemas.WorkflowStepCreate.plugins)
   -------------------------------------------------------------------------- */

/** The step's `plugins` field for this pick. */
export function stepPluginsField(pick: StepPlugin): Record<string, { mode: string; skills: string[] }> {
  return { [pick.name]: { mode: pick.mode, skills: [...pick.skills] } }
}

/** What the plan sends for a step's plugin: its `plugins` field, or why it cannot. */
export type StepPluginPlan =
  | { ok: true; plugins: Record<string, { mode: string; skills: string[] }> }
  | { ok: false; message: string }

export function stepPluginPlan(
  pick: StepPlugin, at: { stepId: string; profile: string }, ctx: StepPluginsContext,
): StepPluginPlan {
  const view = ctx.view !== null && (ctx.view.status === 'ok' || ctx.view.status === 'stale') ? ctx.view.data : null
  if (view === null) {
    return { ok: false, message: `the workspace's plugins were not read, so ${pick.name} cannot be checked; remove the plugin or reload` }
  }
  const problem = stepPluginProblem(view, pick, at.profile)
  if (problem !== null) return { ok: false, message: problem }
  return { ok: true, plugins: stepPluginsField(pick) }
}

/* --------------------------------------------------------------------------
   THE READS: once, on the first sheet opened
   -------------------------------------------------------------------------- */

export interface StepPluginsContext {
  /** `GET /v1/plugins`, or null before anything asked for it. */
  view: Result<PluginsView> | null
}

/** Nothing read yet: what the plan checks a pick against before any sheet opened. */
export const UNREAD_STEP_PLUGINS: StepPluginsContext = { view: null }

export async function loadStepPlugins(): Promise<Result<PluginsView>> {
  if (USE_FIXTURES) return { status: 'ok', data: fixturePluginsView(), fetchedAt: Date.now() }
  return read<PluginsView>(route('/v1/plugins'), () => false)
}

/** The fixture dev server's answer: the real catalogue's shape, gstack allowed. */
function fixturePluginsView(): PluginsView {
  const profiles = ['claude-code', 'claude-code-large', 'claude-code-review', 'claude-code-browser', 'claude-code-lightpanda']
  const reason = 'runs an interview with a person, which needs the question bridge; until it lands only unattended mode runs'
  return {
    tenant_id: 'eng', plugins: { allowed: ['gstack'] }, defaults: { plugins: { allowed: [] } },
    set_at: null, set_by: null, can_edit: true,
    catalogue: [
      { name: 'gstack', version: '1.92.55.0', runner_profiles: profiles, modes: {
        unattended: { available: true, skills: ['review', 'investigate', 'health', 'qa-only', 'qa', 'design-review',
          'document-generate', 'document-release', 'ship', 'plan-eng-review', 'plan-design-review', 'autoplan'] },
        planner: { available: false, reason, skills: ['office-hours', 'plan-ceo-review', 'plan-eng-review', 'plan-devex-review', 'autoplan', 'spec'] },
        designer: { available: false, reason, skills: ['design-consultation', 'plan-design-review', 'design-review', 'design-html'] },
      } },
      { name: 'security-audit', version: 'c1c8a8c1471069fb0e188eeaff69b8e8db6564a8', runner_profiles: profiles, modes: {
        unattended: { available: true, skills: ['security-audit'] },
      } },
    ],
  }
}

/** The read the sheet needs, asked for once, on `want()`. Until then, nothing is fetched. */
export function useStepPlugins(): StepPluginsContext & { want: () => void } {
  const [ctx, setCtx] = useState<StepPluginsContext>({ view: null })
  const asked = useRef(false)
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => { live.current = false }
  }, [])
  const want = useCallback(() => {
    if (asked.current) return
    asked.current = true
    void loadStepPlugins().then((view) => {
      if (!live.current) return
      setCtx({ view })
    })
  }, [])
  return { ...ctx, want }
}

/* --------------------------------------------------------------------------
   THE STEP'S ROW: one chip, or "+ Add a plugin"
   -------------------------------------------------------------------------- */

/** The segmented chip: plugin | mode | skills. */
export function PluginChip({ pick, bad = false, onClick, label }: {
  pick: StepPlugin
  bad?: boolean
  onClick?: () => void
  label?: string
}) {
  const parts = (
    <>
      <span className="psh-chip-k">{pick.name}</span>
      <span>{modeWord(pick.mode)}</span>
      <span className="psh-chip-s">{pick.skills.length === 0 ? 'no skill yet' : pick.skills.join(' · ')}</span>
    </>
  )
  const title = `${pick.name}, ${modeWord(pick.mode)} mode: ${pick.skills.join(', ') || 'no skill yet'}`
  return onClick === undefined ? (
    <span className={`psh-chip is-static${bad ? ' is-bad' : ''}`} aria-label={label ?? title} title={title}>{parts}</span>
  ) : (
    <button type="button" className={`psh-chip${bad ? ' is-bad' : ''}`} onClick={onClick} title={title}
      aria-label={label ?? `Change the plugin: ${title}`}>{parts}</button>
  )
}

/**
 * The step card's plugin row. `pick` is the step's choice; `ctx` the reads,
 * null until a sheet has been opened. Before the catalogue is read every agent
 * step offers "+ Add a plugin", and the sheet says if its runner cannot load one.
 */
export function StepPluginRow({ stepId, profile, pick, ctx, onPick }: {
  stepId: string
  profile: string
  pick: StepPlugin | null
  ctx: StepPluginsContext & { want: () => void }
  onPick: (next: StepPlugin | null) => void
}) {
  const [open, setOpen] = useState(false)
  const view = ctx.view !== null && (ctx.view.status === 'ok' || ctx.view.status === 'stale') ? ctx.view.data : null
  const problem = pick !== null && view !== null ? stepPluginProblem(view, pick, profile) : null
  const loads = view === null || profileLoadsPlugins(view, profile)
  const show = () => { ctx.want(); setOpen(true) }
  return (
    <div className="psh-row">
      {pick !== null ? (
        <PluginChip pick={pick} bad={problem !== null} onClick={show} />
      ) : loads ? (
        <Button size="sm" kind="ghost" onClick={show} aria-label={`Add a plugin to ${stepId}`}>+ Add a plugin</Button>
      ) : (
        <span className="psh-dim psh-small">Plugins: this runner profile cannot load them</span>
      )}
      {problem !== null && (
        <p className="psh-refusal" role="alert">
          <b>Refused at submission:</b> {problem}.{' '}
          {view !== null && !view.catalogue.find((e) => e.name === pick?.name)?.runner_profiles.includes(profile)
            ? 'Pick a runner that loads it, or remove the plugin.'
            : 'Change the plugin, or remove it.'}
        </p>
      )}
      {open && (
        <PluginSheet stepId={stepId} profile={profile} pick={pick} ctx={ctx}
          onUse={(next) => { onPick(next); setOpen(false) }} onClose={() => setOpen(false)} />
      )}
    </div>
  )
}

/* --------------------------------------------------------------------------
   THE SHEET: 1 plugin, 2 mode, 3 skills
   -------------------------------------------------------------------------- */

export function PluginSheet({ stepId, profile, pick, ctx, onUse, onClose }: {
  stepId: string
  profile: string
  pick: StepPlugin | null
  ctx: StepPluginsContext
  onUse: (next: StepPlugin | null) => void
  onClose: () => void
}) {
  const heading = useRef<HTMLHeadingElement>(null)
  const uid = useId().replace(/:/g, '')
  useEffect(() => { heading.current?.focus() }, [])
  useEffect(() => {
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', key)
    return () => document.removeEventListener('keydown', key)
  }, [onClose])

  const view = ctx.view !== null && (ctx.view.status === 'ok' || ctx.view.status === 'stale') ? ctx.view.data : null
  const pickable = (e: PluginCatalogueEntry) => view !== null && view.plugins.allowed.includes(e.name) && e.runner_profiles.includes(profile)
  const first = view?.catalogue.find(pickable)
  const [name, setName] = useState<string | null>(pick?.name ?? first?.name ?? null)
  const entry = view?.catalogue.find((e) => e.name === name) ?? null
  const firstMode = (e: PluginCatalogueEntry | null) => (e === null ? null : modesOf(e).find((m) => e.modes[m]!.available) ?? null)
  const [mode, setMode] = useState<string | null>(pick?.mode ?? firstMode(entry))
  const [ticked, setTicked] = useState<string[]>(pick?.skills ?? [])

  // The defaults land once the catalogue does: a sheet opened before the read
  // starts on the first plugin it may pick and that plugin's first running mode.
  useEffect(() => {
    if (view === null || name !== null) return
    const e = view.catalogue.find(pickable) ?? null
    if (e === null) return
    setName(e.name)
    setMode(firstMode(e))
  }, [view])

  const offer = entry !== null && mode !== null ? entry.modes[mode] : undefined
  const offered = offer?.skills ?? []
  const draft: StepPlugin | null = name !== null && mode !== null ? { name, mode, skills: offered.filter((s) => ticked.includes(s)) } : null
  const problem = view !== null && draft !== null ? stepPluginProblem(view, draft, profile) : null
  const toggle = (s: string) => setTicked(ticked.includes(s) ? ticked.filter((x) => x !== s) : [...ticked, s])

  return (
    <aside className="psh-sheet" role="dialog" aria-modal="false" aria-labelledby={`psh-${uid}-h`}>
      <header className="psh-head">
        <div>
          <span className="psh-dim psh-small">Plugin for step</span>
          <h2 id={`psh-${uid}-h`} ref={heading} tabIndex={-1}>{stepId}</h2>
        </div>
        <Button size="sm" kind="ghost" onClick={onClose}>Close</Button>
      </header>

      {ctx.view === null ? (
        <p className="psh-part psh-dim" role="status">Reading this workspace's plugins…</p>
      ) : view === null ? (
        <p className="psh-part psh-refusal" role="alert">
          This workspace's plugins could not be read
          {ctx.view.status === 'error' ? `: ${ctx.view.error.message}` : ''}. Nothing can be picked until they are.
        </p>
      ) : (
        <>
          <section className="psh-part" aria-labelledby={`psh-${uid}-p1`}>
            <h3 id={`psh-${uid}-p1`}><span className="psh-n">1</span>Plugin</h3>
            {view.catalogue.length === 0 ? (
              <p className="psh-dim">The platform's catalogue lists no plugin.</p>
            ) : (
              <ul className="psh-choices" role="list">
                {view.catalogue.map((e) => {
                  const allowed = view.plugins.allowed.includes(e.name)
                  const loads = e.runner_profiles.includes(profile)
                  const can = allowed && loads
                  const why = !loads ? `${refusalWords.profile(e, profile)}.`
                    : !allowed ? `${refusalWords.notAllowed(e.name)}.`
                      : `Allowed in this workspace · runs on ${profile}`
                  return (
                    <li key={e.name}>
                      <label className={`psh-choice${e.name === name ? ' is-on' : ''}${can ? '' : ' is-off'}`}>
                        <input type="radio" name={`psh-${uid}-plugin`} checked={e.name === name} disabled={!can}
                          onChange={() => { setName(e.name); setMode(firstMode(e)); setTicked([]) }} />
                        <span>
                          <b>{e.name}</b> <Chip title={`${e.version}: pinned in the image, never chosen here`}>{e.version.length > 16 ? e.version.slice(0, 12) : e.version}</Chip>
                          <br /><span className="psh-dim psh-small">{why}</span>
                        </span>
                      </label>
                    </li>
                  )
                })}
              </ul>
            )}
            <p className="psh-dim psh-small">Only plugins this workspace allows can be picked (Config › Plugins).</p>
          </section>

          <section className="psh-part" aria-labelledby={`psh-${uid}-p2`}>
            <h3 id={`psh-${uid}-p2`}><span className="psh-n">2</span>Mode</h3>
            {entry === null ? (
              <p className="psh-dim psh-small">Pick a plugin first.</p>
            ) : (
              <ul className="psh-choices" role="list">
                {modesOf(entry).map((m) => {
                  const o = entry.modes[m]!
                  return (
                    <li key={m}>
                      <label className={`psh-choice${m === mode ? ' is-on' : ''}${o.available ? '' : ' is-off'}`}>
                        <input type="radio" name={`psh-${uid}-mode`} checked={m === mode} disabled={!o.available}
                          onChange={() => { setMode(m); setTicked([]) }} />
                        <span><b>{modeWord(m)}</b><br />
                          <span className="psh-dim psh-small">
                            {o.available
                              ? MODE_MEANS[m] ?? <>what it means: <Dash why="The console has no words for this mode" /></>
                              : `Not yet: ${refusalWords.mode(entry.name, m, o.reason ?? 'does not run yet')}.`}
                          </span>
                        </span>
                      </label>
                    </li>
                  )
                })}
              </ul>
            )}
          </section>

          <section className="psh-part" aria-labelledby={`psh-${uid}-p3`}>
            <h3 id={`psh-${uid}-p3`}>
              <span className="psh-n">3</span>Skills it may use
              {offer !== undefined && <span className="psh-count">{draft?.skills.length ?? 0} of {offered.length}</span>}
            </h3>
            {offer === undefined || !offer.available ? (
              <p className="psh-dim psh-small">Pick a mode that runs first.</p>
            ) : (
              <>
                <ul className="psh-ticks" role="list">
                  {offered.map((s) => (
                    <li key={s}>
                      <label className={`psh-tick${ticked.includes(s) ? ' is-on' : ''}`}>
                        <input type="checkbox" checked={ticked.includes(s)} onChange={() => toggle(s)} />
                        <code>{s}</code>
                        <span className="psh-dim">what it does: <Dash why="Not known: the catalogue serves each skill's name only" /></span>
                      </label>
                    </li>
                  ))}
                </ul>
                <p className="psh-dim psh-small">What each skill does is not served yet; the catalogue names them only.</p>
              </>
            )}
          </section>
        </>
      )}

      <footer className="psh-foot">
        {draft !== null
          ? <PluginChip pick={draft} bad={problem !== null && draft.skills.length > 0} label="What the step will show" />
          : <span className="psh-dim psh-small">No plugin picked</span>}
        <div className="psh-acts">
          {pick !== null && <Button size="sm" kind="ghost" onClick={() => onUse(null)}>Remove the plugin</Button>}
          <Button kind="primary" disabled={draft === null || problem !== null} onClick={() => { if (draft !== null) onUse(draft) }}>
            Use on this step
          </Button>
        </div>
        {draft !== null && problem !== null && draft.skills.length > 0 && (
          <p className="psh-refusal psh-wide" role="alert">{problem}.</p>
        )}
      </footer>
    </aside>
  )
}
