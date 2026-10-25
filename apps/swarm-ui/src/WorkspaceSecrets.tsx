import { useEffect, useId, useLayoutEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode, type RefObject } from 'react'
import {
  createWorkspaceSecret,
  deleteWorkspaceSecret,
  loadWorkspaceSecretCatalogue,
  loadWorkspaceSecrets,
  mintWorkspaceSecretLink,
  rotateWorkspaceSecret,
  setWorkspaceSecretGrant,
  type WorkspaceSecret,
  type WorkspaceSecretCatalogue,
  type WorkspaceSecretConsumer,
  type WorkspaceSecretCreate,
  type WorkspaceSecretList,
  type WorkspaceSecretRotate,
} from './api'
import { Banner, Button, Card, TypedConfirm, routedClick } from './components'
import type { ApiError } from './fetch'
import { SECRETS_ADDRESS, addressToPath, secretsLinkToken } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import './styles/keys.css'

/**
 * SETTINGS › WORKSPACE › SECRETS (lane SEC-UI-LIST over SEC-STORE-API, part
 * of #1347; docs/design/workspace-secrets.md §5). The selected workspace's
 * secrets store: the list, Add and a typed Delete; then SEC-UI-GRANTS's
 * grant matrix and Rotate (`GrantMatrix`, `RotateSecret`).
 *
 * A VALUE IS NEVER HELD BY THIS PAGE. Its fields are uncontrolled: the value
 * lives in the DOM input and nowhere else -- not in React state (a controlled
 * input writes its value back as an attribute), not in storage, not in the
 * address, not in a log. Submit reads each field once, empties it before the
 * request leaves, and hands the value to `createWorkspaceSecret`, which puts
 * it in that one request's body. Nothing here can draw it back: the API never
 * serves a value, and `normWorkspaceSecret` copies a record from an
 * allow-list, so a value, a length or the last characters a response carried
 * by mistake are dropped before any of this sees them. A refusal is the API's
 * message, which never interpolates a value (`payload_for`'s constants).
 *
 * THE ONE-TIME LINK (`/settings/workspace/secrets?action=add&t=..`, paths.ts
 * `SECRETS_LINK_PATH`). App reads the token off the bar before its normalise
 * effect rewrites it (this page reads it itself when rendered alone), the
 * page drops `t` from the bar in a layout effect, before anything paints, and
 * the token is sent back as `link_token` with the first add, then forgotten:
 * the API consumes it before the write, so a retry goes without it. It is
 * held in a ref, never drawn.
 *
 * A rotate link (`?action=rotate&name=..&t=..`) opens Rotate for that name, and
 * its token goes back once, with that rotate only.
 *
 * ROTATE KEEPS ADD'S RULES: uncontrolled fields, read once and emptied before
 * the request leaves, the value in that one `PUT .../value` body and nowhere
 * else. The row then shows the version the list serves.
 *
 * THE GRANT MATRIX writes ONE grant per tick: `PUT .../{name}/grants/{consumer}`
 * with `{enabled}`, for exactly the box ticked (`use-in` is §9 Q3's list of
 * workspaces, not a cell). The box moves at once and goes back if the API
 * refuses; a running attempt keeps what it was released (§4.3), so the page
 * says the change takes effect at the next attempt.
 *
 * CONFIG › SECRETS MOUNTS IT (SSC-UI, part of #2158; secrets-scopes.md
 * §6.1): `embedded` draws it as the page's "For agents" section, with no
 * head of its own, for the workspace (`repoId` null) or for one registered
 * repository (`GET /v1/repositories/{repo_id}/secrets`, the store's shapes).
 * Embedded, EVERY SET OPENS A ONE-TIME FORM: Rotate mints the store's link
 * for that one name first (`POST .../links`) and the form sends its token
 * once, so a value is never written by a form that no link opened; adding
 * is the page's Add a secret wizard, and the add form here is drawn only
 * for an add link. `shadow` draws, under a name, what Config › Secrets'
 * overview says about it (Q1): the secret that wins it, or what it hides.
 *
 * WHO MAY MANAGE is the API's `can_manage` (`workspace_secrets.require_manager`:
 * a personal workspace's person; a group workspace's admin or a platform
 * admin). Only an explicit true offers Add, Rotate, Delete or a live box; the
 * API is still the gate.
 */

const LIST_ROUTE = 'GET /v1/workspace/secrets'
const REPO_LIST_ROUTE = 'GET /v1/repositories/{repo_id}/secrets'
const CATALOGUE_ROUTE = 'GET /v1/workspace/secrets/consumers'

/** The link as the route carries it; `token` is App's capture, absent when rendered alone. */
export interface SecretsLink {
  action: 'add' | 'rotate' | null
  name: string | null
  token?: string | null
}

/** The route's view (`action=add`, `action=rotate&name=..`) as a link. Never a token: the view carries none. */
export function secretsLinkOf(view: string): SecretsLink {
  const q = new URLSearchParams(view)
  const action = q.get('action')
  return { action: action === 'add' || action === 'rotate' ? action : null, name: q.get('name') }
}

/** Drop `t` from the address bar, keeping everything else. Idempotent. */
function dropLinkToken(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('t')) return
  url.searchParams.delete('t')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

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

export interface StructuredField {
  key: string
  label: string
  masked: boolean
}

const DEFAULT_PROVIDER = 'typesafe'
const DEFAULT_KIND = 'api_key'

/** A settings page, routed in-app like the panel's own links. */
function SettingsLink({ address, children }: { address: string; children: ReactNode }) {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(address))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
  return <a href={addressToPath(address)} onClick={go}>{children}</a>
}

/**
 * THE PANE'S ENTRY ON SETTINGS › WORKSPACE (§5: "a pane of the Workspace
 * page"). App draws it under the Workspace page's own cards; it reads
 * nothing, so the Workspace page costs no extra request.
 */
export function WorkspaceSecretsEntry() {
  return (
    <Card title="Secrets">
      <p className="pk-lead">
        The credentials this workspace stores for its agents, tools and components &mdash; a TypeSafe key, a search
        API key &mdash; and which of them each one is granted to. Values are stored write-only.
      </p>
      <p className="pk-uses">
        <SettingsLink address={SECRETS_ADDRESS}>Open Settings › Workspace › Secrets</SettingsLink>
      </p>
    </Card>
  )
}

export function WorkspaceSecretsScreen({
  link,
  repoId = null,
  embedded = false,
  shadow,
}: {
  link: SecretsLink | null
  /** A registration's id: that repository's secrets for agents. Null is the workspace's. */
  repoId?: string | null
  /** Drawn as Config › Secrets' "For agents" section: no head, Rotate through a link. */
  embedded?: boolean
  /** What the overview says about a name's shadowing, drawn under it; nothing for none. */
  shadow?: (name: string) => ReactNode
}) {
  const list = useUrRead(() => loadWorkspaceSecrets(repoId), repoId === null ? 'workspace-secrets' : `repository-secrets:${repoId}`)
  const catalogue = useUrRead(loadWorkspaceSecretCatalogue, 'workspace-secret-catalogue')
  // The link's token, read once; null once it has been sent. A ref, never
  // state or a prop of anything drawn: nothing renders it.
  const token = useRef<string | null | undefined>(undefined)
  if (token.current === undefined) {
    token.current = link === null || link.action === null
      ? null
      : link.token !== undefined ? link.token : secretsLinkToken(window.location.pathname, window.location.search)
  }
  useLayoutEffect(() => {
    if (link !== null) dropLinkToken()
  }, [link])
  // The token goes with the write its link was minted for, once: an add link
  // with the first add, a rotate link with the rotate of the name it names.
  // A link this page minted for itself (embedded Rotate): its token, for that one name.
  const minted = useRef<{ name: string; token: string } | null>(null)
  const takeToken = (action: 'add' | 'rotate', name: string | null = null) => {
    if (action === 'rotate' && minted.current !== null && minted.current.name === name) {
      const own = minted.current.token
      minted.current = null
      return own
    }
    if (link === null || link.action !== action || (action === 'rotate' && link.name !== name)) return null
    const t = token.current ?? null
    token.current = null
    return t
  }
  /**
   * Embedded, Rotate opens only through a one-time link: the link's own, or
   * one minted here for that name. The answer says why when none could be
   * minted, and no form opens.
   */
  const openRotate = async (name: string): Promise<string | null> => {
    if (!embedded) return null
    if (link !== null && link.action === 'rotate' && link.name === name && token.current) return null
    const r = await mintWorkspaceSecretLink('rotate', name, repoId)
    if (r.status === 'error') return `${name}: no one-time form was opened: ${r.error.message}`
    const t = r.status === 'ok' ? r.data.token : null
    if (t === null) return `${name}: no one-time form was opened: the link came back without a token.`
    minted.current = { name, token: t }
    return null
  }
  const manages = (list.state.status === 'ok' || list.state.status === 'stale') && list.state.data.can_manage
  const rotateFirst = link !== null && link.action === 'rotate' ? link.name : null

  return (
    <div className="pk-page">
      {!embedded && (
        <PageHead title="Secrets">
          <UrRefresh reads={[list, catalogue]} ageOf={[list]} />
        </PageHead>
      )}
      {link !== null && link.action === 'add' && (
        <Banner tone="info" title="Opened from a one-time link">
          Paste the value into the form below: the /sc command and this page&rsquo;s address never see it. The link
          works once, for ten minutes.
        </Banner>
      )}
      {link !== null && link.action === 'rotate' && (
        <Banner tone="info" title={`Opened from a one-time rotate link${link.name === null ? '' : ` for ${link.name}`}`}>
          Paste the new value into the Rotate form below: the /sc command and this page&rsquo;s address never see it.
          The link works once, for ten minutes, and only for the secret it names.
        </Banner>
      )}
      <Card title={embedded ? 'For agents' : 'Secrets'}>
        {embedded ? (
          <p className="pk-lead">
            {repoId === null
              ? 'Released to the profiles, tools and components in Granted to.'
              : 'Read only by the profiles, tools and components in Granted to, on the agents the line above names.'}{' '}
            A value is stored write-only: no page and no API route shows it, its length or any of its characters again.
          </p>
        ) : (
          <p className="pk-lead">
            The credentials this workspace stores for its agents, tools and platform components, and who each one is
            granted to. A value is stored write-only: no page and no API route shows it, its length or any of its
            characters again. Back to <SettingsLink address="settings/workspace">Settings › Workspace</SettingsLink>.
          </p>
        )}
        <UrRegion state={list.state} route={repoId === null ? LIST_ROUTE : REPO_LIST_ROUTE} what={repoId === null ? "The workspace's secrets" : "The repository's secrets"} plural onRetry={list.reload}>
          {(d) => <SecretList data={d} onChange={list.reload} rotateFirst={rotateFirst} takeToken={takeToken} repoId={repoId} openRotate={embedded ? openRotate : null} shadow={shadow} />}
        </UrRegion>
      </Card>
      {(list.state.status === 'ok' || list.state.status === 'stale') && (
        <Card title={embedded ? 'Who it is granted to' : 'Grants'}>
          <UrRegion state={catalogue.state} route={CATALOGUE_ROUTE} what="The consumers a secret may be granted to" plural onRetry={catalogue.reload}>
            {(c) => <GrantMatrix data={list.state.status === 'ok' || list.state.status === 'stale' ? list.state.data : null} catalogue={c} onChange={list.reload} repoId={repoId} />}
          </UrRegion>
        </Card>
      )}
      {manages && (!embedded || (link !== null && link.action === 'add')) && (
        <Card title="Add a secret">
          <UrRegion state={catalogue.state} route={CATALOGUE_ROUTE} what="The kinds and providers a secret is stored as" plural onRetry={catalogue.reload}>
            {(c) => <AddSecret catalogue={c} takeToken={() => takeToken('add')} onAdded={list.reload} repoId={repoId} />}
          </UrRegion>
        </Card>
      )}
    </div>
  )
}

/** "3d ago by alice@…", or a dash's reason. */
function setWords(s: WorkspaceSecret): string {
  if (s.set_at === null) return s.set_by === null ? 'not said' : `by ${s.set_by}`
  return s.set_by === null ? timeAgo(s.set_at) : `${timeAgo(s.set_at)} by ${s.set_by}`
}

/** The "Last used" cell: an API that does not serve the field says nothing either way, and the cell says so. */
function LastUsed({ at }: { at: string | null | undefined }) {
  if (at === undefined) return <td data-label="Last used" title="This API does not yet say when a secret was last released to an attempt.">not served</td>
  return at === null ? <td data-label="Last used">never</td> : <td data-label="Last used" title={at}>{timeAgo(at)}</td>
}

function SecretList({
  data,
  onChange,
  rotateFirst,
  takeToken,
  repoId = null,
  openRotate = null,
  shadow,
}: {
  data: WorkspaceSecretList
  onChange: () => void
  /** A rotate link's name: its Rotate form opens on load. */
  rotateFirst: string | null
  takeToken: (action: 'rotate', name: string) => string | null
  repoId?: string | null
  /** Embedded: mint the one-time link a Rotate form opens through; the refusal's words, or null. */
  openRotate?: ((name: string) => Promise<string | null>) | null
  shadow?: (name: string) => ReactNode
}) {
  const [deleting, setDeleting] = useState<string | null>(null)
  const [deleted, setDeleted] = useState<string | null>(null)
  const [rotating, setRotating] = useState<string | null>(rotateFirst)
  const [rotated, setRotated] = useState<string | null>(null)
  const [opening, setOpening] = useState<string | null>(null)
  const target = deleting === null ? undefined : data.secrets.find((s) => s.name === deleting)
  const turning = rotating === null || !data.can_manage ? undefined : data.secrets.find((s) => s.name === rotating && !isForge(s))
  return (
    <>
      {!data.can_manage && (
        <p className="ur-small">
          Only this workspace&rsquo;s admin or a platform admin can add, rotate, grant or delete its secrets (in a
          personal workspace, its person). You can read them here.
        </p>
      )}
      {deleted !== null && (
        <p className="pk-saved" role="status">
          {deleted} was deleted. Running attempts keep the copy they were given.
        </p>
      )}
      {rotated !== null && (
        <p className="pk-saved" role="status">
          {rotated}
        </p>
      )}
      {data.secrets.length === 0 ? (
        <p className="ur-small">No secret is stored for {data.tenant_id || 'this workspace'} yet.</p>
      ) : (
        // The registered-MCP-servers table's rules (keys.css), which also
        // stack it into cards on a phone: the same shape of list, one page over.
        <table role="table" className="mcp-servers" aria-label="Workspace secrets">
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
              return (
                <tr key={s.name}>
                  <th scope="row" data-label="Name"><code>{s.name}</code>{shadow?.(s.name)}</th>
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
                          <Button kind="secondary" size="sm" aria-label={`Rotate ${s.name}`} busy={opening === s.name ? 'Opening…' : false} onClick={() => {
                            setRotated(null)
                            if (openRotate === null) {
                              setRotating(s.name)
                              return
                            }
                            setOpening(s.name)
                            setRotating(null)
                            void openRotate(s.name).then((refused) => {
                              setOpening(null)
                              if (refused === null) setRotating(s.name)
                              else setRotated(refused)
                            })
                          }}>
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
      {turning !== undefined && (
        <RotateSecret
          key={turning.name}
          secret={turning}
          repoId={repoId}
          oneTime={openRotate !== null}
          takeToken={() => takeToken('rotate', turning.name)}
          onCancel={() => setRotating(null)}
          onDone={(words) => {
            setRotating(null)
            setRotated(words)
            onChange()
          }}
        />
      )}
      {target !== undefined && (
        <DeleteSecret
          secret={target}
          repoId={repoId}
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
function DeleteSecret({ secret, repoId = null, onKeep, onDone }: { secret: WorkspaceSecret; repoId?: string | null; onKeep: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const on = secret.grants.filter((g) => g.enabled).map((g) => g.consumer)

  async function confirm() {
    if (busy) return
    setBusy(true)
    setFailed(null)
    const r = await deleteWorkspaceSecret(secret.name, repoId)
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

function isForge(s: WorkspaceSecret): boolean {
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
const GROUPS: readonly (readonly [string, string])[] = [
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
interface Cell {
  open: boolean
  /** The forge row's git cell: shown ticked whatever its grant says (§6.2). */
  locked: boolean
  reason: string | null
  title: string | undefined
  slot: string | null
}

function cellOf(secret: WorkspaceSecret, c: WorkspaceSecretConsumer): Cell | null {
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
  return { open: true, locked: false, reason: null, title: undefined, slot: c.slots.length > 1 ? fits[0]!.slot : null }
}

const cellKey = (name: string, consumer: string) => `${name}\u0000${consumer}`

/**
 * THE GRANT MATRIX (§5): a row per secret, a column per catalogue consumer,
 * grouped Profiles · Tools · Components, a box per cell. A tick moves the
 * box at once and writes ONE grant; a refusal puts it back and says why.
 * A wide matrix scrolls inside its own `.table-wrap`, so the page does not.
 */
function GrantMatrix({ data, catalogue, onChange, repoId = null }: { data: WorkspaceSecretList | null; catalogue: WorkspaceSecretCatalogue; onChange: () => void; repoId?: string | null }) {
  const id = useId()
  // The boxes moved since the list was last read: what was ticked, or what
  // the API answered. Cleared when the list is read again, which then says.
  const [moved, setMoved] = useState<Readonly<Record<string, boolean>>>({})
  const [busy, setBusy] = useState<Readonly<Record<string, true>>>({})
  const [said, setSaid] = useState<{ ok: boolean; words: string } | null>(null)
  useEffect(() => {
    setMoved({})
  }, [data])
  if (data === null) return null
  const groups = GROUPS.map(([kind, label]) => ({ label, cols: catalogue.consumers.filter((c) => c.kind === kind) }))
    .filter((g) => g.cols.length > 0)
  const cols = groups.flatMap((g) => g.cols)
  if (cols.length === 0) return <p className="ur-small">The catalogue names no consumer, so there is nothing to grant to.</p>
  if (data.secrets.length === 0) return <p className="ur-small">No secret is stored yet, so there is nothing to grant.</p>

  async function tick(secret: WorkspaceSecret, c: WorkspaceSecretConsumer, cell: Cell, next: boolean) {
    const key = cellKey(secret.name, c.consumer)
    if (!data?.can_manage || !cell.open || busy[key]) return
    setMoved((m) => ({ ...m, [key]: next }))
    setBusy((b) => ({ ...b, [key]: true }))
    setSaid(null)
    const r = await setWorkspaceSecretGrant(secret.name, c.consumer, next, cell.slot, repoId)
    setBusy((b) => {
      const { [key]: _done, ...rest } = b
      return rest
    })
    if (r.status === 'error') {
      setMoved((m) => {
        const { [key]: _back, ...rest } = m
        return rest
      })
      setSaid({ ok: false, words: `${c.consumer} on ${secret.name} was not changed: ${r.error.message}` })
      return
    }
    const served = r.status === 'ok' ? r.data : null
    const now = served?.grants.find((g) => g.consumer === c.consumer)?.enabled ?? next
    setMoved((m) => ({ ...m, [key]: now }))
    setSaid({
      ok: true,
      words: `${c.consumer} ${now ? 'may now read' : 'may no longer read'} ${secret.name}. This takes effect at the next attempt; an attempt already running keeps what it was given.`,
    })
    onChange()
  }

  return (
    <>
      <p className="pk-lead">
        Tick a box to let that runner profile, tool or platform component read the secret. A box that cannot be
        ticked says why: the consumer reads another kind, or reads no secret yet.
      </p>
      {!data.can_manage && <p className="ur-small">You can read the grants here; only someone who manages this workspace&rsquo;s secrets can change them.</p>}
      <div className="table-wrap" role="region" aria-label="Grant matrix" tabIndex={0}>
        <table className="pools" aria-label="Secret grants">
          <thead>
            <tr>
              <th scope="col" rowSpan={2}>Secret</th>
              {groups.map((g) => <th key={g.label} scope="colgroup" colSpan={g.cols.length}>{g.label}</th>)}
            </tr>
            <tr>
              {cols.map((c) => <th key={c.consumer} scope="col" title={c.consumer}><code>{c.name}</code></th>)}
            </tr>
          </thead>
          <tbody>
            {data.secrets.map((s) => (
              <tr key={s.name}>
                <th scope="row"><code>{s.name}</code></th>
                {cols.map((c) => {
                  const cell = cellOf(s, c)
                  if (cell === null) return <td key={c.consumer} />
                  const key = cellKey(s.name, c.consumer)
                  const served = s.grants.some((g) => g.consumer === c.consumer && g.enabled)
                  const checked = cell.locked || (moved[key] ?? served)
                  const why = cell.reason === null ? undefined : `${id}-${s.name}-${c.consumer}`
                  return (
                    <td key={c.consumer} title={cell.title}>
                      <input
                        type="checkbox"
                        aria-label={`Grant ${s.name} to ${c.consumer}`}
                        aria-describedby={why}
                        checked={checked}
                        disabled={!data.can_manage || !cell.open || busy[key] === true}
                        onChange={(e) => void tick(s, c, cell, e.target.checked)}
                      />
                      {cell.reason !== null && <span id={why} className="jb-note"> {cell.reason}</span>}
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
 * ROTATE (§5, Q5): a new value, in a password field, posted once to
 * `PUT .../{name}/value`. The previous version stays enabled for 24 hours,
 * then the rotation sweep disables it. The fields are uncontrolled and
 * emptied before the request leaves, as Add's are; what the page says after
 * is the version, never anything of the value.
 */
function RotateSecret({
  secret,
  repoId = null,
  oneTime = false,
  takeToken,
  onCancel,
  onDone,
}: {
  secret: WorkspaceSecret
  repoId?: string | null
  /** Opened through a one-time link the page minted: the form says so. */
  oneTime?: boolean
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
    const r = await rotateWorkspaceSecret(secret.name, body, takeToken(), repoId)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error.code === 'SECRET_LINK_INVALID'
        ? `${secret.name} was not rotated: the one-time link is used, expired or not yours. Rotate it again here without it, or ask /sc for a new link.`
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
          {oneTime && ' A one-time form: it works once, for ten minutes, and is never prefilled.'}
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
    return `${name} was not added: the one-time link is used, expired or not yours. Add it again here without it, or ask /sc for a new link.`
  }
  return `${name} was not added: ${e.message}`
}

function AddSecret({
  catalogue,
  takeToken,
  onAdded,
  repoId = null,
}: {
  catalogue: WorkspaceSecretCatalogue
  repoId?: string | null
  /** The one-time link's token, handed over once; null after, or without a link. */
  takeToken: () => string | null
  onAdded: () => void
}) {
  const id = useId()
  const providers = catalogue.providers.length > 0 ? catalogue.providers : [DEFAULT_PROVIDER]
  const kinds = catalogue.kinds.length > 0 ? catalogue.kinds : [DEFAULT_KIND]
  const firstProvider = providers.includes(DEFAULT_PROVIDER) ? DEFAULT_PROVIDER : providers[0]!
  const firstKind = kinds.includes(DEFAULT_KIND) ? DEFAULT_KIND : kinds[0]!
  const [name, setName] = useState('')
  const [provider, setProvider] = useState(firstProvider)
  const [kind, setKind] = useState(firstKind)
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
    const r = await createWorkspaceSecret(body, takeToken(), repoId)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(refusalWords(r.error, named))
      return
    }
    setName('')
    setProvider(firstProvider)
    setKind(firstKind)
    setAdded(named)
    onAdded()
  }

  return (
    <>
      <form className="pk-form" autoComplete="off" aria-label="Add a secret" onSubmit={(e) => void save(e)}>
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
        <div className="pk-form-row">
          <label htmlFor={`${id}-provider`}>Provider</label>
          <select id={`${id}-provider`} value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
          <label htmlFor={`${id}-kind`}>Kind</label>
          <select id={`${id}-kind`} value={kind} onChange={(e) => {
            setKind(e.target.value)
            // The value fields are drawn afresh for the new kind, empty.
            setFilled(false)
          }}>
            {kinds.map((k) => <option key={k} value={k}>{KIND_WORDS[k] ?? k}</option>)}
          </select>
        </div>
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
            Add
          </Button>
        </div>
      </form>
      {added !== null && (
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
export function SecretField({
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
