from __future__ import annotations

from pathlib import Path
from typing import Optional

import os
import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------
# 基本配置
# ---------------------------------------------------------------------
app = FastAPI(title="ProTracker API", version="0.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 目录定位：main.py 位于 app/ 下，static 在仓库根目录
ROOT_DIR = Path(__file__).resolve().parents[1]      # 仓库根
STATIC_DIR = ROOT_DIR / "static"                    # static 目录
INDEX_FILE = STATIC_DIR / "index.html"              # 首页路径

# 挂载静态资源（可选，但建议）
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Faceit API
BASE_URL = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY")


def faceit_get(endpoint: str, params: Optional[dict] = None):
    """简化的 Faceit GET 包装。返回 dict 或 None。"""
    headers = {"Authorization": f"Bearer {API_KEY}"}
    url = f"{BASE_URL}{endpoint}"
    resp = requests.get(url, headers=headers, params=params, timeout=20)
    if resp.status_code != 200:
        print(f"❌ Faceit API error {resp.status_code}: {resp.text}")
        return None
    try:
        return resp.json()
    except Exception:
        return None


# ---------------------------------------------------------------------
# Web 页面路由
# ---------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def serve_index():
    """根路径直接返回前端页面。"""
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))
    # 找不到就给出清晰提示
    return HTMLResponse(
        "<h3>static/index.html 未找到</h3>"
        "<p>请确认仓库根目录存在 <code>static/index.html</code>，"
        "并已推送到 Render 后再访问。</p>",
        status_code=500,
    )


# 可保留一个健康检查（给你自己或 Render 用）
@app.get("/api/health")
def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


# ---------------------------------------------------------------------
# API：Players / Matches / Matches with Stats
# ---------------------------------------------------------------------
@app.get("/players")
def get_player(query: str = Query(..., description="玩家 nickname 或 id")):
    data = faceit_get("/players", {"nickname": query})
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

        if detail and "rounds" in detail and detail["rounds"]:
            round_info = detail["rounds"][0]
            teams = []
            for team in round_info.get("teams", []):
                players = []
                for p in team.get("players", []):
                    players.append(
                        {
                            "player_id": p.get("player_id"),
                            "nickname": p.get("nickname"),
                            "avatar": p.get("avatar"),
                            "stats": p.get("player_stats", {}),  # 详细统计
                        }
                    )
                teams.append(
                    {
                        "team_id": team.get("team_id"),
                        "nickname": team.get("nickname"),
                        "players": players,
                    }
                )
            match_info["teams"] = teams

            # 找到目标玩家的统计
            for team in teams:
                for p in team["players"]:
                    if p["player_id"] == player_id:
                        match_info["player"] = {
                            "nickname": p["nickname"],
                            "stats": p.get("stats", {}),
                        }
                        break

        results.append(match_info)

    return results
