"""The plugins bucket, faked in memory: no credentials, no emulator, no socket."""

from __future__ import annotations


class FakeBucket:
    """Objects by name; records every read and create the job makes.

    `create` refuses a name already there, as `ifGenerationMatch=0` does, with
    `create_error` (when set) raised instead of any create, as a denied or
    unreachable bucket would.
    """

    def __init__(self, objects: dict[str, bytes] | None = None, create_error: Exception | None = None) -> None:
        self.objects = dict(objects or {})
        self.reads: list[tuple[str, int]] = []
        self.creates: list[tuple[str, str]] = []
        self.create_error = create_error

    def read(self, name: str, cap: int) -> tuple[bytes | None, int]:
        from plugin_intake.bucket import ObjectNotFound

        self.reads.append((name, cap))
        if name not in self.objects:
            raise ObjectNotFound()
        data = self.objects[name]
        return (None, len(data)) if len(data) > cap else (data, len(data))

    def create(self, name: str, data: bytes, content_type: str) -> None:
        from plugin_intake.bucket import ObjectExists

        if self.create_error is not None:
            raise self.create_error
        if name in self.objects:
            raise ObjectExists()
        self.objects[name] = data
        self.creates.append((name, content_type))
