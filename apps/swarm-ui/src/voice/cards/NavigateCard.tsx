/**
 * THE NAVIGATE CARD: a console page the planner offers after an answer
 * (docs/design/voice-it.md §3.12.3).
 *
 * NO ADDRESS COMES FROM THE CARD. `destination` is a key of paths.ts's
 * `FIXED` (swarm_api/voice/cards.py `NAVIGATE_DESTINATIONS`, held to it by
 * test_voice_navigate.py) and `object` one id the console builds its own page
 * for; a card that maps to neither is not drawn.
 *
 * ONLY A PERSON'S CLICK OPENS IT. Open posts the accept route on the person's
 * cookie and navigates once, when that answers; No dismisses the offer here
 * and draws no Open again. A card that was not an open offer when it was
 * first drawn (accepted or dismissed earlier, or replayed on a reload) is
 * history: it is drawn with its state and never navigates.
 */

import { createContext, useContext, useRef, useState } from 'react'

import { Button } from '../../components'
import { errorHeading, route, write } from '../../fetch'
import { addressToPath, FIXED } from '../../paths'
import type { CardProps } from '../CardList'
import { obj, str, VoiceCardFrame } from './WorkCards'

export const VOICE_CARD_ACCEPT = '/v1/voice/sessions/{session_id}/cards/{card_id}/accept'

/** The console's navigation when the shell hands none: a path pushed as the console's own links push one. */
export function pushPath(to: string): void {
  window.history.pushState(null, '', to)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** The console's `go`, handed down by the card list. */
export const NavigateTo = createContext<(to: string) => void>(pushPath)

const ID = /^[A-Za-z0-9][A-Za-z0-9_:.-]{0,127}$/

/** An object's own console page, built by the console from its kind and id; null for a kind it has none for. */
export function objectPath(kind: string, id: string): string | null {
  if (!ID.test(id)) return null
  const q = (name: string) => new URLSearchParams({ [name]: id }).toString()
  if (kind === 'agent') return addressToPath(`work/task/${encodeURIComponent(id)}`)
  if (kind === 'workflow') return addressToPath(`work/workflows?${q('wf')}`)
  if (kind === 'run') return addressToPath(`work/runs?${q('run')}`)
  if (kind === 'schedule') return addressToPath(`automate/schedules?${q('schedule')}`)
  if (kind === 'approval') return addressToPath(`automate/approvals?${q('approval')}`)
  return null
}

/** The page an offer opens: its object's own page, else its destination's; null when it names neither. */
export function offerPath(body: Readonly<Record<string, unknown>>): string | null {
  if (body.object !== null && body.object !== undefined) {
    const o = obj(body.object)
    const kind = str(o.kind)
    const id = str(o.id)
    return kind === null || id === null ? null : objectPath(kind, id)
  }
  const dest = str(body.destination)
  return dest !== null && Object.prototype.hasOwnProperty.call(FIXED, dest) ? FIXED[dest] ?? null : null
}

export function NavigateCard({ card, spec, body }: CardProps) {
  const go = useContext(NavigateTo)
  // History is decided once, when the card is first drawn: only an open offer is ever answered here.
  const [history] = useState(() => card.state !== 'proposed')
  const [answer, setAnswer] = useState<'open' | 'busy' | 'opened' | 'no'>('open')
  const [refused, setRefused] = useState<string | null>(null)
  const navigated = useRef(false)
  const to = offerPath(body)
  if (to === null) return null
  const sessionId = card.session_id
  const offered = !history && card.state === 'proposed' && answer !== 'no' && answer !== 'opened' && sessionId !== null

  const open = async () => {
    if (sessionId === null || navigated.current) return
    setAnswer('busy')
    setRefused(null)
    const r = await write(route(VOICE_CARD_ACCEPT, { session_id: sessionId, card_id: card.card_id }), 'POST')
    if (r.status !== 'ok' && r.status !== 'stale') {
      setAnswer('open')
      setRefused(r.status === 'error' ? errorHeading(r.error) : 'Not opened')
      return
    }
    setAnswer('opened')
    if (navigated.current) return
    navigated.current = true
    go(to)
  }

  const shown = answer === 'no' ? { ...card, state: 'dismissed' as const } : card
  return (
    <VoiceCardFrame
      card={shown}
      spec={spec}
      actions={
        offered && (
          <>
            <Button size="sm" kind="primary" disabled={answer === 'busy'} onClick={() => void open()}>
              Open
            </Button>{' '}
            <Button size="sm" kind="secondary" disabled={answer === 'busy'} onClick={() => setAnswer('no')}>
              No
            </Button>
          </>
        )
      }
    >
      {str(body.why) !== null && <p className="vt-card-summary">{str(body.why)}</p>}
      {refused !== null && (
        <p className="c-hint" role="alert">
          {refused}
        </p>
      )}
    </VoiceCardFrame>
  )
}
