// QA2-FIX-17 (part of #2694, the console visual QA pass 2 epic, and #2391):
// the findings whose main file is McpServers.tsx or Notifications.tsx.
//
//   config2-19  Config › MCP servers broke a URL mid-word
//               ('https://mcp.exampl / e.com/docs/mcp'): it breaks only after
//               a '/' or a '.', and the column keeps its widest piece.
//   config2-20  at 390 the highlighted kb row was padded on the left only, so
//               its Disable button met the tint's right edge.
//   runs2-29    an MCP server on Start an agent was a hidden checkbox in a
//               borderless segment: an unpicked one read as a sentence.
//   admin2-09   Tab walked out of the open Notifications dialog onto the page
//               behind it, the dialog still drawn: focus leaving closes it.
//   admin2-17   '1 notification need you': one needs, two need.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

type Json = Record<string, unknown>

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }
const WAIT = { timeout: 4000 }

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

// ---------------------------------------------------------------------------
// Config › MCP servers
// ---------------------------------------------------------------------------

function server(name: string, url: string, mode: string, description = ''): Json {
  return {
    name, url, description, enabled: true, revision: 1,
    auth: { mode, header: 'Authorization', scheme: 'Bearer' }, allowed_profiles: [], allowed_tools: [],
    consumer: `mcp:${name}`, updated_at: null, updated_by: null, last_attached_at: null,
  }
}

const LIST: Json = {
  tenant_id: 'eng', can_manage: true, egress_addresses: [], last_attached_complete: true,
  servers: [
    server('kb', 'https://mcp.example.com/docs/mcp', 'workspace_secret', 'Answers from the team knowledge base.'),
    server('status', 'https://status.example.com/mcp', 'none'),
  ],
}

const OVERVIEW: Json = {
  tenant_id: 'eng', scope: 'workspace', repo_id: null, repository: null, personal_tenant_id: null, can_manage: true,
  rows: [
    { section: 'keys', store: 'mcp_servers', name: 'kb', consumer: 'mcp:kb', secrets: [], status: 'unset', set_at: null, set_by: null, consumers: ['mcp:kb'], can_manage: true },
  ],
}

async function drawServers(): Promise<HTMLTableElement> {
  serve((m, url) => {
    if (m === 'GET' && url.startsWith('/v1/mcp-servers')) return { status: 200, body: LIST }
    if (m === 'GET' && url.startsWith('/v1/secrets/overview')) return { status: 200, body: OVERVIEW }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  const { SetupContext } = await import('../ConfigSetup')
  render(
    <SetupContext.Provider value={{ doc: { status: 'ok', data: { tenant_id: 'eng', steps: [] } as never, fetchedAt: Date.now() }, personal: false }}>
      <McpServersScreen />
    </SetupContext.Provider>,
  )
  return (await screen.findByRole('table', { name: 'Registered MCP servers' })) as HTMLTableElement
}

const row = (table: HTMLTableElement, name: string) =>
  [...table.querySelectorAll<HTMLTableRowElement>('tbody tr')].find((tr) => tr.querySelector('th code')?.textContent === name)!

describe('config2-19: a URL breaks after a "/" or a ".", never mid-word', () => {
  it('offers a break after each "/" and "." and lets the column keep its widest piece', async () => {
    const table = await drawServers()
    const code = row(table, 'kb').querySelector('td[data-label="URL"] > code')!
    // The text, and so a copy of it, is the URL itself.
    expect(code.textContent).toBe('https://mcp.example.com/docs/mcp')
    expect(code.getAttribute('title')).toBe('https://mcp.example.com/docs/mcp')
    // MUTATION: draw `{s.url}` again -> no break opportunity but anywhere: red.
    const pieces = [...code.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent)
    expect(pieces).toEqual(['https://', 'mcp.', 'example.', 'com/', 'docs/', 'mcp'])
    expect(code.querySelectorAll('wbr').length).toBe(5)
    // MUTATION: drop keys.css' URL `overflow-wrap: break-word` -> 'anywhere', the column squeezed to a letter: red.
    expect(painted(code.parentElement!, 'overflow-wrap', WIDE)).toBe('break-word')
  })
})

describe('config2-20: the highlighted stacked row is padded the same on both sides', () => {
  it('pads kb’s needed card 10px left and right at 390', async () => {
    const table = await drawServers()
    const kb = row(table, 'kb')
    await waitFor(() => expect(kb.classList.contains('is-need')).toBe(true), WAIT)
    // MUTATION: put back `padding-left: 10px` alone -> the right side is the base row's 0: red.
    const pad = painted(kb, ['padding', 'padding-left', 'padding-right'], PHONE) ?? ''
    const parts = pad.trim().split(/\s+/)
    expect(parts.length, `padding is ${pad}`).toBe(2)
    expect(parts[1]).toBe('10px')
  })
})

describe('runs2-29: an MCP server on Start an agent looks like a checkbox, on or off', () => {
  async function picker(picked: string[]) {
    vi.resetModules()
    const { McpServerPicker } = await import('../McpServers')
    const list = { status: 'ok', fetchedAt: Date.now(), data: LIST } as never
    render(
      <McpServerPicker list={list} profile="claude-code" max={4} picked={picked} onPick={() => {}}
        fields={{} as never} filled={new Set()} onFilled={() => {}} lifetimes={{}} onLifetime={() => {}} />,
    )
    return [...document.querySelectorAll<HTMLLabelElement>('label.btp-opt')]
  }

  it('draws a box around each option, one colour all round, and its checkbox', async () => {
    const [kb, status] = await picker(['status'])
    expect(kb).toBeDefined()
    expect(status!.classList.contains('is-on')).toBe(true)
    // MUTATION: drop `mcp-opt` from the label, or its rule from keys.css -> no border, a lighter right one: red.
    for (const opt of [kb!, status!]) {
      expect(painted(opt, ['border', 'border-right'], WIDE)).toMatch(/^1px solid /)
      expect(painted(opt, ['border', 'border-right'], { width: 390 })).toMatch(/^1px solid /)
    }
    const box = kb!.querySelector('input[type="checkbox"]')!
    // MUTATION: drop the input rule -> submit.css' `opacity: 0`, the check unseen: red.
    expect(painted(box, 'opacity', WIDE)).toBe('1')
    expect(painted(box, 'position', WIDE)).toBe('static')
  })
})

// ---------------------------------------------------------------------------
// The notifications bell
// ---------------------------------------------------------------------------

function note(id: string, needs: boolean): Json {
  return { id, group: 'task', needs_you: needs, title: `item ${id}`, detail: null, href: '/agents', at: new Date().toISOString(), read: false }
}

async function mountBell(items: Json[]) {
  const needs = items.filter((i) => i.needs_you === true).length
  serve((m, url) =>
    m === 'GET' && url.split('?')[0] === '/v1/notifications'
      ? { status: 200, body: { items, unread_needs_you: needs, unread_updates: items.length - needs, admin_pending: 0, read_at: new Date().toISOString() } }
      : null,
  )
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { NotificationBell } = await import('../Notifications')
  render(
    <>
      <NotificationBell go={vi.fn()} />
      <button type="button">Help</button>
    </>,
  )
  await waitFor(() => expect(document.querySelector('button.nt-bell')).not.toBeNull(), WAIT)
  return document.querySelector<HTMLButtonElement>('button.nt-bell')!
}

const panel = () => document.querySelector<HTMLElement>('.nt-panel')

describe('admin2-17: the count agrees with its verb', () => {
  it('says "1 notification needs you"', async () => {
    await mountBell([note('a', true)])
    // MUTATION: put back `need you` for every count -> '1 notification need you': red.
    await waitFor(() => expect(document.querySelector('.nt [aria-live="polite"]')?.textContent).toBe('1 notification needs you'), WAIT)
  })

  it('says "2 notifications need you"', async () => {
    await mountBell([note('a', true), note('b', true)])
    await waitFor(() => expect(document.querySelector('.nt [aria-live="polite"]')?.textContent).toBe('2 notifications need you'), WAIT)
  })
})

describe('admin2-09: focus leaving the open panel closes it', () => {
  it('closes when Tab moves focus to the page behind, and leaves focus there', async () => {
    const bell = await mountBell([note('a', true)])
    fireEvent.click(bell)
    await waitFor(() => expect(panel()).not.toBeNull(), WAIT)
    // Focus inside the panel keeps it open.
    act(() => within(panel()!).getByRole('button', { name: 'Mark all read' }).focus())
    expect(panel()).not.toBeNull()
    const help = screen.getByRole('button', { name: 'Help' })
    // MUTATION: drop the `focusin` listener -> the dialog stays open over Help: red.
    act(() => help.focus())
    await waitFor(() => expect(panel()).toBeNull(), WAIT)
    expect(document.activeElement).toBe(help)
    expect(bell.getAttribute('aria-expanded')).toBe('false')
  })
})
