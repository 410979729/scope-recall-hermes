"""Corpus time and bounded monotonic clocks shared by benchmark helpers."""

import time


class WorldClock:
    """The world's time, moved by the store's builder, and a monotonic clock that ticks ``TICK`` each time it is read
    unless ``realtime`` is set.  A recall reads it a few hundred times, so none comes near its deadline of seconds on
    any machine, and a loop that waits on it still ends."""

    TICK = 1e-6

    def __init__(self, now: str) -> None:
        """Set corpus time and initialize the deterministic monotonic counter."""
        self.now = now
        self.realtime = False
        self._ticks = 0

    def utc_now(self) -> str:
        """Return the injected corpus time."""
        return self.now

    def monotonic(self) -> float:
        """Read real elapsed time or advance one deterministic deadline tick."""
        if self.realtime:
            return time.monotonic()
        self._ticks += 1
        return self._ticks * self.TICK
