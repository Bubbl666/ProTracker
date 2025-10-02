# --- 替换 app/main.py 里 matches_with_stats 的实现（或直接覆盖整个函数） ---
@app.get("/matches/with_stats")
def matches_with_stats(player_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    require_key()

    limit = max(1, min(limit, 20))
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

    def _to_int(v):
        try:
            return int(v)
        except Exception:
            try:
                return int(float(v))
            except Exception:
                return 0

    def _to_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    out: List[Dict[str, Any]] = []

    for it in items:
        mid = it.get("match_id")
        map_name = None
        score = None
        player_stat: Dict[str, Any] | None = None
        stats_unavailable_reason = None

        # 2) 拉统计
        try:
            stat = requests.get(
                f"{FACEIT_BASE}/matches/{mid}/stats",
                headers=HEADERS,
                timeout=20,
            )
            if stat.status_code == 200:
                s = stat.json()
                rounds = s.get("rounds") or []
                if rounds:
                    # Map / Score 在 round_stats 里
                    rs = rounds[0].get("round_stats") or {}
                    map_name = rs.get("Map") or rs.get("Map Name") or map_name
                    # 比如 "16 / 9" 或 "13 / 11"
                    score = rs.get("Score") or rs.get("Final Score") or score

                    # 找到该玩家
                    players = rounds[0].get("players", [])
                    for pl in players:
                        if pl.get("player_id") == player_id:
                            # 兼容多版本字段
                            kd = pl.get("k/d") or pl.get("K/D Ratio") or pl.get("kd")
                            kr = pl.get("k/r") or pl.get("K/R Ratio") or pl.get("kr")
                            adr = pl.get("adr") or pl.get("ADR")
                            hs  = pl.get("hs") or pl.get("Headshots %") or pl.get("Headshots")

                            player_stat = {
                                "nickname": pl.get("nickname"),
                                "kills": _to_int(pl.get("kills") or pl.get("Kills")),
                                "deaths": _to_int(pl.get("deaths") or pl.get("Deaths")),
                                "assists": _to_int(pl.get("assists") or pl.get("Assists")),
                                "hs": _to_int(hs),
                                "kd": _to_float(kd),
                                "kr": _to_float(kr),
                                "adr": _to_float(adr),
                                "result": pl.get("result") or pl.get("Result"),
                                "team": pl.get("team"),
                            }
                            break
            elif stat.status_code in (403, 404):
                stats_unavailable_reason = "not_ready_or_hidden"
            else:
                stats_unavailable_reason = f"http_{stat.status_code}"
        except requests.RequestException:
            stats_unavailable_reason = "network_error"

        # 3) 如果依然没有 Map/Score，尝试从历史项里兜底（有些历史会带结果）
        if not map_name:
            # 有的历史对象里会塞到 results / teams 的自定义字段，这里做一次兜底解析
            results = it.get("results") or {}
            # 某些房型会把得分塞进 'score' 或自定义里，这里尽力而为
            score = score or results.get("score") or results.get("Score")
            # Map 通常只有 stats 里才有，拿不到就保持 None

        out.append(
            {
                "match_id": mid,
                "game": it.get("game_id"),
                "started_at": it.get("started_at"),
                "finished_at": it.get("finished_at"),
                "map": map_name,
                "score": score,
                "teams": it.get("teams", {}),
                "player": player_stat,              # 可能为 None（无公开统计）
                "stats_unavailable_reason": stats_unavailable_reason,
            }
        )

    return out
