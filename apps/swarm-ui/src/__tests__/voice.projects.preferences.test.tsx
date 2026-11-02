// VOICE IT › PROJECTS AND › PREFERENCES (docs/design/voice-it.md §3.11, §10
// phase four's panes row), over swarm-api's merged routes:
//
//   * Projects lists the person's projects newest first, with each one's
//     title and when it last changed, and its last session and open card
//     count when swarm-api serves them;
//   * Resume opens Talk with the project handed over;
//   * Rename is inline and sends only the title;
//   * Delete stays disabled until the project's title is typed exactly, and
//     sends that title as the route's `confirm`; a refusal is said in place;
//   * Preferences draws every standing preference, edits one at a time with
//     the revision it read, and Undo restores the previous value -- including
//     a change made by voice, which the pane reads like any other.
//
// MUTATIONS: enable Delete before the title matches; drop `confirm` from the
// DELETE body; Resume without handing over the project; Undo without the
// revision; draw the edited value from the request instead of the answer.

import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const fetchMock = vi.hoisted(() => ({ read: vi.fn(), write: vi.fn() }))

vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: fetchMock.read,
  write: fetchMock.write,
}))

const apiMock = vi.hoisted(() => ({ loadMe: vi.fn() }))

vi.mock('../api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api')>()),
  loadMe: apiMock.loadMe,
}))

import { VoiceProjectsScreen } from '../voice/Section'
import { RESUME_KEY, TALK_PATH, VOICE_PROJECT, VOICE_PROJECTS, VoiceProjects, takeResumedProject } from '../voice/Projects'
import { VOICE_CHATGPT, VOICE_PREFERENCES, VOICE_PREFERENCES_UNDO, VoiceStandingPreferences } from '../voice/Preferences'

function ok(data: unknown) {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function refused(status: number, kind: string, message: string) {
  return { status: 'error', error: { kind, status, message } }
}

async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 6; i += 1) await Promise.resolve()
  })
}

function project(id: string, title: string, updated: string, extra: Record<string, unknown> = {}) {
  return {
    project_id: id,
    title,
    status: 'active',
    subject: null,
    size: null,
    brief: {},
    references: {},
    tag: `voice:${id}`,
    created_at: '2026-10-30T09:00:00+00:00',
    updated_at: updated,
    ...extra,
  }
}

const OLD = project('vp_0000000000000000000000000000000a', 'Billing export', '2026-10-31T10:00:00+00:00')
const NEW = project('vp_0000000000000000000000000000000b', 'Postgres migration plan', '2026-11-01T12:00:00+00:00', {
  last_session_at: '2026-11-01T11:30:00+00:00',
  open_cards: 3,
})

function serveProjects(rows: unknown[]): void {
  fetchMock.read.mockImplementation(async (target: { url: string }) => {
    if (target.url === VOICE_PROJECTS) return ok({ projects: rows })
    throw new Error(`unexpected read ${target.url}`)
  })
}

function rowOf(title: string): HTMLElement {
  const row = screen.getByText(title).closest('tr')
  if (row === null) throw new Error(`no row for ${title}`)
  return row
}

describe('Voice it › Projects', () => {
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
    apiMock.loadMe.mockReset()
    window.sessionStorage.clear()
    window.history.replaceState(null, '', '/')
  })

  it('lists the projects newest first, with last session and open cards when served', async () => {
    // Served oldest first on purpose: the pane orders by when each last changed.
    serveProjects([OLD, NEW])
    render(<VoiceProjects />)
    await settle()

    const rows = screen.getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).queryByText(/Billing export|Postgres migration plan/)?.textContent)).toEqual([
      'Postgres migration plan',
      'Billing export',
    ])
    expect(within(rowOf('Postgres migration plan')).getByText('3')).toBeTruthy()
    expect(within(rowOf('Postgres migration plan')).getAllByText(/Nov 1/)).toHaveLength(2)
    // A project with no session or card count served draws a dash, never a 0.
    expect(within(rowOf('Billing export')).queryByText('0')).toBeNull()
    expect(screen.getByRole('columnheader', { name: 'Open cards' })).toBeTruthy()
    expect(screen.getByRole('columnheader', { name: 'Last session' })).toBeTruthy()
  })

  it('says so when there are no projects yet', async () => {
    serveProjects([])
    render(<VoiceProjects />)
    await settle()
    expect(screen.getByText('No projects yet')).toBeTruthy()
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('Resume navigates to Talk with the project', async () => {
    serveProjects([NEW])
    const popped = vi.fn()
    window.addEventListener('popstate', popped)
    render(<VoiceProjects />)
    await settle()

    fireEvent.click(within(rowOf('Postgres migration plan')).getByRole('button', { name: 'Resume' }))

    expect(window.location.pathname).toBe(TALK_PATH)
    expect(popped).toHaveBeenCalled()
    expect(window.sessionStorage.getItem(RESUME_KEY)).toBe(NEW.project_id)
    // Talk takes it once: a reload of Talk later starts a new project.
    expect(takeResumedProject()).toBe(NEW.project_id)
    expect(takeResumedProject()).toBeNull()
    window.removeEventListener('popstate', popped)
  })

  it('renames inline, sending only the title, and draws the answer', async () => {
    serveProjects([OLD, NEW])
    fetchMock.write.mockResolvedValue(ok({ ...OLD, title: 'Billing export v2', updated_at: '2026-11-02T08:00:00+00:00' }))
    render(<VoiceProjects />)
    await settle()

    fireEvent.click(within(rowOf('Billing export')).getByRole('button', { name: 'Rename' }))
    const field = screen.getByLabelText('New title for Billing export')
    fireEvent.change(field, { target: { value: '  Billing export v2 ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save title' }))
    await settle()

    expect(fetchMock.write).toHaveBeenCalledTimes(1)
    const [target, method, body] = fetchMock.write.mock.calls[0]!
    expect((target as { url: string }).url).toBe(VOICE_PROJECT.replace('{id}', OLD.project_id))
    expect(method).toBe('PATCH')
    expect(body).toEqual({ title: 'Billing export v2' })
    // Renamed is changed: it moves to the top.
    const rows = screen.getAllByRole('row').slice(1)
    expect(within(rows[0]!).getByText('Billing export v2')).toBeTruthy()
  })

  it('keeps Delete disabled until the typed title matches, then sends it as confirm', async () => {
    serveProjects([OLD, NEW])
    fetchMock.write.mockResolvedValue(ok({ project_id: OLD.project_id, deleted: true, documents: 4, objects: 2 }))
    render(<VoiceProjects />)
    await settle()

    fireEvent.click(within(rowOf('Billing export')).getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog')
    const confirm = within(dialog).getByRole('button', { name: 'Delete the project' }) as HTMLButtonElement
    const field = within(dialog).getByLabelText(/to confirm/)

    expect(confirm.disabled).toBe(true)
    fireEvent.change(field, { target: { value: 'Billing expor' } })
    expect(confirm.disabled).toBe(true)
    fireEvent.click(confirm)
    expect(fetchMock.write).not.toHaveBeenCalled()

    fireEvent.change(field, { target: { value: 'Billing export' } })
    expect(confirm.disabled).toBe(false)
    fireEvent.click(confirm)
    await settle()

    const [target, method, body] = fetchMock.write.mock.calls[0]!
    expect((target as { url: string }).url).toBe(VOICE_PROJECT.replace('{id}', OLD.project_id))
    expect(method).toBe('DELETE')
    expect(body).toEqual({ confirm: 'Billing export' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.queryByText('Billing export')).toBeNull()
    expect(screen.getByText('Postgres migration plan')).toBeTruthy()
  })

  it("says a refused delete in swarm-api's words and keeps the project", async () => {
    serveProjects([NEW])
    const live = 'This project has a live Voice it session; end it before deleting the project.'
    fetchMock.write.mockResolvedValue(refused(409, 'conflict', live))
    render(<VoiceProjects />)
    await settle()

    fireEvent.click(within(rowOf('Postgres migration plan')).getByRole('button', { name: 'Delete' }))
    const dialog = screen.getByRole('dialog')
    fireEvent.change(within(dialog).getByLabelText(/to confirm/), { target: { value: 'Postgres migration plan' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete the project' }))
    await settle()

    expect(within(screen.getByRole('dialog')).getByText(live)).toBeTruthy()
    expect(screen.getAllByText('Postgres migration plan').length).toBeGreaterThan(0)
  })

  it('is what the Projects screen draws for the person Voice it is open to', async () => {
    apiMock.loadMe.mockResolvedValue(ok({ principal: { is_admin: true } }))
    serveProjects([NEW])
    render(<VoiceProjectsScreen />)
    await settle()

    expect(screen.getByText('Postgres migration plan')).toBeTruthy()
    expect(screen.queryByText('Projects arrive in phase four')).toBeNull()
  })
})

const DEFAULTS = {
  voice: 'marin',
  mic_mode: 'push_to_talk',
  brevity: 'normal',
  stacks: [],
  hosting: [],
  repositories: [],
  options: 3,
  grace_seconds: 30,
  reservations: {},
}

function prefsView(preferences: Record<string, unknown>, revision: number, previous: Record<string, unknown> | null) {
  return { preferences, revision, can_undo: previous !== null, previous }
}

function servePreferences(data: unknown): void {
  fetchMock.read.mockImplementation(async (target: { url: string }) => {
    if (target.url === VOICE_PREFERENCES) return ok(data)
    if (target.url === VOICE_CHATGPT) return ok({ stored: false, stored_at: null })
    throw new Error(`unexpected read ${target.url}`)
  })
}

function prefRow(label: string): HTMLElement {
  const row = screen.getByText(label, { selector: 'td > b' }).closest('tr')
  if (row === null) throw new Error(`no row for ${label}`)
  return row
}

describe('Voice it › Preferences, the standing preferences', () => {
  beforeEach(() => {
    vi.stubGlobal('open', vi.fn())
  })
  afterEach(() => {
    fetchMock.read.mockReset()
    fetchMock.write.mockReset()
    vi.unstubAllGlobals()
  })

  it('draws every standing preference', async () => {
    servePreferences(prefsView({ ...DEFAULTS, stacks: ['Postgres', 'FastAPI'], reservations: { small: 25 } }, 2, null))
    render(<VoiceStandingPreferences />)
    await settle()

    expect(within(prefRow('Voice')).getByText('Marin')).toBeTruthy()
    expect(within(prefRow('Microphone')).getByText('Push to talk')).toBeTruthy()
    expect(within(prefRow('How brief')).getByText('Normal')).toBeTruthy()
    expect(within(prefRow('Preferred stacks')).getByText('Postgres, FastAPI')).toBeTruthy()
    expect(within(prefRow('Preferred hosting')).getByText('None')).toBeTruthy()
    expect(within(prefRow('Preferred repositories')).getByText('None')).toBeTruthy()
    expect(within(prefRow('Options to propose')).getByText('3')).toBeTruthy()
    expect(within(prefRow('Grace window')).getByText('30 seconds')).toBeTruthy()
    expect(within(prefRow('Reservations')).getByText('small $25')).toBeTruthy()
    expect((screen.getByRole('button', { name: /^Undo/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('edits one preference with the revision it read and draws the answer', async () => {
    servePreferences(prefsView(DEFAULTS, 4, null))
    fetchMock.write.mockResolvedValue(ok({ ...prefsView({ ...DEFAULTS, voice: 'cedar' }, 5, DEFAULTS), changed: ['voice'] }))
    render(<VoiceStandingPreferences />)
    await settle()

    fireEvent.click(within(prefRow('Voice')).getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Voice'), { target: { value: 'cedar' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await settle()

    const [target, method, body] = fetchMock.write.mock.calls[0]!
    expect((target as { url: string }).url).toBe(VOICE_PREFERENCES)
    expect(method).toBe('PATCH')
    expect(body).toEqual({ changes: { voice: 'cedar' }, revision: 4 })
    expect(within(prefRow('Voice')).getByText('Cedar')).toBeTruthy()
    expect(screen.getByText(/takes effect at your next session/)).toBeTruthy()
    expect((screen.getByRole('button', { name: /^Undo/ }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('edits a list as comma-separated names', async () => {
    servePreferences(prefsView(DEFAULTS, 1, null))
    fetchMock.write.mockResolvedValue(ok({ ...prefsView({ ...DEFAULTS, hosting: ['Cloud Run', 'GKE'] }, 2, DEFAULTS), changed: ['hosting'] }))
    render(<VoiceStandingPreferences />)
    await settle()

    fireEvent.click(within(prefRow('Preferred hosting')).getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Preferred hosting'), { target: { value: 'Cloud Run, GKE,, ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await settle()

    expect(fetchMock.write.mock.calls[0]![2]).toEqual({ changes: { hosting: ['Cloud Run', 'GKE'] }, revision: 1 })
    expect(within(prefRow('Preferred hosting')).getByText('Cloud Run, GKE')).toBeTruthy()
  })

  it('Undo restores the previous value, including a change made by voice', async () => {
    // "Remember I prefer Postgres", said in a session: the stored edit the pane reads.
    const byVoice = { ...DEFAULTS, stacks: ['Postgres'] }
    servePreferences(prefsView(byVoice, 7, DEFAULTS))
    fetchMock.write.mockResolvedValue(ok(prefsView(DEFAULTS, 8, byVoice)))
    render(<VoiceStandingPreferences />)
    await settle()

    expect(within(prefRow('Preferred stacks')).getByText('Postgres')).toBeTruthy()
    const undo = screen.getByRole('button', { name: /^Undo/ })
    expect(screen.getByText(/Preferred stacks back to None/)).toBeTruthy()
    fireEvent.click(undo)
    await settle()

    const [target, method, body] = fetchMock.write.mock.calls[0]!
    expect((target as { url: string }).url).toBe(VOICE_PREFERENCES_UNDO)
    expect(method).toBe('POST')
    expect(body).toEqual({ revision: 7 })
    expect(within(prefRow('Preferred stacks')).getByText('None')).toBeTruthy()
  })

  it('reads again when the preferences changed since the page read them', async () => {
    let reads = 0
    fetchMock.read.mockImplementation(async () => {
      reads += 1
      return ok(reads === 1 ? prefsView(DEFAULTS, 1, null) : prefsView({ ...DEFAULTS, brevity: 'terse' }, 2, DEFAULTS))
    })
    const stale = 'Your preferences changed since this page read them; reload and try again.'
    fetchMock.write.mockResolvedValue(refused(409, 'conflict', stale))
    render(<VoiceStandingPreferences />)
    await settle()

    fireEvent.click(within(prefRow('Options to propose')).getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Options to propose'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await settle()

    expect(screen.getByText(stale)).toBeTruthy()
    expect(reads).toBe(2)
    expect(within(prefRow('How brief')).getByText('Terse')).toBeTruthy()
  })
})
