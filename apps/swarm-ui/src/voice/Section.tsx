/**
 * VOICE IT, THE SECTION (docs/design/voice-it.md §10, phase one's first lane;
 * owner decision 2026-11-01): its three panes, Talk, Projects and Preferences,
 * each a page at its own path (`/voice/talk`, `/voice/projects`,
 * `/voice/preferences`), and the one rule for who is shown them.
 *
 * WHAT EACH PANE DRAWS TODAY, AND WHY NOTHING MORE. Talk needs swarm-api's
 * session routes, swarm-voice and the WebRTC pane (§10, phase one's later
 * lanes); Projects and Preferences are phase four's (§3.11). A button that
 * starts a session nothing answers would be a stub, so each pane says plainly
 * what it will hold and when, and offers no control that cannot work. Talk
 * draws the presence layout at rest (§11: the ring, still, with nothing said)
 * so the page reads as the one the person will talk on.
 *
 * NOTHING HERE LISTENS. No pane asks for the microphone, opens a connection
 * or reads a secret; the first lane that does is the Talk pane's (§3.10).
 */
import { useEffect, useState, type ReactNode } from 'react'

import { loadMe } from '../api'
import { PageHead } from '../Shell'
import { Absent } from '../primitives'
import type { Me } from '../types'

/**
 * WHO VOICE IT IS OPEN TO. Version one is one person's (§1: one budget, one
 * key, one history), and the person is the platform's owner. swarm-api's
 * session routes (§10, "swarm-api starts and ends a Voice it session for BGD
 * only") will hold the allowed people in its settings and refuse anyone else
 * with a 403; until they land, the console's own identity read is all it has,
 * and the one thing it can say about the owner is that they are a platform
 * admin. THE SWITCH IS THIS FUNCTION: when the session routes serve whether
 * the caller may use Voice it, this reads that answer instead, and the spine
 * (Spine.tsx) and the panes below follow without another change.
 *
 * Unread is not "no": null, so nothing is drawn and then withdrawn.
 */
export function voiceItOpen(me: Me | null): boolean | null {
  return me === null ? null : me.principal.is_admin === true
}

/** The identity read, as `voiceItOpen` answers it, for a pane opened by its address. */
function useVoiceItOpen(): boolean | null {
  const [open, setOpen] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    void loadMe().then((r) => {
      if (!live) return
      setOpen(r.status === 'ok' || r.status === 'stale' ? voiceItOpen(r.data) : false)
    })
    return () => {
      live = false
    }
  }, [])
  return open
}

/**
 * The page's body once the identity read has answered: the pane's own
 * content for the person Voice it is open to, a refusal in place for anyone
 * else (the address is kept, as Admin's pages keep theirs).
 */
function Gate({ children }: { children: ReactNode }) {
  const open = useVoiceItOpen()
  if (open === null) return null
  if (!open) {
    return (
      <Absent kind="admin" heading="Voice it is not open to you" say="Voice it is open only to the people it was set up for">
        In this version Voice it is one person's: the platform owner's.
      </Absent>
    )
  }
  return <>{children}</>
}

/** The presence ring at rest (§11), still: no session is live and nothing is said. */
function RestingRing() {
  return (
    <figure className="vi-ring is-idle" data-state="idle">
      <svg viewBox="0 0 400 400" width="160" height="160" role="img" aria-label="Voice it: no session">
        <circle cx="200" cy="200" r="120" fill="none" stroke="currentColor" strokeOpacity="0.35" strokeWidth="2" />
      </svg>
      <figcaption>Voice it</figcaption>
    </figure>
  )
}

/** Voice it › Talk: where the person talks to the agent, with captions of both sides. */
export function VoiceTalkScreen() {
  return (
    <>
      <PageHead title="Talk" />
      <Gate>
        <RestingRing />
        <Absent kind="zero" heading="No session yet" say="Talking is not switched on yet">
          Talking arrives with phase one's next lanes: the session routes, the voice service, and this
          page's microphone and captions. Nothing on this page listens or records.
        </Absent>
      </Gate>
    </>
  )
}

/** Voice it › Projects: the saved conversations, each resumable (§3.11). */
export function VoiceProjectsScreen() {
  return (
    <>
      <PageHead title="Projects" />
      <Gate>
        <Absent kind="zero" heading="Projects arrive in phase four" say="Saved projects arrive in phase four">
          Each talk will be kept here as a project you can name, reopen and pick up where it stopped.
        </Absent>
      </Gate>
    </>
  )
}

/** Voice it › Preferences: what Voice it remembers about you, each editable (§3.11). */
export function VoicePreferencesScreen() {
  return (
    <>
      <PageHead title="Preferences" />
      <Gate>
        <Absent kind="zero" heading="Preferences arrive in phase four" say="Preferences arrive in phase four">
          What Voice it remembers about how you work will be listed here, and you will be able to change or
          remove any of it.
        </Absent>
      </Gate>
    </>
  )
}
