"""Migration 0001: every tenant has its workspace record (docs/workspaces.md §3.3;
was scripts/workspace-migrate-record.sh).

The submission gate refuses every tenant whose `workspaces/<tenant>` record is
not `ready`. A tenant whose resources Terraform made (before workspaces
existed) has a `tenants/<tenant>` document and no workspace record; the live
deployment's one such tenant was migrated by hand on 2026-10-10. A Day One
install has none: every team is made through the workspace path
(`swarm team add`, Admin › Setup), which writes the record first.

The record's shape is `swarm_api.workspaces.Workspaces.migrate`'s and stated
nowhere else, and that write needs the namespace's live ResourceQuota and the
forge slot pair (scripts/lib/forge-slot.sh). This wheel carries neither, so
this migration never writes a record: it names every tenant without one and
stops the upgrade before any new code serves, rather than let the new
release refuse that tenant's submissions without saying why.
"""

from __future__ import annotations

from . import ADDITIVE, Documents, MigrationFailed, Outcome

TENANTS = "tenants"
WORKSPACES = "workspaces"


class WorkspaceRecord:
    number = 1
    name = "workspace_record"
    phase = ADDITIVE
    explain = "every tenants/<id> has its workspaces/<id> record, so the submission gate admits it"

    def pending(self, db: Documents) -> list[str]:
        missing = []
        for path, _data, _ in db.list(TENANTS):
            tenant = path.split("/", 1)[1]
            if db.get(f"{WORKSPACES}/{tenant}") is None:
                missing.append(tenant)
        return sorted(missing)

    def apply(self, db: Documents) -> Outcome:
        missing = self.pending(db)
        if missing:
            raise MigrationFailed(
                self.number, self.name,
                f"{', '.join(missing)} {'has' if len(missing) == 1 else 'have'} no workspaces/<tenant> record: "
                "Terraform made them before workspaces existed. Their record is written by swarm-api's "
                "Workspaces.migrate with the namespace's live quota, which this installer cannot do for you; "
                "nothing was changed, and the upgrade resumes here once each record exists",
            )
        return Outcome(changed=0, detail="every tenant has its workspace record")


MIGRATION = WorkspaceRecord()
