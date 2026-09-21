from __future__ import annotations

import argparse
import json
import mimetypes
import os
import signal
import threading
import time
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

from sync_service import SPORTS, SyncError, sync_all
from live_service import live_targets, sync_live
from details_service import get_match_details, get_tournament


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_FILE = ROOT / "data" / "schedule.json"
STATUS_FILE = ROOT / "data" / "sync-status.json"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
LIVE_INTERVAL = 10
DETAIL_LIVE_TTL = 4
DETAIL_IDLE_TTL = 120
RETRY_INTERVAL = 300
# A 429 from the official service currently means the anonymous allowance has
# been exhausted, rather than a short transient failure. Avoid retrying every
# five minutes and extending the outage while that quota window recovers.
RATE_LIMIT_RETRY_INTERVAL = 3600
SPORT_PATHS = {
    "TEN": "tennis",
    "BBL": "baseball",
    "CKT": "cricket",
    "VVO": "volleyball",
    "TTE": "table-tennis",
    "BDM": "badminton",
    "HBL": "handball",
}
SPORT_ROUTES = {f"/{slug}" for slug in SPORT_PATHS.values()}


def iso_now() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def is_rate_limit_error(error: Exception) -> bool:
    message = str(error).lower()
    return "429" in message or "rate_limit" in message or "请求额度" in message


SCHEDULE_CHANGE_FIELDS = ("date", "time", "category", "stage", "matchup", "venue")


def schedule_changes(previous: dict | None, current: dict | None) -> list[dict]:
    """Summarize published schedule edits while ignoring live score changes."""
    before = {
        str(row.get("id")): row
        for row in (previous or {}).get("records", [])
        if isinstance(row, dict) and row.get("id")
    }
    after = {
        str(row.get("id")): row
        for row in (current or {}).get("records", [])
        if isinstance(row, dict) and row.get("id")
    }
    # An empty previous snapshot is the initial load, not a schedule change.
    if not before:
        return []
    changes: list[dict] = []
    for match_id in sorted(set(before) | set(after)):
        old = before.get(match_id)
        new = after.get(match_id)
        if old is None:
            changes.append({"type": "added", "id": match_id, "matchup": new.get("matchup", "")})
            continue
        if new is None:
            changes.append({"type": "removed", "id": match_id, "matchup": old.get("matchup", "")})
            continue
        fields = [field for field in SCHEDULE_CHANGE_FIELDS if old.get(field) != new.get(field)]
        if fields:
            changes.append({
                "type": "updated", "id": match_id, "matchup": new.get("matchup") or old.get("matchup", ""),
                "fields": fields,
            })
    return changes


def team_submatch_order(record: dict, details: dict) -> list[str] | None:
    """Return an unambiguous child order, without live scores or state.

    Child IDs remain stable when the venue rearranges a team tie. The
    displayed number can change, so sort by that number and compare IDs.
    Incomplete/unpublished and stale responses cannot establish a new order.
    """
    sport = str(record.get("sport") or "").upper()
    is_team = "团体" in str(record.get("category") or "") or "TEAM" in str(record.get("eventCode") or "").upper()
    if sport not in {"BDM", "TTE"} or not is_team or not isinstance(details, dict) or details.get("stale"):
        return None
    children = details.get("subMatches")
    if not isinstance(children, list) or len(children) < 2:
        return None
    ordered = []
    for child in children:
        if not isinstance(child, dict):
            return None
        child_id = child.get("id")
        # The adapter creates sub-N as a fallback when the source has no
        # stable key. Such an ID describes a slot, not a known child match.
        if not isinstance(child_id, str) or not child_id.startswith(f"{sport}:") or ":sub-" in child_id:
            return None
        number = child.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            return None
        ordered.append((number, child_id))
    if len({number for number, _ in ordered}) != len(ordered) or len({key for _, key in ordered}) != len(ordered):
        return None
    return [key for _, key in sorted(ordered)]


def match_detail_ttl(record: dict) -> int:
    """Choose a short cache window for live and team-tie details.

    A team tie can remain ``isLive=false`` while one of its child matches is
    already running.  Caching those parent responses for the normal idle
    window would hide the child score for two minutes, so team events use the
    live window as well.
    """
    category = str(record.get("category") or "")
    event_code = str(record.get("eventCode") or "").upper()
    status = str(record.get("status") or "").upper()
    live_status = status in {"LIVE", "RUNNING", "IN_PROGRESS"}
    sport = str(record.get("sport") or "").upper()
    # Only TTE/BDM team ties expose child matches.  Volleyball and handball
    # also use team-shaped event codes, but their details are single matches.
    is_team_tie = sport in {"TTE", "BDM"} and ("团体" in category or "TEAM" in event_code)
    return DETAIL_LIVE_TTL if record.get("isLive") or live_status or is_team_tie else DETAIL_IDLE_TTL


def next_eight(now: datetime | None = None) -> datetime:
    current = (now or datetime.now(BEIJING_TZ)).astimezone(BEIJING_TZ)
    target = current.replace(hour=8, minute=0, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target


class AppState:
    def __init__(self, data_file=DATA_FILE, status_file=STATUS_FILE, clock=None,
                 full_sync=sync_all, live_sync=sync_live) -> None:
        self.data_file = data_file
        self.status_file = status_file
        self.clock = clock or (lambda: datetime.now(BEIJING_TZ))
        self.full_sync = full_sync
        self.live_sync = live_sync
        self.lock = threading.Lock()
        self.running = False
        self.stop_event = threading.Event()
        self.team_submatch_orders: dict[str, list[str]] = {}
        self.status = {
            "running": False,
            "lastStarted": None,
            "lastSuccess": None,
            "lastError": None,
            "lastReason": None,
            "progressDone": 0,
            "progressTotal": 0,
            "progressLabel": "",
            "lastLiveSuccess": None,
            "lastLiveError": None,
            "retryAt": None,
            "liveRetryAt": None,
            "liveFailures": 0,
            "scheduleChanged": False,
            "scheduleChangeAt": None,
            "scheduleChangeCount": 0,
            "scheduleChanges": [],
            # Child-order notices are scoped to their team-tie row. Keep this
            # separate from ordinary schedule edits so the UI never promotes
            # a team sub-match reorder to the global banner.
            "teamScheduleChanges": {},
            "dataVersion": None,
        }
        self._load_status()
        self.next_live = self.clock()
        self._read_metadata()

    def _read_metadata(self) -> None:
        try:
            payload = json.loads(self.data_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {"meta": {}, "records": []}
        self.payload = payload
        self.status["dataVersion"] = payload.get("meta", {}).get("generatedAt")

    def _load_status(self) -> None:
        try:
            saved = json.loads(self.status_file.read_text(encoding="utf-8"))
            for key in ("lastStarted", "lastSuccess", "lastError", "lastReason",
                        "lastLiveSuccess", "lastLiveError", "retryAt", "liveRetryAt", "liveFailures",
                        "scheduleChanged", "scheduleChangeAt", "scheduleChangeCount", "scheduleChanges",
                        "teamScheduleChanges"):
                if key in saved and saved[key] is not None:
                    self.status[key] = saved[key]
            if not isinstance(self.status.get("teamScheduleChanges"), dict):
                self.status["teamScheduleChanges"] = {}
            orders = saved.get("teamSubmatchOrders")
            if isinstance(orders, dict):
                self.team_submatch_orders = {
                    key: order for key, order in orders.items()
                    if isinstance(key, str) and isinstance(order, list) and len(order) >= 2
                    and all(isinstance(child_id, str) for child_id in order)
                    and len(set(order)) == len(order)
                }
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return

    def _save_status(self) -> None:
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.status_file.with_suffix(".json.tmp")
        saved = {**self.status, "teamSubmatchOrders": self.team_submatch_orders}
        temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.status_file)

    def observe_match_details(self, record: dict, details: dict) -> None:
        """Persist known child orders and latch a notice after a real reorder.

        The first complete response establishes a baseline. Later missing
        children must not erase it; a larger published set can establish a
        richer baseline without claiming an order change. Only the same
        identified children in a different order trigger the notice.
        """
        order = team_submatch_order(record, details)
        match_id = record.get("id")
        if order is None or not match_id:
            return
        with self.lock:
            previous = self.team_submatch_orders.get(match_id)
            if previous == order:
                return
            if previous and set(previous) != set(order):
                if not set(previous).issubset(order):
                    return
                # More complete metadata is not evidence of a rearrangement.
                previous = None
            self.team_submatch_orders[match_id] = order
            if previous:
                changed_at = self.clock().isoformat(timespec="seconds")
                self.status["teamScheduleChanges"][match_id] = {
                    "changedAt": changed_at,
                    "matchup": record.get("matchup", ""),
                    "fields": ["subMatchOrder"],
                }
            self._save_status()

    @staticmethod
    def _parse_time(value):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed.astimezone(BEIJING_TZ) if parsed.tzinfo else None
        except (TypeError, ValueError):
            return None

    def rate_limit_retry_at(self, now: datetime | None = None) -> datetime | None:
        """Return the latest active provider cooldown, if one is recorded.

        A manual sync is otherwise allowed to bypass the scheduler's backoff
        and immediately replay the same requests that just received HTTP 429.
        Keep the check based on the persisted retry timestamps so it survives
        a local server restart.  The latest deadline is used when the full
        and live jobs have different cooldowns: a full refresh would touch
        both request paths, so it must wait for both to be safe.
        """
        current = (now or self.clock()).astimezone(BEIJING_TZ)
        with self.lock:
            retry_values = (
                self.status.get("retryAt"),
                self.status.get("liveRetryAt"),
            )
            candidates = []
            for value in retry_values:
                parsed = self._parse_time(value)
                if parsed and parsed > current:
                    candidates.append(parsed)
        return max(candidates, default=None)

    def _full_due(self, now):
        last = self._parse_time(self.status.get("lastSuccess"))
        boundary = next_eight(now) - timedelta(days=1)
        stale = not self.data_file.exists() or not last or last < boundary
        # One full refresh also migrates older local datasets to the richer schema.
        stale = stale or not self.payload.get("meta", {}).get("officialDays")
        retry = self._parse_time(self.status.get("retryAt"))
        if retry:
            return retry
        return now if stale else next_eight(now)

    def tick(self) -> str | None:
        """Recompute due work after every wake; never replay each missed day."""
        now = self.clock()
        with self.lock:
            if self.running:
                return None
            if now >= self._full_due(now):
                reason = "retry" if self.status.get("retryAt") else "scheduled"
            else:
                retry = self._parse_time(self.status.get("liveRetryAt"))
                due = max(self.next_live, retry) if retry else self.next_live
                if now < due or not live_targets(self.payload, now):
                    return None
                reason = "live"
        return reason if self.start_sync(reason) else None

    def snapshot(self) -> dict:
        with self.lock:
            snapshot = dict(self.status)
            snapshot["running"] = self.running
            now = self.clock()
            snapshot["nextAutomaticSync"] = self._full_due(now).isoformat(timespec="seconds")
            active = bool(live_targets(self.payload, now))
            retry = self._parse_time(self.status.get("liveRetryAt"))
            snapshot["nextLiveSync"] = max(self.next_live, retry or now).isoformat(timespec="seconds") if active else None
            snapshot["liveActive"] = active
        snapshot["liveEnabled"] = True
        snapshot["liveIntervalSeconds"] = LIVE_INTERVAL
        snapshot["timezone"] = "Asia/Shanghai"
        snapshot["sports"] = SPORTS
        return snapshot

    def start_sync(self, reason: str) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.status.update(
                {
                    "running": True,
                    "lastStarted": self.clock().isoformat(timespec="seconds"),
                    "lastReason": reason,
                    "progressDone": 0,
                    "progressTotal": 0,
                    "progressLabel": "准备连接官网",
                }
            )
            if reason != "live":
                self.status["lastError"] = None
            self._save_status()
        threading.Thread(target=self._run_sync, args=(reason,), name="official-sync", daemon=True).start()
        return True

    def _progress(self, done: int, total: int, label: str) -> None:
        with self.lock:
            self.status["progressDone"] = done
            self.status["progressTotal"] = total
            self.status["progressLabel"] = label

    def _run_sync(self, reason: str) -> None:
        live = reason == "live"
        try:
            with self.lock:
                previous_payload = self.payload
            if live:
                payload = self.live_sync(self.data_file, self.clock(), self._progress)
            else:
                payload = self.full_sync(self.data_file, self._progress)
            with self.lock:
                self.payload = payload
                self.status["dataVersion"] = payload["meta"]["generatedAt"]
                changes = schedule_changes(previous_payload, payload)
                if changes:
                    self.status["scheduleChanged"] = True
                    self.status["scheduleChangeAt"] = self.clock().isoformat(timespec="seconds")
                    self.status["scheduleChangeCount"] = len(changes)
                    self.status["scheduleChanges"] = changes[:20]
                self.status["lastLiveSuccess" if live else "lastSuccess"] = self.clock().isoformat(timespec="seconds")
                if not live:
                    self.status["lastError"] = None
                    self.status["retryAt"] = None
                self.status["lastLiveError"] = None
                self.status["liveRetryAt"] = None
                self.status["liveFailures"] = 0
                self.status["progressLabel"] = "同步完成"
        except Exception as exc:
            with self.lock:
                rate_limited = is_rate_limit_error(exc)
                if live:
                    failures = int(self.status.get("liveFailures") or 0) + 1
                    self.status["liveFailures"] = failures
                    delay = (
                        RATE_LIMIT_RETRY_INTERVAL
                        if rate_limited
                        else min(LIVE_INTERVAL * 2 ** min(failures - 1, 6), RETRY_INTERVAL)
                    )
                    self.status["liveRetryAt"] = (self.clock() + timedelta(seconds=delay)).isoformat()
                    self.status["lastLiveError"] = str(exc)
                else:
                    self.status["lastError"] = str(exc)
                    delay = RATE_LIMIT_RETRY_INTERVAL if rate_limited else RETRY_INTERVAL
                    self.status["retryAt"] = (self.clock() + timedelta(seconds=delay)).isoformat()
                self.status["progressLabel"] = "同步失败"
        finally:
            with self.lock:
                self.next_live = self.clock() + timedelta(seconds=LIVE_INTERVAL)
                self.running = False
                self.status["running"] = False
                self._save_status()


STATE = AppState()


class OfficialCache:
    """Coalesce concurrent readers without blocking unrelated matches."""
    def __init__(self):
        self.lock = threading.Lock()
        self.locks = {}
        self.values = {}

    def get(self, key, ttl, loader):
        with self.lock:
            key_lock = self.locks.setdefault(key, threading.Lock())
        with key_lock:
            previous = self.values.get(key)
            if previous and time.monotonic() - previous[0] < ttl:
                return previous[1]
            try:
                value = loader()
            except Exception:
                if previous:
                    return {**previous[1], "stale": True, "message": "官网暂时连接失败，显示上次成功数据"}
                raise
            self.values[key] = (time.monotonic(), value)
            return value


OFFICIAL_CACHE = OfficialCache()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "AichiSchedule/1.0"

    def log_message(self, format: str, *args) -> None:
        print(f"[{iso_now()}] {self.address_string()} {format % args}", flush=True)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, value: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_rate_limit(self, retry_at: datetime) -> None:
        """Tell callers about the persisted cooldown without touching the API."""
        seconds = max(1, int((retry_at - datetime.now(BEIJING_TZ)).total_seconds() + 0.999))
        minutes, remainder = divmod(seconds, 60)
        if minutes:
            wait_text = f"{minutes}分钟{remainder}秒" if remainder else f"{minutes}分钟"
        else:
            wait_text = f"{seconds}秒"
        self._send_json(
            {
                "accepted": False,
                "message": f"官网暂时限流，请等待{wait_text}后自动重试",
                "retryAt": retry_at.isoformat(timespec="seconds"),
                "retryAfterSeconds": seconds,
            },
            HTTPStatus.TOO_MANY_REQUESTS,
        )

    def _send_static(self, relative_path: str) -> None:
        requested = (STATIC_DIR / relative_path).resolve()
        if STATIC_DIR.resolve() not in requested.parents and requested != STATIC_DIR.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not requested.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = requested.read_bytes()
        mime_type, _ = mimetypes.guess_type(requested.name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", f"{mime_type or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"/api/match", "/api/tournament"}:
            retry_at = STATE.rate_limit_retry_at()
            if retry_at:
                self._send_rate_limit(retry_at)
                return
            query = parse_qs(parsed.query)
            with STATE.lock:
                records = list(STATE.payload.get("records", []))
            try:
                if path == "/api/match":
                    match_id = query.get("id", [""])[0]
                    record = next((row for row in records if row["id"] == match_id), None)
                    if not record:
                        self._send_json({"message": "找不到这场比赛"}, HTTPStatus.NOT_FOUND)
                        return
                    ttl = match_detail_ttl(record)

                    def load_details():
                        details = get_match_details(record)
                        # Observe fresh responses inside the cache's per-match
                        # lock so simultaneous readers cannot replay an older
                        # response after a newer order has been recorded.
                        STATE.observe_match_details(record, details)
                        return details

                    value = OFFICIAL_CACHE.get(("match", match_id), ttl, load_details)
                else:
                    sport = query.get("sport", [""])[0]
                    if sport not in SPORTS:
                        self._send_json({"message": "未知项目"}, HTTPStatus.BAD_REQUEST)
                        return
                    value = OFFICIAL_CACHE.get(("tournament", sport), 55, lambda: get_tournament(sport, records))
                self._send_json(value)
            except Exception:
                self._send_json({"message": "暂时无法读取官网详情，请稍后重试"}, HTTPStatus.BAD_GATEWAY)
            return
        if path == "/api/schedule":
            try:
                payload = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                payload = {
                    "meta": {"generatedAt": None, "timezone": "UTC+8", "total": 0, "sports": SPORTS},
                    "records": [],
                }
            self._send_json(payload)
            return
        if path == "/api/status":
            self._send_json(STATE.snapshot())
            return
        if path == "/api/health":
            self._send_json({"ok": True, "time": iso_now()})
            return
        # The client owns the selected sport in the URL (for example
        # ``/tennis``). Serve the SPA shell for every sport route, including
        # a trailing slash, so a direct visit or browser refresh keeps that
        # selection instead of returning a static-file 404.
        if (path.rstrip("/") or "/") in SPORT_ROUTES or path == "/":
            self._send_static("index.html")
            return
        self._send_static(path.lstrip("/"))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/sync":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        retry_at = STATE.rate_limit_retry_at()
        if retry_at:
            self._send_rate_limit(retry_at)
            return
        started = STATE.start_sync("manual")
        if not started:
            self._send_json({"accepted": False, "message": "同步正在进行中"}, HTTPStatus.CONFLICT)
            return
        self._send_json({"accepted": True, "message": "已开始同步"}, HTTPStatus.ACCEPTED)


def scheduler_loop() -> None:
    while not STATE.stop_event.is_set():
        STATE.tick()
        # A short local timer also notices wake, midnight, and recovered network.
        if STATE.stop_event.wait(1):
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="爱知·名古屋2026赛程与赛果本地网站")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "4173")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), RequestHandler)
    scheduler = threading.Thread(target=scheduler_loop, name="daily-scheduler", daemon=True)
    scheduler.start()

    def stop_server(_signum, _frame) -> None:
        STATE.stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    print(f"Local schedule site: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        STATE.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
