import { useId, useLayoutEffect, useRef, useState, type FormEvent, type MouseEvent, type ReactNode, type RefObject } from 'react'
import {
  createWorkspaceSecret,
  deleteWorkspaceSecret,
  loadWorkspaceSecretCatalogue,
  loadWorkspaceSecrets,
  type WorkspaceSecret,
  type WorkspaceSecretCatalogue,
  type WorkspaceSecretCreate,
  type WorkspaceSecretList,
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
 * secrets store: the list, Add and a typed Delete. The grant matrix and
 * Rotate are SEC-UI-GRANTS, the lane after this one; the list names each
 * secret's enabled consumers read-only until then.
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
 * WHO MAY MANAGE is the API's `can_manage` (`workspace_secrets.require_manager`:
 * a personal workspace's person; a group workspace's admin or a platform
 * admin). Only an explicit true offers Add or Delete; the API is still the gate.
 */

const LIST_ROUTE = 'GET /v1/workspace/secrets'
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
const KIND_WORDS: Readonly<Record<string, string>> = Object.fromEntries([
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
const STRUCTURED: Readonly<Record<string, readonly [StructuredField, StructuredField]>> = {
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

export function WorkspaceSecretsScreen({ link }: { link: SecretsLink | null }) {
  const list = useUrRead(loadWorkspaceSecrets, 'workspace-secrets')
  const catalogue = useUrRead(loadWorkspaceSecretCatalogue, 'workspace-secret-catalogue')
  // The link's token, read once; null once it has been sent. A ref, never
  // state or a prop of anything drawn: nothing renders it.
  const token = useRef<string | null | undefined>(undefined)
  if (token.current === undefined) {
    token.current = link === null || link.action !== 'add'
      ? null
      : link.token !== undefined ? link.token : secretsLinkToken(window.location.pathname, window.location.search)
  }
  useLayoutEffect(() => {
    if (link !== null) dropLinkToken()
  }, [link])
  const takeToken = () => {
    const t = token.current ?? null
    token.current = null
    return t
  }

  return (
    <div className="pk-page">
      <PageHead title="Workspace secrets">
        <UrRefresh reads={[list, catalogue]} ageOf={[list]} />
      </PageHead>
      {link !== null && link.action === 'add' && (
        <Banner tone="info" title="Opened from a one-time link">
          Paste the value into the form below: the /sc command and this page&rsquo;s address never see it. The link
          works once, for ten minutes.
        </Banner>
      )}
      {link !== null && link.action === 'rotate' && (
        <Banner tone="info" title={`Opened from a one-time rotate link${link.name === null ? '' : ` for ${link.name}`}`}>
          This page lists, adds and deletes; Rotate is not on it yet, so the link was not used and nothing was
          changed. Ask /sc for a new link once Rotate is here.
        </Banner>
      )}
      <Card title="Secrets">
        <p className="pk-lead">
          The credentials this workspace stores for its agents, tools and platform components, and who each one is
          granted to. A value is stored write-only: no page and no API route shows it, its length or any of its
          characters again. Back to <SettingsLink address="settings/workspace">Settings › Workspace</SettingsLink>.
        </p>
        <UrRegion state={list.state} route={LIST_ROUTE} what="The workspace's secrets" plural onRetry={list.reload}>
          {(d) => <SecretList data={d} onChange={list.reload} />}
        </UrRegion>
      </Card>
      {(list.state.status === 'ok' || list.state.status === 'stale') && list.state.data.can_manage && (
        <Card title="Add a secret">
          <UrRegion state={catalogue.state} route={CATALOGUE_ROUTE} what="The kinds and providers a secret is stored as" plural onRetry={catalogue.reload}>
            {(c) => <AddSecret catalogue={c} takeToken={takeToken} onAdded={list.reload} />}
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

function SecretList({ data, onChange }: { data: WorkspaceSecretList; onChange: () => void }) {
  const [deleting, setDeleting] = useState<string | null>(null)
  const [deleted, setDeleted] = useState<string | null>(null)
  const target = deleting === null ? undefined : data.secrets.find((s) => s.name === deleting)
  return (
    <>
      {!data.can_manage && (
        <p className="ur-small">
          Only this workspace&rsquo;s admin or a platform admin can add or delete its secrets (in a personal workspace,
          its person). You can read them here.
        </p>
      )}
      {deleted !== null && (
        <p className="pk-saved" role="status">
          {deleted} was deleted. Running attempts keep the copy they were given.
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
                  <th scope="row" data-label="Name"><code>{s.name}</code></th>
                  <td data-label="Provider">{s.provider || 'not said'}</td>
                  <td data-label="Kind">{KIND_WORDS[s.kind] ?? (s.kind || 'not said')}</td>
                  <td data-label="Set" title={s.set_at ?? undefined}>{setWords(s)}</td>
                  <td data-label="Version">{s.current_version ?? 'not said'}</td>
                  <LastUsed at={s.last_used_at} />
                  <td data-label="Granted to">
                    {on.length === 0 ? 'nothing yet' : on.map((c, i) => <span key={c}>{i > 0 && ', '}<code>{c}</code></span>)}
                  </td>
                  {data.can_manage && (
                    <td data-label="Actions">
                      <Button kind="danger" size="sm" aria-label={`Delete ${s.name}`} onClick={() => {
                        setDeleted(null)
                        setDeleting(s.name)
                      }}>
                        Delete
                      </Button>
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
function DeleteSecret({ secret, onKeep, onDone }: { secret: WorkspaceSecret; onKeep: () => void; onDone: () => void }) {
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const on = secret.grants.filter((g) => g.enabled).map((g) => g.consumer)

  async function confirm() {
    if (busy) return
    setBusy(true)
    setFailed(null)
    const r = await deleteWorkspaceSecret(secret.name)
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
}: {
  catalogue: WorkspaceSecretCatalogue
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
    const r = await createWorkspaceSecret(body, takeToken())
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
