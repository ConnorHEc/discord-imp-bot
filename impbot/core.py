"""Pure scoring and rule configuration, independent of Discord and networking."""

from __future__ import annotations

import json
import math
import re
import string
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from urllib.parse import urlparse

STEAM_ID_BASE = 76561197960265728
MAX_ACCOUNT_ID = 2**32 - 1
DEFAULT_MATCH_COUNT = 20
MAX_MATCH_COUNT = 100
MAX_IMAGE_BYTES = 8 * 1024 * 1024
TEMPLATE_FIELDS = {"name", "friend_code", "average", "valid_count", "match_count"}


class UserError(Exception):
    """A failure with a safe message that can be shown to the Discord user."""


class ConfigError(ValueError):
    pass


def validate_match_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_MATCH_COUNT:
        raise UserError(f"场数必须是 1–{MAX_MATCH_COUNT} 之间的整数。")
    return value


def parse_friend_code(value: str) -> int:
    raw = value.strip()
    if not re.fullmatch(r"[0-9]{1,20}", raw):
        raise UserError("好友代码应为一串数字，例如 123456789；也支持 SteamID64。")
    number = int(raw)
    if STEAM_ID_BASE < number <= STEAM_ID_BASE + MAX_ACCOUNT_ID:
        number -= STEAM_ID_BASE
    if not 1 <= number <= MAX_ACCOUNT_ID:
        raise UserError("好友代码超出范围。请复制 Dota 2 的数字好友代码或 SteamID64。")
    return number


@dataclass(frozen=True)
class Match:
    id: int
    start_time: int
    imp: float | None


@dataclass(frozen=True)
class PlayerHistory:
    account_id: int
    name: str
    matches: tuple[Match, ...]
    requested_count: int = DEFAULT_MATCH_COUNT


@dataclass(frozen=True)
class Summary:
    history: PlayerHistory
    average: float | None
    valid_count: int

    @property
    def missing_count(self) -> int:
        return len(self.history.matches) - self.valid_count


def summarize(history: PlayerHistory) -> Summary:
    values = [match.imp for match in history.matches if match.imp is not None]
    return Summary(history, fmean(values) if values else None, len(values))


@dataclass(frozen=True)
class Rule:
    minimum: float | None
    title: str
    text: str
    image: str | None
    include_min: bool = True


def local_image(root: Path, reference: str) -> Path:
    path = (root / reference).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ConfigError("本地图片必须位于机器人目录内，请放入 assets/。")
    if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        raise ConfigError("本地图片只支持 PNG/JPG/JPEG/GIF/WEBP。")
    if not path.is_file():
        raise ConfigError(f"找不到配置的图片：{reference}")
    if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
        raise ConfigError(f"图片为空或超过 8 MiB：{reference}")
    return path


def is_remote_image(reference: str) -> bool:
    return reference.lower().startswith("https://")


def _read_config(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConfigError("无法读取 config.json，请检查文件及 JSON 格式。") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.json 必须是 JSON 对象。")
    return raw


def _validate_image(root: Path, image: str | None) -> None:
    if image is None:
        return
    if not isinstance(image, str) or not image:
        raise ConfigError("image 必须是 null、相对路径或 HTTPS 图片直链。")
    if is_remote_image(image):
        parsed = urlparse(image)
        if not parsed.hostname or parsed.username or parsed.password or len(image) > 2000:
            raise ConfigError("图片直链格式不正确。")
    else:
        local_image(root, image)


def load_unavailable(path: Path) -> tuple[str, str | None]:
    raw = _read_config(path).get("unavailable", {"text": "神秘嘉宾来袭", "image": None})
    if not isinstance(raw, dict):
        raise ConfigError("unavailable 必须是 JSON 对象。")
    text, image = raw.get("text"), raw.get("image")
    if not isinstance(text, str) or not 1 <= len(text) <= 1500:
        raise ConfigError("unavailable.text 必须为 1–1500 字符的字符串。")
    _validate_image(path.parent, image)
    return text, image


def load_rules(path: Path) -> tuple[Rule, ...]:
    raw = _read_config(path)
    if not isinstance(raw.get("rules"), list) or not raw["rules"]:
        raise ConfigError("config.json 需要非空 rules 数组。")
    result: list[Rule] = []
    previous = -math.inf
    for index, entry in enumerate(raw["rules"]):
        if not isinstance(entry, dict):
            raise ConfigError("每条规则必须是 JSON 对象。")
        minimum = entry.get("min")
        if index == 0:
            if minimum is not None:
                raise ConfigError("第一条规则的 min 必须是 null，覆盖最低分。")
        else:
            if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
                raise ConfigError("除第一条外，min 必须是有限数字。")
            if not math.isfinite(minimum) or minimum <= previous:
                raise ConfigError("min 必须严格递增，不能重复。")
            previous = minimum
        include_min = entry.get("include_min", True)
        if not isinstance(include_min, bool):
            raise ConfigError("include_min 必须为 true 或 false。")
        title, body, image = entry.get("title"), entry.get("text"), entry.get("image")
        if not isinstance(title, str) or not 1 <= len(title) <= 200:
            raise ConfigError("title 必须为 1–200 字符的字符串。")
        if not isinstance(body, str) or not 1 <= len(body) <= 1500:
            raise ConfigError("text 必须为 1–1500 字符的字符串。")
        try:
            fields = list(string.Formatter().parse(body))
        except ValueError as exc:
            raise ConfigError("文案中的大括号不匹配。") from exc
        for _, field, spec, conversion in fields:
            if field is not None and (field not in TEMPLATE_FIELDS or spec or conversion):
                raise ConfigError("文案只支持简单占位符：" + ", ".join(sorted(TEMPLATE_FIELDS)))
        _validate_image(path.parent, image)
        result.append(Rule(minimum, title, body, image, include_min))
    return tuple(result)


def select_rule(rules: tuple[Rule, ...], average: float) -> Rule:
    # Use the unrounded average; the displayed two decimals never affect selection.
    for rule in reversed(rules):
        if (rule.minimum is None or average > rule.minimum
                or (rule.include_min and average == rule.minimum)):
            return rule
    raise ConfigError("规则没有覆盖这个分数。")


def format_score(value: float) -> str:
    # Avoid displaying negative zero after rounding.
    return f"{0.0 if abs(value) < 0.005 else value:+.2f}"


def render_text(summary: Summary, rule: Rule) -> str:
    if summary.average is None:
        raise ValueError("Cannot render a rating without IMP values")
    result = rule.text.format(
        name=summary.history.name[:100],
        friend_code=summary.history.account_id,
        average=format_score(summary.average),
        valid_count=summary.valid_count,
        match_count=len(summary.history.matches),
    )
    if len(result) > 3500:
        raise ConfigError("占位符替换后的文案过长，请缩短到 3500 字符以内。")
    return result
