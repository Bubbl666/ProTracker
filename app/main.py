from __future__ import annotations
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates
import requests
import os

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态资源与模板
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# 首页
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})

# 健康检查
@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}

@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")


# ================= 数据接口 =================

# 玩家信息
@app.get("/player")
async def get_player(name: str):
    # 这里调用 Faceit API 拿 player_id
    url = f"https://open.faceit.com/data/v4/players?nickname={name}"
    headers = {"Authorization": f"Bearer {os.getenv('FACEIT_API_KEY')}"}
    resp = requests.get(url, headers=headers)
    data = resp.json()
    return {
        "player_id": data.get("player_id"),
        "nickname": data.get("nickname"),
        "faceit_url": f"https://www.faceit.com/en/players/{name}"
    }

# 最近比赛 + 统计
@app.get("/matches/with_stats")
async def get_matches(player_id: str, limit: int = 5):
    url = f"https://open.faceit.com/data/v4/players/{player_id}/history?game=cs2&limit={limit}"
    headers = {"Authorization": f"Bearer {os.getenv('FACEIT_API_KEY')}"}
    resp = requests.get(url, headers=headers)
    data = resp.json()

    matches = []
    for item in data.get("items", []):
        match_id = item["match_id"]
        score = item["results"]["score"] if "results" in item else "-"
        map_name = item.get("map", "-")
        matches.append({
            "match_id": match_id,
            "demo_url": f"https://www.faceit.com/en/cs2/room/{match_id}",  # 直接跳转房间
            "map": map_name,
            "score": score
        })
    return {"matches": matches}
