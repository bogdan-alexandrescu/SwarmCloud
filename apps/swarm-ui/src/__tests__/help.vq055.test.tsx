// VQ-055 (visual QA of 2026-10-26, part of #2391; admin-help.html H1,
// config-e.html): Help had seven groups and none for Config, Automate or
// Admin; searching "secret" found nothing about secret scopes, the add-a-secret
// wizard or the one-time form; and every Config page's `?` opened the same
// section card, whose `Help →` led to Help's first group.
//
// WHAT EACH CASE HOLDS:
//   * Help has a group for each of Automate, Config and Admin, each with topics,
//     and the Config group has a topic per Config page;
//   * the search finds secret scopes, the wizard and the one-time form;
//   * each Config page's `?`, and each Admin page's, says its own topic and
//     `Help →` opens it rather than the top of Help.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { HELP, HELP_GROUPS, TOPIC_IDS, type TopicId } from '../help'
import { HelpScreen } from '../HelpSection'

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

const inGroup = (g: string) => TOPIC_IDS.filter((id) => HELP[id].group === g)
const groupTitled = (word: string) => HELP_GROUPS.find((g) => g.title.startsWith(`${word}:`))

describe('VQ-055: Help has a group for Automate, Config and Admin', () => {
  it('lists the three groups, each with topics', () => {
    for (const word of ['Automate', 'Config', 'Admin']) {
      const g = groupTitled(word)
      // MUTATION: drop a group from HELP_GROUPS and its title is gone.
      expect(g, `no ${word} group`).toBeDefined()
      expect(inGroup(g!.id).length, `the ${word} group is empty`).toBeGreaterThan(0)
    }
  })

  it('keeps every group title short enough for its count to hold the rail column', () => {
    // Review of this lane, 2026-10-26: 'Automate: schedules and approvals'
    // (33 characters) rendered ~199px in the rail's ~181px text track at 1440,
    // pushing its count past the row's edge. 'Config: setting up a workspace'
    // (30) is the longest that fits. MUTATION: restore the 33-character title.
    for (const g of HELP_GROUPS) expect(g.title.length, g.title).toBeLessThanOrEqual(30)
  })

  it('has a Config topic for the setup strip, secret scopes, the wizard, MCP servers, roles and workspace settings', () => {
    const config = inGroup(groupTitled('Config')!.id)
    for (const id of [
      'config-setup-strip',
      'config-github-access',
      'config-repositories',
      'secret-scopes',
      'add-a-secret',
      'mcp-servers',
      'workspace-roles',
      'workspace-settings',
    ] as TopicId[]) {
      expect(config, id).toContain(id)
    }
    expect(inGroup(groupTitled('Automate')!.id)).toEqual(expect.arrayContaining(['schedules', 'approvals']))
    expect(inGroup(groupTitled('Admin')!.id)).toEqual(
      expect.arrayContaining(['runner-sizes', 'platform-secrets', 'workspaces-you-admin', 'tenant-fields', 'platform-counts']),
    )
  })

  it('says workspace, never tenant, in every new topic a person reads', () => {
    for (const id of ['config-setup-strip', 'config-github-access', 'config-repositories', 'secret-scopes', 'add-a-secret', 'mcp-servers', 'workspace-roles', 'workspace-settings', 'runner-sizes', 'platform-secrets', 'workspaces-you-admin'] as TopicId[]) {
      const t = HELP[id]
      expect([t.subject, t.title, t.short, ...t.long, t.act.say].join(' '), id).not.toMatch(/tenant/i)
    }
  })
})

describe('VQ-055: the search finds the secret topics', () => {
  const search = (q: string) => {
    render(<HelpScreen topic="" />)
    fireEvent.change(document.querySelector('.help-search input')!, { target: { value: q } })
    return [...document.querySelectorAll<HTMLAnchorElement>('a.help-hit')].map((a) => a.getAttribute('href'))
  }

  for (const [q, id] of [
    ['secret', 'secret-scopes'],
    ['secret', 'add-a-secret'],
    ['scope', 'secret-scopes'],
    ['wizard', 'add-a-secret'],
    ['one-time form', 'add-a-secret'],
    ['MCP', 'mcp-servers'],
  ] as const) {
    it(`"${q}" finds ${id}`, () => {
      // MUTATION: drop the topic and the search finds only the ten it found before.
      expect(search(q)).toContain(`#${HELP[id].anchor}`)
    })
  }
})

async function cardAt(path: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', path)
  const { App } = await import('../App')
  render(<App />)
  const glyph = await waitFor(() => {
    const g = document.querySelector<HTMLButtonElement>('.ctl-q .ctl-q-glyph')
    expect(g).not.toBeNull()
    return g!
  })
  fireEvent.click(glyph)
  return await waitFor(() => {
    const c = document.querySelector<HTMLElement>('.ctl-q-card')
    expect(c).not.toBeNull()
    return c!
  })
}

describe('VQ-055: each Config and Admin page’s `?` opens its own topic', () => {
  for (const [path, topic] of [
    ['/config/github', 'config-github-access'],
    ['/config/repositories', 'config-repositories'],
    ['/config/secrets', 'secret-scopes'],
    ['/config/mcp-servers', 'mcp-servers'],
    ['/config/people', 'workspace-roles'],
    ['/config/workspace', 'workspace-settings'],
    ['/admin/runner-sizes', 'runner-sizes'],
    ['/admin/platform-secrets', 'platform-secrets'],
  ] as const) {
    it(`on ${path}, says the ${topic} topic and links to it`, async () => {
      const card = await cardAt(path)
      // MUTATION: drop the page from App.tsx `PAGE_TOPIC` and its card links #help, the first group.
      expect(card.querySelector(`[data-topic="${topic}"]`)?.textContent).toBe(HELP[topic].short)
      expect(card.querySelector('a')!.getAttribute('href')).toBe(`#${HELP[topic].anchor}`)
    })
  }
})
