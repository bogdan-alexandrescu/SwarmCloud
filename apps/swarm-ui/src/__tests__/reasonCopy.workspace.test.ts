/**
 * The reason copy speaks of workspaces, not tenants (VQ-056, TENANT-WORDING-C).
 *
 * BGD decided on 2026-10-26 that every reader-facing "tenant" becomes
 * "workspace". `REASON_COPY` is printed at a reader verbatim -- an agent parked
 * CREDENTIAL_MISSING shows its sentence on the agent page and in the timeline --
 * so its keys keep the contract's enum names and its sentences do not.
 *
 * MUTATION: put back "registered for this tenant" (or "Your tenant", or
 * "a member of its tenant") and the matching assertion fails.
 */
import { describe, expect, it } from 'vitest'
import { REASON_COPY, reasonCopy } from '../types'

describe('REASON_COPY names the workspace', () => {
  it('CREDENTIAL_MISSING says no key is registered for this workspace', () => {
    expect(reasonCopy('CREDENTIAL_MISSING')).toBe('No provider key is registered for this workspace.')
  })

  it('TENANT_LIMIT keeps its enum key and says it is your workspace at its limit', () => {
    expect(reasonCopy('TENANT_LIMIT')).toBe('Your workspace is at its own limit. Only an admin can raise it.')
  })

  it('AWAITING_INPUT waits for a member of its workspace', () => {
    expect(reasonCopy('AWAITING_INPUT')).toContain('waits for a member of its workspace to answer')
  })

  it('no sentence in the table says tenant', () => {
    expect(Object.entries(REASON_COPY).filter(([, text]) => /tenant/i.test(text))).toEqual([])
  })
})
