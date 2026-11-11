/**
 * VOICE IT, THE COST PILL in Talk's header row, beside End (the owner's pick
 * of 11 November 2026, "Header pill"; docs/design/voice-it.md §3.8). The
 * budget meter folded into one small button: the session's cost, the day's
 * total against its cap, and a thin double ring (outside the day, inside the
 * session) for how much of each cap is used. Pressing it opens a panel under
 * it with every ledger line, spent and reserved, for the session and for the
 * day; what the figures leave out is behind the panel's (i).
 *
 * The figures are swarm-api's ledger as served (Meter.tsx's `useLedger`, read
 * once and again every LEDGER_REFRESH_MS while the session is live); the pill
 * sums nothing. Its warning is the console's own usage forms (`usageForm`:
 * nearly full at NEARLY_FULL of the cap, over past it), so it warns where
 * every usage bar does, by colour and the triangle, with no word. A read that
 * fails says so in the pill and shows no figure; swarm-api's words are in the
 * panel.
 */

import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'

import { buttonClass, CIcon, usageForm, type UsageForm } from '../components'
import { BadGlyph, WarnGlyph } from '../components/glyphs'
import { LINE_WORDS, usd, useLedger, type Ledger, type LedgerCap } from './Meter'
import { TalkTip } from './Stage'
import '../styles/cost-pill.css'

type Tone = 'ok' | 'warn' | 'bad'

function toneOf(form: UsageForm): Tone {
  if (form === 'over') return 'bad'
  if (form === 'nearly-full' || form === 'full') return 'warn'
  return 'ok'
}

const formOf = (c: LedgerCap) => usageForm(Number(c.total_usd.toFixed(2)), c.cap_usd)

/** The worse of two tones: what the pill's frame takes. */
function worst(a: Tone, b: Tone): Tone {
  const rank: Tone[] = ['ok', 'warn', 'bad']
  return rank.indexOf(a) >= rank.indexOf(b) ? a : b
}

/** The cap's figure: whole dollars, as the cap is set. */
function capWord(n: number): string {
  return Number.isInteger(n) ? `$${n}` : usd(n)
}

function Arc({ r, share, tone }: { r: number; share: number; tone: Tone }) {
  const len = 2 * Math.PI * r
  const used = Math.max(0, Math.min(1, share))
  return (
    <>
      <circle className="vcp-arc-track" cx="12" cy="12" r={r} />
      {used > 0 && (
        <circle
          className={`vcp-arc is-${tone}`}
          cx="12"
          cy="12"
          r={r}
          strokeDasharray={`${Math.max(used * len, 1.2)} ${len}`}
          transform="rotate(-90 12 12)"
        />
      )}
    </>
  )
}

/** Outside, the day against its cap; inside, the session against its own. */
function CostRing({ ledger }: { ledger: Ledger }) {
  const s = ledger.session
  const d = ledger.day
  return (
    <svg className="vcp-ring" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <Arc r={10} share={d.total_usd / d.cap_usd} tone={toneOf(formOf(d))} />
      <Arc r={6} share={s.total_usd / s.cap_usd} tone={toneOf(formOf(s))} />
    </svg>
  )
}

function InfoMark() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 11v5.5M12 7.75v.01" />
    </svg>
  )
}

function FormGlyph({ form }: { form: UsageForm }) {
  if (form === 'over') return <span className="vcp-mark is-bad"><BadGlyph /></span>
  if (form === 'nearly-full' || form === 'full') return <span className="vcp-mark is-warn"><WarnGlyph /></span>
  return null
}

/** One cap's block: the bar, spent then reserved against the cap, and every line. */
function CapBlock({ title, meta, cap }: { title: string; meta?: string; cap: LedgerCap }) {
  const form = formOf(cap)
  const tone = toneOf(form)
  const pct = (n: number) => `${Math.max(0, Math.min(100, (n / cap.cap_usd) * 100))}%`
  return (
    <section className="vcp-block" aria-label={title}>
      <p className="vcp-block-h">
        <span className="vcp-block-t">{title}</span>
        {meta !== undefined && <span className="vcp-block-meta">{meta}</span>}
        <span className={`vcp-block-fig is-${tone}`}>
          <FormGlyph form={form} />
          <b>{usd(cap.total_usd)}</b> / {capWord(cap.cap_usd)}
        </span>
      </p>
      <div
        className={`vcp-bar is-${tone}`}
        role="img"
        aria-label={`${usd(cap.spent_usd)} spent and ${usd(cap.reserved_usd)} reserved of ${capWord(cap.cap_usd)}`}
      >
        <span className="vcp-bar-spent" style={{ width: pct(cap.spent_usd) }} />
        <span className="vcp-bar-held" style={{ width: pct(cap.reserved_usd) }} />
      </div>
      <table className={`vcp-lines is-${tone}`}>
        <thead>
          <tr>
            <th scope="col" className="vcp-sr">Line</th>
            <th scope="col">
              <span className="vcp-key is-spent" aria-hidden="true" />
              Spent
            </th>
            <th scope="col">
              <span className="vcp-key is-held" aria-hidden="true" />
              Reserved
            </th>
          </tr>
        </thead>
        <tbody>
          {cap.lines.map((l) => (
            <tr key={l.line} className={l.spent_usd === 0 && l.reserved_usd === 0 ? 'is-nil' : undefined}>
              <th scope="row">{LINE_WORDS[l.line] ?? l.line}</th>
              <td>{usd(l.spent_usd)}</td>
              <td>{l.reserved_usd > 0 ? usd(l.reserved_usd) : <span className="vcp-nil">–</span>}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row">Left</th>
            <td colSpan={2} className={cap.remaining_usd < 0 ? 'is-bad' : undefined}>
              {usd(cap.remaining_usd)}
            </td>
          </tr>
        </tfoot>
      </table>
    </section>
  )
}

/**
 * The phone's panel: both caps side by side in one table, so it is short enough to open under a pending
 * read-back and still show every line. The spent and reserved columns are named by their keys, the legend
 * at the panel's foot.
 */
function BothCaps({ ledger }: { ledger: Ledger }) {
  const caps: [title: string, cap: LedgerCap][] = [
    ['This session', ledger.session],
    ['Today', ledger.day],
  ]
  const tones = caps.map(([, c]) => toneOf(formOf(c)))
  const pct = (n: number, c: LedgerCap) => `${Math.max(0, Math.min(100, (n / c.cap_usd) * 100))}%`
  return (
    <table className="vcp-both">
      <thead>
        <tr>
          <th scope="col" className="vcp-both-0">
            <span className="vcp-sr">Line</span>
          </th>
          {caps.map(([title, c], i) => (
            <th key={title} scope="colgroup" colSpan={2} className={`vcp-both-cap is-${tones[i]}`}>
              <span className="vcp-block-t">{title}</span>
              <span className={`vcp-block-fig is-${tones[i]}`}>
                <FormGlyph form={formOf(c)} />
                <b>{usd(c.total_usd)}</b> / {capWord(c.cap_usd)}
              </span>
              <span
                className={`vcp-bar is-${tones[i]}`}
                role="img"
                aria-label={`${usd(c.spent_usd)} spent and ${usd(c.reserved_usd)} reserved of ${capWord(c.cap_usd)}`}
              >
                <span className="vcp-bar-spent" style={{ width: pct(c.spent_usd, c) }} />
                <span className="vcp-bar-held" style={{ width: pct(c.reserved_usd, c) }} />
              </span>
              {i === 1 && <span className="vcp-block-meta">{`${ledger.date} · ${ledger.time_zone}`}</span>}
            </th>
          ))}
        </tr>
        <tr className="vcp-both-keys">
          <td />
          {caps.map(([title], i) => [
            <th key={`${title}s`} scope="col" className={`is-${tones[i]}`}>
              <span className="vcp-key is-spent" aria-hidden="true" />
              <span className="vcp-sr">Spent</span>
            </th>,
            <th key={`${title}r`} scope="col" className={`is-${tones[i]}`}>
              <span className="vcp-key is-held" aria-hidden="true" />
              <span className="vcp-sr">Reserved</span>
            </th>,
          ])}
        </tr>
      </thead>
      <tbody>
        {ledger.session.lines.map((l) => {
          const dl = ledger.day.lines.find((x) => x.line === l.line)
          const nil = (x: { spent_usd: number; reserved_usd: number } | undefined) => x === undefined || (x.spent_usd === 0 && x.reserved_usd === 0)
          return (
            <tr key={l.line}>
              <th scope="row" className={nil(l) && nil(dl) ? 'is-nil' : undefined}>
                {LINE_WORDS[l.line] ?? l.line}
              </th>
              {[l, dl].map((x, i) => [
                <td key={`${i}s`} className={nil(x) ? 'is-nil' : undefined}>
                  {x === undefined ? <span className="vcp-nil">–</span> : usd(x.spent_usd)}
                </td>,
                <td key={`${i}r`} className={nil(x) ? 'is-nil' : undefined}>
                  {x !== undefined && x.reserved_usd > 0 ? usd(x.reserved_usd) : <span className="vcp-nil">–</span>}
                </td>,
              ])}
            </tr>
          )
        })}
      </tbody>
      <tfoot>
        <tr>
          <th scope="row">Left</th>
          {caps.map(([title, c]) => (
            <td key={title} colSpan={2} className={c.remaining_usd < 0 ? 'is-bad' : undefined}>
              {usd(c.remaining_usd)}
            </td>
          ))}
        </tr>
      </tfoot>
    </table>
  )
}

/** What the pill says, for a screen reader: the figures, then the worst form. */
function said(ledger: Ledger): string {
  const s = ledger.session
  const d = ledger.day
  const forms = [formOf(s), formOf(d)]
  const tone = worst(toneOf(forms[0]!), toneOf(forms[1]!))
  return (
    `Cost: this session ${usd(s.total_usd)} of ${capWord(s.cap_usd)}, today ${usd(d.total_usd)} of ${capWord(d.cap_usd)}` +
    (forms.includes('over') ? ', over a cap' : tone === 'warn' ? ', nearly at a cap' : '')
  )
}

export function CostPill({ sessionId, live }: { sessionId: string; live: boolean }) {
  const { ledger, problem } = useLedger(sessionId, live)
  const [open, setOpen] = useState(false)
  const host = useRef<HTMLDivElement | null>(null)
  const pill = useRef<HTMLButtonElement | null>(null)
  const panel = useRef<HTMLDivElement | null>(null)
  const panelId = useId()
  // A failed read shows no figure, the last one served included: the pill never stands on a stale ledger.
  const shown = problem === null ? ledger : null
  const sForm = shown === null ? null : formOf(shown.session)
  const dForm = shown === null ? null : formOf(shown.day)
  const tone: Tone = problem !== null ? 'bad' : sForm === null || dForm === null ? 'ok' : worst(toneOf(sForm), toneOf(dForm))

  // The panel closes on Escape and on a press outside it, and gives focus back to the pill.
  useEffect(() => {
    if (!open) return
    const key = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      setOpen(false)
      pill.current?.focus()
    }
    const press = (e: PointerEvent) => {
      if (host.current !== null && !host.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', key)
    document.addEventListener('pointerdown', press)
    return () => {
      document.removeEventListener('keydown', key)
      document.removeEventListener('pointerdown', press)
    }
  }, [open])

  // The panel never covers a pending read-back (its Go, Not that and clock): it moves left, off the stage's
  // side column, and where the read-back takes the whole width (a phone) it opens under it instead.
  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const el = panel.current
      if (el === null) return
      el.style.transform = ''
      const hold = document.querySelector('.vts-hold.is-pending')
      if (hold === null) return
      const p = el.getBoundingClientRect()
      const h = hold.getBoundingClientRect()
      if (p.right <= h.left || p.left >= h.right || p.bottom <= h.top || p.top >= h.bottom) return
      const dx = p.right - h.left + 12
      el.style.transform = p.left - dx >= 8 ? `translateX(${-dx}px)` : `translateY(${h.bottom - p.top + 8}px)`
    }
    place()
    const seen = new MutationObserver(place)
    seen.observe(document.body, { childList: true, subtree: true })
    window.addEventListener('resize', place)
    return () => {
      seen.disconnect()
      window.removeEventListener('resize', place)
    }
  }, [open])

  const label =
    problem !== null ? `Cost: the ledger could not be read: ${problem}` : shown === null ? 'Cost: reading the ledger' : said(shown)

  return (
    <div className={`vcp${open ? ' is-open' : ''}`} ref={host} data-tone={tone}>
      <button
        type="button"
        ref={pill}
        className="vcp-pill"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={label}
        aria-busy={problem === null && shown === null}
        disabled={problem === null && shown === null}
        onClick={() => setOpen((o) => !o)}
      >
        {shown !== null && sForm !== null && dForm !== null ? (
          <>
            <CostRing ledger={shown} />
            <span className={`vcp-fig is-${toneOf(sForm)}`}>
              <span className="vcp-lbl">Session</span>
              <b>{usd(shown.session.total_usd)}</b>
              <FormGlyph form={sForm} />
            </span>
            <span className="vcp-sep" aria-hidden="true" />
            <span className={`vcp-fig is-${toneOf(dForm)}`}>
              <span className="vcp-lbl">Today</span>
              <b>{usd(shown.day.total_usd)}</b>
              <span className="vcp-cap">/ {capWord(shown.day.cap_usd)}</span>
              <FormGlyph form={dForm} />
            </span>
          </>
        ) : (
          <span className={`vcp-fig is-${tone}`}>
            {problem !== null && <FormGlyph form="over" />}
            <span className="vcp-lbl">Cost</span>
            {problem !== null && <b>Unread</b>}
          </span>
        )}
        <CIcon name="chevron" className="c-ic vcp-chev" />
      </button>
      {open && (
        <div className="vcp-panel" id={panelId} role="region" aria-label="Cost" ref={panel}>
          <p className="vcp-panel-h">
            <span className="vcp-panel-t">Cost</span>
            <button
              type="button"
              className={`${buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} vcp-x`}
              aria-label="Close"
              onClick={() => {
                setOpen(false)
                pill.current?.focus()
              }}
            >
              <CIcon name="close" />
            </button>
          </p>
          {problem !== null && (
            <p className="vcp-problem" role="alert">
              {problem}
            </p>
          )}
          {shown !== null && (
            <>
              <div className="vcp-blocks">
                <CapBlock title="This session" cap={shown.session} />
                <CapBlock title="Today" meta={`${shown.date} · ${shown.time_zone}`} cap={shown.day} />
              </div>
              <BothCaps ledger={shown} />
              <p className="vcp-foot">
                <span className="vcp-legend" aria-hidden="true">
                  <span className="vcp-key is-spent" />
                  Spent
                  <span className="vcp-key is-held" />
                  Reserved
                </span>
                {shown.not_counted.length > 0 && <span>Not counted</span>}
                <TalkTip
                  end
                  tip={`${shown.not_counted.length > 0 ? `Not counted: ${shown.not_counted.join(', ')}. ` : ''}Reserved is running work, held at its step budgets.`}
                >
                  <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label={shown.not_counted.length > 0 ? 'What is not counted' : 'What reserved means'}>
                    <InfoMark />
                  </button>
                </TalkTip>
              </p>
            </>
          )}
        </div>
      )}
    </div>
  )
}
