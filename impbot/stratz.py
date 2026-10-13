"""Async STRATZ GraphQL client; never substitute missing IMP with zero."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict
from typing import Any

import aiohttp

from .core import DEFAULT_MATCH_COUNT, MAX_MATCH_COUNT, Match, PlayerHistory, UserError, validate_match_count

LOGGER = logging.getLogger(__name__)
ENDPOINT = "https://api.stratz.com/graphql"
QUERY = """
query RecentImp($steamAccountId: Long!, $take: Int!) {
  player(steamAccountId: $steamAccountId) {
    steamAccount { name }
    matches(request: { take: $take, orderBy: DESC, minImp: -32768 }) {
      id
      startDateTime
      players { steamAccountId imp }
    }
  }
}
"""


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value) if int(value) > 0 else None
    return None


def parse_history(payload: dict, account_id: int, match_count: int = DEFAULT_MATCH_COUNT) -> PlayerHistory:
    match_count = validate_match_count(match_count)
    data = payload.get("data")
    if not isinstance(data, dict) or "player" not in data:
        raise UserError("STRATZ 返回的数据格式异常，请稍后重试。")
    player = data["player"]
    if player is None:
        return PlayerHistory(account_id, f"玩家 {account_id}", (), match_count)
    if not isinstance(player, dict) or "matches" not in player:
        raise UserError("STRATZ 返回的数据格式异常，请稍后重试。")
    steam = player.get("steamAccount") or {}
    name = steam.get("name") if isinstance(steam, dict) else None
    if player["matches"] is None:
        return PlayerHistory(account_id, str(name or f"玩家 {account_id}"), (), match_count)
    if not isinstance(player["matches"], list):
        raise UserError("STRATZ 返回的比赛列表格式异常，请稍后重试。")
    matches: list[Match] = []
    seen: set[int] = set()
    for raw in player["matches"]:
        if not isinstance(raw, dict):
            raise UserError("STRATZ 比赛记录格式异常，请稍后重试。")
        match_id = _positive_int(raw.get("id"))
        start_time = _positive_int(raw.get("startDateTime"))
        if match_id is None or start_time is None:
            raise UserError("STRATZ 比赛记录缺少 ID 或时间，无法确认最近对局。")
        if match_id in seen:
            continue
        seen.add(match_id)
        participants = raw.get("players")
        if not isinstance(participants, list):
            raise UserError("STRATZ 比赛记录缺少玩家列表，请稍后重试。")
        target = next(
            (p for p in participants if isinstance(p, dict)
             and _positive_int(p.get("steamAccountId")) == account_id),
            None,
        )
        score = target.get("imp") if target is not None else None
        if score is not None and (
            isinstance(score, bool) or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            raise UserError("STRATZ 返回了无效的 IMP 分数，请稍后重试。")
        matches.append(Match(match_id, start_time, float(score) if score is not None else None))
    # The request takes the API's recent window; sort that window for presentation.
    matches.sort(key=lambda match: (match.start_time, match.id), reverse=True)
    return PlayerHistory(account_id, str(name or f"玩家 {account_id}"),
                         tuple(matches[:match_count]), match_count)


def parse_valid_history(payload: dict, account_id: int, match_count: int = DEFAULT_MATCH_COUNT) -> PlayerHistory:
    match_count = validate_match_count(match_count)
    history = parse_history(payload, account_id, MAX_MATCH_COUNT)
    # The server applies minImp across the full history before take, so nulls
    # do not consume a requested slot. Short's lowest value preserves negatives
    # and zero. This additional filter also handles offline raw-data fixtures.
    valid_matches = tuple(match for match in history.matches if match.imp is not None)
    return PlayerHistory(history.account_id, history.name, valid_matches[:match_count], match_count)


class StratzClient:
    def __init__(self, session: aiohttp.ClientSession, token: str, cache_ttl: int = 60):
        self.session = session
        self.token = token.removeprefix("Bearer ").strip()
        self.cache_ttl = cache_ttl
        self._cache: OrderedDict[tuple[int, int], tuple[float, PlayerHistory]] = OrderedDict()
        self._pending: dict[tuple[int, int], asyncio.Task] = {}
        self._slots = asyncio.Semaphore(3)

    async def fetch_history(self, account_id: int, match_count: int = DEFAULT_MATCH_COUNT) -> PlayerHistory:
        match_count = validate_match_count(match_count)
        cache_key = (account_id, match_count)
        cached = self._cache.get(cache_key)
        if cached is not None and time.monotonic() - cached[0] < self.cache_ttl:
            self._cache.move_to_end(cache_key)
            return cached[1]
        if cache_key in self._pending:
            return await asyncio.shield(self._pending[cache_key])
        task = asyncio.create_task(self._fetch_and_cache(account_id, match_count))
        self._pending[cache_key] = task
        try:
            return await task
        finally:
            self._pending.pop(cache_key, None)

    async def _fetch_and_cache(self, account_id: int, match_count: int) -> PlayerHistory:
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=5)
        except asyncio.TimeoutError as exc:
            raise UserError("当前查询较多，请过几秒再试。") from exc
        try:
            history = parse_valid_history(await self._request(account_id, match_count), account_id, match_count)
        finally:
            self._slots.release()
        cache_key = (account_id, match_count)
        self._cache[cache_key] = (time.monotonic(), history)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > 512:
            self._cache.popitem(last=False)
        return history

    async def _request(self, account_id: int, match_count: int) -> dict:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "STRATZ_API",
            "Accept": "application/json",
        }
        try:
            async with self.session.post(
                ENDPOINT,
                json={"query": QUERY, "variables": {"steamAccountId": account_id, "take": match_count}},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=25),
                allow_redirects=False,
            ) as response:
                if response.status in {401, 403}:
                    raise UserError("STRATZ 拒绝访问，请让管理员检查 API token、权限或运行网络（401/403）。")
                if response.status == 429:
                    raise UserError("STRATZ 查询额度已用完或请求太频繁，请稍后再试（429）。")
                if response.status != 200:
                    LOGGER.warning("STRATZ HTTP status %d", response.status)
                    raise UserError(f"STRATZ 暂时无法提供数据（HTTP {response.status}），请稍后再试。")
                try:
                    payload = await response.json()
                except (ValueError, aiohttp.ContentTypeError) as exc:
                    raise UserError("STRATZ 返回了非 JSON 数据，可能被网络防护拦截，请稍后重试。") from exc
        except asyncio.TimeoutError as exc:
            raise UserError("STRATZ 查询超时，请稍后再试。") from exc
        except aiohttp.ClientError as exc:
            raise UserError("无法连接 STRATZ，请检查网络后重试。") from exc
        if not isinstance(payload, dict):
            raise UserError("STRATZ 返回的数据格式异常，请稍后重试。")
        # Never derive a score from partial data when GraphQL reported errors.
        if payload.get("errors"):
            LOGGER.warning("STRATZ reported GraphQL errors (response omitted)")
            raise UserError("STRATZ 查询未成功，请让管理员运行 python bot.py --check-api 好友代码 检查接口和 token。")
        return payload
