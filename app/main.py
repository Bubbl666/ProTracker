from __future__ import annotations

import math
import pytz
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态资源（如果有）
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


# =========================
# 你项目里已有的底层函数（重要）
# =========================
# 说明：以下三个函数名，是为了“对接你已有的抓取逻辑”。
# 如果你项目里函数名不同或集中在某个模块里（比如 faceit.py），只需要把实现替换为你原来可用的调用即可。
# 这三件事必须能做：
# 1) 通过昵称查到 player_id、nickname、country 等（国家码用于时区）
# 2) 取最近 N 场比赛 + 带 Stats（你之前 /matches/with_stats 的原始结构）
# 3) 构造 faceit 比赛链接与玩家主页链接（如果你已有工具函数，可直接用）

def _faceit_match_url(match_id: str) -> str:
    return f"https://www.faceit.com/en/cs2/room/{match_id}"

def _faceit_profile_url(nickname: str) -> str:
    return f"https://www.faceit.com/en/players/{nickname}"

def resolve_player_by_name(nickname: str) -> Dict[str, Any]:
    """
    需要返回：{ 'player_id': str, 'nickname': str, 'country': 'RU'(可无), ... }
    这里默认调用你原有逻辑；如果你本地函数叫别的名，把下面替换成你的实现。
    """
    # 示例占位：你需要换成自己原有的 player 获取逻辑
    # 下面这段请求只是示意，不会真的工作（Faceit开放API需要key）
    # 建议：直接用你之前成功的“昵称->player_id”方法
    raise NotImplementedError("请接入你原来的昵称解析函数：resolve_player_by_name")

def fetch_recent_matches_with_stats(player_id: str, limit: int = 5) -> Dict[str, Any]:
    """
    返回结构需要和你之前 /matches/with_stats 的原始 JSON 相同（你贴过的长 JSON）。
    也就是包含：
    - 'player': {...可选}
    - 'matches': [ { 'match_id', 'score': {'faction1','faction2'}, 'teams':[...], 'started_at', 'finished_at', ... } ... ]
    """
    raise NotImplementedError("请接入你原来的近期比赛+统计的函数：fetch_recent_matches_with_stats")


# =========================
# 工具：国家 -> 时区（粗略）
# =========================
def tz_for_country(country_code: Optional[str]) -> timezone:
    try:
        if country_code:
            zones = pytz.country_timezones.get(country_code.upper())
            if zones:
                return pytz.timezone(zones[0])
    except Exception:
        pass
    return pytz.utc


# =========================
# 工具：安全取数
# =========================
def _num(x: Any, default: float = 0.0) -> float:
    try:
        if x in (None, "", "-"):
            return default
        return float(x)
    except Exception:
        return default


# =========================
# 工具：计算 HLTV 2.0-like rating
# rating ≈ 0.0073 * ADR + 0.3591 * KPR + 0.5334 * (1 - DPR)
# KPR: K/R Ratio；DPR ≈ Deaths / Rounds；Rounds≈Kills/KPR（用 KPR 推回合数）
# =========================
def compute_rating_like(stats: Dict[str, Any]) -> Optional[float]:
    adr = _num(stats.get("ADR"))
    kpr = _num(stats.get("K/R Ratio"))
    kills = _num(stats.get("Kills"))
    deaths = _num(stats.get("Deaths"))

    if kpr <= 0 or kills <= 0:
        # 兜底：用 K/D 估一个 DPR
        kd = _num(stats.get("K/D Ratio"))
        if kd > 0:
            # K/D = kills/deaths -> 近似 DPR = 1/(1+KD) （只是兜底近似）
            dpr = 1.0 / (1.0 + kd)
        else:
            dpr = 0.5
    else:
        rounds = max(1.0, kills / kpr)
        dpr = min(1.0, deaths / rounds)

    rating = 0.0073 * adr + 0.3591 * kpr + 0.5334 * (1.0 - dpr)
    return round(rating, 2)


# =========================
# 工具：从原始JSON里解析“我方队、比分、KAD、是否五杀”等
# =========================
def parse_matches_payload(raw: Dict[str, Any], player_id: str, player_tz: timezone) -> List[Dict[str, Any]]:
    matches_out: List[Dict[str, Any]] = []

    for m in raw.get("matches", []):
        match_id = m.get("match_id")
        started_ts = m.get("started_at")
        map_name = m.get("map") or "-"
        score_obj = m.get("score") or {}
        teams = m.get("teams", [])

        # 1) 找到玩家在哪个队、并拿到个人stats
        my_team_index = None
        my_stats: Dict[str, Any] = {}
        for idx, t in enumerate(teams):
            for p in t.get("players", []):
                if p.get("player_id") == player_id:
                    my_team_index = idx
                    my_stats = p.get("stats") or {}
                    break
            if my_team_index is not None:
                break

        # 如果找不到，跳过
        if my_team_index is None:
            continue

        # 2) 组装比分和胜负
        f1 = int(score_obj.get("faction1") or 0)
        f2 = int(score_obj.get("faction2") or 0)
        # 阵营 0->faction1，1->faction2
        my_score = f1 if my_team_index == 0 else f2
        opp_score = f2 if my_team_index == 0 else f1
        win = my_score > opp_score
        score_str = f"{my_score} / {opp_score}"

        # 3) 本地时间格式
        if started_ts:
            dt_utc = datetime.fromtimestamp(int(started_ts), tz=timezone.utc)
            dt_local = dt_utc.astimezone(player_tz)
            date_str = dt_local.strftime("%m/%d/%Y, %I:%M %p")
        else:
            date_str = ""

        # 4) K/A/D
        kills = int(_num(my_stats.get("Kills")))
        assists = int(_num(my_stats.get("Assists")))
        deaths = int(_num(my_stats.get("Deaths")))
        kad = f"{kills} / {assists} / {deaths}"

        # 5) 是否五杀（Faceit统计里有 Penta Kills）
        is_ace = int(_num(my_stats.get("Penta Kills"))) > 0

        # 6) 估算 rating
        rating = compute_rating_like(my_stats)

        matches_out.append({
            "match_id": match_id,
            "match_url": _faceit_match_url(match_id),
            "map": map_name,
            "date": date_str,
            "score": score_str,
            "win": win,
            "k": kills,
            "a": assists,
            "d": deaths,
            "kad": kad,
            "is_ace": is_ace,
            "rating": rating if rating is not None else "-",
        })

    return matches_out


# =========================
# 视图：主页（模板）
# =========================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# 兼容旧的 /version
@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


# =========================
# 多玩家列视图：前端通过 /player?name=xxx&limit=5
# 返回统一结构：{ player, profile_url, matches:[{date, score, match_url, k/a/d, rating, is_ace}] }
# =========================
@app.get("/player")
def player_view(
    name: str = Query(..., description="玩家昵称"),
    limit: int = Query(5, ge=1, le=20, description="每位玩家条数"),
) -> JSONResponse:
    try:
        # 1) 解析玩家
        info = resolve_player_by_name(name)
        player_id = info.get("player_id")
        nickname = info.get("nickname") or name
        country = info.get("country")  # e.g. 'RU'
        if not player_id:
            raise HTTPException(404, f"player not found: {name}")

        # 2) 取最近比赛+stats（用你原有的“已成功”的函数）
        raw = fetch_recent_matches_with_stats(player_id, limit=limit)

        # 3) 时区（按国家码粗略映射）
        zone = tz_for_country(country)

        # 4) 解析与补齐
        matches = parse_matches_payload(raw, player_id=player_id, player_tz=zone)

        out = {
            "player": nickname,
            "profile_url": _faceit_profile_url(nickname),
            "matches": matches,
        }
        return JSONResponse(out)

    except HTTPException:
        raise
    except NotImplementedError as e:
        # 提示你替换接口实现
        raise HTTPException(500, str(e))
    except Exception as e:
        raise HTTPException(500, f"player route error: {e}")
