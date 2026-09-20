"""Official Results.Periods, SubUnits, group tables and bracket adapters.

Network failures propagate so the HTTP layer retains its last good snapshot.
Null responses mean unpublished data; neither rankings nor links are invented.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import unescape
from typing import Any, Callable

from sync_service import (
    BEIJING_TZ,
    ORG_NAMES,
    SPORTS,
    SyncError,
    _competitor_name,
    _stage_labels,
    _translate_category,
    _translate_stage,
    fetch_official_json,
)

Fetcher = Callable[[str], Any]
PRESTART = {"SCHEDULED", "UNSCHEDULED", "START_LIST", "PROVISIONAL", "GETTING_READY", "POSTPONED", "RESCHEDULED"}
TERMINAL_RESULTS = {"OFFICIAL", "FINISHED", "COMPLETED"}
PLAYER_PHOTO_BASE = "https://results.asiangames2026.org/ag2026/photos/"


def _text(value: Any, default: str = "") -> str:
    return default if value is None or value == "" else str(value)


def _name(value: Any, match_type: str = "T") -> str:
    if not isinstance(value, dict):
        return "待定"
    name = _text(value.get("Name") or value.get("NameS")).strip()
    if name.upper() in {"TBD", "TBA"}:
        return "待定"
    if name.upper() == "BYE" or _text(value.get("Reg")).upper() == "BYE":
        return "轮空"
    return _competitor_name(value, match_type)


def _submatch_name(value: Any) -> str:
    """Return a child-match participant with the published full name.

    The regular schedule intentionally uses short names for individual
    competitors.  The team-match endpoint publishes a full ``Name`` (and,
    for doubles, both names separated by ``/``), so use it here and append
    the country to keep the child match self-contained.  A few provisional
    units omit ``Name`` but include ``Members``; those members are a safe
    fallback for doubles.
    """
    if not isinstance(value, dict):
        return "待定"
    raw = _text(value.get("Name")).strip()
    members = value.get("Members") or []
    if not raw and isinstance(members, list):
        member_names = [
            _text(member.get("Name") or member.get("NameS")).strip()
            for member in members
            if isinstance(member, dict) and _text(member.get("Name") or member.get("NameS")).strip()
        ]
        raw = "/".join(member_names)
    if not raw:
        raw = _text(value.get("NameS")).strip()
    if raw.upper() in {"TBD", "TBA"}:
        return "待定"
    if raw.upper() == "BYE" or _text(value.get("Reg")).upper() == "BYE":
        return "轮空"
    org = ORG_NAMES.get(_text(value.get("Org")).upper(), _text(value.get("Org")))
    if not raw:
        return org or "待定"
    return f"{raw}（{org}）" if org and org not in raw else raw


def _submatch_type(value: Any) -> str:
    """Translate the official child unit type while preserving unknowns."""
    code = _text(value).upper()
    return {"A": "单打", "D": "双打"}.get(code, "")


def _player_photo(reg: Any) -> str:
    """Build the official athlete photo URL from a registration number.

    The Results site publishes participant photos at a stable path keyed by
    ``Reg``.  Keep the value empty when the feed has no usable registration
    number so the client can render its normal avatar fallback.  Restricting
    the characters here also prevents a malformed official value from
    escaping the intended photo directory.
    """
    registration = _text(reg).strip()
    if not registration or not re.fullmatch(r"[A-Za-z0-9_.-]+", registration):
        return ""
    return f"{PLAYER_PHOTO_BASE}{registration}.jpg"


def _lineup_players(value: Any, sport: str, match_type: str = "A") -> list[dict[str, Any]]:
    """Normalize TTE/BDM competitor members for the Line-up view.

    Team competitors expose all selected athletes in ``Members``.  Singles
    and doubles child units expose the same member objects in their own
    competitor entries; a few provisional responses omit ``Members`` and
    publish the athlete directly on the competitor, so retain that fallback
    when the competitor is clearly an individual registration.
    """
    if sport not in {"TTE", "BDM"} or not isinstance(value, dict):
        return []
    members = value.get("Members")
    if not isinstance(members, list) or not members:
        registration = _text(value.get("Reg")).strip()
        # Team registrations look like TTEWTEAM.../BDMMTEAM... and should
        # not be rendered as a country named player when their member list is
        # temporarily absent. Individual registrations are normally numeric;
        # for a clearly individual (Type=A) feed also accept a safe
        # alphanumeric registration used by a few provisional units.
        if not re.fullmatch(r"\d+", registration) and not (match_type.upper() == "A" and re.fullmatch(r"[A-Za-z0-9_.-]+", registration)):
            return []
        members = [value]
    players: list[dict[str, Any]] = []
    for member in members:
        if not isinstance(member, dict):
            continue
        name = _text(member.get("Name") or member.get("NameS")).strip()
        registration = _text(member.get("Reg")).strip()
        if not name and not registration:
            continue
        org = _text(member.get("Org") or value.get("Org")).strip()
        photo = _player_photo(registration)
        country = ORG_NAMES.get(org.upper(), org)
        players.append({
            "name": name or "待定",
            "nameS": _text(member.get("NameS")),
            "org": org,
            "orgName": country,
            "country": country,
            "reg": registration,
            "photo": photo,
            "photoUrl": photo,
            # Keep an explicit alias for clients that call the image an
            # avatar. Both point to the official photo URL.
            "avatar": photo,
            "substitute": bool(member.get("Substitute")),
            "captain": bool(member.get("Captain")),
            "posDesc": _text(member.get("PosDesc")),
            "bib": _text(member.get("Bib")),
        })
    return players


def _now() -> str:
    return datetime.now(BEIJING_TZ).isoformat(timespec="seconds")


def _record_key(record: Any) -> tuple[str, str] | None:
    if isinstance(record, str):
        value, sport = record, ""
    elif isinstance(record, dict):
        value = _text(record.get("unitKey") or record.get("key") or record.get("id"))
        sport = _text(record.get("sport") or record.get("disc")).upper()
    else:
        return None
    if ":" in value:
        prefix, value = value.split(":", 1)
        sport = sport or prefix.upper()
    if sport not in SPORTS or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
        return None
    return sport, value


def _extension(value: dict[str, Any], kind: str, code: str) -> str:
    for item in value.get("Extensions") or []:
        if isinstance(item, dict) and item.get("Type") == kind and item.get("Code") == code:
            return _text(item.get("Value"))
    return ""


def _bracket_name(value: dict[str, Any], match_type: str, sport: str) -> str:
    name = _name(value, match_type)
    if name != "待定":
        return name
    phase = _extension(value, "RESULT_INFO", "ComesFromPhaseKey")
    rank = _extension(value, "RESULT_INFO", "ComesFromRank")
    unit = _extension(value, "RESULT_INFO", "ComesFromUnitKey")
    group = re.search(r"\.GP([A-Z])-?$", phase)
    if group and rank:
        return f"{group.group(1)}组第{rank}名"
    unit_number = re.search(r"\.(\d{4})\d{2}(?:--|\d{2})$", unit)
    if phase and unit_number and rank in {"1", "2"}:
        phase_name, _ = _stage_labels({"Phase": phase, "PhaseDesc": ""}, sport)
        if phase_name != "待定":
            return f"{'胜者' if rank == '1' else '负者'}：{phase_name}第{int(unit_number.group(1))}场"
    return name


def _period_label(period: dict[str, Any], sport: str, index: int) -> str:
    label = _text(period.get("Desc") or period.get("DescS"))
    half = {"1st Half": "上半场", "2nd Half": "下半场", "H1": "上半场", "H2": "下半场"}
    if label in half:
        return half[label]
    if re.fullmatch(r"(?:Set|Game|Inning)\s*\d+", label, re.I) or not label or label.isdigit():
        return f"第{period.get('Order') or index}{'盘' if sport == 'TEN' else '局'}"
    return label


def _period_rows(payload: dict[str, Any], sport: str) -> list[list[str]]:
    info = payload.get("Info") or {}
    result = payload.get("Results") or {}
    if _text(info.get("Status")).upper() in PRESTART and not info.get("IsLive"):
        # Handball preallocates 0-0 halves before play starts.
        return []
    competitors = payload.get("Competitors") or []
    periods = result.get("Periods") or []
    try:
        current = int(result["CurrentPeriod"]) if "CurrentPeriod" in result else len(periods)
    except (ValueError, TypeError):
        current = len(periods)
    rows = []
    for index, period in enumerate(periods, 1):
        if not isinstance(period, dict) or index > current:
            continue
        # The official UI reads Competitors.Splits, using array order as the
        # Home/Away order (not StartOrder, which differs in baseball).
        pair = []
        for side, field in enumerate(("ResHome", "ResAway")):
            splits = (competitors[side].get("Splits") or []) if side < len(competitors) else []
            split = splits[index - 1] if index <= len(splits) and isinstance(splits[index - 1], dict) else {}
            pair.append(_text(split.get("Result"), _text(period.get(field))))
        if not any(pair):
            continue
        label = _period_label(period, sport, index)
        if sport == "TEN":
            if _extension(period, "PERIOD_INFO", "SuperTieBreak").lower() == "true":
                label += "（抢十）"
            else:
                tie = [_extension(period, "PERIOD_INFO", f"{side}TieBreakPoints") for side in ("Home", "Away")]
                show_tie = not info.get("IsLive") or index < current
                if show_tie and all(tie) and tie != ["0", "0"]:
                    rows.append([label, pair[0] or "—", pair[1] or "—"])
                    rows.append([label + "抢七", tie[0], tie[1]])
                    continue
        rows.append([label, pair[0] or "—", pair[1] or "—"])
    return rows


def _plain_html(value: Any) -> str:
    # Cricket FreeResInfo is official HTML; return plain text only.
    text = re.sub(r"<(?:br\s*/?|/p|/div)\s*>", " · ", _text(value), flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    return re.sub(r"\s+", " ", unescape(text)).strip(" ·")


def _cricket_metrics(competitor: dict[str, Any]) -> list[str]:
    """Read the official team summary as Runs, Wickets and Overs."""
    stats = competitor.get("Stats") or {}
    if not stats:
        splits = competitor.get("Splits") or []
        if splits and isinstance(splits[0], dict):
            stats = splits[0].get("Stats") or {}
    result = _text(competitor.get("Result") or competitor.get("ResDetail"))
    match = re.match(r"\s*(\d+)\s*-\s*(\d+)(?:\s*\(([^)]+)\))?", result)
    return [
        _text(stats.get("ST_TEAM_RUNS"), match.group(1) if match else "—"),
        _text(stats.get("ST_TEAM_WICKETS"), match.group(2) if match else "—"),
        _text(stats.get("ST_TEAM_OVERS"), match.group(3) if match and match.group(3) else "—"),
    ]


def _sections(payload: dict[str, Any], sport: str, title: str = "小分") -> list[dict[str, Any]]:
    info = payload.get("Info") or {}
    result = payload.get("Results") or {}
    competitors = payload.get("Competitors") or []
    match_type = _text(info.get("Type"), "T" if sport in {"BBL", "CKT", "VVO", "HBL"} else "A")
    names = [_name(competitors[i], match_type) if i < len(competitors) else "待定" for i in (0, 1)]
    rows = _period_rows(payload, sport)
    entity_label = "队伍" if match_type == "T" else "姓名"
    sections = [{"title": title, "entityLabel": entity_label, "columns": ["局/节", *names], "rows": rows}] if rows else []
    if sport == "TEN" and info.get("IsLive"):
        points = [_extension(result, "RESULT_INFO", f"{side}Points") for side in ("Home", "Away")]
        if any(points):
            sections.append({"title": "当前局", "entityLabel": "姓名", "columns": names, "rows": [[x or "—" for x in points]]})
    if sport == "BBL" and info.get("IsLive"):
        values = [_extension(result, "UNIT_INFO", key) for key in ("Balls", "Strikes", "Outs")]
        if any(values):
            sections.append({"title": "当前打席", "entityLabel": "项目", "columns": ["坏球", "好球", "出局"], "rows": [[x or "—" for x in values]]})
    if sport == "CKT" and _text(info.get("Status")) not in PRESTART:
        metrics = [_cricket_metrics(c) for c in competitors[:2]]
        if metrics and any(any(value != "—" for value in row) for row in metrics):
            sections.append({
                "title": "板球详情", "entityLabel": "队伍",
                "columns": ["项目", *names],
                "rows": [[label, metrics[0][index] if len(metrics) > 0 else "—", metrics[1][index] if len(metrics) > 1 else "—"] for index, label in enumerate(("Runs", "Wickets", "Overs"))],
            })
        cricket_rows = [[name, _plain_html(c.get("FreeResInfo"))] for name, c in zip(names, competitors) if _plain_html(c.get("FreeResInfo"))]
        if cricket_rows:
            sections.append({"title": "得分 / 出局 / 轮数", "columns": ["队伍", "小分"], "rows": cricket_rows})
    return sections


def _submatch_number(payload: dict[str, Any], fallback: int) -> int:
    """Read the current official display number for a child match.

    Team-tie feeds have two different numbers. ``SubMatchNum`` identifies
    the originally assigned discipline slot (for example, the second
    doubles slot), while ``SubunitOrder`` is the order currently published
    by the venue. The latter can change shortly before play starts, so it is
    the number shown in the official UI and must take precedence.
    """
    info = payload.get("Info") or {}
    result = payload.get("Results") or {}

    def positive_int(raw: Any) -> int | None:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # TTE team children use labels such as ``Match 1 M2``: the first number
    # identifies the parent tie and the trailing ``M2`` identifies this
    # child. Prefer that trailing marker or every child would be rendered as
    # Match 1. BDM uses ``Tie 7 Match 2`` without an ``M`` marker, so fall
    # back to the ordinary ``Match N`` label below.
    for description in (info.get("UnitDescA"), info.get("UnitDesc"), info.get("UnitDescS")):
        match = re.search(r"\bM\s*(\d+)\b", _text(description), flags=re.I)
        if match:
            return int(match.group(1))

    # BDM uses “Tie 7 Match 2”. It reflects last-minute scheduling changes,
    # whereas SubMatchNum can remain the original discipline slot (e.g. slot
    # 3 is currently Match 2). Prefer the label when it is available.
    for description in (info.get("UnitDescA"), info.get("UnitDesc"), info.get("UnitDescS")):
        text = _text(description)
        match = re.search(r"\bMatch\s+(\d+)\b", text, flags=re.I)
        if match:
            return int(match.group(1))

    # SubunitOrder is the API's numeric equivalent of the current display
    # order. Some preliminary records publish 0, which means “unset”.
    for raw in (
        _extension(result, "UNIT_INFO", "SubunitOrder"),
        _extension(result, "UNIT_INFO", "SubMatchNum"),
        info.get("UnitNum"),
    ):
        value = positive_int(raw)
        if value is not None:
            return value

    key = _text(info.get("Key") or info.get("RSC"))
    # A child key ends in a two-digit sub-match suffix (…00040001). The
    # preceding digits identify the tie, so do not parse the whole numeric
    # tail as the child number.
    match = re.search(r"(\d{2})$", key)
    return positive_int(match.group(1) if match else "") or fallback


def _submatch_score(competitors: list[Any], side: int, status: str, is_live: bool) -> str:
    """Return a child total while preserving an explicit live ``0``."""
    if side >= len(competitors) or not isinstance(competitors[side], dict):
        return ""
    value = competitors[side].get("Result")
    if value is None or value == "":
        return ""
    if status in PRESTART and not is_live and str(value).strip() == "0":
        return ""
    return _text(value)


def _submatches(payload: dict[str, Any], sport: str) -> list[dict[str, Any]]:
    """Convert official ``SubUnits`` into structured child matches.

    Child units are deliberately kept separate from the parent ``sections``.
    This matters for team events: a tie has several singles/doubles matches,
    and pre-start children often already have published names but no periods.
    """
    children = [child for child in payload.get("SubUnits") or [] if isinstance(child, dict)]
    ordered = sorted(enumerate(children, 1), key=lambda pair: (_submatch_number(pair[1], pair[0]), pair[0]))
    output: list[dict[str, Any]] = []
    for fallback, child in ordered:
        info = child.get("Info") or {}
        competitors = child.get("Competitors") or []
        child_key = _text(info.get("Key") or info.get("RSC"))
        number = _submatch_number(child, fallback)
        status = _text(info.get("Status")).upper()
        is_live = bool(info.get("IsLive")) or status in {"LIVE", "RUNNING"}
        sub_match = {
            "id": f"{sport}:{child_key}" if child_key else f"{sport}:sub-{number}",
            "number": number,
            "type": _submatch_type(info.get("Type")),
            "home": _submatch_name(competitors[0]) if len(competitors) > 0 else "待定",
            "away": _submatch_name(competitors[1]) if len(competitors) > 1 else "待定",
            "homePlayers": _lineup_players(competitors[0], sport, _text(info.get("Type"), "A")) if len(competitors) > 0 else [],
            "awayPlayers": _lineup_players(competitors[1], sport, _text(info.get("Type"), "A")) if len(competitors) > 1 else [],
            "homeScore": _submatch_score(competitors, 0, status, is_live),
            "awayScore": _submatch_score(competitors, 1, status, is_live),
            "status": status,
            "isLive": is_live,
            "sections": _sections(child, sport),
        }
        nested = _submatches(child, sport)
        if nested:
            sub_match["subMatches"] = nested
        output.append(sub_match)
    return output


def get_match_details(record: Any, fetcher: Fetcher | None = None) -> dict[str, Any]:
    parsed = _record_key(record)
    if not parsed:
        raise ValueError("比赛编号无效")
    sport, key = parsed
    payload = (fetcher or fetch_official_json)(f"/s/AG2026/en/{sport}/results/{key}")
    if payload is not None and not isinstance(payload, dict):
        raise SyncError("官网小分数据格式不正确")
    payload = payload or {}
    info = payload.get("Info") or {}
    competitors = payload.get("Competitors") or []
    match_type = _text(info.get("Type"), "T" if sport in {"BBL", "CKT", "VVO", "HBL"} else "A")
    names = [_name(competitors[i], match_type) if i < len(competitors) else "待定" for i in (0, 1)]
    sections = _sections(payload, sport)
    sub_matches = _submatches(payload, sport)
    home_players = _lineup_players(competitors[0], sport, match_type) if len(competitors) > 0 else []
    away_players = _lineup_players(competitors[1], sport, match_type) if len(competitors) > 1 else []
    has_content = bool(sections or sub_matches or home_players or away_players)
    return {
        "available": has_content,
        "updatedAt": _now(),
        "home": names[0],
        "away": names[1],
        "homePlayers": home_players,
        "awayPlayers": away_players,
        "sections": sections,
        "subMatches": sub_matches,
        "message": "" if has_content else "官网尚未公布该场小分",
    }


def _group_table(group: dict[str, Any]) -> dict[str, Any]:
    competitors = [c for c in group.get("Competitors") or [] if isinstance(c, dict)]
    # Rk is rank; Pos can be an initial draw position. Preserve official order
    # and points. Ambiguous sport-specific For/Against fields are not guessed.
    fields = [("排名", "Rk"), ("国家/选手", "Name"), ("已赛", "Played"), ("胜", "Won"), ("平", "Tied"), ("负", "Lost"), ("积分", "Points")]
    fields = [(label, key) for label, key in fields if key in {"Rk", "Name"} or any(_text(c.get(key)) for c in competitors)]
    rows = [[_name(c, "T" if group.get("isTeam") else "A") if key == "Name" else _text(c.get(key), "—") for _, key in fields] for c in competitors]
    return {"name": _translate_stage(_text(group.get("Desc") or group.get("DescA"), "小组")), "columns": [label for label, _ in fields], "rows": rows}


def _bracket_match_order(key: str) -> tuple[int, str]:
    """Sort official bracket units by their published unit number."""
    match = re.search(r"\.(\d+)-*$", key)
    return (int(match.group(1)) if match else 10**12, key)


def _bracket_score(value: Any, sport: str) -> str:
    score = _text(value)
    if sport == "CKT":
        match = re.match(r"\s*(\d+)", score)
        return match.group(1) if match else score
    return score


def _record_participants(record: dict[str, Any]) -> tuple[str, str] | None:
    matchup = _text(record.get("matchup"))
    if " vs " not in matchup:
        return None
    home, away = (part.strip() for part in matchup.split(" vs ", 1))
    if not home or not away or home == "对阵待定" or away == "对阵待定":
        return None
    return home, away


def _record_winner(record: dict[str, Any]) -> str:
    """Return an explicit schedule winner when the bracket feed omits it."""
    sides = (record.get("home"), record.get("away"))
    flags = []
    for side in sides:
        value = side.get("Winner") if isinstance(side, dict) else None
        flags.append(value is True or str(value).strip().lower() in {"true", "1", "yes"})
    if flags == [True, False]:
        return "home"
    if flags == [False, True]:
        return "away"
    return ""


def _enrich_bracket_rounds(rounds: list[dict[str, Any]], records: list[dict[str, Any]] | None) -> None:
    """Use the synchronized schedule as a fallback for confirmed teams.

    The official bracket feed can briefly lag the daily schedule feed. When
    that happens, a published matchup in the schedule is safe to copy into
    the corresponding bracket unit; no names are inferred from scores.
    """
    by_id = {str(record.get("id")): record for record in records or [] if isinstance(record, dict) and record.get("id")}
    for round_ in rounds:
        for match in round_.get("matches", []):
            record = by_id.get(str(match.get("id")))
            if not record:
                continue
            participants = _record_participants(record)
            if participants:
                # The daily schedule is the freshest published matchup. Use
                # it to correct a stale bracket participant as well as a TBD.
                match["home"], match["away"] = participants
            status = _text(record.get("status")).upper()
            if status:
                match["status"] = status
            if status not in TERMINAL_RESULTS:
                match["winner"] = ""
            else:
                # Some official bracket responses include the final score but
                # omit their Win flag. The synchronized schedule carries the
                # explicit winner used by the results table, so copy it when
                # available and keep the bracket flag otherwise.
                winner = _record_winner(record)
                if winner:
                    match["winner"] = winner


def _bracket_rounds(data: Any, sport: str, event_key: str, pool_keys: set[str]) -> list[dict[str, Any]]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise SyncError("官网对阵图数据格式不正确")
    rounds = []
    links: dict[str, str] = {}
    match_type = "T" if "TEAM" in event_key else "A"
    for bracket in data:
        if not isinstance(bracket, dict):
            continue
        for phase in bracket.get("Phases") or []:
            if not isinstance(phase, dict) or phase.get("Code") in pool_keys:
                continue
            matches = []
            for match in phase.get("Matches") or []:
                if not isinstance(match, dict):
                    continue
                info = match.get("Info") or {}
                home, away = match.get("Home") or {}, match.get("Away") or {}
                key = _text(info.get("Key"))
                status = _text(info.get("Status")).upper()
                # Win flags in a canceled or provisional unit are sometimes
                # copied from the original draw. Only terminal official
                # results can mark a bracket team as the winner.
                winner = "" if status and status not in TERMINAL_RESULTS else "home" if home.get("Win") else "away" if away.get("Win") else ""
                item = {"id": f"{sport}:{key}", "home": _bracket_name(home, match_type, sport), "away": _bracket_name(away, match_type, sport), "homeScore": _bracket_score(home.get("Res"), sport), "awayScore": _bracket_score(away.get("Res"), sport), "winner": winner, "status": status}
                # Only explicit feed provenance establishes a link. Numeric
                # ordering is insufficient for classification/bronze matches.
                for side in (home, away):
                    predecessor = _extension(side, "RESULT_INFO", "ComesFromUnitKey")
                    rank = _extension(side, "RESULT_INFO", "ComesFromRank")
                    if predecessor and rank == "1" and not predecessor.endswith(".--------"):
                        links[f"{sport}:{predecessor}"] = item["id"]
                matches.append(item)
            matches.sort(key=lambda item: _bracket_match_order(item["id"]))
            if not matches:
                continue
            name = _text(phase.get("Desc"), "轮次")
            bracket_name = _text(bracket.get("Desc"))
            if name.lower() in {"finals", "final"} and bracket_name.lower() not in {"finals", "final", ""}:
                name = bracket_name
            if name.lower() == "bronze":
                name = "铜牌赛"
            _, stage = _stage_labels({"Phase": phase.get("Code"), "PhaseDesc": name, "UnitDesc": name}, sport)
            rounds.append({"id": f"{bracket.get('Code', '')}:{phase.get('Code', '')}", "name": stage, "matches": matches})
    for round_ in rounds:
        for match in round_["matches"]:
            if match["id"] in links:
                match["nextMatchId"] = links[match["id"]]
    return rounds


def get_tournament(sport: str, records: list[dict[str, Any]] | None = None, fetcher: Fetcher | None = None) -> dict[str, Any]:
    sport = str(sport or "").upper()
    if sport not in SPORTS:
        raise ValueError("项目不存在")
    fetch = fetcher or fetch_official_json
    events_payload = fetch(f"/s/AG2026/en/{sport}/events/phases")
    if events_payload is None:
        events_payload = []
    if not isinstance(events_payload, list):
        raise SyncError("官网项目列表数据格式不正确")
    event_defs = [e for e in events_payload if isinstance(e, dict) and e.get("EvKey")]
    requests = [(str(e["EvKey"]), resource) for e in event_defs for resource in ("groups-v2", "brackets")]
    def read(request: tuple[str, str]) -> tuple[tuple[str, str], Any]:
        event_key, resource = request
        return request, fetch(f"/s/AG2026/en/{sport}/{resource}/{event_key}")
    with ThreadPoolExecutor(max_workers=8) as pool:
        payloads = dict(pool.map(read, requests))
    events = []
    for definition in event_defs:
        key = str(definition["EvKey"])
        groups_payload = payloads[(key, "groups-v2")]
        if groups_payload is not None and not isinstance(groups_payload, dict):
            raise SyncError("官网积分数据格式不正确")
        groups = [g for g in (groups_payload or {}).get("Groups") or [] if isinstance(g, dict)]
        pool_keys = {_text(g.get("Key")) for g in groups if g.get("Type") == "POOL"}
        tables = [_group_table(g) for g in groups if g.get("Competitors")]
        rounds = _bracket_rounds(payloads[(key, "brackets")], sport, key, pool_keys)
        _enrich_bracket_rounds(rounds, records)
        message = "" if tables or rounds else "官网尚未公布该项目积分或对阵图"
        if tables and not rounds:
            message = "循环赛积分；官网暂无淘汰赛对阵图"
        events.append({"id": key, "name": _translate_category(_text(definition.get("Desc"), key)), "groups": tables, "rounds": rounds, "message": message})
    return {"updatedAt": _now(), "events": events, "message": "" if events else "官网尚未公布该项目积分或对阵图"}
