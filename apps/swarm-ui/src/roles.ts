import type { TenantChoice } from './api'

/**
 * THE ROLE LADDER, AS THE CONSOLE READS IT (docs/design/workspace-roles.md
 * §4.1, §4.12; lane ROLES-UI-PEOPLE, part of #1518).
 *
 * Three roles on one ladder, viewer < member < admin, each permitting what is
 * below it (owner answer Q1). swarm_api/roles.py holds the table of what each
 * permits and serves its sentences on `GET /v1/workspace/roles`; this file
 * holds only the ORDER, which a screen needs to decide what to stop offering.
 * The API is the gate: a control hidden here is a courtesy, never the check.
 *
 * A NAME THIS BUILD DOES NOT KNOW PERMITS NOTHING. A missing role, an empty
 * one, or a name a newer API might serve ranks below `viewer`, so the console
 * never reads an unknown answer as a wider role -- the server's own rule for
 * an unreadable grant (`role_unavailable`, never a wider role).
 */
export const ROLES = ['viewer', 'member', 'admin'] as const

export type RoleName = (typeof ROLES)[number]

/**
 * What a workspace's default may be: never `admin`, so a group workspace has
 * no admin until one is named person by person (§4.6, Q2). The server refuses
 * `admin` (409 `default_role_not_allowed`, roles.py `DEFAULT_ROLES`); the
 * selector offers only these two so that refusal is never one click away.
 */
export const DEFAULT_ROLES: readonly RoleName[] = ['viewer', 'member']

/** The address of a workspace's own roles page, Settings › People and roles (§4.8). */
export const WORKSPACE_PEOPLE_ADDRESS = 'settings/people'

/** A role's place on the ladder, or -1 for a name this build does not know. */
export function rank(role: string | null | undefined): number {
  return (ROLES as readonly string[]).indexOf(role ?? '')
}

/** Whether `role` permits what `floor` permits: `atLeast(me, 'member')` before offering a submit. */
export function atLeast(role: string | null | undefined, floor: RoleName): boolean {
  const r = rank(role)
  return r >= 0 && r >= rank(floor)
}

/**
 * The stored role a `GET /v1/tenants/mine` entry carries, or null when it
 * carries none -- an older API, or a role the server could not read. Null is
 * the cue to show the DERIVED role (§4.11), never a guess at a stored one.
 */
export function roleOf(choice: TenantChoice): string | null {
  const name = choice.role?.name
  return typeof name === 'string' && name !== '' ? name : null
}

/** Where a resolved role came from, in words (`me.source`, `TenantChoice.role.source`). */
export function sourceWords(source: string): string {
  switch (source) {
    case 'override':
      return 'given to you by name'
    case 'default':
      return "the workspace's default"
    case 'personal':
      return 'your personal workspace'
    case 'platform_admin':
      return 'as a platform admin'
    default:
      return source
  }
}
