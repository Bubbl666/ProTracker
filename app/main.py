from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
import requests
import os
from datetime import datetime
import pytz

FACEIT_API = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY", "")

HEADERS = {"Authorization": f"Bearer {API_KEY}"}

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态资源与模板
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def fetch_player_id(nickname: str) -> str | None:
    url = f"{FACEIT_API}/players?nickname={nickname}"
    r = requests.get(url, headers=HEADERS)
    if r.status_code == 200:
        return r.json().get("player_id")
    return None


def fetch_matches(player_id: str, limit: int = 5):
    url = f"{FACEIT_API}/players/{player_id}/history?game=cs2&limit={limit}"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        return []
    return r.json().get("items", [])


def fetch_match_stats(match_id: str, player_id: str):
    url = f"{FACEIT_API}/matches/{match_id}/stats"
    r = requests.get(url, headers=HEADERS)
    if r.status_code != 200:
        return None

    data = r.json()
    rounds = data.get("rounds", [])
    if not rounds:
        return None

    round_info = rounds[0]  # 单地图模式
    teams = round_info.get("teams", [])

    player_stats = {}
    score_a, score_b = 0, 0
    winner = None

    for t in teams:
        if "score" in t:
            if score_a == 0:
                score_a = int(t["score"])
            else:
                score_b = int(t["score"])
        if t.get("team_stats", {}).get("Team") == round_info.get("round_stats", {}).get("Winner"):
            winner = t["team_id"]

        for p in t.get("players", []):
            if p["player_id"] == player_id:
                stats = p["player_stats"]
                player_stats = {
                    "kills": int(stats.get("Kills", 0)),
                    "deaths": int(stats.get("Deaths", 0)),
                    "assists": int(stats.get("Assists", 0)),
                    "adr": float(stats.get("ADR", 0)),
                    "hs": int(stats.get("Headshots %", 0)),
                    "kd": round(float(stats.get("K/D Ratio", 0)), 2),
                    "kr": round(float(stats.get("K/R Ratio", 0)), 2),
                }

    return {
        "score": f"{score_a} / {score_b}",
        "win": (score_a > score_b) if player_stats else None,
        **player_stats,
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/player")
async def player_matches(name: str, limit: int = 5):
    player_id = fetch_player_id(name)
    if not player_id:
        return {"error": f"Player {name} not found"}

    matches = fetch_matches(player_id, limit)
    results = []
    for m in matches:
        match_id = m["match_id"]
        stats = fetch_match_stats(match_id, player_id)

        # 转换时间为玩家本地时区
        started_at = m.get("started_at")
        dt_str = ""
        if started_at:
            dt = datetime.fromtimestamp(started_at, pytz.timezone("Europe/Moscow"))  # 可改时区
            dt_str = dt.strftime("%m/%d/%Y, %I:%M %p")

        results.append({
            "match_id": match_id,
            "map": m.get("game_map"),
            "date": dt_str,
            **(stats or {}),
        })

    return {"player": name, "matches": results}


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")
