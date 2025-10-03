# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import math
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_API_URL = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

app = FastAPI(title="ProTracker", version="0.1.1")
templates = Jinja2Templates(directory="app/templates")


# -------------------------
# Utilities
# -------------------------
def room_url(game: str, match_id: str) -> str:
    # Faceit 房间页（可在里面下载 demo）
    game_slug = (game or "cs2").lower()
    return f"https://www.faceit.com/en/{game_slug}/room/{match_id}"


def player_url(nickname: str) -> str:
    # Faceit 玩家主页
    nick = (nickname or "").strip()
    return f"https://www.faceit.com/en/players/{nick}" if nick else "#"


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if isinstance(v, (int, float)):
            return float(v)
        return float(str(v).replace("%", "").strip())
    except Exception:
        return default


def rating_hltv2_approx(player_stats: Dict[str, Any]) -> Optional[float]:
    """
    受 HLTV 2.0 启发的“近似值”（不是官方算法！）
    说明：
      - KR: Faceit 已给“K/R Ratio”（每回合击杀）
      - DPR: 通过估算回合数 round = kills / KR，再 deaths / round 得到
      - ADR: Faceit 已给 “ADR”
      - 参考社区常见近似：R ≈ avg( KR/0.679, (1-DPR)/(1-0.317), ADR/85 )
    注：Faceit 返回的数据不足以计算官方 HLTV 2.0 或 3.0（缺 KAST、Impact 等），
        所以这里只提供一个“HLTV-like”的近似评分，范围大致与 2.0 类似。
    """
    kills = to_float(player_stats.get("Kills", 0))
    deaths = to_float(player_stats.get("Deaths", 0))
    kr = to_float(player_stats.get("K/R Ratio", player_stats.get("K/R", 0)))
    adr = to_float(player_stats.get("ADR", 0))

    if kr <= 0 or kills <= 0:
        # KR 或击杀为 0，无法估算回合
        return None

    rounds_est = max(kills / kr, deaths, 1.0)  # 防止极端值
    dpr = min(max(deaths / rounds_est, 0.0), 1.5)  # 简单夹取

    parts: List[float] = []
    parts.append(kr / 0.679)                    # KPR scaling
    parts.append((1.0 - dpr) / (1.0 - 0.317))   # Survival scaling
    parts.append(adr / 85.0)                    # ADR scaling

    # 简单平均
    rating = sum(parts) / 3.0
    # 夹取到 [0.1, 2.5] 的合理范围，避免数据缺失导致的异常
    rating = min(max(rating, 0.1), 2.5)
    return round(rating, 2)


def short_score_color(player_result_value: Any) -> Tuple[str, bool]:
    """
    根据 Faceit 的 Result（"1"=胜, "0"=负）返回 css 类名和胜负布尔值
    """
    val = str(player_result_value).strip()
    win = (val == "1")
    css = "score-win" if win else "score-lose"
    return css, win


# -------------------------
# Low-level Faceit fetchers
# -------------------------
def fetch_player(nickname_or_id: str) -> Dict[str, Any]:
    # 先按昵称找（players），再回退到 player_id
    if not HEADERS:
        return {}
    # 如果传过来就是 player_id，Faceit 也支持 /players/{id}
    url = f"{FACEIT_API_URL}/players/{nickname_or_id}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    if r.status_code == 200:
        return r.json()

    # 再尝试用昵称查
    url = f"{FACEIT_API_URL}/players?nickname={nickname_or_id}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    return r.json() if r.status_code == 200 else {}


def fetch_recent_matches_with_stats(player_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    这里假设你已有 Faceit 聚合后的“with_stats”接口逻辑。如果你已经在项目中实现，
    直接复用你现有的函数即可。此处保留一个最小包装：从你自己的服务转发。
    """
    base = os.getenv("SELF_BASE_URL", "").rstrip("/")
    # 如果你的服务就跑在本机，直接请求内部路由（FastAPI 内部调度也可以，但用 HTTP 简单一些）
    if base:
        url = f"{base}/matches/with_stats?player_id={player_id}&limit={limit}"
        try:
            r = requests.get(url, timeout=30)
            if r.ok:
                return r.json()
        except Exception:
            pass
    # 兜底：尝试调用你之前写的后端聚合接口（同进程）
    # 如果你的项目中已有 app 内部的实现，可直接 import 调用；这里简单返回空列表
    return []


# -------------------------
# API Endpoints (兼容原有)
# -------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    # 默认打开 UI
    return templates.TemplateResponse("index.html", {"request": request})


class PagerQuery(BaseModel):
    player: str
    limit: int = 5


@app.get("/ui", response_class=HTMLResponse)
def ui(request: Request,
       player: str = Query("donk666"),
       limit: int = Query(5)) -> HTMLResponse:
    # 渲染界面（和 "/" 同一模板，只是带参数）
    return templates.TemplateResponse("index.html", {"request": request, "player": player, "limit": limit})


@app.get("/service")
def service_ok():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


# 你现有的 passthrough/聚合接口保持不变，这里仅留占位
@app.get("/matches/with_stats")
def passthrough_matches_with_stats(player_id: str, limit: int = 5):
    # 假设你已经实现了这个接口（你刚才发的返回值截图就是这个）。
    # 这里简单从环境/上游取，真实项目里请直接返回你已有的聚合结果。
    return JSONResponse([])


# -------------------------
# Page Data endpoint for template (新加)
# -------------------------
@app.get("/page-data", response_class=JSONResponse)
def page_data(nickname: str = Query("donk666"), limit: int = Query(5)):
    """
    提供模板一次取齐的数据：
      - player 对象（含主页链接）
      - 最近比赛（带近似 Rating、比赛链接、胜负颜色类）
    """
    player = fetch_player(nickname)
    player_id = player.get("player_id") or player.get("id") or ""
    faceit_nick = player.get("nickname") or nickname
    faceit_profile = player_url(faceit_nick)

    matches = fetch_recent_matches_with_stats(player_id, limit=limit)

    # 为前端补充显示字段
    baked: List[Dict[str, Any]] = []
    for m in matches:
        g = m.get("game", "cs2")
        match_id = m.get("match_id", "")
        # 显示用字段
        m["room_url"] = room_url(g, match_id)

        # 玩家自己的统计（有 with_stats 时才有）
        p = (m.get("player") or {}).get("stats") or {}
        rating = rating_hltv2_approx(p) if p else None
        m["rating_approx"] = rating

        # 胜负颜色
        css, win = short_score_color(p.get("Result", ""))
        m["score_css"] = css
        m["won"] = win

        baked.append(m)

    return {
        "player": {
            "nickname": faceit_nick,
            "profile_url": faceit_profile,
            "player_id": player_id,  # 不展示给用户，仅保留在返回里备用
        },
        "matches": baked,
        "limit": limit,
    }
