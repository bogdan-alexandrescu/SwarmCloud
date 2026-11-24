/**
 * ONE SESSION'S RECORD, on Telemetry's picked session (Telemetry.tsx): its
 * transcript, as one button, and its summary (swarm-api
 * `GET /v1/voice/sessions/{id}/transcript` and `/summary`).
 *
 * THE TRANSCRIPT is read through the api layer (`loadVoiceTranscript`), which
 * carries the chosen tenant's header and the IAP cookie, and saved from a Blob
 * through an object URL: a bare link would carry no tenant header. A failed
 * read is two words, its reason behind the tooltip, and nothing is saved.
 *
 * THE SUMMARY is the session's own text, drawn only once it is written, and
 * marked `data-prose-content` as the stage's lines are. The console adds no
 * heading and no status line of its own: capped, failed, pending and none
 * draw nothing, and a failed summary read leaves the button alone.
 */
import { useEffect, useState } from 'react'

import { loadVoiceSessionSummary, loadVoiceTranscript, type VoiceSessionSummary } from '../api'
import { Button } from '../components/Button'
import { Tooltip } from '../components/Overlay'
import type { ApiError } from '../fetch'

/** Hand `blob` to the browser as a saved file named `filename`. */
function save(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    URL.revokeObjectURL(url)
  }
}

function Summary({ summary }: { summary: VoiceSessionSummary | null }) {
  if (summary === null || summary.state !== 'written' || summary.text === null) return null
  return (
    <p className="vtel-summary" data-prose-content="">
      {summary.text}
    </p>
  )
}

export function SessionRecord({ sessionId }: { sessionId: string }) {
  const [summary, setSummary] = useState<VoiceSessionSummary | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)

  useEffect(() => {
    let gone = false
    setSummary(null)
    setFailed(null)
    void loadVoiceSessionSummary(sessionId).then((r) => {
      if (gone) return
      setSummary(r.status === 'ok' || r.status === 'stale' ? r.data : null)
    })
    return () => {
      gone = true
    }
  }, [sessionId])

  const download = async (): Promise<void> => {
    setBusy(true)
    setFailed(null)
    try {
      const r = await loadVoiceTranscript(sessionId)
      if (r.status === 'ok' || r.status === 'stale') save(r.data.blob, r.data.filename)
      else if (r.status === 'error') setFailed(r.error)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="vtel-record" aria-label="Session record" data-session-record={sessionId}>
      <div className="vtel-record-head">
        <Button kind="secondary" size="sm" busy={busy} onClick={() => void download()}>
          Download transcript
        </Button>
        {failed !== null && (
          <span role="alert" className="vtel-record-fail">
            <Tooltip tip={failed.message}>
              <span tabIndex={0}>Not downloaded</span>
            </Tooltip>
          </span>
        )}
      </div>
      <Summary summary={summary} />
    </section>
  )
}
