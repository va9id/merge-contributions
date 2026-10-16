#!/usr/bin/env python3
"""
Combine the last year of contributions from a GitHub and a GitLab profile.

Usage:
    python3 merge_contributions.py --github octocat --gitlab johndoe
    python3 merge_contributions.py --github octocat --gitlab johndoe --json out.json
    python3 merge_contributions.py --github octocat --gitlab johndoe --csv out.csv
    python3 merge_contributions.py --github octocat --gitlab johndoe --readme README.md

    --svg writes a GitHub-style SVG calendar (plus a -dark.svg sibling);
    --readme writes that pair next to the README and replaces the README
    with the <picture> element that shows them. The README holds nothing
    else, so anything already in it is overwritten.

GitHub:
    Uses the GraphQL API when GITHUB_TOKEN is set (recommended: exact counts,
    stable schema). Falls back to scraping the public contributions HTML
    fragment when no token is present.

GitLab:
    Uses the public calendar.json endpoint, so the profile must be public.
    Use --gitlab-host for a self-hosted instance.

Output:
    Quiet by default - it prints the README path on success, or a line
    starting with "!" on stderr if nothing could be written.

No third-party dependencies - standard library only.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

UA = "contrib-merge/1.0 (+https://example.invalid)"
TIMEOUT = 30


# --------------------------------------------------------------------------
# HTTP helper
# --------------------------------------------------------------------------


def http(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # One tidy line - error bodies are often a page of HTML.
        body = e.read().decode("utf-8", errors="replace")
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()[:120]
        raise RuntimeError(
            f"HTTP {e.code} from {url}" + (f" - {body}" if body else "")
        ) from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach {url}: {e.reason}") from None


# --------------------------------------------------------------------------
# GitHub
# --------------------------------------------------------------------------

GQL = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def github_graphql(username, token, start, end):
    payload = json.dumps(
        {
            "query": GQL,
            "variables": {
                "login": username,
                "from": f"{start.isoformat()}T00:00:00Z",
                "to": f"{end.isoformat()}T23:59:59Z",
            },
        }
    ).encode()

    raw = http(
        "https://api.github.com/graphql",
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    doc = json.loads(raw)

    if doc.get("errors"):
        msgs = "; ".join(e.get("message", "?") for e in doc["errors"])
        raise RuntimeError(f"GitHub GraphQL error: {msgs}")
    user = (doc.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"GitHub user '{username}' not found")

    cal = user["contributionsCollection"]["contributionCalendar"]
    out = {}
    for week in cal["weeks"]:
        for day in week["contributionDays"]:
            out[day["date"]] = day["contributionCount"]
    return out


# The public fragment renders one <td> per day carrying an id + data-date,
# with the human-readable count in a <tool-tip for="<that id>"> element.
TD_RE = re.compile(
    r'<td[^>]*?id="([^"]+)"[^>]*?data-date="(\d{4}-\d{2}-\d{2})"[^>]*>', re.I
)
TD_RE_ALT = re.compile(
    r'<td[^>]*?data-date="(\d{4}-\d{2}-\d{2})"[^>]*?id="([^"]+)"[^>]*>', re.I
)
TIP_RE = re.compile(r'<tool-tip[^>]*?for="([^"]+)"[^>]*>(.*?)</tool-tip>', re.I | re.S)
LEVEL_RE = re.compile(
    r'<td[^>]*?data-date="(\d{4}-\d{2}-\d{2})"[^>]*?data-level="(\d+)"[^>]*>', re.I
)


def github_scrape(username, start, end):
    url = (
        "https://github.com/users/"
        f"{urllib.parse.quote(username)}/contributions"
        f"?from={start.isoformat()}&to={end.isoformat()}"
    )
    html = http(url, headers={"X-Requested-With": "XMLHttpRequest"})

    if "data-date" not in html:
        raise RuntimeError(
            f"No contribution data in GitHub response for '{username}' "
            "(user may not exist, or the page markup changed)"
        )

    id_to_date = {}
    for m in TD_RE.finditer(html):
        id_to_date[m.group(1)] = m.group(2)
    for m in TD_RE_ALT.finditer(html):
        id_to_date[m.group(2)] = m.group(1)

    counts = {}
    for m in TIP_RE.finditer(html):
        date = id_to_date.get(m.group(1))
        if not date:
            continue
        text = re.sub(r"<[^>]+>", "", m.group(2))
        num = re.search(r"([\d,]+)\s+contribution", text, re.I)
        counts[date] = int(num.group(1).replace(",", "")) if num else 0

    # Days with zero contributions have no tooltip in some renderings;
    # backfill every day the calendar listed.
    for date in id_to_date.values():
        counts.setdefault(date, 0)

    if not counts:  # last resort: approximate from the 0-4 level buckets
        for m in LEVEL_RE.finditer(html):
            counts[m.group(1)] = int(m.group(2))
        if counts:
            print(
                "! GitHub: approximate counts (0-4 buckets). Set "
                "GITHUB_TOKEN for exact numbers.",
                file=sys.stderr,
            )

    return counts


def fetch_github(username, start, end):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return github_graphql(username, token, start, end), "GraphQL API"
    return github_scrape(username, start, end), "public HTML (no token)"


# --------------------------------------------------------------------------
# GitLab
# --------------------------------------------------------------------------


def fetch_gitlab(username, host="https://gitlab.com"):
    url = f"{host.rstrip('/')}/users/{urllib.parse.quote(username)}/calendar.json"
    raw = http(url, headers={"Accept": "application/json"})
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"GitLab returned non-JSON for '{username}' - the profile may be "
            "private or the username wrong."
        ) from None

    if not isinstance(doc, dict):
        raise RuntimeError(f"Unexpected GitLab payload shape: {type(doc).__name__}")

    # calendar.json is {"YYYY-MM-DD": count}
    return {d: int(c) for d, c in doc.items()}, "calendar.json"


# --------------------------------------------------------------------------
# Merge + render
# --------------------------------------------------------------------------


def merge(sources, start, end):
    """sources: {label: {date_str: count}} -> ordered list of per-day rows."""
    totals = defaultdict(lambda: defaultdict(int))
    for label, data in sources.items():
        for date_str, count in data.items():
            totals[date_str][label] += count

    rows = []
    day = start
    while day <= end:
        key = day.isoformat()
        per_source = {label: totals[key].get(label, 0) for label in sources}
        rows.append(
            {
                "date": key,
                **per_source,
                "total": sum(per_source.values()),
            }
        )
        day += dt.timedelta(days=1)
    return rows


MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def level(count, peak):
    if count <= 0:
        return 0
    if peak <= 1:
        return 4
    return min(4, 1 + int(3 * (count - 1) / max(1, peak - 1)))


def calendar_weeks(rows):
    """-> (grid_start, weeks); weeks is a list of 7-item columns, Sun..Sat.

    Cells outside the requested window are None so the first and last
    columns stay ragged, exactly like GitHub's calendar.
    """
    by_date = {r["date"]: r["total"] for r in rows}
    first = dt.date.fromisoformat(rows[0]["date"])
    last = dt.date.fromisoformat(rows[-1]["date"])
    grid_start = first - dt.timedelta(days=(first.weekday() + 1) % 7)

    weeks, day = [], grid_start
    while day <= last:
        col = []
        for _ in range(7):
            in_window = first <= day <= last
            col.append(by_date.get(day.isoformat(), 0) if in_window else None)
            day += dt.timedelta(days=1)
        weeks.append(col)
    return grid_start, weeks


# --------------------------------------------------------------------------
# SVG calendar (GitHub-style) + README embedding
# --------------------------------------------------------------------------

THEMES = {
    "light": {
        "panel": "#ffffff",
        "border": "#d0d7de",
        "title": "#1f2328",
        "muted": "#59636e",
        "scale": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
        "cell_stroke": "rgba(27,31,36,0.06)",
    },
    "dark": {
        "panel": "#0d1117",
        "border": "#30363d",
        "title": "#e6edf3",
        "muted": "#8b949e",
        "scale": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
        "cell_stroke": "rgba(240,246,252,0.10)",
    },
}

# Brand marks on a 24x24 viewBox (simple-icons). The GitHub mark takes the
# theme's text colour, the way GitHub itself uses it; the tanuki keeps its
# orange, which stays legible on both backgrounds.
ICONS = {
    "github": "M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 "
    "11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.7"
    "24-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 "
    "17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838"
    " 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-"
    "1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 "
    "1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 "
    "1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 "
    "2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 "
    "3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 "
    "5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 "
    ".315.21.69.825.57C20.565 22.092 24 17.592 24 "
    "12.297c0-6.627-5.373-12-12-12",
    "gitlab": "m23.6004 9.5927-.0337-.0862L20.3.9814a.851.851 0 0 "
    "0-.3362-.405.8748.8748 0 0 0-.9997.0539.8748.8748 0 0 "
    "0-.29.4399l-2.2055 6.748H7.5375l-2.2057-6.748a.8573.8573 0 0 "
    "0-.29-.4412.8748.8748 0 0 0-.9997-.0537.8585.8585 0 0 "
    "0-.3362.4049L.4332 9.5015l-.0325.0862a6.0657 6.0657 0 0 0 "
    "2.0119 7.0105l.0113.0087.03.0213 4.976 3.7264 2.462 1.8633 "
    "1.4995 1.1321a1.0085 1.0085 0 0 0 1.2197 0l1.4995-1.1321 "
    "2.4619-1.8633 5.006-3.7489.0125-.01a6.0682 6.0682 0 0 0 "
    "2.0094-7.003z",
}
ICON_VIEWBOX = 24
ICON = 14  # rendered icon edge, px
ICON_GAP = 6  # icon -> its own label
BADGE_GAP = 16  # between the two badges
BRAND_FILL = {"gitlab": "#fc6d26"}  # else the theme's title colour

# The GitHub badge is labelled with the username; GitLab is fixed.
GITLAB_LABEL = "Work"

# Approximate Arial/Helvetica advance widths (per 1000 units) - enough to
# right-align the header badges without measuring real glyphs.
_ADV = {
    " ": 278,
    "i": 222,
    "j": 222,
    "l": 222,
    "t": 278,
    "f": 278,
    "r": 333,
    "I": 278,
    "J": 500,
    "k": 500,
    "s": 500,
    "z": 500,
    "c": 500,
    "m": 833,
    "w": 722,
    "W": 944,
    "M": 833,
    "O": 778,
    "G": 778,
    "P": 667,
    "D": 722,
    "A": 667,
    "L": 556,
    "K": 667,
}


def text_width(s, size):
    """Width of `s` in px at `size`px, near enough for layout."""
    return sum(_ADV.get(ch, 556) for ch in s) * size / 1000.0


CELL = 11  # square edge, px
GAP = 3  # space between squares
PITCH = CELL + GAP
PANEL_PAD = 16  # breathing room inside the panel border
WEEKDAY_W = 30  # left gutter for the Mon/Wed/Fri labels
TITLE_H = 24  # the "N contributions" headline
MONTH_H = 16  # the month ruler
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial," "sans-serif"


def esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def svg_calendar(rows, labels, theme="light"):
    """Render the merged calendar as a standalone GitHub-style SVG panel.

    `labels` maps each source key to the name shown beside its icon, in the
    order the badges should appear.
    """
    grid_start, weeks = calendar_weeks(rows)
    peak = max((v for col in weeks for v in col if v), default=0)
    total = sum(v for col in weeks for v in col if v)
    t = THEMES[theme]

    grid_x = PANEL_PAD + WEEKDAY_W
    grid_y = PANEL_PAD + TITLE_H + MONTH_H
    grid_w = len(weeks) * PITCH - GAP
    grid_bottom = grid_y + 7 * PITCH - GAP
    height = grid_bottom + 8 + CELL + PANEL_PAD

    span = "the last year" if 360 <= len(rows) <= 372 else f"the last {len(rows)} days"
    headline = f"{total:,} contributions in {span}"

    # Header badges, right-aligned on the headline's baseline.
    badges = [s for s in labels if s in ICONS]
    badge_w = sum(ICON + ICON_GAP + text_width(labels[s], 12) for s in badges)
    if badges:
        badge_w += BADGE_GAP * (len(badges) - 1)

    # A short window must still leave the headline and badges room.
    header_w = PANEL_PAD + text_width(headline, 13) + 24 + badge_w + PANEL_PAD
    width = max(grid_x + grid_w + PANEL_PAD, int(header_w + 0.5))

    o = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{esc(headline)}">',
        # The panel: a real background and border, not bare squares.
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" '
        f'rx="6" ry="6" fill="{t["panel"]}" stroke="{t["border"]}"/>',
        f'<g font-family="{FONT}" font-size="12">',
        f'<text x="{PANEL_PAD}" y="{PANEL_PAD + 12}" font-size="13" '
        f'fill="{t["title"]}">{esc(headline)}</text>',
    ]

    bx = width - PANEL_PAD - badge_w
    for s in badges:
        label = labels[s]
        scale = ICON / ICON_VIEWBOX
        o.append(
            f'<g transform="translate({bx:.1f},{PANEL_PAD}) '
            f'scale({scale:.4f})" fill="'
            f'{BRAND_FILL.get(s, t["title"])}">'
            f'<path d="{ICONS[s]}"/></g>'
        )
        o.append(
            f'<text x="{bx + ICON + ICON_GAP:.1f}" '
            f'y="{PANEL_PAD + 12}" fill="{t["title"]}">'
            f"{esc(label)}</text>"
        )
        bx += ICON + ICON_GAP + text_width(label, 12) + BADGE_GAP

    o.append(f'<g fill="{t["muted"]}" font-size="10">')

    # Month ruler: label the first column of each new month.
    seen = None
    for w_idx in range(len(weeks)):
        wk = grid_start + dt.timedelta(days=7 * w_idx)
        # Attribute a column to the month of its last day, so a column
        # straddling a boundary is not labelled twice.
        month = (wk + dt.timedelta(days=6)).month
        if month != seen:
            if w_idx < len(weeks) - 2:  # no cramped label at the edge
                x = grid_x + w_idx * PITCH
                o.append(
                    f'<text x="{x}" y="{grid_y - 5}">' f"{MONTHS[month - 1]}</text>"
                )
            seen = month

    # Weekday labels (GitHub shows only Mon/Wed/Fri).
    for row_idx, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        y = grid_y + row_idx * PITCH + CELL - 2
        o.append(f'<text x="{PANEL_PAD}" y="{y}">{name}</text>')

    # Legend, bottom-right, aligned to the grid's right edge.
    legend_y = grid_bottom + 8
    text_y = legend_y + CELL - 2
    swatches_w = 5 * PITCH - GAP
    # Align to the panel edge, not the grid's: a short window can make the
    # header wider than the calendar itself.
    more_x = width - PANEL_PAD
    swatch_x = more_x - 30 - swatches_w
    o.append(f'<text x="{swatch_x - 6}" y="{text_y}" ' f'text-anchor="end">Less</text>')
    o.append(f'<text x="{more_x}" y="{text_y}" ' f'text-anchor="end">More</text>')
    o.append("</g>")

    for i, colour in enumerate(t["scale"]):
        x = swatch_x + i * PITCH
        o.append(
            f'<rect x="{x}" y="{legend_y}" width="{CELL}" '
            f'height="{CELL}" rx="2" ry="2" fill="{colour}" '
            f'stroke="{t["cell_stroke"]}"/>'
        )

    # Day squares.
    for w_idx, col in enumerate(weeks):
        x = grid_x + w_idx * PITCH
        for row_idx, count in enumerate(col):
            if count is None:
                continue
            y = grid_y + row_idx * PITCH
            date = grid_start + dt.timedelta(days=7 * w_idx + row_idx)
            fill = t["scale"][level(count, peak)]
            word = "contribution" if count == 1 else "contributions"
            o.append(
                f'<rect x="{x}" y="{y}" width="{CELL}" height="{CELL}" '
                f'rx="2" ry="2" fill="{fill}" '
                f'stroke="{t["cell_stroke"]}"><title>'
                + esc(f"{count} {word} on {date.isoformat()}")
                + "</title></rect>"
            )

    o.append("</g>")
    o.append("</svg>")
    return "\n".join(o) + "\n"


def dark_sibling(path):
    """contributions.svg -> contributions-dark.svg"""
    base, ext = os.path.splitext(path)
    return f"{base}-dark{ext or '.svg'}"


def write_readme(path, light_rel, dark_rel):
    """The README is the graph and nothing else, so it is simply replaced."""
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            "<picture>\n"
            f'  <source media="(prefers-color-scheme: dark)" '
            f'srcset="{dark_rel}">\n'
            f'  <img alt="Contribution graph" src="{light_rel}">\n'
            "</picture>\n"
        )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main():
    # Paths and upstream error text can carry characters a default
    # cp1252 Windows console would raise on.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass

    p = argparse.ArgumentParser(
        description="Merge the last year of GitHub + GitLab contributions."
    )
    p.add_argument("--github", metavar="USER", help="GitHub username")
    p.add_argument("--gitlab", metavar="USER", help="GitLab username")
    p.add_argument(
        "--gitlab-host",
        default="https://gitlab.com",
        help="Self-hosted GitLab base URL (default: gitlab.com)",
    )
    p.add_argument(
        "--days", type=int, default=365, help="Window size ending today (default: 365)"
    )
    p.add_argument("--json", metavar="FILE", help="Write merged data as JSON")
    p.add_argument("--csv", metavar="FILE", help="Write merged data as CSV")
    p.add_argument(
        "--svg",
        metavar="FILE",
        help="Write a GitHub-style SVG calendar (a dark variant "
        "goes next to it as FILE-dark.svg)",
    )
    p.add_argument(
        "--readme",
        metavar="FILE",
        help="Replace FILE with just the graph (implies --svg "
        "beside it if --svg is omitted)",
    )
    args = p.parse_args()

    if not args.github and not args.gitlab:
        p.error("give at least one of --github / --gitlab")

    end = dt.date.today()
    start = end - dt.timedelta(days=args.days - 1)

    sources, failures = {}, []

    if args.github:
        try:
            data, _ = fetch_github(args.github, start, end)
            sources["github"] = data
        except Exception as e:
            failures.append(f"GitHub ({args.github}): {e}")

    if args.gitlab:
        try:
            data, _ = fetch_gitlab(args.gitlab, args.gitlab_host)
            sources["gitlab"] = data
        except Exception as e:
            failures.append(f"GitLab ({args.gitlab}): {e}")

    for f in failures:
        print(f"! {f}", file=sys.stderr)

    if not sources:
        print("! nothing written: no contribution data", file=sys.stderr)
        sys.exit(1)

    rows = merge(sources, start, end)

    svg_path = args.svg
    if args.readme and not svg_path:
        svg_path = os.path.join(
            os.path.dirname(os.path.abspath(args.readme)), "contributions.svg"
        )

    try:
        if args.json:
            with open(args.json, "w") as fh:
                json.dump(
                    {
                        "window": {"start": start.isoformat(), "end": end.isoformat()},
                        "sources": {"github": args.github, "gitlab": args.gitlab},
                        "totals": {
                            "all": sum(r["total"] for r in rows),
                            **{k: sum(r[k] for r in rows) for k in sources},
                        },
                        "days": rows,
                    },
                    fh,
                    indent=2,
                )

        if args.csv:
            with open(args.csv, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)

        if svg_path:
            names = {"github": args.github, "gitlab": GITLAB_LABEL}
            labels = {s: names[s] for s in sources}
            for path, theme in ((svg_path, "light"), (dark_sibling(svg_path), "dark")):
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(svg_calendar(rows, labels, theme))

        if args.readme:
            base = os.path.dirname(os.path.abspath(args.readme))

            def rel(p):
                return os.path.relpath(p, base).replace(os.sep, "/")

            write_readme(args.readme, rel(svg_path), rel(dark_sibling(svg_path)))
    except OSError as e:
        print(f"! nothing written: {e}", file=sys.stderr)
        sys.exit(1)

    if args.readme:
        print(f"wrote {args.readme}")


if __name__ == "__main__":
    main()
