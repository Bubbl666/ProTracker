# app/main.py
from __future__ import annotations

import os
import math
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

# ========================
# 基础与静态/模板
# ========================

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_API = "https://open.faceit.com/data/v4"
HEADERS = {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}

CS_GAME = "cs2"  # 你的页面就是查 CS2

# ========================
# 页面
# ========================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """
    新的前端页面（多玩家输入、比分颜色区分、可点进房间/下载 Demo、Rating 估算等）
    前端会调用本文件中的 /player 与 /matches/with_stats 两个接口。
    """
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")


# ========================
# 工具函数
# ========================

def _need_key():
    if not FACEIT_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="FACEIT_API_KEY 未配置。请在 Render 环境变量中添加 FACEIT_API_KEY。"
        )

def _get(url: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    简单 GET 包装，带错误处理。
    """
    _need_key()
    r = requests.get(url, headers=HEADERS, params=params or {}, timeout=20)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=r.text)
    return r.json()

def _room_url(match_id: str) -> str:
    # 房间页（也能在页面里拿到 Demo）
    return f"https://www.faceit.com/en/{CS_GAME}/room/{match_id}"

def _player_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{nickname}"

# ---- HLTV 2.0 估算（说明：Faceit开放数据缺少 KAST/Impact 的逐回合计算，这里做可用且稳定的近似）
# 公式思路：以 ADR、K/D、K/R 三项归一化组合并微量考虑 HS%（不把 HS 当主要因子）。
# 经验权重：ADR(0.45) + K/D(0.35) + K/R(0.18) + HS%(0.02)，并做软裁剪（限制极端值）。
def hltv2_like_rating(adr: float, kd: float, kr: float, hs_percent: float) -> float:
    # 归一化：ADR 以 85 为良好基准（职业赛 75~90 常见）
    adr_norm = min(adr / 85.0, 2.0)
    kd_norm = min(kd / 1.10, 2.0)     # 1.10 作为略强基准
    kr_norm = min(kr / 0.80, 2.0)     # 0.80 作为略强基准
    hs_norm = min(max(hs_percent, 0.0), 100.0) / 100.0

    rating = 0.45 * adr_norm + 0.35 * kd_norm + 0.18 * kr_norm + 0.02 * hs_norm
    # 缓和：把结果映射到大致 0.6~2.0
    return round(max(0.60, min(rating, 2.00)), 2)

# ========================
# 数据接口
# ========================

@app.get("/player")
def get_player(name: str = Query(..., description="Faceit 昵称")):
    """
    根据昵称拿 player_id。
    """
    data = _get(f"{FACEIT_API}/players", params={"nickname": name})
    player_id = data.get("player_id")
    if not player_id:
        return {"found": False, "nickname": name}
    return {
        "player_id": player_id,
        "nickname": data.get("nickname") or name,
        "found": True,
        "profile_url": _player_url(name),
    }


@app.get("/matches/with_stats")
def get_matches_with_stats(
    player_id: str = Query(...),
    limit: int = Query(5, ge=1, le=20)
):
    """
    拉取某玩家最近比赛（含基础统计）。
    - Match 行可点击进入房间页（房间页可以下载 Demo）。
    - Score 按输赢上色（需要识别该玩家属于哪一方）。
    - 附带一个 HLTV2.0-like 估算 Rating。
    """
    # 最近比赛（history）
    hist = _get(
        f"{FACEIT_API}/players/{player_id}/history",
        params={"game": CS_GAME, "limit": limit}
    )
    items: List[Dict[str, Any]] = hist.get("items", [])

    out: List[Dict[str, Any]] = []

    for it in items:
        match_id = it.get("match_id")
        if not match_id:
            continue

        # 取比赛详情：为了拿 teams、score、map 等
        # （注意：有时 details 里 map 字段不稳定，这里都做好兜底）
        md = _get(f"{FACEIT_API}/matches/{match_id}")

        # ---- 基础信息
        started_at = int(it.get("started_at", md.get("started_at", 0)))
        finished_at = int(it.get("finished_at", md.get("finished_at", 0)))
        map_name = it.get("map") or md.get("voting", {}).get("map", {}).get("pick", "") or md.get("map", "")
        if not map_name:
            map_name = "-"

        # 解析队伍 & 分数
        teams = md.get("teams", {})
        # Faceit 返回通常有 "faction1"/"faction2"
        f1 = teams.get("faction1", {})
        f2 = teams.get("faction2", {})
        s1 = f1.get("result", {}).get("score", 0)
        s2 = f2.get("result", {}).get("score", 0)

        # 判断玩家在哪队
        def _contains(pid: str, faction: Dict[str, Any]) -> bool:
            for p in faction.get("roster", []):
                if p.get("player_id") == pid:
                    return True
            return False

        in_f1 = _contains(player_id, f1)
        in_f2 = _contains(player_id, f2)
        won = None
        if in_f1:
            won = s1 > s2
        elif in_f2:
            won = s2 > s1

        # ---- 玩家该场统计（stats/players 返回结构较大；使用 match stats endpoint）
        # 某些比赛可能没有完整统计，做好容错。
        adr = kd = kr = hs = 0.0
        kills = deaths = assists = 0
        try:
            # /matches/{match_id}/stats
            ms = _get(f"{FACEIT_API}/matches/{match_id}/stats")
            # stats 里通常有 rounds -> teams -> players
            rounds = ms.get("rounds", [])
            if rounds:
                # 合并各回合/地图的统计（CS2 多数是 BO1 一张）
                players: List[Dict[str, Any]] = []
                for r in rounds:
                    for t in r.get("teams", []):
                        players.extend(t.get("players", []))
                for pl in players:
                    if pl.get("player_id") == player_id:
                        # 字段命名在 Faceit stats 里经常是字符串，需要 float/int 转换
                        def _f(key: str, default=0.0) -> float:
                            v = pl.get("player_stats", {}).get(key)
                            try:
                                return float(v)
                            except Exception:
                                return float(default)

                        def _i(key: str, default=0) -> int:
                            v = pl.get("player_stats", {}).get(key)
                            try:
                                return int(float(v))
                            except Exception:
                                return int(default)

                        kills = _i("Kills", 0)
                        deaths = _i("Deaths", 0)
                        assists = _i("Assists", 0)
                        adr = _f("ADR", 0.0)
                        hs = _f("Headshots %", 0.0)
                        # K/R 可能在 stats 里叫 "K/R Ratio"；没有则用 kills/rounds 估算
                        kr = _f("K/R Ratio", 0.0)
                        if kr <= 0:
                            rds = _i("Rounds", 0)
                            kr = (kills / max(1, rds)) if rds else 0.0

                        kd = (kills / max(1, deaths)) if deaths else float(kills > 0)
                        break
        except Exception:
            # 统计不可用也不要让接口挂掉
            pass

        rating = hltv2_like_rating(adr=adr, kd=kd, kr=kr, hs_percent=hs)

        out.append({
            "match_id": match_id,
            "room_url": _room_url(match_id),   # ✅ 1) Match 可点
            "game": CS_GAME,
            "map": map_name,
            "score": {"faction1": s1, "faction2": s2},
            "started_at": started_at,
            "finished_at": finished_at,
            "won": won,                        # ✅ 5) 前端据此给比分上色
            "player": {
                "player_id": player_id,
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "adr": round(adr, 1) if adr else 0.0,
                "hs": round(hs, 1) if hs else 0.0,
                "kd": round(kd, 2) if kd else 0.0,
                "kr": round(kr, 2) if kr else 0.0,
                "rating_hltv2_like": rating,   # ✅ 2) HLTV 2.0 估算
            }
        })

    return out
