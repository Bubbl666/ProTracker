from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, Any, List, Optional

import pytz
import requests
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

app = FastAPI(title="Pro Tracker 1.0", version="0.1.2")

# 静态资源与模板
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Faceit API
FACEIT_API_BASE = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY", "").strip()

# 默认玩家（你可以直接改这里）
DEFAULT_NICKNAMES: List[str] = ["donk666", "niko", "s1s1", "CEMEN_BAKIN", "m0NESY", "b1t", "nocries"]

# 一行最多显示列数
MAX_COLUMNS_PER_ROW = 5


def _headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


def fetch_player_summary(nickname: str) -> Optional[Dict[str, Any]]:
    r = requests.get(
        f"{FACEIT_API_BASE}/players",
        params={"nickname": nickname},
        headers=_headers(),
        timeout=15,
    )
    return r.json() if r.status_code == 200 else None


def fetch_matches_history(player_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    r = requests.get(
        f"{FACEIT_API_BASE}/players/{player_id}/history",
        params={"game": "cs2", "limit": limit},
        headers=_headers(),
        timeout=15,
    )
    if r.status_code != 200:
        return []
    return r.json().get("items", [])


def fetch_match_stats(match_id: str) -> Optional[Dict[str, Any]]:
    r = requests.get(
        f"{FACEIT_API_BASE}/matches/{match_id}/stats",
        headers=_headers(),
        timeout=20,
    )
    return r.json() if r.status_code == 200 else None


def to_local_time(ts: int, tz_str: str) -> str:
    tz = pytz.timezone(tz_str or "Europe/Moscow")
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%m/%d/%Y, %I:%M %p")


def map_img_filename(map_name: str) -> str:
    """把 faceit 的 Map 字段映射到 /static/maps/*.png"""
    name = (map_name or "").lower()
    mapping = {
        "de_mirage": "mirage.png",
        "de_inferno": "inferno.png",
        "de_dust2": "dust2.png",
        "de_overpass": "overpass.png",
        "de_vertigo": "vertigo.png",
        "de_nuke": "nuke.png",
        "de_train": "train.png",
        "de_ancient": "ancient.png",
        "de_anubis": "anubis.png",
    }
    return mapping.get(name, "default.png")


def hltv_like_rating(player_stats: Dict[str, Any]) -> float:
    kills = float(player_stats.get("Kills", 0))
    deaths = float(player_stats.get("Deaths", 0))
    adr = float(player_stats.get("ADR", 0))
    kd = kills / deaths if deaths > 0 else kills
    impact = kills / (deaths + 1.0)
    rating = kd * 0.45 + (adr / 100.0) * 0.35 + impact * 0.20
    return round(rating, 2)


def _parse_score(score_str: str) -> Optional[tuple[int, int]]:
    try:
        parts = [p.strip() for p in score_str.replace("\\", "/").split("/")]
        if len(parts) != 2:
            return None
        a = int(parts[0])
        b = int(parts[1])
        return a, b
    except Exception:
        return None


def parse_match_panel(item: Dict[str, Any], player_id: str, player_tz: str) -> Optional[Dict[str, Any]]:
    match_id = item.get("match_id")
    if not match_id:
        return None

    stats = fetch_match_stats(match_id)
    if not stats:
        return None

    rounds = stats.get("rounds") or []
    if not rounds:
        return None

    r0 = rounds[0]
    round_stats = r0.get("round_stats", {}) or {}
    teams = r0.get("teams", []) or []
    map_name = round_stats.get("Map", item.get("map", "Unknown"))
    score = round_stats.get("Score", round_stats.get("score", "")) or ""
    winner_id = r0.get("winner", "")

    # 找到玩家所在队伍及其索引
    my_team_id = None
    my_team_index = None
    pstats = None
    for idx, t in enumerate(teams):
        for p in t.get("players", []):
            if p.get("player_id") == player_id:
                my_team_id = t.get("team_id")
                my_team_index = idx
                pstats = p.get("player_stats", {})
                break
        if pstats is not None:
            break

    if pstats is None:
        return None

    # 先按 winner 判断
    result_class = None
    if my_team_id and winner_id:
        result_class = "win" if my_team_id == winner_id else "loss"

    # 如果 winner 不可靠/缺失，回退用 score + 队伍顺序判断
    if result_class is None:
        ab = _parse_score(score)
        if ab and my_team_index in (0, 1):
            a, b = ab
            my_score = a if my_team_index == 0 else b
            opp_score = b if my_team_index == 0 else a
            result_class = "win" if my_score > opp_score else "loss"

    # 再不行，最后兜底为 loss，避免全显示同色
    if result_class is None:
        result_class = "loss"

    kills = int(pstats.get("Kills", 0))
    assists = int(pstats.get("Assists", 0))
    deaths = int(pstats.get("Deaths", 0))
    rating = f"{hltv_like_rating(pstats):.2f}"

    # 五杀高亮
    highlight = False
    for key in ("Penta Kills", "PentaKills", "Penta"):
        if key in pstats and str(pstats[key]).isdigit() and int(pstats[key]) > 0:
            highlight = True
            break

    return {
        "date": to_local_time(int(item.get("started_at", 0)), player_tz),
        "score": score,
        "result_class": result_class,
        "map": map_name,
        "map_img": f"/static/maps/{map_img_filename(map_name)}",
        "k": kills,
        "a": assists,
        "d": deaths,
        "rating": rating,
        "match_link": f"https://www.faceit.com/en/cs2/room/{match_id}",
        "highlight": highlight,
    }


def build_player_panel(nickname: str, limit: int) -> Optional[Dict[str, Any]]:
    prof = fetch_player_summary(nickname)
    if not prof:
        return None

    pid = prof.get("player_id")
    if not pid:
        return None

    tz_str = prof.get("settings", {}).get("timezone") or "Europe/Moscow"
    items = fetch_matches_history(pid, limit=limit)

    matches: List[Dict[str, Any]] = []
    for it in items:
        parsed = parse_match_panel(it, pid, tz_str)
        if parsed:
            matches.append(parsed)

    return {
        "nickname": prof.get("nickname", nickname),
        "faceit_url": f"https://www.faceit.com/en/players/{prof.get('nickname', nickname)}",
        "matches": matches,
    }


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    players: str = Query(None),
    limit: int = Query(5, ge=1, le=20),
):
    nicknames = [x.strip() for x in (players.split(",") if players else DEFAULT_NICKNAMES) if x.strip()]
    panels: List[Dict[str, Any]] = []
    for n in nicknames:
        p = build_player_panel(n, limit)
        if p:
            panels.append(p)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "panels": panels,
            "limit": limit,
            "query_players": ", ".join(nicknames),
            "max_cols": MAX_COLUMNS_PER_ROW,
            "app_version": "0.1.2",
        },
    )


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.2"}


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")
