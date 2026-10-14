import { useEffect, useId, useRef, useState, type FormEvent } from 'react'
import { deleteProviderKey, loadMe, loadProviderKeys, putProviderKey, type ProviderKeyScope, type ProviderKeyStatus, type ProviderKeys } from './api'
import { Banner, Button, Card } from './components'
import type { ApiError } from './fetch'
import { keyLinkToken } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import './styles/keys.css'

/**
 * SETTINGS › API KEYS (lane KEYS-UI, owner request 2026-10-14;
 * docs/provider-keys.md). A person sets, replaces or removes their own
 * TypeSafe key here, and an admin the selected tenant's. Owner decisions: the
 * person's key wins over the tenant's, and THE KEY NEVER PASSES THROUGH
 * CLAUDE -- `/sc` mints a one-time link to this page instead of taking it.
 *
 * THE KEY IS NEVER HELD BY THIS PAGE. The field is uncontrolled: its value
 * lives in the DOM input and nowhere else -- not in React state (a controlled
 * input writes its value back as an attribute), not in localStorage, not in
 * the address. Submit reads it once, empties the field, and hands it to
 * `putProviderKey`, which puts it in that one request's body. Nothing here
 * can draw it back: the API returns status only, `{set, set_at, set_by,
 * version}`, copied field by field in `normProviderKeys`.
 *
 * THE ONE-TIME LINK (`/settings/keys/typesafe?scope=..&t=..`). The token is
 * read off the bar once (App captures it before its normalise rewrites the
 * address; this page reads it itself when rendered alone), dropped from the
 * bar with `history.replaceState`, and sent back as `link_token` with the
 * first save for the scope it was minted for. It authorises nothing -- the
 * session does -- and the API consumes it before the write, so it is sent at
 * most once: a retry after a failed write goes without it.
 *
 * A 503 IS THE DEPLOYED STATE until the owner applies the KEYS-IAM bootstrap
 * role (docs/provider-keys.md §6), so it is said as what it is: keys cannot be
 * saved yet, with the API's own reason.
 */

const PROVIDER = 'typesafe'
const STATUS_ROUTE = 'GET /v1/provider-keys/typesafe'

/** What a task would use, in the words the owner asked for. */
const USES: Readonly<Record<ProviderKeys['resolution']['uses'], string>> = {
  user: 'your key',
  tenant: 'the tenant key',
  none: 'none',
}

/** The link as the route carries it; `token` is App's capture, absent when rendered alone. */
export interface KeyLink {
  scope: ProviderKeyScope | null
  token?: string | null
}

/** Drop `t` from the address bar, keeping everything else. Idempotent. */
function stripLinkToken(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('t')) return
  url.searchParams.delete('t')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

interface Who {
  admin: boolean
  /** The selected tenant is the caller's own workspace: there is no separate tenant key. */
  personal: boolean
}

/** Admin and the selected tenant's kind, from the frame's read. Unknown is not an admin: the API is still the gate. */
function useWho(): Who | null {
  const [who, setWho] = useState<Who | null>(null)
  useEffect(() => {
    let live = true
    void loadMe({ frame: true }).then(
      (r) => {
        if (!live) return
        const ok = r.status === 'ok' || r.status === 'stale'
        setWho({
          admin: ok && r.data.principal?.is_admin === true,
          personal: ok && r.data.tenant?.kind === 'user',
        })
      },
      () => {
        if (live) setWho({ admin: false, personal: false })
      },
    )
    return () => {
      live = false
    }
  }, [])
  return who
}

export function ProviderKeysScreen({ link }: { link: KeyLink | null }) {
  const keys = useUrRead(() => loadProviderKeys(PROVIDER), PROVIDER)
  const who = useWho()
  // The link's token, read once; null once it has been sent.
  const [token, setToken] = useState<string | null>(() => {
    if (link === null || link.scope === null) return null
    if (link.token !== undefined) return link.token
    return keyLinkToken(window.location.pathname, window.location.search)
  })
  useEffect(() => {
    if (link !== null) stripLinkToken()
  }, [link])

  const linkScope = link?.scope ?? null
  const tokenFor = (scope: ProviderKeyScope) => (scope === linkScope ? token : null)

  return (
    <div className="pk-page">
      <PageHead title="API keys">
        <UrRefresh reads={[keys]} />
      </PageHead>
      {link !== null && (
        <Banner tone="info" title="Opened from a one-time link">
          Paste the key below. It goes from this page to the platform&rsquo;s secret store and nowhere else: Claude, the
          /sc command and this page&rsquo;s address never see it. The link works once, for ten minutes.
          {linkScope === 'tenant' && who !== null && !who.admin ? ' It is for the tenant key, which only an admin can set.' : ''}
        </Banner>
      )}
      <Card title="TypeSafe">
        <p className="pk-lead">
          TypeSafe serves the Jev judgment models. A key saved here is write-only: no page, route or log can show it
          again, only whether one is set, when and by whom. Your own key wins over the tenant&rsquo;s.
        </p>
        <UrRegion state={keys.state} route={STATUS_ROUTE} what="TypeSafe key status" onRetry={keys.reload}>
          {(d) =>
            who === null ? (
              <p className="ur-small" role="status">
                Reading who you are…
              </p>
            ) : (
              <>
                <p className="pk-uses">{`Your tasks in ${d.resolution.tenant_id} would use: ${USES[d.resolution.uses]}`}</p>
                {d.consumed_by.length === 0 && (
                  <p className="ur-small">Nothing reads TypeSafe keys yet: this is the key a task would use once something does.</p>
                )}
                <KeyRow
                  scope="user"
                  status={d.user}
                  tenantId={d.tenant.tenant_id}
                  token={tokenFor('user')}
                  focus={linkScope === 'user'}
                  done={linkScope === 'user'}
                  onSent={() => {
                    if (linkScope === 'user') setToken(null)
                  }}
                  onChange={keys.reload}
                />
                {who.admin ? (
                  <KeyRow
                    scope="tenant"
                    status={d.tenant}
                    tenantId={d.tenant.tenant_id}
                    personal={who.personal}
                    token={tokenFor('tenant')}
                    focus={linkScope === 'tenant'}
                    done={linkScope === 'tenant'}
                    onSent={() => {
                      if (linkScope === 'tenant') setToken(null)
                    }}
                    onChange={keys.reload}
                  />
                ) : (
                  <p className="ur-small">
                    {`The tenant key for ${d.tenant.tenant_id} is ${d.tenant.set ? 'set' : 'not set'}; only an admin can set or remove it.`}
                  </p>
                )}
              </>
            )
          }
        </UrRegion>
      </Card>
    </div>
  )
}

/** "set 2h ago by alice@example.com · version 3", or "not set". */
function statusWords(s: ProviderKeyStatus): string {
  if (!s.set) return 'Not set'
  const parts = ['Set']
  if (s.set_at !== null) parts.push(timeAgo(s.set_at))
  if (s.set_by !== null) parts.push(`by ${s.set_by}`)
  const head = parts.join(' ')
  return s.version === null ? head : `${head} · version ${s.version}`
}

/** A write's refusal, in words. A 503 is the not-yet-granted deployment (docs/provider-keys.md §6). */
function failureWords(e: ApiError, what: string, verb: 'saved' | 'removed'): string {
  if (e.httpStatus === 503) {
    return verb === 'saved'
      ? `Keys can't be saved yet: ${e.message}`
      : `${what} was not removed: ${e.message}. Removing again destroys whatever is left.`
  }
  if (e.code === 'KEY_LINK_INVALID') {
    return `${what} was not saved: the one-time link is used, expired or not yours. Save again here without it, or ask /sc for a new link.`
  }
  return `${what} was not ${verb}: ${e.message}`
}

function KeyRow({
  scope,
  status,
  tenantId,
  personal = false,
  token,
  focus,
  done,
  onSent,
  onChange,
}: {
  scope: ProviderKeyScope
  status: ProviderKeyStatus
  tenantId: string
  personal?: boolean
  /** The one-time link's token, when the link was minted for this scope. */
  token: string | null
  focus: boolean
  /** Opened from a link for this scope: a save ends the visit. */
  done: boolean
  onSent: () => void
  onChange: () => void
}) {
  const id = useId()
  const input = useRef<HTMLInputElement>(null)
  // Whether the field holds anything. A boolean, never the value.
  const [filled, setFilled] = useState(false)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)
  const label = scope === 'user' ? 'Your key' : `Tenant key · ${tenantId}`
  const what = scope === 'user' ? 'Your key' : 'The tenant key'
  const writable = scope === 'user' ? status.available : !personal

  useEffect(() => {
    if (focus && writable) input.current?.focus()
  }, [focus, writable])

  async function save(e: FormEvent) {
    e.preventDefault()
    const field = input.current
    if (field === null || busy) return
    const value = field.value
    // Emptied before the request leaves, whatever it answers: the field is
    // never a place the key waits.
    field.value = ''
    setFilled(false)
    if (value === '') return
    setBusy(true)
    setSaved(false)
    setFailed(null)
    // The link is consumed before the write, so it is sent once.
    if (token !== null) onSent()
    const r = await putProviderKey('typesafe', scope, value, token)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(failureWords(r.error, what, 'saved'))
      return
    }
    setSaved(true)
    onChange()
  }

  return (
    <section className="pk-row" aria-labelledby={`${id}-h`}>
      <div className="pk-row-head">
        <h3 id={`${id}-h`}>{label}</h3>
        <span className={status.set ? 'pk-state is-set' : 'pk-state'} title={status.set_at ?? undefined}>
          {statusWords(status)}
        </span>
      </div>
      {!writable ? (
        <p className="ur-small">
          {scope === 'user'
            ? 'You have no ready personal workspace, which is where your own key is kept. Request one, or ask an admin to set a tenant key.'
            : `${tenantId} is your personal workspace, so its key is Your key above. Select a group tenant to set a tenant key.`}
        </p>
      ) : (
        <form className="pk-form" autoComplete="off" aria-label={`${status.set ? 'Replace' : 'Save'} ${what.toLowerCase()}`} onSubmit={(e) => void save(e)}>
          <label htmlFor={`${id}-k`}>{scope === 'user' ? 'TypeSafe key' : `TypeSafe key for ${tenantId}`}</label>
          <div className="pk-form-row">
            <input
              id={`${id}-k`}
              ref={input}
              className="ur-search pk-input"
              type="password"
              autoComplete="off"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              placeholder={status.set ? 'Paste a new key to replace it' : 'Paste the key'}
              onChange={(e) => {
                setFilled(e.target.value !== '')
                if (saved) setSaved(false)
              }}
            />
            <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={!filled}>
              {status.set ? 'Replace' : 'Save'}
            </Button>
            {status.set && (
              <Button kind="danger" size="sm" disabled={asking || busy} onClick={() => setAsking(true)}>
                Remove
              </Button>
            )}
          </div>
        </form>
      )}
      {saved && (
        <p className="pk-saved" role="status">
          {done ? 'Saved — you can close this tab.' : 'Saved.'}
        </p>
      )}
      {failed !== null && (
        <Banner tone="bad" title={failed} role="alert" />
      )}
      {asking && status.set && (
        <TypedRemove
          scope={scope}
          what={what}
          word={scope === 'user' ? 'typesafe' : tenantId}
          onKeep={() => setAsking(false)}
          onDone={() => {
            setAsking(false)
            setSaved(false)
            onChange()
          }}
        />
      )}
    </section>
  )
}

/**
 * REMOVE IS TYPED (CLAUDE.md: a destructive action takes a typed
 * confirmation). The API destroys every version, not disables it, so the
 * only way back is pasting the key again. Your own key is confirmed by typing
 * `typesafe`; the tenant key by typing the tenant's id, so an admin says which
 * tenant they mean. Esc is Keep, never the action, as on Access.
 */
function TypedRemove({
  scope,
  what,
  word,
  onKeep,
  onDone,
}: {
  scope: ProviderKeyScope
  what: string
  word: string
  onKeep: () => void
  onDone: () => void
}) {
  const id = useId()
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => {
    field.current?.focus()
  }, [])
  const matches = typed === word
  const title = scope === 'user' ? 'Remove your TypeSafe key?' : `Remove the TypeSafe key for ${word}?`

  async function remove() {
    if (!matches || busy) return
    setBusy(true)
    setFailed(null)
    const r = await deleteProviderKey('typesafe', scope)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(failureWords(r.error, what, 'removed'))
      return
    }
    onDone()
  }

  return (
    <div
      className="pk-ask"
      role="group"
      aria-label={scope === 'user' ? 'Remove your TypeSafe key' : `Remove the TypeSafe key for ${word}`}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !busy) {
          e.stopPropagation()
          onKeep()
        }
      }}
    >
      <Banner
        tone="warn"
        title={title}
        actions={
          <>
            <Button kind="danger-filled" size="sm" busy={busy ? 'Removing…' : false} disabled={!matches} onClick={() => void remove()}>
              Remove
            </Button>
            <Button size="sm" onClick={onKeep} disabled={busy}>
              Keep
            </Button>
          </>
        }
      >
        Every saved version is destroyed. {scope === 'user' ? 'Your tasks fall back to the tenant key, if one is set.' : 'Tasks here then use each person’s own key, or none.'}
      </Banner>
      <label htmlFor={`${id}-c`}>{`Type ${word} to remove`}</label>
      <input
        id={`${id}-c`}
        ref={field}
        className="ur-search"
        value={typed}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setTyped(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            void remove()
          }
        }}
      />
      {failed !== null && <Banner tone="bad" title={failed} role="alert" />}
    </div>
  )
}
