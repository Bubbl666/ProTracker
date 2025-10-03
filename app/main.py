from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

FACEIT_API = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态与模板
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# ---------- Faceit API 基础 ----------

def faceit_get(url: str, params: Optional[dict] = None) -> Optional[dict]:
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code == 200:
            return r.json()
        return None
    except Exception:
        return None


def fetch_player_profile(nickname: str) -> Optional[dict]:
    """拿 player_id、faceit_url、country、games 等"""
    url = f"{FACEIT_API}/players"
    data = faceit_get(url, params={"nickname": nickname})
    return data


def fetch_player_id(nickname: str) -> Optional[str]:
    prof = fetch_player_profile(nickname)
    return prof.get("player_id") if prof else None


def fetch_matches(player_id: str, limit: int = 5) -> List[dict]:
    """最近比赛（仅 cs2）"""
    url = f"{FACEIT_API}/players/{player_id}/history"
    data = faceit_get(url, params={"game": "cs2", "limit": limit})
    return data.get("items", []) if data else []


def fetch_match_stats_for_player(match_id: str, player_id: str) -> Optional[dict]:
    """
    解析比赛统计：
    - Score: 从 rounds[0].teams[*].score 取
    - 玩家统计: Kills / Deaths / Assists / ADR / HS% / K/D Ratio / K/R Ratio
    """
    url = f"{FACEIT_API}/matches/{match_id}/stats"
    data = faceit_get(url)
    if not data:
        return None

    rounds = data.get("rounds", [])
    if not rounds:
        return None

    r0 = rounds[0]
    teams = r0.get("teams", [])
    if not teams:
        return None

    score_a = score_b = None
    player_stats: Dict[str, Any] = {}

    for t in teams:
        # 两个队的分数
        if "score" in t:
            if score_a is None:
                score_a = int(t["score"])
            else:
                score_b = int(t["score"])

        for p in t.get("players", []):
            if p.get("player_id") == player_id:
                s = p.get("player_stats", {})
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

    score_a = score_a if isinstance(score_a, int) else 0
    score_b = score_b if isinstance(score_b, int) else 0

    return {
        "score_a": score_a,
        "score_b": score_b,
        "score": f"{score_a} / {score_b}",
        **player_stats,
    }


# ---------- 工具 ----------

def format_local_time(ts: Optional[int], tz_name: str) -> str:
    if not ts:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%m/%d/%Y, %I:%M %p")


def faceit_match_url(match_id: str) -> str:
    return f"https://www.faceit.com/en/cs2/room/{match_id}"


def faceit_demo_url(match_id: str) -> str:
    return faceit_match_url(match_id)


def faceit_player_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{nickname}"


# ---------- API ----------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/player")
def get_player(
    name: str,
    limit: int = 5,
    tz: str = Query("Asia/Shanghai", description="IANA 时区，如 Asia/Shanghai、Europe/Moscow")
):
    """
    单个选手：最近比赛（表格字段）
    """
    prof = fetch_player_profile(name)
    if not prof:
        return {"player": name, "profile_url": faceit_player_url(name), "matches": []}

    player_id = prof.get("player_id")
    profile_url = prof.get("faceit_url") or faceit_player_url(name)

    items = fetch_matches(player_id, limit)
    rows: List[dict] = []

    for m in items:
        match_id = m.get("match_id")
        map_name = m.get("game_map") or "-"
        started_at = m.get("started_at")

        stat = fetch_match_stats_for_player(match_id, player_id) or {}

        score_a = stat.get("score_a", 0)
        score_b = stat.get("score_b", 0)
        win = (score_a > score_b)  # 中立推断，用于上色

        rows.append({
            "date": format_local_time(started_at, tz),
            "result": "Win" if win else "Loss",
            "score": f"{score_a} / {score_b}",
            "k": stat.get("kills", 0),
            "a": stat.get("assists", 0),
            "d": stat.get("deaths", 0),
            "kd": stat.get("kd", 0.0),
            "adr": stat.get("adr", 0.0),
            "hs": stat.get("hs", 0),
            "map": map_name,
            "match_url": faceit_match_url(match_id),
            "demo_url": faceit_demo_url(match_id),
        })

    return {"player": name, "profile_url": profile_url, "matches": rows}


@app.get("/health")
def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
def version_redirect():
    return RedirectResponse(url="/health")
