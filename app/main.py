# app/main.py
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
import os
import requests
from dotenv import load_dotenv

# ----- 初始化 & 配置 -----
load_dotenv()

app = FastAPI(title="FACEIT Pro Tracker API", version="0.1.1")

# 允许前端访问（可按需收窄域名）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_BASE = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

def require_key():
    if not FACEIT_API_KEY:
        raise HTTPException(status_code=500, detail="FACEIT_API_KEY missing on server")

# ----- 根路由 -----
@app.get("/")
def root():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}

# ----- 查询玩家 -----
@app.get("/players")
def players(query: str):
    """
    通过昵称模糊查询并返回最匹配的玩家（只取第一个）。
    """
    require_key()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")

    try:
        r = requests.get(
            f"{FACEIT_BASE}/search/players",
            params={"nickname": query, "limit": 1, "offset": 0},
            headers=HEADERS,
            timeout=15,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"network_error: {e}")

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text)

    data = r.json()
    items = data.get("items", [])
    if not items:
        return {"found": False, "query": query}

    p = items[0]
    return {
        "player_id": p.get("player_id"),
        "nickname": p.get("nickname"),
        "found": True,
    }

# ----- 最近比赛 + 统计（健壮解析版） -----
@app.get("/matches/with_stats")
def matches_with_stats(player_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    """
    返回最近 N 场比赛的 match_id、map、score，以及该玩家的个人数据（若可用）。
    """
    require_key()
    limit = max(1, min(int(limit), 20))

    # 1) 历史列表（默认按 CS2）
    try:
        hist = requests.get(
            f"{FACEIT_BASE}/players/{player_id}/history",
            params={"game": "cs2", "offset": 0, "limit": limit},
            headers=HEADERS,
            timeout=20,
        )
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"network_error: {e}")

    if hist.status_code != 200:
        raise HTTPException(status_code=hist.status_code, detail=hist.text)

    items = (hist.json() or {}).get("items", [])
    if not items:
        return []

    def _to_int(v):
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return 0

    def _to_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    out: List[Dict[str, Any]] = []

    for it in items:
        mid = it.get("match_id")
        map_name: Optional[str] = None
        score: Optional[str] = None
        player_stat: Optional[Dict[str, Any]] = None
        stats_unavailable_reason: Optional[str] = None

        # 2) 拉统计
        try:
            stat = requests.get(
                f"{FACEIT_BASE}/matches/{mid}/stats",
                headers=HEADERS,
                timeout=20,
            )
            if stat.status_code == 200:
                s = stat.json() or {}
                rounds = s.get("rounds") or []
                if rounds:
                    rs = rounds[0].get("round_stats") or {}
                    # Map / Score 一般在 round_stats
                    map_name = rs.get("Map") or rs.get("Map Name") or map_name
                    score = rs.get("Score") or rs.get("Final Score") or score

                    players = rounds[0].get("players", [])
                    for pl in players:
                        if pl.get("player_id") == player_id:
                            kd = pl.get("k/d") or pl.get("K/D Ratio") or pl.get("kd")
                            kr = pl.get("k/r") or pl.get("K/R Ratio") or pl.get("kr")
                            adr = pl.get("adr") or pl.get("ADR")
                            hs = pl.get("hs") or pl.get("Headshots %") or pl.get("Headshots")

                            player_stat = {
                                "nickname": pl.get("nickname"),
                                "kills": _to_int(pl.get("kills") or pl.get("Kills")),
                                "deaths": _to_int(pl.get("deaths") or pl.get("Deaths")),
                                "assists": _to_int(pl.get("assists") or pl.get("Assists")),
                                "hs": _to_int(hs),
                                "kd": _to_float(kd),
                                "kr": _to_float(kr),
                                "adr": _to_float(adr),
                                "result": pl.get("result") or pl.get("Result"),
                                "team": pl.get("team"),
                            }
                            break
            elif stat.status_code in (403, 404):
                # 统计尚未生成 / 房间不对外公开
                stats_unavailable_reason = "not_ready_or_hidden"
            else:
                stats_unavailable_reason = f"http_{stat.status_code}"
        except requests.RequestException:
            stats_unavailable_reason = "network_error"

        # 3) 如果仍没有 Map/Score，尝试从历史对象兜底（极少数房型会塞在 results 里）
        if not map_name:
            results = it.get("results") or {}
            score = score or results.get("score") or results.get("Score")

        out.append(
            {
                "match_id": mid,
                "game": it.get("game_id"),
                "started_at": it.get("started_at"),
                "finished_at": it.get("finished_at"),
                "map": map_name,
                "score": score,
                "teams": it.get("teams", {}),
                "player": player_stat,                      # 可能为 None
                "stats_unavailable_reason": stats_unavailable_reason,
            }
        )

    return out
