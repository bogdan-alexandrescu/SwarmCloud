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
 * Personal workspaces are not listed: everyone administers their own, and its
 * settings are under Profile. Neither are a platform admin's reach entries,
 * which are workspaces they can reach, not ones they were made admin of.
 */
import type { MouseEvent as ReactMouseEvent } from 'react'
import { loadMyTenants } from './api'
import { EmptyState, routedClick } from './components'
import { chooseTenant } from './fetch'
import { addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { WORKSPACE_PEOPLE_ADDRESS } from './roles'
import { PageHead } from './Shell'
import { adminWorkspaces, nameTenants } from './Spine'

/** The four pages a workspace admin changes, in the Profile panel's order. */
export const WORKSPACE_ADMIN_LINKS: readonly { label: string; to: string }[] = [
  { label: 'Workspace', to: 'settings/workspace' },
  { label: 'MCP servers', to: 'settings/mcp-servers' },
  { label: 'People and roles', to: WORKSPACE_PEOPLE_ADDRESS },
  // API keys' tenant row is the workspace key; the person's own key above it is theirs.
  { label: 'Workspace key', to: 'settings/keys' },
]

function NoneToAdmin() {
  return (
    <EmptyState kind="empty" heading="You administer no group workspace">
      No group workspace names you as its admin. Your personal workspace is yours to change, under Profile.
    </EmptyState>
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
      <p className="ur-sub">Each group workspace whose stored role for you is admin. A link selects that workspace, then opens the page.</p>
      <UrRegion state={mine.state} route="GET /v1/tenants/mine" what="Your workspaces" plural onRetry={mine.reload} empty={<NoneToAdmin />}>
        {(choices) => {
          const named = nameTenants(adminWorkspaces(choices), null)
          if (named.length === 0) return <NoneToAdmin />
          return (
            <ul className="wya-list">
              {named.map((w) => (
                <li key={w.tenant_id} className="wya-row" data-tenant={w.tenant_id}>
                  <span className="wya-name">
                    <b>{w.display_name}</b>
                    <small>{w.tenant_id}</small>
                  </span>
                  <nav className="wya-links" aria-label={`${w.display_name} settings`}>
                    {WORKSPACE_ADMIN_LINKS.map((l) => (
                      <a key={l.to} href={addressToPath(l.to)} onClick={(e) => open(e, w.tenant_id, l.to)}>
                        {l.label}
                      </a>
                    ))}
                  </nav>
                </li>
              ))}
            </ul>
          )
        }}
      </UrRegion>
    </div>
  )
}
