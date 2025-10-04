from __future__ import annotations

from pathlib import Path
from typing import Optional

import os
import requests
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="ProTracker API", version="0.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 路径
ROOT_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Faceit
BASE_URL = "https://open.faceit.com/data/v4"
API_KEY = os.getenv("FACEIT_API_KEY")


def faceit_get(endpoint: str, params: Optional[dict] = None):
    headers = {"Authorization": f"Bearer {API_KEY}"}
    url = f"{BASE_URL}{endpoint}"
    r = requests.get(url, headers=headers, params=params, timeout=25)
    if r.status_code != 200:
        print(f"[Faceit {r.status_code}] {endpoint} :: {r.text[:300]}")
        return None
    try:
        return r.json()
    except Exception:
        return None


# ---------- Web ----------
@app.get("/", response_class=HTMLResponse)
def serve_index():
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE))
    return HTMLResponse(
        "<h3>static/index.html 未找到</h3>"
        "<p>请把前端放到 <code>static/</code> 目录并重新部署。</p>",
        status_code=500,
    )


@app.get("/api/health")
def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


# ---------- API ----------
@app.get("/players")
def get_player(query: str = Query(..., description="玩家 nickname 或 id")):
    data = faceit_get("/players", {"nickname": query})
    if not data or "player_id" not in data:
        return {"found": False, "nickname": query}
    return {"player_id": data["player_id"], "nickname": data["nickname"], "found": True}


@app.get("/matches")
def get_matches(player_id: str, size: int = 5):
    history = faceit_get(f"/players/{player_id}/history", {"game": "cs2", "limit": size})
    if not history or "items" not in history:
        return []
    out = []
    for m in history["items"]:
        out.append(
            {
                "match_id": m["match_id"],
                "game": m.get("game_id"),
                "started_at": m.get("started_at"),
                "finished_at": m.get("finished_at"),
                "map": (m.get("map") or m.get("voting", {}).get("map")) or None,
                "score": m.get("results", {}).get("score"),
            }
        )
    return out


@app.get("/matches/with_stats")
def get_matches_with_stats(
    player_id: str,
    size: Optional[int] = Query(None, description="每页条数（旧参数名）"),
    limit: Optional[int] = Query(None, description="每页条数（新参数名）"),
):
    lim = limit or size or 5
    history = faceit_get(f"/players/{player_id}/history", {"game": "cs2", "limit": lim})
    if not history or "items" not in history:
        return []

    results = []
    for m in history["items"]:
        match_id = m["match_id"]
        detail = faceit_get(f"/matches/{match_id}/stats")

        # 默认值先填历史里的字段
        info = {
            "match_id": match_id,
            "game": m.get("game_id") or "cs2",
            "started_at": m.get("started_at"),
            "finished_at": m.get("finished_at"),
            "map": (m.get("map") or m.get("voting", {}).get("map")) or None,
            "score": m.get("results", {}).get("score"),
            "teams": [],
            "player": None,
        }

        if detail and detail.get("rounds"):
            rnd = detail["rounds"][0]

            # 从 round_stats 把地图/比分补齐
            rstats = rnd.get("round_stats", {}) or {}
            info["map"] = info["map"] or rstats.get("Map") or rstats.get("Map Name")
            score_text = rstats.get("Score")
            if isinstance(score_text, str) and "/" in score_text:
                # 形如 "13 / 10"
                info["score"] = score_text

            # 组队 + 玩家
            teams_out = []
            flat_player = None
            for t in rnd.get("teams", []):
                players_out = []
                for p in t.get("players", []):
                    pstats = p.get("player_stats", {}) or {}
                    players_out.append(
                        {
                            "player_id": p.get("player_id"),
                            "nickname": p.get("nickname"),
                            "avatar": p.get("avatar"),
                            "stats": pstats,
                        }
                    )

                    # 扁平化目标玩家关键指标
                    if p.get("player_id") == player_id:
                        def _num(key: str, cast=float, default=None):
                            v = pstats.get(key)
                            if v is None:
                                return default
                            try:
                                return cast(v)
                            except Exception:
                                try:
                                    return cast(str(v).replace("%", ""))
                                except Exception:
                                    return default

                        flat_player = {
                            "nickname": p.get("nickname"),
                            "kills": _num("Kills", int, 0),
                            "deaths": _num("Deaths", int, 0),
                            "assists": _num("Assists", int, 0),
                            "adr": _num("ADR", float, 0.0),
                            "hs": _num("Headshots %", float, 0.0),
                            "kd": _num("K/D Ratio", float, 0.0),
                            "kr": _num("K/R Ratio", float, 0.0),
                            "raw": pstats,  # 保留原始
                        }

                teams_out.append(
                    {
                        "team_id": t.get("team_id"),
                        "nickname": t.get("nickname"),
                        "players": players_out,
                    }
                )

            info["teams"] = teams_out
            if flat_player:
                info["player"] = flat_player

        results.append(info)

    return results
