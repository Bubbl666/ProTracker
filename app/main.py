import os
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")
if not FACEIT_API_KEY:
    # 在 Render 上会用环境变量注入，这里只是防护
    print("[WARN] FACEIT_API_KEY is not set. Set it in your Render service.")

BASE = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

app = FastAPI(title="FACEIT Pro Tracker API", version="0.1.0")

# 允许同源前端访问（Render 同站托管时其实不需要，但保留更安全）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 若要限制，填你部署后的域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def faceit_get(client: httpx.AsyncClient, url: str, params: Optional[Dict[str, Any]] = None):
    """小工具：带鉴权 GET 请求 + 错误处理"""
    if not FACEIT_API_KEY:
        raise HTTPException(status_code=500, detail="FACEIT_API_KEY not configured")
    r = await client.get(url, headers=HEADERS, params=params, timeout=20)
    if r.status_code >= 400:
        try:
            data = r.json()
        except Exception:
            data = {"detail": r.text}
        raise HTTPException(status_code=r.status_code, detail=data)
    return r.json()


@app.get("/players")
async def search_players(query: str = Query(..., min_length=1, max_length=32)):
    """
    通过昵称搜索玩家（精准到首个）。
    返回：{ player_id, nickname }
    """
    async with httpx.AsyncClient() as client:
        # 官方搜索接口
        data = await faceit_get(
            client,
            f"{BASE}/search/players",
            params={"nickname": query, "game": "cs2", "offset": 0, "limit": 1},
        )
        items = data.get("items", [])
        if not items:
            return {"player_id": None, "nickname": query, "found": False}
        p = items[0]
        return {
            "player_id": p.get("player_id"),
            "nickname": p.get("nickname") or query,
            "found": True,
        }


@app.get("/matches/with_stats")
async def matches_with_stats(player_id: str, limit: int = 5):
    """
    获取玩家最近比赛并合并该玩家在每场中的统计。
    返回：list[{match_id, map, region, finished_at, winner, score_f1, score_f2, player_stats:{...}}]
    """
    limit = max(1, min(limit, 20))  # 防止过大
    async with httpx.AsyncClient() as client:
        # 最近历史
        hist = await faceit_get(
            client,
            f"{BASE}/players/{player_id}/history",
            params={"game": "cs2", "offset": 0, "limit": limit},
        )
        items: List[Dict[str, Any]] = hist.get("items", [])
        if not items:
            return []

        # 拉取每场的 stats
        results = []
        for it in items:
            match_id = it.get("match_id")
            if not match_id:
                continue
            try:
                stats = await faceit_get(client, f"{BASE}/matches/{match_id}/stats")
            except HTTPException:
                # 部分比赛可能无权限或已清理，跳过
                continue

            # 从 stats 里找到这个 player 的统计
            player_line = None
            map_name = None
            region = None
            winner = None
            score_f1 = None
            score_f2 = None
            finished_at = it.get("finished_at")

            # 结构：rounds -> teams -> players
            rounds = stats.get("rounds") or []
            if rounds:
                rnd0 = rounds[0]
                map_name = rnd0.get("round_stats", {}).get("Map")
                teams = rnd0.get("teams") or []
                # 比分和胜者
                try:
                    t1, t2 = teams[0], teams[1]
                    score_f1 = t1.get("team_stats", {}).get("Final Score")
                    score_f2 = t2.get("team_stats", {}).get("Final Score")
                    if (t1.get("team_stats", {}).get("Team") == rnd0.get("winner")):
                        winner = "faction1"
                    elif (t2.get("team_stats", {}).get("Team") == rnd0.get("winner")):
                        winner = "faction2"
                except Exception:
                    pass

                # 找玩家
                for t in teams:
                    for pl in t.get("players", []):
                        if pl.get("player_id") == player_id:
                            player_line = {
                                "nickname": pl.get("nickname"),
                                "kills": _safe_int(pl.get("player_stats", {}).get("Kills")),
                                "deaths": _safe_int(pl.get("player_stats", {}).get("Deaths")),
                                "kd_ratio": _safe_float(pl.get("player_stats", {}).get("K/D Ratio")),
                                "hs_percent": _safe_percent(pl.get("player_stats", {}).get("Headshots %")),
                                "adr": _safe_float(pl.get("player_stats", {}).get("ADR")),
                                "triple_kills": _safe_int(pl.get("player_stats", {}).get("Triple Kills")),
                                "quadro_kills": _safe_int(pl.get("player_stats", {}).get("Quadro Kills")),
                                "penta_kills": _safe_int(pl.get("player_stats", {}).get("Penta Kills")),
                            }
                            break

            results.append(
                {
                    "match_id": match_id,
                    "map": map_name,
                    "region": region,
                    "finished_at": finished_at,
                    "winner": winner,
                    "score_f1": score_f1,
                    "score_f2": score_f2,
                    "player_stats": player_line,
                }
            )

        return results


def _safe_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _safe_float(v):
    try:
        return float(str(v).replace(",", ".").strip())
    except Exception:
        return None


def _safe_percent(v):
    try:
        s = str(v).replace("%", "").replace(",", ".").strip()
        return float(s)
    except Exception:
        return None
