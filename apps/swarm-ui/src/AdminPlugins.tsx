/**
 * ADMIN › PLUGINS (part of #2688; docs/design/managed-plugins.md §17 lane
 * 10a). The owner picked the cards direction on 6 November 2026: the platform
 * catalogue as a grid of cards, one per plugin, and Add plugin as a full page
 * (AdminPluginInstall.tsx). Every card is drawn from `GET
 * /v1/admin/plugin-catalogue`: the image's built-ins, the platform's
 * installs, and each workspace's private installs, read-only here.
 *
 * A PLATFORM ADMIN'S ALONE. The route is `admin_auth`; anyone else who types
 * the address is shown its 403 by `UrRegion`, never cards. The API is the
 * gate; this page only describes it.
 *
 * NOT HERE: the platform switches panel (lane 9 must merge first), a plugin's
 * own page (lane 10b, `adminPluginPath` is its address) and the routing
 * (lane 10e wires `ADMIN_PLUGINS_PATH` and `ADMIN_PLUGIN_ADD_PATH`).
 */
import { useState } from 'react'
import { loadPluginCatalogue, PLATFORM_PLUGIN_CATALOGUE, type CatalogueEntry, type CatalogueSource, type PluginCatalogue } from './api'
import { ButtonLink, Segmented } from './components'
import { InfoTip } from './People'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { formatDateTime } from './timezone'
import './styles/admin-plugins.css'

const ROUTE = 'GET /v1/admin/plugin-catalogue'

/** The addresses lane 10e routes: the catalogue, Add plugin, and a plugin's page (lane 10b). */
export const ADMIN_PLUGINS_PATH = '/admin/plugins'
export const ADMIN_PLUGIN_ADD_PATH = '/admin/plugins/add'
export function adminPluginPath(installId: string): string {
  return `${ADMIN_PLUGINS_PATH}/${encodeURIComponent(installId)}`
}

/** Where a card sits: baked into the image, the platform's catalogue, or one workspace's. */
export type CardScope = 'builtin' | 'platform' | 'workspace'

export function cardScope(e: CatalogueEntry): CardScope {
  return e.built_in ? 'builtin' : e.scope
}

const SCOPE_WORDS: Readonly<Record<CardScope, string>> = { builtin: 'Built in', platform: 'Platform', workspace: 'Workspace' }
const SCOPE_TIPS: Readonly<Record<CardScope, string>> = {
  builtin: 'Baked into the agent image; an image release updates it, and nobody can remove it.',
  platform: 'In the platform catalogue: a workspace runs it once its admin allows it.',
  workspace: 'One workspace’s private plugin, installed by its admin; only Remove is open to a platform admin.',
}

type Filter = 'all' | CardScope

/** Every card, built-ins first, then the platform's installs, then the workspaces'. */
export function catalogueCards(c: PluginCatalogue): CatalogueEntry[] {
  const byName = (a: CatalogueEntry, b: CatalogueEntry) => a.name.localeCompare(b.name)
  return [...[...c.built_in].sort(byName), ...[...c.installs].sort(byName), ...[...c.others].sort(byName)]
}

/** The source in one line: `owner/repo@ref`, the marketplace entry, the file, or the image. */
export function sourceLine(s: CatalogueSource): string {
  if (s.kind === 'image') return 'Agent image'
  if (s.kind === 'upload') return s.filename ?? 'Upload'
  const at = s.ref ?? (s.commit === null ? null : s.commit.slice(0, 12))
  const repo = `${s.repository ?? '—'}${at === null ? '' : `@${at}`}`
  if (s.kind === 'marketplace') return s.plugin === null ? repo : `${s.plugin} · ${repo}`
  return s.subdir === null ? repo : `${repo} · ${s.subdir}`
}

function initials(name: string): string {
  const parts = name.split(/[^A-Za-z0-9]+/).filter((p) => p !== '')
  const two = parts.length > 1 ? parts[0]!.charAt(0) + parts[1]!.charAt(0) : name.slice(0, 2)
  return two.toUpperCase()
}

function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many
}

export function Mono({ name, scope }: { name: string; scope: CardScope }) {
  return (
    <span className={`ap-mono is-${scope}`} aria-hidden>
      {initials(name)}
    </span>
  )
}

export function ScopeChip({ scope }: { scope: CardScope }) {
  return (
    <span className={`ap-scope is-${scope}`} title={SCOPE_TIPS[scope]}>
      {SCOPE_WORDS[scope]}
    </span>
  )
}

/** What the plugin carries, as counts: a hook is marked, since it runs code on every matching turn. */
export function Badges({ c }: { c: CatalogueEntry['components'] }) {
  return (
    <span className="ap-badges">
      {c.skills > 0 && (
        <span className="ap-badge">
          <b>{c.skills}</b> {plural(c.skills, 'skill', 'skills')}
        </span>
      )}
      {c.hooks > 0 && (
        <span className="ap-badge is-hook" title="A hook runs code on every matching turn">
          <b>{c.hooks}</b> {plural(c.hooks, 'hook', 'hooks')}
        </span>
      )}
      {c.mcp_servers > 0 && (
        <span className="ap-badge">
          <b>{c.mcp_servers}</b> MCP
        </span>
      )}
      {c.commands > 0 && (
        <span className="ap-badge">
          <b>{c.commands}</b> {plural(c.commands, 'command', 'commands')}
        </span>
      )}
      {c.agents > 0 && (
        <span className="ap-badge">
          <b>{c.agents}</b> {plural(c.agents, 'agent', 'agents')}
        </span>
      )}
    </span>
  )
}

function PluginCard({ e }: { e: CatalogueEntry }) {
  const scope = cardScope(e)
  const head = (
    <>
      <Mono name={e.name} scope={scope} />
      <span className="ap-card-id">
        <b>{e.display_name}</b>
        <code>{e.version ?? '—'}</code>
      </span>
      <ScopeChip scope={scope} />
    </>
  )
  return (
    <li className="ap-card" aria-label={e.display_name}>
      {e.built_in ? <div className="ap-card-top">{head}</div> : <a className="ap-card-top" href={adminPluginPath(e.install_id)}>{head}</a>}
      <div className="ap-card-mid">
        {e.description !== null && <span className="ap-card-desc">{e.description}</span>}
        <Badges c={e.components} />
        <span className="ap-card-src" title={sourceLine(e.source)}>
          {sourceLine(e.source)}
        </span>
      </div>
      <div className="ap-card-foot">
        <span className="ap-small">{scope === 'workspace' ? <code>{e.tenant_id ?? '—'}</code> : e.built_in ? `${e.runner_profiles.length} ${plural(e.runner_profiles.length, 'profile', 'profiles')}` : null}</span>
        <span className="ap-small ap-dim">{e.updated_at !== null ? formatDateTime(e.updated_at) : null}</span>
      </div>
    </li>
  )
}

function CatalogueGrid({ data }: { data: PluginCatalogue }) {
  const [filter, setFilter] = useState<Filter>('all')
  const cards = catalogueCards(data)
  const count = (f: Filter) => (f === 'all' ? cards.length : cards.filter((e) => cardScope(e) === f).length)
  const shown = cards.filter((e) => filter === 'all' || cardScope(e) === filter)
  return (
    <>
      <div className="ap-bar">
        <Segmented<Filter>
          label="Show"
          small
          value={filter}
          onChange={setFilter}
          options={(['all', 'builtin', 'platform', 'workspace'] as const).map((k) => ({ key: k, label: k === 'all' ? 'All' : SCOPE_WORDS[k], count: count(k) }))}
        />
        {data.can_install && (
          <ButtonLink kind="primary" size="sm" href={ADMIN_PLUGIN_ADD_PATH}>
            Add plugin
          </ButtonLink>
        )}
      </div>
      <ul className="ap-grid" role="list" aria-label="Plugins">
        {shown.map((e) => (
          <PluginCard key={e.install_id} e={e} />
        ))}
      </ul>
    </>
  )
}

export function AdminPluginsScreen() {
  const read = useUrRead(() => loadPluginCatalogue(PLATFORM_PLUGIN_CATALOGUE), 'admin-plugins')
  return (
    <div className="ap-page">
      <PageHead title="Plugins" info={<InfoTip about="Plugins" tip="The platform catalogue: plugins pinned once, by commit and sha256, and allowed per workspace by its admin." />}>
        <UrRefresh reads={[read]} />
      </PageHead>
      <UrRegion state={read.state} route={ROUTE} what="The plugin catalogue" onRetry={read.reload}>
        {(d) => <CatalogueGrid data={d} />}
      </UrRegion>
    </div>
  )
}
