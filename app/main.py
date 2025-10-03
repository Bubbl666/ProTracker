from __future__ import annotations

import os
import time
import json
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# -----------------------------
# 基础配置
# -----------------------------
load_dotenv()
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_API_BASE = "https://open.faceit.com/data/v4"

app = FastAPI(title="ProTracker API", version="0.1.1")

# 允许前端直接调用（你的网站/本地都能访问）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# 简易内存缓存（避免频繁打 Faceit）
# -----------------------------
CACHE: Dict[str, Tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 60  # 可按需调

def cache_get(key: str) -> Optional[Any]:
    item = CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > CACHE_TTL_SECONDS:
        CACHE.pop(key, None)
        return None
    return value

def cache_set(key: str, value: Any) -> None:
    CACHE[key] = (time.time(), value)

# -----------------------------
# Faceit API 请求助手
# -----------------------------
def faceit_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not FACEIT_API_KEY:
        raise RuntimeError("FACEIT_API_KEY is empty. Set it in Render Environment.")

    url = f"{FACEIT_API_BASE}{path}"
    cache_key = f"GET::{url}::{json.dumps(params or {}, sort_keys=True)}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    headers = {
        "Authorization": f"Bearer {FACEIT_API_KEY}",
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, params=params or {}, timeout=15)
    if resp.status_code >= 400:
        raise RuntimeError(f"Faceit API {path} failed: {resp.status_code} {resp.text}")

    data = resp.json()
    cache_set(cache_key, data)
    return data

# -----------------------------
# 业务函数
# -----------------------------
def find_player_by_nick(nick: str) -> Optional[Dict[str, Any]]:
    """
    返回:
      {
        "player_id": "...",
        "nickname": "...",
        "found": True
      }
      或 None
    """
    if not nick:
        return None
    data = faceit_get("/players", params={"nickname": nick})
    player_id = data.get("player_id")
    nickname = data.get("nickname")
    if player_id and nickname:
        return {"player_id": player_id, "nickname": nickname, "found": True}
    return None

def get_player_matches_with_stats(player_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    拉取最近对局 + 合并统计数据
    """
    hist = faceit_get(f"/players/{player_id}/history", params={"game": "cs2", "limit": limit})
    items = hist.get("items", [])
    results: List[Dict[str, Any]] = []

    for it in items:
        match_id = it.get("match_id")
        if not match_id:
            continue

        # 基础信息
        try:
            match_detail = faceit_get(f"/matches/{match_id}")
        except Exception:
            match_detail = {}

        # 统计信息
        try:
            stats = faceit_get(f"/stats/matches/{match_id}")
        except Exception:
            stats = {}

        # 整理队伍
        teams_block = match_detail.get("teams", {})
        # 兼容两种结构（有些返回 { "faction1": {...}, "faction2": {...} }）
        factions: List[Dict[str, Any]] = []
        if isinstance(teams_block, dict):
            # 可能是 faction1/faction2
            if "faction1" in teams_block or "faction2" in teams_block:
                for key in ("faction1", "faction2"):
                    t = teams_block.get(key)
                    if not t:
                        continue
                    factions.append({
                        "team_id": t.get("team_id"),
                        "nickname": t.get("name") or t.get("nickname") or "",
                        "avatar": t.get("avatar") or "",
                        "type": t.get("type") or "",
                        "players": [
                            {
                                "player_id": p.get("player_id"),
                                "nickname": p.get("nickname"),
                                "avatar": p.get("avatar") or "",
                                "skill_level": p.get("skill_level"),
                                "game_player_id": p.get("game_player_id"),
                                "game_player_name": p.get("game_player_name"),
                                "faceit_url": p.get("faceit_url"),
                            }
                            for p in (t.get("roster") or t.get("players") or [])
                            if isinstance(p, dict)
                        ],
                    })
            else:
                # 也可能是列表 teams
                teams_list = teams_block.get("teams")
                if isinstance(teams_list, list):
                    for t in teams_list:
                        factions.append({
                            "team_id": t.get("team_id"),
                            "nickname": t.get("name") or t.get("nickname") or "",
                            "avatar": t.get("avatar") or "",
                            "type": t.get("type") or "",
                            "players": [
                                {
                                    "player_id": p.get("player_id"),
                                    "nickname": p.get("nickname"),
                                    "avatar": p.get("avatar") or "",
                                    "skill_level": p.get("skill_level"),
                                    "game_player_id": p.get("game_player_id"),
                                    "game_player_name": p.get("game_player_name"),
                                    "faceit_url": p.get("faceit_url"),
                                }
                                for p in (t.get("roster") or t.get("players") or [])
                                if isinstance(p, dict)
                            ],
                        })

        # 提取该玩家在本场的个人统计
        player_stat: Optional[Dict[str, Any]] = None
        try:
            rounds = stats.get("rounds") or []
            if rounds:
                # 一般只有 1 个 map 统计，取第一个
                r0 = rounds[0]
                teams_stats = r0.get("teams") or []
                for ts in teams_stats:
                    for mp in ts.get("players", []):
                        if (mp.get("player_id") == player_id) or (mp.get("nickname") == it.get("nickname")):
                            s = mp.get("player_stats", {})
                            # 常见字段做一层标准化
                            def to_num(x: Any) -> Optional[float]:
                                try:
                                    return float(x)
                                except Exception:
                                    return None
                            player_stat = {
                                "nickname": mp.get("nickname"),
                                "kills": to_num(s.get("Kills")),
                                "deaths": to_num(s.get("Deaths")),
                                "assists": to_num(s.get("Assists")),
                                "adr": to_num(s.get("ADR")),
                                "hs": to_num(s.get("Headshots %")),
                                "kd": to_num(s.get("K/D Ratio")),
                                "kr": to_num(s.get("K/R Ratio")),
                                "raw": s,  # 原始字段也带上，前端想看就能看
                            }
                            break
                    if player_stat:
                        break
        except Exception:
            player_stat = None  # 忽略统计失败

        results.append({
            "match_id": match_id,
            "game": it.get("game"),
            "started_at": it.get("started_at"),
            "finished_at": it.get("finished_at"),
            "map": (it.get("i18n") or {}).get("map") or it.get("map"),
            "score": it.get("i18n", {}).get("score") or it.get("score"),
            "teams": factions,
            "player": player_stat,
            "stats_unavailable_reason": None if player_stat else "stats_not_found_or_private",
        })

    return results

# -----------------------------
# 路由
# -----------------------------
@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"

@app.get("/players")
def api_players(query: str = Query(..., min_length=1)) -> JSONResponse:
    try:
        info = find_player_by_nick(query)
        if not info:
            return JSONResponse({"found": False, "query": query}, status_code=404)
        return JSONResponse(info)
    except Exception as e:
        return JSONResponse({"error": str(e), "found": False, "query": query}, status_code=500)

@app.get("/matches/with_stats")
def api_matches_with_stats(
    player_id: str = Query(..., min_length=10),
    limit: int = Query(5, ge=1, le=20),
) -> JSONResponse:
    try:
        data = get_player_matches_with_stats(player_id, limit=limit)
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e), "player_id": player_id}, status_code=500)

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    """
    优先返回 static/index.html；没有的话返回一个简单页。
    """
    index_path = os.path.join(os.path.dirname(__file__), "..", "static", "index.html")
    index_path = os.path.abspath(index_path)
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read(), status_code=200)
    # 兜底
    html = """
    <!doctype html>
    <html lang="en"><head><meta charset="utf-8"><title>ProTracker</title></head>
    <body>
      <h1>ProTracker API</h1>
      <p>Service is up. Try:</p>
      <ul>
        <li><code>/players?query=donk666</code></li>
        <li><code>/matches/with_stats?player_id=&lt;id&gt;&amp;limit=5</code></li>
      </ul>
    </body></html>
    """
    return HTMLResponse(html, status_code=200)

# 也可把静态文件目录挂到 /static（可选）
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
