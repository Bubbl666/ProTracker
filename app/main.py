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
@app.get("/matches/with_stats")
def matches_with_stats(player_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    require_key()

    limit = max(1, min(limit, 20))  # Faceit 接口通常不建议太大
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

    out: List[Dict[str, Any]] = []

    for it in items:
        mid = it.get("match_id")
        # 2) 每场统计（含玩家 K/D 等）
        try:
            stat = requests.get(
                f"{FACEIT_BASE}/matches/{mid}/stats",
                headers=HEADERS,
                timeout=20,
            )
        except Exception as e:
            logger.warning("stats request error for %s: %s", mid, e)
            stat = None

        player_stat: Dict[str, Any] | None = None

        if stat and stat.status_code == 200:
            try:
                s = stat.json()
                rounds = s.get("rounds") or []
                if rounds:
                    players = rounds[0].get("players", [])
                    for pl in players:
                        if pl.get("player_id") == player_id:
                            # 统一一些字段名；不同比赛格式里键名可能略有差异
                            def _to_float(v):
                                try:
                                    return float(v)
                                except Exception:
                                    return 0.0

                            def _to_int(v):
                                try:
                                    return int(v)
                                except Exception:
                                    return 0

                            player_stat = {
                                "nickname": pl.get("nickname"),
                                "kills": _to_int(pl.get("kills")),
                                "deaths": _to_int(pl.get("deaths")),
                                "assists": _to_int(pl.get("assists")),
                                "hs": _to_int(pl.get("hs")),  # 爆头数
                                "kd": _to_float(pl.get("k/d") or pl.get("kd")),
                                "kr": _to_float(pl.get("k/r")),
                                "adr": _to_float(pl.get("adr")),
                                "result": pl.get("result"),
                                "team": pl.get("team"),
                            }
                            break
            except Exception as e:
                logger.warning("parse stats error for %s: %s", mid, e)

        out.append(
            {
                "match_id": mid,
                "game": it.get("game_id"),
                "started_at": it.get("started_at"),
                "finished_at": it.get("finished_at"),
                "results": it.get("results", {}),
                "teams": it.get("teams", {}),
                "player": player_stat,  # 可能为 None（接口缺失或解析失败）
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
