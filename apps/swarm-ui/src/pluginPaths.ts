// The plugin pages' addresses -- Admin › Plugins' (AdminPlugins.tsx) and
// Config › Plugins' one-time link (ConfigPlugins.tsx) -- and the catalogue's
// card order, apart from the pages, so App.tsx reads them without loading
// either page.
import type { CatalogueEntry, PluginCatalogue } from './api'

/** The addresses lane 10e routes: the catalogue, Add plugin, and a plugin's page (lane 10b). */
export const ADMIN_PLUGINS_PATH = '/admin/plugins'
export const ADMIN_PLUGIN_ADD_PATH = '/admin/plugins/add'
export function adminPluginPath(installId: string): string {
  return `${ADMIN_PLUGINS_PATH}/${encodeURIComponent(installId)}`
}

/** Every card, built-ins first, then the platform's installs, then the workspaces'. */
export function catalogueCards(c: PluginCatalogue): CatalogueEntry[] {
  const byName = (a: CatalogueEntry, b: CatalogueEntry) => a.name.localeCompare(b.name)
  return [...[...c.built_in].sort(byName), ...[...c.installs].sort(byName), ...[...c.others].sort(byName)]
}

/** The one-time link's address (`pluginconfig.mint_link`): `/config/plugins/<install_id>?field=<key>&t=<token>`. */
export interface PluginLinkAt {
  install_id: string
  field: string
  token: string
}

export function pluginLinkOf(pathname: string, search: string): PluginLinkAt | null {
  const m = /^\/config\/plugins\/([pw]-[0-9a-f]{12})\/?$/.exec(pathname)
  if (m === null) return null
  const q = new URLSearchParams(search)
  const field = q.get('field')
  const token = q.get('t')
  if (field === null || field === '' || token === null || token === '') return null
  return { install_id: m[1]!, field, token }
}
