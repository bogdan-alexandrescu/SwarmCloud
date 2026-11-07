"""Every way an intake can refuse, as a fixed code and a fixed sentence.

docs/design/managed-plugins.md §2.3: a refusal carries nothing interpolated. An
entry's path, a manifest's name, a hook's command are the archive's text, which
is the attacker's text, so none of them ever enters a refusal, a log line or the
report's refusal block. The only detail a refusal carries is the offending
entry's index in the archive's own order, as a separate integer field.
"""

from __future__ import annotations

#: code -> sentence. The console and swarm-api look a code up; they never parse
#: the sentence, and a test holds every sentence free of format fields.
REFUSALS: dict[str, str] = {
    # The archive as a whole.
    "archive_too_large": "The archive is larger than 100 MiB.",
    "archive_format_unknown": "The archive is not a .zip or a .tar.gz file.",
    "archive_corrupt": "The archive cannot be read.",
    "archive_empty": "The archive holds no files.",
    "unpacked_too_large": "The archive unpacks to more than 500 MiB.",
    "too_many_files": "The archive holds more than 20,000 files.",
    "expansion_ratio": "The archive expands more than 100 times its packed size.",
    # One entry's name.
    "entry_path_absolute": "An entry's path is absolute.",
    "entry_path_parent": "An entry's path has a '..' segment.",
    "entry_path_invalid": "An entry's path holds a backslash, a NUL, a drive letter or bytes that are not UTF-8.",
    "entry_path_too_deep": "An entry's path has more than 32 segments.",
    "entry_path_segment_too_long": "An entry's path has a segment longer than 255 bytes.",
    "entry_duplicate": "Two entries have the same path, ignoring case and Unicode normalisation.",
    # One entry's kind and mode.
    "entry_special_file": "An entry is a device, a FIFO or a socket.",
    "entry_type_unsupported": "An entry has a type the intake does not accept.",
    "entry_setuid": "An entry has the setuid or setgid bit.",
    "entry_encrypted": "An entry is encrypted.",
    "entry_under_link": "An entry's path passes through a symbolic link.",
    "entry_escapes_root": "An entry's path leaves the plugin's folder.",
    "link_escapes_root": "A symbolic link points outside the plugin's folder.",
    "link_target_missing": "A symbolic link points to nothing in the archive.",
    "link_to_link": "A symbolic link points to another symbolic link.",
    "hardlink_escapes_root": "A hard link points outside the plugin's folder or to something that is not a file.",
    # The plugin root.
    "subdir_invalid": "The requested folder is not a valid relative path.",
    "subdir_missing": "The requested folder is not in the archive.",
    # Dependencies.
    "package_json_invalid": "A package.json is not a JSON object.",
    "dependencies_not_shipped": "A package.json lists dependencies but no node_modules folder is beside it.",
    # The manifest and its components.
    "manifest_invalid": "The plugin's .claude-plugin/plugin.json is not a valid manifest.",
    "manifest_user_config_invalid": "The plugin's userConfig has a field the intake does not accept.",
    "manifest_component_unknown": "The plugin declares a component in a shape the intake does not know.",
    "manifest_path_outside_root": "The plugin's manifest names a path outside the plugin's folder.",
    "manifest_path_missing": "The plugin's manifest names a path that is not in the archive.",
    "hooks_invalid": "The plugin's hooks are not valid hook declarations.",
    "hook_unknown": "The plugin declares a hook event or type the intake does not accept.",
    "hook_path_missing": "A hook names a file in the plugin that is not there.",
    "mcp_invalid": "The plugin's MCP servers are not valid server declarations.",
    "mcp_path_missing": "An MCP server names a file in the plugin that is not there.",
    "skill_invalid": "A skill's SKILL.md cannot be read.",
    # The admin's overlay (§6.3).
    "overlay_invalid": "The overlay is not a valid overlay.",
    "overlay_replaces_manifest": "The overlay declares a manifest but the plugin ships its own.",
    "overlay_replaces_hooks": "The overlay declares hooks but the plugin ships its own.",
    "overlay_conflict": "The plugin already holds a file the overlay writes.",
    "overlay_variable_denied": "An overlay setting names an environment variable no plugin may set.",
    "overlay_hook_path_missing": "An overlay hook names a file that is not in the plugin.",
    # Neither the source nor the overlay names the plugin (§2.3 step 5).
    "manifest_missing": "The plugin has no .claude-plugin/plugin.json and the overlay declares none.",
    # The job itself.
    "intake_internal_error": "The intake job failed before it finished its checks.",
}


class Refusal(Exception):
    """An intake refused, by code; `entry_index` is the archive's own index."""

    def __init__(self, code: str, entry_index: int | None = None) -> None:
        if code not in REFUSALS:
            raise KeyError(code)
        super().__init__(code)
        self.code = code
        self.entry_index = entry_index

    def as_report(self) -> dict[str, object]:
        return {"code": self.code, "sentence": REFUSALS[self.code], "entry_index": self.entry_index}
