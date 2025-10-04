from __future__ import annotations

import os
import pytz
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from dotenv import load_dotenv

# 载入本地 .env（Render 上用环境变量即可）
load_dotenv()

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_API = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

app = FastAPI(title="Pro Tracker 1.0", version="0.1.2")

# 如果你项目里有静态资源目录，挂一下（可选）
if os.path.isdir("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


# ---------------------------
# HTTP 工具
# ---------------------------
def _need_key():
    if not FACEIT_API_KEY:
        raise HTTPException(
            500,
            "FACEIT_API_KEY 未设置。请在 .env 或 Render 环境变量里配置 FACEIT_API_KEY（Faceit Open API Key）。",
        )

def _req(url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    _need_key()
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=20)
        if r.status_code == 404:
            raise HTTPException(404, f"资源不存在：{url}")
        if r.status_code == 401:
            raise HTTPException(401, "FACEIT_API_KEY 无效或权限不足。")
        r.raise_for_status()
        return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"请求 Faceit API 失败：{e}")


# ---------------------------
# URL 构造
# ---------------------------
def faceit_profile_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{nickname}"

def faceit_match_url(match_id: str) -> str:
    return f"https://www.faceit.com/en/cs2/room/{match_id}"


# ---------------------------
# 辅助
# ---------------------------
def tz_for_country(country_code: Optional[str]):
    try:
        if country_code:
            zones = pytz.country_timezones.get(country_code.upper())
            if zones:
                return pytz.timezone(zones[0])
    except Exception:
        pass
    return pytz.utc

def _num(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, "", "-"):
            return default
        return float(x)
    except Exception:
        return default

def _int(x: Any, default: int = 0) -> int:
    try:
        return int(float(x))
    except Exception:
        return default

def compute_rating_like(kills: int, deaths: int, kpr: float, adr: float) -> float:
    # 近似 HLTV2.0：0.0073*ADR + 0.3591*KPR + 0.5334*(1-DPR)
    if kpr <= 0:
        dpr = 0.5
    else:
        rounds = max(1.0, kills / max(1e-9, kpr))
        dpr = min(1.0, deaths / rounds)
    rating = 0.0073 * adr + 0.3591 * kpr + 0.5334 * (1.0 - dpr)
    return round(rating, 2)


# ---------------------------
# Faceit API 封装
# ---------------------------
def api_player_by_nickname(nickname: str) -> Dict[str, Any]:
    url = f"{FACEIT_API}/players"
    return _req(url, params={"nickname": nickname})

def api_player_by_id(pid: str) -> Dict[str, Any]:
    url = f"{FACEIT_API}/players/{pid}"
    return _req(url)

def api_player_matches(player_id: str, limit: int) -> List[Dict[str, Any]]:
    url = f"{FACEIT_API}/players/{player_id}/history"
    data = _req(url, params={"game": "cs2", "offset": 0, "limit": limit})
    return data.get("items", [])

def api_match_stats(match_id: str) -> Dict[str, Any]:
    url = f"{FACEIT_API}/matches/{match_id}/stats"
    return _req(url)


# ---------------------------
# 解析比赛 stats
# ---------------------------
def parse_one_match_for_player(
    match_id: str, player_id: str
) -> Optional[Dict[str, Any]]:
    data = api_match_stats(match_id)
    rounds = data.get("rounds", [])
    if not rounds:
        return None
    r0 = rounds[0]
    round_stats = r0.get("round_stats", {}) or {}
    map_name = round_stats.get("Map") or round_stats.get("Map Name") or "-"
    # Score 统一成 "a/b"
    score_str = (round_stats.get("Score") or "").replace(" ", "").replace("-", "/")
    if "/" not in score_str:
        score_str = "0/0"

    # 找到玩家所在队 & 玩家 stats
    my_team_idx = None
    my_stats: Dict[str, Any] = {}
    teams = r0.get("teams", []) or []
    for idx, t in enumerate(teams):
        for p in t.get("players", []):
            if p.get("player_id") == player_id:
                my_team_idx = idx
                my_stats = p.get("player_stats", {}) or {}
                break
        if my_team_idx is not None:
            break
    if my_team_idx is None:
        return None

    # 计算“我方/对方”分
    try:
        a, b = score_str.split("/", 1)
        a1, b1 = int(a), int(b)
    except Exception:
        a1, b1 = 0, 0
    if my_team_idx == 0:
        s_my, s_opp = a1, b1
    else:
        s_my, s_opp = b1, a1
    win = s_my > s_opp

    # 取 K/A/D、ADR、K/R 并计算 rating
    k = _int(my_stats.get("Kills"))
    a_ = _int(my_stats.get("Assists"))
    d = _int(my_stats.get("Deaths"))
    adr = _num(my_stats.get("ADR"))
    kpr = _num(my_stats.get("K/R Ratio"))
    rating = compute_rating_like(k, d, kpr, adr)
    is_ace = _int(my_stats.get("Penta Kills")) > 0

    return {
        "match_id": match_id,
        "match_url": faceit_match_url(match_id),
        "map": map_name,
        "score": f"{s_my}/{s_opp}",
        "win": win,
        "k": k,
        "a": a_,
        "d": d,
        "rating": rating,
        "is_ace": is_ace,
    }


# ---------------------------
# 路由
# ---------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.2"}


@app.get("/player")
def player_view(
    q: str = Query(..., description="玩家昵称或 'id:PLAYER_ID'"),
    limit: int = Query(5, ge=1, le=20, description="每位玩家条数"),
) -> JSONResponse:
    """
    q 支持两种：
      - 昵称（默认）：如 'donk666'
      - ID：以 'id:' 开头，如 'id:e5e8e2a6-d716-4493-b949-e16965f41654'
    """
    # 1) 获取玩家信息（含 country，用于时区）
    if q.lower().startswith("id:"):
        pid = q.split(":", 1)[1].strip()
        info = api_player_by_id(pid)
    else:
        info = api_player_by_nickname(q)
    player_id = info.get("player_id")
    nickname = info.get("nickname") or q
    country = info.get("country")

    if not player_id:
        raise HTTPException(404, f"player not found: {q}")

    player_tz = tz_for_country(country)

    # 2) 历史比赛
    items = api_player_matches(player_id, limit=limit)
    ts_map: Dict[str, int] = {}
    for it in items:
        mid = it.get("match_id")
        st = it.get("started_at") or it.get("finished_at")
        if mid:
            ts_map[mid] = int(st) if st else 0

    # 3) 逐一拿 stats
    matches: List[Dict[str, Any]] = []
    for it in items:
        mid = it.get("match_id")
        if not mid:
            continue
        m = parse_one_match_for_player(mid, player_id)
        if not m:
            continue

        # 用 history 的 started_at -> 选手本地时间
        started_at_ts = ts_map.get(mid)
        if started_at_ts:
            dt_utc = datetime.fromtimestamp(started_at_ts, tz=timezone.utc)
            dt_local = dt_utc.astimezone(player_tz)
            m["date"] = dt_local.strftime("%m/%d/%Y, %I:%M %p")
        else:
            m["date"] = "-"

        matches.append(m)

    out = {
        "player": nickname,
        "profile_url": faceit_profile_url(nickname),
        "matches": matches,
    }
    return JSONResponse(out)
