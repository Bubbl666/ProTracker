# -*- coding: utf-8 -*-
from __future__ import annotations
import os
from typing import Any, Dict, List, Tuple
import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_API_URL = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

app = FastAPI(title="ProTracker", version="0.1.1")
templates = Jinja2Templates(directory="app/templates")

# ---------- utils ----------
def room_url(game: str, match_id: str) -> str:
    game_slug = (game or "cs2").lower()
    return f"https://www.faceit.com/en/{game_slug}/room/{match_id}"

def player_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{(nickname or '').strip()}" if nickname else "#"

def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if isinstance(v, (int, float)): return float(v)
        return float(str(v).replace("%","").strip())
    except Exception:
        return default

def rating_hltv2_approx(player_stats: Dict[str, Any]):
    kills = to_float(player_stats.get("Kills", 0))
    deaths = to_float(player_stats.get("Deaths", 0))
    kr = to_float(player_stats.get("K/R Ratio", player_stats.get("K/R", 0)))
    adr = to_float(player_stats.get("ADR", 0))
    if kr <= 0 or kills <= 0: return None
    rounds_est = max(kills/kr, deaths, 1.0)
    dpr = min(max(deaths/rounds_est, 0.0), 1.5)
    parts = [kr/0.679, (1.0-dpr)/(1.0-0.317), adr/85.0]
    rating = sum(parts)/3.0
    rating = min(max(rating, 0.1), 2.5)
    return round(rating, 2)

def score_color_and_win(result_val: Any) -> Tuple[str, bool]:
    win = str(result_val).strip() == "1"
    return ("score-win" if win else "score-lose", win)

def fetch_player(nickname_or_id: str) -> Dict[str, Any]:
    if not HEADERS: return {}
    # 先按 id
    r = requests.get(f"{FACEIT_API_URL}/players/{nickname_or_id}", headers=HEADERS, timeout=20)
    if r.status_code == 200: return r.json()
    # 再按昵称
    r = requests.get(f"{FACEIT_API_URL}/players?nickname={nickname_or_id}", headers=HEADERS, timeout=20)
    return r.json() if r.status_code == 200 else {}

def fetch_recent_matches_with_stats(player_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    base = os.getenv("SELF_BASE_URL", "").rstrip("/")
    if base:
        try:
            r = requests.get(f"{base}/matches/with_stats?player_id={player_id}&limit={limit}", timeout=30)
            if r.ok: return r.json()
        except Exception:
            pass
    return []

def bake_matches(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    baked = []
    for m in matches:
        g = m.get("game", "cs2")
        m["room_url"] = room_url(g, m.get("match_id",""))
        pstats = (m.get("player") or {}).get("stats") or {}
        m["rating_approx"] = rating_hltv2_approx(pstats) if pstats else None
        m["score_css"], m["won"] = score_color_and_win(pstats.get("Result",""))
        baked.append(m)
    return baked

# ---------- UI ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request): return templates.TemplateResponse("index.html", {"request": request})

@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request, player: str = Query("donk666"), limit: int = Query(5)):
    return templates.TemplateResponse("index.html", {"request": request, "player": player, "limit": limit})

@app.get("/service")
def service_ok(): return {"ok": True, "service": "protracker", "version": "0.1.1"}

# 你已有的聚合接口（占位，保持兼容）
@app.get("/matches/with_stats")
def passthrough_matches_with_stats(player_id: str, limit: int = 5):
    return JSONResponse([])

# ---------- Single (兼容旧前端) ----------
@app.get("/page-data", response_class=JSONResponse)
def page_data(nickname: str = Query("donk666"), limit: int = Query(5)):
    player = fetch_player(nickname)
    player_id = player.get("player_id") or player.get("id") or ""
    faceit_nick = player.get("nickname") or nickname
    return {
        "player": {"nickname": faceit_nick, "profile_url": player_url(faceit_nick), "player_id": player_id},
        "matches": bake_matches(fetch_recent_matches_with_stats(player_id, limit)),
        "limit": limit,
    }

# ---------- Multi (新加) ----------
@app.get("/page-data-multi", response_class=JSONResponse)
def page_data_multi(nicknames: str = Query("donk666"), limit: int = Query(5)):
    # 支持逗号/空格分隔；最多查 10 个，避免被 API 限速
    raw = [x.strip() for x in nicknames.replace("，",",").replace("\n",",").split(",")]
    names = [n for n in raw if n][:10]
    out = []
    for name in names:
        player = fetch_player(name)
        player_id = player.get("player_id") or player.get("id") or ""
        faceit_nick = player.get("nickname") or name
        matches = bake_matches(fetch_recent_matches_with_stats(player_id, limit))
        out.append({
            "player": {"nickname": faceit_nick, "profile_url": player_url(faceit_nick), "player_id": player_id},
            "matches": matches,
            "limit": limit,
        })
    return {"items": out, "count": len(out)}
