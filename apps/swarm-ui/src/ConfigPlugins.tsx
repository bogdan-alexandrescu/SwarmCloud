/**
 * CONFIG › PLUGINS (lane 10c of docs/design/managed-plugins.md; part of
 * #2688). The owner picked the cards direction on 6 November 2026: the
 * workspace's catalogue as a grid of cards, a full Add plugin page with a
 * live manifest preview, and a page per plugin with its tabs.
 *
 * WHAT A CARD CHANGES. The workspace allows whole plugins, never single
 * skills (each step picks its own): Allowed is the signed `plugins.allowed`
 * block (`PUT /v1/workspaces/{tenant_id}/plugins`, a built-in by name, a
 * platform install by id), and Every step is the signed `plugins.defaults`
 * (`PUT .../plugin-defaults`), which submission merges into each step's
 * signed plugin set. A plugin this workspace installed is allowed by being
 * installed. Both lists are its owner's or admin's; a member reads them.
 *
 * WHAT A PLUGIN'S PAGE SHOWS. Overview: what the pinned copy ships (skills,
 * hooks, servers), its source and digest and what it needs. Settings: the
 * form generated from its fields (PluginSettingsForm.tsx), secrets by status
 * only. Versions and Usage are lane 10b's page (PluginDetail.tsx).
 *
 * A WORKSPACE INSTALL (its owner's or admin's): GitHub, a marketplace entry or
 * an uploaded archive; swarm-api fetches it into quarantine and a
 * network-less intake pins it. Nothing here waits on the intake: Check reads
 * its state once, and a pinned one draws its manifest.
 *
 * Every refusal is drawn with the API's own sentence. Explanations sit
 * behind an (i) (the owner's no-prose rule, 5 November 2026).
 */
import { useEffect, useState, type ReactNode } from 'react'
import {
  installWorkspacePlugin,
  loadPluginDefaults,
  loadWorkspaceCatalogue,
  loadWorkspacePlugins,
  loadWorkspacePluginVersions,
  readWorkspaceIntake,
  savePluginDefaults,
  saveWorkspacePlugins,
  uploadWorkspacePlugin,
  type PluginDefaults,
  type PluginEntry,
  type PluginIntake,
  type PluginScope,
  type PluginSourceBody,
  type PluginVersion,
  type WorkspaceCatalogue,
} from './api'
import { Banner, Button, Chip, Tabs } from './components'
import type { ApiError, Result } from './fetch'
import { InfoTip } from './People'
import { PluginSettingsForm } from './PluginSettingsForm'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { formatDateTime } from './timezone'
import type { WorkspacePlugins } from './types'
import './styles/plugins.css'

const ROUTE = 'GET /v1/workspaces/{tenant_id}/plugin-catalogue'

const LEAD =
  'Plugins this workspace may use. Allowed: a step may add it. Every step: it loads on each agent step unless the step turns it off. ' +
  'Each plugin runs the copy pinned when it was installed or updated; its settings and secrets are this workspace’s.'

const UNSIGNED = 'A list without a valid signature allows nothing.'

const SCOPE_WORDS: Readonly<Record<PluginScope, string>> = { built_in: 'Built in', platform: 'Platform', workspace: 'This workspace' }
const SCOPE_TIP: Readonly<Record<PluginScope, string>> = {
  built_in: 'Baked into the agent image: allowed or not, never removed.',
  platform: 'In the platform catalogue: this workspace allows it here.',
  workspace: 'Installed by this workspace’s owner or admin. No other workspace sees it.',
}

/** `check_step`'s refusal for a plugin the workspace has not allowed, word for word. */
export function notAllowedWords(name: string): string {
  return `this workspace has not allowed ${name}: its owner or admin allows it in the workspace’s plugins setting`
}

/** The name a plugin goes by in the Allowed and Every step lists: a built-in's name, an install's id. */
export function listKey(e: Pick<PluginEntry, 'scope' | 'name' | 'install_id'>): string {
  return e.scope === 'built_in' ? e.name : e.install_id
}

/** The one-time link's address (`pluginconfig.mint_link`): `/config/plugins/<install_id>?field=<key>&t=<token>`. */
export interface PluginLinkAt {
  install_id: string
  field: string
  token: string
}

export function pluginLinkOf(pathname: string, search: string): PluginLinkAt | null {
  const m = /^\/config\/plugins\/([pw]-[0-9a-f]{12})\/?$/.exec(pathname)
  if (m === null) return null
  const q = new URLSearchParams(search)
  const field = q.get('field')
  const token = q.get('t')
  if (field === null || field === '' || token === null || token === '') return null
  return { install_id: m[1]!, field, token }
}

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((n) => b.includes(n))
}

function day(iso: string | null): string {
  return iso === null ? 'when: not known' : formatDateTime(iso)
}

function short(sha: string | null): string {
  return sha === null ? 'not known' : sha.replace(/^sha256:/, '').slice(0, 12)
}

function monogram(name: string): string {
  const parts = name.split(/[-_.\s]+/).filter(Boolean)
  return (parts.length > 1 ? parts[0]![0]! + parts[1]![0]! : name.slice(0, 2)).toLowerCase()
}

function sourceWords(e: PluginEntry): string {
  if (e.scope === 'built_in') return 'Agent image'
  const s = e.source ?? {}
  const repo = typeof s.repository === 'string' ? s.repository : null
  const file = typeof s.filename === 'string' ? s.filename : null
  return repo ?? file ?? 'not known'
}

interface PageData {
  plugins: WorkspacePlugins
  catalogue: WorkspaceCatalogue
  defaults: PluginDefaults
}

/** The page's one read: the Allowed list names the workspace, then its catalogue and its every-step list. */
async function loadPage(): Promise<Result<PageData>> {
  const plugins = await loadWorkspacePlugins()
  if (plugins.status !== 'ok' && plugins.status !== 'stale') {
    return plugins.status === 'error' ? plugins : { status: 'loading', since: Date.now() }
  }
  const tenant = plugins.data.tenant_id
  const [catalogue, defaults] = await Promise.all([loadWorkspaceCatalogue(tenant), loadPluginDefaults(tenant)])
  if (catalogue.status === 'error') return catalogue
  if (defaults.status === 'error') return defaults
  if ((catalogue.status !== 'ok' && catalogue.status !== 'stale') || (defaults.status !== 'ok' && defaults.status !== 'stale')) {
    return { status: 'loading', since: Date.now() }
  }
  return { status: 'ok', fetchedAt: Date.now(), data: { plugins: plugins.data, catalogue: catalogue.data, defaults: defaults.data } }
}

type View = { kind: 'catalogue' } | { kind: 'add' } | { kind: 'plugin'; id: string; tab: 'overview' | 'settings' }

export function ConfigPluginsScreen({ link = null }: { link?: PluginLinkAt | null }) {
  const read = useUrRead(loadPage, 'plugins')
  const [view, setView] = useState<View>(link === null ? { kind: 'catalogue' } : { kind: 'plugin', id: link.install_id, tab: 'settings' })
  return (
    <div className="plg-page">
      <UrRegion state={read.state} route={ROUTE} what="The workspace's plugins" plural onRetry={read.reload}>
        {(d) => {
          if (view.kind === 'add') {
            return <AddPage data={d} onBack={() => setView({ kind: 'catalogue' })} onInstalled={read.reload}
              onOpen={(id) => { read.reload(); setView({ kind: 'plugin', id, tab: 'settings' }) }} />
          }
          if (view.kind === 'plugin') {
            const entry = d.catalogue.entries.find((e) => e.install_id === view.id)
            if (entry !== undefined) {
              return <PluginPage data={d} entry={entry} tab={view.tab} link={link !== null && link.install_id === entry.install_id ? link : null}
                onTab={(tab) => setView({ ...view, tab })} onBack={() => setView({ kind: 'catalogue' })} />
            }
          }
          return (
            <>
              <PageHead title="Plugins" info={<InfoTip about="plugins" tip={LEAD} />}>
                {d.catalogue.can_install && <Button kind="primary" onClick={() => setView({ kind: 'add' })}>Add plugin</Button>}
                <UrRefresh reads={[read]} />
              </PageHead>
              <Catalogue data={d} onSaved={read.reload} onOpen={(id) => setView({ kind: 'plugin', id, tab: 'overview' })} />
            </>
          )
        }}
      </UrRegion>
    </div>
  )
}

/* ---------------------------------------------------------------- bits */

function Switch({ on, label, onChange, inert = false, changed = false, title }: {
  on: boolean
  label: string
  onChange?: (v: boolean) => void
  inert?: boolean
  changed?: boolean
  title?: string
}) {
  return (
    <label className={`plg-switch${changed ? ' is-changed' : ''}${inert ? ' is-inert' : ''}`} title={title}>
      <input type="checkbox" role="switch" checked={on} disabled={inert} aria-label={label} onChange={(e) => onChange?.(e.target.checked)} />
      <span className="plg-switch-track" aria-hidden><span /></span>
      <span className="plg-switch-word">{on ? 'On' : 'Off'}</span>
    </label>
  )
}

function Mono({ e }: { e: Pick<PluginEntry, 'name' | 'scope'> }) {
  return <span className={`plg-mono is-${e.scope}`} aria-hidden>{monogram(e.name)}</span>
}

function ScopeChip({ scope }: { scope: PluginScope }) {
  return <span className={`plg-scope is-${scope}`} title={SCOPE_TIP[scope]}>{SCOPE_WORDS[scope]}</span>
}

function skillCount(e: PluginEntry): number {
  if (e.components !== null) return e.components.skills
  return new Set(Object.values(e.skills_by_mode ?? {}).flat()).size
}

function Badges({ e }: { e: PluginEntry }) {
  const skills = skillCount(e)
  const c = e.components
  return (
    <span className="plg-badges">
      {skills > 0 && <span className="plg-badge"><b>{skills}</b> {skills === 1 ? 'skill' : 'skills'}</span>}
      {c !== null && c.hooks > 0 && <span className="plg-badge is-hook" title="Hooks run code on every matching turn"><b>{c.hooks}</b> {c.hooks === 1 ? 'hook' : 'hooks'}</span>}
      {c !== null && c.mcp_servers > 0 && <span className="plg-badge"><b>{c.mcp_servers}</b> MCP</span>}
      {c !== null && c.commands > 0 && <span className="plg-badge"><b>{c.commands}</b> {c.commands === 1 ? 'command' : 'commands'}</span>}
    </span>
  )
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="plg-facts">
      {rows.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}
    </dl>
  )
}

/* ---------------------------------------------------------------- catalogue */

type Filter = 'all' | PluginScope
type Saved = { kind: 'signed' } | { kind: 'unsigned'; asked: string[] } | { kind: 'saved' }

function Catalogue({ data, onSaved, onOpen }: { data: PageData; onSaved: () => void; onOpen: (id: string) => void }) {
  const { plugins, catalogue, defaults } = data
  const canEdit = plugins.can_edit && defaults.can_edit
  const [allowed, setAllowed] = useState<string[]>(plugins.allowed)
  const [every, setEvery] = useState<string[]>(defaults.defaults)
  useEffect(() => { setAllowed(plugins.allowed); setEvery(defaults.defaults) }, [plugins, defaults])
  const [filter, setFilter] = useState<Filter>('all')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState<Saved | null>(null)

  const isAllowed = (e: PluginEntry, list: readonly string[]) => e.scope === 'workspace' || list.includes(listKey(e))
  const changed = catalogue.entries.filter((e) =>
    isAllowed(e, allowed) !== isAllowed(e, plugins.allowed) || every.includes(listKey(e)) !== defaults.defaults.includes(listKey(e)))
  const turnedOff = catalogue.entries.filter((e) => !isAllowed(e, allowed) && isAllowed(e, plugins.allowed))
  const counts: Record<Filter, number> = {
    all: catalogue.entries.length,
    built_in: catalogue.entries.filter((e) => e.scope === 'built_in').length,
    platform: catalogue.entries.filter((e) => e.scope === 'platform').length,
    workspace: catalogue.entries.filter((e) => e.scope === 'workspace').length,
  }
  const shown = catalogue.entries.filter((e) => filter === 'all' || e.scope === filter)

  function flipAllowed(e: PluginEntry, on: boolean) {
    if (!canEdit || e.scope === 'workspace') return
    setSaved(null)
    setFailed(null)
    const k = listKey(e)
    setAllowed((was) => (on ? [...was.filter((n) => n !== k), k] : was.filter((n) => n !== k)))
    // A plugin no longer allowed is no longer on for every step.
    if (!on) setEvery((was) => was.filter((n) => n !== k))
  }

  function flipEvery(e: PluginEntry, on: boolean) {
    if (!canEdit) return
    setSaved(null)
    setFailed(null)
    const k = listKey(e)
    setEvery((was) => (on ? [...was.filter((n) => n !== k), k] : was.filter((n) => n !== k)))
  }

  async function save() {
    if (busy || !canEdit) return
    setBusy(true)
    setFailed(null)
    let outcome: Saved = { kind: 'saved' }
    // The Allowed list first: a plugin made a default must be allowed when the defaults are checked.
    if (!sameSet(allowed, plugins.allowed)) {
      const asked = [...new Set(allowed)].sort()
      const r = await saveWorkspacePlugins(plugins.tenant_id, asked)
      if (r.status === 'error') {
        setBusy(false)
        setFailed(r.error)
        return
      }
      // The answer reads only a signed list: asking for plugins and reading none back means it was stored unsigned.
      if (r.status === 'ok' || r.status === 'stale') {
        outcome = asked.length > 0 && !sameSet(r.data.allowed, asked) ? { kind: 'unsigned', asked } : { kind: 'signed' }
      }
    }
    if (!sameSet(every, defaults.defaults)) {
      const r = await savePluginDefaults(plugins.tenant_id, [...new Set(every)].sort())
      if (r.status === 'error') {
        setBusy(false)
        setFailed(r.error)
        onSaved()
        return
      }
      if (outcome.kind === 'saved') outcome = { kind: 'signed' }
    }
    setBusy(false)
    setSaved(outcome)
    onSaved()
  }

  const nameOf = (k: string) => catalogue.entries.find((e) => listKey(e) === k)?.name ?? k
  return (
    <div className="plg-body">
      {saved?.kind === 'signed' && <Banner tone="info" title="Saved and signed">From the next submission.</Banner>}
      {saved?.kind === 'saved' && <Banner tone="info" title="Saved">From the next submission.</Banner>}
      {saved?.kind === 'unsigned' && (
        <Banner tone="warn" title="Saved, not signed">
          {saved.asked.map(nameOf).join(', ')}: allows nothing until the deployment has its settings key.
        </Banner>
      )}

      <div className="plg-bar">
        <div className="plg-filters" role="group" aria-label="Show">
          {(['all', 'built_in', 'platform', 'workspace'] as const).map((k) => (
            <button key={k} type="button" className={`plg-filter${filter === k ? ' is-on' : ''}`} aria-pressed={filter === k} onClick={() => setFilter(k)}>
              {k === 'all' ? 'All' : SCOPE_WORDS[k]} <em>{counts[k]}</em>
            </button>
          ))}
        </div>
        {canEdit ? (
          <span className="plg-dim plg-small">
            {plugins.set_at === null && plugins.set_by === null ? 'Never changed' : `${plugins.set_by ?? 'who: not known'} · ${day(plugins.set_at)}`}
            <InfoTip about="the setting" tip={`The last change to the Allowed list. ${UNSIGNED}`} end />
          </span>
        ) : (
          <span className="plg-tick">
            View only
            <InfoTip about="who can change it" tip="Only this workspace’s owner or an admin chooses its plugins and their settings." end />
          </span>
        )}
      </div>

      {shown.length === 0 ? (
        <p className="plg-dim plg-none">No plugin</p>
      ) : (
        <ul className="plg-grid" role="list" aria-label="The plugin catalogue">
          {shown.map((e) => {
            const on = isAllowed(e, allowed)
            const wasOn = isAllowed(e, plugins.allowed)
            const ev = every.includes(listKey(e))
            return (
              <li key={e.install_id} className={`plg-card${on ? '' : ' is-off'}`}>
                <button type="button" className="plg-card-top" onClick={() => onOpen(e.install_id)} aria-label={`Open ${e.name}`}>
                  <Mono e={e} />
                  <span className="plg-card-id">
                    <b>{e.name}</b>
                    <code title={e.version ?? undefined}>{e.version ?? 'version not known'}</code>
                  </span>
                  <ScopeChip scope={e.scope} />
                </button>
                <div className="plg-card-mid">
                  {e.description !== null && <span className="plg-card-desc">{e.description}</span>}
                  <Badges e={e} />
                  <span className="plg-card-src" title={sourceWords(e)}>{sourceWords(e)}</span>
                </div>
                <div className="plg-card-foot">
                  <span className="plg-toggle">
                    <span className="plg-small">Allowed</span>
                    <Switch on={on} label={`${e.name}: allowed in this workspace`} inert={!canEdit || e.scope === 'workspace'} changed={on !== wasOn}
                      title={e.scope === 'workspace' ? SCOPE_TIP.workspace : undefined} onChange={(v) => flipAllowed(e, v)} />
                  </span>
                  <span className="plg-toggle">
                    <span className="plg-small">Every step</span>
                    <Switch on={ev} label={`${e.name}: on for every step`} inert={!canEdit || !on} changed={ev !== defaults.defaults.includes(listKey(e))}
                      onChange={(v) => flipEvery(e, v)} />
                  </span>
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {canEdit && (changed.length > 0 || failed !== null) && (
        <div className="plg-savebar" role="region" aria-label="Changes not saved">
          <div className="plg-savebar-t">
            <b>{changed.map((e) => e.name).join(', ')}</b>
            {turnedOff.length > 0 && (
              <span className="plg-small">
                Refused from the next submission
                <InfoTip about="the refusal" tip={`A step that names ${turnedOff.map((e) => e.name).join(' or ')} is refused with ${turnedOff.map((e) => `“${notAllowedWords(e.name)}”`).join(' and ')}. Steps already running keep what they loaded.`} />
              </span>
            )}
            {failed !== null && <span className="plg-failed" role="alert">Not saved: {failed.message}</span>}
          </div>
          <span className="plg-savebar-b">
            <Button kind="ghost" disabled={busy} onClick={() => { setAllowed(plugins.allowed); setEvery(defaults.defaults); setFailed(null) }}>Discard</Button>
            <Button kind="primary" busy={busy} disabled={changed.length === 0} onClick={() => void save()}>Save and sign</Button>
          </span>
        </div>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- one plugin */

function PluginPage({ data, entry: e, tab, link, onTab, onBack }: {
  data: PageData
  entry: PluginEntry
  tab: 'overview' | 'settings'
  link: PluginLinkAt | null
  onTab: (t: 'overview' | 'settings') => void
  onBack: () => void
}) {
  const allowed = e.scope === 'workspace' || data.plugins.allowed.includes(listKey(e))
  const every = data.defaults.defaults.includes(listKey(e))
  const tabs = e.scope === 'built_in' ? (['overview'] as const) : (['overview', 'settings'] as const)
  const current = tabs.includes(tab as never) ? tab : 'overview'
  return (
    <>
      <nav className="plg-crumb" aria-label="Breadcrumb">
        <button type="button" className="plg-linkish" onClick={onBack}>Plugins</button>
        <span aria-hidden>/</span>
        <span>{e.name}</span>
      </nav>
      <header className="plg-phead">
        <Mono e={e} />
        <div className="plg-phead-t">
          <h1>{e.name} {e.version !== null && <code>{e.version}</code>}</h1>
          <span className="plg-phead-m">
            <ScopeChip scope={e.scope} />
            <Badges e={e} />
            {allowed ? <Chip>Allowed</Chip> : <Chip faint>Not allowed</Chip>}
            {every && <Chip>Every step</Chip>}
          </span>
        </div>
      </header>
      <Tabs className="plg-tabs" label={e.name} current={current} onSelect={(k) => onTab(k === 'settings' ? 'settings' : 'overview')}
        tabs={tabs.map((t) => ({ key: t, label: t === 'overview' ? 'Overview' : 'Settings' }))} />
      {current === 'settings'
        ? <section className="plg-panel"><PluginSettingsForm tenantId={data.plugins.tenant_id} installId={e.install_id} name={e.name} link={link === null ? null : { field: link.field, token: link.token }} /></section>
        : <Overview data={data} entry={e} />}
    </>
  )
}

function Overview({ data, entry: e }: { data: PageData; entry: PluginEntry }) {
  const [versions, setVersions] = useState<Result<PluginVersion[]> | null>(null)
  useEffect(() => {
    if (e.scope !== 'workspace') return
    let live = true
    void loadWorkspacePluginVersions(data.plugins.tenant_id, e.install_id).then((r) => { if (live) setVersions(r) })
    return () => { live = false }
  }, [data.plugins.tenant_id, e.install_id, e.scope])
  const current = versions !== null && (versions.status === 'ok' || versions.status === 'stale') ? versions.data.find((v) => v.current) ?? null : null
  return (
    <div className="plg-ov">
      <div className="plg-ov-main">
        {e.skills_by_mode !== null && Object.entries(e.skills_by_mode).map(([mode, skills]) => (
          <section key={mode} className="plg-panel" aria-label={`${e.name}, ${mode} skills`}>
            <header className="plg-panel-h"><h2>{mode[0]!.toUpperCase() + mode.slice(1)} <em>{skills.length}</em></h2></header>
            <ul className="plg-skills" role="list">{skills.map((s) => <li key={s}><code>{s}</code></li>)}</ul>
          </section>
        ))}
        {current !== null && <Components v={current} />}
        {versions !== null && versions.status === 'error' && (
          <Banner tone="warn" title="Its pinned copy was not read">{versions.error.message}</Banner>
        )}
      </div>
      <aside className="plg-panel plg-ov-side" aria-label="About this copy">
        <Facts rows={[
          ['Source', <code className="plg-wrap">{sourceWords(e)}</code>],
          ...(current?.license ? [['License', current.license] as [string, ReactNode]] : []),
          ['Pinned', e.scope === 'built_in' ? <code>{e.version ?? 'not known'}</code> : <PinnedFacts entry={e} v={current} />],
          ...(e.scope === 'built_in' ? [] : [['Updated', <>{e.updated_by ?? 'who: not known'} · {day(e.updated_at)}</>] as [string, ReactNode]]),
          ...(e.runner_profiles.length > 0 || (current?.profiles.length ?? 0) > 0
            ? [['Runs on', <>{(e.runner_profiles.length > 0 ? e.runner_profiles : current!.profiles).map((r) => <Chip key={r}>{r}</Chip>)}</>] as [string, ReactNode]]
            : []),
          ...(e.hosts.length > 0 || (current?.runtimes.length ?? 0) > 0
            ? [['Needs', <>{current?.runtimes.map((r) => <Chip key={r}>{r}</Chip>)}{e.hosts.map((h) => <Chip key={h}>net: {h}</Chip>)}</>] as [string, ReactNode]]
            : []),
        ]} />
      </aside>
    </div>
  )
}

function PinnedFacts({ entry: e, v }: { entry: PluginEntry; v: PluginVersion | null }) {
  const commit = typeof v?.source.commit === 'string' && v.source.commit !== 'upload' ? v.source.commit : typeof e.source?.commit === 'string' ? e.source.commit : null
  const sha = v?.runnable_sha256 ?? e.runnable_sha256
  return (
    <span className="plg-pin">
      {commit !== null && commit !== 'upload' && <span>commit <code>{commit.slice(0, 12)}</code></span>}
      <span>sha256 <code title={sha ?? undefined}>{short(sha)}</code></span>
    </span>
  )
}

function HookRow({ h }: { h: PluginVersion['hooks'][number] }) {
  return (
    <li className="plg-hook">
      <span className="plg-hook-ev"><b>{h.event}</b>{h.matcher !== null && <code>{h.matcher}</code>}</span>
      <code className="plg-hook-cmd">{h.command}</code>
      <span className="plg-runs" title="A hook is code the agent runs on every matching turn">Runs every turn</span>
    </li>
  )
}

/** What a pinned version ships: skills, hooks, MCP servers, commands and agents. */
function Components({ v }: { v: PluginVersion }) {
  return (
    <>
      {v.skills.length > 0 && (
        <section className="plg-panel" aria-label="Skills">
          <header className="plg-panel-h"><h2>Skills <em>{v.skills.length}</em></h2></header>
          <ul className="plg-skills" role="list">{v.skills.map((s) => <li key={s}><code>{s}</code></li>)}</ul>
        </section>
      )}
      {v.hooks.length > 0 && (
        <section className="plg-panel" aria-label="Hooks">
          <header className="plg-panel-h">
            <h2>Hooks <em>{v.hooks.length}</em></h2>
            <InfoTip about="hooks" tip="Hooks are code the agent runs itself on the event they name, on every matching turn, under the step’s identity." />
          </header>
          <ul className="plg-hooks" role="list">{v.hooks.map((h, i) => <HookRow key={`${h.event}-${i}`} h={h} />)}</ul>
        </section>
      )}
      {v.mcp_servers.length > 0 && (
        <section className="plg-panel" aria-label="MCP servers">
          <header className="plg-panel-h"><h2>MCP servers <em>{v.mcp_servers.length}</em></h2></header>
          <ul className="plg-skills" role="list">
            {v.mcp_servers.map((s) => <li key={s.name}><code>{s.name}</code>{s.url_host !== null && <Chip>net: {s.url_host}</Chip>}</li>)}
          </ul>
        </section>
      )}
      {v.commands.length + v.agents.length > 0 && (
        <section className="plg-panel" aria-label="Commands and agents">
          <header className="plg-panel-h"><h2>Commands and agents <em>{v.commands.length + v.agents.length}</em></h2></header>
          <ul className="plg-skills" role="list">{[...v.commands, ...v.agents].map((s, i) => <li key={`${s}-${i}`}><code>{s}</code></li>)}</ul>
        </section>
      )}
    </>
  )
}

/* ---------------------------------------------------------------- add plugin */

type Source = 'github' | 'marketplace' | 'upload'
const SOURCE_WORDS: Readonly<Record<Source, string>> = { github: 'GitHub', marketplace: 'Marketplace', upload: 'Upload' }
const REPOSITORY = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/

/** The overlay box's text as the API takes it: empty is none, else one JSON object. */
export function overlayOf(text: string): Record<string, unknown> | null | 'invalid' {
  if (text.trim() === '') return null
  try {
    const v: unknown = JSON.parse(text)
    return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : 'invalid'
  } catch {
    return 'invalid'
  }
}

function AddPage({ data, onBack, onInstalled, onOpen }: {
  data: PageData
  onBack: () => void
  onInstalled: () => void
  onOpen: (installId: string) => void
}) {
  const tenant = data.plugins.tenant_id
  const [src, setSrc] = useState<Source>('github')
  const [repository, setRepository] = useState('')
  const [ref, setRef] = useState('')
  const [subdir, setSubdir] = useState('')
  const [entryName, setEntryName] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [overlay, setOverlay] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [intake, setIntake] = useState<PluginIntake | null>(null)
  const [pinned, setPinned] = useState<PluginVersion | null>(null)

  const parsedOverlay = overlayOf(overlay)
  const repoBad = src !== 'upload' && repository !== '' && !REPOSITORY.test(repository.trim())
  const ready = parsedOverlay !== 'invalid' && (src === 'upload'
    ? file !== null
    : REPOSITORY.test(repository.trim()) && ref.trim() !== '' && (src === 'github' || entryName.trim() !== ''))

  async function landed(r: Result<PluginIntake>) {
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    if (r.status !== 'ok' && r.status !== 'stale') return
    setIntake(r.data)
    if (r.data.state === 'pinned' && r.data.install_id !== null) {
      onInstalled()
      const v = await loadWorkspacePluginVersions(tenant, r.data.install_id)
      if (v.status === 'ok' || v.status === 'stale') setPinned(v.data.find((x) => x.current) ?? v.data[0] ?? null)
      else if (v.status === 'error') setFailed(v.error)
    }
  }

  async function install() {
    if (busy || !ready || parsedOverlay === 'invalid') return
    setBusy(true)
    setFailed(null)
    setIntake(null)
    setPinned(null)
    let r: Result<PluginIntake>
    if (src === 'upload') {
      r = await uploadWorkspacePlugin(tenant, file!, subdir.trim(), parsedOverlay)
    } else {
      const source: PluginSourceBody = src === 'github'
        ? { kind: 'github', repository: repository.trim(), ref: ref.trim(), subdir: subdir.trim() }
        : { kind: 'marketplace', repository: repository.trim(), ref: ref.trim(), plugin: entryName.trim() }
      r = await installWorkspacePlugin(tenant, source, parsedOverlay)
    }
    await landed(r)
    setBusy(false)
  }

  async function check() {
    if (busy || intake === null) return
    setBusy(true)
    setFailed(null)
    await landed(await readWorkspaceIntake(tenant, intake.intake_id))
    setBusy(false)
  }

  if (!data.catalogue.can_install) {
    return (
      <>
        <nav className="plg-crumb" aria-label="Breadcrumb"><button type="button" className="plg-linkish" onClick={onBack}>Plugins</button></nav>
        <Banner tone="warn" title="Only this workspace’s owner or an admin installs a plugin" />
      </>
    )
  }

  const pending = intake !== null && (intake.state === 'running' || intake.state === 'awaiting_upload')
  return (
    <>
      <nav className="plg-crumb" aria-label="Breadcrumb">
        <button type="button" className="plg-linkish" onClick={onBack}>Plugins</button>
        <span aria-hidden>/</span>
        <span>Add plugin</span>
      </nav>
      <PageHead title="Add plugin" info={<InfoTip about="adding a plugin" tip="Installed into this workspace only. swarm-api fetches the source into quarantine; an intake with no network unpacks, checks and pins it. Steps run the pinned copy." />} />
      <div className="plg-add">
        <section className="plg-panel" aria-label="Source">
          <div className="plg-srctabs" role="tablist" aria-label="Source">
            {(['github', 'marketplace', 'upload'] as const).map((k) => (
              <button key={k} type="button" role="tab" aria-selected={src === k} className={`plg-srctab${src === k ? ' is-on' : ''}`} onClick={() => setSrc(k)}>
                {SOURCE_WORDS[k]}
              </button>
            ))}
          </div>
          {src !== 'upload' && (
            <>
              <div className="plg-field">
                <label className="plg-lbl" htmlFor="plg-repo">
                  {src === 'marketplace' ? 'Marketplace repository' : 'Repository'}
                  <InfoTip about="the repository" tip="owner/name on GitHub. A private one is read through this workspace’s GitHub connection, once." />
                </label>
                <input id="plg-repo" className={`c-inp is-mono${repoBad ? ' is-bad' : ''}`} value={repository} spellCheck={false} aria-invalid={repoBad || undefined}
                  onChange={(ev) => setRepository(ev.target.value)} />
              </div>
              <div className="plg-row2">
                <div className="plg-field">
                  <label className="plg-lbl" htmlFor="plg-ref">Tag, branch or commit</label>
                  <input id="plg-ref" className="c-inp is-mono" value={ref} spellCheck={false} onChange={(ev) => setRef(ev.target.value)} />
                </div>
                {src === 'github' ? (
                  <div className="plg-field">
                    <label className="plg-lbl" htmlFor="plg-sub">Folder <span className="plg-dim">optional</span></label>
                    <input id="plg-sub" className="c-inp is-mono" value={subdir} spellCheck={false} onChange={(ev) => setSubdir(ev.target.value)} />
                  </div>
                ) : (
                  <div className="plg-field">
                    <label className="plg-lbl" htmlFor="plg-entry">Plugin</label>
                    <input id="plg-entry" className="c-inp is-mono" value={entryName} spellCheck={false} onChange={(ev) => setEntryName(ev.target.value)} />
                  </div>
                )}
              </div>
            </>
          )}
          {src === 'upload' && (
            <div className="plg-row2">
              <div className="plg-field">
                <label className="plg-lbl" htmlFor="plg-file">Archive <span className="plg-dim">.zip, .tar.gz, .tgz</span></label>
                <input id="plg-file" className="c-inp" type="file" accept=".zip,.tar.gz,.tgz,application/zip,application/gzip"
                  onChange={(ev) => setFile(ev.target.files?.[0] ?? null)} />
              </div>
              <div className="plg-field">
                <label className="plg-lbl" htmlFor="plg-usub">Folder <span className="plg-dim">optional</span></label>
                <input id="plg-usub" className="c-inp is-mono" value={subdir} spellCheck={false} onChange={(ev) => setSubdir(ev.target.value)} />
              </div>
            </div>
          )}
          <div className="plg-field">
            <label className="plg-lbl" htmlFor="plg-overlay">
              Overlay <span className="plg-dim">optional</span>
              <InfoTip about="the overlay" tip="JSON: plugin, hooks, settings and requires, for a plugin with no plugin.json or no userConfig. It adds to what upstream ships and never replaces its hooks." />
            </label>
            <textarea id="plg-overlay" className={`c-inp is-mono plg-code${parsedOverlay === 'invalid' ? ' is-bad' : ''}`} rows={4} value={overlay} spellCheck={false}
              aria-invalid={parsedOverlay === 'invalid' || undefined} onChange={(ev) => setOverlay(ev.target.value)} />
            {parsedOverlay === 'invalid' && <span className="plg-err">Not one JSON object</span>}
          </div>
          <div className="plg-src-acts">
            <Button kind="primary" busy={busy && !pending} disabled={!ready || pending} onClick={() => void install()}>Install</Button>
          </div>
        </section>
        <section className="plg-panel plg-preview" aria-label="What the plugin contains" aria-busy={pending || undefined}>
          {failed !== null && <Banner tone="bad" title="Not installed">{failed.message}</Banner>}
          {intake?.state === 'refused' && (
            <Banner tone="bad" title="Not installed">
              {intake.refusal?.sentence ?? intake.refusal?.code ?? 'Refused'}
            </Banner>
          )}
          {pending && (
            <div className="plg-resolving">
              <span className="plg-small"><span className="c-spin" aria-hidden /> {intake.state === 'awaiting_upload' ? 'Waiting for the archive' : 'Pinning'}</span>
              <span className="plg-skel" aria-hidden><span /><span /><span /></span>
              <Button size="sm" kind="ghost" busy={busy} onClick={() => void check()}>Check</Button>
            </div>
          )}
          {intake === null && failed === null && <span className="plg-skel" aria-hidden><span /><span /><span /></span>}
          {pinned !== null && intake?.install_id != null && (
            <ManifestPreview v={pinned} name={pinned.version === null ? 'Pinned' : `${pinned.version}`} onSettings={() => onOpen(intake.install_id!)} />
          )}
        </section>
      </div>
    </>
  )
}

function ManifestPreview({ v, name, onSettings }: { v: PluginVersion; name: string; onSettings: () => void }) {
  return (
    <div className="plg-man">
      <header className="plg-man-h">
        <span className="plg-card-id"><b>Installed</b><code>{name}</code></span>
        {v.license !== null && <Chip>{v.license}</Chip>}
        <Button size="sm" kind="primary" onClick={onSettings}>Settings</Button>
      </header>
      {v.description !== null && <span className="plg-card-desc">{v.description}</span>}
      <Components v={v} />
      <Facts rows={[
        ['Settings', <><b>{v.user_config}</b>&nbsp;userConfig</>],
        ...(v.runtimes.length + v.hosts.length > 0
          ? [['Needs', <>{v.runtimes.map((r) => <Chip key={r}>{r}</Chip>)}{v.hosts.map((h) => <Chip key={h}>net: {h}</Chip>)}</>] as [string, ReactNode]]
          : []),
        ['Pinned', <span className="plg-pin">
          {typeof v.source.commit === 'string' && v.source.commit !== 'upload' && <span>commit <code>{v.source.commit.slice(0, 12)}</code></span>}
          <span>sha256 <code title={v.runnable_sha256 ?? undefined}>{short(v.runnable_sha256)}</code></span>
        </span>],
      ]} />
    </div>
  )
}
