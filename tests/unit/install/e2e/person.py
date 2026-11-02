"""The scripted person at the terminal: answers each prompt from a script,
and records every prompt so the walk can assert what was (and was not) asked."""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: A prompt that sends the person off to run something outside `swarm install`.
RUN_ELSEWHERE = re.compile(
    r"(\brun\b[^.\n]*(`|\b)(gcloud|kubectl|terraform|scripts/|bash |sh |make |curl )"
    r"|copy (this|the) command|paste (it|this) into)",
    re.IGNORECASE,
)


@dataclass
class Person:
    """`answers` maps a regex over the prompt to the answer typed."""

    answers: list[tuple[str, str]] = field(default_factory=list)
    confirms: list[tuple[str, str]] = field(default_factory=list)
    asked: list[tuple[str, str]] = field(default_factory=list)
    unscripted: list[str] = field(default_factory=list)
    secrets_typed: list[str] = field(default_factory=list)
    #: (regex over a page's URL, what the person does there with the world).
    pages: list[tuple[str, Callable[[str, Any], None]]] = field(default_factory=list)

    def on_page(self, url: str, world: Any) -> None:
        for pattern, act in self.pages:
            if re.search(pattern, url):
                act(url, world)

    def _note(self, step: str, prompt: str) -> None:
        self.asked.append((step, prompt))

    def answer(self, step: str, question: str, default: str | None) -> str:
        self._note(step, question)
        for pattern, typed in self.answers:
            if re.search(pattern, question, re.IGNORECASE | re.DOTALL):
                return typed
        if default is not None:
            return default
        self.unscripted.append(f"{step}: {question}")
        return ""

    def secret(self, step: str, what: str, prompt: str) -> str:
        self._note(step, f"(hidden) {prompt}")
        self.secrets_typed.append(what)
        return "typed-" + secrets.token_hex(12)

    def confirm(self, prompt: str) -> str:
        self._note("confirm", prompt)
        for pattern, typed in self.confirms:
            if re.search(pattern, prompt, re.IGNORECASE | re.DOTALL):
                return typed
        self.unscripted.append(f"confirm: {prompt}")
        return ""

    def typed(self, prompt: str) -> str:
        self._note("input", prompt)
        self.unscripted.append(f"input: {prompt}")
        raise EOFError

    def asked_to_run_elsewhere(self) -> list[str]:
        return [f"{step}: {prompt}" for step, prompt in self.asked if RUN_ELSEWHERE.search(prompt)]
