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


# =========================
# 基础配置
# =========================
app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态资源与模板
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

# Faceit Open API（需要你在 Render 环境变量或 .env 设置 FACEIT_API_KEY）
FACEIT_API_BASE = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY", "").strip()

# 默认搜索玩家（你可以改这里）
DEFAULT_NICKNAMES: List[str] = ["donk666", "niko", "s1s1", "m0NESY", "CEMEN_BAKIN", "nocries", "b1t", "jks"]

# 每行最多几列（5 列是 1080p 体验较好）
MAX_COLUMNS_PER_ROW = 5


# =========================
# Faceit API 封装
# =========================
def _headers() -> Dict[str, str]:
    if not API_KEY:
        return {}
    return {"Authorization": f"Bearer {API_KEY}"}


def fetch_player_summary(nickname: str) -> Optional[Dict[str, Any]]:
    """通过昵称拿 player 基本信息（含 country/timezone/profile 等）"""
    url = f"{FACEIT_API_BASE}/players"
    r = requests.get(url, params={"nickname": nickname}, headers=_headers(), timeout=15)
    if r.status_code != 200:
        return None
    return r.json()


def fetch_matches_history(player_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """最近比赛（匹配）"""
    url = f"{FACEIT_API_BASE}/players/{player_id}/history"
    r = requests.get(url, params={"game": "cs2", "limit": limit}, headers=_headers(), timeout=15)
    if r.status_code != 200:
        return []
    return r.json().get("items", [])


def fetch_match_stats(match_id: str) -> Optional[Dict[str, Any]]:
    """比赛详细统计（用于拿到 K/A/D、地图、比分等）"""
    url = f"{FACEIT_API_BASE}/matches/{match_id}/stats"
    r = requests.get(url, headers=_headers(), timeout=20)
    if r.status_code != 200:
        return None
    return r.json()


# =========================
# 业务逻辑
# =========================
def to_local_time(ts: int, tz_str: str) -> str:
    """把 UNIX 秒时间戳转为选手当地时间（tz_str 来自玩家 profile 的 timezone，如果没有，用 Europe/Moscow 兜底）"""
    tz = pytz.timezone(tz_str or "Europe/Moscow")
    dt = datetime.fromtimestamp(ts, tz)
    # 统一显示：MM/DD/YYYY, HH:mm AM/PM
    return dt.strftime("%m/%d/%Y, %I:%M %p")


def map_img_filename(map_name: str) -> str:
    """地图名转图文件名（你只要把 png 放到 static/maps/ 下即可）"""
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
    """
    近似 HLTV 2.0（估算）
    说明：Faceit 不提供完整 2.0 所需全部指标，这里做一个稳定的近似：
      - K/D 权重 0.45
      - ADR/100 权重 0.35
      - (K/(D+1)) 的简单 impact 权重 0.20
    """
    kills = float(player_stats.get("Kills", 0))
    deaths = float(player_stats.get("Deaths", 0))
    adr = float(player_stats.get("ADR", 0))
    kd = kills / deaths if deaths > 0 else kills  # 避免除 0
    impact = kills / (deaths + 1.0)
    rating = kd * 0.45 + (adr / 100.0) * 0.35 + impact * 0.20
    return round(rating, 2)


def parse_match_panel(match_item: Dict[str, Any], player_id: str, player_tz: str) -> Optional[Dict[str, Any]]:
    """
    将一场 history item + stats 转为模板可用数据：
    Date、Score（带颜色）、Map（文本+小图）、K/A/D、Rating、Faceit 链接、是否五杀高亮
    """
    match_id = match_item.get("match_id")
    started_at = int(match_item.get("started_at", 0))
    if not match_id:
        return None

    stats = fetch_match_stats(match_id)
    if not stats:
        return None

    rounds = stats.get("rounds") or []
    if not rounds:
        return None

    r0 = rounds[0]
    teams = r0.get("teams", [])
    round_stats = r0.get("round_stats", {}) or {}
    map_name = round_stats.get("Map", match_item.get("map", "Unknown"))
    score_str = round_stats.get("Score", round_stats.get("score", "")) or ""

    # 找到玩家所在队伍 + 玩家统计
    player_stats = None
    my_team_id = None
    for team in teams:
        for p in team.get("players", []):
            if p.get("player_id") == player_id:
                player_stats = p.get("player_stats", {})
                my_team_id = team.get("team_id")
                break

    if not player_stats:
        return None

    winner = r0.get("winner", "")
    result = "Win" if my_team_id and my_team_id == winner else "Loss"

    kills = int(player_stats.get("Kills", 0))
    assists = int(player_stats.get("Assists", 0))
    deaths = int(player_stats.get("Deaths", 0))

    rating = hltv_like_rating(player_stats)

    # 检查五杀（某些统计提供 Penta Kills）
    has_penta = False
    for key in ("Penta Kills", "PentaKills", "Penta"):
        if key in player_stats and str(player_stats[key]).isdigit() and int(player_stats[key]) > 0:
            has_penta = True
            break

    return {
        "date": to_local_time(started_at, player_tz),                 # 选手当地时间
        "score": score_str,                                           # 形如 "13 / 10"
        "result_class": "win" if result == "Win" else "loss",
        "map": map_name,                                              # 文本
        "map_img": f"/static/maps/{map_img_filename(map_name)}",      # 小图
        "k": kills,
        "a": assists,
        "d": deaths,
        "rating": f"{rating:.2f}",
        "match_link": f"https://www.faceit.com/en/cs2/room/{match_id}",
        "highlight": has_penta,                                       # 五杀高亮
    }


def build_player_panel(nickname: str, limit: int) -> Optional[Dict[str, Any]]:
    """拼装一个玩家面板：标题、Profile 链接、最近 N 场（Date/Score/Map/KAD/Rating）"""
    prof = fetch_player_summary(nickname)
    if not prof:
        return None

    player_id = prof.get("player_id")
    if not player_id:
        return None

    # 有些 profile 会带 timezone；如果没有，兜底用 Europe/Moscow
    tz_str = prof.get("settings", {}).get("timezone") or "Europe/Moscow"

    items = fetch_matches_history(player_id, limit=limit)
    matches: List[Dict[str, Any]] = []
    for it in items:
        parsed = parse_match_panel(it, player_id, tz_str)
        if parsed:
            matches.append(parsed)

    return {
        "nickname": prof.get("nickname", nickname),
        "faceit_url": f"https://www.faceit.com/en/players/{prof.get('nickname', nickname)}",
        "matches": matches
    }


# =========================
# 路由
# =========================
@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    players: str = Query(None, description="逗号分隔的玩家昵称"),
    limit: int = Query(5, ge=1, le=20, description="每位玩家最近场数"),
) -> HTMLResponse:
    # 玩家来源：URL ?players=...，否则用默认
    nicknames = [p.strip() for p in (players.split(",") if players else DEFAULT_NICKNAMES) if p.strip()]
    panels: List[Dict[str, Any]] = []

    for nick in nicknames:
        panel = build_player_panel(nick, limit)
        if panel:
            panels.append(panel)

    context = {
        "request": request,
        "panels": panels,
        "limit": limit,
        "query_players": ", ".join(nicknames),
        "max_cols": MAX_COLUMNS_PER_ROW,
        "app_version": "0.1.1",
    }
    return templates.TemplateResponse("index.html", context)


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")
