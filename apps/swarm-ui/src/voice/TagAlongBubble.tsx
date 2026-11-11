// MOCK-HOOK FILE
/**
 * VOICE IT FOLLOWS YOU, THE CORNER BUBBLE (mock-up, fixture data only;
 * tagAlong.ts). At rest a round bubble in a corner: the voice inside, the mic
 * on its edge, a count while items wait for review. A progress line slides
 * out beside it and folds back; an offer or a new item waits there with its
 * buttons. Pressing the bubble opens its panel; dragging it moves it to the
 * nearest corner. Everything the agent makes stays on Talk: the bubble only
 * says that something waits, and takes the person there.
 */

import { useEffect, useId, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'

import { Button, buttonClass } from '../components'
import { TalkTip } from './Stage'
import { goTo, tagAlong, useTagAlong, waiting, type Corner, type TagAlong } from './tagAlong'
import '../styles/tagalong-bubble.css'

const C = 50
const POINTS = 72

/** A closed path around the centre: the Ring's radial trace, drawn from a synthetic voice. */
function radial(t: number, base: number, amp: number): string {
  let d = ''
  for (let i = 0; i < POINTS; i++) {
    const a = (i / POINTS) * Math.PI * 2 - Math.PI / 2
    const env = 0.55 + 0.45 * Math.sin(t * 3.1) * Math.sin(t * 1.7 + 1)
    const v = Math.abs(Math.sin(a * 5 + t * 6) * 0.6 + Math.sin(a * 9 - t * 9) * 0.3 + Math.sin(a * 3 + t * 2) * 0.25) * env
    const r = base + amp * Math.min(1, v)
    d += `${i === 0 ? 'M' : 'L'}${(C + r * Math.cos(a)).toFixed(1)},${(C + r * Math.sin(a)).toFixed(1)} `
  }
  return `${d}Z`
}

function still(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

/** The voice, small: the Ring's base circle, its halo and its trace while the agent speaks. */
export function Voice({ speaking, muted }: { speaking: boolean; muted: boolean }) {
  const gid = useId().replace(/:/g, '')
  const wave = useRef<SVGPathElement | null>(null)
  const glow = useRef<SVGPathElement | null>(null)
  useEffect(() => {
    if (!speaking || still()) {
      wave.current?.setAttribute('d', '')
      glow.current?.setAttribute('d', '')
      return
    }
    let frame = 0
    const t0 = performance.now()
    const draw = (now: number) => {
      const d = radial((now - t0) / 1000, 27, 17)
      wave.current?.setAttribute('d', d)
      glow.current?.setAttribute('d', d)
      frame = requestAnimationFrame(draw)
    }
    frame = requestAnimationFrame(draw)
    return () => cancelAnimationFrame(frame)
  }, [speaking])
  const state = muted ? 'muted' : speaking ? 'speaking' : 'listening'
  return (
    <svg className={`vtb-voice is-${state}`} viewBox="0 0 100 100" aria-hidden="true">
      <defs>
        <radialGradient id={`h${gid}`} cx="0.5" cy="0.5" r="0.5">
          <stop offset="0.4" stopColor="currentColor" stopOpacity="0" />
          <stop offset="0.62" stopColor="currentColor" stopOpacity="0.22" />
          <stop offset="1" stopColor="currentColor" stopOpacity="0" />
        </radialGradient>
        <filter id={`g${gid}`} x="-30%" y="-30%" width="160%" height="160%">
          <feGaussianBlur stdDeviation="2.4" />
        </filter>
      </defs>
      <circle className="vtb-halo" cx={C} cy={C} r={48} fill={`url(#h${gid})`} />
      <circle className="vtb-base" cx={C} cy={C} r={30} />
      <circle className="vtb-still" cx={C} cy={C} r={30} />
      <path ref={glow} className="vtb-glow" d="" filter={`url(#g${gid})`} />
      <path ref={wave} className="vtb-wave" d="" />
    </svg>
  )
}

function MicGlyph({ off }: { off: boolean }) {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <rect x="9" y="3" width="6" height="11" rx="3" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      {off && <path d="M4 4l16 16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />}
    </svg>
  )
}

function CloseGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

function InfoGlyph() {
  return (
    <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false" width="14" height="14">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="1.8" />
      <path d="M12 11v6M12 7.5v.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  )
}

const stateWord = (s: TagAlong): string => (s.muted ? 'Muted' : s.speaking ? 'Speaking' : 'Listening')

/** The current offer: what was asked, and Yes / Not now. Yes may also be said aloud. */
function OfferBlock({ s, compact = false }: { s: TagAlong; compact?: boolean }) {
  const o = s.offer
  if (o === null) return null
  return (
    <div className={`vtb-offer${compact ? ' is-compact' : ''}`} role="group" aria-label={`Open ${o.page}?`}>
      {!compact && (
        <p className="vtb-say">
          <span className="vtb-say-who">You</span> “{o.asked}”
        </p>
      )}
      <p className="vtb-offer-q">
        <span>
          Open <b>{o.page}</b>?
        </span>
        <TalkTip tip="Say yes, or press Yes. The talk carries on there." end>
          <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="About this offer">
            <InfoGlyph />
          </button>
        </TalkTip>
      </p>
      <span className="vtb-acts">
        <Button kind="primary" size="sm" onClick={() => tagAlong.accept()}>
          Yes
        </Button>
        <Button kind="secondary" size="sm" onClick={() => tagAlong.decline()}>
          Not now
        </Button>
      </span>
    </div>
  )
}

/** What slides out beside the bubble: the offer, a new item, the agent's sentence, one progress line, or that the mic is off. */
function Beside({ s }: { s: TagAlong }) {
  const fresh = s.review.find((r) => r.id === s.notice && !r.seen) ?? null
  if (s.open) return null
  if (s.offer !== null) {
    return (
      <div className="vtb-card is-offer" role="dialog" aria-label="Voice it asks">
        <OfferBlock s={s} />
      </div>
    )
  }
  if (fresh !== null) {
    return (
      <div className="vtb-card is-review" role="status">
        <span className="vtb-card-m">
          <span className="vtb-card-h">
            <span className="vtb-kind">{fresh.kind}</span> to review
          </span>
          <span className="vtb-card-t">{fresh.title}</span>
        </span>
        <span className="vtb-acts">
          <Button kind="primary" size="sm" onClick={() => tagAlong.review(fresh.id)}>
            Review in Voice it
          </Button>
        </span>
      </div>
    )
  }
  if (s.speaking && s.saying !== null) {
    return (
      <p className="vtb-pill is-saying" role="status">
        <span className="vtb-pill-who">Voice it</span>
        <span className="vtb-pill-t">{s.saying}</span>
      </p>
    )
  }
  if (s.peek !== null) {
    return (
      <p className="vtb-pill" role="status">
        <span className="vtb-pill-who">{s.peek.who}</span>
        <span className="vtb-pill-t">{s.peek.text}</span>
      </p>
    )
  }
  if (s.muted) {
    return (
      <p className="vtb-pill is-muted" role="status">
        <span className="vtb-pill-who">Muted</span>
        <Button kind="secondary" size="sm" icon={<MicGlyph off={false} />} onClick={() => tagAlong.setMuted(false)}>
          Unmute
        </Button>
      </p>
    )
  }
  return null
}

/** The panel above the bubble, opened by pressing it. */
function Panel({ s }: { s: TagAlong }) {
  const n = waiting(s)
  return (
    <section className="vtb-panel" aria-label="Voice it">
      <header className="vtb-ph">
        <span className="vtb-ph-t">
          <b>Voice it</b>
          <span className="vtb-ph-p">{s.project}</span>
        </span>
        <span className={`vtb-state is-${s.muted ? 'muted' : s.speaking ? 'speaking' : 'listening'}`}>{stateWord(s)}</span>
        <button type="button" className={buttonClass({ kind: 'ghost', size: 'sm', iconOnly: true })} aria-label="Close" onClick={() => tagAlong.setOpen(false)}>
          <CloseGlyph />
        </button>
      </header>
      {s.offer !== null && (
        <div className="vtb-sec">
          <OfferBlock s={s} compact />
        </div>
      )}
      <div className="vtb-sec">
        <p className="vtb-sec-h">
          To review{n > 0 && <span className="vtb-count-in">{n} new</span>}
        </p>
        <ul className="vtb-list">
          {s.review.map((r) => (
            <li key={r.id} className={r.seen ? 'vtb-item' : 'vtb-item is-new'}>
              <span className="vtb-item-m">
                <span className="vtb-item-t">{r.title}</span>
                <span className="vtb-item-s">
                  {r.kind} · {r.at}
                </span>
              </span>
              <Button kind={r.seen ? 'secondary' : 'primary'} size="sm" onClick={() => tagAlong.review(r.id)} aria-label={`Open ${r.title} in Voice it`}>
                Open
              </Button>
            </li>
          ))}
        </ul>
      </div>
      <div className="vtb-sec">
        <p className="vtb-sec-h">Progress</p>
        <ol className="vtb-prog">
          {s.progress.slice(0, 4).map((p) => (
            <li key={p.id}>
              <time>{p.at}</time>
              <span>
                <b>{p.who}</b> {p.text}
              </span>
            </li>
          ))}
        </ol>
      </div>
      <footer className="vtb-pf">
        <Button kind="secondary" size="sm" aria-pressed={s.muted} icon={<MicGlyph off={s.muted} />} onClick={() => tagAlong.setMuted(!s.muted)}>
          {s.muted ? 'Unmute' : 'Mute'}
        </Button>
        <Button kind="secondary" size="sm" onClick={() => goTo('voice/talk')}>
          Open Voice it
        </Button>
        <Button kind="secondary" size="sm" onClick={() => tagAlong.end()}>
          End
        </Button>
      </footer>
    </section>
  )
}

const CORNERS: Corner[] = ['br', 'bl', 'tr', 'tl']

/** The bubble, on every page but Talk, while the fixture session is live. */
export function TagAlongBubble() {
  const s = useTagAlong()
  const [drag, setDrag] = useState<{ x: number; y: number } | null>(null)
  const start = useRef<{ x: number; y: number; moved: boolean } | null>(null)
  const row = useRef<HTMLDivElement | null>(null)
  const peekKey = s.peek?.id
  // A progress line folds back on its own after a while; so does a new item's card (the count and the marker stay).
  useEffect(() => {
    if (peekKey === undefined || new URLSearchParams(window.location.search).has('vtscene')) return
    const t = setTimeout(() => tagAlong.foldPeek(), 7000)
    return () => clearTimeout(t)
  }, [peekKey])
  useEffect(() => {
    if (s.notice === null || new URLSearchParams(window.location.search).has('vtscene')) return
    const t = setTimeout(() => tagAlong.foldNotice(), 15000)
    return () => clearTimeout(t)
  }, [s.notice])
  const shown = s.on && !s.ended && !window.location.pathname.startsWith('/voice/talk')
  // THE PAGE ENDS WHERE THE BUBBLE BEGINS. The bubble, and whatever slides out
  // beside it, sit in a band the page's scroller gives up (`--vtb-band`), so
  // they never cover a row, an action or a Needs you strip. Inside the
  // scroller `--dock-h` grows by the band too, so the columns and drawers that
  // end above the dock end above the bubble. Measured, because a card is taller
  // than the bubble and a phone stacks it above.
  useEffect(() => {
    const el = row.current
    const main = el?.closest('.sk-main')
    const scroller = main?.querySelector<HTMLElement>(':scope > .ctl-scroll')
    if (!shown || el === null || main == null || scroller == null) return
    const root = document.documentElement
    const publish = () => {
      const r = el.getBoundingClientRect()
      const m = main.getBoundingClientRect()
      const dock = parseFloat(root.style.getPropertyValue('--dock-h')) || 28
      const top = s.corner === 'tr' || s.corner === 'tl'
      const band = Math.ceil(top ? r.bottom - m.top + 12 : m.bottom - dock - r.top + 12)
      // Set only on a change: the root's style is observed, and an equal write is still a mutation.
      const v = `${Math.max(0, band)}px`
      if (root.style.getPropertyValue('--vtb-band') !== v) root.style.setProperty('--vtb-band', v)
      if (top) scroller.style.removeProperty('--dock-h')
      else scroller.style.setProperty('--dock-h', `${dock + Math.max(0, band)}px`)
    }
    publish()
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(publish) : null
    ro?.observe(el)
    const mo = new MutationObserver(publish)
    mo.observe(root, { attributes: true, attributeFilter: ['style'] })
    window.addEventListener('resize', publish)
    return () => {
      ro?.disconnect()
      mo.disconnect()
      window.removeEventListener('resize', publish)
      root.style.removeProperty('--vtb-band')
      scroller.style.removeProperty('--dock-h')
    }
  }, [shown, s.corner])
  if (!shown) return null
  const n = waiting(s)

  const down = (e: ReactPointerEvent<HTMLButtonElement>) => {
    start.current = { x: e.clientX, y: e.clientY, moved: false }
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const move = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const st = start.current
    if (st === null) return
    if (!st.moved && Math.hypot(e.clientX - st.x, e.clientY - st.y) < 6) return
    st.moved = true
    setDrag({ x: e.clientX, y: e.clientY })
  }
  const up = (e: ReactPointerEvent<HTMLButtonElement>) => {
    const st = start.current
    start.current = null
    if (st === null) return
    if (!st.moved) {
      tagAlong.setOpen(!s.open)
      return
    }
    const right = e.clientX > window.innerWidth / 2
    const bottom = e.clientY > window.innerHeight / 2
    tagAlong.setCorner(`${bottom ? 'b' : 't'}${right ? 'r' : 'l'}` as Corner)
    setDrag(null)
  }

  return (
    <div className={`vtb is-${s.corner}${drag !== null ? ' is-dragging' : ''}${s.open ? ' is-open' : ''}`} data-corners={CORNERS.join(' ')}>
      {s.open && <Panel s={s} />}
      <div className="vtb-row" ref={row}>
        <Beside s={s} />
        <div className="vtb-orb" style={drag !== null ? { position: 'fixed', left: drag.x - 32, top: drag.y - 32 } : undefined}>
          <button
            type="button"
            className={`vtb-core is-${s.muted ? 'muted' : s.speaking ? 'speaking' : 'listening'}`}
            aria-expanded={s.open}
            aria-label={`Voice it, ${stateWord(s).toLowerCase()}${n > 0 ? `, ${n} to review` : ''}`}
            title="Voice it: press to open, drag to move"
            onPointerDown={down}
            onPointerMove={move}
            onPointerUp={up}
            onPointerCancel={() => {
              start.current = null
              setDrag(null)
            }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') tagAlong.setOpen(false)
            }}
          >
            <Voice speaking={s.speaking && !s.muted} muted={s.muted} />
          </button>
          <button
            type="button"
            className={`vtb-mic${s.muted ? ' is-off' : ''}`}
            aria-pressed={s.muted}
            aria-label={s.muted ? 'Unmute' : 'Mute'}
            title={s.muted ? 'Unmute' : 'Mute'}
            onClick={() => tagAlong.setMuted(!s.muted)}
          >
            <MicGlyph off={s.muted} />
          </button>
          {n > 0 && (
            <span className="vtb-count" aria-hidden="true">
              {n}
            </span>
          )}
        </div>
      </div>
    </div>
  )
}

/** The spine's Voice it marker while something waits for review, its reason in its tooltip. */
export function TagAlongMark() {
  const s = useTagAlong()
  const mark = useRef<HTMLElement | null>(null)
  const [at, setAt] = useState<{ x: number; y: number } | null>(null)
  const n = waiting(s)
  // The spine clips what overflows it, so the tip is placed on the page from the item's own box.
  useEffect(() => {
    const item = mark.current?.closest('.sk-ri')
    if (!(item instanceof HTMLElement)) return
    const show = () => {
      const r = item.getBoundingClientRect()
      setAt({ x: r.right + 10, y: r.top + r.height / 2 })
    }
    const hide = () => setAt(null)
    item.addEventListener('mouseenter', show)
    item.addEventListener('mouseleave', hide)
    item.addEventListener('focus', show)
    item.addEventListener('blur', hide)
    return () => {
      item.removeEventListener('mouseenter', show)
      item.removeEventListener('mouseleave', hide)
      item.removeEventListener('focus', show)
      item.removeEventListener('blur', hide)
    }
  }, [n])
  if (!s.on || s.ended || n === 0) return null
  const fresh = s.review.find((r) => !r.seen)
  const why = `${n} new to review: ${fresh?.kind ?? 'item'}, ${fresh?.title ?? ''}`
  return (
    <>
      <i ref={mark} className="sk-badge vtb-mark" aria-label={why}>
        {n}
      </i>
      {at !== null && (
        <span className="vtb-mark-tip" role="tooltip" style={{ left: at.x, top: at.y }}>
          <b>{fresh?.kind} to review</b>
          <span>{fresh?.title}</span>
        </span>
      )}
    </>
  )
}

/** Talk's own copy of the current offer, under the captions. */
export function TalkOffer() {
  const s = useTagAlong()
  if (s.offer === null) return null
  return (
    <div className="vtb-talk-offer">
      <OfferBlock s={s} compact />
    </div>
  )
}
