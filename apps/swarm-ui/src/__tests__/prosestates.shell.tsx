// The shell's click-only states (issue 439; the harness and the rule are in
// prosestates.tsx): the rail's menus, the bell's list, the dock, the section
// question card, the error boundary, the not-found page, sign-out, and the
// overlays every screen opens (components/Overlay.tsx) and the table's
// expanded row (components/Table.tsx), each on hand-built props.

import { useState, type ReactElement, type ReactNode } from 'react'
import { fireEvent } from '@testing-library/react'

import { Table } from '../components/Table'
import { CancelWorkflowConfirm, Dialog, Menu, Tooltip, TypedConfirm } from '../components/Overlay'
import { ErrorBoundary } from '../ErrorBoundary'
import { SignOutButton, SignedBackIn } from '../SignOut'
import { leave } from '../signoutState'
import type { ProseState } from './prosestates'

/** Click the first element matching `selector`; a missing one is an opener the page no longer draws. */
export function click(selector: string): void {
  const el = document.querySelector<HTMLElement>(selector)
  if (el === null) throw new Error(`nothing to click: ${selector}`)
  fireEvent.click(el)
}

/** A button that opens `children` (closed through the function it is handed): the click the state is reached by. */
function Opener({ label, children }: Readonly<{ label: string; children: (close: () => void) => ReactNode }>): ReactElement {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" className="ps-open" onClick={() => setOpen(true)}>
        {label}
      </button>
      {open && children(() => setOpen(false))}
    </>
  )
}

function Boom(): ReactElement {
  throw new Error('render failed at row 3')
}

const EMAIL = 'vera@example.com'

/** The sign-out round trip: the button signs out, IAP signs the same account back in, and the shell says so. */
function SignOutTrip(): ReactElement {
  const [back, setBack] = useState(false)
  leave.to = () => setBack(true)
  return back ? <SignedBackIn email={EMAIL} /> : <SignOutButton email={EMAIL} />
}

interface Row {
  id: string
  name: string
}

const ADMIN = 'admin/limits'
const SWITCH_NOTE =
  'Kept for this tab’s session; a new tab opens in your default. Every request names it, and the API checks your membership each time; switching reloads this page’s reads.'

export const SHELL: readonly ProseState[] = [
  {
    key: 'shell/notifications',
    group: 'shell',
    files: ['Notifications.tsx'],
    shows: '.nt-panel',
    walk: { open: 'overview/now', then: () => click('.nt-bell') },
  },
  {
    key: 'shell/question-card',
    group: 'shell',
    files: ['App.tsx'],
    shows: '.ctl-q-card',
    moved: ['Is the platform healthy right now, and if not, what is the first thing to look at?'],
    walk: { open: 'overview/now', then: () => click('.ctl-q-glyph') },
  },
  {
    key: 'shell/help-card',
    group: 'shell',
    files: ['HelpCard.tsx'],
    shows: '[id$="-card"][role="dialog"]',
    moved: ['A digit is a measurement, and a measured zero is a digit too.'],
    walk: { open: 'overview/now', then: () => click('.help-glyph') },
  },
  {
    key: 'shell/dock',
    group: 'shell',
    files: ['Dock.tsx'],
    shows: '.ctl-dock-body',
    walk: { open: 'overview/now', then: () => click('.ctl-dock-line') },
  },
  {
    key: 'shell/workspace-switcher',
    group: 'shell',
    files: ['Spine.tsx'],
    shows: '.sk-tpop',
    lines: ['This tab only'],
    moved: [SWITCH_NOTE],
    walk: { open: ADMIN, persona: 'platform-admin', then: () => click('.sk-panel .sk-tsw') },
  },
  {
    key: 'shell/workspace-sheet',
    group: 'shell',
    files: ['Spine.tsx'],
    shows: '.sk-tpop',
    lines: ['This tab only'],
    moved: [SWITCH_NOTE],
    walk: { open: ADMIN, persona: 'platform-admin', then: () => click('.sk-pbar .sk-tchip') },
  },
  {
    key: 'shell/workspace-tile',
    group: 'shell',
    files: ['Spine.tsx'],
    shows: '.sk-tpop',
    lines: ['This tab only'],
    moved: [SWITCH_NOTE],
    walk: {
      open: ADMIN,
      persona: 'platform-admin',
      then: () => {
        click('[aria-label="Collapse the panel"]')
        click('button.sk-ttile')
      },
    },
  },
  {
    key: 'shell/phone-menu',
    group: 'shell',
    files: ['Spine.tsx'],
    shows: '.sk-side[role="dialog"]',
    walk: { open: 'overview/now', then: () => click('.sk-pbar [aria-label="Open the menu"]') },
  },
  {
    key: 'shell/profile-menu',
    group: 'shell',
    files: ['Spine.tsx'],
    shows: '.sk-flyout.is-profile',
    walk: {
      open: '/settings/profile',
      then: () => {
        click('[aria-label="Collapse the panel"]')
        click('.sk-spine a[data-sec="profile"]')
      },
    },
  },
  {
    // Collapsed, a click on the section you are in opens its pages (visual QA V083).
    key: 'shell/section-flyout',
    group: 'shell',
    files: ['Spine.tsx'],
    shows: '.sk-flyout.is-work',
    walk: {
      open: 'work/running',
      then: () => {
        click('[aria-label="Collapse the panel"]')
        click('.sk-spine a[data-sec="work"]')
      },
    },
  },
  {
    key: 'shell/not-found',
    group: 'shell',
    files: ['NotFound.tsx'],
    shows: '.nf-page',
    lines: ['Nearest page: Overview'],
    moved: ['This console has no page at that address, so nothing here is a reading of the platform.'],
    walk: {
      open: 'overview/now',
      then: () => {
        window.history.pushState(null, '', '/no-such-page')
        window.dispatchEvent(new PopStateEvent('popstate'))
      },
    },
  },
  {
    key: 'shell/error-boundary',
    group: 'shell',
    files: ['ErrorBoundary.tsx'],
    shows: '.app-takeover',
    data: ['render failed at row 3'],
    lines: ['Whatever is running is still running.'],
    moved: [
      'Something in this page threw an error and it could not finish rendering.',
      'This is a bug in the UI, not a statement about the platform.',
    ],
    mount: () => (
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>
    ),
  },
  {
    key: 'shell/sign-out',
    group: 'shell',
    files: ['SignOut.tsx'],
    shows: '.sk-back',
    lines: [`Google signed you back in as ${EMAIL}`],
    moved: [
      `You signed out, and Google signed you back in as ${EMAIL}.`,
      'That is the only Google account signed in to this browser, so Google picked it without asking.',
      'If Google does not bring you back here, open this address again.',
    ],
    mount: () => <SignOutTrip />,
    act: () => click('.sk-out'),
  },
  {
    key: 'overlay/menu',
    group: 'shell',
    files: ['components/Overlay.tsx'],
    shows: '.c-menu',
    mount: () => (
      <Menu
        label="Run actions"
        items={[
          { key: 'retry', label: 'Retry the run' },
          { key: 'stop', label: 'Stop the run', danger: true },
          { key: 'pin', label: 'Pin it', disabledReason: 'Pinned already' },
        ]}
      />
    ),
    act: () => click('.c-menu-host button'),
  },
  {
    key: 'overlay/dialog',
    group: 'shell',
    files: ['components/Overlay.tsx'],
    shows: '.c-dlg',
    mount: () => (
      <Opener label="Rename">
        {(close) => (
          <Dialog title="Rename the pool" onClose={close} actions={<button type="button" onClick={close}>Close</button>}>
            <p>New name for the pool</p>
          </Dialog>
        )}
      </Opener>
    ),
    act: () => click('.ps-open'),
  },
  {
    key: 'overlay/typed-confirm',
    group: 'shell',
    files: ['components/Overlay.tsx'],
    shows: '.c-dlg',
    mount: () => (
      <Opener label="Delete">
        {(close) => (
          <TypedConfirm title="Delete this pool?" name="pool-a" verb="Delete the pool" keep="Keep it" onConfirm={close} onClose={close}>
            <p>Its agents stop.</p>
          </TypedConfirm>
        )}
      </Opener>
    ),
    act: () => click('.ps-open'),
  },
  {
    key: 'overlay/cancel-workflow',
    group: 'shell',
    files: ['components/Overlay.tsx'],
    shows: '.c-dlg',
    data: ['wf_0123456789abcdef'],
    lines: ['2 running steps stop · 3 unstarted steps cancelled · results kept'],
    moved: ['2 running steps are asked to stop at their next heartbeat; 3 steps not yet started are cancelled. Steps that finished keep their results.'],
    mount: () => (
      <Opener label="Cancel">
        {(close) => <CancelWorkflowConfirm workflowId="wf_0123456789abcdef" running={2} notStarted={3} onConfirm={close} onClose={close} />}
      </Opener>
    ),
    act: () => click('.ps-open'),
  },
  {
    key: 'overlay/tooltip',
    group: 'shell',
    files: ['components/Overlay.tsx'],
    shows: '.c-tip:not([hidden])',
    mount: () => (
      <Tooltip tip="Pool a is full">
        <button type="button" className="ps-open">
          Pool a status
        </button>
      </Tooltip>
    ),
    act: () => {
      const el = document.querySelector<HTMLElement>('.ps-open')
      if (el === null) throw new Error('no tooltip host')
      fireEvent.mouseEnter(el)
      fireEvent.focus(el)
    },
  },
  {
    key: 'table/expanded-row',
    group: 'shell',
    files: ['components/Table.tsx'],
    shows: '.c-tbl tr + tr td[colspan]',
    mount: () => (
      <Table<Row>
        caption="Pools"
        rows={[
          { id: 'a', name: 'pool-a' },
          { id: 'b', name: 'pool-b' },
        ]}
        rowKey={(r) => r.id}
        columns={[{ key: 'name', head: 'Pool name', cell: (r) => r.name }]}
        expand={(r) => <span>{r.name} holds 3 agents of 4</span>}
      />
    ),
    act: () => click('.c-car'),
  },
]
