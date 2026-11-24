// The caller's stored role in the selected workspace and the role sentence a
// screen draws beside an action it disables (Profile.tsx's), apart from the
// profile page so the screens that gate an action reach them without loading it.
import { useEffect, useState, useSyncExternalStore } from 'react'
import { loadMe } from './api'
import { chosenTenant, subscribeTenant } from './fetch'
import { atLeast, type RoleName } from './roles'
import type { Me } from './types'

/** `GET /v1/tenants/me` as this build reads it: the selected workspace's stored role, absent from an older API. */
type MeWithRole = Me & { role?: { name?: unknown; source?: unknown } }

/**
 * THE CALLER'S STORED ROLE IN THE SELECTED WORKSPACE, from `GET
 * /v1/tenants/me` (§4.3), read again on every workspace switch. Null while it
 * is read, when the read failed, and when the answer carries no `role`: each
 * is "not known", and an unknown role disables nothing -- the server decides,
 * as it did before the roles build. A known role is never widened.
 */
export function useWorkspaceRole(): { tenant: string; role: string } | null {
  const chosen = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  const [known, setKnown] = useState<{ tenant: string; role: string } | null>(null)
  useEffect(() => {
    let live = true
    setKnown(null)
    void sharedRoleRead(chosen).then((next) => {
      if (live) setKnown(next)
    })
    return () => {
      live = false
    }
  }, [chosen])
  return known
}

/**
 * How long one `GET /v1/tenants/me` answers every gate on the page. A
 * schedule list draws a gate per row, and each asking alone was a read per
 * row; a role an admin changes is drawn within this, and the server decides
 * meanwhile.
 */
const ROLE_READ_REUSE_MS = 30_000

let meRead: { tenant: string | null; at: number; answer: Promise<MeWithRole | null> } | null = null

/**
 * One `GET /v1/tenants/me` for the selected workspace, reused for
 * ROLE_READ_REUSE_MS by every gate and every workspace name on the page.
 */
export function sharedMeRead(chosen: string | null): Promise<MeWithRole | null> {
  const now = Date.now()
  if (meRead !== null && meRead.tenant === chosen && now - meRead.at < ROLE_READ_REUSE_MS) return meRead.answer
  // Called inside a promise, so a read that throws before it returns (not only
  // one that rejects) leaves the role unknown -- no gate, the server decides --
  // instead of an error thrown out of every screen that draws a gate.
  const answer = Promise.resolve().then(() => loadMe({ frame: true })).then(
    (r) => (r.status === 'ok' || r.status === 'stale' ? (r.data as MeWithRole) : null),
    () => null,
  )
  meRead = { tenant: chosen, at: now, answer }
  return answer
}

function sharedRoleRead(chosen: string | null): Promise<{ tenant: string; role: string } | null> {
  return sharedMeRead(chosen).then((me) => {
    const name = me?.role?.name
    if (me === null || typeof name !== 'string' || name === '') return null
    return { tenant: me.tenant?.tenant_id ?? chosen ?? 'this workspace', role: name }
  })
}

/**
 * THE ROLE SENTENCE (§4.8): "Your role in eng is viewer; members decide here"
 * when the caller's stored role is below `floor`, else null. A screen
 * disables the action and draws this beside it. The API is the gate; this
 * only stops offering a click it would refuse (`role_member_required`).
 */
export function useRoleGate(floor: RoleName, verb: string): string | null {
  const known = useWorkspaceRole()
  if (known === null || atLeast(known.role, floor)) return null
  return `Your role in ${known.tenant} is ${known.role}; ${floor}s ${verb} here.`
}
