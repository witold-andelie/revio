"""Startup mascot: a small ASCII owl whose eyes alternately flash a
magnifying-glass border, hinting at revio's core motion (scanning code).

The owl is intentionally tiny (~5 rows × ~15 cols) so it fits in any
80-col terminal without crowding the welcome panel that follows.

Skips itself on non-tty stdout (CI, piped output) and when
REVIO_NO_MASCOT=1 is set — so logs and headless callers stay clean.
"""

from __future__ import annotations

import os
import sys
import time

from rich.align import Align
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text


# Each frame: owl ASCII with the {LE} / {RE} slots replaced by either the
# resting eye glyph or the magnifier-wrapped eye. Each eye-slot is exactly
# 3 columns wide so the silhouette never jitters between frames.
EYE_REST_RAW = " ◉ "          # 3 cols: padding · eye · padding
EYE_SCAN_RAW = "(◉)"           # 3 cols: lens · eye · lens — the magnifier flash

# Body width 11 chars (│ + 9 interior + │). Inside the body: pad · eye3 ·
# gap · eye3 · pad = 1+3+1+3+1 = 9.
_TEMPLATE = """\
[cyan dim]   ╭─────────╮[/]
[cyan dim]   │[/] {LE} {RE} [cyan dim]│[/]
[cyan dim]   │    [/][yellow]v[/][cyan dim]    │[/]
[cyan dim]   ╰────╥────╯[/]
[cyan dim]        ║[/]\
"""


def _wrap_eye(raw: str, scanning: bool) -> str:
    return f"[bold yellow]{raw}[/]" if scanning else f"[bold white]{raw}[/]"


def _frame(left_scanning: bool, right_scanning: bool) -> Text:
    le = _wrap_eye(EYE_SCAN_RAW if left_scanning else EYE_REST_RAW, left_scanning)
    re = _wrap_eye(EYE_SCAN_RAW if right_scanning else EYE_REST_RAW, right_scanning)
    body = _TEMPLATE.replace("{LE}", le).replace("{RE}", re)
    return Text.from_markup(body, justify="left")


# Animation cycle — ~1.4s total. The owl looks left, looks right, blinks,
# settles. Last frame is the resting pose left on screen below.
_SCRIPT: list[tuple[bool, bool, float]] = [
    (False, False, 0.18),  # idle
    (True,  False, 0.22),  # left scan
    (False, False, 0.10),  # blink-back
    (False, True,  0.22),  # right scan
    (False, False, 0.10),
    (True,  True,  0.18),  # both lit (focusing)
    (False, False, 0.20),  # rest
]


def play_startup_animation(console: Console | None = None) -> None:
    """Play the owl animation once on startup. No-op outside a TTY."""
    if os.environ.get("REVIO_NO_MASCOT") == "1":
        return
    if not (console or Console()).is_terminal:
        return
    if not sys.stdout.isatty():
        return

    console = console or Console()

    label = Text("scanning…", style="dim italic", justify="left")
    rest_label = Text("revio", style="bold cyan", justify="left")

    with Live(
        Group(_frame(False, False), label),
        console=console,
        refresh_per_second=20,
        transient=False,
    ) as live:
        for left, right, dwell in _SCRIPT:
            owl = _frame(left, right)
            shown_label = label if (left or right) else rest_label
            live.update(Group(owl, shown_label))
            time.sleep(dwell)


if __name__ == "__main__":
    play_startup_animation()
