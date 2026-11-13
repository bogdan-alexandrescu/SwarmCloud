// ADMIN › SETUP › ADD A TEAM: the printed `swarm team add` line and the
// invites' members page.
//
// WHAT EACH CASE HOLDS:
//   * the line carries only the flags swarm_install/verbs/team.py defines:
//     the group, `--project` (required there, a placeholder here, since the
//     page does not know the project), `--display-name` and `--first-admin`;
//     never `--name`, `--admin` or `--invite`, which that parser refuses
//     (the flags are read from the Python source, so a parser change turns
//     this red);
//   * once a team with invites is asked for, the form links the group's
//     members page in Google Admin the route answered with, where the
//     invites are added; with no invite there is no link.
//
// Fixtures only; no network.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'
import { teamAddLine } from '../AdminSetupTeam'

const TEAM_VERB = join(__dirname, '..', '..', '..', 'swarm-install', 'swarm_install', 'verbs', 'team.py')
const MEMBERS = 'https://admin.google.com/ac/groups/research%40example.com/members'

/** The long flags `swarm team add` defines, read from its parser. */
function addFlags(): Set<string> {
  const src = readFileSync(TEAM_VERB, 'utf8')
  const add = src.slice(src.indexOf('add = sub.add_parser("add"'), src.indexOf('repair = sub.add_parser('))
  return new Set([...add.matchAll(/add\.add_argument\("(--[a-z-]+)"/g)].map((m) => m[1] ?? ""))
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
})

describe('the swarm team add line', () => {
  it('uses only the flags swarm team add defines, --project among them', () => {
    const line = teamAddLine({ group: 'research@example.com', name: 'Research', admin: 'lea@example.com', invites: 'ravi@example.com' })
    // MUTATION: print --name / --admin / --invite again, or drop --project -> fails.
    expect(line).toBe('swarm team add research@example.com --project=<project-id> --display-name Research --first-admin lea@example.com')
    const defined = addFlags()
    expect(defined.has('--project')).toBe(true)
    for (const flag of line.match(/--[a-z-]+/g) ?? []) expect(defined.has(flag), flag).toBe(true)
  })

  it('names the values it cannot fill as placeholders', () => {
    expect(teamAddLine({ group: '', name: '', admin: '', invites: '' })).toBe('swarm team add <group> --project=<project-id>')
  })
})

describe('the invites’ members page', () => {
  const answer = (invites: string[]) =>
    serve((method, url) =>
      method === 'POST' && url === '/v1/admin/setup/teams'
        ? { status: 200, body: { team: 'research', invites: invites.map((email) => ({ email, add_members_url: MEMBERS, member: false })) } }
        : null,
    )
  // Live, so the POST reaches the served route rather than the fixture build's answer.
  const add = async (invites: string) => {
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const mod = await import('../AdminSetupTeam')
    render(<mod.AdminSetupTeam teams={[]} />)
    const flow = screen.getByRole('region', { name: 'Add a team' })
    fireEvent.change(within(flow).getByLabelText(/Workspace group/), { target: { value: 'research@example.com' } })
    fireEvent.change(within(flow).getByLabelText(/Invite/), { target: { value: invites } })
    fireEvent.click(within(flow).getByRole('button', { name: /Add the team/ }))
    return flow
  }

  it('links the group’s members page in Google Admin once the team is asked for', async () => {
    answer(['ravi@example.com'])
    const flow = await add('ravi@example.com')
    await waitFor(() => expect(within(flow).getByRole('status').textContent).toMatch(/research was asked for/))
    // MUTATION: keep only r.data.team and drop the invites' add_members_url -> no link, fails.
    const link = within(flow).getByRole('link', { name: 'Open the group in Google Admin' })
    expect(link.getAttribute('href')).toBe(MEMBERS)
    expect(link.getAttribute('target')).toBe('_blank')
  })

  it('draws no link when nobody was invited', async () => {
    answer([])
    const flow = await add('')
    await waitFor(() => expect(within(flow).getByRole('status').textContent).toMatch(/research was asked for/))
    expect(within(flow).queryByRole('link', { name: 'Open the group in Google Admin' })).toBeNull()
  })
})
