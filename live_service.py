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
    preserve_known_matchups,
)

JAPAN_TZ = ZoneInfo("Asia/Tokyo")
LIVE_NOW_PATH = "/s/AG2026/en/ALL/schedule/live-now"
FINAL_STATUSES = frozenset({"OFFICIAL", "FINISHED", "COMPLETED", "CANCELED", "CANCELLED"})
COMPLETION_CHECK_INTERVAL = 60
# Per-process request history, separate from the published schedule so an empty
# feed does not change its version. sync_live's clock also controls this limit.
_completion_checks: dict[str, dict[tuple[str, str], float]] = {}


def _started(row: dict, now: datetime) -> bool:
    try:
        value = row.get("scheduledAt") or f"{row['date']}T{row['time']}+08:00"
        start = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return start.replace(tzinfo=start.tzinfo or BEIJING_TZ) <= now
    except (KeyError, TypeError, ValueError):
        return False


def _completion_updates(
    output_path: Path, previous: list[dict], current: list[dict],
    targets: list[tuple[str, str]], now: datetime,
) -> list[dict]:
    """Recover final results when live-now stops returning a finished match.

    Read at most one due daily feed per cycle, and each sport/day at most once
    a minute. Missing rows are never deletions. The current live feed wins over
    daily data; newly published fixtures and schedule changes are retained too.
    """
    current_ids = {row["id"] for row in current}
    target_set = set(targets)
    candidates = {
        (row["sport"], row.get("sourceDate", row.get("date")))
        for row in previous
        if row.get("id") not in current_ids
        and str(row.get("status") or "").upper() not in FINAL_STATUSES
        and _started(row, now)
    } & target_set
    checks = _completion_checks.setdefault(str(output_path.resolve()), {})
    for target in set(checks) - target_set:
        del checks[target]
    timestamp = now.timestamp()
    due = [target for target in candidates
           if timestamp - checks.get(target, float("-inf")) >= COMPLETION_CHECK_INTERVAL]
    if not due:
        return []
    sport, day = min(due, key=lambda target: (checks.get(target, float("-inf")), target))
    # Count unsuccessful attempts too; a temporary empty/failed daily response
    # must not turn a five-second score poll into repeated daily-feed requests.
    checks[(sport, day)] = timestamp
    try:
        units = fetch_official_json(f"/s/AG2026/en/{sport}/schedule/daily/{day}", 1)
    except Exception:
        return []
    if not isinstance(units, list):
        return []
    known = {row["id"]: row for row in previous}
    updates = []
    for unit in units:
        if not isinstance(unit, dict) or not unit.get("Status"):
            continue
        if not (unit.get("Key") or unit.get("ResCode")):
            continue
        try:
            record = normalize_unit(unit, sport)
            scores = [(unit.get(side) or {}).get("Result") for side in ("Home", "Away")]
        except (AttributeError, TypeError, ValueError):
            continue
        if not record or record["id"] in current_ids:
            continue
        record["sourceDate"] = day
        old = known.get(record["id"])
        status = record["status"]
        if status in {"OFFICIAL", "FINISHED", "COMPLETED"} and any(
            score is None or not str(score).strip() for score in scores
        ):
            # An incomplete final row cannot replace a known score.
            continue
        if old:
            old_status = str(old.get("status") or "").upper()
            if old_status in FINAL_STATUSES and status not in FINAL_STATUSES:
                continue
            active_before = old.get("isLive") or old_status in {"LIVE", "RUNNING"}
            pending_after = not record["isLive"] and status not in FINAL_STATUSES | {"UNOFFICIAL"}
            if active_before and pending_after:
                for key in ("score", "status", "isLive"):
                    record[key] = old.get(key)
        updates.append(record)
    return updates


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
    previous_records = list(payload["records"])

    # The official site exposes one compressed, cross-sport feed for matches
    # that are currently live.  Polling this aggregate endpoint once per
    # five-second UI refresh avoids the old five-to-seven daily-feed requests
    # per cycle, which quickly exhausts the anonymous API allowance.  It is a
    # partial feed by design; due matches missing from it receive a separate,
    # low-frequency daily check to collect their final results.
    units = fetch_official_json(LIVE_NOW_PATH, 1)
    if not isinstance(units, list):
        raise SyncError("官网实时数据格式不正确")

    # A few test/integration callers historically supplied daily-feed-shaped
    # units without ``Disc``.  Keep that input compatible by falling back to
    # the old per-sport path only for an unidentifiable response.  Production
    # live-now responses always include Disc, so a valid response containing
    # only another sport (for example hockey) does not trigger a burst.
    aggregate_units = [unit for unit in units if isinstance(unit, dict)]
    aggregate_shape = all("Disc" in unit for unit in aggregate_units)
    if aggregate_shape:
        for unit in aggregate_units:
            sport = str(unit.get("Disc") or "").upper()
            if sport not in SPORTS:
                continue
            record = normalize_unit(unit, sport)
            if not record:
                continue
            raw_datetime = str(unit.get("DateTimeRaw") or "")
            try:
                source_date = datetime.fromisoformat(
                    raw_datetime.replace("Z", "+00:00")
                ).astimezone(JAPAN_TZ).date().isoformat()
            except ValueError:
                continue
            record["sourceDate"] = source_date
            replacements.append(record)
        if progress:
            progress(1, 1, "更新今日比分")
    else:
        # Compatibility fallback for a malformed/unversioned response.  This
        # path is also useful if the aggregate feed is temporarily rolled back
        # by the provider, while keeping the normal path to one request.
        errors = []
        with ThreadPoolExecutor(max_workers=min(2, len(targets))) as pool:
            futures = {
                pool.submit(fetch_official_json, f"/s/AG2026/en/{sport}/schedule/daily/{day}", 1): (sport, day)
                for sport, day in targets
            }
            for index, future in enumerate(as_completed(futures), 1):
                sport, day = futures[future]
                try:
                    daily_units = future.result()
                    if not isinstance(daily_units, list):
                        raise SyncError("逐场数据格式不正确")
                    if not daily_units and any(
                        row["sport"] == sport and row.get("sourceDate", row["date"]) == day
                        for row in previous_records
                    ):
                        raise SyncError("官网暂未返回已公布的比赛，保留上次数据")
                    for unit in daily_units:
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

    if aggregate_shape:
        replacements.extend(_completion_updates(output_path, previous_records, replacements, targets, now))
    replacements = preserve_known_matchups(previous_records, replacements)
    if aggregate_shape:
        if not replacements:
            # No current live unit (or only another sport's unit) means there
            # is no schedule/score delta to persist.  Keeping the file bytes
            # unchanged also avoids needless metadata churn every five seconds.
            return payload
        # live-now is partial, so retain every prior record not present in the
        # response.  A current live unit replaces its matching stable ID.
        unique = {row["id"]: row for row in previous_records + replacements}
    else:
        target_set = set(targets)
        records = [row for row in previous_records
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
