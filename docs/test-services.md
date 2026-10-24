# Declaring the services your tests need: `.swarm/test-services.toml`

For a repository whose tests need a database. Without a declaration, an
agent's pre-publish `graph-pytest` check cannot run those tests: every test
errors at setup on `127.0.0.1:5432`, the check reports `skipped`, and a broken
test reaches CI uncaught (#1605: four product-oracle lanes on 2026-10-19).
With one, the worker runs the database and the check judges the tests. The
design, the owner's decisions and what each lane built are in
[design/test-services.md](design/test-services.md); this page is what a
repository writes and what it gets, read from the code on 2026-10-24 at
`5f5d511`.

---

## The file

Check in `.swarm/test-services.toml` at the repository root
(`apps/agent-worker/agent_worker/testservices.py::DECLARATION_PATH`):

```toml
# What this repository's tests need. Names only: the platform owns the image,
# the command, the port and the resources.
[postgres]
version = 16
databases = ["app_test"]
```

That is the whole schema. The worker reads the copy in the working tree, so
a change that adds, edits or deletes the file takes effect in the same
attempt's pre-publish checks.

| key | rule |
|---|---|
| the table name | a catalogue service: `postgres` is the only one |
| `version` | an integer the catalogue offers for that service: `16` is the only one |
| `databases` | a list of 1 to 8 names, each matching `[a-z_][a-z0-9_]{0,62}`, no duplicates; each is created empty, owned by `postgres`. The first is the default database |

The file may be at most 4096 bytes, UTF-8, valid TOML, a regular file inside
the repository (a symlink that leads outside it is not read).

## The catalogue: PostgreSQL 16, one entry

`apps/agent-worker/agent_worker/testservices.py::CATALOGUE` holds `postgres`
with versions `(16,)` and nothing else. The owner chose 16 alone and no Redis
on 2026-10-23 (design §10): a second service or version is added to the
catalogue when the evidence shows repositories need it, never by a key in the
declaration. If your CI runs Postgres 15 or 17, the tests still run against
16 here.

What runs is fixed by the platform:

* the PostgreSQL 16 server installed in `agent-runtime-base`, started by the
  worker as its own child process, never by the agent or by a command your
  repository chose;
* listening on `127.0.0.1:5432` only, so nothing outside the pod reaches it;
* user `postgres`, `trust` authentication, **no password**: any password your
  repository's default URL carries is accepted and ignored;
* the settings in
  `apps/agent-worker/agent_worker/testservices.py::POSTGRES_SETTINGS`: small
  memory (`shared_buffers=64MB`, `max_connections=50`) and no durability
  (`fsync=off`), because the data is thrown away;
* no extension package beside the server's own (`postgresql-16`). A schema,
  an extension your tests `CREATE EXTENSION`, or seed data is created by your
  own fixtures or migrations, exactly as they would against an empty
  database in CI;
* inside the runner's existing memory and CPU: nothing is added to the pod,
  and the kernel's OOM killer is told to take the database before the agent.

## The keys that are refused

Every key but `version` and `databases` is refused by name. These carry a
reason of their own
(`apps/agent-worker/agent_worker/testservices.py::_REFUSED_BECAUSE`):

| key | why |
|---|---|
| `image` | the platform owns the image (invariant 10) |
| `command`, `args` | the platform owns the command and its arguments (invariant 10) |
| `port` | the platform owns the address: `127.0.0.1:5432` |
| `env` | the platform owns the service's environment (invariant 10) |
| `memory`, `cpu` | the platform owns the resources (invariant 7) |
| `settings` | the platform owns the server's settings |
| `init_sql` | a schema, extension or seed data is created by the repository's own fixtures |
| `user` | the platform owns the service's user |

Any other key is refused as "not a declaration key"; any table but
`[postgres]` as "not a catalogue service". Every refusal in the file is
reported at once, one per line, as `<key>: <rule>`, for example:

```text
postgres.image: refused: the platform owns the image (invariant 10); only version and databases
postgres.version: 15 is not offered: version is an integer, one of 16
```

A file with any refusal declares nothing: no service starts and the tests run
as if there were no file.

## What your repository gets

**For the whole attempt, from right after the clone**, the worker runs the
server with your databases created before it listens. The agent's own test
runs while it works reach it at `127.0.0.1:5432`, which is where most
fixtures already default to (product-oracle's `ORACLE_TEST_DATABASE_URL`
defaults there). The agent's environment does **not** carry the variables
below; a test that reads only `DATABASE_URL`, with no default, finds the
database during the pre-publish checks but not during the agent's turn.

**In the pre-publish checks that run pytest** (`graph-pytest` and
`scripts/changed-guards.sh` where you have one), the environment gains
(`apps/agent-worker/agent_worker/prepublish.py::service_env`):

| variable | value |
|---|---|
| `PGHOST` | `127.0.0.1` |
| `PGPORT` | `5432` |
| `PGUSER` | `postgres` |
| `PGDATABASE` | the first database in `databases` |
| `DATABASE_URL` | `postgresql://postgres@127.0.0.1:5432/<first database>` |
| `SWARM_TEST_SERVICES` | `postgres`: the services that answered for this round, so a conftest can tell |

No variable names of your own: the owner chose `DATABASE_URL`, `PG*` and the
default address only for v1 (design §10, Q5). Nothing here is secret, since
no password exists.

**The data is never kept.** It lives on the attempt's cache disk, is never
checkpointed, and is removed when the attempt ends; a resumed attempt starts
an empty server. Within one attempt the worker's server keeps its data across
the agent's turn and both pre-publish rounds, so tests that need an empty or
known state must create it themselves.

**Where it runs.** On a runner with its own cache volume (`SWARM_CACHE_ROOT`),
which the scheduler sets on every GKE pod and never on Cloud Run
(`apps/scheduler/scheduler/dispatch.py::CACHE_MOUNT`); every profile that runs
the pre-publish checks is on GKE. A runner without one keeps its workspace in
memory, and the worker refuses there rather than put a database in memory:
`postgres: service not started: test services need SWARM_CACHE_ROOT on its
own volume; this runner keeps its workspace in memory`
(`apps/agent-worker/agent_worker/testservices.py::NO_CACHE_VOLUME`).

## `skipped`, `failed` and `error` once a service is declared

`skipped` means the check could not judge the change and CI will; `failed`
means it judged the change and the agent gets one fix turn with the output;
`error` means something outside the change went wrong, recorded in the pull
request's advisory section and never sent to the agent.

| what happened | `graph-pytest` (and the guards script) | `test-services` |
|---|---|---|
| no file | as before: every test refused at a local address is `skipped`, "a service the tests need is unreachable (127.0.0.1:5432) ...; CI runs it" | not shown |
| declared, server answering, a test fails or errors | **`failed`**, fix turn | not shown |
| declared, server answering, every test refused at `127.0.0.1:5432` | **`failed`**: the server was there, so the tests connect to it wrongly | not shown |
| declared, server answering, every test refused at another address (say `:6379`) | `skipped`, naming the address "and not declared in .swarm/test-services.toml" | not shown |
| declared, the server stopped during the run (crash, or killed by the OOM killer) | `error`: "the declared postgres stopped during the run (...)", no fix turn | not shown |
| declared, the server did not start | the tests run without it, so `skipped` as with no file | `error`, with the server's last log lines |
| the file is invalid and **this change** made it so | the tests run as if nothing were declared | **`failed`**, the refusals, fix turn |
| the file is invalid on the **base branch** | the tests run as if nothing were declared | `error`, advisory only |

So the one thing a declaration changes for a repository whose tests are
correct is that a broken database test is caught before the pull request
opens, instead of in CI. The decisions are in
`apps/agent-worker/agent_worker/prepublish.py::Gate._services` and
`apps/agent-worker/agent_worker/prepublish.py::_pytest_project`.
