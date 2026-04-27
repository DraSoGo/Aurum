"""
News-blackout filter — blocks new entries around high-impact macro events.

Gold reacts violently to Fed / US CPI / NFP / ECB / BoE headlines. Entering
30 min before a release is a coin-flip. This gate fetches this week's
economic calendar once (cached 1h), then returns (blocked, reason) during
the blackout window surrounding each high-impact event.

Source: ForexFactory free JSON feed (no API key required)
  https://nfs.faireconomy.media/ff_calendar_thisweek.json

Env:
  NEWS_BLACKOUT_MIN        default 30  (minutes before+after event)
  NEWS_BLACKOUT_COUNTRIES  default "USD,EUR,GBP"  (FF uses currency codes)
  NEWS_BLACKOUT_IMPACTS    default "High"
  NEWS_FILTER_ENABLED      default "true"
  NEWS_FEED_URL            override default feed URL
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
from datetime import datetime, timezone
from typing import List, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_CACHE: dict = {
    "ts":              0.0,   # last successful fetch
    "events":          [],
    "last_err_ts":     0.0,   # last warning log (dedupe)
    "last_success_ts": 0.0,   # last time events were actually loaded
}
_TTL_SECS            = 3600   # refetch calendar every 1h
_ERR_LOG_INTERVAL    = 1800   # de-duplicate warning logs to every 30min
_STALE_FAIL_CLOSED_S = 6 * 3600  # feed stale >6h -> fail closed in NY window
_NY_WINDOW_UTC       = (12, 16)  # most US macro releases land here


def _truthy(v: str) -> bool:
    return v.strip().lower() not in ("0", "false", "no", "off", "")


def _parse_iso(dt_str: str) -> datetime:
    """ForexFactory uses ISO-8601 with offset, e.g. 2026-04-24T12:30:00-04:00.
    Returns tz-aware UTC datetime. Raises on bad input."""
    # Python 3.11+ handles -04:00 natively; strip trailing Z just in case.
    s = dt_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_events(raw) -> List[dict]:
    """Normalise FF payload -> [{'dt_utc', 'name', 'country', 'impact'}, ...].
    Silently drops rows that don't parse (holidays, malformed, etc.)."""
    out = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        try:
            dt = _parse_iso(r["date"])
            impact = str(r.get("impact", "")).strip()
            if impact.lower() == "holiday":
                continue
            out.append({
                "dt_utc":  dt,
                "name":    str(r.get("title", "")),
                "country": str(r.get("country", "")),
                "impact":  impact,
            })
        except Exception:
            continue
    return out


def _fetch_feed() -> List[dict]:
    """Return this week's events (cached 1h). Empty list on any failure,
    with de-duplicated warning logging."""
    now = time.time()
    if (now - _CACHE["ts"]) < _TTL_SECS and _CACHE["events"]:
        return _CACHE["events"]
    url = os.getenv("NEWS_FEED_URL", _DEFAULT_URL).strip() or _DEFAULT_URL
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ai-trader/1.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        events = _parse_events(raw)
        _CACHE["ts"]              = now
        _CACHE["events"]          = events
        _CACHE["last_success_ts"] = now
        logger.info("[news] fetched %d events from %s", len(events), url)
        return events
    except Exception as e:
        # De-dupe repeated warnings — feed failures recur every 60s otherwise.
        if (now - _CACHE["last_err_ts"]) > _ERR_LOG_INTERVAL:
            logger.warning("[news] feed fetch failed (%s): %s", url, e)
            _CACHE["last_err_ts"] = now
        return _CACHE["events"] or []


def is_news_blackout(
    now_utc: datetime | None = None,
) -> Tuple[bool, str]:
    """Return (blocked, reason). Blocked=True means skip this cycle's new
    entry attempt. Does NOT affect open-position management."""
    if not _truthy(os.getenv("NEWS_FILTER_ENABLED", "true")):
        return False, "news filter disabled"

    try:
        win_min = int(os.getenv("NEWS_BLACKOUT_MIN", "30"))
    except (TypeError, ValueError):
        win_min = 30
    countries = {
        c.strip().upper()
        for c in os.getenv("NEWS_BLACKOUT_COUNTRIES", "USD,EUR,GBP").split(",")
        if c.strip()
    }
    impacts = {
        i.strip().lower()
        for i in os.getenv("NEWS_BLACKOUT_IMPACTS", "High").split(",")
        if i.strip()
    }

    now = now_utc or datetime.now(timezone.utc)
    win_secs = win_min * 60

    # Pull the feed FIRST so a healthy fetch on a fresh process clears
    # last_success_ts before the stale-check below. Otherwise a process
    # started mid-NY-window with last_success_ts=0 would always tag the
    # feed as "infinitely stale" and fail-closed even when the network
    # is fine.
    events = _fetch_feed()

    # Fail-closed when the calendar is stale and we're inside the peak US
    # macro-release window. Previous behavior: empty cache -> no match ->
    # filter silently disabled, which is the worst outcome if FOMC/CPI
    # drops during a ForexFactory outage. Inside 12-16 UTC (Fed/BLS/BEA
    # typical release window), a >6h stale feed blocks new entries.
    try:
        ny_lo, ny_hi = _NY_WINDOW_UTC
        last_ok = float(_CACHE.get("last_success_ts") or 0.0)
        feed_age = time.time() - last_ok if last_ok > 0 else float("inf")
        in_ny = ny_lo <= now.hour < ny_hi
        if in_ny and feed_age > _STALE_FAIL_CLOSED_S:
            return True, (
                f"news feed stale ({feed_age/3600:.1f}h > 6h) during "
                f"NY macro window — fail-closed"
            )
    except Exception:
        pass

    for ev in events:
        if ev["impact"].lower() not in impacts:
            continue
        if countries and ev["country"].upper() not in countries:
            continue
        delta = (ev["dt_utc"] - now).total_seconds()
        if abs(delta) <= win_secs:
            mins = int(delta / 60)
            when = f"in {mins}m" if mins >= 0 else f"{abs(mins)}m ago"
            return True, (
                f"news blackout: {ev['country']} {ev['name']} "
                f"({ev['impact']}) {when}"
            )
    return False, "no high-impact event in window"
