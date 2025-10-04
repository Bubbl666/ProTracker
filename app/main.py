from __future__ import annotations
import os
import time
from datetime import datetime
import pytz
import requests
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

FACEIT_API_KEY = os.getenv("FACEIT_API_KEY", "")
FACEIT_API = "https://open.faceit.com/data/v4"

DEFAULT_PLAYERS = [
    # 你可以在这里写默认搜索的玩家昵称（或在前端输入框覆盖）
    "donk666", "niko", "s1s1"
]

app = FastAPI(title="Pro Tracker 1.0", version="0.1.1")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def _auth_headers():
    if not FACEIT_API_KEY:
        return {}
    return {"Authorization": f"Bearer {FACEIT_API_KEY}"}


def faceit_get(url: str, params: dict | None = None, default=None):
    try:
        r = requests.get(url, headers=_auth_headers(), params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return default


def get_player_by_nickname(nick: str):
    return faceit_get(f"{FACEIT_API}/players", params={"nickname": nick, "game": "cs2"}, default=None)


def get_player_history(player_id: str, limit: int = 5):
    return faceit_get(
        f"{FACEIT_API}/players/{player_id}/history",
        params={"game": "cs2", "limit": limit, "offset": 0},
        default={"items": []},
    ) or {"items": []}


def get_match_stats(match_id: str):
    return faceit_get(f"{FACEIT_API}/matches/{match_id}/stats", default=None)


def to_local(ts: int, tz_name: str) -> str:
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.utc
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%m/%d/%Y, %I:%M %p")


def pick_map_image(map_name: str) -> str:
    # map_name like 'de_mirage'
    if not map_name:
        return "unknown.png"
    code = map_name.replace("de_", "").lower()
    known = {
        "mirage": "mirage.png",
        "inferno": "inferno.png",
        "dust2": "dust2.png",
        "overpass": "overpass.png",
        "vertigo": "vertigo.png",
        "nuke": "nuke.png",
        "train": "train.png",
        "ancient": "ancient.png",
        "anubis": "anubis.png",
    }
    return known.get(code, "unknown.png")


@app.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    players: str = Query("", description="Comma separated nicknames"),
    limit: int = Query(5, ge=1, le=20),
):
    """
    网页主页：输入多个玩家昵称，查询每人最近 N 场，并显示
    - Date（按选手本地时区）
    - Score（绿=赢，红=输）
    - Map（小图标+名字）
    - K/A/D
    - Rating（存在就显示，不存在用 - ）
    并限制每行最多 5 列。
    """
    # 解析玩家输入
    nicknames = [x.strip() for x in (players or "").split(",") if x.strip()]
    if not nicknames:
        nicknames = DEFAULT_PLAYERS

    cards = []  # 每个选手的卡片数据

    for nick in nicknames:
        p = get_player_by_nickname(nick)
        if not p or p.get("games", {}).get("cs2") is None:
            cards.append({"nickname": nick, "error": True, "matches": []})
            continue

        player_id = p.get("player_id")
        tz = p.get("settings", {}).get("timezone", "UTC")
        faceit_url = f"https://www.faceit.com/en/players/{nick}"

        hist = get_player_history(player_id, limit=limit)
        items = hist.get("items", []) if hist else []

        parsed_matches = []
        for it in items:
            match_id = it.get("match_id")
            started_at = it.get("started_at", 0)
            local_time = to_local(started_at, tz)

            # 调 stats 端点拿详细比分、地图、每人数据
            stats = get_match_stats(match_id)
            score = "-"
            map_name = "-"
            kd = (0, 0, 0)
            rating = "-"
            penta = 0

            if stats and stats.get("rounds"):
                # round 0 信息
                rd = stats["rounds"][0]
                round_stats = rd.get("round_stats", {})
                score = round_stats.get("Score", "-")
                map_name = round_stats.get("Map", "-")
                # 团队里找到这个选手
                for t in rd.get("teams", []):
                    for pl in t.get("players", []):
                        # 比对 player_id 或 nickname
                        if pl.get("player_id") == player_id or pl.get("nickname") == nick:
                            ps = pl.get("player_stats", {})
                            k = int(ps.get("Kills", "0"))
                            a = int(ps.get("Assists", "0"))
                            d = int(ps.get("Deaths", "0"))
                            kd = (k, a, d)
                            # HLTV rating（Faceit stats 里通常叫 "Rating" / "HLTV Rating"）
                            rating = ps.get("Rating") or ps.get("HLTV Rating") or "-"
                            try:
                                penta = int(ps.get("Penta Kills", "0"))
                            except Exception:
                                penta = 0
                            break

            # 胜负，用 score 的格式 "13 / 11" 判断
            res = "Loss"
            try:
                left, right = [int(x.strip()) for x in score.split("/")]
                res = "Win" if left > right else "Loss"
            except Exception:
                pass

            parsed_matches.append(
                {
                    "match_id": match_id,
                    "time_text": local_time,
                    "score": score,
                    "result": res,
                    "map": map_name,
                    "map_img": f"/static/maps/{pick_map_image(map_name)}",
                    "k": kd[0],
                    "a": kd[1],
                    "d": kd[2],
                    "rating": rating,
                    "penta": penta,
                    "faceit_match": f"https://www.faceit.com/en/cs2/room/{match_id}",
                }
            )

        cards.append(
            {
                "nickname": p.get("nickname", nick),
                "faceit_url": faceit_url,
                "matches": parsed_matches,
                "error": False,
            }
        )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "cards": cards,
            "limit": limit,
            "players_str": ", ".join(nicknames),
        },
    )


@app.get("/health")
async def health():
    return {"ok": True, "service": "protracker", "version": "0.1.1"}


@app.get("/version")
async def version_redirect():
    return RedirectResponse(url="/health")
