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

import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react'

import { USE_FIXTURES } from './api'
import { Button, Chip, Dash } from './components'
import { read, route, type Result } from './fetch'
import { InfoTip } from './People'
import type {
  ManagedPluginInstall, PluginConfigView, PluginFormField, PluginOverrideScalar, PluginOverrideValue, StepManagedPlugins,
  WorkspacePluginCatalogue, WorkspacePluginDefaults,
} from './types'
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
   WHAT THE STEP SENDS FOR ITS MANAGED PLUGINS (managed-plugins.md section 7:
   swarm_api.plugins.merge_step_plugins reads `plugins` and `plugins_off`)
   -------------------------------------------------------------------------- */

/** A step with no managed plugin and no default turned off. */
export const NO_MANAGED: StepManagedPlugins = { added: {}, off: [] }

/** A step loads at most this many plugins (`pluginconfig.MAX_PLUGINS`, the field's `max_length`). */
export const MAX_STEP_PLUGINS = 8

/** The refusals, in `pluginconfig.ERROR_MESSAGES`'s words where it has them. */
export const managedWords = {
  unread: "the workspace's managed plugins were not read, so this step's plugins cannot be checked; reload",
  notAllowed: (name: string) => `${name}: the workspace does not allow that plugin`,
  tooMany: `a step runs at most ${MAX_STEP_PLUGINS} plugins`,
  value: (key: string) => `${key}: the value is not of the setting's type`,
  path: (key: string) => `${key}: a path setting is a relative path inside the checkout, with no '..'`,
}

/** Every managed install the workspace sees: its own, then the platform's. */
export function managedInstalls(m: ManagedPluginsRead): ManagedPluginInstall[] {
  return [...m.catalogue.installs, ...m.catalogue.platform_installs]
}

/** A default or an install as a person reads it: its name, or the id when unknown. */
export function pluginLabel(m: ManagedPluginsRead | null, id: string): string {
  const install = m === null ? undefined : managedInstalls(m).find((i) => i.install_id === id)
  return install?.display_name ?? install?.name ?? id
}

/** Only a non-secret, non-file setting takes a per-step override (section 6.4). */
export function isOverridable(field: PluginFormField): boolean {
  return field.kind === 'value' || field.kind === 'path'
}

function pathOk(v: PluginOverrideScalar): boolean {
  return typeof v === 'string' && v !== '' && v.length <= 1024 && !v.includes('\0') && !v.includes('\\')
    && !v.startsWith('/') && !v.startsWith('~') && !/^[A-Za-z]:/.test(v) && !v.split('/').includes('..')
}

function scalarOk(field: PluginFormField, v: PluginOverrideScalar): boolean {
  if (field.type === 'boolean') return typeof v === 'boolean'
  if (field.type === 'number') return typeof v === 'number' && Number.isFinite(v)
  return typeof v === 'string' && v.length <= 4096 && !v.includes('\0')
}

/** Why swarm-api would refuse `value` for `field` (`pluginconfig.check_value`), or null. */
export function overrideProblem(field: PluginFormField, value: PluginOverrideValue): string | null {
  if (!isOverridable(field)) return `${field.key}: a step overrides only a plugin's non-secret values`
  if (field.multiple !== Array.isArray(value) || (Array.isArray(value) && value.length > 64)) return managedWords.value(field.key)
  for (const item of Array.isArray(value) ? value : [value]) {
    if (field.kind === 'path' ? !pathOk(item) : !scalarOk(field, item)) {
      return field.kind === 'path' ? managedWords.path(field.key) : managedWords.value(field.key)
    }
  }
  return null
}

/** What the plan sends for a step's managed plugins: its `plugins` entries and `plugins_off`, or why not. */
export type StepManagedPlan =
  | { ok: true; plugins: Record<string, { overrides?: Record<string, PluginOverrideValue> }>; off: string[] }
  | { ok: false; message: string }

/**
 * The step's managed plugins as `merge_step_plugins` reads them. An added
 * install is `{install_id: {}}`, or `{install_id: {overrides}}`; a default
 * with overrides is named the same way and is not turned off; a default the
 * step turns off is in `plugins_off`, and nothing else is (a name both loaded
 * and turned off is refused). `builtIn` is the step's own built-in pick, which
 * swarm-api keeps in place of a default copy of it.
 */
export function stepManagedPlan(managed: StepManagedPlugins | null | undefined, builtIn: string | null,
  ctx: StepPluginsContext): StepManagedPlan {
  const m = managed ?? NO_MANAGED
  const ids = Object.keys(m.added).sort()
  if (ids.length === 0 && m.off.length === 0) return { ok: true, plugins: {}, off: [] }
  const read = dataOf(ctx.managed)
  if (read === null) return { ok: false, message: managedWords.unread }
  const installs = managedInstalls(read)
  const plugins: Record<string, { overrides?: Record<string, PluginOverrideValue> }> = {}
  for (const id of ids) {
    const install = installs.find((i) => i.install_id === id)
    if (install === undefined) return { ok: false, message: refusalWords.unknown(id) }
    if (install.scope === 'platform' && install.allowed !== true) return { ok: false, message: managedWords.notAllowed(install.name) }
    const overrides = m.added[id]!
    const fields = dataOf(ctx.forms?.[id])?.form.fields
    for (const [key, value] of Object.entries(overrides)) {
      const field = fields?.find((f) => f.key === key)
      const problem = field === undefined ? null : overrideProblem(field, value)
      if (problem !== null) return { ok: false, message: `${install.name} ${problem}` }
    }
    const isDefault = read.defaults.includes(id)
    if (isDefault && Object.keys(overrides).length === 0) continue
    plugins[id] = Object.keys(overrides).length === 0 ? {} : { overrides: { ...overrides } }
  }
  const loaded = new Set([...Object.keys(plugins), ...(builtIn === null ? [] : [builtIn])])
  const off = [...new Set(m.off)].filter((n) => read.defaults.includes(n) && !loaded.has(n)).sort()
  // The step's own `plugins` field holds at most eight (its `max_length`).
  // Defaults are not counted here: swarm-api skips the ones a runner cannot
  // load before it counts the merged set, which only it can see.
  if (loaded.size > MAX_STEP_PLUGINS) return { ok: false, message: managedWords.tooMany }
  return { ok: true, plugins, off }
}

/** The step's every-step defaults, each with whether it is on here; the step's own built-in pick is not a default copy. */
export function stepDefaults(read: ManagedPluginsRead, managed: StepManagedPlugins | null | undefined,
  builtIn: string | null): Array<{ name: string; on: boolean }> {
  const off = (managed ?? NO_MANAGED).off
  return read.defaults.filter((n) => n !== builtIn).map((name) => ({ name, on: !off.includes(name) }))
}

/** `managed` with the default `name` turned off for this step, or back on. */
export function withDefault(managed: StepManagedPlugins | null | undefined, name: string, on: boolean): StepManagedPlugins {
  const m = managed ?? NO_MANAGED
  const off = m.off.filter((n) => n !== name)
  const added = { ...m.added }
  // A default turned off carries no overrides: a name loaded and turned off is refused.
  if (!on) delete added[name]
  return { added, off: on ? off : [...off, name].sort() }
}

/* --------------------------------------------------------------------------
   THE READS: once, when a step first has a runner that runs an agent
   -------------------------------------------------------------------------- */

export interface StepPluginsContext {
  /** `GET /v1/plugins`, or null before anything asked for it. */
  view: Result<PluginsView> | null
  /** The workspace's managed catalogue and its every-step defaults, read
   *  after `view` names the workspace; absent or null before that. */
  managed?: Result<ManagedPluginsRead> | null
  /** Each managed plugin's settings form, by install id, read when its
   *  overrides are first opened. */
  forms?: Readonly<Record<string, Result<PluginConfigView>>>
}

/** The managed half of the builder's read: the catalogue and the defaults. */
export interface ManagedPluginsRead {
  catalogue: WorkspacePluginCatalogue
  defaults: string[]
}

/** Nothing read yet: what the plan checks a pick against before any sheet opened. */
export const UNREAD_STEP_PLUGINS: StepPluginsContext = { view: null, managed: null, forms: {} }

export async function loadStepPlugins(): Promise<Result<PluginsView>> {
  if (USE_FIXTURES) return { status: 'ok', data: fixturePluginsView(), fetchedAt: Date.now() }
  return read<PluginsView>(route('/v1/plugins'), () => false)
}

/** The workspace's managed catalogue and defaults, both or neither. */
export async function loadManagedPlugins(tenantId: string): Promise<Result<ManagedPluginsRead>> {
  if (USE_FIXTURES) return { status: 'ok', data: fixtureManaged(tenantId), fetchedAt: Date.now() }
  const [catalogue, defaults] = await Promise.all([
    read<WorkspacePluginCatalogue>(route('/v1/workspaces/{tenant_id}/plugin-catalogue', { tenant_id: tenantId }), () => false),
    read<WorkspacePluginDefaults>(route('/v1/workspaces/{tenant_id}/plugin-defaults', { tenant_id: tenantId }), () => false),
  ])
  if (catalogue.status === 'error') return catalogue
  if (defaults.status === 'error') return defaults
  const c = dataOf(catalogue)
  const d = dataOf(defaults)
  // `read` never answers empty here (`isEmpty` is false) nor loading; a
  // stale answer is an answer, as `view`'s is.
  if (c === null || d === null) return { status: 'loading', since: Date.now() }
  return { status: 'ok', data: { catalogue: c, defaults: d.defaults }, fetchedAt: Date.now() }
}

/** One managed plugin's settings form and the workspace's values. */
export async function loadPluginForm(tenantId: string, installId: string): Promise<Result<PluginConfigView>> {
  if (USE_FIXTURES) return { status: 'ok', data: fixtureForm(tenantId, installId), fetchedAt: Date.now() }
  return read<PluginConfigView>(route('/v1/workspaces/{tenant_id}/plugin-config/{install_id}', { tenant_id: tenantId, install_id: installId }), () => false)
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

/** The fixture dev server's managed catalogue: invented names, one default. */
function fixtureManaged(tenantId: string): ManagedPluginsRead {
  const install = (id: string, name: string, scope: 'platform' | 'workspace', version: string,
    hooks: number, allowed?: boolean): ManagedPluginInstall => ({
    install_id: id, name, display_name: name, scope, tenant_id: scope === 'workspace' ? tenantId : null, built_in: false,
    state: 'active', current_version: `v-${id.slice(2)}`, version, description: null, hosts: [],
    components: { skills: 0, commands: 0, agents: 0, hooks, mcp_servers: 0 }, ...(allowed === undefined ? {} : { allowed }),
  })
  return {
    catalogue: {
      scope: 'workspace', tenant_id: tenantId, built_in: [], can_install: true,
      platform_installs: [install('p-1a2b3c4d5e6f', 'rules-gate', 'platform', '1.0.0', 2, true),
        install('p-6f5e4d3c2b1a', 'trace-log', 'platform', '0.3.1', 1, false)],
      installs: [install('w-0a1b2c3d4e5f', 'lint-notes', 'workspace', '0.2.0', 1)],
    },
    defaults: ['p-1a2b3c4d5e6f'],
  }
}

function fixtureForm(tenantId: string, installId: string): PluginConfigView {
  const field = (key: string, type: PluginFormField['type'], dflt: PluginOverrideValue | null, sensitive = false): PluginFormField => ({
    key, source: 'user_config', type, kind: sensitive ? 'secret' : type === 'directory' || type === 'file' ? 'path' : 'value',
    title: null, description: null, required: false, multiple: false, sensitive, default: dflt,
  })
  return {
    tenant_id: tenantId, missing: [], can_edit: true,
    form: { install_id: installId, version_id: `v-${installId.slice(2)}`, fields: [
      field('strictness', 'string', 'normal'), field('max_findings', 'number', 20), field('dry_run', 'boolean', false),
      field('notes_dir', 'directory', null), field('api_key', 'string', null, true),
    ] },
    values: { strictness: { set: true, value: 'normal' }, max_findings: { set: false }, dry_run: { set: false },
      notes_dir: { set: false }, api_key: { set: false } },
  }
}

function dataOf<T>(r: Result<T> | null | undefined): T | null {
  return r != null && (r.status === 'ok' || r.status === 'stale') ? r.data : null
}

/** The builder's reads and what asks for them. */
export type StepPluginsReads = StepPluginsContext & { want: () => void; wantForm: (installId: string) => void }

/**
 * The reads the builder needs, asked for once, on `want()`: the catalogue,
 * then the workspace's managed plugins and defaults. Until then, nothing is
 * fetched. `wantForm` reads one plugin's settings form, once.
 */
export function useStepPlugins(): StepPluginsReads {
  const [ctx, setCtx] = useState<StepPluginsContext>(UNREAD_STEP_PLUGINS)
  const asked = useRef(false)
  const formsAsked = useRef(new Set<string>())
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
      setCtx((c) => ({ ...c, view }))
      const tenant = dataOf(view)?.tenant_id
      if (tenant === undefined) return
      void loadManagedPlugins(tenant).then((managed) => {
        if (live.current) setCtx((c) => ({ ...c, managed }))
      })
    })
  }, [])
  const tenant = dataOf(ctx.view)?.tenant_id
  const wantForm = useCallback((installId: string) => {
    if (tenant === undefined || formsAsked.current.has(installId)) return
    formsAsked.current.add(installId)
    void loadPluginForm(tenant, installId).then((form) => {
      if (live.current) setCtx((c) => ({ ...c, forms: { ...c.forms, [installId]: form } }))
    })
  }, [tenant])
  return { ...ctx, want, wantForm }
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

/** A default on the step card: its name, and on every step or off for this one; pressing it switches. */
export function DefaultChip({ name, label, on, onToggle }: {
  name: string
  label: string
  on: boolean
  onToggle: () => void
}) {
  const state = on ? 'every step' : 'off for this step'
  return (
    <button type="button" className={`psh-chip${on ? '' : ' is-bad'}`} aria-pressed={on} onClick={onToggle} title={name}
      aria-label={on ? `${label}: on for every step; turn it off for this step` : `${label}: off for this step; turn it back on`}>
      <span className="psh-chip-k">{label}</span>
      <span className="psh-chip-s">{state}</span>
    </button>
  )
}

/**
 * The step card's plugin row. `pick` is the step's built-in choice; `managed`
 * its managed plugins and the defaults it turns off; `ctx` the reads, asked
 * for when the row first draws (a step with a runner that runs an agent).
 * Before the catalogue is read every agent step offers "+ Add a plugin", and
 * the sheet says if its runner cannot load one. The workspace's every-step
 * defaults are chips, each switching off for this step.
 */
export function StepPluginRow({ stepId, profile, pick, managed = null, ctx, onPick, onManaged }: {
  stepId: string
  profile: string
  pick: StepPlugin | null
  managed?: StepManagedPlugins | null
  ctx: StepPluginsContext & { want: () => void; wantForm?: (installId: string) => void }
  onPick: (next: StepPlugin | null) => void
  onManaged?: (next: StepManagedPlugins) => void
}) {
  const [open, setOpen] = useState(false)
  const [openManaged, setOpenManaged] = useState(false)
  const { want } = ctx
  useEffect(() => { want() }, [want])
  const view = ctx.view !== null && (ctx.view.status === 'ok' || ctx.view.status === 'stale') ? ctx.view.data : null
  const read = dataOf(ctx.managed)
  const problem = pick !== null && view !== null ? stepPluginProblem(view, pick, profile) : null
  const loads = view === null || profileLoadsPlugins(view, profile)
  const show = () => { ctx.want(); setOpen(true) }
  const defaults = read !== null && loads ? stepDefaults(read, managed, pick?.name ?? null) : []
  // An added plugin stays drawn on a runner that cannot load it, so it can be
  // seen and removed rather than refused unseen.
  const added = read !== null ? Object.keys(managed?.added ?? {}).filter((id) => !read.defaults.includes(id)).sort() : []
  const offersManaged = read !== null && loads && onManaged !== undefined && (read.defaults.length > 0 || managedInstalls(read).length > 0)
  const managedProblem = onManaged === undefined ? null : (() => {
    const planned = stepManagedPlan(managed, pick?.name ?? null, ctx)
    return planned.ok ? null : planned.message
  })()
  return (
    <div className="psh-row">
      {(defaults.length > 0 || added.length > 0) && (
        <span className="c-btn-wrap" role="group" aria-label={`Plugins on ${stepId}`}>
          {defaults.map((d) => (
            <DefaultChip key={d.name} name={d.name} label={pluginLabel(read, d.name)} on={d.on}
              onToggle={() => onManaged?.(withDefault(managed, d.name, !d.on))} />
          ))}
          {added.map((id) => (
            <button key={id} type="button" className="psh-chip" onClick={() => setOpenManaged(true)}
              aria-label={`Change the managed plugins: ${pluginLabel(read, id)}`}>
              <span className="psh-chip-k">{pluginLabel(read, id)}</span>
              <span className="psh-chip-s">{Object.keys(managed?.added[id] ?? {}).length > 0 ? Object.keys(managed!.added[id]!).join(' · ') : 'added'}</span>
            </button>
          ))}
        </span>
      )}
      {pick !== null ? (
        <PluginChip pick={pick} bad={problem !== null} onClick={show} />
      ) : loads ? (
        <Button size="sm" kind="ghost" onClick={show} aria-label={`Add a plugin to ${stepId}`}>+ Add a plugin</Button>
      ) : (
        <span className="psh-dim psh-small">Plugins: this runner profile cannot load them</span>
      )}
      {offersManaged && (
        <Button size="sm" kind="ghost" onClick={() => setOpenManaged(true)} aria-label={`Managed plugins on ${stepId}`}>Plugins…</Button>
      )}
      {problem !== null && (
        <p className="psh-refusal" role="alert">
          <b>Refused at submission:</b> {problem}.{' '}
          {view !== null && !view.catalogue.find((e) => e.name === pick?.name)?.runner_profiles.includes(profile)
            ? 'Pick a runner that loads it, or remove the plugin.'
            : 'Change the plugin, or remove it.'}
        </p>
      )}
      {managedProblem !== null && read !== null && (
        <p className="psh-refusal" role="alert"><b>Refused at submission:</b> {managedProblem}.</p>
      )}
      {open && (
        <PluginSheet stepId={stepId} profile={profile} pick={pick} ctx={ctx}
          onUse={(next) => { onPick(next); setOpen(false) }} onClose={() => setOpen(false)} />
      )}
      {openManaged && read !== null && onManaged !== undefined && (
        <StepPluginsSheet stepId={stepId} managed={managed} builtIn={pick?.name ?? null} read={read} ctx={ctx}
          onUse={(next) => { onManaged(next); setOpenManaged(false) }} onClose={() => setOpenManaged(false)} />
      )}
    </div>
  )
}

/* --------------------------------------------------------------------------
   THE MANAGED SHEET: on for every step, added on this step, add
   (the owner's cards direction, 6 November 2026)
   -------------------------------------------------------------------------- */

/** The text a typed override is entered as, and back. */
function overrideText(v: PluginOverrideValue | undefined): string {
  if (v === undefined) return ''
  return Array.isArray(v) ? v.map(String).join('\n') : String(v)
}

function parseOverride(field: PluginFormField, text: string): PluginOverrideValue | undefined {
  const one = (t: string): PluginOverrideScalar => (field.type === 'number' && t.trim() !== '' ? Number(t) : field.type === 'boolean' ? t === 'true' : t)
  if (field.multiple) {
    const items = text.split('\n').filter((t) => t.trim() !== '')
    return items.length === 0 ? undefined : items.map(one)
  }
  return text === '' ? undefined : one(text)
}

/** One plugin's overridable settings: an empty box keeps the workspace's value. */
function OverrideFields({ installId, name, ctx, overrides, onChange }: {
  installId: string
  name: string
  ctx: StepPluginsContext
  overrides: Record<string, PluginOverrideValue>
  onChange: (next: Record<string, PluginOverrideValue>, bad: string[]) => void
}) {
  const form = ctx.forms?.[installId]
  const config = dataOf(form)
  const [texts, setTexts] = useState<Record<string, string>>({})
  if (form === undefined || form.status === 'loading') return <p className="psh-dim psh-small" role="status">…</p>
  if (config === null) {
    return <p className="psh-refusal" role="alert">{form.status === 'error' ? form.error.message : name}</p>
  }
  const fields = config.form.fields.filter(isOverridable)
  if (fields.length === 0) return <p className="psh-dim psh-small">—</p>
  const set = (field: PluginFormField, text: string) => {
    const nextTexts = { ...texts, [field.key]: text }
    setTexts(nextTexts)
    const next: Record<string, PluginOverrideValue> = {}
    const bad: string[] = []
    for (const f of fields) {
      const t = f.key in nextTexts ? nextTexts[f.key]! : overrideText(overrides[f.key])
      const v = parseOverride(f, t)
      if (v === undefined) continue
      if (overrideProblem(f, v) !== null) bad.push(f.key)
      else next[f.key] = v
    }
    onChange(next, bad)
  }
  return (
    <ul className="psh-choices" role="list" aria-label={`${name} settings for this step`}>
      {fields.map((f) => {
        const text = f.key in texts ? texts[f.key]! : overrideText(overrides[f.key])
        const parsed = parseOverride(f, text)
        const problem = parsed === undefined ? null : overrideProblem(f, parsed)
        const saved = config.values[f.key]
        const inherited = saved?.set ? saved.value : f.default
        const placeholder = inherited === undefined || inherited === null ? '' : overrideText(inherited)
        const id = `psh-ov-${installId}-${f.key}`
        return (
          <li key={f.key} className="psh-row">
            <span>
              <label htmlFor={id}><code>{f.title ?? f.key}</code></label>
              {f.description !== null && <InfoTip about={f.title ?? f.key} tip={f.description} />}
            </span>
            {f.type === 'boolean' && !f.multiple ? (
              <select id={id} className="c-inp" value={text} onChange={(e) => set(f, e.target.value)}>
                <option value="">{placeholder === '' ? '—' : placeholder}</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : f.multiple ? (
              <textarea id={id} className="c-inp is-mono" rows={3} value={text} placeholder={placeholder}
                aria-invalid={problem !== null || undefined} onChange={(e) => set(f, e.target.value)} />
            ) : (
              <input id={id} className={`c-inp${f.kind === 'path' ? ' is-mono' : ''}`} type={f.type === 'number' ? 'number' : 'text'}
                value={text} placeholder={placeholder} aria-invalid={problem !== null || undefined}
                onChange={(e) => set(f, e.target.value)} />
            )}
            {problem !== null && <p className="psh-refusal" role="alert">{problem}.</p>}
          </li>
        )
      })}
    </ul>
  )
}

/** One plugin in the managed sheet: name, version, its action, and its settings when opened. */
function ManagedRow({ install, id, label, sub, on, action, settings }: {
  install: ManagedPluginInstall | undefined
  id: string
  label: string
  sub: string
  on: boolean
  action: ReactNode
  settings: ReactNode
}) {
  return (
    <li>
      <div className={`psh-choice${on ? ' is-on' : ' is-off'}`}>
        <span style={{ flex: 1, minWidth: 0 }}>
          <b>{label}</b>{' '}{install?.version != null && <Chip title={install.current_version ?? id}>{install.version}</Chip>}
          <br /><span className="psh-dim psh-small">{sub}</span>
        </span>
        {action}
      </div>
      {settings}
    </li>
  )
}

export function StepPluginsSheet({ stepId, managed, builtIn, read, ctx, onUse, onClose }: {
  stepId: string
  managed: StepManagedPlugins | null
  builtIn: string | null
  read: ManagedPluginsRead
  ctx: StepPluginsContext & { wantForm?: (installId: string) => void }
  onUse: (next: StepManagedPlugins) => void
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
  const [draft, setDraft] = useState<StepManagedPlugins>(managed ?? NO_MANAGED)
  const [openSettings, setOpenSettings] = useState<string | null>(null)
  const [bad, setBad] = useState<Record<string, string[]>>({})
  const installs = managedInstalls(read)
  const find = (id: string) => installs.find((i) => i.install_id === id)
  const defaults = stepDefaults(read, draft, builtIn)
  const added = Object.keys(draft.added).filter((id) => !read.defaults.includes(id)).sort()
  const addable = installs.filter((i) => !read.defaults.includes(i.install_id) && !(i.install_id in draft.added))
  const subOf = (i: ManagedPluginInstall | undefined, scope: string) => {
    if (i === undefined) return scope
    const parts = Object.entries(i.components).filter(([, n]) => (n ?? 0) > 0).map(([k, n]) => `${n} ${k.replace('_', ' ')}`)
    return [...parts, scope].join(' · ')
  }
  const toggleSettings = (id: string) => {
    if (openSettings === id) { setOpenSettings(null); return }
    ctx.wantForm?.(id)
    setOpenSettings(id)
  }
  const settingsOf = (id: string, name: string) => openSettings !== id ? null : (
    <OverrideFields installId={id} name={name} ctx={ctx} overrides={draft.added[id] ?? {}}
      onChange={(next, wrong) => {
        setBad({ ...bad, [id]: wrong })
        setDraft((d) => ({ ...d, added: { ...d.added, [id]: next } }))
      }} />
  )
  const settingsButton = (id: string, name: string) => (
    <Button size="sm" kind="ghost" aria-expanded={openSettings === id} onClick={() => toggleSettings(id)}
      aria-label={`${name} settings for this step`}>Settings</Button>
  )
  const planned = stepManagedPlan(draft, builtIn, { ...ctx, managed: { status: 'ok', data: read, fetchedAt: 0 } })
  const wrong = Object.values(bad).some((keys) => keys.length > 0)
  return (
    <aside className="psh-sheet" role="dialog" aria-modal="false" aria-labelledby={`psh-${uid}-mh`}>
      <header className="psh-head">
        <div>
          <span className="psh-dim psh-small">Plugins for step</span>
          <h2 id={`psh-${uid}-mh`} ref={heading} tabIndex={-1}>{stepId}</h2>
        </div>
        <Button size="sm" kind="ghost" onClick={onClose}>Close</Button>
      </header>

      {defaults.length > 0 && (
        <section className="psh-part" aria-labelledby={`psh-${uid}-d`}>
          <h3 id={`psh-${uid}-d`}>On for every step <InfoTip about="defaults" tip="The workspace turns these on for every agent step. This step can turn one off." /></h3>
          <ul className="psh-choices" role="list">
            {defaults.map((d) => {
              const install = find(d.name)
              const label = pluginLabel(read, d.name)
              return (
                <ManagedRow key={d.name} install={install} id={d.name} label={label} on={d.on}
                  sub={subOf(install, install === undefined ? 'built-in' : install.scope)}
                  action={
                    <span className="c-btn-wrap">
                      {d.on && install !== undefined && settingsButton(d.name, label)}
                      <label className="psh-small">
                        <input type="checkbox" checked={!d.on} aria-label={`${label} off for this step`}
                          onChange={() => {
                            setDraft(withDefault(draft, d.name, !d.on))
                            if (d.on && openSettings === d.name) setOpenSettings(null)
                          }} /> off for this step
                      </label>
                    </span>
                  }
                  settings={d.on ? settingsOf(d.name, label) : null} />
              )
            })}
          </ul>
        </section>
      )}

      {added.length > 0 && (
        <section className="psh-part" aria-labelledby={`psh-${uid}-a`}>
          <h3 id={`psh-${uid}-a`}>Added on this step</h3>
          <ul className="psh-choices" role="list">
            {added.map((id) => {
              const install = find(id)
              const label = pluginLabel(read, id)
              return (
                <ManagedRow key={id} install={install} id={id} label={label} on sub={subOf(install, install?.scope ?? '')}
                  action={
                    <span className="c-btn-wrap">
                      {settingsButton(id, label)}
                      <Button size="sm" kind="ghost" aria-label={`Remove ${label} from this step`} onClick={() => {
                        const next = { ...draft.added }
                        delete next[id]
                        setDraft({ ...draft, added: next })
                        setBad({ ...bad, [id]: [] })
                        if (openSettings === id) setOpenSettings(null)
                      }}>Remove</Button>
                    </span>
                  }
                  settings={settingsOf(id, label)} />
              )
            })}
          </ul>
        </section>
      )}

      {addable.length > 0 && (
        <section className="psh-part" aria-labelledby={`psh-${uid}-n`}>
          <h3 id={`psh-${uid}-n`}>Add</h3>
          <ul className="psh-choices" role="list">
            {addable.map((i) => {
              const can = i.scope === 'workspace' || i.allowed === true
              const label = i.display_name ?? i.name
              return (
                <ManagedRow key={i.install_id} install={i} id={i.install_id} label={label} on={false}
                  sub={can ? subOf(i, i.scope) : managedWords.notAllowed(i.name)}
                  action={
                    <Button size="sm" kind="ghost" disabled={!can} aria-label={`Add ${label} to this step`}
                      onClick={() => setDraft({ ...draft, added: { ...draft.added, [i.install_id]: {} } })}>+ Add</Button>
                  }
                  settings={null} />
              )
            })}
          </ul>
        </section>
      )}

      <footer className="psh-foot">
        <div className="psh-acts">
          <Button kind="primary" disabled={!planned.ok || wrong} onClick={() => onUse(draft)}>Use on this step</Button>
        </div>
        {!planned.ok && <p className="psh-refusal psh-wide" role="alert">{planned.message}.</p>}
      </footer>
    </aside>
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
