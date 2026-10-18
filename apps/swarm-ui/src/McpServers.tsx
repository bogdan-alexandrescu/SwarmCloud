import { useId, useState, type FormEvent, type MouseEvent, type MutableRefObject } from 'react'
import { USE_FIXTURES, loadCapacity } from './api'
import { Banner, Button, Card, routedClick } from './components'
import { read, route, write, type ApiError, type ApiRoute, type Result } from './fetch'
import { addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo, type RunnerProfile } from './types'
import './styles/keys.css'

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
export const MCP_SERVERS_ADDRESS = 'settings/mcp-servers'
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

function fixtureNotServed<T>(target: ApiRoute): Result<T> {
  void target
  return {
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build serves no MCP-server routes.' },
  }
}

/** `GET /v1/mcp-servers`: the selected workspace's registrations, and whether this caller manages them. */
export async function loadMcpServers(): Promise<Result<McpServerList>> {
  const target = route(LIST)
  if (USE_FIXTURES) return fixtureNotServed(target)
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
  if (m === 'dispatch') return 'per-run, sent by the caller with each task'
  return m === 'none' ? 'none' : 'from the workspace store'
}

/**
 * The "Last used" cell. Null is a fact -- no task of this workspace has
 * attached the server -- so it reads "never", unless the API says its read was
 * cut off, when it is only "not recently". An API that does not serve the
 * field says nothing either way, and the cell says so.
 */
function LastUsed({ at, complete }: { at: string | null | undefined; complete: boolean }) {
  if (at === undefined) return <td title="This API does not say when a server was last used.">not served</td>
  if (at !== null) return <td title={at}>{timeAgo(at)}</td>
  return complete ? <td>never</td>
    : <td title="Not among the newest attaches the API read; it may have been used before them.">not recently</td>
}

/** Settings › MCP servers, routed in-app like the panel's own links. */
export function McpServersLink() {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(MCP_SERVERS_ADDRESS))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
  return <a href={addressToPath(MCP_SERVERS_ADDRESS)} onClick={go}>Settings › MCP servers</a>
}

export function McpServersScreen() {
  const list = useUrRead(loadMcpServers, 'mcp-servers')
  return (
    <div className="pk-page">
      <PageHead title="MCP servers" />
      <Card title="Registered MCP servers" action={<UrRefresh reads={[list]} />}>
        <p className="pk-lead">
          The MCP servers a task&rsquo;s agent may call during its run, by name. A task names them on Submit; the
          worker reaches each one for the agent and holds its credential, which the agent never sees. SwarmCloud
          never calls a registered URL itself: the first request a server sees is a worker&rsquo;s, from one of the
          addresses below.
        </p>
        <UrRegion state={list.state} route={`GET ${LIST}`} what="The workspace's MCP servers" plural onRetry={list.reload}>
          {(d) => <Registrations data={d} onChange={list.reload} />}
        </UrRegion>
      </Card>
    </div>
  )
}

function Registrations({ data, onChange }: { data: McpServerList; onChange: () => void }) {
  const [editing, setEditing] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const edited = editing === null ? undefined : data.servers.find((s) => s.name === editing)

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
      <EgressAddresses addresses={data.egress_addresses} />
      {!data.can_manage && (
        <p className="ur-small">
          Only a platform admin can register or change a group workspace&rsquo;s MCP servers (in a personal
          workspace, its person). You can read them here.
        </p>
      )}
      {data.servers.length === 0 ? (
        <p className="ur-small">No MCP server is registered for {data.tenant_id || 'this workspace'} yet.</p>
      ) : (
        <table role="table" aria-label="Registered MCP servers">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">URL</th>
              <th scope="col">Credential</th>
              <th scope="col">Profiles</th>
              <th scope="col">Tools</th>
              <th scope="col">State</th>
              <th scope="col">Last used</th>
              {data.can_manage && <th scope="col" aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {data.servers.map((s) => (
              <tr key={s.name}>
                <th scope="row">
                  <code>{s.name}</code>
                  {s.description !== '' && <div className="jb-note">{s.description}</div>}
                </th>
                <td><code className="sbf-cut" title={s.url}>{s.url}</code></td>
                <td>
                  {modeWords(s.auth.mode)}
                  {s.auth.mode === 'workspace_secret' && <>, granted to <code>{s.consumer}</code></>}
                </td>
                <td>{s.allowed_profiles.length === 0 ? 'every profile that takes MCP servers' : s.allowed_profiles.join(', ')}</td>
                <td>{s.allowed_tools.length === 0 ? 'every tool it lists' : s.allowed_tools.join(', ')}</td>
                <td title={s.updated_at === null ? undefined : `changed ${timeAgo(s.updated_at)}${s.updated_by === null ? '' : ` by ${s.updated_by}`}`}>
                  {s.enabled ? 'enabled' : 'disabled'}
                </td>
                <LastUsed at={s.last_attached_at} complete={data.last_attached_complete} />
                {data.can_manage && (
                  <td>
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
      {data.can_manage && edited !== undefined && (
        <ServerForm key={`edit-${edited.name}`} editing={edited} onDone={() => { setEditing(null); onChange() }} />
      )}
      {data.can_manage && <ServerForm key="register" editing={null} onDone={onChange} />}
    </>
  )
}

function EgressAddresses({ addresses }: { addresses: string[] | null }) {
  const id = useId()
  return (
    <section className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>Addresses to allowlist</h3>
      </div>
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
    </section>
  )
}

/** The profiles a registration may name: those whose served contract declares `mcp_servers`. */
function mcpProfiles(profiles: Readonly<Record<string, RunnerProfile>>): string[] {
  return Object.entries(profiles)
    .filter(([, p]) => declaredMcpServers(p) !== null)
    .map(([name]) => name)
    .sort()
}

function splitTools(raw: string): string[] {
  return [...new Set(raw.split(/[\s,]+/).map((t) => t.trim()).filter((t) => t !== ''))]
}

const AUTH_CHOICES: ReadonlyArray<{ mode: McpAuthMode; label: string }> = [
  { mode: 'none', label: 'No credential — the server is open to SwarmCloud’s addresses' },
  { mode: 'dispatch', label: 'Per-run credential — the caller sends one with each task, and it dies with the task' },
  { mode: 'workspace_secret', label: 'Workspace secret — the secret this workspace grants to mcp:<name>' },
]

/** Register (editing null) or change one registration. Its name is fixed once registered. */
function ServerForm({ editing, onDone }: { editing: McpServer | null; onDone: () => void }) {
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

  const served = capacity.state.status === 'ok' || capacity.state.status === 'stale' ? mcpProfiles(capacity.state.data.runner_profiles) : null
  const offered = [...new Set([...(served ?? []), ...profiles])].sort()
  const verb = editing === null ? 'Register' : 'Save'

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
    <section className="pk-row" aria-labelledby={`${id}-h`}>
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
              What a task names: a lower-case letter, then letters, digits and hyphens. It cannot be changed later.
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
              {served === null
                ? 'The runner list could not be read, so this registration allows every profile that takes MCP servers.'
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
              <span className="jb-note">None ticked allows every profile that takes MCP servers.</span>
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
          ? 'Sent once with this task, to the worker only, and destroyed when the task ends.'
          : 'Sent once with this workflow, to the workers of the steps that name it only, and destroyed when its tasks end.'}
        {' '}The field is emptied as soon as the platform answers.
      </span>
    </span>
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
                <label className={`btp-opt${on ? ' is-on' : ''}`} title={s.description || undefined}>
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
          {picked.length >= max && <p className="btp-hint">A task names at most {max}.</p>}
        </>
      )}
    </fieldset>
  )
}
