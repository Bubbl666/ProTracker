# app/main.py
# ==== 新增：顶部导入 ====
import time
from typing import Optional, Dict, Any

# ==== 新增：简单的内存缓存，避免对 /players/{id} 频繁打点 ====
_PROFILE_TTL = 3600  # 1h
_profile_cache: Dict[str, Dict[str, Any]] = {}  # player_id -> {"data":..., "ts":...}

async def fetch_player_profile(session, player_id: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    hit = _profile_cache.get(player_id)
    if hit and now - hit["ts"] < _PROFILE_TTL:
        return hit["data"]

    url = f"{FACEIT_API}/players/{player_id}"
    try:
        async with session.get(url, headers=faceit_headers(), timeout=20) as r:
            if r.status != 200:
                return None
            raw = await r.json()
    except Exception:
        return None

    data = {
        "player_id": raw.get("player_id"),
        "nickname": raw.get("nickname"),
        "avatar": (raw.get("avatar") or ""),
        "skill_level": (raw.get("games", {})
                          .get("cs2", {})
                          .get("skill_level") or raw.get("games", {})
                          .get("csgo", {})
                          .get("skill_level") or None),
        "game_player_id": (raw.get("games", {})
                            .get("cs2", {})
                            .get("game_player_id")
                           or raw.get("games", {})
                            .get("csgo", {})
                            .get("game_player_id")),
        "faceit_url": (raw.get("faceit_url") or "").replace("{lang}", "en"),
    }
    _profile_cache[player_id] = {"data": data, "ts": now}
    return data

# ==== 修改：把队伍里每个玩家补全资料（可选） ====
async def _enrich_team_players(session, team: Dict[str, Any], enrich: bool):
    """team: {'team_id','nickname','players':[{'player_id','nickname',...}] }"""
    if not enrich:
        # 填上兼容字段，但不额外请求
        for p in team.get("players", []):
            p.setdefault("avatar", "")
            p.setdefault("skill_level", None)
            p.setdefault("game_player_id", None)
            p.setdefault("faceit_url", None)
        return

    # 并发拉去资料（注意：人数多时会增大延迟&调用量）
    tasks = []
    for p in team.get("players", []):
        tasks.append(fetch_player_profile(session, p["player_id"]))
    profs = await asyncio.gather(*tasks, return_exceptions=True)

    for p, prof in zip(team.get("players", []), profs):
        if isinstance(prof, dict) and prof.get("player_id"):
            p["avatar"] = prof.get("avatar") or ""
            p["skill_level"] = prof.get("skill_level")
            p["game_player_id"] = prof.get("game_player_id")
            p["faceit_url"] = prof.get("faceit_url")
        else:
            p.setdefault("avatar", "")
            p.setdefault("skill_level", None)
            p.setdefault("game_player_id", None)
            p.setdefault("faceit_url", None)

# ==== 修改：在拼装比赛数据时调用上面的补全 ====
@app.get("/matches/with_stats")
async def matches_with_stats(
    player_id: str = Query(..., description="Faceit player_id"),
    limit: int = Query(5, ge=1, le=20),
    game: str = Query("cs2"),
    enrich: int = Query(0, description="为队友/对手补全头像和段位，1=开启")
):
    async with aiohttp.ClientSession() as session:
        # ...（原先你的拉取历史&stats代码）...

        # 假设最后得到 match_dict，里面有 teams 两个元素，每个元素是 team dict
        # 在返回之前补全队伍成员信息：
        for m in matches:
            teams = m.get("teams") or []
            for t in teams:
                await _enrich_team_players(session, t, enrich=bool(enrich))

        return matches

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

# =========================
# Config & Helpers
# =========================

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "").strip()
if not FACEIT_API_KEY:
    # 让服务仍可启动，根路径会给出提示；真正调用时会抛错
    pass

FACEIT_BASE = "https://open.faceit.com/data/v4"
HTTP_TIMEOUT = 10  # 秒

HEADERS = {
    "Authorization": f"Bearer {FACEIT_API_KEY}" if FACEIT_API_KEY else "",
    "Accept": "application/json",
}

def _check_key():
    if not FACEIT_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="FACEIT_API_KEY is missing. Please set it on your server.",
        )

def http_get(url: str, params: Optional[dict] = None) -> requests.Response:
    """统一 GET，带超时与简单重试（应对偶发 5xx / 限流）。"""
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=HTTP_TIMEOUT)
            # 429 限流/5xx 轻度退避
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.6 * (attempt + 1))
                continue
            return resp
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(0.4 * (attempt + 1))
    # 理论上不会走到这里
    raise HTTPException(status_code=502, detail="Network error when calling Faceit API")

def find_player_by_nickname(nickname: str) -> Dict[str, Any]:
    """
    GET /players?nickname={nick}
    成功返回 { player_id, nickname, found: true }；否则 { found: false }。
    """
    _check_key()
    url = f"{FACEIT_BASE}/players"
    resp = http_get(url, params={"nickname": nickname})
    if resp.status_code == 200:
        data = resp.json()
        return {
            "player_id": data.get("player_id"),
            "nickname": data.get("nickname"),
            "found": True,
        }
    elif resp.status_code == 404:
        return {"found": False}
    else:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

def get_history(player_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    GET /players/{player_id}/history?game=cs2&offset=0&limit=n
    返回 Faceit 原始的 items 列表。
    """
    _check_key()
    url = f"{FACEIT_BASE}/players/{player_id}/history"
    resp = http_get(url, params={"game": "cs2", "offset": 0, "limit": limit})
    if resp.status_code != 200:
        if resp.status_code == 404:
            return []
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    j = resp.json()
    return j.get("items", []) or []

def get_match_stats(match_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    GET /matches/{match_id}/stats
    成功返回 (stats_json, None)
    若统计不可用/不存在，返回 (None, reason)
    """
    _check_key()
    url = f"{FACEIT_BASE}/matches/{match_id}/stats"
    resp = http_get(url)
    if resp.status_code == 200:
        return resp.json(), None
    if resp.status_code in (403, 404):
        # 403：可能是私有/受限；404：统计尚未生成
        return None, "not_ready_or_hidden"
    # 其它错误
    raise HTTPException(status_code=resp.status_code, detail=resp.text)

def get_match_basic(match_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    GET /matches/{match_id}
    作为 stats 缺失的兜底，拿到 map/双方阵容（注意结构不同）
    """
    _check_key()
    url = f"{FACEIT_BASE}/matches/{match_id}"
    resp = http_get(url)
    if resp.status_code == 200:
        return resp.json(), None
    if resp.status_code == 404:
        return None, "match_not_found"
    raise HTTPException(status_code=resp.status_code, detail=resp.text)

def _extract_summary_from_stats(stats_json: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    从 /stats 结构归纳出 (map, score, teams)，
    其中 teams 结构仿照你现在前端用的样子：[{ team_id, nickname, players: [...] }, ...]
    """
    rounds = stats_json.get("rounds", [])
    if not rounds:
        return "-", "-", []
    r0 = rounds[0]
    round_stats = r0.get("round_stats", {}) or {}
    map_name = round_stats.get("Map", "-")
    score = f"{round_stats.get('Score', '-')}"
    teams_src = r0.get("teams", []) or []

    teams: List[Dict[str, Any]] = []
    for t in teams_src:
        players = []
        for p in t.get("players", []) or []:
            players.append(
                {
                    "player_id": p.get("player_id"),
                    "nickname": p.get("nickname"),
                    "avatar": p.get("player_stats", {}).get("Avatar", ""),  # 不一定有
                    "skill_level": None,
                    "game_player_id": p.get("game_player_id"),
                    "game_player_name": p.get("game_player_name"),
                    "faceit_url": p.get("faceit_url"),
                }
            )
        teams.append(
            {
                "team_id": t.get("team_id"),
                "nickname": t.get("team_stats", {}).get("Team", t.get("team_id", "")),
                "avatar": "",
                "type": "",
                "players": players,
            }
        )
    return map_name, score, teams

def _find_player_stats_in_round(stats_json: Dict[str, Any], player_id: str) -> Optional[Dict[str, Any]]:
    """
    在 /stats 的 rounds[0].teams[*].players[*] 中按 player_id 精确查找该玩家，并提炼 K/D/ADR/HS/KR 等。
    """
    rounds = stats_json.get("rounds", [])
    if not rounds:
        return None
    r0 = rounds[0]
    for t in r0.get("teams", []) or []:
        for p in t.get("players", []) or []:
            if p.get("player_id") == player_id:
                ps = p.get("player_stats", {}) or {}
                # Faceit 字段名称因 Hub 略有出入，尽量兜底
                def f(name: str, default: Any = None):
                    return ps.get(name, default)
                # K/D/ADR/HS/KR 常见字段
                kills = int(f("Kills", f("K", 0)) or 0)
                deaths = int(f("Deaths", 0) or 0)
                assists = int(f("Assists", 0) or 0)
                adr = float(f("ADR", 0.0) or 0.0)
                hs = float(f("Headshots %", f("HS %", 0.0)) or 0.0)
                kast = float(f("K/R Ratio", 0.0) or 0.0)  # 有些 Hub 把 K/R 叫做 KAST，不同模板会不同
                kr = float(f("K/R Ratio", 0.0) or 0.0)

                kd = round((kills / deaths) if deaths else float(kills), 2)

                return {
                    "nickname": p.get("nickname"),
                    "kills": kills,
                    "deaths": deaths,
                    "assists": assists,
                    "adr": round(adr, 1),
                    "hs": round(hs, 1),
                    "kd": kd,
                    "kr": round(kr, 2),
                    "raw": ps,  # 保留原始字段以便调试
                }
    return None

def _extract_summary_from_match(match_json: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    当 /stats 不可用时，尽量从 /matches/{id} 里凑 map/score/teams 的摘要（字段与 stats 不同）。
    """
    payload = match_json or {}
    # 地图在 voting 或 match_config 里可能出现，兜底为 '-'
    map_name = "-"
    try:
        voting = payload.get("voting", {})
        if isinstance(voting, dict):
            map_votes = voting.get("map", {}).get("entities", [])
            # entities[*] 里有 {name, class_name}，但不一定就是最终地图，这里只兜底显示第一个
            if map_votes:
                map_name = map_votes[0].get("name", "-")
    except Exception:
        pass

    # score 很难从 /matches 拿到（通常在 /stats），这里用 '-'
    score = "-"

    teams: List[Dict[str, Any]] = []
    for side_key in ("faction1", "faction2"):
        t = payload.get(side_key)
        if not t:
            continue
        players = []
        for p in t.get("roster", []) or []:
            players.append(
                {
                    "player_id": p.get("player_id"),
                    "nickname": p.get("nickname"),
                    "avatar": p.get("avatar", ""),
                    "skill_level": p.get("skill_level"),
                    "game_player_id": p.get("game_player_id"),
                    "game_player_name": p.get("game_player_name"),
                    "faceit_url": p.get("faceit_url"),
                }
            )
        teams.append(
            {
                "team_id": t.get("team_id"),
                "nickname": t.get("nickname", t.get("team_id", "")),
                "avatar": t.get("avatar", ""),
                "type": "",
                "players": players,
            }
        )
    return map_name, score, teams


# =========================
# FastAPI App
# =========================

app = FastAPI(title="FACEIT Pro Tracker API", version="0.1.1")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 如需只允许你的网站，请改成你的域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def root():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}

@app.get("/players")
def players(query: str = Query(..., description="Faceit 昵称")):
    """
    根据昵称查玩家。
    """
    result = find_player_by_nickname(query)
    if not result.get("found"):
        raise HTTPException(status_code=404, detail="Player not found")
    return result

@app.get("/match_stats")
def match_stats(match_id: str, player_id: Optional[str] = None):
    """
    单场统计（带 player_id 则尝试提取该玩家个人数据）。
    """
    stats, reason = get_match_stats(match_id)
    if not stats:
        # 统计不可用时，提供一点基本信息兜底
        match_json, _ = get_match_basic(match_id)
        map_name, score, teams = _extract_summary_from_match(match_json or {})
        return {
            "match_id": match_id,
            "available": False,
            "stats_unavailable_reason": reason,
            "map": map_name,
            "score": score,
            "teams": teams,
            "player": None,
        }

    map_name, score, teams = _extract_summary_from_stats(stats)
    player_data = None
    if player_id:
        player_data = _find_player_stats_in_round(stats, player_id)

    return {
        "match_id": match_id,
        "available": True,
        "map": map_name,
        "score": score,
        "teams": teams,
        "player": player_data,
    }

@app.get("/matches/with_stats")
def matches_with_stats(
    player_id: str = Query(..., description="Faceit player_id"),
    limit: int = Query(5, ge=1, le=20),
):
    """
    最近比赛列表：对每场尝试合并 /stats 中的个人统计。
    返回结构与你前端已使用的结构兼容：
      - map / score / teams（队伍与队员）
      - player（若找到个人统计）
      - stats_unavailable_reason（统计不可用时的原因）
    """
    items = get_history(player_id, limit=limit)
    results: List[Dict[str, Any]] = []

    for it in items:
        mid = it.get("match_id")
        started_at = it.get("started_at")
        finished_at = it.get("finished_at")
        game = it.get("game", "cs2")

        # 优先 /stats（同时可拿到 teams 与个人数据）
        stats_json, reason = get_match_stats(mid)
        if stats_json:
            map_name, score, teams = _extract_summary_from_stats(stats_json)
            player_stats = _find_player_stats_in_round(stats_json, player_id)
            results.append(
                {
                    "match_id": mid,
                    "game": game,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "map": map_name,
                    "score": score,
                    "teams": teams,
                    "player": player_stats,
                    "stats_unavailable_reason": None if player_stats else None,  # 有 stats 就没有不可用原因
                }
            )
            continue

        # /stats 不可用，兜底 /matches 拿基本信息
        match_json, _ = get_match_basic(mid)
        map_name, score, teams = _extract_summary_from_match(match_json or {})
        results.append(
            {
                "match_id": mid,
                "game": game,
                "started_at": started_at,
                "finished_at": finished_at,
                "map": map_name,
                "score": score,
                "teams": teams,
                "player": None,
                "stats_unavailable_reason": reason,
            }
        )

    return results
