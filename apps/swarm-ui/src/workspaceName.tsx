// The selected workspace's name and where its setup is, in words (Profile.tsx's),
// apart from the profile page so the pages that draw them do not load it.
import { useEffect, useState, useSyncExternalStore } from 'react'
import { chosenTenant, subscribeTenant } from './fetch'
import { sharedMeRead } from './roleGate'
import { displayNameOf, OWN_WORKSPACE_LABEL } from './Spine'

/**
 * THE SELECTED WORKSPACE'S NAME, as its own page's breadcrumb draws it
 * (WorkspacePage): "Your workspace" for the person's own, else its display
 * name, else its id. Null while `GET /v1/tenants/me` is read and when it
 * failed, read again on every workspace switch.
 */
export function useWorkspaceName(): string | null {
  const chosen = useSyncExternalStore(subscribeTenant, chosenTenant, chosenTenant)
  const [known, setKnown] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setKnown(null)
    void sharedMeRead(chosen).then((me) => {
      if (!live) return
      const t = me?.tenant
      if (t === undefined || t === null || typeof t.tenant_id !== 'string') return setKnown(null)
      setKnown(t.kind === 'user' ? OWN_WORKSPACE_LABEL : (displayNameOf(t.display_name) ?? t.tenant_id))
    })
    return () => {
      live = false
    }
  }, [chosen])
  return known
}

/**
 * WHERE A WORKSPACE'S SETUP AND SETTINGS ARE, IN WORDS (admin routes lane;
 * part of archived issue 2688): the selected workspace's own page, Admin › Workspaces ›
 * <its name> › Setup or Settings. Config › Workspace Settings left the Config
 * panel for that page; "this workspace" while its name is not known.
 */
export function workspacePageWords(name: string | null, tab: 'Setup' | 'Settings'): string {
  return `Admin › Workspaces › ${name ?? 'this workspace'} › ${tab}`
}

/**
 * workspacePageWords for the selected workspace, its name read where the
 * words are drawn, so a page that does not draw them makes no read.
 */
export function WorkspaceTabWords({ tab }: { tab: 'Setup' | 'Settings' }) {
  return <>{workspacePageWords(useWorkspaceName(), tab)}</>
}
