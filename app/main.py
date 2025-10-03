from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from zoneinfo import ZoneInfo

# -----------------------------
# Config
# -----------------------------
FACEIT_API = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态 & 模板
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# 常见国家 → 时区（玩家本地时间用此；未命中回退 UTC）
COUNTRY_TZ: Dict[str, str] = {
    # 欧洲
    "RU": "Europe/Moscow", "UA": "Europe/Kyiv", "PL": "Europe/Warsaw",
    "DE": "Europe/Berlin", "FR": "Europe/Paris", "ES": "Europe/Madrid",
    "IT": "Europe/Rome", "SE": "Europe/Stockholm", "NO": "Europe/Oslo",
    "FI": "Europe/Helsinki", "DK": "Europe/Copenhagen", "CZ": "Europe/Prague",
    "SK": "Europe/Bratislava", "HU": "Europe/Budapest", "RO": "Europe/Bucharest",
    "BG": "Europe/Sofia", "GR": "Europe/Athens", "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels", "PT": "Europe/Lisbon", "IE": "Europe/Dublin",
    "GB": "Europe/London", "LT": "Europe/Vilnius", "LV": "Europe/Riga",
    "EE": "Europe/Tallinn", "IS": "Atlantic/Reykjavik", "CH": "Europe/Zurich",
    "AT": "Europe/Vienna", "BA": "Europe/Sarajevo", "RS": "Europe/Belgrade",
    "HR": "Europe/Zagreb", "SI": "Europe/Ljubljana", "ME": "Europe/Podgorica",
    "MK": "Europe/Skopje", "AL": "Europe/Tirane", "TR": "Europe/Istanbul",
    # 美洲
    "US": "America/New_York", "CA": "America/Toronto", "BR": "America/Sao_Paulo",
    "AR": "America/Argentina/Buenos_Aires", "CL": "America/Santiago",
    "MX": "America/Mexico_City",
    # 亚太
    "CN": "Asia/Shanghai", "TW": "Asia/Taipei", "HK": "Asia/Hong_Kong",
    "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "SG": "Asia/Singapore",
    "MY": "Asia/Kuala_Lumpur", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh",
    "ID": "Asia/Jakarta", "PH": "Asia/Manila", "IN": "Asia/Kolkata",
    "AU": "Australia/Sydney", "NZ": "Pacific/Auckland",
    # 中东/非洲（常见 Faceit 地区）
    "AE": "Asia/Dubai", "SA": "Asia/Riyadh", "EG": "Africa/Cairo", "ZA": "Africa/Johannesburg",
}


# -----------------------------
# Faceit API helpers
# -----------------------------
def faceit_get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def fetch_player_profile(nickname: str) -> Optional[dict]:
    """Players endpoint：拿到 player_id / country / faceit_url 等。"""
    return faceit_get(f"{FACEIT_API}/players", params={"nickname": nickname})


def fetch_player_id(nickname: str) -> Optional[str]:
    p = fetch_player_profile(nickname)
    return p.get("player_id") if p else None


def fetch_matches(player_id: str, limit: int = 5) -> List[dict]:
    """最近比赛（仅 cs2）。"""
    data = faceit_get(f"{FACEIT_API}/players/{player_id}/history",
                      params={"game": "cs2", "limit": limit})
    return data.get("items", []) if data else []


def parse_score_from_round(r0: dict) -> Tuple[int, int]:
    """
    兼容两种统计：
    - r0["round_stats"]["Score"] 形如 "13 / 10"
    - r0["teams"][i]["score"]
    """
    # 1) 优先 round_stats.Score
    round_stats = r0.get("round_stats") or {}
    score_str = round_stats.get("Score")
    if isinstance(score_str, str) and "/" in score_str:
        try:
            a, b = score_str.replace(" ", "").split("/")
            return int(a), int(b)
        except Exception:
            pass

    # 2) 退化：teams[*].score
    teams = r0.get("teams", [])
    scores = []
    for t in teams:
        sc = t.get("score")
        try:
            scores.append(int(sc))
        except Exception:
            pass
    if len(scores) >= 2:
        return scores[0], scores[1]

    return 0, 0


def fetch_match_stats_for_player(match_id: str, player_id: str) -> Optional[dict]:
    """
    返回：
    {
      "score_a": 13, "score_b": 10, "player_side": 0/1,
      "kills": int, "assists": int, "deaths": int,
      "adr": float, "hs": int, "kd": float, "kr": float
    }
    """
    data = faceit_get(f"{FACEIT_API}/matches/{match_id}/stats")
    if not data:
        return None

    rounds = data.get("rounds") or []
    if not rounds:
        return None

    r0 = rounds[0]
    score_a, score_b = parse_score_from_round(r0)

    # 找到玩家所在队，顺便抓玩家统计
    player_stats: Dict[str, Any] = {}
    player_side = None
    for side_idx, team in enumerate(r0.get("teams", [])):
        for p in team.get("players", []):
            if p.get("player_id") == player_id:
                s = p.get("player_stats", {}) or {}

                def fget(name: str, cast=float, default=0):
                    v = s.get(name, default)
                    try:
                        return cast(v)
                    except Exception:
                        return default

                player_stats = {
                    "kills": fget("Kills", int, 0),
                    "deaths": fget("Deaths", int, 0),
                    "assists": fget("Assists", int, 0),
                    "adr": round(fget("ADR", float, 0.0), 1),
                    "hs": fget("Headshots %", int, 0),
                    "kd": round(fget("K/D Ratio", float, 0.0), 2),
                    "kr": round(fget("K/R Ratio", float, 0.0), 2),
                }
                player_side = side_idx
                break

    if player_side is None:
        # 找不到该玩家（极少数异常场景）
        player_side = 0

    return {
        "score_a": score_a,
        "score_b": score_b,
        "player_side": player_side,
        **player_stats,
    }


def faceit_match_url(match_id: str) -> str:
    return f"https://www.faceit.com/en/cs2/room/{match_id}"


def faceit_player_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{nickname}"


def get_player_tz_from_country(country_code: Optional[str]) -> ZoneInfo:
    tz = COUNTRY_TZ.get((country_code or "").upper())
    try:
        return ZoneInfo(tz) if tz else ZoneInfo("UTC")
    except Exception:
        return ZoneInfo("UTC")


def format_local_time(ts: Optional[int], tz: ZoneInfo) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz).strftime("%m/%d/%Y, %I:%M %p")


# -----------------------------
# FastAPI endpoints
# -----------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/player")
def api_player(
    name: str,
    limit: int = 5
):
    """
    按“玩家自己的当地时间”返回最近比赛。
    """
    profile = fetch_player_profile(name)
    if not profile:
        return {"player": name, "profile_url": faceit_player_url(name), "matches": []}

    player_id = profile.get("player_id")
    profile_url = profile.get("faceit_url") or faceit_player_url(name)
    country = (profile.get("country") or "").upper()
    tz = get_player_tz_from_country(country)

    items = fetch_matches(player_id, limit)
    rows: List[dict] = []

    for m in items:
        match_id = m.get("match_id")
        started_at = m.get("started_at")
        map_name = m.get("game_map") or "-"

        stat = fetch_match_stats_for_player(match_id, player_id)
        if not stat:
            # 没拿到 stats，仍然显示基本信息（Score 置空）
            rows.append({
                "date": format_local_time(started_at, tz),
                "match_id": match_id,
                "match_url": faceit_match_url(match_id),
                "result": "-",
                "score": "-",
                "k": 0, "a": 0, "d": 0, "kd": 0.0, "adr": 0.0, "hs": 0,
                "map": map_name,
                "win": False,
            })
            continue

        sa, sb = stat["score_a"], stat["score_b"]
        side = stat["player_side"]
        # 玩家所在队的分数
        my_score = sa if side == 0 else sb
        op_score = sb if side == 0 else sa
        win = my_score > op_score

        rows.append({
            "date": format_local_time(started_at, tz),
            "match_id": match_id,
            "match_url": faceit_match_url(match_id),
            "result": "Win" if win else "Loss",
            "score": f"{sa} / {sb}",
            "k": stat["kills"], "a": stat["assists"], "d": stat["deaths"],
            "kd": stat["kd"], "adr": stat["adr"], "hs": stat["hs"],
            "map": map_name,
            "win": win,
        })

    return {"player": name, "profile_url": profile_url, "matches": rows}


@app.get("/health")
def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
def version_redirect():
    return RedirectResponse(url="/health")
