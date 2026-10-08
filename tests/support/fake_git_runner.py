"""Scripted, recording ``GitRunner`` for unit tests; it never starts a process.

Responses are registered by argv prefix. The rule with the longest matching
prefix answers; rules of equal length answer in registration order, and a rule
registered with ``times=N`` is skipped once used up. Every call is recorded
(including unscripted ones) so tests can assert argv, cwd and environment
overrides, which is how credential isolation and hook-disabling are proven.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from wikiops.providers.github_wiki.ports import CommandResult


@dataclass(frozen=True)
class RecordedCall:
    """One ``GitRunner.run`` invocation as the runner received it."""

    argv: tuple[str, ...]
    cwd: Path | None
    env_overrides: Mapping[str, str | None]
    timeout: float
    stdin: str | None


class UnscriptedCallError(AssertionError):
    """A command ran that no scripted rule covers."""


Responder = Callable[[RecordedCall], CommandResult]


@dataclass
class _Rule:
    prefix: tuple[str, ...]
    responder: Responder
    remaining: int | None

    def matches(self, argv: tuple[str, ...]) -> bool:
        if self.remaining == 0:
            return False
        return argv[: len(self.prefix)] == self.prefix


@dataclass
class FakeGitRunner:
    """Hermetic ``GitRunner``: scripted by argv prefix, recording every call."""

    calls: list[RecordedCall] = field(default_factory=list)
    _rules: list[_Rule] = field(default_factory=list)

    def script(
        self,
        prefix: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
        times: int | None = None,
    ) -> None:
        """Answer calls starting with ``prefix`` with a fixed result."""

        def respond(call: RecordedCall) -> CommandResult:
            return CommandResult(call.argv, returncode, stdout, stderr, timed_out)

        self.script_with(prefix, respond, times=times)

    def script_with(
        self, prefix: Sequence[str], responder: Responder, *, times: int | None = None
    ) -> None:
        """Answer calls starting with ``prefix`` with a result computed per call."""
        self._rules.append(_Rule(tuple(prefix), responder, times))

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env_overrides: Mapping[str, str | None],
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult:
        call = RecordedCall(tuple(argv), cwd, dict(env_overrides), timeout, stdin)
        self.calls.append(call)
        candidates = [rule for rule in self._rules if rule.matches(call.argv)]
        if not candidates:
            raise UnscriptedCallError(f"unscripted command: {list(call.argv)}")
        # max() keeps the first of equal-length rules, i.e. registration order.
        rule = max(candidates, key=lambda candidate: len(candidate.prefix))
        if rule.remaining is not None:
            rule.remaining -= 1
        return rule.responder(call)

    @property
    def argvs(self) -> list[tuple[str, ...]]:
        """Every recorded argv, in call order."""
        return [call.argv for call in self.calls]

    def calls_matching(self, prefix: Sequence[str]) -> list[RecordedCall]:
        """The recorded calls whose argv starts with ``prefix``."""
        wanted = tuple(prefix)
        return [call for call in self.calls if call.argv[: len(wanted)] == wanted]
