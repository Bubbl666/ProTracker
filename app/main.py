# app/main.py
import os
import logging
from typing import List, Dict, Any

import requests
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ------------------------------------------------------------------------------
# 基础配置
# ------------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("protracker")

app = FastAPI(title="ProTracker API", version="0.1.0")

# 挂载静态页面（/ -> static/index.html）
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """首页：返回静态页面"""
    return FileResponse("static/index.html")


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


# ------------------------------------------------------------------------------
# Faceit 直连（使用服务端环境变量中的 API Key）
# ------------------------------------------------------------------------------
FACEIT_KEY = os.getenv("FACEIT_API_KEY", "").strip()
HEADERS = {"Authorization": f"Bearer {FACEIT_KEY}"} if FACEIT_KEY else {}
FACEIT_BASE = "https://open.faceit.com/data/v4"


def require_key():
    if not HEADERS:
        raise HTTPException(status_code=500, detail="FACEIT_API_KEY not set on server")


# ------------------------------------------------------------------------------
# /players  —— 根据昵称取 player_id
# 统一返回: {"player_id": ..., "nickname": ..., "found": bool}
# ------------------------------------------------------------------------------
@app.get("/players")
def get_player(query: str = Query(..., min_length=1)) -> Dict[str, Any]:
    require_key()

    # 先尝试精确匹配
    try:
        r = requests.get(
            f"{FACEIT_BASE}/players",
            params={"nickname": query},
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 200:
            j = r.json()
            return {"player_id": j["player_id"], "nickname": j["nickname"], "found": True}
    except Exception as e:
        logger.warning("Exact lookup failed: %s", e)

    # 再退回模糊搜索
    try:
        r = requests.get(
            f"{FACEIT_BASE}/search/players",
            params={"nickname": query, "offset": 0, "limit": 1},
            headers=HEADERS,
            timeout=15,
        )
        if r.status_code == 200:
            j = r.json()
            items = j.get("items", [])
            if items:
                p = items[0]
                return {"player_id": p["player_id"], "nickname": p["nickname"], "found": True}
    except Exception as e:
        logger.error("Search lookup failed: %s", e)

    return {"player_id": None, "nickname": query, "found": False}


# ------------------------------------------------------------------------------
# /matches/with_stats —— 拉取最近比赛 + 个人统计
# 直接读取 Faceit 官方接口，无需本地采集/数据库
# ------------------------------------------------------------------------------
# --- 替换 app/main.py 里 matches_with_stats 的实现（或直接覆盖整个函数） ---
@app.get("/matches/with_stats")
def matches_with_stats(player_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    require_key()

    limit = max(1, min(limit, 20))
    # 1) 历史列表（CS2）
    hist = requests.get(
        f"{FACEIT_BASE}/players/{player_id}/history",
        params={"game": "cs2", "offset": 0, "limit": limit},
        headers=HEADERS,
        timeout=20,
    )
    if hist.status_code != 200:
        raise HTTPException(status_code=hist.status_code, detail=hist.text)

    hist_json = hist.json()
    items = hist_json.get("items", [])
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
        map_name = None
        score = None
        player_stat: Dict[str, Any] | None = None
        stats_unavailable_reason = None

        # 2) 拉统计
        try:
            stat = requests.get(
                f"{FACEIT_BASE}/matches/{mid}/stats",
                headers=HEADERS,
                timeout=20,
            )
            if stat.status_code == 200:
                s = stat.json()
                rounds = s.get("rounds") or []
                if rounds:
                    # Map / Score 在 round_stats 里
                    rs = rounds[0].get("round_stats") or {}
                    map_name = rs.get("Map") or rs.get("Map Name") or map_name
                    # 比如 "16 / 9" 或 "13 / 11"
                    score = rs.get("Score") or rs.get("Final Score") or score

                    # 找到该玩家
                    players = rounds[0].get("players", [])
                    for pl in players:
                        if pl.get("player_id") == player_id:
                            # 兼容多版本字段
                            kd = pl.get("k/d") or pl.get("K/D Ratio") or pl.get("kd")
                            kr = pl.get("k/r") or pl.get("K/R Ratio") or pl.get("kr")
                            adr = pl.get("adr") or pl.get("ADR")
                            hs  = pl.get("hs") or pl.get("Headshots %") or pl.get("Headshots")

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
                stats_unavailable_reason = "not_ready_or_hidden"
            else:
                stats_unavailable_reason = f"http_{stat.status_code}"
        except requests.RequestException:
            stats_unavailable_reason = "network_error"

        # 3) 如果依然没有 Map/Score，尝试从历史项里兜底（有些历史会带结果）
        if not map_name:
            # 有的历史对象里会塞到 results / teams 的自定义字段，这里做一次兜底解析
            results = it.get("results") or {}
            # 某些房型会把得分塞进 'score' 或自定义里，这里尽力而为
            score = score or results.get("score") or results.get("Score")
            # Map 通常只有 stats 里才有，拿不到就保持 None

        out.append(
            {
                "match_id": mid,
                "game": it.get("game_id"),
                "started_at": it.get("started_at"),
                "finished_at": it.get("finished_at"),
                "map": map_name,
                "score": score,
                "teams": it.get("teams", {}),
                "player": player_stat,              # 可能为 None（无公开统计）
                "stats_unavailable_reason": stats_unavailable_reason,
            }
        )

    return out


# ------------------------------------------------------------------------------
# 全局异常兜底（避免 500 时返回 HTML）
# ------------------------------------------------------------------------------
@app.exception_handler(Exception)
def unhandled_exc(_, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
