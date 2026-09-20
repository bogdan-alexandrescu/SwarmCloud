import { useEffect, useState } from 'react'
import { AccountsScreen } from './Accounts'
import { AgentDetailScreen } from './AgentDetail'
import { ActivityScreen, TenantsScreen } from './Activity'
import { AdminSettingsScreen } from './AdminSettings'
import { AgentsScreen } from './Agents'
import { CapacityScreen } from './Capacity'
import { ControlRoomScreen } from './ControlRoom'
import { HoldersScreen } from './Holders'
import { PlatformCountsScreen } from './PlatformCounts'
import { QuotaDetailScreen } from './QuotaDetail'
import { DataSourceStrip } from './DataSources'
import { Nav } from './Shell'
import { TroubleScreen } from './Trouble'
import { WorkflowsScreen } from './Workflows'

const SCREENS = [
  'home', 'trouble', 'capacity', 'holders', 'agents', 'workflows',
  'activity', 'quota', 'counts', 'tenants', 'settings',
] as const
type ScreenId = (typeof SCREENS)[number]

/** The panes under Settings. `#settings` alone means the first one. */
const SETTINGS_TABS = [
  ['limits', 'Platform limits'],
  ['accounts', 'Accounts'],
] as const
type SettingsTab = (typeof SETTINGS_TABS)[number][0]

interface Route {
  screen: ScreenId
  /** Set when the hash is `#agents/<task id>`. */
  taskId: string | null
  /** Set when the hash is `#settings/<tab>`. Defaults to the first tab. */
  settingsTab: SettingsTab
}

function fromHash(): Route {
  const h = window.location.hash.replace(/^#/, '')
  const [head = '', ...rest] = h.split('/')
  const screen = (SCREENS as readonly string[]).includes(head) ? (head as ScreenId) : 'home'
  // Task ids are opaque and may contain characters that were encoded on the
  // way in, so rejoin the tail rather than assuming a single segment.
  const tail = rest.join('/')
  const tab = SETTINGS_TABS.find(([id]) => id === tail)
  return {
    screen,
    taskId: screen === 'agents' && tail ? decodeURIComponent(tail) : null,
    // An unrecognised tail falls back to the first pane rather than rendering
    // nothing: a mistyped hash must not produce a blank Settings screen.
    settingsTab: tab ? tab[0] : 'limits',
  }
}

export function App() {
  const [at, setAt] = useState<Route>(fromHash)

  // The hash IS the router. A real router earns its place when there are
  // nested layouts or loaders; today it would be a dependency that does
  // nothing fifteen lines cannot, and back/forward already work -- including
  // out of the detail drawer, which is why the drawer is a route rather than
  // component state.
  useEffect(() => {
    const onHash = () => setAt(fromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const go = (to: string) => {
    window.location.hash = to
  }

  return (
    <div className="app">
      <Nav at={at.screen} go={go} />
      {at.screen === 'home' && <ControlRoomScreen />}
      {at.screen === 'trouble' && <TroubleScreen />}
      {at.screen === 'capacity' && <CapacityScreen />}
      {at.screen === 'holders' && <HoldersScreen />}
      {at.screen === 'quota' && <QuotaDetailScreen />}
      {at.screen === 'counts' && <PlatformCountsScreen />}
      {at.screen === 'agents' && <AgentsScreen onOpen={(id) => go(`agents/${encodeURIComponent(id)}`)} />}
      {at.screen === 'workflows' && <WorkflowsScreen />}
      {at.screen === 'activity' && <ActivityScreen />}
      {at.screen === 'tenants' && <TenantsScreen />}
      {at.screen === 'settings' && (
        // SETTINGS IS TWO PANES, and the switcher lives here rather than in the
        // top nav for a reason worth recording: the tab list in Shell.tsx
        // belongs to another track, and Accounts is a Settings pane rather than
        // a peer of Capacity or Agents. Both panes are real routes
        // (`#settings` and `#settings/accounts`), so back and forward work
        // between them exactly as they do everywhere else.
        <>
          {/* `role="tablist"` + `role="tab"` + `aria-selected` is the
              convention Agents.tsx already uses, and reusing the `.tabs` class
              without it would leave the selected pane expressed only as a CSS
              class -- two unlabelled buttons, with nothing telling a screen
              reader which one is showing, on the screen that hands out live
              OAuth credentials. */}
          <div className="tabs settings-tabs" role="tablist" aria-label="Settings panes">
            {SETTINGS_TABS.map(([id, label]) => (
              <button
                key={id}
                role="tab"
                aria-selected={at.settingsTab === id}
                className={at.settingsTab === id ? 'on' : ''}
                onClick={() => go(id === 'limits' ? 'settings' : `settings/${id}`)}
              >
                {label}
              </button>
            ))}
          </div>
          {at.settingsTab === 'limits' && <AdminSettingsScreen />}
          {at.settingsTab === 'accounts' && <AccountsScreen />}
        </>
      )}
      {at.taskId && <AgentDetailScreen taskId={at.taskId} onClose={() => go('agents')} />}
      <DataSourceStrip />
    </div>
  )
}
