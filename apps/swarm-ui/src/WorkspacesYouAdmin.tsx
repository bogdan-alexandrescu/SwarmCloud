/**
 * ADMIN › WORKSPACES YOU ADMIN (NAV-ADMIN-ROLE; part of #1633, owner decision
 * 2026-10-19 "All under Profile; Admin by role"; docs/design/workspace-roles.md
 * §4.8).
 *
 * A workspace admin had no place that said which workspaces were theirs: the
 * Admin item was a platform admin's, locked for everyone else, and a group
 * workspace's settings were reachable only by selecting it first in the tenant
 * block and then finding the page under Profile. This page lists each GROUP
 * workspace whose stored role for the caller is `admin` (`adminWorkspaces`),
 * by name, with its four admin pages.
 *
 * EACH LINK SELECTS ITS WORKSPACE FIRST, then navigates: `chooseTenant`, the
 * same switch the tenant block makes, so the page it lands on reads that
 * workspace and the shell's switch rules (a kept form's banner, the reach
 * banner) apply exactly as they do there. A link opened in a new tab carries
 * only the address; that tab acts in whatever workspace it selects.
 *
 * EACH WORKSPACE CARRIES ITS OWN SETUP PROGRESS (CONFIG-PANEL-E; part of
 * #2157, proposal E S10a): the setup strip's count, segments and next step,
 * read for that workspace (`loadWorkspaceSetup`, which names it in
 * `X-Swarm-Tenant` without switching the one in hand), so an admin of several
 * sees which one is behind. A read that failed draws "not read", never done.
 *
 * IT LEADS WITH THE WORKSPACE FURTHEST BEHIND (CONFIG-ADMIN-POLISH; part of
 * #2157, S10a): the Config pages' lead (`AdminLead`, as Pool limits and
 * Tenants draw it) naming it, its next step, and one button, "Open <X>'s
 * Config", which selects it and opens that step's Config page. Its links are
 * the Config tabs' own names and addresses, since Config replaced the
 * Settings pages (CONFIG-NAV): Workspace Settings, MCP servers, People and
 * roles, Secrets.
 *
 * THE ROLE IS THE ONE THE API ACTS ON (VQ-044; part of #2391): each entry's
 * `role` is `RoleStore.resolve`'s answer, which is `admin` for a platform admin
 * in every group workspace they are in, whatever their stored grant says. So
 * the page never calls it a stored role, and marks such a row "admin as a
 * platform admin", the words People and roles uses for the same answer.
 *
 * Personal workspaces are not listed: everyone administers their own, and its
 * settings are under Profile. Neither are a platform admin's reach entries,
 * which are workspaces they can reach, not ones they were made admin of.
 */
import { useEffect, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { loadMyTenants } from './api'
import { SetupProgress, behindOf, setupSteps } from './ConfigSetup'
import { loadWorkspaceSetup } from './configSetupApi'
import type { Result } from './fetch'
import type { OnboardingDoc } from './types'
import { ButtonLink, EmptyState, routedClick } from './components'
import { chooseTenant } from './fetch'
import { AdminLead } from './AdminSettings'
import { CONFIG_SECRETS_ADDRESS, addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { WORKSPACE_PEOPLE_ADDRESS } from './roles'
import { PageHead } from './Shell'
import { adminWorkspaces, nameTenants } from './Spine'

/** The four Config pages a workspace admin changes, by their tab names (S10a). */
export const WORKSPACE_ADMIN_LINKS: readonly { label: string; to: string }[] = [
  { label: 'Workspace Settings', to: 'config/workspace' },
  { label: 'MCP servers', to: 'config/mcp-servers' },
  { label: 'People and roles', to: WORKSPACE_PEOPLE_ADDRESS },
  // Workspace secrets and the workspace key (Keys for agents and MCP).
  { label: 'Secrets', to: CONFIG_SECRETS_ADDRESS },
]

/** Where "Open <X>'s Config" lands when no step is next: Config's first page, as `/config` does. */
const CONFIG_LANDING = 'config/github'

function NoneToAdmin() {
  return (
    <EmptyState kind="empty" heading="You administer no group workspace">
      No group workspace names you as its admin. Your personal workspace is yours to change, under Profile.
    </EmptyState>
  )
}

/** Each listed workspace's setup read, by tenant id; absent while in flight. */
function useWorkspaceSetups(ids: readonly string[]): Readonly<Record<string, Result<OnboardingDoc>>> {
  const [reads, setReads] = useState<Record<string, Result<OnboardingDoc>>>({})
  const key = ids.join('\n')
  useEffect(() => {
    const controller = new AbortController()
    setReads({})
    for (const id of key === '' ? [] : key.split('\n')) {
      void loadWorkspaceSetup(id, controller.signal).then((r) => {
        if (!controller.signal.aborted) setReads((was) => ({ ...was, [id]: r }))
      })
    }
    return () => controller.abort()
  }, [key])
  return reads
}

const docOf = (r: Result<OnboardingDoc> | undefined): OnboardingDoc | null => (r !== undefined && (r.status === 'ok' || r.status === 'stale') ? r.data : null)

function AdminList({ choices, open }: { choices: Parameters<typeof adminWorkspaces>[0]; open: (e: ReactMouseEvent, tenantId: string, to: string) => void }) {
  const listed = adminWorkspaces(choices)
  // `nameTenants` keeps no role, so whose admin role is a platform admin's is read before naming.
  const asPlatformAdmin = new Set(listed.filter((c) => c.role?.source === 'platform_admin').map((c) => c.tenant_id))
  const named = nameTenants(listed, null)
  const setups = useWorkspaceSetups(named.map((w) => w.tenant_id))
  if (named.length === 0) return <NoneToAdmin />
  // Every listed workspace is a group one, so each has four steps.
  const rows = named.map((w) => ({ w, read: setups[w.tenant_id], steps: setupSteps(docOf(setups[w.tenant_id]), false) }))
  const behind = behindOf(rows.filter((r) => docOf(r.read) !== null))
  const behindNext = behind === null ? null : (behind.steps.find((s) => s.state !== 'done' && s.state !== 'unknown') ?? null)
  return (
    <>
      {behind !== null && (
        <AdminLead
          tone="next"
          kicker="Your workspaces"
          title={`${behind.w.display_name} is behind`}
          body={behindNext !== null ? `Next: ${behindNext.label.toLowerCase()} (${behindNext.line}).` : undefined}
          action={
            <ButtonLink kind="primary" href={addressToPath(behindNext?.to ?? CONFIG_LANDING)} onClick={(e) => open(e, behind.w.tenant_id, behindNext?.to ?? CONFIG_LANDING)}>
              Open {behind.w.display_name}’s Config
            </ButtonLink>
          }
        />
      )}
      <ul className="wya-list">
        {rows.map(({ w, read, steps }) => (
          <li key={w.tenant_id} className="wya-row" data-tenant={w.tenant_id}>
            <span className="wya-name">
              <b>{w.display_name}</b>
              <small>{w.tenant_id}</small>
              {asPlatformAdmin.has(w.tenant_id) && <small className="wya-reading" data-via="platform_admin">admin as a platform admin</small>}
            </span>
            <nav className="wya-links" aria-label={`${w.display_name} settings`}>
              {WORKSPACE_ADMIN_LINKS.map((l) => (
                <a key={l.to} href={addressToPath(l.to)} onClick={(e) => open(e, w.tenant_id, l.to)}>
                  {l.label}
                </a>
              ))}
            </nav>
            <div className="wya-setup">
              {read === undefined ? (
                <small className="wya-reading">Reading setup…</small>
              ) : read.status === 'error' ? (
                <small className="wya-reading">Setup not read: {read.error.message}</small>
              ) : (
                <SetupProgress name={w.display_name} steps={steps} onNext={(s, e) => open(e, w.tenant_id, s.to)} />
              )}
            </div>
          </li>
        ))}
      </ul>
    </>
  )
}

export function WorkspacesYouAdminScreen({ go }: { go: (to: string) => void }) {
  const mine = useUrRead(loadMyTenants, 'mine')
  const open = (e: ReactMouseEvent, tenantId: string, to: string) => {
    if (!routedClick(e)) return
    e.preventDefault()
    chooseTenant(tenantId)
    go(to)
  }
  return (
    <div className="ur-page wya-page">
      <PageHead title="Workspaces you admin">
        <UrRefresh reads={[mine]} />
      </PageHead>
      <p className="ur-sub">Each group workspace where your role is admin, given to you by name or held as a platform admin, with its own setup progress. A link selects that workspace, then opens the page.</p>
      <UrRegion state={mine.state} route="GET /v1/tenants/mine" what="Your workspaces" plural onRetry={mine.reload} empty={<NoneToAdmin />}>
        {(choices) => <AdminList choices={choices} open={open} />}
      </UrRegion>
    </div>
  )
}
