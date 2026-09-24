"""HTTP routes. Every tenant-scoped handler takes its tenant from the token."""

from . import admin, attempts, health, platform, tasks, tenants, workflows

__all__ = ["admin", "attempts", "health", "platform", "tasks", "tenants", "workflows"]
