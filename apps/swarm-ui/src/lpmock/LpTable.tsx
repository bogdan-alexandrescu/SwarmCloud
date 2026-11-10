// MOCK-HOOK FILE
// Capacity › LLM Providers, direction "table": one dense table per sub-page, vendor chips, lends as
// indented sub-rows, and every add / edit / lend / replace / delete in a right side sheet.
import { Fragment, useEffect, useState, type ReactNode } from 'react'
import { PageHead } from '../Shell'
import { Info } from '../AccountsVerdict'
import {
  CLASSES, PLAN_LABEL, VENDOR_LABEL, WORKSPACES, fixtureSet, tierToday,
  type LpAccount, type LpState, type Lend, type Vendor,
} from './fixtures'
import { lpParam, type LpSub } from './lp'
import './lp.css'

type Filter = 'all' | Vendor
type Sheet =
  | { kind: 'add-plan' }
  | { kind: 'add-key' }
  | { kind: 'manage'; id: string }
  | { kind: 'lend'; id: string }
  | { kind: 'replace'; id: string }
  | { kind: 'delete'; id: string }
  | null

const usd = (n: number) => `$${n < 100 ? n.toFixed(2) : Math.round(n).toLocaleString('en-US')}`

function sheetFromUrl(): Sheet {
  const s = lpParam('sheet')
  const id = lpParam('id') ?? ''
  if (s === 'add-plan' || s === 'add-key') return { kind: s }
  if (s === 'manage' || s === 'lend' || s === 'replace' || s === 'delete') return { kind: s, id }
  return null
}

/** The state cell: a dot alone when nothing is asked; the word where it asks for something. */
function StateCell({ a, onResume }: { a: LpAccount; onResume?: () => void }) {
  if (a.state === 'available')
    return (
      <span className="lp-st is-ok" title="Available: takes new agents">
        <i aria-hidden="true" />
        <span className="lp-vh">Available</span>
      </span>
    )
  // Limited and Draining ask nothing of anyone: a coloured dot and the figure, the word only for a reader.
  const word: Record<Exclude<LpState, 'available'>, ReactNode> = {
    limited: <><span className="lp-vh">Limited, </span>until {a.until}</>,
    exhausted: <>Credit gone</>,
    paused: <>Paused</>,
    draining: <><span className="lp-vh">Draining, </span>{a.drainLeft} left</>,
  }
  const tip: Record<Exclude<LpState, 'available'>, string> = {
    limited: `Limited: the provider rate-limited it; it takes agents again at ${a.until}`,
    exhausted: `Refused for credit since ${a.until}; probed hourly with a one-token call`,
    paused: 'Holders keep it; nothing new is assigned',
    draining: `Draining: nothing new; ${a.drainLeft} holder${a.drainLeft === 1 ? '' : 's'} left, each moving to the next tier at its next turn`,
  }
  return (
    <span className="lp-stw">
      <span className={`lp-st is-${a.state}${a.state === 'limited' || a.state === 'draining' ? ' is-quiet' : ''}`} title={tip[a.state]}>
        <i aria-hidden="true" />
        {word[a.state]}
      </span>
      {(a.state === 'exhausted' || a.state === 'paused') && onResume && (
        <button type="button" className="c-btn is-sm" onClick={onResume}>
          Resume
        </button>
      )}
    </span>
  )
}

/** used over cap, as a number pair and a hairline bar; over the cap turns the bar red. */
function Over({ used, cap, fmt = String, unit, title }: { used: number; cap: number | null; fmt?: (n: number) => string; unit?: string; title?: string }) {
  if (cap === null)
    return (
      <span className="lp-over" title={title}>
        <span className="lp-over-n">
          {fmt(used)} <span className="lp-dim">· no cap</span>
        </span>
      </span>
    )
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0
  const tone = used >= cap ? 'is-full' : pct >= 80 ? 'is-near' : ''
  return (
    <span className={`lp-over ${tone}`} title={title}>
      <span className="lp-over-n">
        {fmt(used)}
        <span className="lp-dim">
          {' '}/ {fmt(cap)}
          {unit ? ` ${unit}` : ''}
        </span>
      </span>
      <span className="lp-bar" aria-hidden="true">
        <b style={{ width: `${pct}%` }} />
      </span>
    </span>
  )
}

function Win({ w, name }: { w: { usedPct: number; resets: string } | undefined; name: string }) {
  if (!w) return <span className="lp-dim">—</span>
  const tone = w.usedPct >= 100 ? 'is-full' : w.usedPct >= 80 ? 'is-near' : ''
  return (
    <span className={`lp-over ${tone}`} title={`${name}: ${w.usedPct}% used, resets ${w.resets}`}>
      <span className="lp-over-n">
        {w.usedPct}%<span className="lp-dim"> · {w.resets}</span>
      </span>
      <span className="lp-bar" aria-hidden="true">
        <b style={{ width: `${Math.min(100, w.usedPct)}%` }} />
      </span>
    </span>
  )
}

function VoiceMark({ on }: { on: boolean }) {
  return on ? (
    <span className="lp-voice" title="Shared with Voice it">
      Voice it
    </span>
  ) : (
    <span className="lp-dim" title="Not shared with Voice it">
      —
    </span>
  )
}

function Who({ a }: { a: LpAccount }) {
  return (
    <span className="lp-who">
      <span className="lp-name" title={a.name}>
        {a.name}
      </span>
      <span className="lp-meta">
        <span className="mono" title={a.label}>{a.label}</span>
        {a.plan ? (
          <span className="lp-added" title={`${PLAN_LABEL[a.vendor]} ${a.plan}; added by ${a.addedBy} · ${a.addedAt}`}>{`${PLAN_LABEL[a.vendor]} ${a.plan} · ${a.addedBy} · ${a.addedAt}`}</span>
        ) : (
          <span className="lp-added" title={a.replaced ? `Added by ${a.addedBy} · ${a.addedAt}; replaced by ${a.replaced.by} · ${a.replaced.at}` : `Added by ${a.addedBy} · ${a.addedAt}`}>
            {a.replaced ? `replaced · ${a.replaced.by} · ${a.replaced.at}` : `${a.addedBy} · ${a.addedAt}`}
          </span>
        )}
      </span>
      <span className="lp-classes" title="Model classes it serves">
        {a.classes.join(' · ')}
      </span>
    </span>
  )
}

function LendRow({ l, kind, cols, onEdit }: { l: Lend; kind: 'plan' | 'key'; cols: number; onEdit: () => void }) {
  return (
    <tr className="lp-lend">
      <td colSpan={2} data-l="Lent to">
        <span className="lp-lend-who">
          <span className="lp-lend-arm" aria-hidden="true" />
          <span className="mono">{l.tenant}</span>
        </span>
      </td>
      <td data-l="Agents">
        {l.maxAgents === null ? (
          <span className="lp-over">
            <span className="lp-over-n">
              {l.agents} <span className="ctl-mark is-partial" title="Carried over from before lend caps; it is bounded by the plan's own max agents until a lender saves one">cap not set</span>
            </span>
          </span>
        ) : (
          <Over used={l.agents} cap={l.maxAgents} title={`${l.tenant}: agents now over the lend's cap`} />
        )}
      </td>
      <td colSpan={Math.max(1, cols - 4)} className="lp-opt" data-l={kind === 'key' ? 'Spend' : ''}>
        {kind === 'key' && l.spend !== undefined && l.spendCap !== undefined && <Over used={l.spend} cap={l.spendCap} fmt={usd} title={`${l.tenant}: this month's estimated spend over the lend's cap`} />}
      </td>
      <td className="lp-act">
        <button type="button" className="c-btn is-sm is-ghost" onClick={onEdit}>
          Edit lend
        </button>
      </td>
    </tr>
  )
}

function Table({
  sub, rows, filter, flash, open, onResume, sel, narrowed,
}: {
  sel: string | null
  narrowed: boolean
  sub: LpSub
  rows: LpAccount[]
  filter: Filter
  flash: string | null
  open: (s: Sheet) => void
  onResume: (id: string) => void
}) {
  const plans = sub === 'plans'
  const narrow = narrowed && typeof window !== 'undefined' && window.innerWidth >= 1100
  const cols = narrow ? 4 : plans ? 9 : 8
  const vendors: Vendor[] = filter === 'all' ? ['anthropic', 'openai'] : [filter]
  return (
    <div className="lp-tablewrap">
      <table className={`lp-table ${plans ? 'is-plans' : 'is-keys'}`}>
        <thead>
          <tr>
            <th>{plans ? 'Plan' : 'Key'}</th>
            <th>State</th>
            <th>
              Agents <Info about="Agents" tip="Live holders over max agents, counted from the grant (LEASED), not from RUNNING" />
            </th>
            {plans ? (
              <>
                <th className="lp-opt">5-hour</th>
                <th className="lp-opt">7-day</th>
              </>
            ) : (
              <>
                <th className="lp-opt">
                  Spend, Nov <Info about="Spend" tip="The platform's own estimate from each attempt's token count and list price, this calendar month (UTC), over the key's spend cap. Never the provider's figure: a plain key cannot read it." />
                </th>
              </>
            )}
            <th className="lp-opt">
              Today <Info about="Today" tip={plans ? 'Attempts that ran on this plan today' : 'Attempts that ran on this key today; in brackets, those that came here because every plan was spent'} />
            </th>
            <th className="lp-opt">Voice it</th>
            <th className="lp-opt">Lent</th>
            <th className="lp-act">
              <span className="lp-vh">Actions</span>
            </th>
          </tr>
        </thead>
        {vendors.map((v) => {
          const vr = rows.filter((r) => r.vendor === v)
          if (vr.length === 0) return null
          return (
            <tbody key={v}>
              {filter === 'all' && (
                <tr className="lp-group">
                  <th colSpan={cols} scope="colgroup">
                    {VENDOR_LABEL[v]} <span className="lp-dim">{vr.length}</span>
                  </th>
                </tr>
              )}
              {vr.map((a) => (
                <Fragment key={a.id}>
                  <tr className={`lp-row is-${a.state}${flash === a.id ? ' is-flash' : ''}${sel === a.id ? ' is-sel' : ''}`} aria-selected={sel === a.id || undefined}>
                    <td data-l="">
                      <Who a={a} />
                    </td>
                    <td data-l="State">
                      <StateCell a={a} onResume={sel === a.id ? undefined : () => onResume(a.id)} />
                    </td>
                    <td data-l="Agents">
                      <Over used={a.holders} cap={a.maxAgents} title="Live holders over max agents" />
                    </td>
                    {plans ? (
                      <>
                        <td data-l="5-hour" className="lp-opt">
                          <Win w={a.fiveHour} name="5-hour window" />
                        </td>
                        <td data-l="7-day" className="lp-opt">
                          <Win w={a.sevenDay} name="7-day window" />
                        </td>
                      </>
                    ) : (
                      <>
                        <td data-l="Spend" className="lp-opt">
                          <Over used={a.spend ?? 0} cap={a.spendCap ?? null} fmt={usd} title="Estimated spend this month over the key's cap" />
                        </td>

                      </>
                    )}
                    <td data-l="Today" className="lp-num lp-opt">
                      {a.today}
                      {!plans && (a.fallbackToday ?? 0) > 0 && (
                        <span className="lp-dim" title="Came here because every plan was spent">
                          {' '}({a.fallbackToday})
                        </span>
                      )}
                    </td>
                    <td data-l="Voice it" className="lp-opt">
                      <VoiceMark on={a.voiceIt} />
                    </td>
                    <td data-l="Lent" className="lp-num lp-opt">
                      {a.lends.length > 0 ? a.lends.length : <span className="lp-dim">—</span>}
                    </td>
                    <td className="lp-act">
                      <button type="button" className="c-btn is-sm" onClick={() => open({ kind: 'manage', id: a.id })}>
                        Manage
                      </button>
                    </td>
                  </tr>
                  {a.lends.map((l) => (
                    <LendRow key={l.tenant} l={l} kind={a.kind} cols={cols} onEdit={() => open({ kind: 'lend', id: a.id })} />
                  ))}
                </Fragment>
              ))}
            </tbody>
          )
        })}
      </table>
    </div>
  )
}

/* ---------------- the side sheet ---------------- */

function SheetFrame({ title, sub, onClose, onBack, children, foot }: { title: string; sub?: ReactNode; onClose: () => void; onBack?: () => void; children: ReactNode; foot?: ReactNode }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <aside className="lp-sheet" role="dialog" aria-modal="false" aria-label={title}>
      <header className="lp-sheet-head">
        {onBack && (
          <button type="button" className="c-btn is-sm is-ghost" onClick={onBack}>
            Back
          </button>
        )}
        <div className="lp-sheet-t">
          <h2>{title}</h2>
          {sub && <div className="lp-sheet-sub">{sub}</div>}
        </div>
        <button type="button" className="c-btn is-sm is-ghost lp-x" aria-label="Close" onClick={onClose}>
          ✕
        </button>
      </header>
      <div className="lp-sheet-body">{children}</div>
      {foot && <footer className="lp-sheet-foot">{foot}</footer>}
    </aside>
  )
}

function Field({ label, info, children, hint }: { label: string; info?: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="lp-f">
      <span className="lp-f-l">
        {label}
        {info && <Info about={label} tip={info} />}
      </span>
      {children}
      {hint && <span className="lp-f-h">{hint}</span>}
    </label>
  )
}

function Seg<T extends string>({ value, options, onPick, label }: { value: T; options: { key: T; label: ReactNode }[]; onPick: (v: T) => void; label: string }) {
  return (
    <div className="c-seg lp-seg" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o.key} type="button" role="radio" aria-checked={value === o.key} onClick={() => onPick(o.key)}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

function Switch({ on, onFlip, label }: { on: boolean; onFlip: () => void; label: string }) {
  return (
    <button type="button" role="switch" aria-checked={on} className={`lp-sw${on ? ' is-on' : ''}`} onClick={onFlip}>
      <i aria-hidden="true" />
      <span>{label}</span>
    </button>
  )
}

function Section({ title, info, children, aside }: { title: string; info?: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="lp-ss">
      <h3>
        {title}
        {info && <Info about={title} tip={info} />}
        {aside && <span className="lp-ss-aside">{aside}</span>}
      </h3>
      {children}
    </section>
  )
}

function AddPlan({ close, initialVendor }: { close: () => void; initialVendor: Vendor | null }) {
  const [vendor, setVendor] = useState<Vendor | null>(initialVendor)
  const [how, setHow] = useState<'here' | 'link'>('here')
  const [label, setLabel] = useState(initialVendor === 'anthropic' ? 'design-review' : '')
  return (
    <SheetFrame title="Add plan" onClose={close}>
      <Field label="Vendor">
        <Seg<Vendor>
          label="Vendor"
          value={vendor ?? ('' as Vendor)}
          onPick={setVendor}
          options={[
            { key: 'anthropic', label: 'Anthropic Claude' },
            { key: 'openai', label: 'OpenAI ChatGPT' },
          ]}
        />
      </Field>
      {vendor && (
        <>
          <Field label="Signed in by">
            <Seg
              label="Signed in by"
              value={how}
              onPick={setHow}
              options={[
                { key: 'here', label: 'Me, here' },
                { key: 'link', label: "The plan's owner" },
              ]}
            />
          </Field>
          <Field label="Label" info="The plan's secret is named after it, so it is fixed once added; the display name can change later.">
            <input className="lp-in mono" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="team-primary" spellCheck={false} />
          </Field>
          {how === 'here' ? (
            vendor === 'anthropic' ? (
              <ol className="lp-steps">
                <li className="is-done">
                  <span className="lp-step-n">1</span>
                  <span className="lp-step-t">Claude sign-in</span>
                  <button type="button" className="c-btn is-sm">
                    Open sign-in
                  </button>
                  <Info about="Claude sign-in" tip="Opens Claude's sign-in in a new tab. Sign in with the subscription's account; it shows a one-time code." />
                </li>
                <li className="is-now">
                  <span className="lp-step-n">2</span>
                  <span className="lp-step-t">Code</span>
                  <input className="lp-in mono" placeholder="Paste the code" autoComplete="off" spellCheck={false} defaultValue="" aria-label="Sign-in code" />
                </li>
              </ol>
            ) : (
              <ol className="lp-steps">
                <li className="is-now">
                  <span className="lp-step-n">1</span>
                  <span className="lp-step-t">Codex sign-in</span>
                  <span className="lp-code mono" title="Enter it on the sign-in page; it expires in 15 minutes">
                    QKRT-7FWN
                  </span>
                  <button type="button" className="c-btn is-sm">
                    Open sign-in
                  </button>
                </li>
                <li>
                  <span className="lp-step-n">2</span>
                  <span className="lp-step-t lp-dim">Waiting for the sign-in</span>
                </li>
              </ol>
            )
          ) : (
            <>
              <Field label="Owner's email" info="A one-time link bound to this verified email: single use, ten minutes.">
                <input className="lp-in" type="email" placeholder="owner@example.com" />
              </Field>
            </>
          )}
          <Field label="Max agents">
            <input className="lp-in lp-in-n" type="number" min={1} defaultValue={3} />
          </Field>
        </>
      )}
      <div className="lp-sheet-acts">
        <button type="button" className="c-btn is-primary" disabled={!vendor}>
          {how === 'link' ? 'Create link' : 'Add plan'}
        </button>
        <button type="button" className="c-btn is-ghost" onClick={close}>
          Cancel
        </button>
      </div>
    </SheetFrame>
  )
}

function AddKey({ close, onAdd, prefill }: { close: () => void; onAdd: (a: LpAccount) => void; prefill: boolean }) {
  const [vendor, setVendor] = useState<Vendor | null>(prefill ? 'anthropic' : null)
  const [label, setLabel] = useState(prefill ? 'research-batch' : '')
  const [how, setHow] = useState<'paste' | 'link'>('paste')
  const [val, setVal] = useState('')
  const [voice, setVoice] = useState(false)
  const [max, setMax] = useState(4)
  const [cap, setCap] = useState('200')
  const ok = vendor !== null && /^[a-z0-9-]{3,}$/.test(label) && (how === 'link' || val.length >= 20)
  const add = () => {
    if (!vendor) return
    onAdd({
      id: `new-${label}`, kind: 'key', vendor, name: label.replace(/-/g, ' ').replace(/^./, (c) => c.toUpperCase()), label,
      state: 'available', holders: 0, maxAgents: max, perWorkspace: [], classes: CLASSES[vendor].filter((c) => c !== 'live'),
      voiceIt: voice, today: 0, fallbackToday: 0, spend: 0, spendCap: cap === '' ? null : Number(cap),
      addedBy: 'You', addedAt: 'just now', lends: [],
    })
  }
  return (
    <SheetFrame title="Add key" onClose={close}>
      <Field label="Vendor">
        <Seg<Vendor>
          label="Vendor"
          value={vendor ?? ('' as Vendor)}
          onPick={setVendor}
          options={[
            { key: 'anthropic', label: 'Anthropic' },
            { key: 'openai', label: 'OpenAI' },
          ]}
        />
      </Field>
      <Field label="Label" info="The key's secret is named after it, so it is fixed once added; the display name can change later.">
        <input className="lp-in mono" value={label} onChange={(e) => setLabel(e.target.value.toLowerCase())} placeholder="build-pool" spellCheck={false} />
      </Field>
      <Field label="Key from">
        <Seg
          label="Key from"
          value={how}
          onPick={setHow}
          options={[
            { key: 'paste', label: 'Paste it here' },
            { key: 'link', label: 'Send a link' },
          ]}
        />
      </Field>
      {how === 'paste' ? (
        <Field
          label="Key"
          info="Checked with one read-only call to the vendor, then stored in Secret Manager only. It is never shown again, not even masked: there is nothing to reveal."
        >
          <input
            className="lp-in mono"
            type="password"
            autoComplete="off"
            spellCheck={false}
            name="lp-key"
            value={val}
            onChange={(e) => setVal(e.target.value)}
            placeholder={vendor === 'openai' ? 'sk-…' : 'sk-ant-…'}
          />
        </Field>
      ) : (
        <Field label="Key holder's email" info="A one-time link bound to this verified email: single use, ten minutes. The key goes only in that page's form.">
          <input className="lp-in" type="email" placeholder="holder@example.com" />
        </Field>
      )}
      <div className="lp-f-row">
        <Field label="Max agents">
          <input className="lp-in lp-in-n" type="number" min={1} value={max} onChange={(e) => setMax(Number(e.target.value))} />
        </Field>
        <Field label="Spend cap / month" info="Optional on the key itself; metered from the platform's own estimate.">
          <span className="lp-money">
            <span>$</span>
            <input className="lp-in lp-in-n" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} />
          </span>
        </Field>
      </div>
      <Switch on={voice} onFlip={() => setVoice(!voice)} label="Share with Voice it" />
      <div className="lp-sheet-acts">
        <button type="button" className="c-btn is-primary" disabled={!ok} onClick={add}>
          {how === 'link' ? 'Create link' : 'Add key'}
        </button>
        <button type="button" className="c-btn is-ghost" onClick={close}>
          Cancel
        </button>
      </div>
    </SheetFrame>
  )
}

function Manage({ a, close, go, onResume }: { a: LpAccount; close: () => void; go: (s: Sheet) => void; onResume: () => void }) {
  const key = a.kind === 'key'
  const [name, setName] = useState(a.name)
  const [max, setMax] = useState(lpParam('edit') === '1' ? 5 : a.maxAgents)
  const [cap, setCap] = useState(a.spendCap == null ? '' : String(lpParam('edit') === '1' ? 400 : a.spendCap))
  const [classes, setClasses] = useState<string[]>(lpParam('edit') === '1' ? a.classes.filter((c) => c !== 'opus') : a.classes)
  const [pw, setPw] = useState(a.perWorkspace)
  const [voice, setVoice] = useState(a.voiceIt)
  const dirty = max !== a.maxAgents || classes.join() !== a.classes.join() || cap !== (a.spendCap == null ? '' : String(a.spendCap)) || name !== a.name
  return (
    <SheetFrame
      title={a.name}
      sub={
        <>
          <span className="mono">{a.label}</span>
          <span>{key ? `${VENDOR_LABEL[a.vendor]} API key` : `${PLAN_LABEL[a.vendor]} ${a.plan}`}</span>
          {a.state !== 'exhausted' && <StateCell a={a} />}
        </>
      }
      onClose={close}
      foot={
        <>
          <button type="button" className="c-btn is-primary" disabled={!dirty}>
            Save
          </button>
          <button type="button" className="c-btn is-ghost" onClick={close}>
            Cancel
          </button>
          <span className="lp-grow" />
          <button type="button" className="c-btn is-danger" onClick={() => go({ kind: 'delete', id: a.id })}>
            {key ? 'Delete key' : 'Remove plan'}
          </button>
        </>
      }
    >
      {a.state === 'exhausted' && a.probe && (
        <div className="lp-band is-bad" role="status">
          <div>
            <b>Credit gone</b> <span className="lp-dim">since {a.until}</span>{' '}
            <Info about="Probe" tip="Every hour the broker makes one one-token call on the key. A success clears the state by itself; Resume clears it now, after the credit is topped up." />
            <div className="lp-probe">
              Probe {a.probe.at} · <span className="mono">{a.probe.result}</span> · next {a.probe.next}
            </div>
          </div>
          <button type="button" className="c-btn is-primary" onClick={onResume}>
            Resume
          </button>
        </div>
      )}
      <Section title="Leasing">
        <div className="lp-btnrow">
          {a.state === 'exhausted' ? null : a.state === 'paused' || a.state === 'draining' ? (
            <button type="button" className="c-btn" onClick={onResume}>
              Resume
            </button>
          ) : (
            <>
              <button type="button" className="c-btn" title="Holders keep it; nothing new is assigned">
                Pause
              </button>
              <button type="button" className="c-btn" title="Nothing new; holders move to the next tier at their next turn">
                Drain
              </button>
            </>
          )}
          {key ? (
            <button type="button" className="c-btn" onClick={() => go({ kind: 'replace', id: a.id })}>
              Replace key
            </button>
          ) : (
            <button type="button" className="c-btn" title="Read the windows now">
              Refresh
            </button>
          )}
        </div>
      </Section>
      <Section title="Limits">
        <div className="lp-f-row">
          <Field label="Max agents" info="Live holders at once, everyone together; counted from the grant.">
            <input className="lp-in lp-in-n" type="number" min={1} value={max} onChange={(e) => setMax(Number(e.target.value))} />
          </Field>
          {key && (
            <Field label="Spend cap / month" info="Past it the key reads as no quota left and holders move at their next turn. Metered from the platform's estimate.">
              <span className="lp-money">
                <span>$</span>
                <input className="lp-in lp-in-n" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} placeholder="none" />
              </span>
            </Field>
          )}
        </div>
        <div className="lp-f">
          <span className="lp-f-l">
            Per workspace
            <Info about="Per workspace" tip="At most this many of the agents at once may be one workspace's, the owner's included." />
          </span>
          {pw.length > 0 && <table className="lp-mini">
            <tbody>
              {pw.map((p, i) => (
                <tr key={p.tenant}>
                  <td className="mono">{p.tenant}</td>
                  <td>
                    <input
                      className="lp-in lp-in-n"
                      type="number"
                      min={1}
                      value={p.max}
                      aria-label={`${p.tenant} max agents`}
                      onChange={(e) => setPw(pw.map((q, j) => (j === i ? { ...q, max: Number(e.target.value) } : q)))}
                    />
                  </td>
                  <td>
                    <button type="button" className="c-btn is-sm is-ghost" onClick={() => setPw(pw.filter((_, j) => j !== i))}>
                      Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>}
          <button type="button" className="c-btn is-sm lp-add-pw" onClick={() => setPw([...pw, { tenant: WORKSPACES.find((w) => !pw.some((p) => p.tenant === w) && !a.lends.some((l) => l.tenant === w)) ?? 'tidewater', max: 1 }])}>
            Add workspace cap
          </button>
        </div>
        <div className="lp-f">
          <span className="lp-f-l">Model classes</span>
          <div className="lp-checks">
            {CLASSES[a.vendor].map((c) => (
              <button
                key={c}
                type="button"
                className="c-chip is-pick"
                aria-pressed={classes.includes(c)}
                onClick={() => setClasses(classes.includes(c) ? classes.filter((x) => x !== c) : [...classes, c])}
              >
                {c}
              </button>
            ))}
          </div>
        </div>
      </Section>
      <Section
        title="Lends"
        aside={
          <button type="button" className="c-btn is-sm" onClick={() => go({ kind: 'lend', id: a.id })}>
            Lend
          </button>
        }
      >
        {a.lends.length > 0 && (
          <table className="lp-mini is-lends">
            <tbody>
              {a.lends.map((l) => (
                <tr key={l.tenant}>
                  <td className="mono">{l.tenant}</td>
                  <td>{l.maxAgents === null ? <span className="ctl-mark is-partial">cap not set</span> : <Over used={l.agents} cap={l.maxAgents} />}</td>
                  {key && <td>{l.spend !== undefined && <Over used={l.spend} cap={l.spendCap ?? null} fmt={usd} />}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
      <Section title="Voice it" info={key ? 'Serves Voice it after the plans and before its dedicated key. Platform admins only: the cost lands on this key.' : 'Serves Voice it as its first tier. A ChatGPT plan serves the conversation model, never GPT-Live.'}>
        <Switch on={voice} onFlip={() => setVoice(!voice)} label="Share with Voice it" />
      </Section>
      <Section title="Name">
        <input className="lp-in" value={name} onChange={(e) => setName(e.target.value)} aria-label="Display name" />
        <div className="lp-f-h">
          Added by {a.addedBy} · {a.addedAt}
          {a.replaced ? ` · replaced by ${a.replaced.by} · ${a.replaced.at}` : ''}
        </div>
      </Section>
    </SheetFrame>
  )
}

function LendSheet({ a, close, back }: { a: LpAccount; close: () => void; back: () => void }) {
  const key = a.kind === 'key'
  const free = WORKSPACES.filter((w) => !a.lends.some((l) => l.tenant === w))
  const [t, setT] = useState(lpParam('to') ?? free[0] ?? '')
  const [max, setMax] = useState(2)
  const [cap, setCap] = useState(key ? '150' : '')
  const ok = t !== '' && max >= 1 && (!key || Number(cap) > 0)
  return (
    <SheetFrame
      title={`Lend ${a.name}`}
      sub={
        <>
          <span className="mono">{a.label}</span>
          <span>
            {a.holders} / {a.maxAgents} agents now
          </span>
        </>
      }
      onBack={back}
      onClose={close}
    >
      <Field label="Workspace">
        <select className="lp-in" value={t} onChange={(e) => setT(e.target.value)}>
          {free.map((w) => (
            <option key={w}>{w}</option>
          ))}
        </select>
      </Field>
      <div className="lp-f-row">
        <Field label="Agents at once" info="Required, at least 1. Enforced in the same transaction that grants the hold; within the key's own max agents.">
          <input className="lp-in lp-in-n" type="number" min={1} max={a.maxAgents} value={max} onChange={(e) => setMax(Number(e.target.value))} />
        </Field>
        {key && (
          <Field label="Spend cap / month" info="Required on a key. That workspace's estimated spend on this key this month; at the cap its next grant is refused and running holders move at their next turn.">
            <span className="lp-money">
              <span>$</span>
              <input className="lp-in lp-in-n" inputMode="decimal" value={cap} onChange={(e) => setCap(e.target.value)} />
            </span>
          </Field>
        )}
      </div>
      <div className="lp-sheet-acts">
        <button type="button" className="c-btn is-primary" disabled={!ok}>
          Lend
        </button>
        <button type="button" className="c-btn is-ghost" onClick={back}>
          Cancel
        </button>
      </div>
      {a.lends.length > 0 && (
        <Section title="Lent now" info="Narrowing a cap below its use closes nothing: the borrower runs on and its next grant is refused until it is under the cap.">
          <table className="lp-mini is-lends">
            <tbody>
              {a.lends.map((l) => (
                <tr key={l.tenant}>
                  <td className="mono">{l.tenant}</td>
                  <td>{l.maxAgents === null ? <span className="ctl-mark is-partial">cap not set</span> : <Over used={l.agents} cap={l.maxAgents} />}</td>
                  {key && <td>{l.spend !== undefined && <Over used={l.spend} cap={l.spendCap ?? null} fmt={usd} />}</td>}
                  <td>
                    <button type="button" className="c-btn is-sm is-ghost">
                      End
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Section>
      )}
    </SheetFrame>
  )
}

function ReplaceSheet({ a, close, back }: { a: LpAccount; close: () => void; back: () => void }) {
  const [how, setHow] = useState<'paste' | 'link'>('paste')
  const [val, setVal] = useState(lpParam('typed') === '1' ? 'x'.repeat(48) : '')
  return (
    <SheetFrame
      title={`Replace key ${a.name}`}
      sub={
        <>
          <span className="mono">{a.label}</span>
          <span>
            {a.replaced ? `replaced by ${a.replaced.by} · ${a.replaced.at}` : `added by ${a.addedBy} · ${a.addedAt}`}
          </span>
        </>
      }
      onBack={back}
      onClose={close}
    >
      <Field label="New key from">
        <Seg
          label="New key from"
          value={how}
          onPick={setHow}
          options={[
            { key: 'paste', label: 'Paste it here' },
            { key: 'link', label: 'Send a link' },
          ]}
        />
      </Field>
      {how === 'paste' ? (
        <Field label="New key" info="Checked with one read-only call, then stored as a new version under the same label. Holders keep the old value until their next turn; the old version is destroyed after the hold TTL. Caps, lends, sharing and spend are kept.">
          <input className="lp-in mono" type="password" autoComplete="off" spellCheck={false} value={val} onChange={(e) => setVal(e.target.value)} placeholder={a.vendor === 'openai' ? 'sk-…' : 'sk-ant-…'} />
        </Field>
      ) : (
        <Field label="Key holder's email" info="A one-time link bound to this verified email: single use, ten minutes.">
          <input className="lp-in" type="email" placeholder="holder@example.com" />
        </Field>
      )}
      <div className="lp-sheet-acts">
        <button type="button" className="c-btn is-primary" disabled={how === 'paste' && val.length < 20}>
          {how === 'link' ? 'Create link' : 'Replace key'}
        </button>
        <button type="button" className="c-btn is-ghost" onClick={back}>
          Cancel
        </button>
      </div>
    </SheetFrame>
  )
}

const VENDOR_KEYS: Record<Vendor, string> = {
  anthropic: 'https://console.anthropic.com/settings/keys',
  openai: 'https://platform.openai.com/api-keys',
}

function DeleteSheet({ a, close, back, onDelete }: { a: LpAccount; close: () => void; back: () => void; onDelete: () => void }) {
  const key = a.kind === 'key'
  const [typed, setTyped] = useState(lpParam('typed') === '1' ? a.label : '')
  return (
    <SheetFrame title={key ? `Delete API key ${a.name}` : `Remove plan ${a.name}`} onBack={back} onClose={close}>
      {key ? (
        <p className="lp-del-line">
          Revoke it at{' '}
          <a href={VENDOR_KEYS[a.vendor]} target="_blank" rel="noreferrer">
            {VENDOR_LABEL[a.vendor]}
          </a>{' '}
          too.
          <Info
            about="Delete"
            tip="The stored key is destroyed now. Agents running on it keep the value they already read until their next turn, then move to the next tier or park. Deleting here does not revoke the key at the provider: a copy anywhere else keeps working until it is revoked there."
          />
        </p>
      ) : (
        <p className="lp-del-line">
          {a.holders === 1 ? '1 holder moves at its next turn.' : `${a.holders} holders move at their next turn.`}
          <Info about="Remove" tip="Live holds close now. The plan's secret is kept, so an accidental removal can be signed in again." />
        </p>
      )}
      {a.holders + a.lends.length > 0 && (
        <dl className="lp-kv">
          <dt>Agents now</dt>
          <dd>{a.holders}</dd>
          {a.lends.length > 0 && (
            <>
              <dt>Lent to</dt>
              <dd className="mono">{a.lends.map((l) => l.tenant).join(', ')}</dd>
            </>
          )}
        </dl>
      )}
      <Field label={`Type ${a.label} to ${key ? 'delete' : 'remove'}`}>
        <input className="lp-in mono" value={typed} onChange={(e) => setTyped(e.target.value)} autoComplete="off" spellCheck={false} />
      </Field>
      <div className="lp-sheet-acts">
        <button type="button" className="c-btn is-danger-filled" disabled={typed !== a.label} onClick={onDelete}>
          {key ? 'Delete key' : 'Remove plan'}
        </button>
        <button type="button" className="c-btn is-ghost" onClick={back}>
          Cancel
        </button>
      </div>
    </SheetFrame>
  )
}

/* ---------------- the page ---------------- */

export function LlmProvidersTable({ sub, go }: { sub: LpSub; go: (to: string) => void }) {
  const fx = fixtureSet(lpParam('fx'))
  const [plans, setPlans] = useState(fx.plans)
  const [keys, setKeys] = useState(fx.keys)
  const [filter, setFilter] = useState<Filter>(((lpParam('vendor') as Filter | null) ?? 'all'))
  const [sheet, setSheet] = useState<Sheet>(sheetFromUrl())
  const [flash, setFlash] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  // /capacity/accounts lands on Plans (the redirect of §6.1): App's canonical address for this route.

  const rows = sub === 'plans' ? plans : keys
  const all = rows.filter((r) => filter === 'all' || r.vendor === filter)
  const count = (v: Filter) => rows.filter((r) => v === 'all' || r.vendor === v).length
  const find = (id: string) => [...plans, ...keys].find((a) => a.id === id) ?? null
  const close = () => setSheet(null)
  const resume = (id: string) => {
    const f = (xs: LpAccount[]) => xs.map((a) => (a.id === id ? { ...a, state: 'available' as LpState, probe: undefined } : a))
    setPlans(f(plans))
    setKeys(f(keys))
    setSheet((s) => (s && 'id' in s && s.id === id && s.kind === 'manage' ? s : s))
  }
  const sheetAcc = sheet && 'id' in sheet ? find(sheet.id) : null
  const tier = tierToday(plans, keys)

  return (
    <div className={`lp-page${sheet ? ' has-sheet' : ''}`}>
      <PageHead title="LLM Providers" info={<Info about="LLM Providers" tip="Agents take a plan first, then the shared API key pool; Voice it then falls to its own dedicated OpenAI key. The vendors never stand in for each other." />} />
      <div className="lp-bar1">
        <nav className="c-seg lp-subnav" aria-label="LLM Providers">
          {(['plans', 'api-keys'] as LpSub[]).map((s) => (
            <a
              key={s}
              href={`/capacity/llm-providers/${s}`}
              aria-current={sub === s ? 'page' : undefined}
              onClick={(e) => {
                e.preventDefault()
                setSheet(null)
                setFilter('all')
                go(`capacity/llm-providers/${s}`)
              }}
            >
              {s === 'plans' ? 'Plans' : 'API Keys'} <em>{s === 'plans' ? plans.length : keys.length}</em>
            </a>
          ))}
        </nav>
        {tier.plan + tier.key + tier.voice > 0 && <span className="lp-tier" title="Attempts today by the tier that served them">
          <span className="lp-tier-k">Today</span>
          {tier.plan > 0 && <span className="lp-chip is-plan">Plan {tier.plan}</span>}
          {tier.key > 0 && <span className="lp-chip is-key">API key {tier.key}</span>}
          {tier.voice > 0 && <span className="lp-chip is-voice">Voice it key {tier.voice}</span>}
          <Info about="Tiers today" tip="How many attempts each tier served today. API key attempts came after every plan of their vendor was spent, so this is what the fallback is costing." />
        </span>}
        <span className="lp-grow" />
        {rows.length > 0 && (
          <button type="button" className="c-btn is-primary" onClick={() => setSheet({ kind: sub === 'plans' ? 'add-plan' : 'add-key' })}>
            {sub === 'plans' ? 'Add plan' : 'Add key'}
          </button>
        )}
      </div>
      {rows.length === 0 ? (
        <section className="lp-empty">
          <h2>
            {sub === 'plans' ? 'No plans' : 'No API keys'}
            <Info about="No API keys" tip={sub === 'plans' ? 'Agents need a plan or a key of their vendor.' : 'Agents fall back to keys only after the plans, so a page with none is valid.'} />
          </h2>
          <span className="ctl-mark is-zero" title="The broker answered; it holds none">
            read · 0
          </span>
          <button type="button" className="c-btn is-primary" onClick={() => setSheet({ kind: sub === 'plans' ? 'add-plan' : 'add-key' })}>
            {sub === 'plans' ? 'Add plan' : 'Add key'}
          </button>
        </section>
      ) : (
        <>
          {toast && (
            <div className="lp-toast" role="status">
              {toast}
              <button type="button" className="c-btn is-sm is-ghost" onClick={() => setToast(null)}>
                Dismiss
              </button>
            </div>
          )}
          <div className="lp-filters" role="group" aria-label="Vendor">
            {(['all', 'anthropic', 'openai'] as Filter[]).map((v) => (
              <button key={v} type="button" className="c-chip is-pick lp-fchip" aria-pressed={filter === v} onClick={() => setFilter(v)}>
                {v === 'all' ? 'All' : VENDOR_LABEL[v]} <span className="lp-dim">{count(v)}</span>
              </button>
            ))}
          </div>
          <Table narrowed={sheet !== null} sel={sheet && 'id' in sheet ? sheet.id : null} sub={sub} rows={all} filter={filter} flash={flash} open={setSheet} onResume={resume} />
        </>
      )}
      {sheet && <div className="lp-scrim" onClick={close} aria-hidden="true" />}
      {sheet?.kind === 'add-plan' && <AddPlan close={close} initialVendor={(lpParam('pv') as Vendor | null) ?? null} />}
      {sheet?.kind === 'add-key' && (
        <AddKey
          close={close}
          prefill={lpParam('prefill') === '1'}
          onAdd={(a) => {
            setKeys([...keys, a])
            setFilter('all')
            setSheet(null)
            setFlash(a.id)
            setToast(`Key ${a.label} added`)
          }}
        />
      )}
      {sheet?.kind === 'manage' && sheetAcc && <Manage key={sheetAcc.id + sheetAcc.state} a={sheetAcc} close={close} go={setSheet} onResume={() => resume(sheetAcc.id)} />}
      {sheet?.kind === 'lend' && sheetAcc && <LendSheet a={sheetAcc} close={close} back={() => setSheet({ kind: 'manage', id: sheetAcc.id })} />}
      {sheet?.kind === 'replace' && sheetAcc && <ReplaceSheet a={sheetAcc} close={close} back={() => setSheet({ kind: 'manage', id: sheetAcc.id })} />}
      {sheet?.kind === 'delete' && sheetAcc && (
        <DeleteSheet
          a={sheetAcc}
          close={close}
          back={() => setSheet({ kind: 'manage', id: sheetAcc.id })}
          onDelete={() => {
            setKeys(keys.filter((k) => k.id !== sheetAcc.id))
            setPlans(plans.filter((k) => k.id !== sheetAcc.id))
            setSheet(null)
            setToast(`${sheetAcc.label} deleted; its secret is destroyed`)
          }}
        />
      )}
    </div>
  )
}
