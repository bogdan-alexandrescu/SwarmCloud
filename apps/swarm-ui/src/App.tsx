import { useEffect, useState } from 'react'
import { AgentDetailScreen } from './AgentDetail'
import { ActivityScreen, TenantsScreen } from './Activity'
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
  'activity', 'quota', 'counts', 'tenants',
] as const
type ScreenId = (typeof SCREENS)[number]

interface Route {
  screen: ScreenId
  /** Set when the hash is `#agents/<task id>`. */
  taskId: string | null
}

function fromHash(): Route {
  const h = window.location.hash.replace(/^#/, '')
  const [head = '', ...rest] = h.split('/')
  const screen = (SCREENS as readonly string[]).includes(head) ? (head as ScreenId) : 'home'
  // Task ids are opaque and may contain characters that were encoded on the
  // way in, so rejoin the tail rather than assuming a single segment.
  const tail = rest.join('/')
  return { screen, taskId: screen === 'agents' && tail ? decodeURIComponent(tail) : null }
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
      {at.taskId && <AgentDetailScreen taskId={at.taskId} onClose={() => go('agents')} />}
      <DataSourceStrip />
    </div>
  )
}
