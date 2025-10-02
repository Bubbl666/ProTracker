# app/main.py
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import os
import requests

app = FastAPI(title="Faceit Pro Tracker API")

# ---------- 前端静态 ----------
# 把静态目录挂在 /static，避免覆盖 /players 等 API 路由
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 根路径返回前端页面
@app.get("/", include_in_schema=False)
def home():
    index_path = os.path.join(STATIC_DIR, "index.html")
    return FileResponse(index_path)

# ---------- API 示例（可用即可） ----------
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_BASE = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

def _faceit_get(url, params=None):
    if not FACEIT_API_KEY:
        # 没配 key 也要有可读的错误提示
        return None, {"error": "FACEIT_API_KEY is not set in environment variables."}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        if r.status_code >= 400:
            return None, {"error": f"Faceit API {r.status_code}", "detail": r.text}
        return r.json(), None
    except Exception as e:
        return None, {"error": "request_failed", "detail": str(e)}

@app.get("/players")
def search_player(query: str = Query(..., description="Faceit 昵称")):
    """
    返回形如：
    {
      "player_id": "...",
      "nickname": "donk666",
      "found": true
    }
    """
    # 用官方搜索接口拿第一个结果
    url = f"{FACEIT_BASE}/search/players"
    data, err = _faceit_get(url, params={"nickname": query, "game": "cs2", "limit": 1})
    if err:
        return JSONResponse(err, status_code=400)

    items = (data or {}).get("items", [])
    if not items:
        return {"found": False}

    p = items[0]
    return {
        "player_id": p.get("id") or p.get("player_id"),
        "nickname": p.get("nickname") or p.get("name") or query,
        "found": True,
    }

@app.get("/matches/with_stats")
def matches_with_stats(player_id: str, limit: int = 10):
    """
    简化实现：先返回最近对战历史；（需要更详细统计可再按 match_id 去 /matches/{id}/stats 拉）
    """
    url = f"{FACEIT_BASE}/players/{player_id}/history"
    data, err = _faceit_get(url, params={"game": "cs2", "limit": limit})
    if err:
        return JSONResponse(err, status_code=400)
    return data or []
