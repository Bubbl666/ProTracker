from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态资源与模板
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """
    前端页面：多玩家输入 + 列表展示。
    数据仍调用你现有后端接口：
      - GET /player?name=...
      - GET /matches/with_stats?player_id=...&limit=...
    """
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


# 可选：为了兼容你之前直接访问根域名拿到JSON的习惯，做个重定向到 /health
@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")
