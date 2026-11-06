import { Fragment, useContext, useEffect, useId, useRef, useState, type FormEvent, type MouseEvent, type MutableRefObject } from 'react'
import { USE_FIXTURES, loadCapacity, loadSecretsOverview, type SecretsRow } from './api'
import { Banner, Button, ButtonLink, Card, routedClick } from './components'
import { SetupContext } from './ConfigSetup'
import { noteFixtureProbe, read, route, write, type ApiError, type ApiRoute, type Result } from './fetch'
import { addressToPath, secretsAddAddress } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo, type RunnerProfile } from './types'
import './styles/keys.css'
import './styles/workspace-settings.css'

/**
 * SETTINGS › MCP SERVERS, AND THE SUBMIT FORM'S PICKER (lane MCP-UI over
 * MCP-API; docs/design/registered-mcp-servers.md §4.2, §4.3, §4.9, §4.11;
 * part of #1433).
 *
 * A workspace registers its own MCP servers by name; a task names them in
 * `input.mcp_servers` (contract request 88) and its agent reaches each one
 * through the worker's relay, which holds the credential the agent never sees.
 *
 * THE PAGE. Every member reads the list (`GET /v1/mcp-servers`); only a
 * manager -- the API's `can_manage`: a platform admin for a group workspace,
 * the person for their own (owner decision Q1) -- is offered the register
 * form, Edit and Enable/Disable. The API is the gate; the page only stops
 * offering what would be refused. There is no Remove: a server is disabled,
 * never deleted, so "which URL did attempt X call" keeps an answer.
 *
 * A REGISTRATION HAS NO CREDENTIAL, so neither does its form. A `dispatch`
 * server's credential is the caller's, sent with each task; a
 * `workspace_secret` server's is the workspace secret granted to `mcp:<name>`.
 *
 * LAST USED (MCP-API-B's `last_attached_at`, §4.11) is when a task of this
 * workspace last attached the server. Null is "never" -- unless the list's
 * `last_attached_complete` is false, when the API's newest-first read was cut
 * off and null only means "not recently" -- and an API that does not serve the
 * field says so rather than "never".
 *
 * THE NAT ADDRESSES (§4.9) are where every worker's call leaves the platform,
 * so a server's owner can allowlist them. Read from the list's
 * `egress_addresses`; an API that does not serve the field gets a sentence
 * saying so, never an empty list that reads as "no addresses".
 *
 * THE SWITCH. Until SWARM_MCP_SERVERS=on a write is 503 `mcp_unavailable`
 * and the API's own message is shown; reads answer what is stored.
 *
 * THE PICKER is drawn on Submit only for a profile that DECLARES
 * `mcp_servers`, and offers the enabled registrations that profile may use,
 * by name: a disabled server is no choice, because swarm-api refuses it
 * (`mcp_server_unregistered`). A picked `dispatch` server asks for its per-run
 * credential in an UNCONTROLLED password field: the value lives in the DOM
 * node alone, never in React state, is read once when the form sends it as
 * the top-level `mcp_credentials` map (never in `input`, which is stored and
 * shown), and the field is emptied as soon as the API has answered.
 *
 * THE WORKFLOW FORM (MCP-UI-WF, §4.3 "Workflows") draws the picker per step
 * and asks for each picked `dispatch` server's credential once, for the whole
 * workflow (`McpCredentialField`), because `WorkflowCreate.mcp_credentials` is
 * one map every step naming the server uses.
 *
 * THE LEAD (CONFIG-S5-LEADS; part of #2157, proposal E S5a, S5d) is one
 * sentence over the body as built: the server whose credential is missing,
 * with one button to Config › Secrets, or the green "nothing waiting".
 * The list cannot say whether a `workspace_secret` server's secret is stored,
 * so the lead reads the setup read's `config_setup.secrets` (CONFIG-SETUP-READ,
 * `swarm_api.config_setup`), whose `mcp_server` items make the same test
 * Config › Secrets' MCP row makes. That source unread, or not served, falls
 * back to the workspace's secrets overview, the read Config › Secrets draws
 * (`mcpLeadWith`); with neither able to say it is "—" and why: never green.
 *
 * THE ROWS (MCP-ROWS; part of #2157, S5a, S5d). A server the same read lists
 * as missing its credential says "needed, not set" in its Credential cell, so
 * the row the lead names is findable in the table; with the read unknown no
 * row says it, because the list alone cannot tell. "Register an MCP server" is
 * the card head's button and the form opens from it, and the addresses to
 * allowlist are a fold under the table: an admin reads them once, when a
 * server's owner asks.
 */

export type McpAuthMode = 'none' | 'dispatch' | 'workspace_secret'

export interface McpServer {
  name: string
  url: string
  description: string
  auth: { mode: McpAuthMode; header: string; scheme: string }
  /** Empty: every profile that takes MCP servers. */
  allowed_profiles: string[]
  /** Empty: every tool the server lists (owner decision Q4). */
  allowed_tools: string[]
  enabled: boolean
  revision: number | null
  /** The grant name a `workspace_secret` server's key is granted to. */
  consumer: string
  updated_at: string | null
  updated_by: string | null
  /** When a task of this workspace last attached it; null when none has,
   *  undefined when this API does not serve the field (MCP-API-B). */
  last_attached_at: string | null | undefined
}

export interface McpServerList {
  tenant_id: string
  can_manage: boolean
  servers: McpServer[]
  /** The NAT addresses to allowlist, or null when this API does not serve them. */
  egress_addresses: string[] | null
  /** False when the API's newest-first read of attaches was cut off, so a
   *  null `last_attached_at` means "not recently" rather than "never". */
  last_attached_complete: boolean
}

const LIST = '/v1/mcp-servers'
const ONE = '/v1/mcp-servers/{name}'
/** Config › MCP servers, as SECTIONS spells it (the old settings/ address redirects here). */
export const MCP_SERVERS_ADDRESS = 'config/mcp-servers'
/** The task input the picks travel under (contract request 88). */
export const MCP_SERVERS_KEY = 'mcp_servers'
/** The declaration's own bound, used when a served contract omits it. */
const MAX_PICKS = 4

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function text(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function strings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string' && s !== '') : []
}

function mode(v: unknown): McpAuthMode {
  return v === 'dispatch' || v === 'workspace_secret' ? v : 'none'
}

function normServer(raw: unknown): McpServer | null {
  if (!isRecord(raw)) return null
  const name = text(raw.name)
  if (name === null) return null
  const auth = isRecord(raw.auth) ? raw.auth : {}
  return {
    name,
    url: text(raw.url) ?? '',
    description: typeof raw.description === 'string' ? raw.description : '',
    auth: { mode: mode(auth.mode), header: text(auth.header) ?? 'Authorization', scheme: typeof auth.scheme === 'string' ? auth.scheme : 'Bearer' },
    allowed_profiles: strings(raw.allowed_profiles),
    allowed_tools: strings(raw.allowed_tools),
    // Only an explicit true is enabled: an unread flag is never offered.
    enabled: raw.enabled === true,
    revision: typeof raw.revision === 'number' ? raw.revision : null,
    consumer: text(raw.consumer) ?? `mcp:${name}`,
    updated_at: text(raw.updated_at),
    updated_by: text(raw.updated_by),
    last_attached_at: !('last_attached_at' in raw) ? undefined : text(raw.last_attached_at),
  }
}

export function normMcpServerList(raw: unknown): McpServerList {
  const r = isRecord(raw) ? raw : {}
  return {
    tenant_id: text(r.tenant_id) ?? '',
    can_manage: r.can_manage === true,
    servers: (Array.isArray(r.servers) ? r.servers : []).map(normServer).filter((s): s is McpServer => s !== null),
    egress_addresses: Array.isArray(r.egress_addresses) ? strings(r.egress_addresses) : null,
    // Only an explicit false is incomplete: an API that predates the flag
    // read every attach it had.
    last_attached_complete: r.last_attached_complete !== false,
  }
}

/** The fixture build's answer to an MCP-server WRITE: not served, as an API without the routes answers. */
function fixtureNotServed<T>(target: ApiRoute): Result<T> {
  void target
  return {
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build serves no MCP-server writes.' },
  }
}

/**
 * `GET /v1/mcp-servers` in the fixture build (QAFIX4-FIXTURE-A, visual QA run
 * 4 overview-automate-011): two servers on the personal workspace, which its
 * owner manages, and the egress addresses the intro points at ("from one of
 * the addresses below"), from the documentation range. One authenticates
 * with the dispatch's identity, one with nothing; one is disabled, one was
 * never attached. No secret appears: a server's credential is a workspace
 * secret the API never serves.
 */
async function fixtureMcpServers(target: ApiRoute): Promise<Result<McpServerList>> {
  await new Promise((r) => setTimeout(r, 90))
  noteFixtureProbe(target, 90, true)
  const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
  const raw = {
    tenant_id: 'u-bogdan',
    can_manage: true,
    egress_addresses: ['203.0.113.17', '203.0.113.18'],
    last_attached_complete: true,
    servers: [
      {
        name: 'docs-search', url: 'https://mcp.example.com/docs/mcp', description: 'Searches the engineering handbook and the API reference.',
        auth: { mode: 'dispatch' }, allowed_profiles: ['claude-code'], allowed_tools: ['search', 'fetch_page'],
        enabled: true, revision: 3, updated_at: ago(2 * 86_400_000), updated_by: 'alice@example.com', last_attached_at: ago(40 * 60_000),
      },
      // kb is the server Config › Secrets lists as missing its credential
      // (`mcp:kb`, unset, in the fixture's workspace overview): without it
      // here the lead named a server the table did not draw (QA1-FIX-14,
      // config-25).
      {
        name: 'kb', url: 'https://kb.example.com/mcp', description: 'Answers from the team knowledge base.',
        auth: { mode: 'workspace_secret' }, allowed_profiles: [], allowed_tools: [],
        enabled: true, revision: 1, updated_at: ago(86_400_000), updated_by: 'alice@example.com', last_attached_at: null,
      },
      {
        name: 'status-board', url: 'https://status.example.com/mcp', description: 'Reads the public status page.',
        auth: { mode: 'none' }, allowed_profiles: [], allowed_tools: [],
        enabled: false, revision: 1, updated_at: ago(9 * 86_400_000), updated_by: 'alice@example.com', last_attached_at: null,
      },
    ],
  }
  return { status: 'ok', fetchedAt: Date.now(), data: normMcpServerList(raw) }
}

/** `GET /v1/mcp-servers`: the selected workspace's registrations, and whether this caller manages them. */
export async function loadMcpServers(): Promise<Result<McpServerList>> {
  const target = route(LIST)
  if (USE_FIXTURES) return fixtureMcpServers(target)
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normMcpServerList(r.data) }
  return r
}

function registerMcpServer(body: Record<string, unknown>): Promise<Result<unknown>> {
  const target = route(LIST)
  if (USE_FIXTURES) return Promise.resolve(fixtureNotServed(target))
  return write(target, 'POST', body)
}

function patchMcpServer(name: string, changes: Record<string, unknown>): Promise<Result<unknown>> {
  const target = route(ONE, { name })
  if (USE_FIXTURES) return Promise.resolve(fixtureNotServed(target))
  return write(target, 'PATCH', changes)
}

/** A write's failure in the API's words; `mcp_unavailable` is the switch being off. */
function refusal(e: ApiError): string {
  return e.code === 'mcp_unavailable' ? `${e.message} Nothing was changed.` : e.message
}

/** How a registration's credential reaches the relay, in the table's words. */
function modeWords(m: McpAuthMode): string {
  if (m === 'dispatch') return 'per-run, sent by the caller with each agent'
  return m === 'none' ? 'none' : 'from the workspace store'
}

/**
 * The "Last used" cell. Null is a fact -- no task of this workspace has
 * attached the server -- so it reads "never", unless the API says its read was
 * cut off, when it is only "not recently". An API that does not serve the
 * field says nothing either way, and the cell says so.
 */
function LastUsed({ at, complete }: { at: string | null | undefined; complete: boolean }) {
  if (at === undefined) return <td data-label="Last used" title="This API does not say when a server was last used.">not served</td>
  if (at !== null) return <td data-label="Last used" title={at}>{timeAgo(at)}</td>
  return complete ? <td data-label="Last used">never</td>
    : <td data-label="Last used" title="Not among the newest attaches the API read; it may have been used before them.">not recently</td>
}

/** Config › MCP servers, routed in-app like the panel's own links. */
export function McpServersLink() {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(MCP_SERVERS_ADDRESS))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
  return <a href={addressToPath(MCP_SERVERS_ADDRESS)} onClick={go}>Config › MCP servers</a>
}

/** Routed in-app, as `McpServersLink`: this screen is drawn without App's `go`. */
function routedTo(address: string) {
  return (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(address))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
}

/**
 * Where a missing credential is set: Config › Secrets' Add a secret, the
 * server's consumer ticked (MCP-LEAD-FORM, BGD 2026-10-25: "Open the form,
 * consumer picked"). Several servers missing theirs open it with none ticked:
 * one secret is one server's credential. Never `?action=add`: that is the /sc
 * one-time link's address, and the page then says it was opened from one
 * (CONFIG-S5-LEADS review).
 */
function setCredentialAddress(names: readonly string[]): string {
  return secretsAddAddress(names.length === 1 ? `mcp:${names[0]}` : null)
}

/** What the lead says, from the setup read's secrets step. Pure, for the tests' reasons. */
export type McpLead =
  | { kind: 'unknown'; reason: string }
  | { kind: 'missing'; names: string[]; of: number | null }
  | { kind: 'clear' }

/** The `mcp_server` part of `config_setup.secrets` (`swarm_api.config_setup.secrets_step`); null while the read has not landed. */
export function mcpLeadOf(doc: Result<unknown>): McpLead | null {
  if (doc.status === 'loading') return null
  if (doc.status === 'error' || doc.status === 'empty') return { kind: 'unknown', reason: 'the setup read could not be read' }
  const setup = isRecord(doc.data) ? doc.data.config_setup : undefined
  const step = isRecord(setup) ? setup.secrets : undefined
  if (!isRecord(step)) return { kind: 'unknown', reason: 'this API does not serve which secrets are needed and not stored' }
  const source = isRecord(step.sources) ? step.sources.mcp_servers : undefined
  if (!isRecord(source) || typeof source.needed !== 'number') {
    return { kind: 'unknown', reason: (isRecord(source) ? text(source.reason) : null) ?? 'the MCP servers’ credentials were not measured' }
  }
  const items = Array.isArray(step.items) ? step.items : []
  const names = [...new Set(items.flatMap((i) => (isRecord(i) && i.kind === 'mcp_server' && text(i.name) !== null ? [text(i.name)!] : [])))]
  if (names.length === 0) return { kind: 'clear' }
  return { kind: 'missing', names, of: typeof step.needed === 'number' ? step.needed : null }
}

/** The MCP servers Config › Secrets' overview lists with their credential unset: its own "needed" test (Secrets.tsx `neededOf`). */
export function mcpUnsetOf(rows: readonly SecretsRow[]): string[] {
  return [...new Set(rows.filter((r) => r.section === 'keys' && r.store === 'mcp_servers' && r.status === 'unset').map((r) => r.name))]
}

/**
 * WHAT THE CONSOLE ALREADY KNOWS, BEFORE "NOT KNOWN" (QA1-FIX-14, config-25).
 * With the setup read unable to say, the lead said whether a server is missing
 * its credential is not known -- on a page whose sibling, Config › Secrets,
 * said kb's was not set. That page reads the workspace's secrets overview;
 * so does this one, and only then. A server it lists unset is missing. It
 * listing none leaves the answer unknown rather than green: the overview is
 * the fallback, not the source the "nothing waiting" lead is measured from.
 */
export function mcpLeadWith(lead: McpLead | null, unset: readonly string[] | null): McpLead | null {
  if (lead?.kind !== 'unknown' || unset === null || unset.length === 0) return lead
  return { kind: 'missing', names: [...unset], of: null }
}

/** The overview's unset MCP servers, read only while `ask`; null while unread or when the read failed. */
function useSecretsUnset(ask: boolean): string[] | null {
  const [names, setNames] = useState<string[] | null>(null)
  useEffect(() => {
    if (!ask) return undefined
    let live = true
    void loadSecretsOverview('workspace').then((r) => {
      if (live) setNames(r.status === 'ok' || r.status === 'stale' ? mcpUnsetOf(r.data.rows) : null)
    })
    return () => {
      live = false
    }
  }, [ask])
  return ask ? names : null
}

/** The page's one answer to "which server is missing its credential", for the lead and the rows alike. */
function useMcpLead(): McpLead | null {
  const { doc } = useContext(SetupContext)
  const lead = mcpLeadOf(doc)
  return mcpLeadWith(lead, useSecretsUnset(lead?.kind === 'unknown'))
}

function joined(names: readonly string[]): string {
  return names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/**
 * The page's lead: one sentence, at most one button (proposal E).
 *
 * NONE REGISTERED IS ITS OWN LEAD (VQ-046, S5a: every page opens with its next
 * action). The setup read counts missing credentials, not registrations, so
 * with no server at all it says "nothing missing" -- which drew a green
 * "Nothing waiting" over an empty list. The list's own read says none is
 * registered, and the lead carries Register an MCP server for someone who may.
 */
function McpServersLead({ lead, list, onRegister }: { lead: McpLead | null; list: McpServerList | null; onRegister: () => void }) {
  if (list !== null && list.servers.length === 0) {
    return (
      <section className="ws-lead" aria-label="No MCP server yet">
        <div className="ws-lead-t">
          <span className="ws-lead-k">No MCP server yet</span>
          <b>No MCP server is registered for {list.tenant_id || 'this workspace'} yet.</b>
          <span className="ws-lead-s">
            {list.can_manage
              ? 'Register one to let an agent call it during its run.'
              : 'A platform admin registers a group workspace’s MCP servers.'}
          </span>
        </div>
        {list.can_manage && (
          <div className="ws-lead-a">
            <Button kind="primary" size="sm" onClick={onRegister}>
              Register an MCP server
            </Button>
          </div>
        )}
      </section>
    )
  }
  if (lead === null) return null
  if (lead.kind === 'unknown') {
    return (
      // The unknown count is the kicker's dash, as `Secrets set · 1 missing` is
      // its figure; the sentence starts with a word (QA1-FIX-14, config-25:
      // it began "— whether", a bold line opening on a dangling dash).
      <section className="ws-lead is-next" aria-label="Secrets set · —">
        <div className="ws-lead-t">
          <span className="ws-lead-k">Secrets set · —</span>
          <b>Whether an MCP server is missing its credential is not known: {lead.reason}.</b>
        </div>
      </section>
    )
  }
  if (lead.kind === 'clear') {
    return (
      <section className="ws-lead is-ok" aria-label="Nothing waiting">
        <div className="ws-lead-t">
          <span className="ws-lead-k">Nothing waiting</span>
          <b>No MCP server here is missing its credential.</b>
        </div>
      </section>
    )
  }
  const n = lead.names.length
  // "1 of the 1 missing is here" reads as a slip: when every missing secret is an MCP server's, the count alone.
  const kicker = lead.of === null ? 'Secrets set'
    : lead.of <= n ? `Secrets set · ${n} missing`
      : `Secrets set · ${n} of the ${lead.of} missing ${n === 1 ? 'is' : 'are'} here`
  return (
    <section className="ws-lead is-next" aria-label={kicker}>
      <div className="ws-lead-t">
        <span className="ws-lead-k">{kicker}</span>
        <b>
          {n === 1 ? `MCP server ${lead.names[0]} has no credential stored` : `MCP servers ${joined(lead.names)} have no credential stored`}
          , so every call to {n === 1 ? 'it' : 'them'} fails until a workspace secret is granted to {n === 1 ? `mcp:${lead.names[0]}` : 'each'}.
        </b>
      </div>
      <div className="ws-lead-a">
        <ButtonLink kind="primary" size="sm" href={addressToPath(setCredentialAddress(lead.names))} onClick={routedTo(setCredentialAddress(lead.names))}>
          {n === 1 ? `Set ${lead.names[0]}’s credential` : 'Set their credentials'}
        </ButtonLink>
      </div>
    </section>
  )
}

export function McpServersScreen() {
  const list = useUrRead(loadMcpServers, 'mcp-servers')
  const lead = useMcpLead()
  // The servers the lead names as missing their credential; empty when it is clear or unknown.
  const missing: ReadonlySet<string> = new Set(lead?.kind === 'missing' ? lead.names : [])
  const [registering, setRegistering] = useState(false)
  const read = list.state.status === 'ok' || list.state.status === 'stale' ? list.state.data : null
  const manages = read !== null && read.can_manage
  // With none registered the lead carries Register an MCP server, so the card does not draw it a second time.
  const none = read !== null && read.servers.length === 0
  return (
    <div className="pk-page">
      <PageHead title="MCP servers" />
      <McpServersLead lead={lead} list={read} onRegister={() => setRegistering(true)} />
      <Card
        title="Registered MCP servers"
        action={(
          <span className="mcp-head-a">
            {manages && (!none || registering) && (
              <Button size="sm" aria-expanded={registering} onClick={() => setRegistering((was) => !was)}>
                Register an MCP server
              </Button>
            )}
            <UrRefresh reads={[list]} />
          </span>
        )}
      >
        <p className="pk-lead">
          The MCP servers an agent may call during its run, by name. An agent names them on Submit; the
          worker reaches each one for the agent and holds its credential, which the agent never sees. SwarmCloud
          never calls a registered URL itself: the first request a server sees is a worker&rsquo;s, from one of the
          addresses below.
        </p>
        <UrRegion state={list.state} route={`GET ${LIST}`} what="The workspace's MCP servers" plural onRetry={list.reload}>
          {(d) => <Registrations data={d} missing={missing} registering={registering} onChange={list.reload} />}
        </UrRegion>
      </Card>
    </div>
  )
}

function Registrations({ data, missing, registering, onChange }: {
  data: McpServerList
  missing: ReadonlySet<string>
  registering: boolean
  onChange: () => void
}) {
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const edited = editing === null ? undefined : data.servers.find((s) => s.name === editing)
  const named = [...new Set(data.servers.flatMap((s) => s.allowed_profiles))].sort()

  async function flip(s: McpServer) {
    if (busy !== null) return
    setBusy(s.name)
    setFailed(null)
    const r = await patchMcpServer(s.name, { enabled: !s.enabled })
    setBusy(null)
    if (r.status === 'error') {
      setFailed(`${s.name} was not ${s.enabled ? 'disabled' : 'enabled'}: ${refusal(r.error)}`)
      return
    }
    onChange()
  }

  return (
    <>
      {!data.can_manage && (
        <p className="ur-small">
          Only a platform admin can register or change a group workspace&rsquo;s MCP servers (in a personal
          workspace, its person). You can read them here.
        </p>
      )}
      {data.servers.length === 0 ? (
        <p className="ur-small">No MCP server is registered for {data.tenant_id || 'this workspace'} yet.</p>
      ) : (
        <table role="table" className="mcp-servers mcp-registered" aria-label="Registered MCP servers">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">URL</th>
              <th scope="col">Credential</th>
              <th scope="col">Profiles</th>
              <th scope="col">Tools</th>
              <th scope="col">State</th>
              <th scope="col" data-col="last">Last used</th>
              {data.can_manage && <th scope="col" aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {data.servers.map((s) => (
              <tr key={s.name} className={missing.has(s.name) ? 'is-need' : undefined}>
                <th scope="row" data-label="Name">
                  <code>{s.name}</code>
                  {s.description !== '' && <div className="jb-note">{s.description}</div>}
                </th>
                <td data-label="URL"><code className="sbf-cut" title={s.url}><UrlBreaks url={s.url} /></code></td>
                <td data-label="Credential">
                  {modeWords(s.auth.mode)}
                  {s.auth.mode === 'workspace_secret' && <>, granted to <code>{s.consumer}</code></>}
                  {missing.has(s.name) && <small className="mcp-need">needed, not set</small>}
                </td>
                <td data-label="Profiles">{s.allowed_profiles.length === 0 ? 'every profile that takes MCP servers' : s.allowed_profiles.join(', ')}</td>
                <td data-label="Tools">{s.allowed_tools.length === 0 ? 'every tool it lists' : s.allowed_tools.join(', ')}</td>
                <td data-label="State" title={s.updated_at === null ? undefined : `changed ${timeAgo(s.updated_at)}${s.updated_by === null ? '' : ` by ${s.updated_by}`}`}>
                  {s.enabled ? 'enabled' : 'disabled'}
                </td>
                <LastUsed at={s.last_attached_at} complete={data.last_attached_complete} />
                {data.can_manage && (
                  <td data-label="Actions">
                    <span className="pk-form-row">
                      <Button size="sm" aria-label={`Edit ${s.name}`} onClick={() => setEditing(editing === s.name ? null : s.name)}>
                        Edit
                      </Button>
                      <Button size="sm" kind={s.enabled ? 'danger' : 'secondary'} aria-label={`${s.enabled ? 'Disable' : 'Enable'} ${s.name}`}
                        busy={busy === s.name} disabled={busy !== null && busy !== s.name} onClick={() => void flip(s)}>
                        {s.enabled ? 'Disable' : 'Enable'}
                      </Button>
                    </span>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
      <EgressAddresses addresses={data.egress_addresses} />
      {data.can_manage && edited !== undefined && (
        <ServerForm key={`edit-${edited.name}`} editing={edited} named={named} onDone={() => { setEditing(null); onChange() }} />
      )}
      {data.can_manage && registering && <ServerForm key="register" editing={null} named={named} onDone={onChange} />}
    </>
  )
}

function EgressAddresses({ addresses }: { addresses: string[] | null }) {
  const id = useId()
  const count = addresses === null ? 'not served' : addresses.length === 0 ? 'none served'
    : `${addresses.length} ${addresses.length === 1 ? 'address' : 'addresses'}`
  // Folded (S5a): an admin opens it when a server's owner asks what to admit.
  return (
    <section className="mcp-allow" aria-labelledby={`${id}-h`}>
      <details>
        <summary>
          <b id={`${id}-h`}>Addresses to allowlist</b>
          <span className="mcp-allow-n">{count}</span>
        </summary>
        <div className="mcp-allow-b">
          {addresses === null ? (
            <p className="ur-small">
              The platform&rsquo;s outbound addresses are not served by this API yet, so they cannot be shown here.
              Every worker&rsquo;s call to a registered server leaves through the platform&rsquo;s fixed NAT addresses.
            </p>
          ) : addresses.length === 0 ? (
            <p className="ur-small">The API served no outbound addresses for this platform.</p>
          ) : (
            <>
              <p className="ur-small">
                Every worker&rsquo;s call to a registered server leaves the platform from one of these addresses. A
                server that allowlists its callers should admit them.
              </p>
              <ul className="pk-uses">
                {addresses.map((a) => <li key={a}><code>{a}</code></li>)}
              </ul>
            </>
          )}
        </div>
      </details>
    </section>
  )
}

/**
 * The profiles a registration may name: those whose served contract declares
 * `mcp_servers`. NULL WHEN NO CONTRACT SERVES `inputs` AT ALL (visual QA
 * 5b:QA-010, #1680): an API older than MCP-INPUTS, or the fixture build, has
 * not said which profiles take MCP servers, and reading that silence as "none"
 * drew "No profile here takes MCP servers yet" under a table listing
 * docs-search for claude-code. `inputs: {}` is served-and-empty, a measured
 * "declares nothing" (`swarm_api.runnerinputs.input_contract`), and counts.
 */
function mcpProfiles(profiles: Readonly<Record<string, RunnerProfile>>): string[] | null {
  const entries = Object.entries(profiles)
  const served = entries.some(([, p]) => {
    const contract: unknown = p.input_contract
    return isRecord(contract) && isRecord(contract.inputs)
  })
  if (!served) return null
  return entries
    .filter(([, p]) => declaredMcpServers(p) !== null)
    .map(([name]) => name)
    .sort()
}

function splitTools(raw: string): string[] {
  return [...new Set(raw.split(/[\s,]+/).map((t) => t.trim()).filter((t) => t !== ''))]
}

const AUTH_CHOICES: ReadonlyArray<{ mode: McpAuthMode; label: string }> = [
  { mode: 'none', label: 'No credential — the server is open to SwarmCloud’s addresses' },
  { mode: 'dispatch', label: 'Per-run credential — the caller sends one with each agent, and it dies with the agent' },
  { mode: 'workspace_secret', label: 'Workspace secret — the secret this workspace grants to mcp:<name>' },
]

/** Register (editing null) or change one registration. Its name is fixed once registered. */
function ServerForm({ editing, named, onDone }: { editing: McpServer | null; named: readonly string[]; onDone: () => void }) {
  const id = useId()
  const capacity = useUrRead(loadCapacity, 'capacity')
  const [name, setName] = useState(editing?.name ?? '')
  const [url, setUrl] = useState(editing?.url ?? '')
  const [description, setDescription] = useState(editing?.description ?? '')
  const [authMode, setAuthMode] = useState<McpAuthMode>(editing?.auth.mode ?? 'none')
  const [header, setHeader] = useState(editing?.auth.header ?? 'Authorization')
  const [scheme, setScheme] = useState(editing?.auth.scheme ?? 'Bearer')
  const [profiles, setProfiles] = useState<string[]>(editing?.allowed_profiles ?? [])
  const [tools, setTools] = useState((editing?.allowed_tools ?? []).join(', '))
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)

  const state = capacity.state
  const read = state.status === 'ok' || state.status === 'stale'
  const served = state.status === 'ok' || state.status === 'stale' ? mcpProfiles(state.data.runner_profiles) : null
  // The profiles this workspace's registrations already name are offered
  // whatever the runner list says: the API accepted each of them as taking MCP
  // servers, and the table above lists them (visual QA 6:overview-automate-019,
  // #1680), so the form never says none does under a row naming one.
  const offered = [...new Set([...(served ?? []), ...named, ...profiles])].sort()
  // Words for the person, not about the API: what could not be read, and so
  // where the offered names came from.
  const unsaid = served === null
    ? read
      ? 'Could not read which runners take MCP servers'
      : 'Could not read the runner list'
    : null
  const verb = editing === null ? 'Register' : 'Save'

  // THE FORM OPENS WHERE IT IS READ (QA1-FIX-14, config-27). It is drawn under
  // the table and the addresses, below the fold at 1440x900, and the press
  // left focus on the button: nothing at the top of the page changed. On
  // opening it scrolls into view and its first field -- Name, or URL for an
  // edit -- takes focus. Once, on mount: a save keeps the form open, and a
  // reader typing in it is not moved.
  const top = useRef<HTMLElement>(null)
  useEffect(() => {
    const el = top.current
    if (el === null) return
    // jsdom draws nothing, so it has no `scrollIntoView`; a browser always does.
    if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'start', behavior: 'smooth' })
    el.querySelector<HTMLInputElement>('form input')?.focus({ preventScroll: true })
  }, [])

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    setBusy(true)
    setSaved(null)
    setFailed(null)
    // `header` and `scheme` only when they are not the API's defaults, so a
    // body says what was chosen rather than restating the defaults.
    const auth: Record<string, unknown> = { mode: authMode }
    if (authMode !== 'none') {
      if (header.trim() !== '' && header.trim() !== 'Authorization') auth.header = header.trim()
      if (scheme !== 'Bearer') auth.scheme = scheme
    }
    const fields = { url: url.trim(), description: description.trim(), auth, allowed_profiles: profiles, allowed_tools: splitTools(tools) }
    const r = editing === null ? await registerMcpServer({ name: name.trim(), ...fields }) : await patchMcpServer(editing.name, fields)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(`${editing === null ? 'Not registered' : `${editing.name} was not changed`}: ${refusal(r.error)}`)
      return
    }
    if (editing === null) {
      setSaved(`Registered ${name.trim()}.`)
      setName('')
      setUrl('')
      setDescription('')
      setAuthMode('none')
      setHeader('Authorization')
      setScheme('Bearer')
      setProfiles([])
      setTools('')
    }
    onDone()
  }

  const label = editing === null ? 'Register an MCP server' : `Change ${editing.name}`
  return (
    <section ref={top} className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>{label}</h3>
      </div>
      <form className="pk-form" autoComplete="off" aria-label={label} onSubmit={(e) => void save(e)}>
        {editing === null && (
          <>
            <label htmlFor={`${id}-name`}>Name</label>
            <input id={`${id}-name`} className="ur-search pk-input" value={name} required maxLength={31} spellCheck={false}
              placeholder="kb" aria-describedby={`${id}-name-why`} onChange={(e) => setName(e.target.value)} />
            <span id={`${id}-name-why`} className="jb-note">
              What an agent names: a lower-case letter, then letters, digits and hyphens. It cannot be changed later.
            </span>
          </>
        )}
        <label htmlFor={`${id}-url`}>URL</label>
        <input id={`${id}-url`} className="ur-search pk-input" type="url" value={url} required maxLength={512} spellCheck={false}
          placeholder="https://mcp.example.com/mcp" aria-describedby={`${id}-url-why`} onChange={(e) => setUrl(e.target.value)} />
        <span id={`${id}-url-why`} className="jb-note">
          Public HTTPS on port 443, by host name, speaking streamable HTTP. A private or IP address is refused.
        </span>
        <label htmlFor={`${id}-desc`}>Description</label>
        <input id={`${id}-desc`} className="ur-search pk-input" value={description} maxLength={280}
          aria-describedby={`${id}-desc-why`} onChange={(e) => setDescription(e.target.value)} />
        <span id={`${id}-desc-why`} className="jb-note">Shown to the agent beside the name, so it knows what the server is for.</span>

        <fieldset className="jb-options">
          <legend>Credential</legend>
          {AUTH_CHOICES.map((c) => (
            <label key={c.mode} className={`jb-option${authMode === c.mode ? ' is-on' : ''}`}>
              <input type="radio" name={`${id}-auth`} value={c.mode} checked={authMode === c.mode} onChange={() => setAuthMode(c.mode)} />
              <span className="jb-text">{c.label}</span>
            </label>
          ))}
        </fieldset>
        {authMode !== 'none' && (
          <div className="pk-form-row">
            <label htmlFor={`${id}-header`}>Header</label>
            <input id={`${id}-header`} className="ur-search pk-input" value={header} maxLength={64} spellCheck={false}
              onChange={(e) => setHeader(e.target.value)} />
            <label htmlFor={`${id}-scheme`}>Scheme</label>
            <select id={`${id}-scheme`} value={scheme} onChange={(e) => setScheme(e.target.value)}>
              <option value="Bearer">Bearer</option>
              <option value="">none — the credential is the header&rsquo;s whole value</option>
            </select>
          </div>
        )}

        <fieldset className="jb-options">
          <legend>Profiles that may use it</legend>
          {offered.length === 0 ? (
            <span className="jb-note">
              {unsaid !== null
                ? `${unsaid}, so this registration allows every profile that takes MCP servers.`
                : 'No profile here takes MCP servers yet; leaving this empty allows every one that does.'}
            </span>
          ) : (
            <>
              {offered.map((p) => (
                <label key={p} className="jb-option">
                  <input type="checkbox" value={p} checked={profiles.includes(p)}
                    onChange={(e) => setProfiles((was) => (e.target.checked ? [...was, p] : was.filter((x) => x !== p)))} />
                  <span className="jb-text"><code>{p}</code></span>
                </label>
              ))}
              <span className="jb-note">
                {unsaid !== null && `${unsaid}; offering the ones your servers already use. `}
                Tick a profile to let only its agents use this server; none ticked allows every profile that takes MCP
                servers.
              </span>
            </>
          )}
        </fieldset>

        <label htmlFor={`${id}-tools`}>Allowed tools</label>
        <textarea id={`${id}-tools`} className="ur-search pk-input" rows={2} value={tools} spellCheck={false}
          aria-describedby={`${id}-tools-why`} onChange={(e) => setTools(e.target.value)} />
        <span id={`${id}-tools-why`} className="jb-note">
          Tool names, separated by commas or spaces. Empty allows every tool the server lists.
        </span>

        <div className="pk-form-row">
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false}
            disabled={(editing === null && name.trim() === '') || url.trim() === ''}>
            {verb}
          </Button>
        </div>
      </form>
      {saved !== null && <p className="pk-saved" role="status">{saved}</p>}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </section>
  )
}

// ---------------------------------------------------------------------------
// The Submit form's picker
// ---------------------------------------------------------------------------

/** The declaration's maximum, or null when this profile does not declare `mcp_servers`. */
export function declaredMcpServers(profile: RunnerProfile | null | undefined): number | null {
  const contract: unknown = profile?.input_contract
  const inputs = isRecord(contract) ? contract.inputs : undefined
  const decl = isRecord(inputs) ? inputs[MCP_SERVERS_KEY] : undefined
  if (!isRecord(decl)) return null
  return typeof decl.maximum === 'number' && decl.maximum > 0 ? decl.maximum : MAX_PICKS
}

/** The registrations a task on `profile` may name: enabled, and allowed for it. */
export function offeredMcpServers(list: McpServerList, profile: string): McpServer[] {
  return list.servers.filter((s) => s.enabled && (s.allowed_profiles.length === 0 || s.allowed_profiles.includes(profile)))
}

/** `input` with the picks under `mcp_servers`; none picked sends no key. */
export function withMcpServers(input: Record<string, unknown>, names: readonly string[]): Record<string, unknown> {
  if (names.length === 0) return input
  const out: Record<string, unknown> = Object.create(null) as Record<string, unknown>
  Object.assign(out, input)
  out[MCP_SERVERS_KEY] = [...names]
  return out
}

/** How long a per-run credential may live (the API takes 60 s to 24 h); the task's end destroys it sooner. */
export const CREDENTIAL_LIFETIMES: ReadonlyArray<{ seconds: number; label: string }> = [
  { seconds: 900, label: '15 minutes' },
  { seconds: 3600, label: '1 hour' },
  { seconds: 14400, label: '4 hours' },
  { seconds: 86400, label: '24 hours' },
]
/** One hour, the design's example (§4.3): long enough for a task, short for a leaked value. */
export const DEFAULT_LIFETIME = 3600

/** Each picked `dispatch` server's credential field, by server name. The values stay in the nodes. */
export type CredentialFields = MutableRefObject<Map<string, HTMLInputElement>>

/**
 * The `mcp_credentials` map for `dispatch` servers among `names`, read from
 * their fields. Exported for Submit, which calls it once per send.
 */
export function readCredentials(
  fields: CredentialFields,
  servers: readonly McpServer[],
  names: readonly string[],
  lifetimes: Readonly<Record<string, number>>,
): Record<string, { value: string; expires_in: number }> {
  const out: Record<string, { value: string; expires_in: number }> = {}
  for (const s of servers) {
    if (!names.includes(s.name) || s.auth.mode !== 'dispatch') continue
    const value = fields.current.get(s.name)?.value ?? ''
    if (value !== '') out[s.name] = { value, expires_in: lifetimes[s.name] ?? DEFAULT_LIFETIME }
  }
  return out
}

/** Empty every credential field: called once the API has answered a send. */
export function clearCredentials(fields: CredentialFields): void {
  for (const el of fields.current.values()) el.value = ''
}

/**
 * One `dispatch` server's per-run credential: an UNCONTROLLED password field
 * whose node is registered in `fields` under the server's name, and its
 * lifetime. Drawn by the picker beside a pick on the task form, and once per
 * server for the whole workflow on the workflow form (§4.3 "Workflows").
 */
export function McpCredentialField({
  server,
  fields,
  filled,
  onFilled,
  lifetime,
  onLifetime,
  scope = 'task',
}: {
  server: McpServer
  fields: CredentialFields
  filled: boolean
  onFilled: (name: string, filled: boolean) => void
  lifetime: number | undefined
  onLifetime: (name: string, seconds: number) => void
  /** What the credential is sent with: one task, or every step of a workflow that names it. */
  scope?: 'task' | 'workflow'
}) {
  const id = useId()
  const name = server.name
  return (
    <span className="pk-form-row">
      <label htmlFor={`${id}-c`}>Credential for {name}</label>
      <input
        id={`${id}-c`}
        ref={(el) => {
          if (el === null) fields.current.delete(name)
          else fields.current.set(name, el)
        }}
        className="ur-search pk-input"
        type="password"
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        placeholder={filled ? undefined : 'Paste the credential for this run'}
        aria-describedby={`${id}-why`}
        onChange={(e) => onFilled(name, e.target.value !== '')}
      />
      <label htmlFor={`${id}-ttl`}>expires after</label>
      <select id={`${id}-ttl`} value={lifetime ?? DEFAULT_LIFETIME}
        onChange={(e) => onLifetime(name, Number(e.target.value))}>
        {CREDENTIAL_LIFETIMES.map((l) => <option key={l.seconds} value={l.seconds}>{l.label}</option>)}
      </select>
      <span id={`${id}-why`} className="jb-note">
        {scope === 'task'
          ? 'Sent once with this agent, to the worker only, and destroyed when the agent ends.'
          : 'Sent once with this workflow, to the workers of the steps that name it only, and destroyed when its agents end.'}
        {' '}The field is emptied as soon as the platform answers.
      </span>
    </span>
  )
}

/**
 * A URL that may wrap only after a `/` or a `.` (QA2 config2-19). The cell
 * broke anywhere, so the table squeezed the column and a reader saw
 * 'https://mcp.exampl / e.com/docs/mcp'. Each `<wbr>` is a break the browser
 * may take; the text, its copy and its title stay the URL itself.
 */
function UrlBreaks({ url }: { url: string }) {
  const parts = url.split(/(?<=[/.])(?!\/)/)
  return (
    <>
      {parts.map((part, i) => (
        <Fragment key={i}>
          {i > 0 && <wbr />}
          {part}
        </Fragment>
      ))}
    </>
  )
}

export function McpServerPicker({
  list,
  profile,
  max,
  picked,
  onPick,
  fields,
  filled,
  onFilled,
  lifetimes,
  onLifetime,
  credentialsElsewhere,
}: {
  /** The workspace's registrations; null while the first read runs. */
  list: Result<McpServerList> | null
  profile: string
  max: number
  picked: readonly string[]
  onPick: (names: string[]) => void
  fields: CredentialFields
  filled: ReadonlySet<string>
  onFilled: (name: string, filled: boolean) => void
  lifetimes: Readonly<Record<string, number>>
  onLifetime: (name: string, seconds: number) => void
  /** Where a picked `dispatch` server's credential is asked for instead of
   *  beside the pick: the workflow form asks once for every step. */
  credentialsElsewhere?: string
}) {
  const id = useId()
  const data = list !== null && (list.status === 'ok' || list.status === 'stale') ? list.data : null
  const offered = data === null ? [] : offeredMcpServers(data, profile)
  return (
    <fieldset className="btp" aria-labelledby={`${id}-h`}>
      <legend className="btp-h" id={`${id}-h`}>MCP servers</legend>
      {list === null || list.status === 'loading' ? (
        <p className="btp-hint">Reading this workspace&rsquo;s MCP servers…</p>
      ) : data === null ? (
        <p className="btp-hint">
          This workspace&rsquo;s MCP servers could not be read
          {list.status === 'error' ? ` (${list.error.message})` : ''}, so none is offered. <McpServersLink />
        </p>
      ) : offered.length === 0 ? (
        <p className="btp-hint">
          No enabled MCP server is registered for {profile} in this workspace. <McpServersLink />
        </p>
      ) : (
        <>
          {offered.map((s) => {
            const on = picked.includes(s.name)
            return (
              <div key={s.name} className="btp-row">
                <label className={`btp-opt mcp-opt${on ? ' is-on' : ''}`} title={s.description || undefined}>
                  <input type="checkbox" value={s.name} checked={on} disabled={!on && picked.length >= max}
                    onChange={(e) => onPick(e.target.checked ? [...picked, s.name] : picked.filter((n) => n !== s.name))} />
                  <span><code>{s.name}</code>{s.description !== '' && <> &middot; {s.description}</>}</span>
                </label>
                {on && s.auth.mode === 'dispatch' && (credentialsElsewhere === undefined ? (
                  <McpCredentialField server={s} fields={fields} filled={filled.has(s.name)} onFilled={onFilled}
                    lifetime={lifetimes[s.name]} onLifetime={onLifetime} />
                ) : (
                  <span className="jb-note">{credentialsElsewhere}</span>
                ))}
                {on && s.auth.mode === 'workspace_secret' && (
                  <span className="jb-note">Uses the workspace secret granted to <code>{s.consumer}</code>.</span>
                )}
              </div>
            )
          })}
          {picked.length >= max && <p className="btp-hint">An agent names at most {max}.</p>}
        </>
      )}
    </fieldset>
  )
}
