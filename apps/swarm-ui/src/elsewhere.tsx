/**
 * A LINK INTO ANOTHER OF YOUR WORKSPACES (the owner's pick of 2026-11-04,
 * "offer to switch"). Seen on the live deployment: a workflow link from the
 * Engineering workspace, opened in a tab acting as the personal workspace,
 * drew `Not found · HTTP 404. No workflow <id> exists in this tenant.` -- true
 * of the workspace in hand, and no help to a person who can read it one
 * switch away.
 *
 * The API answers the same 404 for an id that does not exist and for one in
 * another workspace, and this does not widen that answer. On a 404 the page
 * asks each OTHER workspace the person is a member of -- the profile's
 * workspace list (`GET /v1/profile`), which holds memberships only, never a
 * platform admin's reach into a workspace they are not in -- for the same id:
 * one read each, all at once, each with that workspace's own `X-Swarm-Tenant`,
 * as the switcher sends it. What the answers mean:
 *
 *   - exactly one holds it: "This workflow is in <name>, not <here>.", with
 *     `Switch to <name>` and `Back to the list`. NOTHING SWITCHES WITHOUT THE
 *     CLICK; the click switches as the switcher does, with its notice, and the
 *     page's reads run again under the new workspace (`Screen` re-reads on a
 *     switch, and the shell remounts the page);
 *   - none holds it and every read answered: the page's own 404, unchanged;
 *   - anything else -- two holders (ids are unique, so a read misled) or a
 *     read that failed -- is said as "could not tell", each candidate a
 *     button, and each failed read said with its own failure, never counted as
 *     an absence.
 *
 * Names are the workspaces' display names as the switcher draws them
 * (`nameTenants`), never a group's email.
 */
import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import { USE_FIXTURES, loadMe, loadMyTenants, loadProfile, type TenantChoice } from './api'
import { Banner, Button, acknowledge } from './components'
import { chooseTenant, chosenTenant, errorHeading, noteTenantSwitch, read, subscribeTenant, type ApiError, type ApiRoute } from './fetch'
import { OWN_WORKSPACE_LABEL, displayNameOf, isReach, nameTenants } from './Spine'
import type { Me, ProfileWorkspace } from './types'

/** One workspace, by its id and the name the switcher gives it. */
export interface WorkspaceSide {
  tenant_id: string
  name: string
}

/** A workspace whose read of the id failed: not an absence. */
export interface FailedAsk {
  workspace: WorkspaceSide
  error: ApiError
}

/** What asking the person's other workspaces found. */
export type Elsewhere =
  | { kind: 'looking' }
  /** Every other workspace answered 404, or there is no other. */
  | { kind: 'nowhere' }
  | { kind: 'found'; here: WorkspaceSide; there: WorkspaceSide }
  /** More than one could hold it: two answered, or a read failed. */
  | { kind: 'unsure'; here: WorkspaceSide; holders: WorkspaceSide[]; failed: FailedAsk[] }
  /** The person's own workspace list could not be read, so nothing was asked. */
  | { kind: 'unlisted'; error: ApiError | null }

/**
 * THE NAME A WORKSPACE IS DRAWN BY: the person's own is "Your workspace", as
 * in the switcher; a group's is `/v1/tenants/mine`'s stored name, then the
 * name this browser last saw (`nameTenants`), then the profile's name if it is
 * not an address, then its id. Never an email.
 */
function namer(mine: readonly TenantChoice[], me: Me | null): (w: ProfileWorkspace) => string {
  const named = nameTenants(mine, me)
  return (w) => {
    if (w.kind === 'user') return OWN_WORKSPACE_LABEL
    const n = named.find((c) => c.tenant_id === w.tenant_id)
    return n?.display_name ?? displayNameOf(w.display_name) ?? w.tenant_id
  }
}

/**
 * Ask every other workspace of the person's for `target`, one read each, in
 * parallel. `target` is the same route the page read and was answered 404.
 */
export async function lookElsewhere(target: ApiRoute): Promise<Exclude<Elsewhere, { kind: 'looking' }>> {
  // The fixtures serve one workspace; there is nowhere else to ask.
  if (USE_FIXTURES) return { kind: 'nowhere' }
  const [profile, mine, me] = await Promise.all([loadProfile(), loadMyTenants(), loadMe({ frame: true })])
  if (profile.status !== 'ok' && profile.status !== 'stale') {
    return { kind: 'unlisted', error: profile.status === 'error' ? profile.error : null }
  }
  const choices = mine.status === 'ok' || mine.status === 'stale' ? mine.data : []
  const name = namer(choices, me.status === 'ok' || me.status === 'stale' ? me.data : null)
  const side = (w: ProfileWorkspace): WorkspaceSide => ({ tenant_id: w.tenant_id, name: name(w) })
  const chosen = chosenTenant()
  const acting = profile.data.workspaces.find((w) => w.is_acting) ?? profile.data.workspaces.find((w) => w.tenant_id === chosen)
  const here: WorkspaceSide = acting === undefined ? { tenant_id: chosen ?? '', name: 'this workspace' } : side(acting)
  // A platform admin's reach is never a membership; the profile does not list
  // it, and this holds that even against an API that did.
  const reach = new Set(choices.filter(isReach).map((c) => c.tenant_id))
  const others = profile.data.workspaces.filter(
    (w) => w.selectable && !w.is_acting && w.tenant_id !== here.tenant_id && w.tenant_id !== chosen && !reach.has(w.tenant_id),
  )
  const answers = await Promise.all(
    others.map(async (w) => ({ w, r: await read<unknown>(target, () => false, { tenant: w.tenant_id, frame: true }) })),
  )
  const holders: WorkspaceSide[] = []
  const failed: FailedAsk[] = []
  for (const { w, r } of answers) {
    if (r.status === 'ok' || r.status === 'stale' || r.status === 'empty') holders.push(side(w))
    else if (r.status === 'error' && r.error.kind !== 'not_found') failed.push({ workspace: side(w), error: r.error })
  }
  const only = holders[0]
  if (holders.length === 1 && only !== undefined) return { kind: 'found', here, there: only }
  if (holders.length === 0 && failed.length === 0) return { kind: 'nowhere' }
  return { kind: 'unsure', here, holders, failed }
}

/**
 * `lookElsewhere` for the page in hand, or null while `target` is null (the
 * page's own read did not answer 404). Asked once per id and workspace in
 * hand: the page's polls do not ask again.
 */
export function useElsewhere(target: ApiRoute | null): Elsewhere | null {
  const chosen = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  const url = target?.url ?? null
  const [found, setFound] = useState<{ key: string; answer: Elsewhere } | null>(null)
  const key = `${chosen ?? ''}\n${url ?? ''}`
  useEffect(() => {
    if (target === null) return
    let live = true
    setFound({ key, answer: { kind: 'looking' } })
    void lookElsewhere(target).then((answer) => {
      if (live) setFound({ key, answer })
    })
    return () => {
      live = false
    }
    // `target` is rebuilt on each render; its URL is what it asks.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])
  if (url === null) return null
  return found !== null && found.key === key ? found.answer : { kind: 'looking' }
}

/**
 * SWITCH THIS TAB AS THE SWITCHER DOES (Spine.tsx `choose`): the switch is
 * noted for the page it was made on, the choice is made, and the same notice
 * says so, with the way back.
 */
export function switchWorkspace(from: WorkspaceSide, to: WorkspaceSide): void {
  noteTenantSwitch({ from: { id: from.tenant_id, name: from.name }, to: { id: to.tenant_id, name: to.name }, kept: false })
  chooseTenant(to.tenant_id)
  acknowledge(
    <>
      Now acting as <b>{to.name}</b>. This page’s reads are reloading.
    </>,
    from.tenant_id === ''
      ? undefined
      : {
          label: `Back to ${from.name}`,
          onClick: () => {
            noteTenantSwitch({ from: { id: to.tenant_id, name: to.name }, to: { id: from.tenant_id, name: from.name }, kept: false })
            chooseTenant(from.tenant_id)
          },
        },
  )
}

/** The way back: a link to the list, or the drawer's own close. */
export type BackToList = { href: string } | { onClick: () => void }

function BackLink({ back }: { back: BackToList }) {
  return 'href' in back ? (
    <a href={back.href}>Back to the list</a>
  ) : (
    <Button kind="ghost" onClick={back.onClick}>
      Back to the list
    </Button>
  )
}

/**
 * WHAT A 404 PAGE DRAWS once its other workspaces were asked. `notFound` is
 * the page's own 404 sentence, drawn unchanged when no other workspace holds
 * the id, and while they are being asked. `what` is the noun: "workflow",
 * "agent". `onSwitched` runs after a switch, for a page that re-reads itself.
 */
export function ElsewhereNotice({
  found,
  what,
  back,
  notFound,
  onSwitched,
}: {
  found: Elsewhere
  what: string
  back: BackToList
  notFound: ReactNode
  onSwitched?: () => void
}) {
  const go = (here: WorkspaceSide, to: WorkspaceSide) => {
    switchWorkspace(here, to)
    onSwitched?.()
  }
  if (found.kind === 'looking') {
    return (
      <>
        {notFound}
        <p className="muted small" role="status" data-elsewhere="looking">
          Looking for it in your other workspaces…
        </p>
      </>
    )
  }
  if (found.kind === 'nowhere') return <>{notFound}</>
  if (found.kind === 'unlisted') {
    return (
      <>
        {notFound}
        <p className="muted small" role="status" data-elsewhere="unlisted">
          Your other workspaces could not be read
          {found.error === null ? '' : ` (${errorHeading(found.error)}: ${found.error.message})`}, so whether one of them
          holds this {what} is not known.
        </p>
      </>
    )
  }
  if (found.kind === 'found') {
    const { here, there } = found
    return (
      <div className="tenant-switch-offer" data-elsewhere="found" style={{ marginTop: 'var(--ctl-s3)' }}>
        <Banner
          tone="info"
          role="status"
          title={`This ${what} is in ${there.name}, not ${here.name}.`}
          actions={
            <>
              <Button kind="primary" onClick={() => go(here, there)}>
                {`Switch to ${there.name}`}
              </Button>
              <BackLink back={back} />
            </>
          }
        >
          It is one of your workspaces; this one answers as it would for a {what} that does not exist.
        </Banner>
      </div>
    )
  }
  const { here, holders, failed } = found
  const candidates = [...holders, ...failed.map((f) => f.workspace)]
  return (
    <div className="tenant-switch-offer" data-elsewhere="unsure" style={{ marginTop: 'var(--ctl-s3)' }}>
      <Banner
        tone="warn"
        role="status"
        title={`Could not tell which of your workspaces holds this ${what}.`}
        actions={
          <>
            {candidates.map((w) => (
              <Button key={w.tenant_id} onClick={() => go(here, w)}>
                {w.name}
              </Button>
            ))}
            <BackLink back={back} />
          </>
        }
      >
        {`${here.name} answers HTTP 404.`}
        {holders.length > 1 ? ` ${holders.map((w) => w.name).join(' and ')} each answered with it.` : ''}
        {failed.map((f) => ` Reading it in ${f.workspace.name} failed (${errorHeading(f.error)}: ${f.error.message}).`).join('')}
      </Banner>
    </div>
  )
}
