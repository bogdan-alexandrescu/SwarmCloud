"""HTTP routes. Every tenant-scoped handler takes its tenant from the token."""

from . import admin, health, platform, tasks, tenants, workflows

__all__ = ["admin", "health", "platform", "tasks", "tenants", "workflows"]
