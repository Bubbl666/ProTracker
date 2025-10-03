from __future__ import annotations

import os
import math
import pytz
import requests
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
from dotenv import load_dotenv

# 载入环境变量（本地跑会用到 .env；Render 上直接用 Dashboard 配）
load_dotenv()

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
if not FACEIT_API_KEY:
    # 不马上报错；在首次调用 API 时再给出友好提示
    pass

FACEIT_API = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态目录（可选）
if os.path.isdir("app/static"):
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


def _req(url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """带 key 的 GET 请求 + 统一报错。"""
    if not FACEIT_API_KEY:
        raise HTTPException(
            500,
            "FACEIT_API_KEY 未设置。请在 .env 或环境变量中配置 FACEIT_API_KEY（Faceit Open API Key）。",
        )
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
# Faceit URL 构造
# ---------------------------
def faceit_profile_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{nickname}"

def faceit_match_url(match_id: str) -> str:
    # 新 UI 下的 room 链接
    return f"https://www.faceit.com/en/cs2/room/{match_id}"


# ---------------------------
# 国家 -> 时区（粗略映射）
# ---------------------------
def tz_for_country(country_code: Optional[str]) -> timezone:
    try:
        if country_code:
            zones = pytz.country_timezones.get(country_code.upper())
            if zones:
                return pytz.timezone(zones[0])
    except Exception:
        pass
    return pytz.utc


# ---------------------------
# 小工具
# ---------------------------
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


# ---------------------------
# 评分（HLTV 2.0-like 估算）
# rating ≈ 0.0073*ADR + 0.3591*KPR + 0.5334*(1-DPR)
# KPR = K/R；DPR ≈ Deaths / Rounds；Rounds≈Kills/KPR
# ---------------------------
def compute_rating_like(kills: int, deaths: int, kpr: float, adr: float) -> Optional[float]:
    if kpr <= 0:
        # 兜底估 DPR
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
    data = _req(url, params={"nickname": nickname})
    # 期望字段：player_id, nickname, country 等
    return data

def api_player_matches(player_id: str, limit: int) -> List[Dict[str, Any]]:
    """拉取玩家最近比赛列表（只拿 match_id、started_at 等）"""
    url = f"{FACEIT_API}/players/{player_id}/history"
    # game=cs2；limit <= 20 足够
    data = _req(url, params={"game": "cs2", "offset": 0, "limit": limit})
    # 返回 data.items: [{match_id, game_mode, finished_at/started_at, ...}, ...]
    return data.get("items", [])

def api_match_stats(match_id: str) -> Dict[str, Any]:
    """拉比赛的 stats（包含 teams/players/stats，能拿到 K/A/D、Score、地图等）"""
    url = f"{FACEIT_API}/matches/{match_id}/stats"
    return _req(url)


# ---------------------------
# 解析比赛 stats -> 我们需要的字段
# ---------------------------
def parse_one_match_for_player(
    match_id: str, player_id: str, player_tz: timezone
) -> Optional[Dict[str, Any]]:
    data = api_match_stats(match_id)

    # 典型结构：rounds: [ { round_stats: {"Map":"de_mirage","Score":"13-10","Winner":"faction1", ...},
    #                      teams: [ { team_stats:{...}, players:[{player_id,nickname,player_stats:{...}}...] }, ... ] } ]
    rounds = data.get("rounds", [])
    if not rounds:
        return None

    r0 = rounds[0]
    round_stats = r0.get("round_stats", {}) or {}
    map_name = round_stats.get("Map") or round_stats.get("Map Name") or "-"
    score_str = round_stats.get("Score") or ""  # e.g. "13-10"
    # Score 有时是 "13 / 10" 或 "13-10"，做个统一
    score_str = score_str.replace(" ", "")
    score_str = score_str.replace("-", "/")
    if "/" not in score_str:
        # 兜底
        score_str = "0/0"
    s_my, s_opp = 0, 0  # 暂且；下面会用 team index 精确判断

    # 球员在哪个队，并拿 stats
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

    # 解析比分
    a, b = score_str.split("/", 1)
    try:
        a1 = int(a)
        b1 = int(b)
    except Exception:
        a1, b1 = 0, 0

    if my_team_idx == 0:
        s_my, s_opp = a1, b1
    else:
        s_my, s_opp = b1, a1
    win = s_my > s_opp

    # K/A/D、ADR、K/R
    kills = _int(my_stats.get("Kills"))
    assists = _int(my_stats.get("Assists"))
    deaths = _int(my_stats.get("Deaths"))
    adr = _num(my_stats.get("ADR"))
    kpr = _num(my_stats.get("K/R Ratio"))
    rating = compute_rating_like(kills, deaths, kpr, adr)

    # Penta Kills（五杀）
    penta = _int(my_stats.get("Penta Kills"))
    is_ace = penta > 0

    # 开始时间（取 rounds[0].round_stats 或 matches history 里会有；这里用 stats 的 "Date" 可能是字符串）
    started_at = None
    dt_str = round_stats.get("Date")  # 不一定存在；多半没有
    if dt_str:
        # 尝试解析；这里很多时候 faceit 不提供
        try:
            started_at = datetime.fromisoformat(dt_str)
        except Exception:
            started_at = None

    # stats 接口常没有 started_at；我们让页面以“-”展示，以免误导
    # 更可靠的 started_at 来自 history；我们在调用者层面注入（见 player_view）
    out = {
        "match_id": match_id,
        "match_url": faceit_match_url(match_id),
        "map": map_name,
        "score": f"{s_my} / {s_opp}",
        "win": win,
        "k": kills,
        "a": assists,
        "d": deaths,
        "is_ace": is_ace,
        "rating": rating if rating is not None else "-",
        "started_at": started_at,  # 可能为 None；稍后替换为 history 的时间
    }
    return out


# ---------------------------
# 路由：主页
# ---------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


# ---------------------------
# 路由：聚合一个玩家 -> { player, profile_url, matches:[...] }
# ---------------------------
@app.get("/player")
def player_view(
    name: str = Query(..., description="玩家昵称"),
    limit: int = Query(5, ge=1, le=20, description="每位玩家条数")
) -> JSONResponse:
    # 1) 基本信息（拿 country 作时区）
    info = api_player_by_nickname(name)
    player_id = info.get("player_id")
    nickname = info.get("nickname") or name
    country = info.get("country")
    if not player_id:
        raise HTTPException(404, f"player not found: {name}")
    player_tz = tz_for_country(country)

    # 2) 历史 match ids
    items = api_player_matches(player_id, limit=limit)

    matches: List[Dict[str, Any]] = []
    # history 里可拿 started_at（秒时间戳）
    index_by_id_ts: Dict[str, int] = {}
    ts_map: Dict[str, int] = {}
    for it in items:
        mid = it.get("match_id")
        st = it.get("started_at") or it.get("finished_at")
        if mid:
            ts_map[mid] = int(st) if st else 0

    # 3) 拉每个 match 的 stats
    for it in items:
        mid = it.get("match_id")
        if not mid:
            continue
        m = parse_one_match_for_player(mid, player_id, player_tz)
        if not m:
            continue

        # 用 history 的 started_at 显示为“选手当地时间”
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
