// CONFIG › PEOPLE AND ROLES DRAWN AS S5b'S CARDS (lane CONFIG-PEOPLE-S5; part
// of #2391, BGD 2026-10-26: apply the decided mock-up, config-e.html S5b, S5e,
// S5g, S5h, renderers `cpeople` and `cpeopleMember`).
//
// WHAT EACH CASE HOLDS:
//   * an admin's page is a lead, a "Roles in this workspace" card, then a row
//     of two separate cards, "Give a person their own role" and "Role events"
//     (`.ws-grid`, one column under 900 px: S5e);
//   * the roster is a table even for a lone admin, with the caller's own row
//     (marked "you"), and the caller is never listed twice;
//   * a person's role is picked on the console's segmented control
//     (viewer | member | admin), not a <select>, and the PUT carries the pick;
//   * a member's (or viewer's) Roles card carries "read only for members" in
//     its head, and Role events is its own card; no form, no row;
//   * the lead carries the mock-up's body sentence under its title.
//
// MUTATIONS, each of which turns a case red: put the form or the events back
// inside the Roles card; draw the table only when someone holds a role of
// their own; put the <select> back; drop the chip; drop the lead's body.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const ROLES_TABLE = [
  { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
  { name: 'member', summary: 'Submits and approves.' },
  { name: 'admin', summary: 'A member who also manages people’s roles.' },
]

function person(email: string, role: string, reason = ''): Json {
  return {
    email, role, source: 'override', granted_by: 'ops@example.com',
    granted_at: '2026-10-18T09:14:03.120000Z', granted_via: 'console', reason,
  }
}

function roles(me: Json, canManage: boolean, people: Json[], over: Json = {}): Json {
  return {
    tenant_id: 'eng', kind: 'group', default_role: 'viewer', default_source: 'set',
    me: { email: 'carol@example.com', via: 'member', ...me },
    people, roles: ROLES_TABLE, can_manage: canManage,
    audit: [
      {
        action: 'workspace_role_granted', by: 'ops@example.com', at: '2026-10-18T09:14:03Z',
        target_email: 'dana@example.com', detail: { from: 'viewer', to: 'admin', via: 'console', reason: 'workspace owner' },
      },
    ],
    ...over,
  }
}

const LONE_ADMIN = roles({ role: 'admin', source: 'platform_admin', via: 'platform_admin' }, true, [])
const ADMIN_HELD = roles({ role: 'admin', source: 'override', granted_by: 'ops@example.com', granted_at: '2026-10-18T09:14:03.120000Z' }, true,
  [person('carol@example.com', 'admin', 'workspace owner'), person('dana@example.com', 'member')])
const MEMBER = roles({ role: 'member', source: 'override', granted_by: 'dana@example.com', granted_at: '2026-10-18T09:14:03.120000Z' }, false,
  [person('dana@example.com', 'admin'), person('carol@example.com', 'member')])

function people(body: Json, write?: (m: string, url: string, b: unknown) => { status: number; body: unknown } | null) {
  return serve((m, url, b) => {
    if (m === 'GET' && url === '/v1/workspace/roles') return { status: 200, body }
    if (m !== 'GET' && write) return write(m, url, b)
    return null
  })
}

async function draw(body: Json, write?: (m: string, url: string, b: unknown) => { status: number; body: unknown } | null) {
  const calls = people(body, write)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
  const { SetupContext } = await import('../ConfigSetup')
  const data = { tenant_id: 'eng', steps: [], config_setup: { secrets: null, people: { applies: true, done: false, holders: 0, admins: 0, pending: null, reason: null } } }
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: data as never, fetchedAt: Date.now() }, personal: false }}>
      <WorkspacePeopleScreen />
    </SetupContext.Provider>,
  )
  await screen.findByRole('heading', { name: 'Role events' })
  return calls
}

/** The card (`section.c-card`) whose head names `title`. */
function card(title: string): HTMLElement {
  const h = screen.getByRole('heading', { name: title })
  const c = h.closest('section.c-card')
  expect(c, `"${title}" is not a card's title`).not.toBeNull()
  expect(c!.querySelector('.c-card-h')?.contains(h), `"${title}" is not in its card's head`).toBe(true)
  return c as HTMLElement
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe("S5b: an admin's page is the lead, the Roles card, and a row of two cards", () => {
  it('draws Give a person their own role and Role events as separate cards in one row, after the Roles card', async () => {
    await draw(ADMIN_HELD)
    const rolesCard = card('Roles in this workspace')
    const give = card('Give a person their own role')
    const events = card('Role events')
    // MUTATION: put the form or the events back inside the Roles card -> fails.
    expect(rolesCard.contains(give)).toBe(false)
    expect(rolesCard.contains(events)).toBe(false)
    const row = give.parentElement!
    expect(row.classList.contains('ws-grid')).toBe(true)
    expect(events.parentElement).toBe(row)
    expect([...row.children]).toEqual([give, events])
    // The row follows the Roles card.
    expect(rolesCard.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps what is built but not drawn: the permits list, Set the default and Return to default sit in the Roles card', async () => {
    await draw(ADMIN_HELD)
    const rolesCard = card('Roles in this workspace')
    expect(within(rolesCard).getByRole('heading', { name: 'What each role permits' })).toBeTruthy()
    expect(within(rolesCard).getByRole('button', { name: 'Set default' })).toBeTruthy()
    expect(within(rolesCard).getByRole('button', { name: 'Return dana@example.com to the default' })).toBeTruthy()
  })
})

describe('S5b: the roster is a table even for a lone admin, with their own row', () => {
  it('draws the lone admin’s own row, marked you, when nobody holds a role of their own', async () => {
    await draw(LONE_ADMIN)
    // MUTATION: draw the table only when `people` is not empty -> fails.
    const table = within(card('Roles in this workspace')).getByRole('table', { name: 'Who holds which role in eng' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(1)
    const mine = visible(rows[0]!)
    expect(mine).toContain('carol@example.com')
    expect(mine).toContain('you')
    expect(mine).toContain('admin')
    expect(mine).toContain('platform admin')
    // Nothing to return: the caller holds no role of their own.
    expect(within(rows[0]!).queryByRole('button')).toBeNull()
  })

  it('lists the caller once, from their own grant, when they hold one', async () => {
    await draw(ADMIN_HELD)
    const table = screen.getByRole('table', { name: 'Who holds which role in eng' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    expect(rows.filter((r) => visible(r).includes('carol@example.com'))).toHaveLength(1)
    const mine = rows.find((r) => visible(r).includes('carol@example.com'))!
    expect(visible(mine)).toContain('you')
    expect(visible(mine)).toContain('workspace owner')
  })

  it('reads each person’s role off the roster, the caller’s included, for a member', async () => {
    await draw(MEMBER)
    const table = screen.getByRole('table', { name: 'Who holds which role in eng' })
    const byEmail = (e: string) => within(table).getAllByRole('row').find((r) => visible(r).includes(e))!
    expect(within(byEmail('dana@example.com')).getByText('admin')).toBeTruthy()
    expect(within(byEmail('carol@example.com')).getByText('member')).toBeTruthy()
  })
})

describe("S5b: a person's role is the console's segmented control", () => {
  it('offers viewer, member and admin as one segmented group, and the PUT carries the pressed one', async () => {
    const calls = await draw(ADMIN_HELD, (m, url) =>
      m === 'PUT' && url === '/v1/workspace/roles/person'
        ? { status: 200, body: { email: 'erin@example.com', role: 'admin', previous_role: 'viewer', source: 'override', granted_at: '2026-10-19T08:00:00.000000Z', member: true } }
        : null)
    const give = card('Give a person their own role')
    // MUTATION: put the <select> back -> fails.
    expect(within(give).queryByRole('combobox')).toBeNull()
    const seg = within(give).getByRole('group', { name: 'Role' })
    expect(seg.classList.contains('c-seg')).toBe(true)
    const options = within(seg).getAllByRole('button')
    expect(options.map((b) => b.textContent)).toEqual(['viewer', 'member', 'admin'])
    expect(within(seg).getByRole('button', { name: 'member' }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(within(seg).getByRole('button', { name: 'admin' }))
    expect(within(seg).getByRole('button', { name: 'admin' }).getAttribute('aria-pressed')).toBe('true')
    expect(within(seg).getByRole('button', { name: 'member' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.change(within(give).getByRole('textbox', { name: 'Email' }), { target: { value: 'erin@example.com' } })
    fireEvent.click(within(give).getByRole('button', { name: 'Set role' }))
    // The outcome is said where the person was given the role.
    await within(give).findByText(/erin@example\.com is now admin/i)
    const put = calls.find((c) => c.method === 'PUT')
    expect(put?.body).toEqual({ email: 'erin@example.com', role: 'admin', via: 'console' })
  })
})

describe('S5g, S5h: a member reads the page with its controls taken off', () => {
  it('carries "read only for members" in the Roles card head, and Role events as its own card', async () => {
    const calls = await draw(MEMBER)
    const rolesCard = card('Roles in this workspace')
    const head = rolesCard.querySelector('.c-card-h') as HTMLElement
    // MUTATION: drop the chip, or leave the hint at the foot alone -> fails.
    expect(within(head).getByText('read only for members').classList.contains('c-chip')).toBe(true)
    const events = card('Role events')
    expect(rolesCard.contains(events)).toBe(false)
    expect(within(events).getByRole('list', { name: 'Role events' })).toBeTruthy()
    // No form, so no row: the events card stands alone.
    expect(screen.queryByRole('heading', { name: 'Give a person their own role' })).toBeNull()
    expect(events.parentElement?.classList.contains('ws-grid')).toBe(false)
    expect(screen.queryByRole('group', { name: 'Role' })).toBeNull()
    expect(screen.queryAllByRole('button', { name: /set role|set default|return/i })).toHaveLength(0)
    expect(calls.filter((c) => c.method !== 'GET')).toHaveLength(0)
  })

  it('draws no chip for an admin', async () => {
    await draw(ADMIN_HELD)
    expect(screen.queryByText('read only for members')).toBeNull()
  })

  it('draws each event as its sentence over its age and reason, as S5b’s rows', async () => {
    await draw(MEMBER)
    const item = within(screen.getByRole('list', { name: 'Role events' })).getAllByRole('listitem')[0]!
    expect(item.querySelector('b')?.textContent).toMatch(/^ops@example\.com gave dana@example\.com the role admin/)
    expect(item.querySelector('small')?.textContent).toMatch(/ · workspace owner$/)
  })
})

describe("the lead carries the mock-up's body sentence", () => {
  it("says who to give a role to and what everyone else holds, for an admin alone", async () => {
    await draw(LONE_ADMIN)
    const lead = document.querySelector('.ws-lead') as HTMLElement
    // MUTATION: drop the Lead's `body` -> fails.
    const body = lead.querySelector('.ws-lead-s')
    expect(body?.textContent).toBe('Give each person who will run agents their own role. Everyone else signed in to eng holds viewer, the default.')
  })

  it('says what the member holds, and from whom', async () => {
    await draw(MEMBER)
    await waitFor(() => expect(document.querySelector('.ws-lead .ws-lead-s')).not.toBeNull())
    const body = document.querySelector('.ws-lead .ws-lead-s')!.textContent ?? ''
    expect(body).toMatch(/^You hold member, given by dana@example\.com .+\. Everyone else signed in to eng holds viewer, the default\.$/)
  })
})
