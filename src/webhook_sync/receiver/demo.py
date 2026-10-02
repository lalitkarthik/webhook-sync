"""The demo fail-first switch (receiver.md, Q106, Q120): `wsync serve --demo-fail-first N`.

A stored delivery is answered 500 for the first N attempts of its event id, the first included,
so the source's own retries make the duplicate-storm scene. The count lives in memory.
"""

from dataclasses import dataclass, field


@dataclass
class FailFirst:
    """Counts each event id's attempts and answers 500 to the first `n`."""

    n: int
    _attempts: dict[tuple[str, str], int] = field(default_factory=dict[tuple[str, str], int])

    def attempt(self, source: str, event_id: str) -> int:
        """Count one more attempt of this event and return its number, the first being 1."""
        key = (source, event_id)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        return self._attempts[key]

    def answer(self, attempt: int, status_code: int) -> int:
        """The status this attempt is answered with: 500 up to `n`, then the normal one."""
        return 500 if attempt <= self.n else status_code
