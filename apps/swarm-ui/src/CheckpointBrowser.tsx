import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type MouseEvent } from 'react'

import { Mark } from './AgentDetail'
import { USE_FIXTURES } from './api'
import { ArtifactViewer } from './ArtifactViewer'
import { encoded, errorHeading, noteFixtureProbe, read, route, type ApiError, type Result } from './fetch'
import { bytesLabel, type ArtifactContent } from './types'

/**
 * THE CHECKPOINT BROWSER. Owner decision 2026-09-24 on redesign-v2 S3 (A3).
 *
 * A checkpoint was a row with a size, a manifest status and a resumability
 * verdict, and nothing could say what was IN it: `types.ts` recorded
 * "CONTENTS ARE NOT RECORDED ANYWHERE". The server now reads the tarball, and
 * this is the screen for it -- a tree of the archive's members, the SAME
 * `ArtifactViewer` the run's outputs use for any one file, and a link to the
 * whole archive.
 *
 * FOUR RULES, each of which a simpler version of this file would break:
 *
 *  1. `status` IS READ BEFORE `files`. `absent` carries `files: null` and is
 *     drawn as a missing archive, never as a checkpoint with nothing in it.
 *     `files: []` is drawn as a measured zero only when `status` is `ok` and
 *     `truncated` is false -- a fully read, genuinely empty workspace.
 *  2. A CUT LISTING SAYS SO. Entry cap, read budget, inflate budget and a
 *     corrupt archive each end the list early, and each is a `partial` mark
 *     on the surface. A tree showing the first 5000 of 90000 files and a tree
 *     showing all of them are otherwise identical.
 *  3. A FAILED READ IS NOT AN EMPTY ONE. The loaders pass no empty predicate;
 *     a 503 arrives as `error` and is drawn as `not read` with a retry.
 *  4. NO LOCATION IS EVER CONSTRUCTED FROM A MEMBER NAME the server did not
 *     list, and a member the server flagged `unsafe` (`../x`, `/etc/x`) is
 *     shown and never requested -- `memberHref` refuses the segments a browser
 *     would normalise away before the request left.
 *
 * The loaders live HERE rather than in api.ts so this lane did not have to
 * edit a file another lane is changing in parallel. They read the one
 * `USE_FIXTURES` flag api.ts exports, and `test_checkpoint_content.py` holds
 * their path literals to the router the same way `test_runtimes_screen.py`
 * holds api.ts's.
 */

// ---------------------------------------------------------------------------
// The payloads, field for field what swarm_api.checkpoint_content serves
// ---------------------------------------------------------------------------

export type CheckpointMemberType =
  | 'file'
  | 'dir'
  | 'symlink'
  | 'hardlink'
  | 'fifo'
  | 'chardev'
  | 'blockdev'
  | 'other'

export interface CheckpointMember {
  path: string
  size: number
  /** Permission bits only (`mode & 0o7777`). */
  mode: number
  type: CheckpointMemberType
  /** A link's target, redacted by the server. Null for anything not a link. */
  link: string | null
  /** The name points outside the archive's root. Listed, never openable. */
  unsafe: boolean
  /**
   * The name (or a link's target) held bytes that are not UTF-8, and `path`
   * / `link` show them ESCAPED (`caf\xe9.txt`). Listed, never openable: the
   * escaped spelling is a display, and the server refuses it as a path. Not
   * `unsafe` -- a restore unpacks such a name without complaint.
   */
  undecodable: boolean
}

export type CheckpointFilesStatus = 'ok' | 'absent' | 'corrupt'
export type CheckpointTruncation = 'entry_cap' | 'scan_budget' | 'inflate_budget' | 'corrupt'

export interface CheckpointFiles {
  task_id: string
  tenant_id: string
  attempt_id: string
  checkpoint_id: string
  prefix: string
  archive: { key: string; uri: string; bytes: number | null }
  manifest: {
    status: 'present' | 'absent' | 'unreadable'
    detail: string | null
    file_count: number | null
    archive_bytes: number | null
    archive_sha256: string | null
    created_at: string | null
    label: string | null
    archive_key_agrees: boolean | null
  }
  status: CheckpointFilesStatus
  detail: string | null
  /** NULL for `absent`. Never `[]` for a listing that did not happen. */
  files: CheckpointMember[] | null
  count: number
  truncated: boolean
  truncated_reason: CheckpointTruncation | null
  /** Null whenever either count is not a complete fact. */
  file_count_agrees: boolean | null
  scanned_bytes: number
  inflated_bytes: number
  limits: { entries: number; scan_bytes: number; inflate_bytes: number }
}

/** The artifact-content shape, plus where in the checkpoint this came from. */
export interface CheckpointFileContent extends ArtifactContent {
  checkpoint_id: string
  path: string
  content_type: string | null
  member: { type: CheckpointMemberType; mode: number; size: number } | null
}

// ---------------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------------

/**
 * A member path as URL segments, or null if it must never be requested.
 *
 * Encoded segment by segment so `/` stays the separator the route's
 * `{path:path}` expects. A `.`, `..` or empty segment returns null: the
 * browser would resolve `a/../b` to `b` before the request left, so the
 * request would name a DIFFERENT member than the row the reader clicked.
 */
export function memberHref(path: string): string | null {
  const parts = path.split('/')
  if (parts.some((p) => p === '' || p === '.' || p === '..')) return null
  return parts.map(encodeURIComponent).join('/')
}

/**
 * `GET /v1/tasks/{id}/checkpoints/{n}/files`. No empty predicate: `files: []`
 * with `status: ok` is an answer, and a failed listing is a 503 that `read`
 * classifies as an error.
 */
export async function loadCheckpointFiles(
  taskId: string,
  attemptId: string,
  checkpointId: string,
  options: { fixtures?: boolean } = {},
): Promise<Result<CheckpointFiles>> {
  if (options.fixtures ?? USE_FIXTURES) return fixtureFiles(taskId, attemptId, checkpointId)
  const query = new URLSearchParams({ attempt_id: attemptId })
  // Path literal and query kept apart, as api.ts keeps them: the seam test
  // reads the literal's SHAPE against the router's declarations, and the
  // registry keys the read by it (CH-18).
  return read<CheckpointFiles>(
    route('/v1/tasks/{id}/checkpoints/{n}/files', { id: taskId, n: checkpointId }, query),
    () => false,
  )
}

/** `GET /v1/tasks/{id}/checkpoints/{n}/files/{path}` -- one member, as text. */
export async function loadCheckpointFile(
  taskId: string,
  attemptId: string,
  checkpointId: string,
  filePath: string,
  options: { fixtures?: boolean } = {},
): Promise<Result<CheckpointFileContent>> {
  const member = memberHref(filePath)
  if (member === null) {
    return {
      status: 'error',
      error: {
        kind: 'invalid',
        httpStatus: null,
        code: null,
        message: 'This path has an empty, "." or ".." segment, so it was not requested.',
      },
    }
  }
  if (options.fixtures ?? USE_FIXTURES) {
    return fixtureFile(taskId, attemptId, checkpointId, filePath)
  }
  const query = new URLSearchParams({ attempt_id: attemptId })
  // `{path}` arrives ALREADY ENCODED, segment by segment (`memberHref`), so
  // its `/` separators reach the route's `{path:path}` as separators.
  return read<CheckpointFileContent>(
    route('/v1/tasks/{id}/checkpoints/{n}/files/{path}', { id: taskId, n: checkpointId, path: encoded(member) }, query),
    () => false,
  )
}

/**
 * `GET /v1/tasks/{id}/checkpoints/{n}/content` -- the whole archive, as an
 * attachment. A plain link: behind IAP the browser carries the session cookie
 * on a same-origin navigation exactly as it does on `fetch`, and a link is
 * what lets the browser stream a 2 GiB file to disk instead of into memory.
 */
export function checkpointDownloadHref(
  taskId: string,
  attemptId: string,
  checkpointId: string,
): string {
  const query = new URLSearchParams({ attempt_id: attemptId })
  const path = `/v1/tasks/${encodeURIComponent(taskId)}/checkpoints/${encodeURIComponent(checkpointId)}/content`
  return path + `?${query}`
}

export interface CheckpointLoaders {
  files: (taskId: string, attemptId: string, checkpointId: string) => Promise<Result<CheckpointFiles>>
  file: (
    taskId: string,
    attemptId: string,
    checkpointId: string,
    path: string,
  ) => Promise<Result<CheckpointFileContent>>
}

const LOADERS: CheckpointLoaders = {
  files: (t, a, c) => loadCheckpointFiles(t, a, c),
  file: (t, a, c, p) => loadCheckpointFile(t, a, c, p),
}

// ---------------------------------------------------------------------------
// The tree
// ---------------------------------------------------------------------------

export interface TreeNode {
  name: string
  path: string
  /** Null for a directory the archive implies but does not list itself. */
  member: CheckpointMember | null
  children: Map<string, TreeNode>
}

/**
 * Members into a tree, with the unsafe ones set apart.
 *
 * Directories the archive only IMPLIES (a `a/b.txt` with no `a` entry) are
 * created as nodes with no member, so a tar written without directory entries
 * still browses as a tree. Unsafe members are never placed in it: their names
 * contain the segments a tree would have to interpret.
 */
export function buildTree(members: CheckpointMember[]): {
  root: TreeNode
  unsafe: CheckpointMember[]
} {
  const root: TreeNode = { name: '', path: '', member: null, children: new Map() }
  const unsafe: CheckpointMember[] = []
  for (const m of members) {
    if (m.unsafe) {
      unsafe.push(m)
      continue
    }
    const parts = m.path.split('/')
    let node = root
    parts.forEach((part, i) => {
      const path = parts.slice(0, i + 1).join('/')
      let child = node.children.get(part)
      if (child === undefined) {
        child = { name: part, path, member: null, children: new Map() }
        node.children.set(part, child)
      }
      if (i === parts.length - 1) child.member = m
      node = child
    })
  }
  return { root, unsafe }
}

function isDir(node: TreeNode): boolean {
  return node.member === null || node.member.type === 'dir' || node.children.size > 0
}

function sortedChildren(node: TreeNode): TreeNode[] {
  return [...node.children.values()].sort((a, b) => {
    const da = isDir(a)
    const db = isDir(b)
    if (da !== db) return da ? -1 : 1
    return a.name.localeCompare(b.name)
  })
}

/** `0o644` as `0644`, the way `ls -l` readers expect permission bits. */
export function modeLabel(mode: number): string {
  return (mode & 0o7777).toString(8).padStart(4, '0')
}

/** Every directory path, for "expand all" on a small archive. */
function allDirs(node: TreeNode, into: Set<string> = new Set()): Set<string> {
  for (const child of node.children.values()) {
    if (isDir(child)) {
      into.add(child.path)
      allDirs(child, into)
    }
  }
  return into
}

/** Below this many members the tree opens fully; above it, one level. */
const EXPAND_ALL_UNDER = 200

// ---------------------------------------------------------------------------
// The component
// ---------------------------------------------------------------------------

export function CheckpointBrowser({
  taskId,
  attemptId,
  checkpointId,
  onClose,
  loaders = LOADERS,
}: {
  taskId: string
  attemptId: string
  checkpointId: string
  onClose: () => void
  /** Injected by the component tests; the live loaders otherwise. */
  loaders?: CheckpointLoaders
}) {
  const [state, setState] = useState<Result<CheckpointFiles>>({
    status: 'loading',
    since: Date.now(),
  })

  const load = useCallback(async () => {
    setState({ status: 'loading', since: Date.now() })
    setState(await loaders.files(taskId, attemptId, checkpointId))
  }, [loaders, taskId, attemptId, checkpointId])

  useEffect(() => {
    void load()
  }, [load])

  // THE TWO WAYS THE SERVER SAYS "NO ARCHIVE" (AG-10). A listing whose
  // `status` is `absent` is one; a 404 is the other -- the checkpoint, or its
  // attempt, is not there to list -- and it was drawn as a FAILED read, with
  // `try again` beside it and `download archive` above it. Nothing failed, a
  // retry gets the same 404, and the download link points at an object that
  // does not exist. Both are the absent treatment, with neither control.
  const absent =
    (state.status === 'ok' && state.data.status === 'absent') ||
    (state.status === 'error' && state.error.kind === 'not_found')

  return (
    <div className="ckb" role="region" aria-label={`Checkpoint ${checkpointId} files`}>
      <div className="ckb-head">
        <h3 className="mono">{checkpointId}</h3>
        <span className="ckb-meta">attempt {attemptId}</span>
        {/* The way to the whole archive, including when the listing was cut
            by a budget -- that is exactly when it is needed. Withheld only
            once the server has said there is no archive to download. */}
        {!absent && (
          <a
            className="ckb-download"
            href={checkpointDownloadHref(taskId, attemptId, checkpointId)}
            download
          >
            download archive
          </a>
        )}
        <button
          type="button"
          className="art-close"
          onClick={onClose}
          aria-label="Close checkpoint files"
        >
          ✕
        </button>
      </div>
      <Body
        state={state}
        onRetry={() => void load()}
        taskId={taskId}
        attemptId={attemptId}
        checkpointId={checkpointId}
        loaders={loaders}
      />
    </div>
  )
}

function Body({
  state,
  onRetry,
  taskId,
  attemptId,
  checkpointId,
  loaders,
}: {
  state: Result<CheckpointFiles>
  onRetry: () => void
  taskId: string
  attemptId: string
  checkpointId: string
  loaders: CheckpointLoaders
}) {
  switch (state.status) {
    case 'loading':
      return (
        <p className="art-loading">
          <Mark
            kind="pending"
            say={`Reading the archive of ${checkpointId}. The read is in flight.`}
          />
          <span className="ctl-pending art-loading-bar" />
        </p>
      )
    case 'error':
      if (state.error.kind === 'not_found') {
        return <ArchiveAbsent detail={state.error.message} />
      }
      return <Failed error={state.error} onRetry={onRetry} />
    case 'empty':
      // Unreachable -- the loader passes `() => false` -- and handled anyway:
      // an unhandled Result member is how a blank panel ships.
      return (
        <Failed
          error={{
            kind: 'server_error',
            httpStatus: null,
            code: null,
            message: 'The listing returned a shape this browser does not handle.',
          }}
          onRetry={onRetry}
        />
      )
    case 'ok':
    case 'stale':
      return (
        <Listing
          data={state.data}
          taskId={taskId}
          attemptId={attemptId}
          checkpointId={checkpointId}
          loaders={loaders}
        />
      )
  }
}

function Failed({ error, onRetry }: { error: ApiError; onRetry: () => void }) {
  return (
    <div className="ctl-empty is-failed" role="status">
      <h3>
        <Mark
          kind="unread"
          say={`${errorHeading(error)}. Nothing may be concluded about what this checkpoint contains: it is not empty and it is not missing, the read did not complete.`}
        />{' '}
        {errorHeading(error)}
      </h3>
      <p>{error.message}</p>
      <button type="button" className="retry" onClick={onRetry}>
        try again
      </button>
    </div>
  )
}

/**
 * THE ABSENT ARCHIVE, drawn once for both of the server's ways of saying it:
 * a listing with `status: absent`, and a 404 on the listing route. No retry
 * and no download -- asking again gets the same answer, and there is no object
 * to download. `detail` is the server's own sentence about THIS checkpoint,
 * the one sentence §6.9 leaves an empty state.
 */
function ArchiveAbsent({ detail }: { detail: string | null }) {
  return (
    <div className="ctl-empty is-partial" role="status">
      <h3>
        <Mark
          kind="absent"
          say="This checkpoint has no archive object in the bucket, so there are no files to list. It was reclaimed, or its upload never completed. This is not an empty checkpoint, and not a failed read."
        />{' '}
        archive not in the bucket
      </h3>
      {detail !== null && <p>{detail}</p>}
    </div>
  )
}

const CUT: Readonly<Record<CheckpointTruncation, string>> = {
  entry_cap: 'entry cap',
  scan_budget: 'read budget',
  inflate_budget: 'inflate budget',
  corrupt: 'archive corrupt',
}

function cutSay(data: CheckpointFiles): string {
  switch (data.truncated_reason) {
    case 'entry_cap':
      return `The listing stops at ${data.limits.entries} entries, so these are the first ${data.count} and the archive holds more. The whole archive is in the download.`
    case 'scan_budget':
      return `The listing stopped after reading ${bytesLabel(data.scanned_bytes)} of the archive, the most one request may read. Later files exist and are not shown; the whole archive is in the download.`
    case 'inflate_budget':
      return `The listing stopped after decompressing ${bytesLabel(data.inflated_bytes)}, the most one request may inflate. Later files exist and are not shown; the whole archive is in the download.`
    case 'corrupt':
      return 'The archive stops being readable part-way through, so these are the files before that point and no more.'
    case null:
      return 'The listing is not the whole archive.'
  }
}

function Listing({
  data,
  taskId,
  attemptId,
  checkpointId,
  loaders,
}: {
  data: CheckpointFiles
  taskId: string
  attemptId: string
  checkpointId: string
  loaders: CheckpointLoaders
}) {
  const members = data.files
  const tree = useMemo(() => buildTree(members ?? []), [members])
  const [open, setOpen] = useState<Set<string>>(() =>
    (members?.length ?? 0) <= EXPAND_ALL_UNDER ? allDirs(tree.root) : new Set(),
  )
  const [selected, setSelected] = useState<CheckpointMember | null>(null)

  const toggle = useCallback((path: string) => {
    setOpen((cur) => {
      const next = new Set(cur)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }, [])

  // One stable loader per opened file: ArtifactViewer re-reads when the
  // function it is given changes, so a fresh closure every render would be a
  // fresh request every render.
  const loadSelected = useMemo(
    () =>
      selected === null
        ? undefined
        : () => loaders.file(taskId, attemptId, checkpointId, selected.path),
    [loaders, taskId, attemptId, checkpointId, selected],
  )

  if (data.status === 'absent' || members === null) {
    // `files: null`. NOT a checkpoint with nothing in it: there is no
    // archive object to have anything in.
    return <ArchiveAbsent detail={data.detail} />
  }

  return (
    <>
      {data.status === 'corrupt' && (
        <div className="ctl-empty is-failed" role="status">
          <h3>
            <Mark
              kind="unread"
              say="The archive is in the bucket and stops being a readable tar.gz part-way through. The files below are the ones before that point; whether it held more is not known."
            />{' '}
            archive not readable past here
          </h3>
          {data.detail !== null && <p>{data.detail}</p>}
        </div>
      )}

      <ul className="ctl-facts ckb-facts">
        <li className={`ctl-fact${data.truncated ? ' is-absent' : ''}`}>
          <b>entries</b>
          {data.count}
          {data.truncated && (
            <>
              {' '}
              <Mark kind="partial" say={cutSay(data)} />{' '}
              <span className="ckb-cut">
                {data.truncated_reason === null ? 'cut' : CUT[data.truncated_reason]}
              </span>
            </>
          )}
        </li>
        <li className="ctl-fact">
          <b>archive</b>
          {bytesLabel(data.archive.bytes)}
        </li>
        <li className={`ctl-fact${data.file_count_agrees === false ? ' is-absent' : ''}`}>
          <b>manifest</b>
          {data.manifest.status !== 'present' ? (
            <Mark
              kind={data.manifest.status === 'unreadable' ? 'unread' : 'absent'}
              say={
                data.manifest.status === 'unreadable'
                  ? 'The manifest is there and could not be read, so its file count is not known and nothing was compared.'
                  : 'There is no manifest: the worker never wrote the commit marker for this checkpoint, so no restore would select it.'
              }
            />
          ) : data.manifest.file_count === null ? (
            <Mark kind="absent" say="The manifest records no file count." />
          ) : (
            <>
              {data.manifest.file_count} files
              {data.file_count_agrees === false && (
                <>
                  {' '}
                  <Mark
                    kind="partial"
                    say={`The manifest records ${data.manifest.file_count} files and this listing counted a different number of non-directory entries. The archive does not hold what its commit marker says.`}
                  />
                </>
              )}
            </>
          )}
        </li>
      </ul>

      {tree.unsafe.length > 0 && (
        <div className="ctl-empty is-partial" role="status">
          <h3>
            <Mark
              kind="partial"
              say="These members name a location outside the archive's root. A restore refuses an archive that contains one, so this checkpoint cannot be resumed as it stands. They are listed so that is visible, and they cannot be opened."
            />{' '}
            {tree.unsafe.length} unsafe {tree.unsafe.length === 1 ? 'path' : 'paths'}
          </h3>
          <ul className="ckb-unsafe">
            {tree.unsafe.map((m) => (
              <li key={m.path} className="mono">
                {m.path}
              </li>
            ))}
          </ul>
        </div>
      )}

      {members.length === 0 ? (
        data.status === 'ok' && !data.truncated ? (
          // A MEASURED ZERO: the archive was read to its end and holds
          // nothing. The one case `files: []` is allowed to mean "empty".
          <p className="art-empty">
            <Mark
              kind="zero"
              say="The archive was read to its end and holds no files. The workspace was empty when this checkpoint was taken."
            />{' '}
            0 files
          </p>
        ) : null
      ) : (
        <Branch
          node={tree.root}
          depth={0}
          open={open}
          toggle={toggle}
          selected={selected?.path ?? null}
          onOpen={setSelected}
        />
      )}

      {selected !== null && loadSelected !== undefined && (
        <ArtifactViewer
          key={selected.path}
          taskId={taskId}
          artifact={{ name: selected.path, bytes: selected.size, uri: data.archive.uri }}
          load={loadSelected}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  )
}

function Branch({
  node,
  depth,
  open,
  toggle,
  selected,
  onOpen,
}: {
  node: TreeNode
  depth: number
  open: Set<string>
  toggle: (path: string) => void
  selected: string | null
  onOpen: (member: CheckpointMember) => void
}) {
  return (
    <ul
      role={depth === 0 ? 'tree' : 'group'}
      className={depth === 0 ? 'ckb-tree' : 'ckb-group'}
      aria-label={depth === 0 ? 'Files in this checkpoint' : undefined}
      // One handler, on the tree, for every row in it: key events from a
      // row's button bubble here. Only the root carries it, or a key pressed
      // in a nested directory would be handled once per level above it.
      onKeyDown={depth === 0 ? (e) => treeKey(e, toggle) : undefined}
    >
      {sortedChildren(node).map((child) => (
        <Node
          key={child.path}
          node={child}
          depth={depth}
          open={open}
          toggle={toggle}
          selected={selected}
          onOpen={onOpen}
        />
      ))}
    </ul>
  )
}

function Node({
  node,
  depth,
  open,
  toggle,
  selected,
  onOpen,
}: {
  node: TreeNode
  depth: number
  open: Set<string>
  toggle: (path: string) => void
  selected: string | null
  onOpen: (member: CheckpointMember) => void
}) {
  if (isDir(node)) {
    const expanded = open.has(node.path)
    return (
      <li role="treeitem" aria-expanded={expanded}>
        <div className="ckb-node is-dir is-control" onClick={rowClick(() => toggle(node.path))}>
          <button
            type="button"
            className="ckb-open"
            data-path={node.path}
            data-dir="true"
            data-expanded={expanded ? 'true' : 'false'}
          >
            {expanded ? '▾' : '▸'} {node.name}/
          </button>
          <span className="ckb-meta">{node.children.size}</span>
        </div>
        {expanded && (
          <Branch
            node={node}
            depth={depth + 1}
            open={open}
            toggle={toggle}
            selected={selected}
            onOpen={onOpen}
          />
        )}
      </li>
    )
  }

  // Not a directory, so the member is present by construction of `isDir`.
  const m = node.member as CheckpointMember
  const isOpen = selected === m.path
  // An undecodable name is shown by its escaped bytes, and that spelling is
  // not one the server will accept back as a path -- so it is never a button
  // that could only ever answer "refused".
  const openable = m.type === 'file' && !m.undecodable
  return (
    <li role="treeitem" aria-selected={isOpen}>
      <div
        className={`ckb-node${openable ? ' is-control' : ''}${isOpen ? ' is-open' : ''}`}
        onClick={openable ? rowClick(() => onOpen(m)) : undefined}
      >
        {openable ? (
          <button type="button" className="ckb-open" aria-expanded={isOpen} data-path={m.path}>
            {node.name}
          </button>
        ) : (
          // A link or a special file has no content of its own to open. What
          // it IS stays on the surface -- the target for a link, the type
          // otherwise -- rather than a dead button.
          <span className="ckb-name">
            {node.name}
            {m.link !== null && <span className="ckb-meta"> → {m.link}</span>}
            {m.undecodable && (
              // The flag covers a link's TARGET too, so the words do not say
              // "name": a symlink with a Latin-1 target has an ordinary name.
              <span
                className="ckb-meta"
                title="This name or link target holds bytes that are not UTF-8, shown here escaped. That spelling cannot be requested, so a file like this cannot be opened here; it is in the downloaded archive."
              >
                {' '}
                · not UTF-8, shown escaped{m.type === 'file' ? '; not openable here' : ''}
              </span>
            )}
          </span>
        )}
        <span className="ckb-meta">
          {m.type === 'file' ? bytesLabel(m.size) : m.type} · {modeLabel(m.mode)}
        </span>
      </div>
    </li>
  )
}

/**
 * THE ROW IS THE CONTROL. Measured on the live inspector on 2026-09-24: a click
 * on the `result.json` row opened nothing. Only the NAME was a button; the
 * size, the mode and the run to the panel's edge -- most of what a pointer
 * lands on, and the centre of the row an automated click aims for -- were
 * inert, and a click there was dropped without a trace.
 *
 * So the handler is on the row, and the name stays a native `<button>` inside
 * it: Tab reaches it, Enter and Space activate it, and the click that
 * activation produces bubbles to this same handler -- one action, whichever
 * part of the row or whichever key started it. The button carries no handler
 * of its own, or a click on it would run twice (and a directory would open
 * and shut in the same event). A click beside the name also moves focus to
 * the name, so the arrow keys carry on from the row just used.
 */
function rowClick(act: () => void): (e: MouseEvent<HTMLDivElement>) => void {
  return (e) => {
    act()
    const button = e.currentTarget.querySelector<HTMLButtonElement>('button.ckb-open')
    if (button !== null && e.target !== button) button.focus()
  }
}

/**
 * THE TREE'S KEYS, per the WAI-ARIA tree pattern a `role="tree"` promises:
 * Up and Down move between visible rows, Home and End go to the ends, Right
 * opens a shut directory (or steps into an open one), Left shuts an open
 * directory (or steps out to the one containing the row). Every row keeps
 * its own Tab stop -- a native button's -- so nothing here is needed to reach
 * a row, only to move between them faster.
 *
 * The rows are read off the DOM in document order rather than recomputed from
 * the tree, because the DOM is exactly what is visible: a shut directory's
 * children are not rendered, so they are not in the list and are skipped.
 */
function treeKey(e: KeyboardEvent<HTMLUListElement>, toggle: (path: string) => void): void {
  const here = e.target
  if (!(here instanceof HTMLButtonElement) || !here.classList.contains('ckb-open')) return
  const rows = [...e.currentTarget.querySelectorAll<HTMLButtonElement>('button.ckb-open')]
  const i = rows.indexOf(here)
  if (i < 0) return
  const item = here.closest<HTMLElement>('li[role="treeitem"]')
  const dir = here.dataset.dir === 'true'
  const expanded = here.dataset.expanded === 'true'
  const path = here.dataset.path ?? ''

  let next: HTMLButtonElement | undefined
  switch (e.key) {
    case 'ArrowDown':
      next = rows[i + 1]
      break
    case 'ArrowUp':
      next = rows[i - 1]
      break
    case 'Home':
      next = rows[0]
      break
    case 'End':
      next = rows[rows.length - 1]
      break
    case 'ArrowRight':
      if (dir && !expanded) toggle(path)
      else if (dir) {
        const first = rows[i + 1]
        if (first !== undefined && item !== null && item.contains(first)) next = first
      }
      break
    case 'ArrowLeft':
      if (dir && expanded) toggle(path)
      else next = parentRow(item)
      break
    default:
      return
  }
  e.preventDefault()
  next?.focus()
}

/** The button of the directory row that contains `item`, if any. */
function parentRow(item: HTMLElement | null): HTMLButtonElement | undefined {
  const parent = item?.parentElement?.closest<HTMLElement>('li[role="treeitem"]')
  if (!parent) return undefined
  const row = [...parent.children].find((c) => c.classList.contains('ckb-node'))
  return row?.querySelector<HTMLButtonElement>('button.ckb-open') ?? undefined
}

// ---------------------------------------------------------------------------
// Fixtures -- the ABSENCES, not only the happy path
// ---------------------------------------------------------------------------

/**
 * `ckpt_0001` in api.ts's checkpoint fixture has an unreadable manifest and no
 * `archive.tar.gz`, so here it is `absent` -- the state a browser most easily
 * draws as "empty". Every other id gets a listing that is CUT (entry cap),
 * disagrees with its manifest, and carries an unsafe member, a symlink and a
 * name that is not UTF-8, so all five of those renderings are looked at in
 * development rather than first seen in production.
 */
async function fixtureFiles(
  taskId: string,
  attemptId: string,
  checkpointId: string,
): Promise<Result<CheckpointFiles>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/tasks/{id}/checkpoints/{n}/files', { id: taskId, n: checkpointId }), 60, true)
  const prefix = `tenants/u-bogdan/tasks/${taskId}/attempts/${attemptId}/checkpoints/${checkpointId}`
  const absent = checkpointId === 'ckpt_0001'
  const files: CheckpointMember[] = [
    { path: 'progress', size: 0, mode: 0o755, type: 'dir', link: null, unsafe: false, undecodable: false },
    { path: 'progress/step-0001.md', size: 612, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false },
    { path: 'progress/step-0002.md', size: 0, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false },
    { path: 'state.json', size: 88, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false },
    { path: 'latest', size: 0, mode: 0o777, type: 'symlink', link: 'state.json', unsafe: false, undecodable: false },
    { path: 'screenshot.png', size: 48_211, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: false },
    { path: '../outside.txt', size: 12, mode: 0o644, type: 'file', link: null, unsafe: true, undecodable: false },
    // A Latin-1 name, as the server escapes it: listed, not openable.
    { path: 'caf\\xe9.txt', size: 5, mode: 0o644, type: 'file', link: null, unsafe: false, undecodable: true },
  ]
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: attemptId,
      checkpoint_id: checkpointId,
      prefix,
      archive: {
        key: `${prefix}/archive.tar.gz`,
        uri: `gs://swarm-artifacts/${prefix}/archive.tar.gz`,
        bytes: absent ? null : 18_442_240,
      },
      manifest: {
        status: absent ? 'unreadable' : 'present',
        detail: absent ? 'The object is there and did not parse as JSON.' : null,
        file_count: absent ? null : 184,
        archive_bytes: absent ? null : 18_442_240,
        archive_sha256: null,
        created_at: null,
        label: absent ? null : 'periodic',
        archive_key_agrees: absent ? null : true,
      },
      status: absent ? 'absent' : 'ok',
      detail: absent
        ? 'this checkpoint has no archive object, so there is nothing to list; it was reclaimed, or its upload never completed'
        : null,
      files: absent ? null : files,
      count: absent ? 0 : files.length,
      truncated: !absent,
      truncated_reason: absent ? null : 'entry_cap',
      file_count_agrees: null,
      scanned_bytes: absent ? 0 : 1_048_576,
      inflated_bytes: absent ? 0 : 3_145_728,
      limits: { entries: files.length, scan_bytes: 268_435_456, inflate_bytes: 1_073_741_824 },
    },
  }
}

async function fixtureFile(
  taskId: string,
  attemptId: string,
  checkpointId: string,
  filePath: string,
): Promise<Result<CheckpointFileContent>> {
  await new Promise((r) => setTimeout(r, 30))
  noteFixtureProbe(
    route('/v1/tasks/{id}/checkpoints/{n}/files/{path}', { id: taskId, n: checkpointId, path: encoded(filePath) }),
    30,
    true,
  )
  const prefix = `tenants/u-bogdan/tasks/${taskId}/attempts/${attemptId}/checkpoints/${checkpointId}`
  const bodies: Record<string, string> = {
    'progress/step-0001.md': '# Step one\n\nRead the repository and listed the modules.\n',
    'progress/step-0002.md': '',
    'state.json': '{\n  "completed_steps": 1\n}\n',
  }
  const binary = filePath.endsWith('.png')
  const content = binary ? null : (bodies[filePath] ?? `fixture checkpoint file ${filePath}\n`)
  const bytes = content === null ? 48_211 : new TextEncoder().encode(content).length
  const uri = `gs://swarm-artifacts/${prefix}/archive.tar.gz`
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: attemptId,
      checkpoint_id: checkpointId,
      path: filePath,
      content_type: binary ? null : 'text/plain',
      member: { type: 'file', mode: 0o644, size: bytes },
      artifact: { name: filePath, bytes, uri },
      status: binary ? 'binary' : 'ok',
      detail: binary ? "this file's type is not on the text allowlist" : null,
      key: `${prefix}/archive.tar.gz`,
      uri,
      content,
      total_bytes: bytes,
      offset: 0,
      returned_bytes: content === null ? 0 : bytes,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 11 },
    },
  }
}
