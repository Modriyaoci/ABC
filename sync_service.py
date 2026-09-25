from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


API_BASE = "https://back.results.asiangames2026.org"
OFFICIAL_RESULTS_URL = "https://results.asiangames2026.org/#/schedule/daily/"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
JAPAN_TZ = ZoneInfo("Asia/Tokyo")
SYSTEM_CA_FILE = Path("/etc/ssl/cert.pem")
SSL_CONTEXT = ssl.create_default_context(cafile=str(SYSTEM_CA_FILE) if SYSTEM_CA_FILE.exists() else None)
# The development machine may expose a shared HTTP(S) proxy through the
# environment.  That proxy is also used by other jobs and has already
# exhausted the official service's anonymous allowance.  Prefer a direct
# connection for this feed; set OFFICIAL_USE_SYSTEM_PROXY=1 only when a
# network requires the configured proxy.
DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=SSL_CONTEXT),
)

SPORTS = {
    "TEN": "网球",
    "BBL": "棒球",
    "CKT": "板球",
    "VVO": "排球",
    "TTE": "乒乓球",
    "BDM": "羽毛球",
    "HBL": "手球",
}

ORG_NAMES = {
    "AFG": "阿富汗",
    "BAN": "孟加拉国",
    "BHU": "不丹",
    "BRN": "巴林",
    "BRU": "文莱",
    "CAM": "柬埔寨",
    "CHN": "中国",
    "HKG": "中国香港",
    "INA": "印度尼西亚",
    "IND": "印度",
    "IRI": "伊朗",
    "IRQ": "伊拉克",
    "JOR": "约旦",
    "JPN": "日本",
    "KAZ": "哈萨克斯坦",
    "KGZ": "吉尔吉斯斯坦",
    "KOR": "韩国",
    "KSA": "沙特阿拉伯",
    "KUW": "科威特",
    "LAO": "老挝",
    "LBN": "黎巴嫩",
    "MAC": "中国澳门",
    "MAS": "马来西亚",
    "MDV": "马尔代夫",
    "MGL": "蒙古",
    "MYA": "缅甸",
    "NEP": "尼泊尔",
    "OMA": "阿曼",
    "PAK": "巴基斯坦",
    "PHI": "菲律宾",
    "PLE": "巴勒斯坦",
    "PRK": "朝鲜",
    "QAT": "卡塔尔",
    "SGP": "新加坡",
    "SRI": "斯里兰卡",
    "SYR": "叙利亚",
    "THA": "泰国",
    "TJK": "塔吉克斯坦",
    "TKM": "土库曼斯坦",
    "TLS": "东帝汶",
    "TPE": "中华台北",
    "UAE": "阿联酋",
    "UZB": "乌兹别克斯坦",
    "VIE": "越南",
    "YEM": "也门",
}

VENUE_NAMES = {
    "Nagoya City Higashiyama Park Tennis Center": "名古屋市东山公园网球中心",
    "Okazaki Chuo Sogo Park Baseball Stadium": "冈崎中央综合公园棒球场",
    "Toyohashi Municipal Baseball Stadium": "丰桥市民棒球场",
    "Korogi Athletic Park": "Korogi Athletic Park",
    "Okazaki Chuo Sogo Gymnasium": "冈崎中央综合公园体育馆",
    "Okazaki Chuo Sogo Park Gymnasium": "冈崎中央综合公园体育馆",
    "Park Arena Komaki": "小牧公园竞技场",
    "SKY HALL TOYOTA": "SKY HALL TOYOTA",
    "Ichinomiya City Municipal Gymnasium": "一宫市综合体育馆",
    "Kasugai City Gymnasium": "春日井市综合体育馆",
    "ENTRIO": "ENTRIO",
}

STATUS_NAMES = {
    "SCHEDULED": "待赛",
    "RUNNING": "进行中",
    "LIVE": "进行中",
    "OFFICIAL": "已结束",
    "FINISHED": "已结束",
    "CANCELED": "已取消",
    "CANCELLED": "已取消",
    "DELAYED": "延期",
    "POSTPONED": "延期",
    "RESCHEDULED": "已改期",
    "INTERRUPTED": "比赛中断",
}


class SyncError(RuntimeError):
    pass


def _open_official(request: urllib.request.Request, timeout: float):
    if os.environ.get("OFFICIAL_USE_SYSTEM_PROXY", "").lower() in {"1", "true", "yes"}:
        return urllib.request.urlopen(request, timeout=timeout, context=SSL_CONTEXT)
    return DIRECT_OPENER.open(request, timeout=timeout)


# The results service applies a fairly strict per-client rate limit.  A full
# refresh can otherwise create a burst of requests (one for each sport and
# then one for every competition day), while the live poller creates another
# burst every five seconds.  Keep requests spaced out process-wide so the
# callers can still use small worker pools without overwhelming the official
# API.  The value is configurable for local diagnostics, but the conservative
# default is appropriate for the free Render instance.
REQUEST_INTERVAL_SECONDS = max(
    0.1, float(os.environ.get("OFFICIAL_REQUEST_INTERVAL", "0.75"))
)
RATE_LIMIT_BACKOFF_SECONDS = max(
    2.0, float(os.environ.get("OFFICIAL_RATE_LIMIT_BACKOFF", "8"))
)
_request_lock = threading.Lock()
_next_request_at = 0.0
_rate_limit_until = 0.0


def _wait_for_request() -> None:
    """Throttle all official requests, including requests from worker threads."""
    global _next_request_at
    with _request_lock:
        now = time.monotonic()
        target = max(now, _next_request_at, _rate_limit_until)
        _next_request_at = target + REQUEST_INTERVAL_SECONDS
    delay = target - now
    if delay > 0:
        time.sleep(delay)


def _set_rate_limit_cooldown(seconds: float) -> None:
    global _rate_limit_until
    if seconds <= 0:
        return
    with _request_lock:
        _rate_limit_until = max(_rate_limit_until, time.monotonic() + seconds)


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """Read a standards-compliant Retry-After value, if the server sent one."""
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        # HTTP-date Retry-After is uncommon for this API.  Treat an unknown
        # value as absent and use our exponential fallback instead.
        return None


def _decode_response(body: bytes) -> Any:
    stripped = body.lstrip()
    if stripped.startswith((b"[", b"{")):
        return json.loads(stripped.decode("utf-8"))

    try:
        binary_text = body.decode("utf-8").encode("latin-1")
        return json.loads(zlib.decompress(binary_text).decode("utf-8"))
    except (UnicodeDecodeError, UnicodeEncodeError, zlib.error, json.JSONDecodeError):
        pass

    try:
        return json.loads(zlib.decompress(body).decode("utf-8"))
    except (zlib.error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        preview = body[:120].decode("utf-8", errors="replace")
        raise SyncError(f"官网响应无法解析：{preview}") from exc


def fetch_official_json(path: str, retries: int = 3) -> Any:
    # Do not append a unique cache-busting query to every request.  That
    # bypasses the official CDN and turns the five-second live poll into a
    # stream of origin requests, which is what triggers HTTP 429.  Explicit
    # no-cache headers still let a cache revalidate a response when needed.
    url = f"{API_BASE}{path}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Cache-Control": "no-cache",
        "Origin": "https://results.asiangames2026.org",
        "Pragma": "no-cache",
        "Referer": "https://results.asiangames2026.org/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36 "
        "AichiNagoyaLocalSchedule/1.0",
    }

    total_retries = max(1, retries)
    last_error: Exception | None = None
    for attempt in range(total_retries):
        _wait_for_request()
        try:
            request = urllib.request.Request(url, headers=headers)
            with _open_official(request, timeout=25) as response:
                if response.status != 200:
                    raise SyncError(f"官网返回 HTTP {response.status}")
                return _decode_response(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # The provider currently returns a JSON ``rate_limit_exceeded``
                # body without Retry-After. Keep the user-facing error
                # actionable instead of exposing Python's generic HTTP text.
                last_error = SyncError("官网匿名请求额度已用尽（HTTP 429），请稍后重试")
                # Honour the server's requested cooldown where possible.  In
                # its absence, back off exponentially (8, 16, 32 seconds by
                # default), and share the cooldown with all worker threads.
                retry_after = _retry_after_seconds(exc)
                delay = max(
                    retry_after or 0.0,
                    RATE_LIMIT_BACKOFF_SECONDS * (2 ** attempt),
                )
                _set_rate_limit_cooldown(delay)
                # This provider's anonymous quota response has no
                # Retry-After header and will not recover within this call.
                # Stop immediately so a full refresh does not spend minutes
                # repeating doomed requests. A server-provided Retry-After,
                # on the other hand, is safe to honour once.
                if retry_after is None:
                    break
            else:
                last_error = exc
                delay = 1.5 * (attempt + 1) if attempt + 1 < total_retries else 0.0
            if attempt + 1 < total_retries and delay:
                time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, SyncError) as exc:
            last_error = exc
            if attempt + 1 < total_retries:
                time.sleep(1.5 * (attempt + 1))

    raise SyncError(f"无法连接官网：{last_error}")


def _translate_category(event_desc: str) -> str:
    text = (event_desc or "").strip()
    lowered = text.lower()
    rules = [
        ("mixed doubles", "混合双打"),
        ("women's doubles", "女子双打"),
        ("womens doubles", "女子双打"),
        ("men's doubles", "男子双打"),
        ("mens doubles", "男子双打"),
        ("women's singles", "女子单打"),
        ("womens singles", "女子单打"),
        ("men's singles", "男子单打"),
        ("mens singles", "男子单打"),
        ("women's team", "女子团体"),
        ("womens team", "女子团体"),
        ("men's team", "男子团体"),
        ("mens team", "男子团体"),
        ("mixed team", "混合团体"),
    ]
    for token, translated in rules:
        if token in lowered:
            return translated
    if "women" in lowered:
        return "女子"
    if "men" in lowered:
        return "男子"
    if "mixed" in lowered:
        return "混合"
    return text or "公开组"


def _translate_stage(text: str) -> str:
    value = (text or "").strip()
    if not value:
        return "待定"

    replacements = [
        (r"Women['’]?s\s+", ""),
        (r"Men['’]?s\s+", ""),
        (r"^(?:Women|Men)\s+", ""),
        (r"^(?:Mixed Doubles|Doubles|Singles|Team)\s+", ""),
        (r"Gold Medal (?:Team )?(Match|Game)", "金牌赛"),
        (r"Bronze Medal (?:Team )?(Match|Game)", "铜牌赛"),
        (r"Classification\s+(?:Match\s+)?(\d+)(st|nd|rd|th)\s*[-–]\s*(\d+)(st|nd|rd|th)", r"第\1至\3名排位赛"),
        (r"(\d+)(st|nd|rd|th) Place Match", r"第\1名赛"),
        (r"Victory Ceremony", "颁奖仪式"),
        (r"Mixed Doubles", "混合双打"),
        (r"Doubles", "双打"),
        (r"Singles", "单打"),
        (r"Team", "团体"),
        (r"Round of\s*(\d+)", r"\1强赛"),
        (r"Quarter[- ]?finals?", "1/4决赛"),
        (r"Semi[- ]?finals?", "半决赛"),
        (r"Preliminary Round", "预赛"),
        (r"Opening Round", "小组赛"),
        (r"Group Stage", "小组赛"),
        (r"First Stage", "第一阶段"),
        (r"Super Round", "超级循环赛"),
        (r"Placement Round", "排位赛"),
        (r"Classification Round", "排位赛"),
        (r"Finals?", "决赛"),
        (r"Round Match\s*(\d+)", r"循环赛第\1场"),
        (r"Match\s*(\d+)", r"第\1场"),
        (r"1st Round", "第一轮"),
        (r"2nd Round", "第二轮"),
        (r"3rd Round", "第三轮"),
        (r"First Round", "第一轮"),
        (r"Second Round", "第二轮"),
        (r"Third Round", "第三轮"),
        (r"Group\s*([A-Z])", r"\1组"),
        (r"Pool\s*([A-Z])", r"\1组"),
        (r"Game\s*(\d+)", r"第\1场"),
        (r"Round", "循环赛"),
    ]
    for pattern, translated in replacements:
        value = re.sub(pattern, translated, value, flags=re.IGNORECASE)
    value = re.sub(r"\s*-\s*", " ", value)
    value = re.sub(r"(1/4决赛|半决赛|决赛)\s+(\d+)$", r"\1第\2场", value)
    value = re.sub(r"决赛\s*(铜牌赛|金牌赛)", r"\1", value)
    value = re.sub(r"(铜牌赛|金牌赛)\s+第(\d+)场", r"\1第\2场", value)
    value = re.sub(r"\s+", " ", value).strip(" -")
    return value or "待定"


def _stage_labels(item: dict[str, Any], disc: str) -> tuple[str, str]:
    """Use official phase identity without treating every sport's SFNL as a semifinal.

    The current TEN feed calls R32 "Second Round" even when it is the first
    published doubles round. R32 / 8FNL describe draw size unambiguously.
    BBL's SFNL is a Super Round and women's HBL FNL is a round robin, so
    those official descriptions take precedence over generic code mappings.
    """
    phase_source = str(item.get("PhaseDesc") or item.get("PhaseDescA") or "")
    unit_source = str(item.get("UnitDesc") or item.get("UnitDescA") or phase_source)
    phase = _translate_stage(phase_source)
    unit = _translate_stage(unit_source)
    phase_code = str(item.get("Phase") or "").rsplit(".", 1)[-1].upper()
    code_labels = {
        "R64-": "64强赛", "R32-": "32强赛", "8FNL": "16强赛",
        "QFNL": "1/4决赛", "SFNL": "半决赛", "FNL-": "决赛",
    }
    special_round = (
        disc == "BBL" and "super round" in phase_source.lower()
    ) or (disc == "HBL" and phase_source.strip().lower() == "round")
    if phase_code in code_labels and not special_round:
        phase = code_labels[phase_code]

    # The unit distinguishes bronze/gold and individual placing matches within
    # the same official final/classification phase.
    if re.search(r"(?:金牌赛|铜牌赛|第\d+名赛)", unit):
        return phase, unit
    match_number = re.search(r"(?:Match|Game)\s+(\d+)\s*$", unit_source, re.IGNORECASE)
    if not match_number:
        match_number = re.search(r"(?:Quarter[- ]?final|Semi[- ]?final)\s+(\d+)\s*$", unit_source, re.IGNORECASE)
    if phase != "待定":
        if match_number:
            if phase == "循环赛":
                return phase, f"循环赛第{match_number.group(1)}场"
            return phase, f"{phase} · 第{match_number.group(1)}场"
        return phase, phase
    return phase, unit


def _translate_placeholder(name: str) -> str:
    value = (name or "").strip()
    value = re.sub(r"Winner of\s+", "胜者：", value, flags=re.IGNORECASE)
    value = re.sub(r"Loser of\s+", "负者：", value, flags=re.IGNORECASE)
    value = re.sub(r"(\d+)(st|nd|rd|th) Group ([A-Z])", r"\3组第\1", value, flags=re.IGNORECASE)
    return value


def _competitor_name(competitor: dict[str, Any] | None, match_type: str) -> str:
    competitor = competitor or {}
    org = str(competitor.get("Org") or "").upper()
    org_name = ORG_NAMES.get(org, org)
    name = str(competitor.get("NameS") or competitor.get("Name") or "").strip()

    if match_type == "T" and org_name:
        return org_name
    if name:
        name = _translate_placeholder(name)
        return f"{name}（{org_name}）" if org_name and org_name not in name else name
    return org_name or "待定"


def _has_known_matchup(record: dict[str, Any]) -> bool:
    """Return whether a normalized record has an actual published matchup."""
    matchup = str(record.get("matchup") or "").strip()
    return bool(matchup and "待定" not in matchup)


def preserve_known_matchups(
    previous: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep published names when a live feed briefly omits competitors.

    The official schedule occasionally returns the same unit with empty Home /
    Away objects while it is refreshing. Treating that response as authoritative
    makes a whole day's known table-tennis matchups flash to “对阵待定”. A
    later response with real competitors still replaces the old values; only a
    pending incoming row is repaired from the last successful snapshot.
    """
    known = {
        str(row.get("id")): row
        for row in previous
        if row.get("id") and _has_known_matchup(row)
    }
    repaired: list[dict[str, Any]] = []
    for row in incoming:
        old = known.get(str(row.get("id")))
        if old and not _has_known_matchup(row):
            row = dict(row)
            for key in ("home", "away", "matchup"):
                if old.get(key):
                    row[key] = old[key]
        repaired.append(row)
    return repaired


def _score(item: dict[str, Any], disc: str = "") -> str:
    home_value = (item.get("Home") or {}).get("Result")
    away_value = (item.get("Away") or {}).get("Result")
    home = str(home_value if home_value is not None else "").strip()
    away = str(away_value if away_value is not None else "").strip()
    if home or away:
        if disc == "CKT":
            # Cricket's main score is runs; wickets and overs are shown in
            # the expandable detail. Keep the schedule column compact.
            def runs(value: str) -> str:
                match = re.match(r"\s*(\d+)", value)
                return match.group(1) if match else value
            return f"{runs(home)} : {runs(away)}"
        return f"{home or '–'} : {away or '–'}"
    status = str(item.get("Status") or "").upper()
    return STATUS_NAMES.get(status, "待赛")


def normalize_unit(item: dict[str, Any], disc: str) -> dict[str, Any] | None:
    if item.get("IsPhase") is True:
        return None

    raw_datetime = str(item.get("DateTimeRaw") or "")
    if not raw_datetime:
        return None
    try:
        beijing_time = datetime.fromisoformat(raw_datetime.replace("Z", "+00:00")).astimezone(BEIJING_TZ)
    except ValueError:
        return None

    match_type = str(item.get("Type") or "")
    home = _competitor_name(item.get("Home"), match_type)
    away = _competitor_name(item.get("Away"), match_type)
    matchup = f"{home} vs {away}" if home != "待定" or away != "待定" else "对阵待定"
    unit_source = str(item.get("UnitDesc") or item.get("UnitDescA") or "")
    phase_source = str(item.get("PhaseDesc") or "")
    stage_source = unit_source or phase_source
    home_data = bool((item.get("Home") or {}).get("HasData"))
    away_data = bool((item.get("Away") or {}).get("HasData"))
    if "victory ceremony" in stage_source.lower() and not home_data and not away_data:
        return None
    phase, stage = _stage_labels(item, disc)
    # The badminton court schedule publishes these mixed-doubles second-round
    # ties as “Not Before 16:00”. DateTimeRaw remains the original rolling
    # court slot (11:50/15:00/etc.), so use the official not-before time for
    # the affected 25 September session instead of showing a stale slot.
    if disc == "BDM" and str(item.get("Phase") or "").startswith("X.DOUBLES") and "8FNL" in str(item.get("Phase") or "") and beijing_time.date().isoformat() == "2026-09-25":
        beijing_time = beijing_time.replace(hour=16, minute=0)
    venue_source = str(item.get("VenueDesc") or item.get("LocDesc") or "")
    status = str(item.get("Status") or "SCHEDULED").upper()

    return {
        "id": f"{disc}:{item.get('Key') or item.get('ResCode') or raw_datetime}",
        "sport": disc,
        "sportName": SPORTS[disc],
        "officialKey": str(item.get("Key") or ""),
        "eventCode": str(item.get("Event") or ""),
        "phaseCode": str(item.get("Phase") or ""),
        "resCode": str(item.get("ResCode") or ""),
        "phaseOrder": item.get("PhaseOrder", 0),
        "rawStage": unit_source,
        "rawPhase": phase_source,
        "phase": phase,
        "sourceDate": beijing_time.astimezone(JAPAN_TZ).strftime("%Y-%m-%d"),
        "scheduledAt": beijing_time.isoformat(timespec="seconds"),
        "home": item.get("Home") or {},
        "away": item.get("Away") or {},
        "date": beijing_time.strftime("%Y-%m-%d"),
        "time": beijing_time.strftime("%H:%M"),
        "category": _translate_category(str(item.get("EventDesc") or "")),
        "stage": stage,
        "matchup": matchup,
        "score": _score(item, disc),
        "venue": VENUE_NAMES.get(venue_source, venue_source or "待定"),
        "status": status,
        "isLive": bool(item.get("IsLive")) or status in {"LIVE", "RUNNING"},
    }


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def sync_all(
    output_path: Path,
    progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    day_lists: dict[str, list[dict[str, Any]]] = {}
    day_errors: list[str] = []

    # Keep the initial index lookup gentle as well.  The request-level limiter
    # protects the process, while a small pool avoids a burst of seven sockets
    # when the daily job starts after a Render restart.
    with ThreadPoolExecutor(max_workers=min(2, len(SPORTS))) as pool:
        future_to_disc = {
            pool.submit(fetch_official_json, f"/s/AG2026/en/{disc}/schedule/days"): disc
            for disc in SPORTS
        }
        for future in as_completed(future_to_disc):
            disc = future_to_disc[future]
            try:
                result = future.result()
                if not isinstance(result, list):
                    raise SyncError("比赛日数据格式不正确")
                day_lists[disc] = result
            except Exception as exc:  # noqa: BLE001
                day_errors.append(f"{SPORTS[disc]}：{exc}")

    if day_errors:
        raise SyncError("；".join(day_errors))

    tasks = [
        (disc, str(day.get("raw")))
        for disc, days in day_lists.items()
        for day in days
        if day.get("raw")
    ]
    total = len(tasks)
    completed = 0
    records: list[dict[str, Any]] = []
    fetch_errors: list[str] = []
    lock = threading.Lock()

    def fetch_day(disc: str, date: str) -> tuple[str, str, Any]:
        path = f"/s/AG2026/en/{disc}/schedule/daily/{date}"
        return disc, date, fetch_official_json(path)

    # Daily feeds are numerous; two workers plus the global limiter complete a
    # full refresh predictably without tripping the official rate limit.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(fetch_day, disc, date): (disc, date) for disc, date in tasks}
        for future in as_completed(futures):
            disc, date = futures[future]
            try:
                _, _, units = future.result()
                if not isinstance(units, list):
                    raise SyncError("逐场数据格式不正确")
                for unit in units:
                    if isinstance(unit, dict):
                        record = normalize_unit(unit, disc)
                        if record:
                            records.append(record)
            except Exception as exc:  # noqa: BLE001
                fetch_errors.append(f"{SPORTS[disc]} {date}：{exc}")
            finally:
                with lock:
                    completed += 1
                    if progress:
                        progress(completed, total, f"{SPORTS[disc]} {date}")

    if fetch_errors:
        raise SyncError("；".join(fetch_errors[:5]))

    previous_records: list[dict[str, Any]] = []
    try:
        previous_payload = json.loads(output_path.read_text(encoding="utf-8"))
        previous_records = [row for row in previous_payload.get("records", []) if isinstance(row, dict)]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    records = preserve_known_matchups(previous_records, records)
    unique = {record["id"]: record for record in records}
    ordered = sorted(
        unique.values(),
        key=lambda row: (row["date"], row["time"], list(SPORTS).index(row["sport"]), row["id"]),
    )
    counts = {disc: sum(1 for row in ordered if row["sport"] == disc) for disc in SPORTS}
    payload = {
        "meta": {
            "generatedAt": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
            "timezone": "UTC+8",
            "source": OFFICIAL_RESULTS_URL,
            "total": len(ordered),
            "counts": counts,
            "sports": SPORTS,
            "officialDays": {
                disc: sorted(str(day["raw"]) for day in days if day.get("raw"))
                for disc, days in day_lists.items()
            },
        },
        "records": ordered,
    }
    _atomic_json_write(output_path, payload)
    return payload
