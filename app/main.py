from __future__ import annotations

import os
import math
import typing as t
from functools import lru_cache
from datetime import datetime, timezone

import requests
from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates


# ------------------------------------------------------------------------------
# 基础
# ------------------------------------------------------------------------------
app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")

# 静态与模板（保持你的目录结构）
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
FACEIT_API_BASE = "https://open.faceit.com/data/v4"

CS_GAME = os.getenv("CS_GAME", "cs2")  # 允许切换 csgo / cs2，默认 cs2


def _tz_from_offset_minutes(offset_min: int | None) -> timezone:
    """
    将 Faceit 选手 profile 中的 timezone 偏移(分钟)转换为 tzinfo.
    若无信息，返回 UTC。
    """
    try:
        if offset_min is None:
            return timezone.utc
        return timezone.utc if offset_min == 0 else timezone(timedelta(minutes=offset_min))
    except Exception:
        return timezone.utc


def _bearer_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {FACEIT_API_KEY}"} if FACEIT_API_KEY else {}


def _get_json(url: str, params: dict | None = None, timeout: int = 12) -> dict:
    r = requests.get(url, headers=_bearer_headers(), params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


# ------------------------------------------------------------------------------
# Faceit 辅助：比分 + 房间页 + 阵营
# ------------------------------------------------------------------------------
@lru_cache(maxsize=1024)
def get_match_core(match_id: str) -> dict:
    """
    读取单场比赛详情，提取：
      - faction1 team_id 与分数
      - faction2 team_id 与分数
      - 房间页 URL（可下载 demo）
    """
    core = {"faction1": None, "faction2": None, "room_url": None}
    if not FACEIT_API_KEY:
        return core  # 无 KEY 时直接返回空

    data = _get_json(f"{FACEIT_API_BASE}/matches/{match_id}")

    # 房间页
    if isinstance(data.get("faceit_url"), str):
        core["room_url"] = data["faceit_url"]

    # faction -> team_id
    if isinstance(data.get("teams"), dict):
        for fx in ("faction1", "faction2"):
            team = data["teams"].get(fx) or {}
            core[fx] = {"team_id": team.get("team_id"), "score": None}

    # 优先从 results 取分
    if isinstance(data.get("results"), dict):
        sc = data["results"].get("score") or {}
        if core["faction1"]:
            core["faction1"]["score"] = sc.get("faction1")
        if core["faction2"]:
            core["faction2"]["score"] = sc.get("faction2")

    # 若无 results，则尝试最后一回合的 round_stats
    f1 = core["faction1"]["score"] if core["faction1"] else None
    f2 = core["faction2"]["score"] if core["faction2"] else None
    if (f1 is None or f2 is None) and isinstance(data.get("rounds"), list):
        last = None
        for rnd in data["rounds"]:
            if rnd.get("status") == "finished":
                last = rnd
        if last and isinstance(last.get("round_stats"), dict):
            rs = last["round_stats"]
            # Faceit 场景字段名常见为 "Score Team A" / "Score Team B"
            try:
                if f1 is None and isinstance(rs.get("Score Team A"), str):
                    core["faction1"]["score"] = int(rs["Score Team A"])
                if f2 is None and isinstance(rs.get("Score Team B"), str):
                    core["faction2"]["score"] = int(rs["Score Team B"])
            except Exception:
                pass

    return core


def _fmt_score_text(core: dict) -> str | None:
    try:
        f1 = core["faction1"]["score"]
        f2 = core["faction2"]["score"]
        if isinstance(f1, int) and isinstance(f2, int):
            return f"{f1} / {f2}"
        return None
    except Exception:
        return None


def _which_faction(player_team_id: str | None, core: dict) -> int | None:
    """
    玩家所在阵营：1 或 2。无法判定则 None。
    """
    if not player_team_id:
        return None
    try:
        if core["faction1"] and core["faction1"]["team_id"] == player_team_id:
            return 1
        if core["faction2"] and core["faction2"]["team_id"] == player_team_id:
            return 2
    except Exception:
        pass
    return None


# ------------------------------------------------------------------------------
# 公开接口
# ------------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """
    前端页面：多玩家输入 + 列表(分列)展示。
    页面会请求：
      - GET /player?name=xxx
      - GET /matches/with_stats?player_id=...&limit=...
    """
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/health")
def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
def version_redirect():
    return RedirectResponse(url="/health")


# ------------------------------------------------------------------------------
# 玩家查找（兼容名称/ID）
# ------------------------------------------------------------------------------
@app.get("/player")
def player_lookup(name: str = Query(..., description="Faceit 昵称或玩家ID")):
    """
    - 如果是 UUID 形态，当作 player_id
    - 否则调用 Faceit /players?nickname= 搜索
    """
    name = name.strip()
    if not name:
        raise HTTPException(400, "empty name")

    # 先按 ID 尝试
    try:
        if len(name) >= 32 and "-" in name:
            data = _get_json(f"{FACEIT_API_BASE}/players/{name}")
            return {
                "player_id": data.get("player_id"),
                "nickname": data.get("nickname"),
                "found": True,
                "profile_url": data.get("faceit_url"),
                "timezone_offset": data.get("settings", {}).get("timezone"),
            }
    except Exception:
        pass

    # 按昵称
    try:
        data = _get_json(f"{FACEIT_API_BASE}/players", params={"nickname": name, "game": CS_GAME})
        return {
            "player_id": data.get("player_id"),
            "nickname": data.get("nickname") or name,
            "found": True,
            "profile_url": data.get("faceit_url"),
            "timezone_offset": data.get("settings", {}).get("timezone"),
        }
    except Exception:
        # 不暴露外部细节
        return {"player_id": None, "nickname": name, "found": False}


# ------------------------------------------------------------------------------
# 比分接口（前端懒加载、或你想单测时用）
# ------------------------------------------------------------------------------
@app.get("/match/score")
def match_score(match_id: str = Query(..., description="Faceit match id")):
    core = get_match_core(match_id)
    return {
        "match_id": match_id,
        "score_text": _fmt_score_text(core),
        "faction1": core["faction1"]["score"] if core["faction1"] else None,
        "faction2": core["faction2"]["score"] if core["faction2"] else None,
        "room_url": core.get("room_url"),
    }


# ------------------------------------------------------------------------------
# 最近比赛（含统计 + 比分 + 阵营 + 房间链接）
#   说明：
#   1) 只拉最近 N 场 Faceit 比赛（历史接口 + 逐场详情）。
#   2) 统计项：Kills/Deaths/Assists/ADR/HS%/K:D/K:R/简单 Rating (HLTV 2.0-like 简化版)。
#   3) 新增字段：
#       - room_url: Faceit 房间页（可下载 demo）
#       - score_text: "13 / 10"
#       - faction: 1/2（玩家所在阵营，用于胜负着色）
# ------------------------------------------------------------------------------
@app.get("/matches/with_stats")
def matches_with_stats(
    player_id: str = Query(..., description="Faceit player_id"),
    limit: int = Query(5, ge=1, le=50),
):
    if not FACEIT_API_KEY:
        # 没有 API Key 也允许跑（比分为空，胜负靠 Result 兜底）
        pass

    # 1) 拉最近比赛 ID 列表
    try:
        hist = _get_json(
            f"{FACEIT_API_BASE}/players/{player_id}/history",
            params={"game": CS_GAME, "limit": limit},
        )
    except requests.HTTPError as e:
        raise HTTPException(status_code=e.response.status_code, detail="faceit history error")
    except Exception:
        raise HTTPException(500, "faceit history error")

    items = hist.get("items") or []
    out_matches: list[dict] = []

    # 2) 遍历每场，拉详情 & 拼装你现有字段（名称尽量与之前一致）
    for it in items:
        match_id = it.get("match_id")
        if not match_id:
            continue

        # 2.1 详情
        try:
            detail = _get_json(f"{FACEIT_API_BASE}/matches/{match_id}")
        except Exception:
            # 失败也尽量给壳数据
            out_matches.append({"match_id": match_id, "map": None, "score": None, "teams": [], "player": {}})
            continue

        # 地图 & 时间
        map_name = None
        if isinstance(detail.get("voting"), dict):
            try:
                map_name = (detail["voting"]["map"]["pick"][0]).lower()
            except Exception:
                pass
        # 退化：不少场景直接存在 "competition_map" 或 round_stats 中
        if not map_name and isinstance(detail.get("rounds"), list) and detail["rounds"]:
            rs0 = detail["rounds"][0].get("round_stats") or {}
            map_name = (rs0.get("Map") or rs0.get("Mapname") or "").lower() or None

        started_at = detail.get("started_at") or detail.get("created_at") or it.get("started_at")
        finished_at = detail.get("finished_at") or it.get("finished_at")

        # 2.2 队伍、玩家阵营、玩家统计
        teams_out = []
        found_player_stat: dict | None = None
        player_team_id: str | None = None

        if isinstance(detail.get("teams"), dict):
            for fx in ("faction1", "faction2"):
                team = detail["teams"].get(fx) or {}
                team_id = team.get("team_id")
                team_nickname = team.get("nickname")
                players_out = []
                for p in team.get("roster") or []:
                    pid = p.get("player_id")
                    nick = p.get("nickname")
                    stats = p.get("player_stats") or {}

                    players_out.append(
                        {
                            "player_id": pid,
                            "nickname": nick,
                            "avatar": None,
                            "stats": stats,  # 原始统计一并回传（你前端已在用）
                        }
                    )

                    if pid == player_id:
                        found_player_stat = {"nickname": nick, "stats": stats}
                        player_team_id = team_id

                teams_out.append({"team_id": team_id, "nickname": team_nickname, "players": players_out})

        # 2.3 简化统计汇总（兼容你的前端展示项）
        def _f(stats: dict, k: str, default: float = 0.0) -> float:
            try:
                v = stats.get(k)
                if v is None:
                    return default
                if isinstance(v, (int, float)):
                    return float(v)
                if isinstance(v, str):
                    if v.endswith("%"):
                        return float(v.replace("%", "").strip())
                    return float(v)
                return default
            except Exception:
                return default

        player_box = {"nickname": found_player_stat["nickname"] if found_player_stat else None}
        s = (found_player_stat or {}).get("stats") or {}

        kills = int(_f(s, "Kills", 0))
        deaths = int(_f(s, "Deaths", 0))
        assists = int(_f(s, "Assists", 0))
        adr = float(_f(s, "ADR", _f(s, "ADR:", 0)))   # 有的字段带冒号
        hs = float(_f(s, "Headshots %", _f(s, "HS%", 0)))
        kd = round(kills / deaths, 2) if deaths else float(kills)
        rounds = max(1, int(_f(s, "Rounds", _f(s, "Rounds Played", 0))))
        kr = round(kills / rounds, 2)

        # 一个稳定的 2.0-like 估算（非官方，仅用于观感）
        # 简版：rating = 0.0073*kills + 0.359*kr + 0.532*(kills/(kills+deaths)) + 0.2*(hs/100)
        # 避免除零
        kpr = kills / rounds
        survival = kills / (kills + deaths) if (kills + deaths) else 0.0
        rating = round(0.0073 * kills + 0.359 * kpr + 0.532 * survival + 0.2 * (hs / 100.0), 2)

        player_box.update(
            {
                "kills": kills,
                "deaths": deaths,
                "assists": assists,
                "adr": round(adr, 1),
                "hs": round(hs, 1),
                "kd": round(kd, 2),
                "kr": round(kr, 2),
            }
        )

        # 2.4 比分/房间/阵营
        core = get_match_core(match_id) if FACEIT_API_KEY else {"faction1": None, "faction2": None, "room_url": None}
        score_text = _fmt_score_text(core)
        faction = _which_faction(player_team_id, core)
        room_url = core.get("room_url")

        # 2.5 兼容性：若没有 score_text，回退到 items 的 result 提示（不展示数字）
        if not score_text and isinstance(it.get("results"), dict):
            sc = it["results"].get("score") or {}
            try:
                s1 = sc.get("faction1")
                s2 = sc.get("faction2")
                if isinstance(s1, int) and isinstance(s2, int):
                    score_text = f"{s1} / {s2}"
            except Exception:
                pass

        out_matches.append(
            {
                "match_id": match_id,
                "game": CS_GAME,
                "started_at": started_at,
                "finished_at": finished_at,
                "map": map_name,
                "score": score_text,            # ✅ 前端直接显示
                "room_url": room_url,           # ✅ 点击去 Faceit 房间页（下载 demo）
                "faction": faction,             # ✅ 玩家在 f1/f2，用于胜负着色
                "teams": teams_out,
                "player": player_box,
                "stats_unavailable_reason": None,
            }
        )

    return out_matches
