/**
 * VOICE IT › SETTINGS (docs/design/voice-it-own-speech.md §11; the owner's
 * pick of 5 November 2026, "cards and a timeline"): four cards over swarm-api's
 * settings routes (routes/voice_settings.py).
 *
 *   Engine     own speech or GPT-Live, the fast model that answers, the
 *              planner, debug mode, and the OpenAI key's status;
 *   Machine    the speech machine's size and zone;
 *   Models     speech to text, text to speech, the voice;
 *   Behaviour  the idle stop.
 *
 * NAMES ONLY, FROM THE CATALOGUE. Every choice is a name `GET
 * /v1/voice/settings/catalogue` lists; the page holds no list of its own, so a
 * model the catalogue retires disappears here the same day.
 *
 * PENDING VERSUS APPLIED. A machine field (size, models, idle stop, zone)
 * takes effect at the speech machine's next start; swarm-api says which ones
 * differ from what the machine last started with (`pending`), and each carries
 * a tick: applied, or next start.
 *
 * ONE VERSION. A save sends the version the page read; when someone else
 * saved first swarm-api answers 409, the page reads again and says who saved,
 * and the person keeps theirs or takes the other.
 *
 * DEBUG MODE (the owner's pick of 10 November 2026, "Trace on Telemetry"). A
 * switch, saved the moment it is flipped (`PATCH {version, debug}`), not part
 * of the page's Save; a platform admin's, read only for anyone else. A save
 * that fails says so in the head and leaves the switch as it was. On, the row
 * opens the traces on Telemetry.
 *
 * THE KEY. Its status only: set or not, by whom and when. swarm-api never
 * serves the key, and the page keeps nothing else of the answer.
 *
 * THE DEDICATED KEY (docs/design/llm-providers.md §4.4, §6.4). Its status,
 * and for a platform admin (`dedicated_key.can_delete`) a Delete behind the
 * typed confirmation: the key's label typed back enables "Delete key", which
 * sends `DELETE {label}` once. The row moves only with the answer: a delete
 * that fails says so in the dialog and leaves the row as it was.
 *
 * Telemetry's timeline joins this section when its server lands (build plan
 * row L10); this page draws nothing for it.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'

import {
  deleteVoiceDedicatedKey,
  loadMe,
  loadVoiceCatalogue,
  loadVoiceSettings,
  saveVoiceSettings,
  type VoiceCatalogue,
  type VoiceDedicatedKey,
  type VoiceSettingField,
  type VoiceSettingsView,
} from '../api'
import type { ApiError } from '../fetch'
import { Absent } from '../primitives'
import { PageHead } from '../Shell'
import { clockTime } from '../types'
import '../styles/voice-settings.css'

type Draft = Partial<Record<VoiceSettingField, string>>
type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'idle'

/** Which card a field lives on, for a refusal that names it. */
const CARD_OF: Readonly<Record<VoiceSettingField, string>> = {
  engine: 'engine',
  conversation_model: 'engine',
  planner_model: 'engine',
  machine: 'machine',
  zone: 'machine',
  stt_model: 'models',
  tts_model: 'models',
  tts_voice: 'models',
  idle: 'behaviour',
}

const when = (iso: string | null): string => clockTime(iso)?.text ?? '—'

/** An explanation behind an (i): the surface carries none. */
function Why({ say }: { say: string }) {
  return (
    <button type="button" className="vset-why" title={say} aria-label={say}>
      i
    </button>
  )
}

function Tick({ tone, children, say }: { tone: Tone; children: ReactNode; say?: string }) {
  return (
    <span className={`vset-tick t-${tone}`} title={say}>
      <i aria-hidden="true" />
      {children}
    </span>
  )
}

function Card({ title, id, aside, band, children }: { title: string; id: string; aside?: ReactNode; band?: ReactNode; children: ReactNode }) {
  return (
    <section className="vset-card" aria-labelledby={`vset-${id}`} data-card={id}>
      <header className="vset-card-head">
        <h2 id={`vset-${id}`}>{title}</h2>
        {aside}
      </header>
      {band}
      <div className="vset-card-body">{children}</div>
    </section>
  )
}

function Band({ tone, children, actions }: { tone: 'warn' | 'bad'; children: ReactNode; actions?: ReactNode }) {
  return (
    <div className={`vset-band is-${tone}`} role="alert">
      <div className="vset-band-text">{children}</div>
      {actions && <div className="vset-band-acts">{actions}</div>}
    </div>
  )
}

function Row({ label, why, children }: { label: string; why?: string; children: ReactNode }) {
  return (
    <div className="vset-row">
      <div className="vset-row-lbl">
        {label}
        {why && <Why say={why} />}
      </div>
      <div className="vset-row-val">{children}</div>
    </div>
  )
}

function Btn({ children, primary, onClick, disabled }: { children: ReactNode; primary?: boolean; onClick: () => void; disabled?: boolean }) {
  return (
    <button type="button" className={`vset-btn${primary ? ' is-primary' : ''}`} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  )
}

function Seg({ value, options, onPick, label, ro, chips }: { value: string; options: readonly { key: string; label: ReactNode }[]; onPick: (k: string) => void; label: string; ro: boolean; chips?: boolean }) {
  if (ro) return <span className="vset-ro-val">{options.find((o) => o.key === value)?.label ?? value}</span>
  return (
    <div className={`vset-seg${chips ? ' is-chips' : ''}`} role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" role="radio" aria-checked={o.key === value} className={o.key === value ? 'is-on' : ''} onClick={() => onPick(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

function Pick({ value, options, onPick, label, ro }: { value: string; options: readonly { key: string; label: string }[]; onPick: (k: string) => void; label: string; ro: boolean }) {
  if (ro) return <span className="vset-ro-val">{options.find((o) => o.key === value)?.label ?? value}</span>
  return (
    <select className="vset-select" value={value} aria-label={label} onChange={(e) => onPick(e.target.value)}>
      {options.map((o) => (
        <option key={o.key} value={o.key}>
          {o.label}
        </option>
      ))}
    </select>
  )
}

/** Applied, or what the next start changes, for one machine field. */
function Applied({ view, field }: { view: VoiceSettingsView; field: VoiceSettingField }) {
  if (!view.pending.includes(field)) {
    return (
      <Tick tone="ok" say={`Applied at the start ${when(view.applied?.at ?? null)}`}>
        Applied
      </Tick>
    )
  }
  const was = view.applied?.[field as 'machine'] ?? null
  return (
    <Tick tone="info" say={was ? `Applied now: ${was}` : 'The speech machine has not started yet'}>
      Next start
    </Tick>
  )
}

function KeyStatus({ view }: { view: VoiceSettingsView }) {
  const k = view.openai_key
  return (
    <div className="vset-key" data-key-status={k.set === null ? 'unknown' : k.set ? 'set' : 'unset'}>
      <span className="vset-key-name">
        OpenAI key
        <Why say="GPT-Live's key, held by the quota broker and never shown here. It is added from Capacity › Accounts." />
      </span>
      {k.set === true ? (
        <Tick tone="ok" say={k.by && k.at ? `Set by ${k.by}, ${when(k.at)}` : 'Set'}>
          Set{k.by ? ` · ${k.by}` : ''}
          {k.at ? ` · ${when(k.at)}` : ''}
        </Tick>
      ) : k.set === false ? (
        <Tick tone="warn" say="GPT-Live cannot start without it; own speech does not need it">
          Not set
        </Tick>
      ) : (
        <Tick tone="idle" say="The quota broker could not say">
          Unknown
        </Tick>
      )}
      {k.link.open && (
        <Tick tone="info" say="A one-time link to add the key is open">
          Link open · until {when(k.link.expires_at)}
        </Tick>
      )}
    </div>
  )
}

/** The broker's state of the dedicated key, as a tick. */
const KEY_STATE: Readonly<Record<string, { tone: Tone; text: string }>> = {
  available: { tone: 'ok', text: 'Available' },
  limited: { tone: 'warn', text: 'Limited' },
  paused: { tone: 'idle', text: 'Paused' },
  draining: { tone: 'warn', text: 'Draining' },
  credit_gone: { tone: 'bad', text: 'Credit gone' },
}

/** The dedicated key's row, and its Delete behind the typed label (§4.4). */
function DedicatedKey({ k, canEdit, onDeleted }: { k: VoiceDedicatedKey; canEdit: boolean; onDeleted: (view: VoiceSettingsView) => void }) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // A second click before the first answer re-renders must not send a second delete.
  const sending = useRef(false)
  const name = k.display_name ?? k.label ?? 'key'
  const state = k.state !== null ? (KEY_STATE[k.state] ?? { tone: 'idle' as Tone, text: k.state }) : null
  const label = k.label
  const deletable = canEdit && k.can_delete && k.set === true && label !== null

  const close = () => {
    setOpen(false)
    setTyped('')
    setError(null)
  }
  const remove = async () => {
    if (label === null || typed !== label || sending.current) return
    sending.current = true
    setBusy(true)
    setError(null)
    const r = await deleteVoiceDedicatedKey(label)
    sending.current = false
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      close()
      onDeleted(r.data)
    } else if (r.status === 'error') {
      setError(r.error.message)
    }
  }

  return (
    <div className="vset-key" data-dedicated-key={k.set === null ? 'unknown' : k.set ? 'set' : 'unset'}>
      <span className="vset-key-name">
        Dedicated key
        <Why say="Voice it's own OpenAI key, the last tier after plans and shared keys. Held by the quota broker and never shown here." />
      </span>
      {k.set === true ? (
        <span className="vset-switchrow">
          <span className="vset-mono">{name}</span>
          {state !== null && (
            <Tick tone={state.tone} say={k.by && k.at ? `Added by ${k.by}, ${when(k.at)}` : state.text}>
              {state.text}
              {k.by ? ` · ${k.by}` : ''}
              {k.at ? ` · ${when(k.at)}` : ''}
            </Tick>
          )}
          {deletable && !open && <Btn onClick={() => setOpen(true)}>Delete</Btn>}
        </span>
      ) : k.set === false ? (
        <Tick tone="idle" say="No dedicated key is stored">
          Not set
        </Tick>
      ) : (
        <Tick tone="idle" say="The quota broker could not say">
          Unknown
        </Tick>
      )}
      {open && deletable && (
        <div className="vset-band is-bad" role="dialog" aria-label={`Delete API key ${name}`}>
          <div className="vset-band-text">
            <strong>Delete API key {name}</strong>
            <span>
              Revoke it at OpenAI too.
              <Why say="The stored key is destroyed now. Sessions running on it keep the value they already read until their next renew, then move to the next tier or end. Deleting here does not revoke the key at the provider: a copy anywhere else keeps working until it is revoked there." />
            </span>
            <input
              className="c-inp vset-mono"
              aria-label={`Type ${label} to delete`}
              placeholder={label}
              autoComplete="off"
              spellCheck={false}
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
            />
            {error !== null && (
              <Tick tone="bad" say={error}>
                Not deleted · {error}
              </Tick>
            )}
          </div>
          <div className="vset-band-acts">
            <Btn onClick={close} disabled={busy}>
              Cancel
            </Btn>
            <Btn primary onClick={() => void remove()} disabled={busy || typed !== label}>
              Delete key
            </Btn>
          </div>
        </div>
      )}
    </div>
  )
}

/** Telemetry, where the traces are kept: out of the router, as the console's other navigations go. */
function openTraces(): void {
  window.history.pushState(null, '', '/voice/telemetry')
  window.dispatchEvent(new PopStateEvent('popstate'))
}

interface Conflict {
  by: string | null
  at: string | null
  version: number
}

/** Voice it › Settings: the four cards, editable for a platform admin, read only otherwise. */
export function VoiceSettings({ canEdit }: { canEdit: boolean }) {
  const [catalogue, setCatalogue] = useState<VoiceCatalogue | null>(null)
  const [view, setView] = useState<VoiceSettingsView | null>(null)
  const [readError, setReadError] = useState<ApiError | null>(null)
  const [draft, setDraft] = useState<Draft>({})
  const [saving, setSaving] = useState(false)
  const [conflict, setConflict] = useState<Conflict | null>(null)
  const [refused, setRefused] = useState<{ field: string; message: string } | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [debugSaving, setDebugSaving] = useState(false)

  const load = useCallback(async (): Promise<VoiceSettingsView | null> => {
    const [c, s] = await Promise.all([loadVoiceCatalogue(), loadVoiceSettings()])
    if (c.status === 'error' || s.status === 'error') {
      setReadError(c.status === 'error' ? c.error : s.status === 'error' ? s.error : null)
      return null
    }
    if (c.status === 'loading' || s.status === 'loading' || c.status === 'empty' || s.status === 'empty') return null
    setReadError(null)
    setCatalogue(c.data)
    setView(s.data)
    return s.data
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  if (readError !== null && (readError.kind === 'admin_required' || readError.kind === 'forbidden')) {
    return (
      <>
        <PageHead title="Settings" />
        <Absent kind="admin" heading="Voice it is not open to you" say="Voice it is open only to the people it was set up for">
          In this version Voice it is one person's: the platform owner's.
        </Absent>
      </>
    )
  }
  if (readError !== null) {
    return (
      <div className="vset">
        <PageHead title="Settings" />
        <div className="vset-fail" role="alert">
          <Tick tone="bad" say={readError.message}>
            Settings not read{readError.httpStatus !== null ? ` · ${readError.httpStatus}` : ''}
          </Tick>
          <Btn onClick={() => void load()}>Retry</Btn>
        </div>
      </div>
    )
  }
  if (catalogue === null || view === null) {
    return (
      <div className="vset">
        <PageHead title="Settings" />
      </div>
    )
  }

  const ro = !canEdit
  const value = (f: VoiceSettingField): string => draft[f] ?? view.settings[f]
  const changed = (Object.keys(draft) as VoiceSettingField[]).filter((f) => draft[f] !== view.settings[f])
  const pick = (f: VoiceSettingField) => (v: string) => {
    setRefused(null)
    setDraft((d) => {
      const next: Draft = { ...d, [f]: v }
      // A text-to-speech model brings its own voices: keep the voice only when the new model has it.
      if (f === 'tts_model') {
        const model = catalogue.tts_models.find((m) => m.name === v)
        const voice = next.tts_voice ?? view.settings.tts_voice
        if (model && !model.voices.includes(voice)) next.tts_voice = model.default_voice
      }
      return next
    })
  }

  const save = async (version: number) => {
    const changes: Draft = {}
    for (const f of changed) changes[f] = draft[f]
    setSaving(true)
    setSaveError(null)
    setRefused(null)
    const r = await saveVoiceSettings(version, changes)
    if (r.status === 'ok' || r.status === 'stale') {
      setView(r.data)
      setDraft({})
      setConflict(null)
    } else if (r.status === 'error' && r.error.kind === 'conflict') {
      const fresh = await load()
      if (fresh !== null) setConflict({ by: fresh.updated_by, at: fresh.updated_at, version: fresh.version })
    } else if (r.status === 'error' && r.error.kind === 'invalid') {
      const field = (r.error.detail as { field?: unknown } | undefined)?.field
      setRefused({ field: typeof field === 'string' ? field : 'settings', message: r.error.message })
    } else if (r.status === 'error') {
      setSaveError(r.error.message)
    }
    setSaving(false)
  }

  /** Debug mode is saved on its own, at once: the switch moves only with the answer. */
  const flipDebug = async () => {
    setDebugSaving(true)
    setSaveError(null)
    const r = await saveVoiceSettings(view.version, { debug: !view.debug })
    if (r.status === 'ok' || r.status === 'stale') {
      setView(r.data)
    } else if (r.status === 'error') {
      setSaveError(r.error.message)
      // Someone else saved first: read again, so the next flip sends their version.
      if (r.error.kind === 'conflict') await load()
    }
    setDebugSaving(false)
  }

  const discard = () => {
    setDraft({})
    setConflict(null)
    setRefused(null)
    setSaveError(null)
  }

  /** The band a card carries: someone else's save, or a refusal of one of its fields. */
  const bandFor = (card: string): ReactNode => {
    if (conflict !== null && changed.some((f) => CARD_OF[f] === card)) {
      return (
        <Band
          tone="warn"
          actions={
            <>
              <Btn onClick={discard}>Use theirs</Btn>
              <Btn primary onClick={() => void save(conflict.version)} disabled={saving}>
                Save mine over it
              </Btn>
            </>
          }
        >
          <strong>Saved first by {conflict.by ?? 'someone else'}</strong>
          <span>
            v{conflict.version} · {when(conflict.at)}
          </span>
        </Band>
      )
    }
    if (refused !== null && (CARD_OF[refused.field as VoiceSettingField] ?? 'engine') === card) {
      return (
        <Band tone="bad">
          <strong>Refused</strong>
          <span>
            <span className="vset-mono">{refused.field}</span>: {refused.message}
          </span>
        </Band>
      )
    }
    return undefined
  }

  const engine = catalogue.engines.find((e) => e.name === value('engine'))
  const tts = catalogue.tts_models.find((m) => m.name === value('tts_model'))
  const priceMax = Math.max(...catalogue.machines.map((m) => m.usd_per_hour), 0.01)
  const updated = view.updated_by ? `v${view.version} · ${view.updated_by} · ${when(view.updated_at)}` : `v${view.version}`

  return (
    <div className="vset">
      <PageHead title="Settings">
        <span className="vset-headfacts">
          <span className="vset-fact" title={`Version ${view.version}`}>
            {updated}
          </span>
          {ro && (
            <span className="vset-lock" title="Only platform admins change Voice it's settings">
              Read only
            </span>
          )}
          {saveError !== null && (
            <Tick tone="bad" say={saveError}>
              Not saved
            </Tick>
          )}
          {!ro && changed.length > 0 && conflict === null && (
            <>
              <Btn onClick={discard}>Discard</Btn>
              <Btn primary onClick={() => void save(view.version)} disabled={saving}>
                Save
              </Btn>
            </>
          )}
        </span>
      </PageHead>

      <div className="vset-grid">
        <Card title="Engine" id="engine" aside={<Why say="The engine every Start uses. When it cannot start, the session runs in text mode." />} band={bandFor('engine')}>
          <Row label="Speech">
            <Seg ro={ro} label="Engine" value={value('engine')} onPick={pick('engine')} options={catalogue.engines.map((e) => ({ key: e.name, label: e.label }))} />
          </Row>
          {engine?.uses_machine !== false && (
            <Row label="Replies" why="The fast model that answers each spoken sentence">
              <Pick ro={ro} label="Fast model" value={value('conversation_model')} onPick={pick('conversation_model')} options={catalogue.conversation_models.map((m) => ({ key: m.name, label: m.label }))} />
            </Row>
          )}
          <Row label="Plans and acts" why="The planner's model class, read at each session's start">
            <Pick
              ro={ro}
              label="Planner model"
              value={value('planner_model')}
              onPick={pick('planner_model')}
              options={catalogue.planner_models.map((m) => ({ key: m.name, label: m.model ? `${m.label} · ${m.model}` : m.label }))}
            />
          </Row>
          <div className="vset-row" data-row="debug">
            <div className="vset-row-lbl">
              Debug
              <Why say="Every session keeps a turn-by-turn trace: what the planner was given, what it read, the cards and mock-ups it drew or refused and why, and what the voice was told. Talk shows the last turn under the Stage; Telemetry keeps the traces. Text is masked. Off, nothing is drawn and nothing is kept. A change reaches the next session." />
            </div>
            <div className="vset-row-val">
              <span className="vset-switchrow">
                <button
                  type="button"
                  role="switch"
                  aria-checked={view.debug}
                  aria-label="Debug mode"
                  className="vset-switch"
                  disabled={ro || debugSaving}
                  onClick={() => void flipDebug()}
                >
                  <i aria-hidden="true" />
                </button>
                {view.debug && <Btn onClick={openTraces}>Traces</Btn>}
              </span>
            </div>
          </div>
          <KeyStatus view={view} />
          <DedicatedKey k={view.dedicated_key} canEdit={canEdit} onDeleted={setView} />
        </Card>

        <Card
          title="Machine"
          id="machine"
          aside={
            catalogue.zones.length <= 1 ? (
              <span className="vset-card-aside">
                <span className="vset-mono vset-sub">{value('zone')}</span>
                <Applied view={view} field="zone" />
              </span>
            ) : undefined
          }
          band={bandFor('machine')}
        >
          {catalogue.zones.length > 1 && (
            <Row label="Zone">
              <Seg ro={ro} label="Zone" value={value('zone')} onPick={pick('zone')} options={catalogue.zones.map((z) => ({ key: z, label: <span className="vset-mono">{z}</span> }))} />
              <Applied view={view} field="zone" />
            </Row>
          )}
          <div className="vset-mach-cols">
            <Applied view={view} field="machine" />
            <span className="vset-mach-unit">
              per hour
              <Why say={catalogue.machines.map((m) => `${m.name}: ${m.price_source ?? 'no source'}${m.price_read_at ? `, ${m.price_read_at}` : ''}`).join('; ')} />
            </span>
          </div>
          <div className="vset-machines" role={ro ? undefined : 'radiogroup'} aria-label="Machine">
            {catalogue.machines.map((m) => {
              const on = m.name === value('machine')
              const body = (
                <>
                  <span className="vset-radio" aria-hidden="true" />
                  <span className="vset-mach-id">
                    <span className="vset-mono">{m.name}</span>
                    <span className="vset-mach-spec">
                      {m.vcpu} vCPU · {m.memory_gb} GB{m.gpus > 0 ? ` · ${m.gpus} GPU` : ''}
                    </span>
                  </span>
                  <span className="vset-mach-bar" aria-hidden="true">
                    <i style={{ width: `${Math.round((m.usd_per_hour / priceMax) * 100)}%` }} />
                  </span>
                  <span className="vset-mach-price">${m.usd_per_hour.toFixed(2)}</span>
                </>
              )
              if (ro) return on ? <div key={m.name} className="vset-mach">{body}</div> : null
              return (
                <button key={m.name} type="button" role="radio" aria-checked={on} className={`vset-mach${on ? ' is-on' : ''}`} onClick={() => pick('machine')(m.name)}>
                  {body}
                </button>
              )
            })}
          </div>
        </Card>

        <Card title="Models" id="models" band={bandFor('models')}>
          <Row label="Speech to text">
            <Pick ro={ro} label="Speech-to-text model" value={value('stt_model')} onPick={pick('stt_model')} options={catalogue.stt_models.map((m) => ({ key: m.name, label: m.disk_gb !== null ? `${m.name} · ${m.disk_gb} GB` : m.name }))} />
            <Applied view={view} field="stt_model" />
          </Row>
          <Row label="Text to speech">
            <Pick ro={ro} label="Text-to-speech model" value={value('tts_model')} onPick={pick('tts_model')} options={catalogue.tts_models.map((m) => ({ key: m.name, label: m.name }))} />
            <Applied view={view} field="tts_model" />
          </Row>
          <Row label="Voice">
            <Seg ro={ro} chips label="Voice" value={value('tts_voice')} onPick={pick('tts_voice')} options={(tts?.voices ?? []).map((v) => ({ key: v, label: <span className="vset-mono id">{v}</span> }))} />
          </Row>
        </Card>

        <Card title="Behaviour" id="behaviour" band={bandFor('behaviour')}>
          <Row label="Idle stop" why="With nobody speaking for this long, the session ends and the machine stops">
            <Seg ro={ro} label="Idle stop" value={value('idle')} onPick={pick('idle')} options={catalogue.idle.map((i) => ({ key: i.name, label: i.name.replace(/m$/, ' min') }))} />
            <Applied view={view} field="idle" />
          </Row>
        </Card>
      </div>
    </div>
  )
}

/**
 * Voice it › Settings as a page: editable for a platform admin; for anyone
 * else, read only when swarm-api lets them read (a Voice it person), and the
 * section's refusal when it does not.
 */
export function VoiceSettingsScreen() {
  const [admin, setAdmin] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    void loadMe().then((r) => {
      if (live) setAdmin((r.status === 'ok' || r.status === 'stale') && r.data.principal.is_admin === true)
    })
    return () => {
      live = false
    }
  }, [])
  if (admin === null) return null
  return <VoiceSettings canEdit={admin} />
}
