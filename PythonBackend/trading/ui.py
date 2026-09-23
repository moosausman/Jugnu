"""
ui.py -- the terminal's typography.

Every panel is built from the same few primitives here, and all of them
take the window width and respect it. The first version of this screen
hardcoded column positions, which looked fine in a wide window and turned
into confetti in an 80-column one: numbers under the wrong headings, prose
sliced mid-word, charts wider than the pane.

Rules:
  * nothing is ever wider than the window; tables shed columns instead
  * numbers are right-aligned in columns measured from the content
  * colour means something (up, down, warning, accent) and is never
    decoration, so the screen reads in a plain-white terminal too
  * one gutter, two spaces, used by everything
"""
from __future__ import annotations

import re
import textwrap

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
INK = "\x1b[38;5;253m"
DIM = "\x1b[38;5;247m"
FAINT = "\x1b[38;5;243m"
CLAY = "\x1b[38;5;209m"
GREEN = "\x1b[38;5;114m"
RED = "\x1b[38;5;203m"
AMBER = "\x1b[38;5;179m"
BAR = "\x1b[48;5;236m"

GUTTER = "  "
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def plain(text: str) -> str:
    return _ANSI.sub("", text)


def width_of(text: str) -> int:
    return len(plain(text))


visible_width = width_of        # the terminal imports it under this name


def fit(text: str, width: int) -> str:
    """Exactly `width` visible characters, colours preserved."""
    length = width_of(text)
    if length <= width:
        return text + " " * (width - length)
    out, count, i = "", 0, 0
    while i < len(text) and count < width - 1:
        if text[i] == "\x1b":
            end = text.find("m", i)
            if end == -1:
                break
            out += text[i:end + 1]
            i = end + 1
            continue
        out += text[i]
        count += 1
        i += 1
    return out + "…" + RESET


def pad(text: str, width: int, align: str = "<") -> str:
    """Pad to a column width without truncating -- for table cells, where
    the caller has already budgeted the space."""
    gap = max(0, width - width_of(text))
    if align == ">":
        return " " * gap + text
    if align == "^":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def money(value: float, unit: str = "") -> str:
    if abs(value) >= 1_000_000_000:
        return f"{unit}{value / 1e9:,.1f}bn"
    if abs(value) >= 100_000:
        return f"{unit}{value / 1000:,.1f}k"
    if abs(value) >= 1000:
        return f"{unit}{value:,.0f}"
    return f"{unit}{value:,.2f}"


def percent(value: float, digits: int = 1, colour: bool = True, sign: bool = True) -> str:
    text = f"{value:+.{digits}f}%" if sign else f"{value:.{digits}f}%"
    if not colour:
        return text
    return (GREEN if value >= 0 else RED) + text + RESET


def tint(value: float, text: str) -> str:
    return (GREEN if value >= 0 else RED) + text + RESET


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def rule(label: str = "", width: int = 80, colour: str = CLAY) -> str:
    """A section divider. The label sits in the line, not above it, so a
    screenful of results still reads as a list of sections."""
    if not label:
        return FAINT + "─" * width + RESET
    head = f"{FAINT}──{RESET} {colour}{label}{RESET} "
    tail = max(0, width - width_of(head))
    return head + FAINT + "─" * tail + RESET


def heading(text: str) -> str:
    return f"{GUTTER}{BOLD}{INK}{text}{RESET}"


def note(text: str, width: int, marker: str = "", colour: str = DIM,
         indent: str = GUTTER) -> list[str]:
    """Prose, wrapped to the window. Never truncated mid-thought."""
    prefix = f"{marker} " if marker else ""
    room = max(30, width - len(indent) - len(plain(prefix)))
    lines = textwrap.wrap(text, room) or [""]
    out = [f"{indent}{colour}{prefix}{lines[0]}{RESET}"]
    pad_left = indent + " " * len(plain(prefix))
    out += [f"{pad_left}{colour}{line}{RESET}" for line in lines[1:]]
    return out


def stats(pairs: list[tuple[str, str]], width: int, per_row: int | None = None) -> list[str]:
    """Label-above-value blocks, packed into as many columns as fit.

    This is the layout that fixed the screen: figures used to be strung
    along one line with labels between them, which at 80 columns wrapped
    into nonsense. Now each figure owns a column, and the number of columns
    follows the window.
    """
    if not pairs:
        return []
    cell = max(max(width_of(label) for label, _ in pairs),
               max(width_of(value) for _, value in pairs)) + 3
    columns = per_row or max(1, min(len(pairs), (width - len(GUTTER)) // cell))
    rows: list[str] = []
    for start in range(0, len(pairs), columns):
        chunk = pairs[start:start + columns]
        rows.append(GUTTER + "".join(f"{FAINT}{pad(label, cell)}{RESET}" for label, _ in chunk))
        rows.append(GUTTER + "".join(pad(value, cell) for _, value in chunk))
        rows.append("")
    return rows[:-1]


def table(headers: list[str], rows: list[list[str]], width: int,
          aligns: str = "", trim: list[int] | None = None) -> list[str]:
    """A table that fits. Columns are measured from their contents; when
    the total is too wide, the columns in `trim` (least important first)
    are dropped rather than letting the line wrap."""
    if not rows:
        return []
    keep = list(range(len(headers)))
    aligns = (aligns or "<" * len(headers)).ljust(len(headers), "<")

    def measure(columns: list[int]) -> list[int]:
        return [max(width_of(headers[c]),
                    max((width_of(row[c]) for row in rows if c < len(row)), default=0)) + 2
                for c in columns]

    while len(keep) > 1 and sum(measure(keep)) + len(GUTTER) > width:
        droppable = [c for c in (trim or []) if c in keep]
        if not droppable:
            break
        keep.remove(droppable[0])
    widths = measure(keep)

    out = [GUTTER + "".join(f"{FAINT}{pad(headers[c], w, aligns[c])}{RESET}"
                            for c, w in zip(keep, widths))]
    for row in rows:
        out.append(GUTTER + "".join(pad(row[c] if c < len(row) else "", w, aligns[c])
                                    for c, w in zip(keep, widths)))
    return out


def bar(value: float, scale: float, width: int, colour: str | None = None) -> str:
    """A proportional bar; scale is the value that fills it."""
    if scale <= 0:
        return ""
    length = max(0, min(width, int(abs(value) / scale * width)))
    colour = colour or (GREEN if value >= 0 else RED)
    return colour + "█" * length + RESET + " " * (width - length)


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int) -> str:
    if not values or width < 4:
        return ""
    step = max(1, len(values) / width)
    sampled = [values[min(len(values) - 1, int(i * step))] for i in range(min(width, len(values)))]
    low, high = min(sampled), max(sampled)
    if high - low < 1e-12:
        return SPARK[0] * len(sampled)
    return "".join(SPARK[min(7, int((v - low) / (high - low) * 7))] for v in sampled)


def chart(series: list[tuple[list[float], str, str]], width: int, height: int,
          left_label: str = "", right_label: str = "", axis: bool = True) -> list[str]:
    """Overlaid series in one panel, sized to the window. Each series is
    (values, mark, colour); the first is drawn behind the rest."""
    if not series or width < 24:
        return []
    axis_width = 10 if axis else 0
    plot_width = max(10, width - axis_width - len(GUTTER))
    sampled = []
    for values, mark, colour in series:
        if not values:
            continue
        step = max(1, len(values) / plot_width)
        sampled.append(([values[min(len(values) - 1, int(i * step))]
                         for i in range(plot_width)], mark, colour))
    if not sampled:
        return []
    low = min(min(v) for v, _, _ in sampled)
    high = max(max(v) for v, _, _ in sampled)
    span = (high - low) or 1
    out = []
    for level in range(height, 0, -1):
        top = low + span * level / height
        bottom = low + span * (level - 1) / height
        line = ""
        for i in range(plot_width):
            drawn = " "
            for values, mark, colour in reversed(sampled):
                value = values[i]
                if bottom <= value < top or (level == height and value >= top):
                    drawn = f"{colour}{mark}{RESET}"
                    break
            line += drawn
        label = f"{money(top):>{axis_width - 2}}" if axis else ""
        out.append(f"{GUTTER}{FAINT}{label} │{RESET}{line}" if axis else GUTTER + line)
    if axis:
        out.append(f"{GUTTER}{FAINT}{' ' * (axis_width - 2)} └{'─' * plot_width}{RESET}")
    if left_label or right_label:
        gap = max(1, plot_width - len(left_label) - len(right_label))
        out.append(f"{GUTTER}{FAINT}{' ' * axis_width}{left_label}{' ' * gap}{right_label}{RESET}")
    return out


def legend(items: list[tuple[str, str, str]]) -> str:
    """(mark, colour, label) …"""
    return GUTTER + "   ".join(f"{colour}{mark}{RESET} {FAINT}{label}{RESET}"
                               for mark, colour, label in items)
