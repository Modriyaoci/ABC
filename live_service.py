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
    apply_verified_tennis_snapshots, apply_court_sequencing,
    filter_unverified_tennis_rows, filter_unlocated_current_tennis_rows,
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
        # Court start times can change before play begins. live-now cannot
        # report those changes, so include upcoming court sessions in the
        # existing throttled daily check (no extra polling loop).
        and (_started(row, now) or row.get("sport") in {"TEN", "BDM"})
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
            record = normalize_unit(unit, sport, day)
            scores = [(unit.get(side) or {}).get("Result") for side in ("Home", "Away")]
        except (AttributeError, TypeError, ValueError):
            continue
        # A finished daily result may replace a unit still present in
        # live-now as Scheduled. Non-final daily rows must never overwrite the
        # fresher live score.
        if not record or (record["id"] in current_ids and str(record.get("status") or "").upper() not in FINAL_STATUSES | {"UNOFFICIAL"}):
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
            active_before = old.get("isLive") or old_status in {"LIVE", "RUNNING", "IN_PROGRESS", "SUSPENDED", "INTERRUPTED"}
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
            # The live feed supplies scores/status; the daily page fetched
            # below supplies the latest tennis date, court and order. Do not
            # freeze those fields from the previous cache: weather relocations
            # and interrupted matches can move between days or courts.
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
        # The tennis venue page can move the first start time repeatedly
        # before play (09:00 -> 10:00 -> 11:00 -> 12:00). Check the current
        # Tokyo-day schedule on every live cycle so the local court sequence
        # follows the latest official baseline.
        tennis_day = next((day for sport, day in targets if sport == "TEN"), None)
        if tennis_day:
            daily = fetch_official_json(f"/s/AG2026/en/TEN/schedule/daily/{tennis_day}", 1)
            if isinstance(daily, list):
                for official_order, unit in enumerate(daily):
                    if not isinstance(unit, dict):
                        continue
                    record = normalize_unit(unit, "TEN", tennis_day)
                    if record:
                        record["sourceDate"] = tennis_day
                        record["officialCourtOrder"] = official_order
                        replacements.append(record)
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
                    for official_order, unit in enumerate(daily_units):
                        if isinstance(unit, dict):
                            record = normalize_unit(unit, sport)
                            if record:
                                record["sourceDate"] = day
                                if sport == "TEN":
                                    record["officialCourtOrder"] = official_order
                                replacements.append(record)
                except Exception as exc:
                    errors.append(f"{SPORTS[sport]}：{exc}")
                if progress:
                    progress(index, len(targets), "更新今日比分")
        if errors:
            raise SyncError("；".join(errors))

    if aggregate_shape:
        replacements.extend(_completion_updates(output_path, previous_records, replacements, targets, now))
    # The aggregate live feed and the daily tennis reconciliation can contain
    # the same unit in one cycle.  The daily row is needed for its current
    # court/order/time, but it commonly still carries SCHEDULED (and an empty
    # score) while the live row already has the real set score.  If we simply
    # de-duplicate by taking the last row, the daily snapshot rolls an active
    # match back to “待赛/比赛中断”.  Merge schedule fields from the newest
    # daily row while retaining the freshest live state and score.
    merged_replacements: dict[str, dict] = {}
    active_statuses = {"LIVE", "RUNNING", "IN_PROGRESS", "SUSPENDED", "INTERRUPTED"}
    for incoming in replacements:
        key = str(incoming.get("id") or "")
        if not key:
            continue
        previous_row = merged_replacements.get(key)
        if previous_row is None:
            merged_replacements[key] = incoming
            continue
        old_active = bool(previous_row.get("isLive")) or str(previous_row.get("status") or "").upper() in active_statuses
        new_active = bool(incoming.get("isLive")) or str(incoming.get("status") or "").upper() in active_statuses
        # The incoming row is authoritative for schedule metadata.  State
        # fields are then overlaid from whichever duplicate is live.
        merged = dict(previous_row)
        merged.update(incoming)
        incoming_status = str(incoming.get("status") or "").upper()
        incoming_score = str(incoming.get("score") or "").strip()
        incoming_final = incoming_status in FINAL_STATUSES and (
            incoming_status in {"CANCELED", "CANCELLED"}
            or incoming_score not in {"", "待赛", "—", "-"}
        )
        # Daily snapshots can also say INTERRUPTED, but they do not carry the
        # current set score.  Keep the aggregate live state whenever it is
        # active; only a daily final row with a concrete result may replace it.
        if old_active and not incoming_final:
            for field in ("score", "status", "isLive", "home", "away", "matchup", "actualEndAt"):
                if field in previous_row:
                    merged[field] = previous_row[field]
        elif new_active and not old_active:
            for field in ("score", "status", "isLive", "home", "away", "matchup", "actualEndAt"):
                if field in incoming:
                    merged[field] = incoming[field]
        merged_replacements[key] = merged
    replacements = preserve_known_matchups(previous_records, list(merged_replacements.values()))
    if aggregate_shape:
        if not replacements:
            # No current live unit (or only another sport's unit) means there
            # is no schedule/score delta to persist.  Keeping the file bytes
            # unchanged also avoids needless metadata churn every five seconds.
            return payload
        # live-now is partial, so retain every prior record not present in the
        # response.  A current live unit replaces its matching stable ID.
        unique = {
            (row["id"], str(row.get("date") or row.get("sourceDate") or "")): row
            for row in previous_records + replacements
        }
    else:
        target_set = set(targets)
        records = [row for row in previous_records
                   if (row["sport"], row.get("sourceDate", row["date"])) not in target_set]
        unique = {
            (row["id"], str(row.get("date") or row.get("sourceDate") or "")): row
            for row in records + replacements
        }
    records = list(unique.values())
    # Every incremental score/status cycle also reconciles the latest
    # published start times and court order. This keeps Followed-by and delay
    # propagation aligned while a match is still running.
    # Reapply the page snapshot after merging live data. For tennis it is the
    # sole authority for Starting-at/Not-Before/Followed-by and court order;
    # live data remains authoritative for score and status.
    # The rendered official page is the authoritative source for current
    # tennis court slots.  Add newly published rows as well as updating rows
    # already present in the cached aggregate; otherwise a provisional API
    # cache can permanently hide newly published matches.
    records = apply_verified_tennis_snapshots(records, add_missing=True)
    # A live cycle must apply the same official-day allow-list as a full sync;
    # otherwise rows removed from today's page can reappear from yesterday's
    # cached schedule after court-time propagation.
    records = filter_unverified_tennis_rows(records)
    records = filter_unlocated_current_tennis_rows(records)
    records = apply_court_sequencing(records, previous_records)
    payload["records"] = sorted({
        (row["id"], str(row.get("date") or row.get("sourceDate") or "")): row
        for row in records
    }.values(), key=lambda row: (
        row.get("date", ""), row.get("time", ""), list(SPORTS).index(row["sport"]), row["id"]
    ))
    payload["meta"].update({
        "generatedAt": datetime.now(BEIJING_TZ).isoformat(timespec="microseconds"),
        "total": len(payload["records"]),
        "counts": {sport: sum(row["sport"] == sport for row in payload["records"]) for sport in SPORTS},
    })
    _atomic_json_write(output_path, payload)
    return payload
