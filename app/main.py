from __future__ import annotations

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import requests
import os
from typing import Optional

app = FastAPI(title="ProTracker API", version="0.1.1")

# 允许前端调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY")


def faceit_get(endpoint: str, params: Optional[dict] = None):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=headers, params=params)
    if resp.status_code != 200:
        print(f"❌ Faceit API error {resp.status_code}: {resp.text}")
        return None
    return resp.json()


@app.get("/")
def root():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/players")
def get_player(query: str = Query(..., description="玩家 nickname 或 id")):
    data = faceit_get(f"/players", {"nickname": query})
    if not data or "player_id" not in data:
        return {"found": False, "nickname": query}
    return {
        "player_id": data["player_id"],
        "nickname": data["nickname"],
        "found": True,
    }


@app.get("/matches")
def get_matches(player_id: str, size: int = 5):
    matches = faceit_get(f"/players/{player_id}/history", {"game": "cs2", "limit": size})
    if not matches or "items" not in matches:
        return []
    return [
        {
            "match_id": m["match_id"],
            "game": m.get("game_id"),
            "started_at": m.get("started_at"),
            "finished_at": m.get("finished_at"),
            "map": m.get("map"),
            "score": m.get("results", {}).get("score"),
        }
        for m in matches["items"]
    ]


@app.get("/matches/with_stats")
def get_matches_with_stats(player_id: str, size: int = 5):
    matches = faceit_get(f"/players/{player_id}/history", {"game": "cs2", "limit": size})
    if not matches or "items" not in matches:
        return []

    results = []
    for m in matches["items"]:
        match_id = m["match_id"]
        detail = faceit_get(f"/matches/{match_id}/stats")

        match_info = {
            "match_id": match_id,
            "game": m.get("game_id"),
            "started_at": m.get("started_at"),
            "finished_at": m.get("finished_at"),
            "map": m.get("map"),
            "score": m.get("results", {}).get("score"),
            "teams": [],
            "player": None,
        }

        if detail and "rounds" in detail:
            round_info = detail["rounds"][0]
            teams = []
            for team in round_info["teams"]:
                teams.append({
                    "team_id": team["team_id"],
                    "nickname": team["nickname"],
                    "players": [
                        {
                            "player_id": p["player_id"],
                            "nickname": p["nickname"],
                            "avatar": p["avatar"],
                            "stats": p.get("player_stats", {}),
                        }
                        for p in team["players"]
                    ],
                })
            match_info["teams"] = teams

            # 找目标玩家数据
            for team in teams:
                for p in team["players"]:
                    if p["player_id"] == player_id:
                        match_info["player"] = {
                            "nickname": p["nickname"],
                            "stats": p.get("stats", {}),
                        }

        results.append(match_info)

    return results
