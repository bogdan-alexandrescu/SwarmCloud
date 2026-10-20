// A FIELD IN A COLUMN FORM IS ONE CONTROL HIGH (lane FORM-FIELDS; #1667).
//
// `.pk-input { flex: 1 1 260px }` is a ROW rule: in a `.pk-form-row` the
// 260px basis is the field's width. McpServers' register form (Name, URL,
// Description, the Tools textarea) and WorkspacePeople's role form (Reason)
// put `.pk-input` straight into `.pk-form`, which is `flex-direction:
// column`, where the same basis is the field's HEIGHT: Chrome drew each of
// them 260px tall on 2026-10-20.
//
// WHAT EACH CASE HOLDS, read off the shipped sheets with `painted` (the
// cascade at a stated width, since jsdom applies no `@media` and orders rules
// by source position alone):
//   * every `.pk-form > .pk-input` paints `flex-basis: auto` at desktop and
//     phone width, and none paints 260px;
//   * every `.pk-form-row > .pk-input` still paints 260px at desktop and 100%
//     at phone width;
//   * the Tools textarea paints `height: auto`, so it is its two rows high.
//
// Fixtures only; no network. Nothing here is credential-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { serve } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

type Json = Record<string, unknown>

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 400 }

/**
 * The flex-basis the cascade gives `el`: the winner of `flex` and
 * `flex-basis`, with the basis read out of a `flex` shorthand. `none` and
 * `auto` are `0 0 auto` and `1 1 auto`; a one-value shorthand that is a
 * length is the basis; three values carry it last.
 */
function basis(el: Element, env: CascadeEnv): string | null {
  const v = painted(el, ['flex', 'flex-basis'], env)
  if (v === null) return null
  const parts = v.trim().split(/\s+/)
  if (parts.length === 1) return parts[0] === 'none' ? 'auto' : parts[0]!
  if (parts.length === 3) return parts[2]!
  throw new Error(`cannot read a basis out of \`${v}\``)
}

function mcpList(): Json {
  return {
    tenant_id: 'eng',
    can_manage: true,
    servers: [],
    note: 'swarm-api never calls a registered URL',
    egress_addresses: ['203.0.113.7'],
  }
}

const ROLES_TABLE = [
  { name: 'viewer', summary: 'Sees everything in this workspace; changes nothing.' },
  { name: 'member', summary: 'Submits, approves, answers, cancels, and manages schedules.' },
  { name: 'admin', summary: "A member who also manages the workspace's settings and people's roles." },
]

const ADMIN: Json = {
  tenant_id: 'eng',
  kind: 'group',
  default_role: 'member',
  default_source: 'migration',
  me: { email: 'carol@example.com', via: 'member', role: 'admin', source: 'override' },
  people: [],
  roles: ROLES_TABLE,
  can_manage: true,
  audit: [],
}

async function mountMcp(): Promise<HTMLFormElement> {
  serve((m, url) => (m === 'GET' && url.startsWith('/v1/mcp-servers') ? { status: 200, body: mcpList() } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { McpServersScreen } = await import('../McpServers')
  render(<McpServersScreen />)
  return (await screen.findByRole('form', { name: /register an mcp server/i })) as HTMLFormElement
}

async function mountPeople(): Promise<HTMLButtonElement> {
  serve((m, url) => (m === 'GET' && url === '/v1/workspace/roles' ? { status: 200, body: ADMIN } : null))
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { WorkspacePeopleScreen } = await import('../WorkspacePeople')
  render(<WorkspacePeopleScreen />)
  return (await screen.findByRole('button', { name: 'Set role' })) as HTMLButtonElement
}

/** Both forms, mounted together, so every field below is a shipped one. */
async function mountBoth(): Promise<void> {
  await mountMcp()
  await mountPeople()
}

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  window.history.replaceState(null, '', '/')
})

describe('Settings › a field in a column form is one control high (#1667)', () => {
  it('every .pk-form > .pk-input paints flex-basis auto, and none paints 260px', async () => {
    await mountBoth()
    const fields = [...document.querySelectorAll<HTMLElement>('.pk-form > .pk-input')]
    // Name, URL, Description, Tools (McpServers) and Reason (WorkspacePeople).
    expect(fields.length).toBeGreaterThanOrEqual(5)
    for (const f of fields) {
      // MUTATION: delete `.pk-form > .pk-input { flex: none; }` from keys.css
      // and `.pk-input { flex: 1 1 260px }` paints 260px here: red.
      expect(basis(f, WIDE), `${f.id} at desktop`).toBe('auto')
      expect(basis(f, WIDE), `${f.id} at desktop`).not.toBe('260px')
      // MUTATION: lower the column rule to `.pk-input` specificity (or move
      // it into the 719px block as `flex-basis: 100%`) and the phone rule
      // paints 100% here, which in a column is the form's whole height: red.
      expect(basis(f, PHONE), `${f.id} at phone`).toBe('auto')
    }
  })

  it('every .pk-form-row > .pk-input still paints 260px at desktop and 100% at phone', async () => {
    await mountBoth()
    const fields = [...document.querySelectorAll<HTMLElement>('.pk-form-row > .pk-input')]
    // At least WorkspacePeople's Email field, beside the Role selector.
    expect(fields.length).toBeGreaterThanOrEqual(1)
    for (const f of fields) {
      // MUTATION: write the fix as `.pk-input { flex: none; }` (every field,
      // rows included) and a row field stops taking the row's width: red.
      expect(basis(f, WIDE), `${f.id} at desktop`).toBe('260px')
      // MUTATION: drop `flex-basis: 100%` from `.pk-input` in the 719px block
      // and a row field shares a phone's line with its buttons again: red.
      expect(basis(f, PHONE), `${f.id} at phone`).toBe('100%')
    }
  })

  it('the Tools textarea paints height auto, so it is as tall as its rows', async () => {
    const form = await mountMcp()
    const tools = form.querySelector<HTMLTextAreaElement>(':scope > textarea.pk-input')
    expect(tools).not.toBeNull()
    expect(tools!.rows).toBe(2)
    for (const env of [WIDE, PHONE]) {
      // MUTATION: delete `.pk-form > textarea.pk-input { height: auto; ... }`
      // from keys.css and `.ur-search { height: var(--h-ctl) }` clips the two
      // rows to one control: red.
      expect(painted(tools!, 'height', env)).toBe('auto')
    }
  })
})
