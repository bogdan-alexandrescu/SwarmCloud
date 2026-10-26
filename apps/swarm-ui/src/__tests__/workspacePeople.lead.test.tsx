// CONFIG › PEOPLE AND ROLES' LEAD (CONFIG-S5-LEADS; part of #2157, BGD's
// pick of proposal E, docs/web-ui/mockups/config-e.html S5b, S5e, S5g, S5h).
//
// WHAT EACH CASE HOLDS:
//   * an admin (`can_manage`) alone in the workspace -- the setup read's step
//     4 says nobody else holds a role -- is led to invite someone, with ONE
//     button, to the "Give a person their own role" form already built;
//   * an admin with people waiting for an admin is told how many;
//   * otherwise the admin's lead is green and has no button;
//   * a member's (or viewer's) lead names who administers the workspace and
//     draws no control at all;
//   * a step-4 source that could not be read is "—" and its reason.
//
// MUTATIONS, each of which turns a case red: drop the lead; draw the invite
// for a member; read green when `holders` is 0; drop the waiting count.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { serve, visible } from './repofixture'

type Json = Record<string, unknown>

const ROLES_TABLE = [
  { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
  { name: 'member', summary: 'Submits and approves.' },
  { name: 'admin', summary: 'A member who also manages people’s roles.' },
]

function person(email: string, role: string): Json {
  return {
    email, role, source: 'override', granted_by: 'ops@example.com',
    granted_at: '2026-10-18T09:14:03.120000Z', granted_via: 'console', reason: '',
  }
}

function roles(canManage: boolean, people: Json[]): Json {
  return {
    tenant_id: 'eng', kind: 'group', default_role: 'viewer', default_source: 'set',
    me: { email: 'carol@example.com', role: canManage ? 'admin' : 'member', source: 'override', via: 'member' },
    people, roles: ROLES_TABLE, can_manage: canManage, audit: [],
  }
}

function step4(over: Json): Json {
  return {
    applies: true, done: true, holders: 2, admins: 1, pending: null, reason: null,
    pending_reason: 'this workspace takes no requests to join; its people come from its group', ...over,
  }
}

async function draw(body: Json, people: Json | undefined) {
  serve((m, url) => (m === 'GET' && url === '/v1/workspace/roles' ? { status: 200, body } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
  const { SetupContext } = await import('../ConfigSetup')
  const configSetup = people === undefined ? undefined : { secrets: null, people }
  const data = { tenant_id: 'eng', steps: [], ...(configSetup === undefined ? {} : { config_setup: configSetup }) }
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: data as never, fetchedAt: Date.now() }, personal: false }}>
      <WorkspacePeopleScreen />
    </SetupContext.Provider>,
  )
  await screen.findByRole('heading', { name: 'Role events' })
  const lead = document.querySelector('.ws-lead')
  expect(lead, 'the page has no lead').not.toBeNull()
  return lead as HTMLElement
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('VQ-047: the lead and the body give one statement of who holds what, in one size', () => {
  it('says nobody else holds a role of their own in the lead and the body alike, and never "Nobody in eng"', async () => {
    const lead = await draw(roles(true, [person('carol@example.com', 'admin')]), step4({ done: false, holders: 0, admins: 0 }))
    const said = visible(lead)
    const body = visible(screen.getByLabelText('Default role'))
    // MUTATION: put back "Only you hold a role" in the lead or "Nobody in eng holds a role of their own" -> fails.
    expect(said).not.toContain('Only you hold')
    expect(body).toContain('Everyone else in eng holds viewer')
    expect(body).toContain('nobody else holds a role of their own yet')
    expect(visible(document.body)).not.toContain('Nobody in eng holds a role of their own')
    expect(visible(document.body)).not.toContain('everyone holds the default')
  })

  it('names the people listed below when others hold their own', async () => {
    await draw(roles(true, [person('dana@example.com', 'member')]), step4({ holders: 1, admins: 0 }))
    const body = visible(screen.getByLabelText('Default role'))
    expect(body).toContain('unless they are listed below with a role of their own')
    expect(body).not.toContain('nobody else')
  })

  it('sets the caller’s role and the default in the same type, and the roles in the console’s key/value list', async () => {
    await draw(roles(true, []), step4({ holders: 0, admins: 0 }))
    // MUTATION: set the default's line `ur-small` again -> fails (two sizes, the QA's finding).
    expect(screen.getByLabelText('Your role').className).toBe('pk-lead')
    expect(screen.getByLabelText('Default role').className).toBe('pk-lead')
    const dl = screen.getByRole('heading', { name: 'What each role permits' }).closest('section')!.querySelector('dl')!
    expect(dl.classList.contains('pf-facts')).toBe(true)
  })
})

describe('Config › People and roles: the admin’s lead (S5b, S5e)', () => {
  it('invites someone when nobody else holds a role, with one button to the role form', async () => {
    const lead = await draw(roles(true, [person('carol@example.com', 'admin')]), step4({ done: false, holders: 0, admins: 0 }))
    expect(visible(lead)).toContain('Nobody else in eng holds a role of their own yet')
    expect(lead.classList.contains('is-ok')).toBe(false)
    const buttons = within(lead).getAllByRole('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0]!.textContent).toBe('Give a person a role')
    fireEvent.click(buttons[0]!)
    expect(document.activeElement).toBe(screen.getByLabelText('Email'))
  })

  it('names how many people are waiting for an admin', async () => {
    const lead = await draw(roles(true, [person('dana@example.com', 'member')]), step4({ holders: 1, admins: 0, pending: 3, pending_reason: null }))
    expect(visible(lead)).toContain('3 people are waiting for an admin')
    expect(lead.classList.contains('is-ok')).toBe(false)
  })

  it('reads green, with no button, when others hold a role and nobody waits', async () => {
    const lead = await draw(roles(true, [person('dana@example.com', 'member'), person('lee@example.com', 'viewer')]), step4({}))
    expect(lead.classList.contains('is-ok')).toBe(true)
    expect(visible(lead)).toContain('2 other people hold a role in eng')
    expect(within(lead).queryAllByRole('button')).toHaveLength(0)
  })

  it('is "—" with its reason when step 4 could not be read', async () => {
    const reason = "this workspace's roles could not be read; retry shortly"
    const lead = await draw(roles(true, []), step4({ done: null, holders: null, admins: null, reason }))
    expect(lead.classList.contains('is-ok')).toBe(false)
    expect(visible(lead)).toContain('—')
    expect(visible(lead)).toContain(reason)
  })

  it('is "—" with its reason when the setup read carries no config_setup', async () => {
    const lead = await draw(roles(true, []), undefined)
    expect(visible(lead)).toContain('—')
    expect(visible(lead)).toMatch(/does not serve/i)
  })
})

describe('Config › People and roles: the member’s lead (S5g, S5h)', () => {
  it('names who administers the workspace and draws no control', async () => {
    const lead = await draw(roles(false, [person('dana@example.com', 'admin'), person('pat@example.com', 'viewer')]), step4({}))
    expect(visible(lead)).toContain('dana@example.com administers eng')
    expect(visible(lead)).not.toContain('pat@example.com')
    expect(within(lead).queryAllByRole('button')).toHaveLength(0)
    expect(within(lead).queryAllByRole('link')).toHaveLength(0)
    expect(screen.queryByRole('form', { name: /give a person their own role/i })).toBeNull()
  })

  it('names every admin when there are several', async () => {
    const lead = await draw(roles(false, [person('dana@example.com', 'admin'), person('sam@example.com', 'admin')]), step4({}))
    expect(visible(lead)).toContain('dana@example.com and sam@example.com administer eng')
  })

  it('says a platform admin names the first admin when nobody holds admin yet', async () => {
    const lead = await draw(roles(false, [person('pat@example.com', 'viewer')]), step4({}))
    expect(visible(lead)).toMatch(/nobody in eng holds admin yet/i)
    expect(within(lead).queryAllByRole('button')).toHaveLength(0)
  })
})

// THE ROSTER STACKS ON A PHONE. Rendered at 390 px (CONFIG-S5-LEADS review,
// shot dark-phone-04) the roster table was 589 px wide inside the content
// column's scroller: When, Reason and Return to default were off-screen. It
// now uses keys.css's stacking table (`.mcp-servers`, #1680), whose cards
// put each value under its column's name, so every cell carries a label.
describe('Config › People and roles: the roster on a phone', () => {
  it('is the stacking table, every cell labelled with its column', async () => {
    await draw(roles(true, [person('dana@example.com', 'member'), person('lee@example.com', 'viewer')]), step4({}))
    const table = screen.getByRole('table', { name: 'People with their own role' })
    expect(table.classList.contains('mcp-servers')).toBe(true)
    const cells = [...table.querySelectorAll('tbody th, tbody td')]
    expect(cells.length).toBe(12)
    for (const cell of cells) expect(cell.getAttribute('data-label'), cell.textContent ?? '').toMatch(/^(Person|Role|Given by|When|Reason|Actions)$/)
  })
})
