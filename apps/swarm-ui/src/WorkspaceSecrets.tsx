import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react'
import {
  type SecretStoreApi,
  type WorkspaceSecret,
  type WorkspaceSecretCatalogue,
  type WorkspaceSecretConsumer,
  type WorkspaceSecretCreate,
  type WorkspaceSecretGrant,
  type WorkspaceSecretList,
  type WorkspaceSecretRotate,
} from './api'
import { Banner, Button, Dash, TypedConfirm } from './components'
import type { ApiError } from './fetch'
import { timeAgo } from './types'
import './styles/keys.css'

/**
 * THE SECRETS STORE'S PARTS (lane SEC-UI-LIST over SEC-STORE-API, then
 * SEC-UI-GRANTS, part of #1347; docs/design/workspace-secrets.md §5). The
 * store's list with a typed Delete, its grant matrix, Rotate and Add. Config ›
 * Secrets (Secrets.tsx, lane SSC-UI, part of #2158) is the page that draws
 * them: this file was that page, Settings › Workspace › Secrets, until the
 * secrets-scopes design gave one store several scopes, and each part now
 * writes to the store it is handed (`SecretStoreApi`: the workspace's own, or
 * one repository's, docs/design/secrets-scopes.md §2) and to no other.
 *
 * A VALUE IS NEVER HELD BY THESE PARTS. Their fields are uncontrolled: the
 * value lives in the DOM input and nowhere else -- not in React state (a
 * controlled input writes its value back as an attribute), not in storage,
 * not in the address, not in a log. Submit reads each field once, empties it
 * before the request leaves, and hands the value to the store's `create` or
 * `rotate`, which puts it in that one request's body. Nothing here can draw
 * it back: the API never serves a value, and `normWorkspaceSecret` copies a
 * record from an allow-list, so a value, a length or the last characters a
 * response carried by mistake are dropped before any of this sees them. A
 * refusal is the API's message, which never interpolates a value
 * (`payload_for`'s constants).
 *
 * THE ONE-TIME LINK'S TOKEN is the page's (Secrets.tsx): a part takes it
 * through `takeToken`, once, for the write its link was minted for.
 *
 * ROTATE KEEPS ADD'S RULES: uncontrolled fields, read once and emptied before
 * the request leaves, the value in that one `PUT .../value` body and nowhere
 * else.
 *
 * THE GRANT MATRIX writes ONE grant per tick: `PUT .../{name}/grants/{consumer}`
 * with `{enabled}`, for exactly the box ticked. The box moves at once and goes
 * back if the API refuses; a running attempt keeps what it was released
 * (§4.3), so the page says the change takes effect at the next attempt.
 *
 * A HOST-SCOPED SLOT IS ITS OWN COLUMN (CF-ACCESS-HOSTS-UI, part of #2442).
 * A grant into `cf-access` carries the exact hosts its secret may be sent to
 * (swarm_api `slot_hosts`), so that cell has a hosts field beside its box:
 * the grant's current list, add and remove, checked here as the API checks
 * it, and written with the box (`{enabled, slot, hosts}`) or by Save. A tick
 * with no host is refused before any call, in the API's own words. The field
 * edits who may read the secret and where it goes, never what it is: nothing
 * here reads or holds a value.
 *
 * WHO MAY MANAGE is the API's `can_manage`. Only an explicit true offers
 * Rotate, Delete or a live box; the API is still the gate.
 */


/**
 * A kind in the reader's words. An unknown one is shown as the API spells it.
 * Built from pairs rather than `name: '...'` lines: the worker's publish-time
 * credential scan reads a line like that as a key assignment.
 */
export const KIND_WORDS: Readonly<Record<string, string>> = Object.fromEntries([
  ['api_key', 'API key'],
  ['bearer_token', 'Bearer token'],
  ['basic_auth', 'Username and password'],
  ['oauth_client', 'OAuth client'],
  ['opaque', 'Opaque'],
])

/**
 * The structured kinds' two fields (§2.1, swarm_api `STRUCTURED_FIELDS`): the
 * key the API takes, the label, and whether the field is masked. The first of
 * each pair names an identity, the second is the secret part.
 */
export const STRUCTURED: Readonly<Record<string, readonly [StructuredField, StructuredField]>> = {
  basic_auth: [
    { key: 'username', label: 'Username', masked: false },
    { key: 'password', label: 'Password', masked: true },
  ],
  oauth_client: [
    { key: 'client_id', label: 'Client ID', masked: false },
    { key: 'client_secret', label: 'Client secret', masked: true },
  ],
}

interface StructuredField {
  key: string
  label: string
  masked: boolean
}

export const DEFAULT_PROVIDER = 'typesafe'
export const DEFAULT_KIND = 'api_key'

/** "3d ago by alice@…", or a dash's reason. */
function setWords(s: WorkspaceSecret): string {
  if (s.set_at === null) return s.set_by === null ? 'not said' : `by ${s.set_by}`
  return s.set_by === null ? timeAgo(s.set_at) : `${timeAgo(s.set_at)} by ${s.set_by}`
}

/**
 * The "Last used" cell: an API that does not serve the field says nothing
 * either way, and the cell says so as a dash with its reason (visual QA pass
 * 1, config-22): "not served" in every row was the API's word, not the reader's.
 */
function LastUsed({ at }: { at: string | null | undefined }) {
  if (at === undefined) return <td data-label="Last used"><Dash why="Not reported by this API yet: it does not say when a secret was last released to an attempt" /></td>
  return at === null ? <td data-label="Last used">never</td> : <td data-label="Last used" title={at}>{timeAgo(at)}</td>
}

/**
 * THE STORE'S LIST: name, provider, kind, set, version, last used and what it
 * is granted to, with Rotate and a typed Delete for a manager. Rotate opens
 * the one-time form (`onRotate`, the page's), never a field in this table.
 * `note` is what the page knows about a row that the store does not: which
 * scope wins its name (the overview's shadowing, §2.4).
 */
export function SecretList({
  data,
  store,
  label,
  onChange,
  onRotate,
  note,
}: {
  data: WorkspaceSecretList
  store: SecretStoreApi
  /** The table's accessible name: "Workspace secrets", "Repository secrets". */
  label: string
  onChange: () => void
  onRotate: (name: string) => void
  note?: (name: string) => ReactNode
}) {
  const [deleting, setDeleting] = useState<string | null>(null)
  const [deleted, setDeleted] = useState<string | null>(null)
  const target = deleting === null ? undefined : data.secrets.find((s) => s.name === deleting)
  return (
    <>
      {deleted !== null && (
        <p className="pk-saved" role="status">
          {deleted} was deleted. Running attempts keep the copy they were given.
        </p>
      )}
      {data.secrets.length === 0 ? (
        <p className="ur-small">No secret is stored here yet.</p>
      ) : (
        // The registered-MCP-servers table's rules (keys.css), which also
        // stack it into cards on a phone: the same shape of list, one page over.
        <table role="table" className="mcp-servers" aria-label={label}>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Provider</th>
              <th scope="col">Kind</th>
              <th scope="col">Set</th>
              <th scope="col">Version</th>
              <th scope="col" data-col="last">Last used</th>
              <th scope="col">Granted to</th>
              {data.can_manage && <th scope="col" aria-label="Actions" />}
            </tr>
          </thead>
          <tbody>
            {data.secrets.map((s) => {
              const on = s.grants.filter((g) => g.enabled).map((g) => g.consumer)
              const said = note?.(s.name)
              return (
                <tr key={s.name}>
                  <th scope="row" data-label="Name">
                    <code>{s.name}</code>
                    {said !== undefined && said !== null && <span className="sec-notes">{said}</span>}
                  </th>
                  <td data-label="Provider">{s.provider || 'not said'}</td>
                  <td data-label="Kind">{KIND_WORDS[s.kind] ?? (s.kind || 'not said')}</td>
                  <td data-label="Set" title={s.set_at ?? undefined}>{setWords(s)}</td>
                  <td data-label="Version">{s.current_version ?? 'not said'}</td>
                  <LastUsed at={s.last_used_at} />
                  <td data-label="Granted to">
                    {on.length === 0 ? 'nothing yet' : on.map((c, i) => <span key={c}>{i > 0 && ', '}<code>{c}</code></span>)}
                  </td>
                  {data.can_manage && (
                    // §6.2: a forge row is written by its own path, so it offers neither.
                    <td data-label="Actions">
                      {isForge(s) ? (
                        <span className="jb-note">Set under Access and Git tokens</span>
                      ) : (
                        <span className="pk-form-row">
                          <Button kind="secondary" size="sm" aria-label={`Rotate ${s.name}`} onClick={() => onRotate(s.name)}>
                            Rotate
                          </Button>
                          <Button kind="danger" size="sm" aria-label={`Delete ${s.name}`} onClick={() => {
                            setDeleted(null)
                            setDeleting(s.name)
                          }}>
                            Delete
                          </Button>
                        </span>
                      )}
                    </td>
                  )}
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {target !== undefined && (
        <DeleteSecret
          secret={target}
          store={store}
          onKeep={() => setDeleting(null)}
          onDone={() => {
            setDeleting(null)
            setDeleted(target.name)
            onChange()
          }}
        />
      )}
    </>
  )
}

/**
 * DELETE IS TYPED (§5; CLAUDE.md: a destructive action takes a typed
 * confirmation). The person types the secret's NAME, which is also what the
 * API's body repeats. The dialog lists every enabled grant the delete turns
 * off, and says what it does not reach: an attempt already running keeps
 * the copy it was released (§4.3).
 */
function DeleteSecret({ secret, store, onKeep, onDone }: { secret: WorkspaceSecret; store: SecretStoreApi; onKeep: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const on = secret.grants.filter((g) => g.enabled).map((g) => g.consumer)

  async function confirm() {
    if (busy) return
    setBusy(true)
    setFailed(null)
    const r = await store.remove(secret.name)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(`${secret.name} was not deleted: ${r.error.message}`)
      return
    }
    onDone()
  }

  return (
    <TypedConfirm
      title={`Delete ${secret.name}`}
      name={secret.name}
      verb="Delete"
      keep="Keep it"
      busy={busy ? 'Deleting…' : false}
      error={failed}
      onConfirm={() => void confirm()}
      onClose={onKeep}
    >
      <p>
        Every version of <code>{secret.name}</code> is destroyed, not disabled: the only way back is adding the value
        again.
      </p>
      {on.length === 0 ? (
        <p>No grant is enabled, so no consumer loses it.</p>
      ) : (
        <>
          <p>These grants are disabled:</p>
          <ul className="pk-uses">
            {on.map((c) => <li key={c}><code>{c}</code></li>)}
          </ul>
        </>
      )}
      <p>Running attempts keep the copy they were already given; the next attempt gets nothing.</p>
    </TypedConfirm>
  )
}

/** A forge token's kind (§6.2): listed, written by its own path, granted only to `component:git`. */
const FORGE_KIND = 'forge_token'

export function isForge(s: Pick<WorkspaceSecret, 'kind'>): boolean {
  return s.kind === FORGE_KIND
}

/** A kind as the object of "reads": "jev-browser reads an API key". Pairs, as KIND_WORDS. */
const KIND_PHRASE: Readonly<Record<string, string>> = Object.fromEntries([
  ['api_key', 'an API key'],
  ['bearer_token', 'a bearer token'],
  ['basic_auth', 'a username and password'],
  ['oauth_client', 'an OAuth client'],
  ['opaque', 'an opaque value'],
  [FORGE_KIND, 'a forge token'],
])

/** The matrix's column groups, in §5's order. A consumer of another kind is not a column. */
export const GROUPS: readonly (readonly [string, string])[] = [
  ['profile', 'Profiles'],
  ['tool', 'Tools'],
  ['component', 'Components'],
]

/**
 * ONE CELL OF THE MATRIX, decided from the catalogue alone, as the API's
 * `check_grant` decides a write: `null` is no cell at all (`component:git`
 * off a forge row, §6.2); otherwise a box, `open` when a tick could be
 * written, with the reason it cannot when it is not, and the slot to name
 * only when the consumer reads more than one.
 */
export interface Cell {
  open: boolean
  /** The forge row's git cell: shown ticked whatever its grant says (§6.2). */
  locked: boolean
  reason: string | null
  title: string | undefined
  slot: string | null
}

export function cellOf(secret: Pick<WorkspaceSecret, 'kind'>, c: WorkspaceSecretConsumer): Cell | null {
  const forgeOnly = c.slots.length > 0 && c.slots.every((sl) => sl.kinds.length > 0 && sl.kinds.every((k) => k === FORGE_KIND))
  if (isForge(secret)) {
    if (forgeOnly) return { open: false, locked: true, reason: 'the worker’s git credentials; locked', title: c.note || undefined, slot: null }
    return { open: false, locked: false, reason: 'a forge token is never granted here', title: 'A forge token is never readable by an agent (design §6.2).', slot: null }
  }
  if (forgeOnly) return null
  if (c.slots.length === 0) return { open: false, locked: false, reason: `${c.name} reads no secret yet`, title: c.note || undefined, slot: null }
  if (!c.grantable) return { open: false, locked: false, reason: c.note || `${c.name} is not grantable here`, title: undefined, slot: null }
  const fits = c.slots.filter((sl) => sl.kinds.includes(secret.kind))
  if (fits.length === 0) {
    const kinds = Array.from(new Set(c.slots.flatMap((sl) => sl.kinds)))
    return { open: false, locked: false, reason: `${c.name} reads ${kinds.map((k) => KIND_PHRASE[k] ?? k).join(' or ')}`, title: undefined, slot: null }
  }
  // A host-scoped slot is offered only by its own column (`columnsOf`), where
  // its hosts are asked for: Add's grants and the plain cell fill another.
  const pick = fits.find((sl) => !isHostScoped(sl.slot)) ?? fits[0]!
  return { open: true, locked: false, reason: null, title: undefined, slot: c.slots.length > 1 ? pick.slot : null }
}

/**
 * HOST-SCOPED SLOTS AND THEIR RULE, as swarm_api/workspace_secrets.py states
 * them (`HOST_SCOPED_SLOTS`, `MAX_SLOT_HOSTS`, `SLOT_HOST`, `slot_hosts`).
 * The API is still the gate; these let the form refuse before the call, in
 * the API's words, and a test holds them to that module.
 */
export const HOST_SCOPED_SLOTS: readonly string[] = ['cf-access']
export const MAX_SLOT_HOSTS = 16
/** A lower-case DNS name with a dot: no port, IP literal, wildcard, scheme or path. Matched exactly. */
export const SLOT_HOST = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/
/** `slot_hosts`' refusal of an enabled grant with no host. */
export const hostsNeeded = (slot: string) =>
  `a grant into the ${slot} slot names the hosts its secret may be sent to (\`hosts\`, 1 to ${MAX_SLOT_HOSTS} host names); it is sent to no other`
/** `slot_hosts`' refusal of a list over the cap. */
export const HOSTS_TOO_MANY = `\`hosts\` is a list of 1 to ${MAX_SLOT_HOSTS} host names`
/** `slot_hosts`' refusal of a host `SLOT_HOST` does not match. */
export const HOST_REFUSED = 'each host is a lower-case DNS name with a dot, matched exactly: no scheme, port, path, wildcard or IP address'

export function isHostScoped(slot: string | null): boolean {
  return slot !== null && HOST_SCOPED_SLOTS.includes(slot)
}

/** Why `host` cannot join `hosts`, in the API's words, or null when it can. */
export function hostProblem(hosts: readonly string[], host: string): string | null {
  if (!SLOT_HOST.test(host)) return HOST_REFUSED
  if (hosts.includes(host)) return `${host} is listed already`
  if (hosts.length >= MAX_SLOT_HOSTS) return HOSTS_TOO_MANY
  return null
}

/**
 * ONE COLUMN OF THE MATRIX: a consumer, or one host-scoped slot of it. `view`
 * is the consumer as that column offers it (its other slots, or that slot
 * alone), which `cellOf` decides from; `hostSlot` names the slot a
 * host-scoped column always writes.
 */
export interface Column {
  key: string
  consumer: WorkspaceSecretConsumer
  view: WorkspaceSecretConsumer
  hostSlot: string | null
}

export function columnsOf(c: WorkspaceSecretConsumer): Column[] {
  const hosted = c.slots.filter((sl) => isHostScoped(sl.slot))
  if (hosted.length === 0) return [{ key: c.consumer, consumer: c, view: c, hostSlot: null }]
  const rest = c.slots.filter((sl) => !isHostScoped(sl.slot))
  return [
    ...(rest.length === 0 ? [] : [{ key: c.consumer, consumer: c, view: { ...c, slots: rest }, hostSlot: null }]),
    ...hosted.map((sl) => ({ key: `${c.consumer}#${sl.slot}`, consumer: c, view: { ...c, slots: [sl] }, hostSlot: sl.slot })),
  ]
}

/** A column's cell: `cellOf` on its view, naming the slot whenever the consumer reads more than one. */
export function cellAt(secret: Pick<WorkspaceSecret, 'kind'>, col: Column): Cell | null {
  const cell = cellOf(secret, col.view)
  if (cell === null || !cell.open || col.consumer.slots.length < 2) return cell
  const slot = col.hostSlot ?? cell.slot ?? col.view.slots.find((sl) => sl.kinds.includes(secret.kind))?.slot ?? null
  return { ...cell, slot }
}

/** The consumer's grant when it fills this column's slot: a plain column's is any slot not host-scoped. */
function grantIn(secret: WorkspaceSecret, col: Column): WorkspaceSecretGrant | undefined {
  const g = secret.grants.find((x) => x.consumer === col.consumer.consumer)
  if (g === undefined) return undefined
  return (col.hostSlot === null ? !isHostScoped(g.slot) : g.slot === col.hostSlot) ? g : undefined
}

const cellKey = (name: string, column: string) => `${name}\u0000${column}`

/**
 * THE GRANT MATRIX (§5): a row per secret, a column per catalogue consumer
 * (and per host-scoped slot of one, `columnsOf`), grouped Profiles · Tools ·
 * Components, a box per cell. A tick moves the box at once and writes ONE
 * grant; a refusal puts it back and says why. A wide matrix scrolls inside
 * its own `.table-wrap`, so the page does not.
 */
export function GrantMatrix({ data, store, catalogue, onChange }: { data: WorkspaceSecretList | null; store: SecretStoreApi; catalogue: WorkspaceSecretCatalogue; onChange: () => void }) {
  const id = useId()
  // The boxes moved since the list was last read: what was ticked, or what
  // the API answered. Cleared when the list is read again, which then says.
  const [moved, setMoved] = useState<Readonly<Record<string, boolean>>>({})
  const [busy, setBusy] = useState<Readonly<Record<string, true>>>({})
  const [said, setSaid] = useState<{ ok: boolean; words: string } | null>(null)
  // A host-scoped cell's list as edited here, and what the form refused in
  // it; both cleared when the list is read again, which then shows the
  // grant's own hosts.
  const [drafts, setDrafts] = useState<Readonly<Record<string, readonly string[]>>>({})
  const [refused, setRefused] = useState<Readonly<Record<string, string>>>({})
  useEffect(() => {
    setMoved({})
    setDrafts({})
    setRefused({})
  }, [data])
  if (data === null) return null
  const groups = GROUPS.map(([kind, label]) => ({ label, cols: catalogue.consumers.filter((c) => c.kind === kind).flatMap(columnsOf) }))
    .filter((g) => g.cols.length > 0)
  const cols = groups.flatMap((g) => g.cols)
  if (cols.length === 0) return <p className="ur-small">The catalogue names no consumer, so there is nothing to grant to.</p>
  if (data.secrets.length === 0) return <p className="ur-small">No secret is stored yet, so there is nothing to grant.</p>

  const hostsOf = (secret: WorkspaceSecret, col: Column): readonly string[] =>
    drafts[cellKey(secret.name, col.key)] ?? grantIn(secret, col)?.hosts ?? []
  const refuse = (key: string, words: string | null) =>
    setRefused((r) => {
      const { [key]: _was, ...rest } = r
      return words === null ? rest : { ...rest, [key]: words }
    })

  async function write(secret: WorkspaceSecret, col: Column, cell: Cell, next: boolean) {
    const key = cellKey(secret.name, col.key)
    if (!data?.can_manage || !cell.open || busy[key]) return
    const hosts = col.hostSlot === null ? null : hostsOf(secret, col)
    if (col.hostSlot !== null && next && hosts !== null && hosts.length === 0) {
      // `slot_hosts` refuses this; say so here and send nothing.
      refuse(key, hostsNeeded(col.hostSlot))
      return
    }
    refuse(key, null)
    setMoved((m) => ({ ...m, [key]: next }))
    setBusy((b) => ({ ...b, [key]: true }))
    setSaid(null)
    // An untick keeps a host-scoped grant's list, so a tick back finds it.
    const sent = hosts !== null && hosts.length > 0 ? hosts : null
    const r = await store.grant(secret.name, col.consumer.consumer, next, cell.slot, sent)
    setBusy((b) => {
      const { [key]: _done, ...rest } = b
      return rest
    })
    const where = col.hostSlot === null ? '' : ` in its ${col.hostSlot} slot`
    if (r.status === 'error') {
      setMoved((m) => {
        const { [key]: _back, ...rest } = m
        return rest
      })
      setSaid({ ok: false, words: `${col.consumer.consumer}${where} on ${secret.name} was not changed: ${r.error.message}` })
      return
    }
    const served = r.status === 'ok' && r.data !== null ? grantIn(r.data, col) : undefined
    const now = r.status === 'ok' && r.data !== null ? served?.enabled === true : next
    setMoved((m) => ({ ...m, [key]: now }))
    if (served !== undefined && col.hostSlot !== null) setDrafts((d) => ({ ...d, [key]: served.hosts }))
    const to = now && sent !== null ? `, sent only to ${(served?.hosts ?? sent).join(', ')}` : ''
    setSaid({
      ok: true,
      words: `${col.consumer.consumer} ${now ? 'may now read' : 'may no longer read'} ${secret.name}${where}${to}. This takes effect at the next attempt; an attempt already running keeps what it was given.`,
    })
    onChange()
  }

  function edit(secret: WorkspaceSecret, col: Column, on: boolean, next: readonly string[]) {
    const key = cellKey(secret.name, col.key)
    setDrafts((d) => ({ ...d, [key]: next }))
    refuse(key, on && next.length === 0 && col.hostSlot !== null ? hostsNeeded(col.hostSlot) : null)
  }

  return (
    <>
      <p className="pk-lead">
        Tick a box to let that runner profile, tool or platform component read the secret. A box that cannot be
        ticked says why: the consumer reads another kind, or reads no secret yet. A <code>cf-access</code> grant
        also names the hosts its secret may be sent to, exactly, and no other.
      </p>
      {!data.can_manage && <p className="ur-small">You can read the grants here; only someone who manages this workspace&rsquo;s secrets can change them.</p>}
      <div className="table-wrap" role="region" aria-label="Grant matrix" tabIndex={0}>
        <table className="pools sec-matrix" aria-label="Secret grants">
          <thead>
            <tr>
              <th scope="col" rowSpan={2}>Secret</th>
              {groups.map((g) => <th key={g.label} scope="colgroup" colSpan={g.cols.length}>{g.label}</th>)}
            </tr>
            <tr>
              {cols.map((col) => (
                <th key={col.key} scope="col" title={col.hostSlot === null ? col.consumer.consumer : `${col.consumer.consumer}, its ${col.hostSlot} slot`}>
                  <code>{col.consumer.name}</code>
                  {col.hostSlot !== null && <span className="sec-sub">{col.hostSlot} · hosts</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.secrets.map((s) => (
              <tr key={s.name}>
                <th scope="row"><code>{s.name}</code></th>
                {cols.map((col) => {
                  const cell = cellAt(s, col)
                  if (cell === null) return <td key={col.key} />
                  const key = cellKey(s.name, col.key)
                  const served = grantIn(s, col)?.enabled === true
                  const checked = cell.locked || (moved[key] ?? served)
                  const why = cell.reason === null ? undefined : `${id}-${s.name}-${col.key}`
                  const label = col.hostSlot === null
                    ? `Grant ${s.name} to ${col.consumer.consumer}`
                    : `Grant ${s.name} to ${col.consumer.consumer} in its ${col.hostSlot} slot`
                  return (
                    <td key={col.key} title={cell.title} className={col.hostSlot !== null && cell.open ? 'sec-hosted' : undefined}>
                      <input
                        type="checkbox"
                        aria-label={label}
                        aria-describedby={why}
                        checked={checked}
                        disabled={!data.can_manage || !cell.open || busy[key] === true}
                        onChange={(e) => void write(s, col, cell, e.target.checked)}
                      />
                      {cell.reason !== null && <span id={why} className="jb-note"> {cell.reason}</span>}
                      {col.hostSlot !== null && cell.open && (
                        <HostList
                          secret={s.name}
                          consumer={col.consumer.consumer}
                          slot={col.hostSlot}
                          hosts={hostsOf(s, col)}
                          saved={grantIn(s, col)?.hosts ?? []}
                          on={checked}
                          canEdit={data.can_manage && busy[key] !== true}
                          refused={refused[key] ?? null}
                          onEdit={(next) => edit(s, col, checked, next)}
                          onRefuse={(words) => refuse(key, words)}
                          onSave={() => void write(s, col, cell, true)}
                        />
                      )}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {said !== null && (said.ok
        ? <p className="pk-saved" role="status">{said.words}</p>
        : <Banner tone="bad" title={said.words} role="alert" />)}
    </>
  )
}

/**
 * A HOST-SCOPED CELL'S HOSTS: the list, each with Remove, and a field to add
 * one. Checked as `slot_hosts` checks it, so a bad host, a duplicate or a
 * 17th is refused here in the API's words. On an enabled grant an edit is a
 * draft until Save writes it; on an unticked one the tick sends it. Only
 * host names pass through here: no value is read, held or drawn.
 */
function HostList({
  secret,
  consumer,
  slot,
  hosts,
  saved,
  on,
  canEdit,
  refused,
  onEdit,
  onRefuse,
  onSave,
}: {
  secret: string
  consumer: string
  slot: string
  hosts: readonly string[]
  saved: readonly string[]
  on: boolean
  canEdit: boolean
  refused: string | null
  onEdit: (next: readonly string[]) => void
  onRefuse: (words: string) => void
  onSave: () => void
}) {
  const field = useRef<HTMLInputElement>(null)
  const what = `${secret} in ${consumer}'s ${slot} slot`
  const changed = hosts.length !== saved.length || hosts.some((h, i) => h !== saved[i])
  function add(e: FormEvent) {
    e.preventDefault()
    const input = field.current
    if (input === null) return
    const host = input.value.trim()
    if (host === '') return
    const problem = hostProblem(hosts, host)
    if (problem !== null) {
      onRefuse(problem)
      return
    }
    input.value = ''
    onEdit([...hosts, host].sort())
  }
  return (
    <div className="sec-hosts">
      {hosts.length === 0
        ? <p className="jb-note">No host yet: it is sent to none.</p>
        : (
          <ul className="sec-hosts-list" aria-label={`Hosts for ${what}`}>
            {hosts.map((h) => (
              <li key={h}>
                <code>{h}</code>
                {canEdit && (
                  <button type="button" className="sec-hosts-x" aria-label={`Remove ${h} from ${what}`} onClick={() => onEdit(hosts.filter((x) => x !== h))}>
                    &times;
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      {canEdit && (
        <form className="sec-hosts-add" aria-label={`Add a host for ${what}`} onSubmit={add}>
          <input
            ref={field}
            type="text"
            className="pk-input"
            aria-label={`Host for ${what}`}
            placeholder="admin.example.com"
            autoComplete="off"
            spellCheck={false}
            inputMode="url"
          />
          <Button kind="secondary" size="sm" type="submit">Add</Button>
          {on && changed && <Button kind="primary" size="sm" onClick={onSave}>Save hosts</Button>}
        </form>
      )}
      <span className="jb-note">{hosts.length} of {MAX_SLOT_HOSTS} hosts, matched exactly</span>
      {refused !== null && <p className="sec-hosts-bad" role="alert">{refused}</p>}
    </div>
  )
}

/**
 * ROTATE (§5, Q5): a new value, in a password field, posted once to
 * `PUT .../{name}/value`. The previous version stays enabled for 24 hours,
 * then the rotation sweep disables it. The fields are uncontrolled and
 * emptied before the request leaves, as Add's are; what the page says after
 * is the version, never anything of the value.
 */
export function RotateSecret({
  secret,
  store,
  takeToken,
  onCancel,
  onDone,
}: {
  secret: WorkspaceSecret
  store: SecretStoreApi
  takeToken: () => string | null
  onCancel: () => void
  onDone: (words: string) => void
}) {
  const id = useId()
  const [filled, setFilled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const value = useRef<HTMLInputElement>(null)
  const first = useRef<HTMLInputElement>(null)
  const second = useRef<HTMLInputElement>(null)
  const structured = STRUCTURED[secret.kind]
  const fields = (): RefObject<HTMLInputElement>[] => (structured === undefined ? [value] : [first, second])
  const recheck = () => setFilled(fields().every((f) => (f.current?.value ?? '') !== ''))

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    // Read once, and emptied before the request leaves, whatever it answers.
    const read = fields().map((f) => {
      const v = f.current?.value ?? ''
      if (f.current !== null) f.current.value = ''
      return v
    })
    setFilled(false)
    if (read.some((v) => v === '')) return
    const body: WorkspaceSecretRotate = structured === undefined
      ? { value: read[0]! }
      : { fields: { [structured[0].key]: read[0]!, [structured[1].key]: read[1]! } }
    setBusy(true)
    setFailed(null)
    const r = await store.rotate(secret.name, body, takeToken())
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error.code === 'SECRET_LINK_INVALID'
        ? `${secret.name} was not rotated: the one-time link is used, expired or not yours. Nothing was written. Open the form again from Rotate, or ask /sc for a new link.`
        : `${secret.name} was not rotated: ${r.error.message}`)
      return
    }
    const version = r.status === 'ok' ? r.data?.current_version ?? null : null
    onDone(`${secret.name} was rotated${version === null ? '' : `: version ${version} is current`}. The previous version stays enabled for 24 hours, then is disabled.`)
  }

  return (
    <>
      <form className="pk-form" autoComplete="off" aria-label={`Rotate ${secret.name}`} onSubmit={(e) => void save(e)}>
        <p className="pk-lead">
          A new version of <code>{secret.name}</code>. Attempts that start after this read the new value; the
          previous one stays enabled for 24 hours, for attempts already running.
        </p>
        {structured === undefined ? (
          <SecretField id={`${id}-value`} label="New value" masked input={value} onInput={recheck} />
        ) : (
          <>
            <SecretField id={`${id}-${structured[0].key}`} label={`New ${structured[0].label.toLowerCase()}`}
              masked={structured[0].masked} input={first} onInput={recheck} />
            <SecretField id={`${id}-${structured[1].key}`} label={`New ${structured[1].label.toLowerCase()}`}
              masked={structured[1].masked} input={second} onInput={recheck} />
          </>
        )}
        <div className="pk-form-row">
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Rotating…' : false} disabled={!filled}>
            Rotate
          </Button>
          <Button kind="ghost" size="sm" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      </form>
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </>
  )
}

/** A create's refusal, in words. The API's message never carries a value. */
function refusalWords(e: ApiError, name: string): string {
  if (e.code === 'SECRET_LINK_INVALID') {
    return `${name} was not added: the one-time link is used, expired or not yours. Nothing was written. Open the form again from Add a secret, or ask /sc for a new link.`
  }
  return `${name} was not added: ${e.message}`
}

/** What the wizard already chose, so the form asks only for the value (SSC-UI). */
export interface AddFixed {
  name: string
  provider: string
  kind: string
}

export function AddSecret({
  catalogue,
  store,
  fixed = null,
  takeToken,
  onAdded,
}: {
  catalogue: WorkspaceSecretCatalogue
  store: SecretStoreApi
  /** The name, provider and kind the wizard chose; null asks for them here. */
  fixed?: AddFixed | null
  /** The one-time link's token, handed over once; null after, or without a link. */
  takeToken: () => string | null
  /** Called with the name, after the store answered. */
  onAdded: (name: string) => void
}) {
  const id = useId()
  const providers = catalogue.providers.length > 0 ? catalogue.providers : [DEFAULT_PROVIDER]
  const kinds = catalogue.kinds.length > 0 ? catalogue.kinds : [DEFAULT_KIND]
  const firstProvider = providers.includes(DEFAULT_PROVIDER) ? DEFAULT_PROVIDER : providers[0]!
  const firstKind = kinds.includes(DEFAULT_KIND) ? DEFAULT_KIND : kinds[0]!
  const [name, setName] = useState(fixed?.name ?? '')
  const [provider, setProvider] = useState(fixed?.provider ?? firstProvider)
  const [kind, setKind] = useState(fixed?.kind ?? firstKind)
  // Whether every value field holds something. A boolean, never a value.
  const [filled, setFilled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [added, setAdded] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const value = useRef<HTMLInputElement>(null)
  const first = useRef<HTMLInputElement>(null)
  const second = useRef<HTMLInputElement>(null)
  const structured = STRUCTURED[kind]

  const fields = (): RefObject<HTMLInputElement>[] => (structured === undefined ? [value] : [first, second])
  const recheck = () => {
    setFilled(fields().every((f) => (f.current?.value ?? '') !== ''))
    if (added !== null) setAdded(null)
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    const named = name.trim()
    // Read once, and emptied before the request leaves, whatever it answers:
    // a field is never a place the value waits.
    const read = fields().map((f) => {
      const v = f.current?.value ?? ''
      if (f.current !== null) f.current.value = ''
      return v
    })
    setFilled(false)
    if (named === '' || read.some((v) => v === '')) return
    const body: WorkspaceSecretCreate = structured === undefined
      ? { name: named, provider, kind, value: read[0]! }
      : { name: named, provider, kind, fields: { [structured[0].key]: read[0]!, [structured[1].key]: read[1]! } }
    setBusy(true)
    setAdded(null)
    setFailed(null)
    // The link is consumed before the write, so it is sent once.
    const r = await store.create(body, takeToken())
    setBusy(false)
    if (r.status === 'error') {
      setFailed(refusalWords(r.error, named))
      return
    }
    if (fixed === null) {
      setName('')
      setProvider(firstProvider)
      setKind(firstKind)
    }
    setAdded(named)
    onAdded(named)
  }

  return (
    <>
      <form className="pk-form" autoComplete="off" aria-label="Add a secret" onSubmit={(e) => void save(e)}>
        {fixed !== null ? (
          // The wizard chose these; the form asks for the value alone.
          <dl className="sec-kv">
            <dt>Name</dt>
            <dd><code>{fixed.name}</code></dd>
            <dt>Provider</dt>
            <dd>{fixed.provider}</dd>
            <dt>Kind</dt>
            <dd>{KIND_WORDS[fixed.kind] ?? fixed.kind}</dd>
          </dl>
        ) : (
          <>
        <label htmlFor={`${id}-name`}>Name</label>
        <input id={`${id}-name`} className="ur-search pk-input" value={name} required maxLength={64} spellCheck={false}
          autoCapitalize="off" autoCorrect="off" placeholder="typesafe-key" aria-describedby={`${id}-name-why`}
          onChange={(e) => {
            setName(e.target.value)
            if (added !== null) setAdded(null)
          }} />
        <span id={`${id}-name-why`} className="jb-note">
          What grants name: lower-case letters, digits and hyphens, starting with a letter or a digit. Never the value.
        </span>
        {/* Each label with its control, so a narrow form wraps a pair, never a label from its select. */}
        <div className="pk-form-row">
          <span className="pk-form-row">
            <label htmlFor={`${id}-provider`}>Provider</label>
            <select id={`${id}-provider`} value={provider} onChange={(e) => setProvider(e.target.value)}>
              {providers.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </span>
          <span className="pk-form-row">
            <label htmlFor={`${id}-kind`}>Kind</label>
            <select id={`${id}-kind`} value={kind} onChange={(e) => {
              setKind(e.target.value)
              // The value fields are drawn afresh for the new kind, empty.
              setFilled(false)
            }}>
              {kinds.map((k) => <option key={k} value={k}>{KIND_WORDS[k] ?? k}</option>)}
            </select>
          </span>
        </div>
          </>
        )}
        {structured === undefined ? (
          <SecretField key={`${kind}-value`} id={`${id}-value`} label="Value" masked input={value} onInput={recheck} />
        ) : (
          <>
            <SecretField key={`${kind}-${structured[0].key}`} id={`${id}-${structured[0].key}`} label={structured[0].label}
              masked={structured[0].masked} input={first} onInput={recheck} />
            <SecretField key={`${kind}-${structured[1].key}`} id={`${id}-${structured[1].key}`} label={structured[1].label}
              masked={structured[1].masked} input={second} onInput={recheck} />
          </>
        )}
        <div className="pk-form-row">
          <Button kind="primary" size="sm" type="submit" busy={busy ? 'Adding…' : false} disabled={!filled || name.trim() === ''}>
            {fixed === null ? 'Add' : 'Set secret'}
          </Button>
        </div>
      </form>
      {added !== null && fixed === null && (
        <p className="pk-saved" role="status">
          {added} was added. Grant it to a profile, tool or component before an agent can use it.
        </p>
      )}
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </>
  )
}

/**
 * One value field: UNCONTROLLED, so its value is never React state and never
 * an attribute; never prefilled; autocomplete off. Its identity half (a
 * username, a client id) is drawn plain, the secret half masked.
 */
function SecretField({
  id,
  label,
  masked,
  input,
  onInput,
}: {
  id: string
  label: string
  masked: boolean
  input: RefObject<HTMLInputElement>
  onInput: () => void
}) {
  return (
    <>
      <label htmlFor={id}>{label}</label>
      <input
        id={id}
        ref={input}
        className="ur-search pk-input"
        type={masked ? 'password' : 'text'}
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        placeholder={masked ? 'Paste the value' : undefined}
        onChange={onInput}
      />
    </>
  )
}
