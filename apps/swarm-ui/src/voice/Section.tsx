/**
 * VOICE IT, THE SECTION (docs/design/voice-it.md §10, phase one's first lane;
 * owner decision 2026-11-01): its three panes, Talk, Projects and Preferences,
 * each a page at its own path (`/voice/talk`, `/voice/projects`,
 * `/voice/preferences`), and the one rule for who is shown them.
 *
 * WHAT EACH PANE DRAWS TODAY, AND WHY NOTHING MORE. Talk is the Talk pane
 * (Talk.tsx, §3.10): its start control asks swarm-api's session routes for a
 * session, and while swarm-voice is not deployed here swarm-api refuses it and
 * the pane shows that refusal as served. Projects (Projects.tsx) and
 * Preferences (Preferences.tsx) are §3.11's panes over swarm-api's projects
 * and preferences routes: the saved projects to resume, rename or delete, and
 * the standing preferences with Undo beside the ChatGPT sign-in.
 *
 * ONLY TALK LISTENS, AND ONLY WHEN ASKED. Nothing asks for the microphone
 * until the person presses Start talking; Projects and Preferences open no
 * call and read no secret's value.
 */
import { useEffect, useState, type ReactNode } from 'react'

import { loadMe } from '../api'
import { PageHead } from '../Shell'
import { Absent } from '../primitives'
import type { Me } from '../types'
import { VoicePreferences, VoiceStandingPreferences } from './Preferences'
import { VoiceProjects } from './Projects'
import { TalkPane } from './Talk'

/**
 * WHO VOICE IT IS OPEN TO. Version one is one person's (§1: one budget, one
 * key, one history), and the person is the platform's owner. swarm-api's
 * session routes (§10, "swarm-api starts and ends a Voice it session for owner
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

/**
 * Voice it › Talk: where the person talks to the agent, with captions of both
 * sides. The Talk pane (Talk.tsx) draws its own ring, its start control and
 * its captions; until swarm-voice is deployed here, a start is refused by
 * swarm-api and the pane says so in the server's words.
 */
export function VoiceTalkScreen() {
  return (
    <>
      <PageHead title="Talk" />
      <Gate>
        <TalkPane />
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
        <VoiceProjects />
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
        <VoiceStandingPreferences />
        <VoicePreferences />
      </Gate>
    </>
  )
}
