"""your-first-instrument — a sense of time for a model that has none.

Why time? Ask your Claude "how long have we been talking?" WITHOUT this
connected. It can only guess: no clock lives in a context window. This
server is the smallest honest fix — and the pattern generalizes to any
instrument you can imagine. See docs/adr/ for every choice made here.
"""
import json
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "your-first-instrument",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", 8000)),
)

# Where the courses actually meet. The server's own clock is NOT the
# authority: deployed on Render it runs in UTC, so after 8pm Eastern
# `date.today()` would roll to tomorrow and quietly drop a day from every
# deadline. Class is in Philadelphia; the planner counts in Philadelphia.
COURSE_TZ = ZoneInfo("America/New_York")


def _today() -> date:
    """Today's date in Philadelphia, wherever this server happens to run."""
    return datetime.now(COURSE_TZ).date()


@mcp.tool()
def current_time() -> str:
    """The current date and time (UTC, Philadelphia, and the server's own local)."""
    now = datetime.now(timezone.utc)
    return (f"UTC: {now.isoformat()} · Philadelphia: "
            f"{now.astimezone(COURSE_TZ).isoformat()} · "
            f"server local: {datetime.now().isoformat()}")

@mcp.tool()
def seconds_since(iso_timestamp: str) -> str:
    """Seconds elapsed since an ISO timestamp (e.g. '2026-09-10T17:15:00')."""
    then = datetime.fromisoformat(iso_timestamp)
    if then.tzinfo is None:
        # Naive timestamps are read as LOCAL time, not UTC. (Bug caught in-room
        # during the R5 leveling test, session 3: a student read the code with
        # their collaborator and noticed line-2 assumed UTC while now() was
        # offset-aware — the audit test, passing in the wild. Thank you.)
        then = then.replace(tzinfo=datetime.now().astimezone().tzinfo)
    delta = datetime.now(timezone.utc) - then
    return f"{delta.total_seconds():.0f} seconds ({delta})"

# ─── the reading planner ──────────────────────────────────────────────────
# COMM 2898 and URBS 1153, Fall 2026. The schedule lives in readings.json, NOT
# in this file: a semester moves weekly, and you shouldn't edit Python to push
# a due date. Combined with the clock above, the model can finally answer
# "what do I owe, by when, and how many hours is that really?"

READINGS_PATH = Path(__file__).parent / "readings.json"

# Pages per hour, by how hard the text is to move through. These are STARTING
# GUESSES — calibrate them to yourself: time one real reading, divide pages by
# hours, replace the number. A rate you measured beats one you inherited.
PAGES_PER_HOUR = {
    "dense": 13,     # philosophy, legal opinions, close argument (Sandel, Crenshaw, SFFA)
    "moderate": 20,  # history and scholarly prose (Sugrue, Countryman, Cohen)
    "light": 30,     # journalism, transcripts, op-eds
}
NOTES_MULTIPLIER = 1.25  # annotating/outlining costs about a quarter more time


def _load_readings() -> list[dict]:
    if not READINGS_PATH.exists():
        return []
    return json.loads(READINGS_PATH.read_text())


def _hours_for(item: dict) -> float:
    """Hours this item will actually cost. Milestones cost nothing to 'do'."""
    kind = item.get("kind", "reading")
    if kind == "milestone":
        return 0.0
    if kind == "media":
        return item.get("minutes", 0) / 60
    rate = PAGES_PER_HOUR.get(item.get("density", "moderate"), 15)
    hours = item["pages"] / rate
    if item.get("notes"):
        hours *= NOTES_MULTIPLIER
    return hours


def _fmt_hours(hours: float) -> str:
    h, m = divmod(round(hours * 60), 60)
    if h and m:
        return f"{h}h {m:02d}m"
    return f"{h}h" if h else f"{m}m"


def _describe(item: dict) -> str:
    kind = item.get("kind", "reading")
    if kind == "milestone":
        return f"    ▸ {item['title']}"
    if kind == "media":
        return f"    · {item['title']} · {item['minutes']}min ≈ {_fmt_hours(_hours_for(item))}"
    # "~" flags a page count I estimated rather than read off a page range.
    pages = f"{'~' if item.get('estimated') else ''}{item['pages']}pp"
    tags = item.get("density", "moderate") + ("+notes" if item.get("notes") else "")
    return f"    · {item['title']} · {pages} {tags} ≈ {_fmt_hours(_hours_for(item))}"


@mcp.tool()
def readings_due(days: int = 7, course: Optional[str] = None) -> str:
    """Readings due in the next N days for COMM 2898 and URBS 1153, grouped by
    class date, with the hours each needs and a day-by-day pace to finish on
    time. Knows today's date, so 'how much reading do I have this week?' and
    'how many hours should I put in?' are answerable. Filter with course=
    'COMM 2898' or 'URBS 1153'."""
    today = _today()
    horizon = today + timedelta(days=days)

    by_date: dict[date, list[dict]] = defaultdict(list)
    for item in _load_readings():
        if course and course.lower() not in item["course"].lower():
            continue
        due = date.fromisoformat(item["due"])
        if today <= due <= horizon:
            by_date[due].append(item)

    lines = [f"Today is {today:%A, %B %-d, %Y}. Due within {days} days:", ""]
    grand_total = 0.0

    for due in sorted(by_date):
        items = by_date[due]
        hours = sum(_hours_for(i) for i in items)
        grand_total += hours
        left = (due - today).days

        if left == 0:
            header = f"{due:%a %b %-d} — TODAY"
            pace = "all of it today"
        else:
            header = f"{due:%a %b %-d} — {left} day{'s' if left > 1 else ''} out"
            pace = f"~{_fmt_hours(hours / left)}/day if this were the only thing"

        lines.append(f"{header} · ≈ {_fmt_hours(hours)}  →  {pace}" if hours
                     else f"{header}")
        for c in sorted({i["course"] for i in items}):
            lines.append(f"  {c}")
            lines += [_describe(i) for i in items if i["course"] == c]
        lines.append("")

    if not by_date:
        return f"Today is {today:%A, %B %-d, %Y}. Nothing due in the next {days} days."

    rate, binding = _sustainable_rate(by_date, today)
    lines.append(f"TOTAL over the next {days} days: {_fmt_hours(grand_total)}")
    lines.append(
        f"THE REAL NUMBER: ≈ {_fmt_hours(rate)}/day, every day, to stay ahead of "
        f"every deadline. (Set by {binding:%a %b %-d} — that's your bottleneck.)"
    )
    lines.append(
        f"Spreading the total evenly would be {_fmt_hours(grand_total / days)}/day, "
        "but that misses earlier deadlines — ignore it."
    )
    lines.append("(~ = page count estimated, not read off a page range. Fix it in readings.json.)")
    return "\n".join(lines)


def _sustainable_rate(by_date: dict, today: date) -> tuple:
    """The slowest constant daily pace that still meets EVERY deadline.

    Per-deadline paces are computed in isolation, so they silently double-book
    the same calendar days — 'work 2h/day for Monday' and 'work 1h/day for next
    Monday' both mean *tomorrow*. The honest figure is the largest cumulative
    load-over-time-remaining across all deadlines: whichever due date demands
    the steepest rate sets the pace for everything before it.
    """
    cumulative, rate, binding = 0.0, 0.0, None
    for due in sorted(by_date):
        cumulative += sum(_hours_for(i) for i in by_date[due])
        left = max((due - today).days, 1)  # due today → you have today, not zero days
        if cumulative / left > rate:
            rate, binding = cumulative / left, due
    return rate, binding


@mcp.tool()
def next_class_prep(course: Optional[str] = None) -> str:
    """Everything due for the very next class meeting, and the hours it needs.
    Answers 'what's the next thing I actually have to read?'"""
    today = _today()
    upcoming = [i for i in _load_readings()
                if date.fromisoformat(i["due"]) >= today
                and (not course or course.lower() in i["course"].lower())]
    if not upcoming:
        return "Nothing upcoming in readings.json."

    due = min(date.fromisoformat(i["due"]) for i in upcoming)
    items = [i for i in upcoming if date.fromisoformat(i["due"]) == due]
    hours = sum(_hours_for(i) for i in items)
    left = (due - today).days
    when = "today" if left == 0 else f"in {left} day{'s' if left > 1 else ''}"

    lines = [f"Next up: {due:%A, %B %-d} ({when}) · ≈ {_fmt_hours(hours)} of work", ""]
    for c in sorted({i["course"] for i in items}):
        lines.append(f"  {c}")
        lines += [_describe(i) for i in items if i["course"] == c]
    if left > 0:
        lines += ["", f"Pace to be ready: ~{_fmt_hours(hours / left)}/day for {left} days."]
    return "\n".join(lines)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
