/**
 * VOICE IT'S MIC: open mic with barge-in, mute, and push-to-talk
 * (docs/design/voice-it.md §3.10).
 *
 * All three are the mic track's `enabled` and nothing else. Mute keeps the
 * session (it is still billed per second); push-to-talk enables the track only
 * while Space or the Hold to talk button is held, which works whatever turn
 * mode GPT-Live offers (§9: a manual turn mode is not verified), because a
 * disabled track sends silence and silence is not a turn.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

export type MicMode = 'open' | 'ptt'

export interface MicState {
  muted: boolean
  mode: MicMode
  /** Push-to-talk is held right now. */
  held: boolean
}

export const MIC_START: MicState = { muted: false, mode: 'open', held: false }

/** Whether the mic track sends: unmuted, and on open mic or while held. */
export function micEnabled(s: MicState): boolean {
  if (s.muted) return false
  return s.mode === 'open' || s.held
}

/** Words for where the mic stands, under the controls. */
export function micWord(s: MicState): string {
  if (s.muted) return 'Muted: Voice it cannot hear you'
  if (s.mode === 'ptt') return s.held ? 'Listening while you hold' : 'Hold Space or the button to talk'
  return 'Listening; talk over Voice it to interrupt'
}

/** A key pressed in a field that takes text is typing, never a control. */
export function typingIn(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  if (target.isContentEditable) return true
  const tag = target.tagName
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag !== 'INPUT') return false
  const type = (target as HTMLInputElement).type
  return !['button', 'checkbox', 'radio', 'range', 'reset', 'submit', 'color', 'file', 'image'].includes(type)
}

/**
 * The mic's state, held on `track` while `live`. Keyboard, while live: hold
 * Space to talk on push-to-talk, M to mute. A blur of the window lets go of a
 * hold, so a key released in another window does not leave the mic open.
 */
export function useMic(track: MediaStreamTrack | null, live: boolean) {
  const [mic, setMic] = useState<MicState>(MIC_START)
  const ref = useRef(mic)
  ref.current = mic

  const apply = useCallback(
    (next: MicState) => {
      ref.current = next
      if (track !== null) track.enabled = micEnabled(next)
      setMic(next)
    },
    [track],
  )
  const update = useCallback((patch: Partial<MicState>) => apply({ ...ref.current, ...patch }), [apply])

  // A new call starts from the person's choices, with nothing held.
  useEffect(() => {
    if (track !== null) track.enabled = micEnabled({ ...ref.current, held: false })
    setMic((m) => ({ ...m, held: false }))
  }, [track])

  useEffect(() => {
    if (!live) return
    const down = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || typingIn(e.target)) return
      if (e.code === 'Space' || e.key === ' ') {
        if (ref.current.mode !== 'ptt') return
        e.preventDefault()
        if (!ref.current.held) update({ held: true })
      } else if ((e.key === 'm' || e.key === 'M') && !e.repeat) {
        e.preventDefault()
        update({ muted: !ref.current.muted })
      }
    }
    const up = (e: KeyboardEvent) => {
      if ((e.code === 'Space' || e.key === ' ') && ref.current.held) update({ held: false })
    }
    const letGo = () => {
      if (ref.current.held) update({ held: false })
    }
    window.addEventListener('keydown', down)
    window.addEventListener('keyup', up)
    window.addEventListener('blur', letGo)
    return () => {
      window.removeEventListener('keydown', down)
      window.removeEventListener('keyup', up)
      window.removeEventListener('blur', letGo)
    }
  }, [live, update])

  return {
    mic,
    setMuted: (muted: boolean) => update({ muted }),
    setMode: (mode: MicMode) => update({ mode, held: false }),
    hold: (held: boolean) => {
      if (ref.current.mode === 'ptt' && ref.current.held !== held) update({ held })
    },
  }
}
