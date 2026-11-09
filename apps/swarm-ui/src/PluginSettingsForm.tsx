/**
 * ONE PLUGIN'S SETTINGS FOR THIS WORKSPACE (docs/design/managed-plugins.md
 * §6, lane 10c; part of #2688). The form is generated from the fields the
 * pinned version declares, its manifest's `userConfig` or the admin's overlay
 * (`pluginconfig.form_fields`): a string a text box, a number a number box, a
 * boolean a switch, a directory or file a path inside the checkout, a list
 * one item per line; `required` marked, the description behind an (i).
 *
 * A SECRET NEVER COMES BACK. A secret or file setting shows whether it is set,
 * by whom and when, or the workspace key it is bound to, and never a value.
 * Its value is entered only through a one-time link (`POST .../links`: ten
 * minutes, one use, this caller's): the link's token is held in memory, the
 * box is write-only, and the value is dropped from the page the moment it is
 * sent. A link opened from elsewhere arrives as `link` and opens its box.
 *
 * Saving non-secret values names the version the form was read at and the
 * settings' revision, so a stale form is refused by the API with its reason
 * and nothing is overwritten. Only the workspace's owner or admin edits
 * (`can_edit`); everyone else reads.
 */
import { useEffect, useState } from 'react'
import {
  bindPluginSecret,
  loadPluginConfig,
  mintPluginSecretLink,
  removePluginSecret,
  savePluginValues,
  storePluginSecret,
  type PluginConfig,
  type PluginField,
  type PluginFieldState,
  type PluginSecretLink,
} from './api'
import { Banner, Button } from './components'
import type { ApiError, Result } from './fetch'
import { InfoTip } from './People'
import { UrRegion } from './RepositoriesParts'
import { formatDateTime } from './timezone'
import './styles/plugins.css'

/** The workspace keys a secret may be bound to, by the API's provider name. */
export const BINDING_WORDS: Readonly<Record<string, string>> = { typesafe: 'TypeSafe key' }

/** A path setting's own check, as the API makes it: relative, inside the checkout, no `..`. */
export function pathOk(value: string): boolean {
  if (value === '' || value.length > 1024 || value.includes('\\') || value.includes('\0')) return false
  if (value.startsWith('/') || value.startsWith('~') || /^[A-Za-z]:/.test(value)) return false
  return !value.split('/').includes('..')
}

function label(f: PluginField): string {
  return f.title ?? f.key
}

/** A stored value as its control holds it: a list one item per line, a number or text as typed. */
function asDraft(f: PluginField, v: unknown): string | boolean {
  if (f.type === 'boolean') return v === true
  if (f.multiple) return Array.isArray(v) ? v.map(String).join('\n') : ''
  return v === undefined || v === null ? '' : String(v)
}

/** A draft as the API takes it, or `invalid`. An empty box clears the value (`null`). */
function fromDraft(f: PluginField, d: string | boolean): unknown | 'invalid' {
  if (f.type === 'boolean') return d === true
  const text = String(d)
  if (f.multiple) {
    const items = text.split('\n').map((s) => s.trim()).filter((s) => s !== '')
    if (f.kind === 'path' && !items.every(pathOk)) return 'invalid'
    return items.length === 0 ? null : items
  }
  if (text.trim() === '') return null
  if (f.type === 'number') {
    const n = Number(text)
    return Number.isFinite(n) ? n : 'invalid'
  }
  if (f.kind === 'path' && !pathOk(text.trim())) return 'invalid'
  return f.kind === 'path' ? text.trim() : text
}

function sameValue(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null)
}

export function PluginSettingsForm({ tenantId, installId, name, link = null }: {
  tenantId: string
  installId: string
  name: string
  /** A one-time link opened on this console: its field and token. */
  link?: { field: string; token: string } | null
}) {
  const [state, setState] = useState<Result<PluginConfig>>({ status: 'loading', since: Date.now() })
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let live = true
    void loadPluginConfig(tenantId, installId).then((r) => {
      if (live) setState(r)
    })
    return () => {
      live = false
    }
  }, [tenantId, installId, nonce])
  return (
    <UrRegion state={state} route="GET /v1/workspaces/{tenant_id}/plugin-config/{install_id}" what={`${name}'s settings`} plural onRetry={() => setNonce((n) => n + 1)}>
      {(config) => <SettingsBody config={config} tenantId={tenantId} name={name} link={link} onChanged={() => setNonce((n) => n + 1)} />}
    </UrRegion>
  )
}

/** The one-time box open for a field: the link it came with, the value typed (never drawn back). */
interface OpenLink {
  field: string
  link: PluginSecretLink
}

function SettingsBody({ config: served, tenantId, name, link, onChanged }: {
  config: PluginConfig
  tenantId: string
  name: string
  link: { field: string; token: string } | null
  onChanged: () => void
}) {
  const [config, setConfig] = useState(served)
  useEffect(() => setConfig(served), [served])
  const [drafts, setDrafts] = useState<Record<string, string | boolean>>({})
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState(false)
  const [open, setOpen] = useState<OpenLink | null>(
    link === null ? null : { field: link.field, link: { field: link.field, url: null, path: '', expires_at: null, token: link.token } },
  )

  const valueFields = config.fields.filter((f) => f.kind === 'value' || f.kind === 'path')
  const changed = valueFields.filter((f) => f.key in drafts && !sameValue(fromDraft(f, drafts[f.key]!), config.values[f.key]?.value))
  const invalid = changed.filter((f) => fromDraft(f, drafts[f.key]!) === 'invalid')

  async function save() {
    if (busy || !config.can_edit || changed.length === 0 || invalid.length > 0) return
    setBusy(true)
    setFailed(null)
    setSaved(false)
    const values = Object.fromEntries(changed.map((f) => [f.key, fromDraft(f, drafts[f.key]!)]))
    const r = await savePluginValues(tenantId, config.install_id, { version_id: config.version_id, revision: config.revision, values })
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setConfig(r.data)
      setDrafts({})
      setSaved(true)
    }
  }

  if (config.fields.length === 0) {
    return <p className="plg-dim plg-none">No settings</p>
  }

  return (
    <div className="plg-settings">
      {saved && <Banner tone="info" title="Saved">From the next submission.</Banner>}
      <header className="plg-panel-h">
        <h2>
          Settings <em>{config.fields.length}</em>
        </h2>
        <InfoTip
          about="settings"
          tip={`Built from ${name}'s ${config.fields.some((f) => f.source === 'overlay') ? 'declared settings' : 'userConfig'}. Set once for this workspace; a step may override a non-secret one. Secrets are set through a one-time link and never shown.`}
        />
        {config.set_at !== null && (
          <span className="plg-dim plg-small plg-push">
            {config.set_by ?? 'who: not known'} · {formatDateTime(config.set_at)}
          </span>
        )}
        {!config.can_edit && <span className="plg-tick plg-push">View only</span>}
      </header>
      <ul className="plg-form" role="list" aria-label={`${name}'s settings`}>
        {config.fields.map((f) => (
          <FieldRow
            key={f.key}
            field={f}
            state={config.values[f.key] ?? { set: false }}
            missing={config.missing.includes(f.key)}
            canEdit={config.can_edit}
            draft={drafts[f.key]}
            onDraft={(d) => {
              setSaved(false)
              setDrafts((was) => ({ ...was, [f.key]: d }))
            }}
            tenantId={tenantId}
            installId={config.install_id}
            open={open?.field === f.key ? open : null}
            onOpen={setOpen}
            onChanged={() => {
              setOpen(null)
              onChanged()
            }}
          />
        ))}
      </ul>
      {config.can_edit && (changed.length > 0 || failed !== null) && (
        <div className="plg-savebar" role="region" aria-label="Settings not saved">
          <div className="plg-savebar-t">
            <b>{changed.map(label).join(', ')}</b>
            {invalid.length > 0 && <span className="plg-failed">{invalid.map(label).join(', ')}: not of the setting's type</span>}
            {failed !== null && <span className="plg-failed" role="alert">Not saved: {failed.message}</span>}
          </div>
          <span className="plg-savebar-b">
            <Button kind="ghost" disabled={busy} onClick={() => { setDrafts({}); setFailed(null) }}>Discard</Button>
            <Button kind="primary" busy={busy} disabled={changed.length === 0 || invalid.length > 0} onClick={() => void save()}>Save</Button>
          </span>
        </div>
      )}
    </div>
  )
}

function FieldRow({ field: f, state, missing, canEdit, draft, onDraft, tenantId, installId, open, onOpen, onChanged }: {
  field: PluginField
  state: PluginFieldState
  missing: boolean
  canEdit: boolean
  draft: string | boolean | undefined
  onDraft: (d: string | boolean) => void
  tenantId: string
  installId: string
  open: OpenLink | null
  onOpen: (o: OpenLink | null) => void
  onChanged: () => void
}) {
  const written = f.kind === 'secret' || f.kind === 'file'
  return (
    <li className={`plg-frow${missing ? ' is-missing' : ''}`}>
      <div className="plg-frow-l">
        <span className="plg-ftitle">
          {label(f)}
          {f.description !== null && <InfoTip about={label(f)} tip={f.description} />}
        </span>
        <code className="plg-small plg-dim">{f.variable ?? f.key}</code>
      </div>
      <div className="plg-frow-v">
        {written
          ? <WrittenField field={f} state={state} canEdit={canEdit} tenantId={tenantId} installId={installId} open={open} onOpen={onOpen} onChanged={onChanged} />
          : <ValueControl field={f} stored={state.value} draft={draft} canEdit={canEdit} onDraft={onDraft} />}
      </div>
      <div className="plg-frow-r">
        {f.required && <span className={`plg-mark${missing ? ' is-missing' : ''}`}>{missing ? 'Missing' : 'Required'}</span>}
        {written && <span className="plg-lock">{f.kind === 'file' ? 'File' : 'Secret'}</span>}
      </div>
    </li>
  )
}

function ValueControl({ field: f, stored, draft, canEdit, onDraft }: {
  field: PluginField
  stored: unknown
  draft: string | boolean | undefined
  canEdit: boolean
  onDraft: (d: string | boolean) => void
}) {
  const shown = draft ?? asDraft(f, stored === undefined ? (f.type === 'boolean' ? f.default : undefined) : stored)
  const changed = draft !== undefined && !sameValue(fromDraft(f, draft), stored)
  const bad = draft !== undefined && fromDraft(f, draft) === 'invalid'
  const cls = `c-inp${f.kind === 'path' || f.type === 'number' ? ' is-mono' : ''}${changed ? ' is-changed' : ''}${bad ? ' is-bad' : ''}`
  const placeholder = f.default === null || f.default === undefined || f.type === 'boolean' ? undefined : String(f.default)
  if (f.type === 'boolean') {
    return (
      <label className={`plg-switch${changed ? ' is-changed' : ''}${canEdit ? '' : ' is-inert'}`}>
        <input type="checkbox" role="switch" checked={shown === true} disabled={!canEdit} aria-label={label(f)} onChange={(e) => onDraft(e.target.checked)} />
        <span className="plg-switch-track" aria-hidden><span /></span>
        <span className="plg-switch-word">{shown === true ? 'On' : 'Off'}</span>
      </label>
    )
  }
  if (f.multiple) {
    return (
      <textarea className={`${cls} plg-list`} rows={3} value={String(shown)} disabled={!canEdit} aria-label={label(f)} aria-invalid={bad || undefined}
        placeholder={placeholder} spellCheck={false} onChange={(e) => onDraft(e.target.value)} />
    )
  }
  return (
    <>
      <input className={f.type === 'number' ? `${cls} plg-num` : cls} type={f.type === 'number' ? 'number' : 'text'} value={String(shown)} disabled={!canEdit}
        aria-label={label(f)} aria-invalid={bad || undefined} placeholder={placeholder} spellCheck={false} onChange={(e) => onDraft(e.target.value)} />
      {bad && f.kind === 'path' && (
        <span className="plg-err">
          Not a path in the checkout
          <InfoTip about="paths" tip="A path setting is relative to the checkout, with no '..', and does not start with / or ~." />
        </span>
      )}
    </>
  )
}

/** A secret or a file setting: its status, never its value, and the actions an owner or admin has. */
function WrittenField({ field: f, state, canEdit, tenantId, installId, open, onOpen, onChanged }: {
  field: PluginField
  state: PluginFieldState
  canEdit: boolean
  tenantId: string
  installId: string
  open: OpenLink | null
  onOpen: (o: OpenLink | null) => void
  onChanged: () => void
}) {
  const [busy, setBusy] = useState<'link' | 'remove' | 'bind' | null>(null)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const bindable = f.kind === 'secret' ? f.bindable.filter((p) => p in BINDING_WORDS) : []

  async function act(kind: 'link' | 'remove' | 'bind', run: () => Promise<Result<unknown>>, then: (data: unknown) => void) {
    if (busy !== null) return
    setBusy(kind)
    setFailed(null)
    const r = await run()
    setBusy(null)
    if (r.status === 'error') setFailed(r.error)
    else if (r.status === 'ok' || r.status === 'stale') then(r.data)
  }

  return (
    <div className="plg-secret">
      <span className="plg-status">
        <SecretStatus field={f} state={state} />
      </span>
      {canEdit && open === null && (
        <span className="plg-acts">
          <Button size="sm" kind="ghost" busy={busy === 'link'}
            onClick={() => void act('link', () => mintPluginSecretLink(tenantId, installId, f.key), (d) => onOpen({ field: f.key, link: d as PluginSecretLink }))}>
            {state.set && state.binding === undefined ? 'Replace' : 'Set'}
          </Button>
          {bindable.filter((p) => state.binding?.provider !== p).map((p) => (
            <Button key={p} size="sm" kind="ghost" busy={busy === 'bind'}
              onClick={() => void act('bind', () => bindPluginSecret(tenantId, installId, f.key, p), onChanged)}>
              Use the {BINDING_WORDS[p]}
            </Button>
          ))}
          {state.set && (
            <Button size="sm" kind="ghost" busy={busy === 'remove'}
              onClick={() => void act('remove', () => removePluginSecret(tenantId, installId, f.key), onChanged)}>
              Remove
            </Button>
          )}
        </span>
      )}
      {failed !== null && <span className="plg-failed" role="alert">Refused: {failed.message}</span>}
      {canEdit && open !== null && (
        <OneTimeBox field={f} open={open} tenantId={tenantId} installId={installId} onClose={() => onOpen(null)} onStored={onChanged} />
      )}
    </div>
  )
}

export function SecretStatus({ field: f, state }: { field: PluginField; state: PluginFieldState }) {
  if (state.binding !== undefined) {
    return (
      <span className="plg-tick is-ok">
        <Tick />
        {BINDING_WORDS[state.binding.provider] ?? state.binding.provider}
        <InfoTip about="the workspace key" tip="The step gets this workspace's key, through its adapter; the plugin never holds a copy here." />
      </span>
    )
  }
  if (!state.set) return <span className="plg-dim">Not set</span>
  const who = state.set_by ?? 'who: not known'
  const when = state.set_at === null || state.set_at === undefined ? 'when: not known' : formatDateTime(state.set_at)
  return (
    <span className="plg-tick is-ok">
      <Tick />
      Set by {who}, {when}
      {f.kind === 'file' && typeof state.bytes === 'number' && <span className="plg-dim"> · {state.bytes} B</span>}
    </span>
  )
}

function Tick() {
  return <svg viewBox="0 0 16 16" aria-hidden><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>
}

/**
 * The write-only box a one-time link opens: the link, to hand to whoever
 * holds the value, and the box itself. The value lives in this component
 * only until it is sent, and is cleared whether the API takes it or not.
 */
function OneTimeBox({ field: f, open, tenantId, installId, onClose, onStored }: {
  field: PluginField
  open: OpenLink
  tenantId: string
  installId: string
  onClose: () => void
  onStored: () => void
}) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [copied, setCopied] = useState(false)
  const token = open.link.token
  const address = open.link.url ?? (open.link.path === '' ? null : open.link.path)

  async function store() {
    if (busy || value === '' || token === null) return
    setBusy(true)
    setFailed(null)
    const sent = value
    setValue('')
    const r = await storePluginSecret(tenantId, installId, f.key, sent, token)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    onStored()
  }

  return (
    <div className="plg-onetime" role="group" aria-label={`${label(f)}: one-time link`}>
      {address !== null && (
        <span className="plg-link">
          <code title={address}>{address.replace(/([?&]t=)[^&]+/, '$1…')}</code>
          <Button size="sm" kind="ghost" onClick={() => {
            void navigator.clipboard?.writeText(address).then(() => setCopied(true), () => setCopied(false))
          }}>
            {copied ? 'Copied' : 'Copy link'}
          </Button>
          {open.link.expires_at !== null && <span className="plg-dim plg-small">until {formatDateTime(open.link.expires_at)}</span>}
          <InfoTip about="the one-time link" tip="One use, ten minutes, for you only. Open it where the value is, or type the value here." end />
        </span>
      )}
      {token === null ? (
        <span className="plg-failed" role="alert">This link carries no token.</span>
      ) : (
        <span className="plg-onetime-in">
          {f.kind === 'file' ? (
            <textarea className="c-inp is-mono plg-code" rows={6} value={value} spellCheck={false} autoComplete="off"
              aria-label={`${label(f)}: content`} onChange={(e) => setValue(e.target.value)} />
          ) : (
            <input className="c-inp is-mono" type="password" value={value} autoComplete="new-password" spellCheck={false}
              aria-label={`${label(f)}: value`} onChange={(e) => setValue(e.target.value)} />
          )}
          <Button kind="primary" size="sm" busy={busy} disabled={value === ''} onClick={() => void store()}>Store</Button>
          <Button kind="ghost" size="sm" disabled={busy} onClick={() => { setValue(''); onClose() }}>Cancel</Button>
        </span>
      )}
      {failed !== null && <span className="plg-failed" role="alert">Not stored: {failed.message}</span>}
    </div>
  )
}
