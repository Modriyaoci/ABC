"""Refresh only current competition days, preserving the complete schedule."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import json

from sync_service import (
    BEIJING_TZ, SPORTS, SyncError, _atomic_json_write,
    fetch_official_json, normalize_unit,
)

JAPAN_TZ = ZoneInfo("Asia/Tokyo")


def live_targets(payload: dict, now: datetime) -> list[tuple[str, str]]:
    """The official daily endpoint groups dates in Japan, not Beijing."""
    japan_today = now.astimezone(JAPAN_TZ).date()
    today = japan_today.isoformat()
    targets = {
        (sport, today)
        for sport, days in payload.get("meta", {}).get("officialDays", {}).items()
        if sport in SPORTS and today in days
    }
    for row in payload.get("records", []):
        if row.get("sport") not in SPORTS:
            continue
        source_date = row.get("sourceDate")
        if not source_date:
            try:
                source_date = datetime.fromisoformat(
                    f"{row['date']}T{row['time']}+08:00"
                ).astimezone(JAPAN_TZ).date().isoformat()
            except (KeyError, TypeError, ValueError):
                continue
        # Keep a match crossing midnight until its official result arrives.
        if source_date == today or (
            row.get("isLive")
            and source_date == (japan_today - timedelta(days=1)).isoformat()
        ):
            targets.add((row["sport"], source_date))
    return sorted(targets)


def sync_live(output_path: Path, now: datetime | None = None, progress=None) -> dict:
    now = now or datetime.now(BEIJING_TZ)
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    targets = live_targets(payload, now)
    if not targets:
        return payload
    replacements = []
    errors = []
    # Refresh every sport in parallel.  With seven enabled sports, four
    # workers could make the latter requests miss the five-second cadence when
    # the official endpoint was slow.
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        futures = {
            pool.submit(fetch_official_json, f"/s/AG2026/en/{sport}/schedule/daily/{day}", 1): (sport, day)
            for sport, day in targets
        }
        for index, future in enumerate(as_completed(futures), 1):
            sport, day = futures[future]
            try:
                units = future.result()
                if not isinstance(units, list):
                    raise SyncError("逐场数据格式不正确")
                # An unexplained empty response must not erase known matches.
                if not units and any(
                    row["sport"] == sport and row.get("sourceDate", row["date"]) == day
                    for row in payload["records"]
                ):
                    raise SyncError("官网暂未返回已公布的比赛，保留上次数据")
                for unit in units:
                    if isinstance(unit, dict):
                        record = normalize_unit(unit, sport)
                        if record:
                            record["sourceDate"] = day
                            replacements.append(record)
            except Exception as exc:
                errors.append(f"{SPORTS[sport]}：{exc}")
            if progress:
                progress(index, len(targets), "更新今日比分")
    if errors:
        raise SyncError("；".join(errors))

    target_set = set(targets)
    records = [row for row in payload["records"]
               if (row["sport"], row.get("sourceDate", row["date"])) not in target_set]
    unique = {row["id"]: row for row in records + replacements}
    payload["records"] = sorted(unique.values(), key=lambda row: (
        row["date"], row["time"], list(SPORTS).index(row["sport"]), row["id"]
    ))
    payload["meta"].update({
        "generatedAt": datetime.now(BEIJING_TZ).isoformat(timespec="microseconds"),
        "total": len(unique),
        "counts": {sport: sum(row["sport"] == sport for row in unique.values()) for sport in SPORTS},
    })
    _atomic_json_write(output_path, payload)
    return payload
