# app/main.py
import os
import asyncio
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

APP_TITLE = "ProTracker API"
FACEIT_API_KEY = os.environ.get("FACEIT_API_KEY")  # 在 Render 的 Environment 里配置
FACEIT_API_BASE = "https://open.faceit.com/data/v4"

if not FACEIT_API_KEY:
    # 仍允许应用启动，但在请求时给出明确错误
    pass

HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

app = FastAPI(title=APP_TITLE)

# 允许前端跨域（如果你把网页托管在同一个服务，其实也无所谓）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1) 首页/静态页面：把仓库中的 static/index.html 作为网站首页
#    说明：FastAPI 先匹配已定义的 API 路由，再交给静态文件，因此不会“遮住”下面的 /players 等接口
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.get("/healthz")
def healthz() -> Dict[str, Any]:
    return {"ok": True, "app": APP_TITLE}

# ---- Faceit 封装 ----
async def _get_json(client: httpx.AsyncClient, url: str, params: Optional[dict] = None) -> dict:
    if not FACEIT_API_KEY:
        raise HTTPException(status_code=500, detail="FACEIT_API_KEY is not configured on the server.")
    r = await client.get(url, params=params or {}, headers=HEADERS, timeout=20.0)
    if r.status_code >= 400:
        # 透传 faceit 错误更易排查
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

# 2) 查玩家：/players?query=donk666
@app.get("/players")
async def search_players(query: str = Query(..., min_length=1)):
    async with httpx.AsyncClient(base_url=FACEIT_API_BASE) as client:
        data = await _get_json(client, "/players", params={"nickname": query})
    # 如果 nickname 不存在，官方接口也可能返回 404；上面已抛异常
    return {
        "player_id": data.get("player_id"),
        "nickname": data.get("nickname"),
        "found": bool(data.get("player_id")),
    }

# 3) 查最近比赛（带个人统计）：/matches/with_stats?player_id=...&limit=5&game=cs2
@app.get("/matches/with_stats")
async def matches_with_stats(
    player_id: str,
    limit: int = 5,
    game: str = "cs2",
):
    limit = max(1, min(limit, 20))  # 防止一次拉太多
    async with httpx.AsyncClient(base_url=FACEIT_API_BASE) as client:
        # 先拿比赛列表
        history = await _get_json(
            client,
            f"/players/{player_id}/history",
            params={"game": game, "size": limit},
        )
        items = history.get("items", [])

        async def one(match: dict):
            mid = match.get("match_id")
            if not mid:
                return None
            # 获取比赛统计，定位到该玩家的统计项
            stats = await _get_json(client, f"/matches/{mid}/stats")
            pstat = None
            try:
                rounds = stats.get("rounds", [])
                if rounds:
                    for team in rounds[0]["teams"]:
                        for p in team.get("players", []):
                            if p.get("player_id") == player_id:
                                s = p.get("player_stats", {})
                                # 字段可能为字符串，这里做一次安全转换
                                def num(v, t=float):
                                    if v is None:
                                        return None
                                    v = str(v).replace("%", "")
                                    try:
                                        return t(v)
                                    except Exception:
                                        return None
                                pstat = {
                                    "nickname": p.get("nickname"),
                                    "kills": num(s.get("Kills"), int),
                                    "deaths": num(s.get("Deaths"), int),
                                    "assists": num(s.get("Assists"), int),
                                    "kdratio": num(s.get("K/D Ratio")),
                                    "kratio": num(s.get("K/R Ratio")),
                                    "hs_percent": num(s.get("Headshots %")),
                                    "result": team.get("team_stats", {}).get("Final Score"),
                                }
                                raise StopIteration
            except StopIteration:
                pass

            return {
                "match_id": mid,
                "game": match.get("game_id", game),
                "played_at": match.get("started_at"),
                "stats": pstat,
            }

        results = [r for r in await asyncio.gather(*(one(m) for m in items)) if r]
    return results
