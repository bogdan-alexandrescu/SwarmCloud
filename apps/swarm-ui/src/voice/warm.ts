/**
 * THE WARM REQUEST (docs/design/voice-it-own-speech.md §4.1; owner,
 * 23 November 2026). Talk, opened with no session, asks swarm-api once to
 * start the speech machine in the background, so the first spoken turn does
 * not wait out its cold start. swarm-api decides everything: whether own
 * speech is the engine, whether the person could start a session, whether
 * the machine is already up, and its per-person rate limit. The page only
 * says what is true: "Speech is starting" for the seconds swarm-api gives the
 * start (`within_seconds`), or nothing at all. A refusal says nothing here:
 * Start says its own when pressed.
 */

import { useEffect, useRef, useState } from 'react'

import { USE_FIXTURES } from '../api'
import { route, write } from '../fetch'

export const VOICE_SPEECH_WARM = '/v1/voice/speech/warm'

/** `POST /v1/voice/speech/warm`: the seconds the machine's start may take when it was started, else null. Never throws. */
export async function warmSpeech(): Promise<number | null> {
  try {
    const r = await write(route(VOICE_SPEECH_WARM), 'POST', {})
    if (r.status !== 'ok') return null
    const d = r.data as { warming?: unknown; within_seconds?: unknown } | null
    if (d === null || d.warming !== true) return null
    const s = d.within_seconds
    return typeof s === 'number' && Number.isFinite(s) && s > 0 ? Math.min(s, 600) : null
  } catch {
    return null
  }
}

/**
 * Asks once, when the page opens and `open` (no session held) is true then;
 * true while the machine's start may still be running. A later change of
 * `open` (a session ended) asks nothing: End stopped the machine on purpose.
 */
export function useSpeechWarm(open: boolean): boolean {
  const [waitMs, setWaitMs] = useState<number | null>(null)
  const asked = useRef(false)
  const openAtMount = useRef(open)
  useEffect(() => {
    if (asked.current || !openAtMount.current || USE_FIXTURES) return
    asked.current = true
    void warmSpeech().then((seconds) => {
      if (seconds !== null) setWaitMs(seconds * 1000)
    })
  }, [])
  useEffect(() => {
    if (waitMs === null) return
    const t = window.setTimeout(() => setWaitMs(null), waitMs)
    return () => window.clearTimeout(t)
  }, [waitMs])
  return waitMs !== null
}
