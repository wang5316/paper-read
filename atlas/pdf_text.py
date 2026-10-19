"""PDF 取文本:下载 + 逐页抽取 + 选页。

只允许 arxiv.org,只接受 https 与体积上限;没有 PyMuPDF 时不是崩溃,而是
抛 NoPdf,让上层把这篇记为「取不到全文」并跳过 —— 站点照常构建。
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MAX_PDF_BYTES = 60_000_000
ALLOWED_HOSTS = {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}


class NoPdf(RuntimeError):
    """这篇没有可用全文(PDF 缺失/不合法/解析失败)。"""


class PdfError(NoPdf):
    """PDF 存在但解析失败。"""


def download_pdf(url, timeout=120, opener=None, max_bytes=MAX_PDF_BYTES) -> bytes:
    text = str(url or "").strip()
    if not text:
        raise NoPdf("empty pdf url")
    parts = urllib.parse.urlsplit(text)
    if parts.scheme != "https":
        raise NoPdf(f"pdf url must be https: {text}")
    if parts.hostname not in ALLOWED_HOSTS:
        raise NoPdf(f"pdf host not allowed: {parts.hostname}")
    request = urllib.request.Request(text, headers={"Accept": "application/pdf"})
    opener = opener or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NoPdf(f"pdf download failed: {exc}") from exc
    if len(payload) > max_bytes:
        raise NoPdf("pdf exceeds size limit")
    if not payload.startswith(b"%PDF"):
        raise NoPdf("response is not a PDF")
    return payload


def load_pages(pdf_bytes, max_pages=40):
    """返回 [(页码从 1 开始, 文本)]。"""
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise NoPdf("PyMuPDF (fitz) is not installed; pip install -r requirements.txt") from exc
    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - PyMuPDF 抛的异常类型很杂
        raise PdfError(f"cannot open pdf: {exc}") from exc
    try:
        pages = []
        for index in range(min(len(document), max_pages)):
            text = " ".join((document.load_page(index).get_text("text") or "").split())
            if text:
                pages.append((index + 1, text))
        if not pages:
            raise PdfError("pdf has no extractable text (scanned?)")
        return pages
    finally:
        document.close()


def select_pages(pages, max_chars=48_000, first=2):
    """首页若干页必选,其余按「结果/实验」线索挑,总字符数封顶。"""
    if not pages:
        return []
    keys = ("result", "experiment", "evaluat", "ablation", "table", "improv", "outperform")
    chosen = list(pages[:first])
    rest = []
    for page_no, text in pages[first:]:
        score = sum(1 for key in keys if key in text.lower())
        if score:
            rest.append((score, page_no, text))
    rest.sort(key=lambda item: (-item[0], item[1]))
    budget = max_chars - sum(len(text) for _, text in chosen)
    for _score, page_no, text in rest:
        if budget <= 0:
            break
        if len(text) > budget:
            continue
        chosen.append((page_no, text))
        budget -= len(text)
    return sorted(chosen)


def cache_path(cache_dir, key) -> Path:
    return Path(cache_dir) / f"{key}.pdf"


def read_cache(cache_dir, key):
    path = cache_path(cache_dir, key)
    if path.exists():
        payload = path.read_bytes()
        if payload.startswith(b"%PDF"):
            return payload
    return None


def write_cache(cache_dir, key, payload) -> None:
    path = cache_path(cache_dir, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
