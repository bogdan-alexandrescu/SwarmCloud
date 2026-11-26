// The admin section's click-only states (issue 439; the harness and the rule
// are in prosestates.tsx): Platform people's sheet and dialogs, Pool limits'
// editor and folds, Runner sizes' confirms, Platform counts' menu and fold,
// Setup's steps and tips, Health's tips, the all-workspace schedules'
// dialogs, and Voice it's settings, stage pins and the voice components the
// fixture build never draws (prosestates.admin.mounts.tsx).

import { fireEvent, screen } from '@testing-library/react'

import { settle } from './proseapp'
import type { ProseState } from './prosestates'
import { clickText, fold } from './prosestates.agent'
import { clickDrawn, drawn, hover, press } from './prosestates.overview'
import { click } from './prosestates.shell'
import {
  mountCostPill,
  mountDebugTrace,
  mountFloating,
  mountNewWorkspaceMember,
  mountNewWorkspaceNotGranted,
  mountNewWorkspaceRefused,
  mountPeopleLoans,
  mountProjects,
  mountRoster,
  mountSchedules,
  mountSessionRecord,
  mountTeamRefused,
  mountVoiceConflict,
  mountVoiceRefused,
  mountWorkingCard,
} from './prosestates.admin.mounts'
import { refuseRestoreAsCut } from './prosestates.opened.mounts'

/** RunnerSizes.tsx's CUT_TIP: why a memory cut asks twice, behind the cut confirms' (i). */
const CUT_TIP = 'Each class listed gets less memory than its 7-day max peak. Memory never bursts: an attempt that needs more is killed and goes back to its last checkpoint.'

const ADMIN_P = 'platform-admin' as const
const PEOPLE = 'admin/people'
const LIMITS = 'admin/limits'
const SIZES = 'admin/runner-sizes'
const COUNTS = 'admin/counts'
const SETUP = 'admin/setup'
const VOICE = 'voice/settings'

/** What a grant of admin gives (People.tsx's GRANT_GIVES), behind the grant dialogs' (i). */
const GRANT_GIVES = 'An admin can approve workspaces, change every ceiling and pool limit, lend accounts and grant admin to others.'

/** The fixture build's answer to a route it does not serve: the API's error message, shown as it came. */
const NOT_SERVED = 'The fixture build does not serve this route'

/** Type `text` into the first input under `selector`. */
async function typeInto(selector: string, text: string): Promise<void> {
  const input = await drawn(selector)
  fireEvent.change(input, { target: { value: text } })
  await settle()
}

/** Pool limits' folded cards, each with what its open body keeps and what it moved behind its (i). */
const FOLDS: readonly { name: string; lines?: string[]; moved?: string[] }[] = [
  { name: 'Additional repositories' },
  {
    name: 'Agent budget ceiling',
    lines: ['Turns nothing on by itself'],
    moved: ['The most any workspace’s per-agent budget can be.', 'It turns nothing on: a workspace that sets no budget has none.', 'At 80% of its budget an agent is told to finish and publish.'],
  },
  {
    name: 'Jev’s rate',
    lines: ['Prices Capacity › Jev usage'],
    moved: ['What Jev costs, in dollars per million tokens.', 'Capacity › Jev usage prices its calls at this rate; until it is set, that page says “rate not set” and shows no dollars.'],
  },
  { name: 'Model prices' },
  { name: 'GitHub fallback token' },
]

/** Fill the inline New workspace form's group, with `admin` as its first admin, and create it. */
async function fileGroup(admin: string): Promise<void> {
  await press('.nw-inline', 'Group')
  fireEvent.change(screen.getByRole('textbox', { name: 'Group address' }), { target: { value: 'research@example.com' } })
  if (admin !== '') fireEvent.change(screen.getByRole('textbox', { name: 'First admin’s email address (optional)' }), { target: { value: admin } })
  await settle()
  await press('.nw-inline', 'Create workspace')
}

/** People's first Manage button. */
const manage = (): Promise<void> => press('main', 'Manage')

export const ADMIN: readonly ProseState[] = [
  // ---------------------------------------------------------------- Platform people
  {
    key: 'admin/people-audit',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.pp-audit.is-open',
    walk: { open: PEOPLE, persona: ADMIN_P, then: () => click('.pp-fold') },
  },
  {
    key: 'admin/people-sheet',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.pp-drawer',
    lines: ['Off'],
    moved: ['An admin through a group is an admin but has no role recorded here, so they show as off.'],
    data: [NOT_SERVED],
    walk: { open: PEOPLE, persona: ADMIN_P, then: manage },
  },
  {
    key: 'admin/people-sheet-ceiling',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg, .pp-drawer p.ur-hint[role="status"], .pp-drawer [role="alert"]',
    data: [NOT_SERVED],
    walk: {
      open: PEOPLE,
      persona: ADMIN_P,
      then: async () => {
        await manage()
        await clickDrawn('.pp-drawer button[aria-label="One more"]')
        await press('.pp-drawer', 'Save')
      },
    },
  },
  {
    key: 'admin/people-sheet-admin-switch',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    lines: ['Full platform admin powers'],
    moved: [GRANT_GIVES],
    walk: {
      open: PEOPLE,
      persona: ADMIN_P,
      then: async () => {
        await manage()
        await clickDrawn('.pp-drawer .pp-switch')
      },
    },
  },
  {
    key: 'admin/people-approve',
    group: 'admin',
    files: ['People.tsx'],
    shows: 'main p.ur-hint[role="status"], main .pp-said-cmd, main [role="alert"]',
    data: [NOT_SERVED],
    walk: { open: PEOPLE, persona: ADMIN_P, then: () => press('main', 'Approve') },
  },
  {
    key: 'admin/people-deny',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.pp-deny',
    walk: { open: PEOPLE, persona: ADMIN_P, then: () => press('main', 'Deny') },
  },
  {
    key: 'admin/people-grant-admin',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    walk: { open: PEOPLE, persona: ADMIN_P, then: () => press('main', 'Grant admin') },
  },
  {
    key: 'admin/people-grant-admin-ask',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    lines: ['Full platform admin powers'],
    moved: [GRANT_GIVES],
    data: ['dana@example.com'],
    walk: {
      open: PEOPLE,
      persona: ADMIN_P,
      then: async () => {
        await press('main', 'Grant admin')
        await typeInto('.c-dlg input', 'dana@example.com')
        await press('.c-dlg', 'Grant admin')
      },
    },
  },
  {
    key: 'admin/people-remove-admin',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    walk: { open: PEOPLE, persona: ADMIN_P, then: () => press('main', 'Remove admin') },
  },
  {
    key: 'admin/people-loans',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.pp-loans[open]',
    walk: { open: PEOPLE, persona: ADMIN_P, then: () => click('.pp-loans > summary') },
  },
  // ---------------------------------------------------------------- Pool limits
  {
    key: 'admin/limits-editor',
    group: 'admin',
    files: ['AdminSettings.tsx'],
    shows: '.adm-side',
    walk: { open: LIMITS, persona: ADMIN_P, then: () => click('button[aria-label="Edit ceiling for eng (workspace)"]') },
  },
  {
    key: 'admin/limits-drastic',
    group: 'admin',
    files: ['AdminSettings.tsx'],
    shows: '.adm-drastic',
    lines: ['Drastic change'],
    moved: ['This is a drastic change: to 0, or a cut of half or more.'],
    walk: {
      open: LIMITS,
      persona: ADMIN_P,
      then: async () => {
        click('button[aria-label="Edit ceiling for eng (workspace)"]')
        await typeInto('.adm-side input', '0')
      },
    },
  },
  {
    key: 'admin/limits-save',
    group: 'admin',
    files: ['AdminSettings.tsx'],
    shows: 'tr:has(.adm-saved-what), .adm-side [role="alert"]',
    walk: {
      open: LIMITS,
      persona: ADMIN_P,
      then: async () => {
        click('button[aria-label="Edit ceiling for eng (workspace)"]')
        await typeInto('.adm-side input', '7')
        await press('.adm-side', 'Save')
      },
    },
  },
  {
    key: 'admin/limits-quota',
    group: 'admin',
    files: ['AdminSettings.tsx'],
    shows: '.adm-side',
    walk: { open: LIMITS, persona: ADMIN_P, then: () => click('button[aria-label="Edit ceiling for anthropic · u-owner"]') },
  },
  ...FOLDS.map(
    ({ name, lines, moved }): ProseState => ({
      key: `admin/limits-fold-${name.toLowerCase().replace(/[^a-z]+/g, '-')}`,
      group: 'admin',
      files: ['AdminSettings.tsx'],
      shows: '.adm-fold-card.is-open',
      ...(lines === undefined ? {} : { lines }),
      ...(moved === undefined ? {} : { moved }),
      walk: { open: LIMITS, persona: ADMIN_P, then: () => clickText('.adm-fold', name) },
    }),
  ),
  // ---------------------------------------------------------------- Runner sizes
  {
    key: 'admin/runner-sizes-restore',
    group: 'admin',
    files: ['RunnerSizes.tsx'],
    shows: '.c-dlg',
    lines: ['Writes a new version'],
    moved: ['A restore writes a NEW version with version 1’s sizes; History keeps every version, and the next admission takes it.'],
    walk: { open: SIZES, persona: ADMIN_P, then: () => press('main', 'Restore version 1') },
  },
  {
    key: 'admin/runner-sizes-defaults',
    group: 'admin',
    files: ['RunnerSizes.tsx'],
    shows: '.c-dlg',
    lines: ['Writes a new version'],
    moved: ['A restore writes a NEW version with the code defaults’s sizes; History keeps every version, and the next admission takes it.'],
    walk: { open: SIZES, persona: ADMIN_P, then: () => press('main', 'Restore the code defaults') },
  },
  {
    key: 'admin/runner-sizes-save',
    group: 'admin',
    files: ['RunnerSizes.tsx'],
    shows: '.rs-save:has(.rs-saved, .rs-error), .c-dlg',
    // The reason typed, and the API's refusal of the write (the fixture build serves no write route).
    data: ['one more core for the browser lanes', NOT_SERVED],
    walk: {
      open: SIZES,
      persona: ADMIN_P,
      then: async () => {
        const first = await drawn('.rs-field input')
        await typeInto('.rs-field input', String(Number((first as HTMLInputElement).value) + 1))
        await typeInto('.rs-save textarea', 'one more core for the browser lanes')
        await press('main', 'Save')
      },
    },
  },
  // ---------------------------------------------------------------- Platform counts
  {
    key: 'admin/counts-breakdown',
    group: 'admin',
    files: ['PlatformOverTime.tsx'],
    shows: '.pot-menu',
    walk: { open: COUNTS, persona: ADMIN_P, then: () => press('main', 'Breakdown') },
  },
  // ---------------------------------------------------------------- Setup
  {
    key: 'admin/setup-more',
    group: 'admin',
    files: ['consolefinish/ChecklistFinish.tsx'],
    shows: '.cfc-more[open]',
    walk: { open: SETUP, persona: ADMIN_P, then: () => fold('main', 'Capacity and add-ons') },
  },
  {
    key: 'admin/setup-tip',
    group: 'admin',
    files: ['AdminSetupSteps.tsx'],
    shows: '.c-tip:not([hidden])',
    walk: { open: SETUP, persona: ADMIN_P, then: () => hover('main') },
  },
  {
    key: 'admin/setup-sign-in-opened',
    group: 'admin',
    files: ['AdminSetupSteps.tsx'],
    shows: 'main p.c-hint[role="status"], main [role="alert"]',
    lines: ['Sign-in for ana opened in a new tab'],
    moved: ['Your sign-in for ana opened in a new tab: sign in to Claude there, and this page sees the account at its next read.'],
    walk: {
      open: SETUP,
      persona: ADMIN_P,
      then: async () => {
        await press('[data-step="accounts"]', '5. Claude accounts')
        fireEvent.change(screen.getAllByLabelText('Account label')[0]!, { target: { value: 'ana' } })
        await settle()
        await press('main', 'Sign in my Claude account')
      },
    },
  },
  {
    key: 'admin/health-tip',
    group: 'admin',
    files: ['AdminSetupHealth.tsx'],
    shows: '.c-tip:not([hidden])',
    walk: { open: 'admin/health', persona: ADMIN_P, then: () => hover('main') },
  },
  {
    key: 'admin/setup-team-refused',
    group: 'admin',
    files: ['AdminSetupTeam.tsx'],
    shows: '[role="alert"]',
    mount: mountTeamRefused,
    act: async () => {
      await typeInto('.asm-team input', 'eng@example.com')
      await press('body', 'Add the team')
    },
  },
  // ---------------------------------------------------------------- People: the drawer's asks the fixture never draws
  {
    key: 'admin/people-approve-denied',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    moved: ['A CI job will create its identity and namespace.'],
    data: ['Use the eng space.'],
    mount: mountPeopleLoans,
    act: async () => {
      await clickDrawn('tr[data-email="fay@example.com"] button')
      await press('.pp-drawer', 'Approve')
    },
  },
  {
    key: 'admin/people-lend',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    lines: ['Starts no new agent'],
    moved: ['Their work may then run on an account eng owns.', 'This account is paused: it starts no new agent until its owner makes it available again.'],
    data: ['Use the eng space.'],
    mount: mountPeopleLoans,
    act: async () => {
      await clickDrawn('tr[data-email="bob@example.com"] button')
      await typeInto('.pp-drawer [data-section="claude"] select', 'acct-eng-2')
      await press('.pp-drawer [data-section="claude"]', 'Lend')
    },
  },
  {
    key: 'admin/people-reclaim',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    moved: ['eng stops lending it to them; their new work is no longer offered it.'],
    data: ['Use the eng space.'],
    mount: mountPeopleLoans,
    act: async () => {
      await clickDrawn('tr[data-email="hal@example.com"] button')
      await press('.pp-drawer [data-section="claude"]', 'Reclaim')
    },
  },
  // ---------------------------------------------------------------- Voice it › Settings: a save's answers
  {
    key: 'voice/settings-conflict',
    group: 'admin',
    files: ['voice/Settings.tsx'],
    shows: '[data-card="behaviour"] [role="alert"]',
    mount: mountVoiceConflict,
    act: async () => {
      await press('[data-card="behaviour"]', '2 min')
      await press('body', 'Save')
    },
  },
  {
    key: 'voice/settings-refused',
    group: 'admin',
    files: ['voice/Settings.tsx'],
    shows: '[data-card="behaviour"] [role="alert"]',
    data: ['idle takes a name from the catalogue'],
    mount: mountVoiceRefused,
    act: async () => {
      await press('[data-card="behaviour"]', '2 min')
      await press('body', 'Save')
    },
  },
  // ---------------------------------------------------------------- New workspace: the form's answers
  {
    key: 'admin/new-workspace-first-admin-pending',
    group: 'admin',
    files: ['NewWorkspace.tsx'],
    shows: '[data-first-admin]',
    lines: ['frank@example.com is its first admin.'],
    moved: ['They are not in research@example.com yet: the role takes effect once they are, because membership stays the group’s.'],
    mount: mountNewWorkspaceMember(false),
    act: () => fileGroup('frank@example.com'),
  },
  {
    key: 'admin/new-workspace-first-admin-member',
    group: 'admin',
    files: ['NewWorkspace.tsx'],
    shows: '[data-first-admin]',
    lines: ['frank@example.com is its first admin.'],
    moved: ['They are in research@example.com, so the role applies now.'],
    mount: mountNewWorkspaceMember(true),
    act: () => fileGroup('frank@example.com'),
  },
  {
    key: 'admin/new-workspace-first-admin-unchecked',
    group: 'admin',
    files: ['NewWorkspace.tsx'],
    shows: '[data-first-admin]',
    lines: ['frank@example.com is its first admin; membership not checked'],
    moved: ['Whether they are in research@example.com could not be checked; the role takes effect once they are in it.'],
    mount: mountNewWorkspaceMember(null),
    act: () => fileGroup('frank@example.com'),
  },
  {
    key: 'admin/new-workspace-first-admin-exists',
    group: 'admin',
    files: ['NewWorkspace.tsx'],
    shows: '[data-first-admin]',
    lines: ['frank@example.com was not made its admin'],
    moved: ['This workspace already existed.', 'Name its admins on People and roles.'],
    mount: mountNewWorkspaceNotGranted('workspace_exists'),
    act: () => fileGroup('frank@example.com'),
  },
  {
    key: 'admin/new-workspace-first-admin-refused',
    group: 'admin',
    files: ['NewWorkspace.tsx'],
    shows: '[data-first-admin]',
    lines: ['frank@example.com was not made its admin (not_in_domain)'],
    moved: ['Send the form again while it is being created, or name them on People and roles.'],
    mount: mountNewWorkspaceNotGranted('not_in_domain'),
    act: () => fileGroup('frank@example.com'),
  },
  {
    key: 'admin/new-workspace-refused',
    group: 'admin',
    files: ['NewWorkspace.tsx'],
    shows: '.nw-inline [role="alert"]',
    data: ['research@example.com already has a workspace.'],
    mount: mountNewWorkspaceRefused,
    act: () => fileGroup(''),
  },
  // ---------------------------------------------------------------- Schedules, every workspace
  {
    key: 'admin/schedules-action',
    group: 'admin',
    files: ['AdminSchedules.tsx'],
    shows: '.c-dlg',
    lines: ['Live work is not cancelled.'],
    moved: ['It stops firing until its workspace resumes it.', 'The change is written to the schedule’s audit as yours.'],
    mount: mountSchedules,
    act: () => press('main, body', 'Pause'),
  },
  {
    key: 'admin/schedules-pause-all',
    group: 'admin',
    files: ['AdminSchedules.tsx'],
    shows: '.c-dlg',
    lines: ['Live work is not cancelled.'],
    moved: ['Every enabled schedule of eng stops firing.', 'Its members can resume each one.'],
    mount: mountSchedules,
    act: () => press('body', 'Pause all of'),
  },
  // ---------------------------------------------------------------- Voice it
  {
    key: 'voice/settings-delete-key',
    group: 'admin',
    files: ['voice/Settings.tsx'],
    shows: '.vset-band[role="dialog"]',
    walk: { open: VOICE, persona: ADMIN_P, then: () => clickText('.vset-btn', 'Delete') },
  },
  {
    key: 'voice/settings-unsaved',
    group: 'admin',
    files: ['voice/Settings.tsx'],
    shows: '.vset-headfacts:has(.vset-btn.is-primary)',
    walk: { open: VOICE, persona: ADMIN_P, then: () => clickText('button[role="radio"]', 'GPT-Live') },
  },
  {
    key: 'voice/stage-pin',
    group: 'admin',
    files: ['voice/Stage.tsx'],
    shows: '.vts-note',
    data: ['Keep the filter row in view when the steps scroll.'],
    walk: { open: 'voice/talk', persona: ADMIN_P, then: () => clickDrawn('.vts-pin') },
  },
  {
    key: 'voice/roster-chip',
    group: 'admin',
    files: ['voice/Roster.tsx'],
    shows: '.vtc-pop:not([hidden])',
    mount: mountRoster,
    act: () => click('.vtc-chip-btn'),
  },
  {
    key: 'voice/working-card',
    group: 'admin',
    files: ['voice/WorkingCard.tsx'],
    shows: '.vtc-steps',
    mount: mountWorkingCard,
    act: () => click('.vtc-work-head'),
  },
  {
    key: 'voice/cost-pill',
    group: 'admin',
    files: ['voice/CostPill.tsx'],
    shows: '[role="dialog"], .vcp-panel',
    mount: mountCostPill,
    act: () => clickDrawn('button[aria-expanded]'),
  },
  {
    key: 'voice/debug-trace-turn',
    group: 'admin',
    files: ['voice/DebugTrace.tsx'],
    shows: '#vdg-turn-1',
    mount: mountDebugTrace,
    act: () => clickDrawn('.vdg-open'),
  },
  {
    key: 'voice/floating-unfold',
    group: 'admin',
    files: ['voice/Floating.tsx'],
    shows: '.vtb-pill',
    mount: mountFloating,
    act: () => click('.vtb-core'),
  },
  {
    key: 'voice/projects-delete',
    group: 'admin',
    files: ['voice/Projects.tsx'],
    shows: '.c-dlg',
    lines: ['What it started keeps running.'],
    moved: ['This deletes the project, its cards and turns, and its transcripts, mockups and plans.', 'What it started is not stopped: agents and workflows it launched keep running.'],
    mount: mountProjects,
    act: () => press('body', 'Delete'),
  },
  {
    key: 'voice/session-record-refused',
    group: 'admin',
    files: ['voice/SessionRecord.tsx'],
    shows: 'Not downloaded',
    mount: mountSessionRecord,
    act: () => press('body', 'Download transcript'),
  },
  // ---------------------------------------------------------------- openers no state opened (review round 2)
  {
    key: 'admin/people-admins-grant',
    group: 'admin',
    files: ['People.tsx'],
    shows: '.c-dlg',
    lines: ['Full platform admin powers'],
    moved: [GRANT_GIVES],
    data: ['dana@example.com'],
    walk: {
      open: PEOPLE,
      persona: ADMIN_P,
      then: async () => {
        await typeInto('.pp-grant input', 'dana@example.com')
        await press('.pp-grant', 'Grant admin')
      },
    },
  },
  {
    key: 'admin/runner-sizes-save-cut',
    group: 'admin',
    files: ['RunnerSizes.tsx'],
    shows: '.c-dlg',
    lines: ['An attempt over its memory is killed'],
    moved: [CUT_TIP],
    data: ['less memory for the standard lanes'],
    walk: {
      open: SIZES,
      persona: ADMIN_P,
      then: async () => {
        await drawn('.rs-field')
        // The standard class's memory, 8 GiB, cut to 4: under its 7-day max peak of 5.6 GiB.
        const memory = [...document.querySelectorAll<HTMLElement>('.rs-field')].find((f) => /^standard memory/i.test(f.querySelector('label')?.textContent ?? ''))
        if (memory === undefined) throw new Error('no memory field')
        fireEvent.change(memory.querySelector('input') as HTMLInputElement, { target: { value: '4' } })
        await settle()
        await typeInto('.rs-save textarea', 'less memory for the standard lanes')
        await press('.rs-save', 'Save')
      },
    },
  },
  {
    key: 'admin/runner-sizes-restore-cut',
    group: 'admin',
    files: ['RunnerSizes.tsx'],
    shows: '.c-dlg',
    lines: ['An attempt over its memory is killed'],
    moved: [`Restoring version 1 cuts memory for standard below the class’s 7-day max peak. ${CUT_TIP}`],
    data: ['the old sizes were right'],
    walk: {
      open: SIZES,
      persona: ADMIN_P,
      then: async () => {
        await refuseRestoreAsCut(['standard'])
        await press('main', 'Restore version 1')
        await typeInto('.c-dlg textarea', 'the old sizes were right')
        await press('.c-dlg', 'Restore')
      },
    },
  },
]
