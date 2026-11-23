// The workspace pages' addresses -- Workspace Settings' parts
// (WorkspaceSettings.tsx), the workspace page's tabs (WorkspacePage.tsx) and
// the join page (JoinWorkspace.tsx) -- apart from the pages, so App.tsx reads
// them without loading any of the three.

export type WorkspacePart = 'settings' | 'setup' | 'new'

/** The part a route's view names: `part=setup` or `part=new`; anything else is Settings. */
export function workspacePartOf(view: string | null): WorkspacePart {
  const part = new URLSearchParams(view ?? '').get('part')
  return part === 'setup' || part === 'new' ? part : 'settings'
}

export type WorkspaceTab = 'people' | 'roles' | 'settings' | 'setup'

export const WORKSPACE_TABS: readonly WorkspaceTab[] = ['people', 'roles', 'settings', 'setup']

/** The tab a query names (`tab=roles`); anything else is People. */
export function workspaceTabOf(view: string | null | undefined): WorkspaceTab {
  const t = new URLSearchParams(view ?? '').get('tab')
  return (WORKSPACE_TABS as readonly string[]).includes(t ?? '') ? (t as WorkspaceTab) : 'people'
}

/** The page's path. Not a section and not a tab: only a shared link leads here. */
export const JOIN_PATH = '/join'

/** The code a join link carries in its fragment, on `/join` only; null anywhere else or with none. */
export function joinCodeFrom(pathname: string, hash: string): string | null {
  if (pathname !== JOIN_PATH && pathname !== `${JOIN_PATH}/`) return null
  const code = hash.replace(/^#/, '').trim()
  return code === '' ? null : code
}
