# task_5d4a82ccc6e245ed967f  (SUCCEEDED)

## Prompt

You are in a clone of SwarmCloud. Audit apps/agent-worker/ for any path where a tenant credential could reach somewhere it should not - a log line, an artifact, an environment variable visible to a child process, a checkpoint uploaded to GCS. Note that agents run as root in the v2 design, which makes /proc/1/environ readable by anything they spawn. Give concrete paths. At most 5.

## Report

I read through `apps/agent-worker/` (secrets.py, redact.py, logs.py, procman.py, checkpoint.py, workspace.py, lifecycle.py, cliagent.py, gitops.py, objectstore.py). The credential-handling code is unusually careful — argv never carries secrets, only declared env-var names are exported, the worker's own logger scrubs everything it touches — but there are real gaps. Four concrete paths, worst first:

**1. Periodic checkpoints upload the agent's entire working tree to GCS with zero redaction.**
`agent_worker/checkpoint.py:165-236` (`CheckpointManager.create` → `_write_archive`) tars up `ws.work` and uploads it every `checkpoint_interval_seconds` (called from `lifecycle.py:339-341`), completely independent of the scrubbing path. `lifecycle.py:837-864` (`_redact_before_upload`) only ever touches `ws.stdout_path`, `ws.stderr_path`, `ws.artifacts/**`, and `ws.result_path` — it never runs against `ws.work`. Since `workspace.py:100-101` sets `HOME=ws.work` and `cwd=ws.work` for the CLI agent, any file the agent writes there — a `.npmrc`, a cached CLI auth config, a debug dump, shell history from a tool call that echoed `$ANTHROPIC_API_KEY` — rides into `archive.tar.gz` in GCS in plaintext, well before the attempt even finishes.

**2. Root + shared PID namespace defeats the env-allowlisting the code otherwise does carefully.**
`secrets.py` and `cliagent.py` go to real lengths to hand the CLI child only the one declared credential variable (`cliagent.py:255-268`). But per the v2 design, the agent runs as root, and it exists specifically to execute tenant/prompt-directed shell commands. Any such command — including one reached via prompt injection from a cloned repo or fetched web page — inherits the credential directly (subprocess env inheritance is the normal case for tool calls), and as root it can additionally read `/proc/<pid>/environ` for any other process sharing the namespace, not just its own. That makes the entire "only declared names reach the child" design in `secrets.py:130-193` a control that only holds until the first tool call the agent itself makes — at which point envvar scoping stops being a boundary.

**3. An unused alternate credential can leak into `stderr.log` unregistered.**
`swarm_common/profiles.py:149-150` declares `secrets=("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")` with `secrets_any_of=True`. If a tenant's JSON secret happens to supply both (e.g. left over from switching plans), `secrets.py:152-161` puts *both* into the runner subprocess's environment. But `cliagent.py:243` only registers the one `_credential_env` selects: `log.register_secret(os.environ.get(credential_env))`. The other credential sits unregistered in that process's own `os.environ` — if it ever surfaces in that runner's stderr (a traceback, a verbose library log), it goes to `ws.stderr_path` and then to GCS unredacted, since nothing ever told the logger it was sensitive.

**4. Oversized artifacts are uploaded even when redaction silently skips them.**
`redact.py:55-80` (`scrub_file`) intentionally leaves a file untouched if it's over `max_bytes` (default 64MB) — documented as "corrupting an artifact to protect a key that's probably not in it is the wrong trade." But `lifecycle.py:858-864` never checks `scrub_file`'s return value, and `_upload_outputs` (`lifecycle.py:866-927`) uploads every artifact regardless of whether scrubbing succeeded. An oversized artifact the agent writes (allowed up to `max_artifact_bytes` = 512MB) that happens to contain the credential in plaintext is uploaded to GCS exactly as-is, with no signal that redaction was skipped.

Everything else I checked — argv construction, the git credential file, the structured logger, the generic/browser runners, Firestore writes in `control.py` — routes through `register_secret`/`scrub_text`/`scrub_value` correctly and looks sound.
