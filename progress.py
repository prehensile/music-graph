#!/usr/bin/env python3
"""
Periodic plain-text progress reporting.

This replaces alive_progress. A drawn progress bar assumes a terminal is
watching: it redraws with control characters, and it hooks stdout so that
anything else printed inside its context gets rewritten. Neither suits this
project, where the long runs are unattended and their output is a log file
read back through a web page.

A timestamped line every PROGRESS_INTERVAL seconds behaves identically whether
or not anyone is looking, greps cleanly, and costs nothing per record.

    hb = Heartbeat("release", total=file_size, unit="releases")
    for record in ...:
        hb.tick(position=fp.tell())
    hb.finish()

Set PROGRESS_INTERVAL=0 to silence it.
"""

import os
import sys
import time

INTERVAL = float(os.environ.get("PROGRESS_INTERVAL", "30"))


def format_duration(seconds):
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


class Heartbeat:
    """
    Counts work and prints a progress line at most every INTERVAL seconds.

    `total` and the `position` passed to tick() must share units -- bytes when
    tracking progress through a file, rows when tracking a query. Leave both
    unset to report a bare count with no percentage or ETA.
    """

    def __init__(self, label, total=None, unit="records", stream=None):
        self.label = label
        self.total = total or 0
        self.unit = unit
        self.stream = stream or sys.stdout
        self.start = time.time()
        self.last = self.start
        self.count = 0

    def _write(self, text):
        print(f"  [{self.label}] {text}", file=self.stream, flush=True)

    def begin(self, note=""):
        self._write(f"starting{' ' + note if note else ''}")

    def tick(self, count=None, position=None):
        self.count = self.count + 1 if count is None else count
        if not INTERVAL:
            return
        now = time.time()
        if now - self.last < INTERVAL:
            return
        self.last = now

        elapsed = max(now - self.start, 1e-6)
        parts = []
        pos = self.count if position is None else position
        if self.total and pos > 0:
            parts.append(f"{pos / self.total * 100:5.1f}%")
        parts.append(f"{self.count:,} {self.unit}")
        parts.append(f"{self.count / elapsed:,.0f}/s")
        if self.total and pos > 0:
            parts.append(f"ETA {format_duration((self.total - pos) / (pos / elapsed))}")
        self._write(" | ".join(parts))

    def finish(self, note=""):
        elapsed = time.time() - self.start
        suffix = f", {note}" if note else ""
        self._write(
            f"done: {self.count:,} {self.unit} in {format_duration(elapsed)}"
            f" ({self.count / max(elapsed, 1e-6):,.0f}/s){suffix}"
        )
