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
 * SETTINGS AND TELEMETRY are one page with two tabs (own-speech design
 * §12.5, build plan rows U3 and U4): Settings (Settings.tsx) for everyone it
 * reads for, read only unless they are a platform admin, and Telemetry
 * (Telemetry.tsx) beside it for platform admins only, the readers swarm-api's
 * platform telemetry view has.
 * Each tab has its own path, `/voice/settings` and `/voice/telemetry`, and
 * its own menu entry (owner's pick, 2026-11-08): the route says which tab
 * opens, and the tablist still switches between them in place.
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
import { VoiceSettings } from './Settings'
import { TalkPane } from './Talk'
import { TalkTrack, trackMock } from './TrackMock' // MOCK-HOOK
import { VoiceTelemetry } from './Telemetry'

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
        {/* MOCK-HOOK */ trackMock() ? <TalkTrack /> : <TalkPane />}
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

export type VoiceSettingsTab = 'settings' | 'telemetry'

/**
 * Voice it › Settings and Telemetry: the two tabs. A platform admin edits
 * Settings and is shown Telemetry; anyone else sees Settings read only (or its
 * refusal, as swarm-api answers) and no Telemetry tab at all. Unread is not
 * "no": nothing is drawn until the identity read answers.
 */
export function VoiceSettingsSectionScreen({ tab = 'settings' }: { tab?: VoiceSettingsTab }) {
  const open = useVoiceItOpen()
  const [picked, setPicked] = useState<VoiceSettingsTab>(tab)
  // A NEW ROUTE OPENS ITS OWN TAB: going from /voice/settings to
  // /voice/telemetry keeps this component mounted, so the tab the route names
  // replaces the one picked in place, during render rather than after a paint.
  const [routed, setRouted] = useState<VoiceSettingsTab>(tab)
  if (routed !== tab) {
    setRouted(tab)
    setPicked(tab)
  }
  if (open === null) return null
  const shown: VoiceSettingsTab = open ? picked : 'settings'
  const tabs: readonly { key: VoiceSettingsTab; label: string }[] = open
    ? [
        { key: 'settings', label: 'Settings' },
        { key: 'telemetry', label: 'Telemetry' },
      ]
    : [{ key: 'settings', label: 'Settings' }]
  return (
    <>
      {tabs.length > 1 && (
        <div className="vtel-tabs" role="tablist" aria-label="Voice it settings">
          {tabs.map((t) => (
            <button key={t.key} type="button" role="tab" id={`vtab-${t.key}`} aria-selected={t.key === shown} aria-controls={`vpanel-${t.key}`} className="vtel-tab" onClick={() => setPicked(t.key)}>
              {t.label}
            </button>
          ))}
        </div>
      )}
      <div role={tabs.length > 1 ? 'tabpanel' : undefined} id={`vpanel-${shown}`} aria-labelledby={tabs.length > 1 ? `vtab-${shown}` : undefined}>
        {shown === 'telemetry' ? <VoiceTelemetry /> : <VoiceSettings canEdit={open} />}
      </div>
    </>
  )
}
