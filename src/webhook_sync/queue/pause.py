"""Paused sources and sinks: in P1 an in-memory flag per source and sink (A28).

`claim` hands out nothing of a paused source or sink; a `pause`-class failure sets the flag.
The `sinks` table, probes and `wsync resume` come with P3.
"""

from typing import Literal

type PauseTarget = Literal["source", "sink"]
"""What a `pause`-class error stops: the source it read from, or the sink it wrote to."""


class Pauses:
    def __init__(self) -> None:
        self._sources: set[str] = set()
        self._sinks: set[str] = set()

    def pause(self, target: PauseTarget, name: str) -> None:
        (self._sources if target == "source" else self._sinks).add(name)

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(self._sources)

    @property
    def sinks(self) -> frozenset[str]:
        return frozenset(self._sinks)
