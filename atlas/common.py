"""公共工具:路径、时间、原子写、标识符归一、配置校验。

全部使用标准库;这一层不允许出现网络请求,方便单测。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

ARXIV_ABS = "https://arxiv.org/abs/"
ARXIV_PDF = "https://arxiv.org/pdf/"

_TRAILING_VERSION = re.compile(r"v(\d+)$", re.I)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
_ARXIV_NEW = re.compile(r"(\d{4}\.\d{4,5})(?:v(\d+))?$")
_ARXIV_OLD = re.compile(r"([a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v(\d+))?$")


# --------------------------------------------------------------------------- 时间


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    """ISO-8601 UTC,秒级,以 Z 结尾。"""
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_time(value) -> datetime:
    """解析 ISO 字符串;失败抛 ValueError(调用方决定是失败还是忽略)。"""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def days_ago(now: datetime, days: float) -> datetime:
    return now - timedelta(days=days)


# --------------------------------------------------------------------------- JSON


def read_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    text = path.read_text("utf-8")
    if not text.strip():
        return default
    return json.loads(text)


def write_json(path, payload) -> None:
    """原子写:临时文件 + fsync + os.replace,崩溃不留半个 JSON。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def digest(payload) -> str:
    """稳定哈希:字典按键排序,便于做 query_hash / source_hash / 事件键。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------- 标识符


def arxiv_ref(raw):
    """从 `.../abs/2211.15654v3` / `arXiv:2211.15654` 里取 (编号, 版本号)。"""
    text = str(raw or "").strip()
    if not text:
        return "", 0
    text = text.rstrip("/")
    for pattern in (_ARXIV_NEW, _ARXIV_OLD):
        hit = pattern.search(text)
        if hit:
            version = int(hit.group(2) or 0)
            return hit.group(1), version
    return "", 0


def strip_version(raw) -> str:
    aid, _ = arxiv_ref(raw)
    if aid:
        return aid
    return _TRAILING_VERSION.sub("", str(raw or "").strip())


def doi_id(raw):
    """返回规范化 DOI,非法则空串。"""
    text = str(raw or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    return text if _DOI.match(text) else ""


def identity_keys(paper) -> set:
    """强标识键集合:arxiv:<id>(去版本) 与 doi:<doi>;arXiv DOI 反推 arXiv 键。"""
    keys = set()
    identifiers = paper.get("identifiers") or {}
    for raw in identifiers.get("arxiv") or []:
        aid = strip_version(raw)
        if aid:
            keys.add("arxiv:" + aid)
    for raw in identifiers.get("doi") or []:
        doi = doi_id(raw)
        if not doi:
            continue
        keys.add("doi:" + doi)
        hit = _ARXIV_DOI.match(doi)
        if hit:
            aid = strip_version(hit.group(1))
            if aid:
                keys.add("arxiv:" + aid)
    return keys


def title_key(title) -> str:
    """题名归一:只留字母数字,用于「同题名但身份未证实」的告警。"""
    text = str(title or "").lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def safe_url(raw) -> str:
    """出站链接白名单:只放行 http(s),拒绝带用户名或非 ASCII 主机的地址。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    if not parts.hostname or "@" in parts.netloc:
        return ""
    if any(ord(char) > 127 for char in parts.netloc):
        return ""
    return text


# ------------------------------------------------------------------------- 配置


class ConfigError(ValueError):
    """配置不合法:直接失败,不要带着坏配置跑抓取。"""


DEFAULT_CLASSIFY = {
    "scope": {"id": "3d", "name": "3D", "patterns": [r"\b3[ -]?d\b", r"point[ -]cloud"]},
    "task": {
        "primary": {"id": "semantic", "name": "语义分割", "patterns": [r"semantic[ -]?segmentation"]},
        "adjacent": {"id": "adjacent", "name": "邻近任务", "patterns": [r"instance[ -]?segmentation"]},
        "any": {"id": "segmentation", "name": "分割", "patterns": [r"segment"]},
    },
    "tracks": [],
    "methods": [],
}


def load_config(path=None) -> dict:
    """读 config.json 并做硬校验;宁可启动即失败,也不要跑完半轮才出错。"""
    path = Path(path) if path else ROOT / "config.json"
    config = read_json(path, None)
    if not isinstance(config, dict):
        raise ConfigError(f"config not found or not an object: {path}")

    arxiv = config.setdefault("arxiv", {})
    page_size = int(arxiv.get("page_size", 50))
    if not 1 <= page_size <= 2000:
        raise ConfigError("arxiv.page_size must be within 1..2000")
    interval = float(arxiv.get("request_interval_seconds", 5.0))
    if interval < 3.0:
        raise ConfigError("arxiv.request_interval_seconds must be >= 3 (arXiv rate limit)")
    if int(arxiv.get("max_pages_per_query", 12)) < 1:
        raise ConfigError("arxiv.max_pages_per_query must be >= 1")
    batch = int(arxiv.get("revision_batch_size", 40))
    if not 0 <= batch <= 100:
        raise ConfigError("arxiv.revision_batch_size must be within 0..100")

    seen = set()
    for query in arxiv.get("queries") or []:
        qid = str(query.get("id") or "").strip()
        if not qid:
            raise ConfigError("every arxiv query needs a non-empty id")
        if qid in seen:
            raise ConfigError(f"duplicate query id: {qid}")
        if not str(query.get("query") or "").strip():
            raise ConfigError(f"query {qid} has an empty search expression")
        seen.add(qid)
    if not seen:
        raise ConfigError("config.arxiv.queries is empty")

    classify = config.setdefault("classify", DEFAULT_CLASSIFY)
    if not classify.get("scope", {}).get("patterns"):
        raise ConfigError("classify.scope.patterns is required")
    for key in ("tracks", "methods"):
        ids = [str(item.get("id") or "") for item in classify.get(key) or []]
        if any(not item for item in ids):
            raise ConfigError(f"every classify.{key} entry needs an id")
        if len(set(ids)) != len(ids):
            raise ConfigError(f"duplicate id in classify.{key}")

    # 专题只能引用 classify 里真实存在的标签 —— 上游项目正是在这里对不上号。
    track_ids = {str(item["id"]) for item in classify.get("tracks") or []}
    method_ids = {str(item["id"]) for item in classify.get("methods") or []}
    topic_ids = set()
    for group in config.get("topics") or []:
        for topic in group.get("items") or []:
            tid = str(topic.get("id") or "").strip()
            if not tid:
                raise ConfigError("every topic needs an id")
            if tid in topic_ids:
                raise ConfigError(f"duplicate topic id: {tid}")
            topic_ids.add(tid)
            match = topic.get("match") or {}
            unknown_tracks = set(match.get("tracks") or []) - track_ids
            unknown_methods = set(match.get("methods") or []) - method_ids
            if unknown_tracks:
                raise ConfigError(
                    f"topic {tid} references unknown tracks: {sorted(unknown_tracks)}"
                )
            if unknown_methods:
                raise ConfigError(
                    f"topic {tid} references unknown methods: {sorted(unknown_methods)}"
                )
    return config
