import { useEffect, useState } from 'react'
import { recentName, RECENT_WORKFLOWS_EVENT } from './Spine'

/**
 * The name Recent holds for `id`, kept current as the list or the page records
 * it: the page's title, and the breadcrumb's last segment (App.tsx, VQ-015).
 */
export function useRecentName(id: string | null): string | null {
  const [name, setName] = useState<string | null>(() => (id === null ? null : recentName(id)))
  useEffect(() => {
    const again = () => setName(id === null ? null : recentName(id))
    again()
    globalThis.addEventListener?.(RECENT_WORKFLOWS_EVENT, again)
    return () => globalThis.removeEventListener?.(RECENT_WORKFLOWS_EVENT, again)
  }, [id])
  return name
}
