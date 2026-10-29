"""Tests for apps/swarm-install (the Day One installer).

apps/swarm-install is a member of the root uv workspace (pyproject.toml, the
dev group), so `swarm_install` is imported as an installed package like every
other app. Every service Dockerfile's `uv export` prunes it
(tests/unit/scripts/test_service_image_install.py): no service image ships it.
"""
