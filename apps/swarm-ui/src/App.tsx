import { useEffect, useState } from 'react'
import { AgentsScreen } from './Agents'
import { CapacityScreen } from './Capacity'
import { Nav } from './Shell'
import { WorkflowsScreen } from './Workflows'

const SCREENS = ['capacity', 'agents', 'workflows'] as const
type ScreenId = (typeof SCREENS)[number]

function fromHash(): ScreenId {
  const h = window.location.hash.replace('#', '')
  return (SCREENS as readonly string[]).includes(h) ? (h as ScreenId) : 'capacity'
}

export function App() {
  const [at, setAt] = useState<ScreenId>(fromHash)

  // The hash IS the router. A real router earns its place when there are
  // detail routes with ids in them; today it would be a dependency that does
  // nothing three lines cannot, and back/forward already work.
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
      <Nav at={at} go={go} />
      {at === 'capacity' && <CapacityScreen />}
      {at === 'agents' && <AgentsScreen />}
      {at === 'workflows' && <WorkflowsScreen />}
    </div>
  )
}
