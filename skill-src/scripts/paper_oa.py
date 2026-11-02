#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OA 论文检索 / OA 全文定位 / PDF 下载归档（纯标准库，无需 pip 安装）。

给 DSH skill「campus-library-papers」使用。核心原则：
- 只走合法 OA 渠道（Unpaywall / OpenAlex / Semantic Scholar / arXiv / Europe PMC），
  不访问 Sci-Hub 等盗版镜像，不绕过付费墙。
- 所有命令默认输出结构化 JSON 到 stdout（stderr 只放警告），便于 agent 解析后决定下一步。
- 下载的 PDF 先校验 %PDF 魔数，再落到 papers/ 目录，并写 manifest.jsonl 去重。

子命令：
  search      "关键词或标题"               检索候选论文（OpenAlex/Crossref/S2/arXiv）
  oa          <DOI|arxiv:ID|pmid:ID|PMCID> 解析 OA 全文位置（不下载）
  fetch       <DOI|arxiv:ID|pmid:ID|URL>   下载最佳 OA PDF 到 papers/，写清单
  batch       <文件，每行一个 DOI/标识>     批量 fetch（并发 + 断点续跑 + 状态文件）
  organize    <目录>                       重建 manifest.jsonl 对应的 index.csv
  import-file <本地 PDF>                   核验并登记浏览器/人工已下载的 PDF（不联网，默认单行 JSON）

中文 DOI：10.16383/、10.19721/、10.15918/、10.3969/ 等前缀的 DOI 不被
Crossref/OpenAlex/Semantic Scholar 收录，工具改走 chndoi.org 多重解析 -> doi.org 回退，
再从落地页提取 PDF/全文链接；解析失败报 source_unavailable（不是 no_oa），不伪造官网 URL。

常用（所有命令默认输出 JSON）：
  python paper_oa.py search "graph neural network drug discovery" --limit 8
  python paper_oa.py fetch 10.1038/nature12373
  python paper_oa.py batch dois.txt --out papers/2025-综述 --workers 3 --resume
  python paper_oa.py import-file 已下载.pdf --doi 10.16383/j.aas.c220001 \
      --title "题名" --name "自动化学报_2024" --version-type published --source-type publisher

退出码（所有子命令统一，脚本化请按此判断）：
  0  成功/已保存：检索完成、OA 位置解析成功、PDF 已下载并核验（downloaded / exists / ok）
  1  参数或配置错误：命令行参数非法、配置缺失或无效、标识符无法识别（invalid_identifier），
     以及致命错误（输入文件不可读、断点文件不可写等）
  2  需要用户/没有合法 OA：no_oa、source_unavailable（元数据源不可用，不能断言没有 OA）、
     organize 找不到清单
  3  部分完成/待核验/下载失败：saved_pending_verification、download_failed、batch 存在未完成项

batch 断点文件（--state，默认 <out>/batch-state.jsonl）每行一条 JSON：
  {"identifier": ..., "status": ..., "path": ..., "error": ..., "timestamp": ...}
  --resume 跳过状态为 downloaded / exists / saved_pending_verification 的条目；
  --workers N（默认 3，1=串行）用 concurrent.futures 并发，manifest 追加加锁，
  同一 host 的请求起始时间由全局锁保证最小间隔（config.request_interval_seconds）。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_lib
from pdf_check import check_bytes, check_file, verified
import json
import os
import math
import re
import sys
import threading
import time
import unicodedata
import shutil
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib import error, parse, request

try:  # Windows 控制台默认 GBK，统一 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

DEFAULT_USER_AGENT = "campus-library-papers/1.0 (OA paper downloader; contact: {email})"
DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
ARXIV_DOI_RE = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)
ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(v\d+)?$|^([a-z\-]+/\d{7})(v\d+)?$", re.I)

DEFAULT_CONFIG = {
    "email": "",                      # Unpaywall/Crossref 礼貌联系方式，必填
    "download_dir": "papers",
    "timeout_seconds": 30,
    "request_interval_seconds": 1.0,  # Generic interval; arXiv enforced separately below
    "max_pdf_mb": 120,
    "sources": ["openalex", "crossref", "semanticscholar", "arxiv"],
    "user_agent": DEFAULT_USER_AGENT,
}

# 中文 DOI 前缀：这些注册机构的 DOI 一般不在 Crossref/OpenAlex/S2 收录，
# 需要走 chndoi.org 多重解析或期刊官网。可通过 config.chinese_doi_prefixes 扩展/覆盖。
CHINESE_DOI_PREFIXES = ("10.16383", "10.19721", "10.15918", "10.3969")

# ---------------------------------------------------------------------------
# 统一 CLI 退出码（见模块 docstring）
# ---------------------------------------------------------------------------
EXIT_OK = 0            # 成功/已保存
EXIT_USAGE = 1         # 参数或配置错误 / 致命错误
EXIT_UNAVAILABLE = 2   # 需要用户 / 无合法 OA（no_oa / source_unavailable）
EXIT_PARTIAL = 3       # 部分完成 / 待核验 / 下载失败

_STATUS_EXIT = {
    "ok": EXIT_OK,
    "downloaded": EXIT_OK,
    "exists": EXIT_OK,
    "no_oa": EXIT_UNAVAILABLE,
    "source_unavailable": EXIT_UNAVAILABLE,
    "empty": EXIT_UNAVAILABLE,
    "partial": EXIT_PARTIAL,
    "saved_pending_verification": EXIT_PARTIAL,
    "download_failed": EXIT_PARTIAL,
    "error": EXIT_PARTIAL,
    "missing_files": EXIT_PARTIAL,
    "invalid_identifier": EXIT_USAGE,
    "invalid_argument": EXIT_USAGE,
    "config_error": EXIT_USAGE,
    "invalid_pdf": EXIT_USAGE,
}


def exit_code_for(status: str) -> int:
    """把结果状态映射为统一退出码（未知状态按「下载失败」处理）。"""
    return _STATUS_EXIT.get(str(status or "").strip().lower(), EXIT_PARTIAL)


# batch --resume 认为「已完成、可跳过」的状态
BATCH_DONE_STATUSES = ("downloaded", "exists", "saved_pending_verification")
BATCH_DEFAULT_WORKERS = 3

# 并发 batch 的线程安全：请求限速 / manifest+state 追加 / stderr 进度
_RATE_LOCK = threading.Lock()
_APPEND_LOCK = threading.Lock()
_STDERR_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    cfg["_source_errors"] = []
    if path and not Path(path).is_file():
        raise ValueError("指定配置文件不存在")
    candidates = []
    if path:
        candidates.append(Path(path))
    env_path = os.environ.get("CAMPUS_PAPERS_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path(__file__).resolve().parent.parent / "config" / "config.json")
    candidates.append(Path.home() / ".dsh" / "campus-papers" / "config.json")
    for p in candidates:
        try:
            if p.is_file():
                # utf-8-sig：兼容 Windows 记事本/PowerShell 写入的 BOM
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("配置必须为 JSON 对象")
                cfg.update({k: v for k, v in data.items() if v is not None})
                break
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"配置无法读取: {p.name}") from exc
    for name in ("timeout_seconds", "max_pdf_mb", "request_interval_seconds"):
        value = cfg.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 必须为正数")
    return cfg


def public_url(url: str) -> str:
    parts = parse.urlsplit(url or "")
    if parts.scheme not in ("http", "https"):
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if parts.port:
        host += ":" + str(parts.port)
    return parse.urlunsplit((parts.scheme, host, parts.path, "", ""))


def ua(cfg: dict) -> str:
    email = cfg.get("email") or "unknown@example.com"
    return (cfg.get("user_agent") or DEFAULT_USER_AGENT).format(email=email)


def request_interval(host: str, cfg: dict) -> float:
    """同一 host 两次请求之间的最小间隔（arXiv 强制 >= 3s）。"""
    host = (host or "").lower()
    interval = float(cfg.get("request_interval_seconds", 1) or 0)
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        interval = max(interval, 3.0)
    return max(interval, 0.0)


def pace_request(cfg: dict, host: str) -> None:
    """线程安全地串行化同一 host 的请求起始时间，保证最小间隔（礼貌限速）。

    并发 batch 时多个 worker 共用同一个锁与 ``cfg['_last_request_at']``，
    因此对同一出版社/API 的连续请求不会挤在一起（P2-5）。
    """
    interval = request_interval(host, cfg)
    with _RATE_LOCK:
        last = cfg.setdefault("_last_request_at", {})
        now = time.monotonic()
        remaining = interval - (now - last.get(host, -float("inf")))
        if remaining > 0:
            time.sleep(remaining)
            now = time.monotonic()
        last[host] = now


def http_get(
    url: str,
    cfg: dict,
    *,
    accept: str = "*/*",
    max_bytes: int | None = None,
    allow_redirects: bool = True,
    retries: int = 2,
) -> dict:
    """GET 并返回 {status, url, headers, body(bytes)}；网络错误抛 RuntimeError。"""
    timeout = float(cfg.get("timeout_seconds", 30))
    headers = {"User-Agent": ua(cfg), "Accept": accept}
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        pace_request(cfg, (parse.urlsplit(url).hostname or "").lower())
        req = request.Request(url, headers=headers, method="GET")
        try:
            if not allow_redirects:
                class NoRedirect(request.HTTPRedirectHandler):
                    def redirect_request(self, req, fp, code, msg, headers, newurl):
                        return None
                opener = request.build_opener(NoRedirect())
                response = opener.open(req, timeout=timeout)
            else:
                response = request.urlopen(req, timeout=timeout)
            with response as resp:
                body = resp.read(max_bytes or 8 * 1024 * 1024)
                return {
                    "status": resp.status,
                    "url": resp.geturl(),
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                    "body": body,
                }
        except error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(max_bytes or 8 * 1024 * 1024)
            except Exception:
                pass
            result = {
                "status": exc.code,
                "url": url,
                "headers": {k.lower(): v for k, v in (exc.headers or {}).items()},
                "body": body,
            }
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = (exc.headers or {}).get("Retry-After", "")
                delay = 1.5 * (attempt + 1)
                if retry_after:
                    # Do not guess HTTP-date delays or wait indefinitely; let the caller retry later.
                    if not str(retry_after).isdigit() or int(retry_after) > 60:
                        return result
                    delay = max(delay, int(retry_after))
                time.sleep(delay)
                last_exc = exc
                continue
            return result
        except (error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise RuntimeError(f"请求失败 {public_url(url)}: {type(exc).__name__}") from exc
    raise RuntimeError(f"请求失败 {public_url(url)}: {type(last_exc).__name__}")


def http_json(url: str, cfg: dict, *, timeout_note: str = "") -> dict | list | None:
    try:
        res = http_get(url, cfg, accept="application/json")
    except RuntimeError as exc:
        cfg.setdefault("_source_errors", []).append({"source": timeout_note or "API", "reason": "network_error"})
        print(f"[warn] {timeout_note or 'API'} 请求失败: {exc}", file=sys.stderr)
        return None
    if res["status"] != 200:
        cfg.setdefault("_source_errors", []).append({"source": timeout_note or "API", "status": res["status"]})
        print(f"[warn] {timeout_note or 'API'} HTTP {res['status']}: {public_url(url)}", file=sys.stderr)
        return None
    try:
        data = json.loads(res["body"].decode("utf-8", "replace"))
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        return data
    except (json.JSONDecodeError, ValueError):
        cfg.setdefault("_source_errors", []).append({"source": timeout_note or "API", "reason": "invalid_json"})
        print(f"[warn] JSON 解析失败: {public_url(url)}", file=sys.stderr)
        return None


def polite_sleep(cfg: dict) -> None:
    time.sleep(float(cfg.get("request_interval_seconds", 0.4)))


def clean_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = " ".join(str(v) for v in value if v)
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_doi(value: str) -> str | None:
    if not value:
        return None
    doi = value.strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.I)
    doi = doi.strip().strip('<>"').rstrip(".,;")
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    # 保留原始大小写（10.48550/arXiv.xxxx 依赖大小写），比较时统一 .lower()
    return doi if DOI_RE.match(doi) else None


def classify_identifier(value: str) -> tuple[str, str]:
    """返回 (kind, normalized)：kind ∈ doi|arxiv|pmid|pmcid|url。"""
    raw = (value or "").strip()
    doi = normalize_doi(raw)
    if doi:
        return "doi", doi
    low = raw.lower()
    if low.startswith("arxiv:"):
        value = raw.split(":", 1)[1].strip()
        return ("arxiv", value) if ARXIV_ID_RE.fullmatch(value) else ("unknown", raw)
    if ARXIV_ID_RE.match(raw):
        return "arxiv", raw
    if low.startswith("pmid:"):
        value = raw.split(":", 1)[1].strip()
        return ("pmid", value) if value.isdigit() else ("unknown", raw)
    if low.startswith("pmcid:"):
        value = raw.split(":", 1)[1].strip().upper().removeprefix("PMC")
        return ("pmcid", "PMC" + value) if value.isdigit() else ("unknown", raw)
    if re.match(r"^PMC\d+$", raw, re.I):
        return "pmcid", raw.upper()
    if raw.startswith(("http://", "https://")):
        return "url", raw
    return "unknown", raw


def safe_filename(text: str, maxlen: int = 80) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\\/:*?\"<>|\r\n\t]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:maxlen].strip(" .") or "untitled"


def doi_slug(doi: str, maxlen: int = 70) -> str:
    """DOI -> 安全文件名片段，例如 10.1234/example(2025) -> 10.1234_example_2025。"""
    slug = re.sub(r"[^0-9A-Za-z._-]+", "_", (doi or "").strip()).strip("._-")
    return slug[:maxlen].strip("._-")


def split_authors(value) -> list[str]:
    """--authors 参数：支持 ``;``/``,``/``、`` 分隔，也接受列表。"""
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"\s*[;；,，、]\s*", str(value or ""))
    return [author for author in (clean_text(item) for item in raw) if author][:20]


def is_chinese_doi(doi: str | None, cfg: dict | None = None) -> bool:
    """判断是否为中文 DOI 注册机构（chndoi.org）前缀。"""
    value = normalize_doi(doi or "") or ""
    if not value:
        return False
    prefixes = CHINESE_DOI_PREFIXES
    if cfg is not None:
        extra = cfg.get("chinese_doi_prefixes")
        if isinstance(extra, list) and extra:
            prefixes = tuple(str(prefix) for prefix in extra)
    low = value.lower()
    return any(low == prefix.lower() or low.startswith(prefix.lower() + "/")
               for prefix in prefixes)


CHINESE_DOI_HINTS = [
    "中文 DOI（10.16383/、10.19721/、10.15918/、10.3969/ 等）通常不被 Crossref/OpenAlex/"
    "Semantic Scholar 收录，本工具对这类 DOI 直接走 chndoi.org 多重解析，失败再回退 doi.org。",
    "自动化学报、机械工程学报、中国公路学报、北京理工大学学报等期刊官网多数提供免费 PDF："
    "在官网按题名检索确认落地页后人工下载，再用 import-file 登记。",
    "解析失败时不要伪造期刊官网 URL：请用题名+作者检索确认，或在校内网络下人工下载全文。",
]


def chinese_doi_hints() -> list[str]:
    return list(CHINESE_DOI_HINTS)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------
def paper_record(**kwargs) -> dict:
    record = {
        "title": "", "year": None, "authors": [], "venue": "", "doi": None,
        "oa": None, "oa_url": None, "citations": None, "source": "", "id": None,
    }
    record.update({k: v for k, v in kwargs.items() if v not in (None, "", [])})
    return record


def search_openalex(query: str, cfg: dict, limit: int) -> list[dict]:
    mailto = cfg.get("email") or ""
    url = (
        "https://api.openalex.org/works?"
        + parse.urlencode({
            "search": query,
            "per-page": str(limit),
            **({"mailto": mailto} if mailto else {}),
        })
    )
    data = http_json(url, cfg, timeout_note="OpenAlex")
    records = []
    for item in (data or {}).get("results", []):
        authors = [
            clean_text((a.get("author") or {}).get("display_name"))
            for a in item.get("authorships", [])
        ][:6]
        oa = item.get("open_access") or {}
        best = item.get("best_oa_location") or {}
        loc = item.get("primary_location") or {}
        records.append(paper_record(
            title=clean_text(item.get("title")),
            year=item.get("publication_year"),
            authors=[a for a in authors if a],
            venue=clean_text((loc.get("source") or {}).get("display_name")),
            doi=normalize_doi(item.get("doi") or ""),
            oa=oa.get("is_oa"),
            oa_url=best.get("pdf_url") or best.get("landing_page_url") or oa.get("oa_url"),
            citations=item.get("cited_by_count"),
            source="openalex",
            id=item.get("id"),
        ))
    polite_sleep(cfg)
    return records


def search_crossref(query: str, cfg: dict, limit: int) -> list[dict]:
    mailto = cfg.get("email") or ""
    url = (
        "https://api.crossref.org/works?"
        + parse.urlencode({
            "query.bibliographic": query,
            "rows": str(limit),
            **({"mailto": mailto} if mailto else {}),
        })
    )
    data = http_json(url, cfg, timeout_note="Crossref")
    records = []
    for item in ((data or {}).get("message") or {}).get("items", []):
        authors = [
            clean_text(f"{a.get('given','')} {a.get('family','')}".strip())
            for a in item.get("author", [])
        ][:6]
        issued = (item.get("issued") or {}).get("date-parts") or [[None]]
        year = issued[0][0] if issued and issued[0] else None
        records.append(paper_record(
            title=clean_text((item.get("title") or [""])[0]),
            year=year,
            authors=[a for a in authors if a],
            venue=clean_text((item.get("container-title") or [""])[0]),
            doi=normalize_doi(item.get("DOI") or ""),
            source="crossref",
            id=item.get("DOI"),
            oa_url=None,
        ))
    polite_sleep(cfg)
    return records


def search_semanticscholar(query: str, cfg: dict, limit: int) -> list[dict]:
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/search?"
        + parse.urlencode({
            "query": query,
            "limit": str(min(limit, 100)),
            "fields": "title,year,authors,externalIds,openAccessPdf,venue,citationCount",
        })
    )
    data = http_json(url, cfg, timeout_note="SemanticScholar")
    records = []
    for item in (data or {}).get("data", []):
        records.append(paper_record(
            title=clean_text(item.get("title")),
            year=item.get("year"),
            authors=[clean_text((a or {}).get("name")) for a in item.get("authors", [])][:6],
            venue=clean_text(item.get("venue")),
            doi=normalize_doi((item.get("externalIds") or {}).get("DOI") or ""),
            oa=bool(item.get("openAccessPdf")),
            oa_url=(item.get("openAccessPdf") or {}).get("url"),
            citations=item.get("citationCount"),
            source="semanticscholar",
            id=item.get("paperId"),
        ))
    polite_sleep(cfg)
    return records


def search_arxiv(query: str, cfg: dict, limit: int) -> list[dict]:
    url = (
        "https://export.arxiv.org/api/query?"
        + parse.urlencode({
            "search_query": f"all:{query}",
            "start": "0",
            "max_results": str(limit),
        })
    )
    try:
        res = http_get(url, cfg, accept="application/atom+xml")
    except RuntimeError as exc:
        print(f"[warn] arXiv 请求失败: {exc}", file=sys.stderr)
        return []
    if res["status"] != 200:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(res["body"])
    except ET.ParseError:
        return []
    records = []
    for entry in root.findall("a:entry", ns):
        title = clean_text(entry.findtext("a:title", default="", namespaces=ns))
        published = entry.findtext("a:published", default="", namespaces=ns)
        authors = [
            clean_text(a.findtext("a:name", default="", namespaces=ns))
            for a in entry.findall("a:author", ns)
        ][:6]
        link_pdf = ""
        for link in entry.findall("a:link", ns):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                link_pdf = link.get("href", "")
        records.append(paper_record(
            title=title,
            year=int(published[:4]) if published[:4].isdigit() else None,
            authors=[a for a in authors if a],
            venue="arXiv",
            oa_url=link_pdf or None,
            oa=True,
            source="arxiv",
            id=entry.findtext("a:id", default="", namespaces=ns),
        ))
    polite_sleep(cfg)
    return records


def search(query: str, cfg: dict, limit: int, oa_only: bool = False) -> list[dict]:
    wanted = [s.lower() for s in cfg.get("sources", DEFAULT_CONFIG["sources"])]
    found: list[dict] = []
    if "openalex" in wanted:
        found += search_openalex(query, cfg, limit)
    if "crossref" in wanted:
        found += search_crossref(query, cfg, limit)
    if "semanticscholar" in wanted:
        found += search_semanticscholar(query, cfg, limit)
    if "arxiv" in wanted:
        found += search_arxiv(query, cfg, limit)

    # 去重：DOI 优先，其次标题小写
    merged: list[dict] = []
    seen: set[str] = set()
    for rec in found:
        doi_key = (rec.get("doi") or "").lower()
        key = doi_key or "t:" + rec.get("title", "").lower()
        if key in seen:
            continue
        seen.add(key)
        if oa_only and not (rec.get("oa") or rec.get("oa_url")):
            continue
        merged.append(rec)
    merged.sort(key=lambda r: (r.get("citations") or 0, r.get("year") or 0), reverse=True)
    return merged[:limit]


# ---------------------------------------------------------------------------
# OA 全文定位
# ---------------------------------------------------------------------------
def openalex_work(doi: str, cfg: dict) -> dict | None:
    mailto = cfg.get("email") or ""
    url = "https://api.openalex.org/works/doi:" + parse.quote(doi, safe="/")
    if mailto:
        url += "?" + parse.urlencode({"mailto": mailto})
    data = http_json(url, cfg, timeout_note="OpenAlex work")
    if not (isinstance(data, dict) and data.get("id")):
        return None
    # OpenAlex 对未知 DOI 可能回退到模糊匹配；校验返回的 DOI 是否真是要找的那篇
    returned = normalize_doi(data.get("doi") or "")
    if returned and returned.lower() != doi.lower():
        return None
    return data


def crossref_meta(doi: str, cfg: dict) -> dict | None:
    url = "https://api.crossref.org/works/" + parse.quote(doi, safe="/")
    data = http_json(url, cfg, timeout_note="Crossref meta")
    if isinstance(data, dict) and "message" in data:
        return data["message"]
    return None


def s2_by_doi(doi: str, cfg: dict) -> dict | None:
    url = (
        "https://api.semanticscholar.org/graph/v1/paper/DOI:"
        + parse.quote(doi, safe="/")
        + "?fields=title,year,authors,venue,openAccessPdf,externalIds,citationCount"
    )
    data = http_json(url, cfg, timeout_note="SemanticScholar")
    if not (isinstance(data, dict) and data.get("title")):
        return None
    returned = normalize_doi((data.get("externalIds") or {}).get("DOI") or "")
    if returned and returned.lower() != doi.lower():
        return None
    return data


def unpaywall_locations(doi: str, cfg: dict) -> list[dict]:
    email = (cfg.get("email") or "").strip()
    if not email:
        print("[warn] 未配置 email，跳过 Unpaywall（在 config.json 填 email 可显著提高 OA 命中率）",
              file=sys.stderr)
        return []
    if "example." in email.lower() or email.lower().endswith("@test.com"):
        print("[warn] Unpaywall 拒绝 example.com 之类的占位邮箱，请在 config.json 填真实 email；本次跳过",
              file=sys.stderr)
        return []
    url = (
        "https://api.unpaywall.org/v2/"
        + parse.quote(doi, safe="/")
        + "?"
        + parse.urlencode({"email": email})
    )
    try:
        res = http_get(url, cfg, accept="application/json")
    except RuntimeError as exc:
        print(f"[warn] Unpaywall 请求失败: {exc}", file=sys.stderr)
        return []
    if res["status"] == 422:
        print("[warn] Unpaywall 422：email 需为真实地址（不要用 example.com / test.com 等占位域名）",
              file=sys.stderr)
        return []
    if res["status"] != 200:
        print(f"[warn] Unpaywall HTTP {res['status']}", file=sys.stderr)
        return []
    try:
        data = json.loads(res["body"].decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, dict):
        return []
    locations = []
    for loc in (data.get("oa_locations") or []):
        if not isinstance(loc, dict):
            continue
        url_pdf = loc.get("url_for_pdf") or loc.get("url")
        if url_pdf:
            locations.append({
                "pdf": loc.get("url_for_pdf"),
                "url": loc.get("url"),
                "source": "unpaywall",
                "host": loc.get("host_type"),
                "version": loc.get("version"),
                "license": loc.get("license"),
                "is_best": bool(loc.get("is_best")),
            })
    polite_sleep(cfg)
    return locations


def openalex_locations(work: dict) -> list[dict]:
    locations = []
    for loc in work.get("locations") or []:
        if not isinstance(loc, dict) or loc.get("is_oa") is not True:
            continue
        pdf = loc.get("pdf_url")
        landing = loc.get("landing_page_url")
        if pdf or landing:
            locations.append({
                "pdf": pdf,
                "url": landing,
                "source": "openalex",
                "host": loc.get("source", {}).get("type") if isinstance(loc.get("source"), dict) else None,
                "version": loc.get("version"),
                "license": loc.get("license"),
                "is_best": bool(loc.get("is_best_oa_location") or loc.get("is_oa")),
            })
    best = (work.get("best_oa_location") or {})
    if isinstance(best, dict) and best.get("is_oa") is not False and (best.get("pdf_url") or best.get("landing_page_url")):
        locations.insert(0, {
            "pdf": best.get("pdf_url"),
            "url": best.get("landing_page_url"),
            "source": "openalex-best",
            "host": None,
            "version": best.get("version"),
            "license": best.get("license"),
            "is_best": True,
        })
    return locations


def arxiv_locations(doi: str | None, arxiv_id: str | None, cfg: dict) -> list[dict]:
    identifier = arxiv_id
    if not identifier and doi:
        match = ARXIV_DOI_RE.match(doi)
        if match:
            identifier = match.group(1)
    if not identifier:
        return []
    identifier = identifier.strip()
    return [
        {"pdf": f"https://arxiv.org/pdf/{identifier}", "url": f"https://arxiv.org/abs/{identifier}",
         "source": "arxiv", "host": "arxiv", "version": "preprint", "license": None, "is_best": True},
        {"pdf": f"https://export.arxiv.org/pdf/{identifier}", "url": f"https://arxiv.org/abs/{identifier}",
         "source": "arxiv-export", "host": "arxiv", "version": "preprint", "license": None, "is_best": False},
    ]


def europepmc_locations(doi: str | None, pmid: str | None, pmcid: str | None, cfg: dict) -> list[dict]:
    query = None
    if pmcid:
        query = f"PMCID:{pmcid}"
    elif pmid:
        query = f"EXT_ID:{pmid}"
    elif doi:
        query = f'DOI:"{doi}"'
    if not query:
        return []
    url = (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
        + parse.urlencode({"query": query, "format": "json", "resultType": "core", "pageSize": "3"})
    )
    data = http_json(url, cfg, timeout_note="EuropePMC")
    locations = []
    for item in ((data or {}).get("resultList") or {}).get("result", []):
        pmcid_found = item.get("pmcid")
        if not pmcid_found or item.get("isOpenAccess") != "Y":
            continue
        if doi and normalize_doi(item.get("doi") or "") and item["doi"].casefold() != doi.casefold():
            continue
        if pmcid and pmcid_found.casefold() != pmcid.casefold():
            continue
        if pmid and str(item.get("id")) != str(pmid):
            continue
        locations.append({
            "pdf": f"https://europepmc.org/articles/{pmcid_found}?pdf=render",
            "url": f"https://europepmc.org/article/{item.get('source','MED')}/{item.get('id')}",
            "source": "europepmc",
            "host": "europepmc",
            "version": "published",
            "license": item.get("license"),
            "is_best": False,
        })
        locations.append({
            "pdf": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid_found}/pdf/",
            "url": f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid_found}/",
            "source": "pmc",
            "host": "pmc",
            "version": "published",
            "license": item.get("license"),
            "is_best": False,
        })
    polite_sleep(cfg)
    return locations


def arxiv_meta(arxiv_id: str, cfg: dict) -> dict:
    """用 arXiv API 取标题/作者/年份，文件名更可读。"""
    if not arxiv_id:
        return {}
    url = "https://export.arxiv.org/api/query?" + parse.urlencode({"id_list": arxiv_id})
    try:
        res = http_get(url, cfg, accept="application/atom+xml")
    except RuntimeError:
        return {}
    if res["status"] != 200:
        return {}
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(res["body"])
    except ET.ParseError:
        return {}
    entry = root.find("a:entry", ns)
    if entry is None:
        return {}
    published = entry.findtext("a:published", default="", namespaces=ns)
    polite_sleep(cfg)
    return {
        "title": clean_text(entry.findtext("a:title", default="", namespaces=ns)),
        "authors": [
            clean_text(a.findtext("a:name", default="", namespaces=ns))
            for a in entry.findall("a:author", ns)
        ][:10],
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "venue": "arXiv",
        "doi": None,
    }


def _chinese_doi_locations(landing_url: str, html_text: str, source_label: str) -> list[dict]:
    """把落地页本身与页面里提取到的 PDF/全文链接转成 OA/机构访问位置。"""
    locations: list[dict] = []
    if landing_url:
        locations.append({
            "pdf": None, "url": landing_url, "source": source_label,
            "host": (parse.urlsplit(landing_url).hostname or "").lower(),
            "version": "published", "license": None, "is_best": True,
        })
    meta_pdfs: list[str] = []
    fulltext_links: list[str] = []
    if html_text:
        meta_pdfs = extract_pdf_url_from_html(html_text, landing_url)
        fulltext_links = extract_fulltext_links(html_text, landing_url)
    seen: set[str] = set()
    for url in meta_pdfs + fulltext_links:
        if not url or url in seen:
            continue
        seen.add(url)
        # citation_pdf_url 明确声明是 PDF；普通链接按 URL 形态判断
        is_pdf = url in meta_pdfs or _looks_like_pdf_url(url)
        locations.append({
            "pdf": url if is_pdf else None, "url": url, "source": source_label,
            "host": (parse.urlsplit(url).hostname or "").lower(),
            "version": "published" if is_pdf else "unknown",
            "license": None, "is_best": is_pdf,
        })
    return dedupe_locations(locations)


def resolve_chinese_doi(doi: str, cfg: dict) -> dict:
    """中文 DOI 专门解析路径。

    1) 先请求 ``https://www.chndoi.org/Resolution/Handler?doi=<urlencode>`` 并跟随重定向拿落地页；
    2) 拿不到落地页时 fallback ``https://doi.org/<doi>``；
    3) 从落地页 HTML 通用提取 PDF/全文链接（过滤图片/静态资源）；
    4) 解析失败只如实报告 ``source_unavailable`` + 中文提示，绝不伪造期刊官网 URL。

    返回 ``{status, doi, chinese_doi, landing_url, locations, source_errors, hints}``。
    """
    result: dict = {
        "status": "source_unavailable",
        "doi": doi,
        "chinese_doi": True,
        "landing_url": None,
        "locations": [],
        "source_errors": [],
        "hints": chinese_doi_hints(),
    }
    source_label = "chndoi"
    landing: str | None = None
    html_text = ""
    encoded = parse.quote(doi, safe="")
    chndoi_url = "https://www.chndoi.org/Resolution/Handler?" + parse.urlencode({"doi": doi})
    accept = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"

    # 1) chndoi.org 多重解析（跟随重定向拿落地页）
    try:
        res = http_get(chndoi_url, cfg, accept=accept)
    except RuntimeError as exc:
        result["source_errors"].append(
            {"source": "chndoi", "reason": "network_error", "detail": str(exc)})
        res = None
    if res is not None:
        final_url = res.get("url") or chndoi_url
        html_text = _decode_html(res.get("body") or b"")
        if res.get("status") == 200 and _is_external_landing(final_url):
            landing = final_url
        elif res.get("status") == 200:
            refresh = extract_meta_refresh_url(html_text, final_url)
            if refresh and _is_external_landing(refresh):
                landing = refresh
        elif res.get("status") not in (301, 302, 303, 307, 308):
            result["source_errors"].append({"source": "chndoi", "status": res.get("status")})

    # 2) doi.org 兜底（chndoi 超时/无落地页/中间页跳转失败）
    if not landing:
        doi_url = "https://doi.org/" + encoded
        try:
            res2 = http_get(doi_url, cfg, accept=accept)
        except RuntimeError as exc:
            result["source_errors"].append(
                {"source": "doi-resolver", "reason": "network_error", "detail": str(exc)})
        else:
            final_url = res2.get("url") or doi_url
            body = res2.get("body") or b""
            html2 = _decode_html(body) if body else ""
            if res2.get("status") == 200 and _is_external_landing(final_url):
                landing = final_url
                source_label = "doi-resolver"
                html_text = html2 or html_text
            elif res2.get("status") == 200:
                refresh = extract_meta_refresh_url(html2, final_url)
                if refresh and _is_external_landing(refresh):
                    landing = refresh
                    source_label = "doi-resolver"
                    html_text = html2
            elif res2.get("status") not in (301, 302, 303, 307, 308):
                result["source_errors"].append(
                    {"source": "doi-resolver", "status": res2.get("status")})

    if not landing:
        # 解析失败：明确报 source_unavailable（而不是 no_oa），并给出中文 DOI 提示
        result["source_errors"].append({"source": "chndoi", "reason": "source_unavailable"})
        return result

    locations = _chinese_doi_locations(landing, html_text, source_label)
    result["landing_url"] = landing
    result["locations"] = locations
    result["status"] = "ok"
    if not any(loc.get("pdf") or _looks_like_pdf_url(loc.get("url") or "")
               for loc in locations):
        # 只找到落地页、没找到 PDF/全文链接：如实记录，让调用方人工确认，不猜测 URL
        result["source_errors"].append({"source": source_label, "reason": "no_pdf_link"})
    return result


def resolve_oa(identifier: str, cfg: dict) -> dict:
    """解析标识符 -> {kind, id, metadata, locations, hints?}。"""
    kind, value = classify_identifier(identifier)
    locations: list[dict] = []
    metadata: dict = {}
    hints: list[str] = []
    doi = value if kind == "doi" else None
    arxiv_id = value if kind == "arxiv" else None
    pmid = value if kind == "pmid" else None
    pmcid = value if kind == "pmcid" else None

    if kind == "url":
        locations.append({"pdf": value, "url": value, "source": "direct-url",
                          "host": None, "version": None, "license": None, "is_best": True})
        return {"kind": kind, "id": value, "metadata": metadata, "locations": locations}
    if kind == "unknown":
        return {"kind": kind, "id": value, "metadata": metadata, "locations": locations}

    if doi:
        arxiv_doi = ARXIV_DOI_RE.match(doi)
        if arxiv_doi and not arxiv_id:
            arxiv_id = arxiv_doi.group(1)
        if is_chinese_doi(doi, cfg):
            # 中文 DOI 不在 Crossref/OpenAlex/S2 收录范围：走 chndoi 专用路径，
            # 跳过多源查询（避免每篇 3 次无谓 404 请求与误判 no_oa）。
            chinese = resolve_chinese_doi(doi, cfg)
            locations += chinese.get("locations") or []
            for item in chinese.get("source_errors") or []:
                cfg.setdefault("_source_errors", []).append(item)
            hints += chinese.get("hints") or chinese_doi_hints()
            if not metadata.get("doi"):
                metadata = merge_metadata(metadata, {"doi": doi})
        else:
            # arXiv DOI 不属于 Crossref 体系，Unpaywall 会返回 422，直接跳过
            if not arxiv_doi:
                locations += unpaywall_locations(doi, cfg)
            locations += arxiv_locations(doi, arxiv_id, cfg)
            work = openalex_work(doi, cfg)
            if work:
                locations += openalex_locations(work)
                metadata = merge_metadata(metadata, work_to_meta(work))
            s2 = s2_by_doi(doi, cfg)
            if s2:
                pdf = (s2.get("openAccessPdf") or {}).get("url")
                if pdf:
                    locations.append({"pdf": pdf, "url": pdf, "source": "semanticscholar",
                                      "host": None, "version": None, "license": None, "is_best": False})
                metadata = merge_metadata(metadata, s2_to_meta(s2))
                external = s2.get("externalIds") or {}
                if not arxiv_id and external.get("ArXiv"):
                    arxiv_id = external["ArXiv"]
                if not pmid and external.get("PubMed"):
                    pmid = str(external["PubMed"])
                if not pmcid and external.get("PubMedCentral"):
                    pmcid = "PMC" + str(external["PubMedCentral"])
            locations += arxiv_locations(doi, arxiv_id, cfg)
            if pmid or pmcid:
                locations += europepmc_locations(doi, pmid, pmcid, cfg)
            if not metadata.get("title"):
                meta = crossref_meta(doi, cfg)
                if meta:
                    metadata = merge_metadata(metadata, crossref_to_meta(meta))
            if not metadata.get("title") and arxiv_id:
                metadata = merge_metadata(metadata, arxiv_meta(arxiv_id, cfg))
    elif arxiv_id:
        locations += arxiv_locations(None, arxiv_id, cfg)
        metadata = merge_metadata({"title": "", "authors": [], "year": None,
                                   "venue": "arXiv", "doi": None}, arxiv_meta(arxiv_id, cfg))
    elif pmid or pmcid:
        locations += europepmc_locations(None, pmid, pmcid, cfg)
        for loc in locations:
            if loc.get("url"):
                metadata = {"title": "", "authors": [], "year": None, "venue": "Europe PMC",
                            "doi": None}
                break

    result = {"kind": kind, "id": value, "metadata": metadata,
              "locations": dedupe_locations(locations)}
    if hints:
        result["hints"] = hints
    return result


def dedupe_locations(locations: list[dict]) -> list[dict]:
    seen: set[str] = set()
    ordered = sorted(locations, key=lambda loc: (not loc.get("is_best"), not loc.get("pdf")))
    result = []
    for loc in ordered:
        key = loc.get("pdf") or loc.get("url") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(loc)
    return result


def work_to_meta(work: dict) -> dict:
    authors = [
        clean_text((a.get("author") or {}).get("display_name"))
        for a in (work.get("authorships") or [])
    ][:10]
    return {
        "title": clean_text(work.get("title")),
        "year": work.get("publication_year"),
        "authors": [a for a in authors if a],
        "venue": clean_text(((work.get("primary_location") or {}).get("source") or {}).get("display_name")),
        "doi": normalize_doi(work.get("doi") or "") or None,
        "abstract": None,
    }


def s2_to_meta(s2: dict) -> dict:
    return {
        "title": clean_text(s2.get("title")),
        "year": s2.get("year"),
        "authors": [clean_text((a or {}).get("name")) for a in s2.get("authors", [])][:10],
        "venue": clean_text(s2.get("venue")),
        "doi": normalize_doi((s2.get("externalIds") or {}).get("DOI") or "") or None,
    }


def crossref_to_meta(meta: dict) -> dict:
    issued = (meta.get("issued") or {}).get("date-parts") or [[None]]
    return {
        "title": clean_text((meta.get("title") or [""])[0]),
        "year": issued[0][0] if issued and issued[0] else None,
        "authors": [clean_text(f"{a.get('given','')} {a.get('family','')}".strip())
                    for a in meta.get("author", [])][:10],
        "venue": clean_text((meta.get("container-title") or [""])[0]),
        "doi": normalize_doi(meta.get("DOI") or "") or None,
    }


def merge_metadata(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in (extra or {}).items():
        if value and not merged.get(key):
            merged[key] = value
    merged.setdefault("authors", [])
    return merged


# ---------------------------------------------------------------------------
# 下载
# ---------------------------------------------------------------------------
def extract_pdf_url_from_html(html: str, page_url: str) -> list[str]:
    candidates: list[str] = []
    patterns = [
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+\.pdf[^"\']*)["\'][^>]+name=["\']citation_pdf_url["\']',
        r'<link[^>]+type=["\']application/pdf["\'][^>]+href=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I):
            candidates.append(parse.urljoin(page_url, html_lib.unescape(match.group(1))))
    return candidates[:5]


_LINK_ANCHOR_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_STATIC_ASSET_RE = re.compile(
    r"\.(?:png|jpe?g|gif|svg|webp|bmp|ico|css|js|map|woff2?|ttf|otf|eot|mp4|mp3|zip|rar|7z"
    r"|xml|json|txt|csv)(?:$|[?#])",
    re.I,
)
_FULLTEXT_TEXT_RE = re.compile(r"(pdf|full[\s_-]?text|download|全文|下载|原文|正文)", re.I)


def _looks_like_pdf_url(url: str) -> bool:
    """URL 是否像 PDF（.pdf 结尾，或期刊常见的 /pdf 路径段、?pdf=render）。"""
    low = (url or "").lower()
    return ".pdf" in low or "pdf=render" in low or bool(re.search(r"/pdf(?:[/?#]|$)", low))


def _decode_html(body: bytes) -> str:
    """按 <meta charset> 解码落地页 HTML，失败回退 utf-8/replace。"""
    text = (body or b"").decode("utf-8", "replace")
    match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", text[:4096], re.I)
    if match:
        try:
            return (body or b"").decode(match.group(1), "replace")
        except LookupError:
            return text
    return text


def extract_fulltext_links(html_text: str, page_url: str) -> list[str]:
    """从落地页 HTML 提取 PDF/全文链接（通用提取，匹配不到就返回空，绝不适造 URL）。

    只接受 http(s) 链接，过滤图片/静态资源，优先 .pdf。
    """
    if not html_text:
        return []
    candidates: list[str] = []
    for match in _LINK_ANCHOR_RE.finditer(html_text):
        raw_href = html_lib.unescape(match.group(1) or "").strip()
        if not raw_href or raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = parse.urljoin(page_url, raw_href)
        parts = parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        if _STATIC_ASSET_RE.search(parts.path) or _STATIC_ASSET_RE.search(parts.query or ""):
            continue
        anchor_text = clean_text(re.sub(r"<[^>]+>", " ", match.group(2) or ""))
        href_low = absolute.lower()
        looks_like_fulltext = (
            ".pdf" in href_low
            or "download" in href_low
            or "fulltext" in href_low
            or "pdf" in anchor_text.lower()
            or bool(_FULLTEXT_TEXT_RE.search(anchor_text))
        )
        if not looks_like_fulltext or absolute in candidates:
            continue
        candidates.append(absolute)
    candidates.sort(key=lambda url: 0 if _looks_like_pdf_url(url) else 1)
    return candidates[:5]


def extract_meta_refresh_url(html_text: str, page_url: str) -> str | None:
    """提取 <meta http-equiv="refresh"> 跳转目标（chndoi.org 中间页常见）。"""
    if not html_text:
        return None
    match = re.search(
        r"<meta[^>]+http-equiv\s*=\s*[\"']?refresh[\"']?[^>]*content\s*=\s*[\"']([^\"']+)[\"']",
        html_text, re.I,
    )
    if not match:
        return None
    target = re.search(r"url\s*=\s*(.+)$", html_lib.unescape(match.group(1)), re.I)
    if not target:
        return None
    absolute = parse.urljoin(page_url, target.group(1).strip().strip("'\""))
    parts = parse.urlsplit(absolute)
    return absolute if parts.scheme in ("http", "https") and parts.netloc else None


def _is_external_landing(url: str) -> bool:
    """判断重定向终点是否为出版社/期刊落地页（排除 doi.org / chndoi.org 自身）。"""
    host = (parse.urlsplit(url or "").hostname or "").lower()
    if not host:
        return False
    if host == "chndoi.org" or host.endswith(".chndoi.org"):
        return False
    if host in ("doi.org", "dx.doi.org", "www.doi.org"):
        return False
    return True


def looks_like_pdf(body: bytes) -> bool:
    return body.startswith(b"%PDF-") and b"%%EOF" in body[-4096:]


def try_download_pdf(url: str, cfg: dict) -> dict:
    """尝试把一个 URL 变成 PDF 字节；失败返回 {ok:False, reason}。"""
    max_bytes = int(float(cfg.get("max_pdf_mb", 120)) * 1024 * 1024)
    try:
        res = http_get(url, cfg, accept="application/pdf,*/*", max_bytes=max_bytes + 1)
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), "tried": url}
    if res["status"] != 200:
        return {"ok": False, "status": res["status"], "reason": f"HTTP {res['status']}", "tried": url}
    body = res["body"]
    ctype = (res["headers"].get("content-type") or "").lower()
    if len(body) > max_bytes:
        return {"ok": False, "reason": f"文件超过大小上限（{max_bytes} 字节，见 config.max_pdf_mb）",
                "tried": url}
    if looks_like_pdf(body):
        return {"ok": True, "body": body, "url": res["url"], "content_type": ctype}
    html = body.decode("utf-8", "replace")
    if "citation_pdf_url" in html or ".pdf" in html.lower():
        for candidate in extract_pdf_url_from_html(html, res["url"]):
            try:
                sub = http_get(candidate, cfg, accept="application/pdf,*/*", max_bytes=max_bytes + 1)
            except RuntimeError:
                continue
            if sub["status"] == 200 and len(sub["body"]) <= max_bytes and looks_like_pdf(sub["body"]):
                return {"ok": True, "body": sub["body"], "url": sub["url"],
                        "content_type": (sub["headers"].get("content-type") or "").lower()}
    return {"ok": False, "reason": f"不是 PDF（Content-Type={ctype or 'unknown'}）", "tried": url}


def build_pdf_name(metadata: dict, identifier: str) -> str:
    """归档文件名：year[_surname]_标题.pdf；没有标题时用 DOI_slug，不用 nd_anon 前缀。"""
    year = metadata.get("year")
    authors = metadata.get("authors") or []
    title = metadata.get("title") or ""
    if title:
        parts: list[str] = []
        if year:
            parts.append(str(year))
        if authors:
            parts.append(safe_filename(authors[0].split()[-1], 24))
        parts.append(safe_filename(title, 70))
        return "_".join(part for part in parts if part) + ".pdf"
    if normalize_doi(identifier or ""):
        slug = doi_slug(identifier)
    else:
        slug = safe_filename(norm_identifier(identifier) or "paper", 60).replace(" ", "_")
    return f"{year}_{slug}.pdf" if year else (slug or "paper") + ".pdf"


def read_manifest(manifest: Path) -> list[dict]:
    if not manifest.is_file():
        return []
    records = []
    with _APPEND_LOCK:  # 与并发 batch 的追加互斥，避免读到半行
        try:
            text = manifest.read_text(encoding="utf-8")
        except OSError:
            return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            if isinstance(record, dict):
                records.append(record)
        except json.JSONDecodeError:
            continue
    return records


def append_jsonl(path: Path, record: dict) -> None:
    """线程安全地追加一行 JSON（manifest.jsonl / batch-state.jsonl 共用）。"""
    line = json.dumps(record, ensure_ascii=False)
    with _APPEND_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def open_exclusive_destination(out_dir: Path, filename: str):
    """独占创建目标文件，重名时自动加 _2/_3 后缀；返回 (path, handle)。

    用 ``open('xb')`` 的 O_EXCL 语义代替 exists() 检查，并发/多进程下不会互相覆盖。
    """
    candidate = out_dir / filename
    stem = Path(filename).stem
    counter = 2
    while True:
        try:
            return candidate, candidate.open("xb")
        except FileExistsError:
            candidate = out_dir / f"{stem}_{counter}.pdf"
            counter += 1


def norm_identifier(value: str) -> str:
    """把 DOI / arXiv / PMID 的各种写法归一化，用于清单去重比较。"""
    text = (value or "").strip()
    text = re.sub(r"^(arxiv:|doi:|pmid:|pmcid:)\s*", "", text, flags=re.I)
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"^https?://", "", text, flags=re.I)
    return text.rstrip("/").lower()


def record_path(record: dict, manifest: Path) -> Path:
    p = Path(record.get("path") or "__missing__")
    if p.is_absolute():
        return p.resolve()
    # Legacy records stored paths relative to the original working directory.
    # Try beside manifest, then its parent, without depending on current cwd.
    for candidate in (manifest.parent / p, manifest.parent.parent / p):
        if candidate.is_file():
            return candidate.resolve()
    return (manifest.parent / p).resolve()


def record_file_valid(record: dict, manifest: Path) -> bool:
    if not record.get("path") or not record.get("sha256"):
        return False
    validation = check_file(record_path(record, manifest), record.get("doi") or "", record.get("title") or "")
    return verified(validation) and validation.get("sha256") == record["sha256"]


def find_existing(manifest: Path, doi: str | None, identifier: str | None = None) -> dict | None:
    normalized = norm_identifier(identifier) if identifier else ""
    for record in reversed(read_manifest(manifest)):
        if not record_file_valid(record, manifest):
            continue
        if doi and (record.get("doi") or "").lower() == doi.lower():
            return record
        if normalized:
            recorded = norm_identifier(record.get("identifier") or "")
            if recorded and recorded == normalized:
                return record
    return None


def fetch_one(identifier: str, cfg: dict, out_dir: Path, *, force: bool = False) -> dict:
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg["_source_errors"] = []
    manifest = out_dir / "manifest.jsonl"
    if not force:
        kind, value = classify_identifier(identifier)
        existing = find_existing(manifest, value if kind == "doi" else None, identifier)
        if existing:
            validation = check_file(record_path(existing, manifest), existing.get("doi") or "", existing.get("title") or "")
            status = "exists" if verified(validation) else "saved_pending_verification"
            return {"status": status, "path": str(record_path(existing, manifest)), "record": existing, "verification": validation}
    resolved = resolve_oa(identifier, cfg)
    if resolved.get("kind") == "unknown":
        return {
            "status": "invalid_identifier",
            "identifier": identifier,
            "reason": "无法识别为 DOI / arXiv ID / PMID / PMCID / URL",
            "examples": ["10.1038/nature12373", "arxiv:1706.03762", "pmid:12345678",
                         "https://arxiv.org/pdf/1706.03762"],
        }
    metadata = resolved.get("metadata") or {}
    doi = metadata.get("doi") or (resolved["id"] if resolved["kind"] == "doi" else None)

    if not force:
        existing = find_existing(manifest, doi, identifier)
        if existing:
            return {"status": "exists", "doi": doi, "path": existing.get("path"),
                    "title": existing.get("title") or metadata.get("title"), "record": existing}

    locations = resolved.get("locations") or []
    hints = list(resolved.get("hints") or [])
    if not locations:
        payload = {
            "status": "source_unavailable" if cfg.get("_source_errors") else "no_oa",
            "source_errors": list(cfg.get("_source_errors", [])),
            "identifier": identifier,
            "kind": resolved["kind"],
            "doi": doi,
            "title": metadata.get("title"),
            "suggestions": [
                "校内网络下用浏览器打开出版社页面，人工下载（数字图书馆已授权）",
                "校外时把出版社 URL 交给 scripts/webvpn_url.py 重写成 WebVPN 地址再访问",
                "通过图书馆文献传递/馆际互借申请全文",
                "给通讯作者发邮件索取（可在 open-access.md 找模板）",
            ],
        }
        if hints:
            payload["hints"] = hints
        return payload

    errors = []
    attempted = set()
    blocked_hosts = set()
    for loc in locations:
        for key in ("pdf", "url"):
            target = loc.get(key)
            if not target or target in attempted or parse.urlsplit(target).netloc in blocked_hosts:
                continue
            attempted.add(target)
            outcome = try_download_pdf(target, cfg)
            if not outcome.get("ok"):
                if outcome.get("status") in (403, 429):
                    blocked_hosts.add(parse.urlsplit(target).netloc)
                errors.append({"url": public_url(target), "reason": outcome.get("reason")})
                continue
            validation = check_bytes(outcome["body"], doi or "", metadata.get("title") or "")
            if validation.get("valid") is False:
                errors.append({"url": public_url(target), "reason": validation.get("reason")})
                continue
            digest = hashlib.sha256(outcome["body"]).hexdigest()
            if not force:
                for old in read_manifest(manifest):
                    if old.get("sha256") == digest and record_file_valid(old, manifest):
                        return {
                            "status": "exists" if verified(validation) else "saved_pending_verification",
                            "sha256": digest,
                            "path": old.get("path"),
                            "title": old.get("title") or metadata.get("title"),
                            "note": "内容与已下载文件相同（sha256 一致），未重复保存",
                            "record": old,
                        }
            filename = build_pdf_name(metadata, resolved["id"])
            destination, handle = open_exclusive_destination(out_dir, filename)
            with handle:
                handle.write(outcome["body"])
            record = {
                "doi": doi,
                "title": metadata.get("title") or "",
                "authors": metadata.get("authors") or [],
                "year": metadata.get("year"),
                "venue": metadata.get("venue") or "",
                "identifier": public_url(identifier),
                "status": "downloaded" if verified(validation) else "saved_pending_verification",
                "verification": validation,
                "source": loc.get("source"),
                "source_type": "publisher" if loc.get("source") in ("chndoi", "doi-resolver") else "oa",
                "license": loc.get("license"),
                "version": loc.get("version"),
                "url": public_url(outcome.get("url") or ""),
                "path": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": digest,
                "retrieved_at": now_iso(),
            }
            append_jsonl(manifest, record)
            return record
    payload = {
        "status": "download_failed",
        "identifier": identifier,
        "doi": doi,
        "title": metadata.get("title"),
        "errors": errors,
        "suggestions": [
            "换一个 OA 版本（出版社 PDF 可能有防盗链），或浏览器人工打开候选 URL",
            "校内网络下访问出版社页面人工下载",
        ],
    }
    if hints:
        payload["hints"] = hints
    return payload


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def emit_json(payload: dict, pretty: bool = False) -> None:
    """输出 JSON：默认单行（便于脚本 ``json.loads(stdout)``），--pretty 时多行缩进。"""
    if pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def cmd_search(args, cfg: dict) -> int:
    results = search(args.query, cfg, args.limit, oa_only=args.oa_only)
    payload = {"status": "ok", "query": args.query, "count": len(results), "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_oa(args, cfg: dict) -> int:
    cfg["_source_errors"] = []
    resolved = resolve_oa(args.identifier, cfg)
    if resolved.get("kind") == "unknown":
        print(json.dumps({
            "status": "invalid_identifier",
            "identifier": args.identifier,
            "reason": "无法识别为 DOI / arXiv ID / PMID / PMCID / URL",
            "examples": ["10.1038/nature12373", "arxiv:1706.03762", "pmid:12345678",
                         "https://arxiv.org/pdf/1706.03762"],
        }, ensure_ascii=False, indent=2))
        return EXIT_USAGE
    payload = {"status": "ok" if resolved["locations"] else ("source_unavailable" if cfg.get("_source_errors") else "no_oa"),
               "source_errors": cfg.get("_source_errors", []), **resolved}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_OK if resolved["locations"] else EXIT_UNAVAILABLE


def cmd_fetch(args, cfg: dict) -> int:
    out_dir = Path(args.out or cfg.get("download_dir") or "papers").expanduser()
    result = fetch_one(args.identifier, cfg, out_dir, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code_for(result.get("status") or "error")


def load_batch_state(state_path: Path) -> dict[str, dict]:
    """读取断点文件：归一化 identifier -> 最后一条状态记录。"""
    state: dict[str, dict] = {}
    if not state_path.is_file():
        return state
    try:
        lines = state_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"[warn] 无法读取断点文件 {state_path}: {exc}", file=sys.stderr)
        return state
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"[warn] 忽略无法解析的断点记录: {state_path}", file=sys.stderr)
            continue
        if isinstance(record, dict) and record.get("identifier"):
            state[norm_identifier(str(record["identifier"]))] = record
    return state


def batch_state_record(identifier: str, result: dict) -> dict:
    """断点文件一行：identifier / status / path / error / timestamp。"""
    error: str | None = str(result["reason"]) if result.get("reason") else None
    if not error and result.get("errors"):
        error = "; ".join(str(item.get("reason") if isinstance(item, dict) else item)
                          for item in result["errors"][:3])
    if not error and result.get("source_errors"):
        error = "; ".join(str(item.get("reason") or item.get("status") or item)
                          for item in result["source_errors"][:3])
    return {
        "identifier": public_url(identifier),
        "doi":result.get("doi"),"title":result.get("title"),"sha256":result.get("sha256"),
        "status": result.get("status") or "error",
        "path": result.get("path"),
        "error": error,
        "timestamp": now_iso(),
    }


def cmd_batch(args, cfg: dict) -> int:
    """并发批量 fetch：--workers 并发、--resume 断点续跑、--state 状态文件。"""
    source = Path(args.file)
    if not source.is_file():
        print(json.dumps({"status": "error", "message": f"文件不存在: {source}"}, ensure_ascii=False))
        return EXIT_USAGE
    workers = getattr(args, "workers", BATCH_DEFAULT_WORKERS)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        print(json.dumps({"status": "invalid_argument", "reason": "--workers 必须为 >=1 的整数"},
                         ensure_ascii=False))
        return EXIT_USAGE
    force = bool(getattr(args, "force", False))
    resume = bool(getattr(args, "resume", False))
    try:
        lines = source.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        print(json.dumps({"status": "error", "message": f"无法读取清单 {source}: {exc}"},
                         ensure_ascii=False))
        return EXIT_USAGE
    identifiers = [line.strip() for line in lines
                   if line.strip() and not line.strip().startswith("#")]
    out_dir = Path(args.out or cfg.get("download_dir") or "papers").expanduser()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(json.dumps({"status": "error", "message": f"无法创建输出目录 {out_dir}: {exc}"},
                         ensure_ascii=False))
        return EXIT_USAGE
    state_path = Path(getattr(args, "state", None) or (out_dir / "batch-state.jsonl")).expanduser()
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        if state_path.exists() and state_path.is_dir():
            raise OSError("断点文件路径是目录")
    except OSError as exc:
        print(json.dumps({"status": "error", "message": f"断点文件不可用 {state_path}: {exc}"},
                         ensure_ascii=False))
        return EXIT_USAGE

    done_state = load_batch_state(state_path) if resume else {}
    pending: list[tuple[int, str]] = []
    skipped_items: dict[int, dict] = {}
    for index, identifier in enumerate(identifiers):
        entry = done_state.get(norm_identifier(identifier)) if resume else None
        if entry and not force and str(entry.get("status") or "") in ("downloaded","exists") and record_file_valid(entry,state_path):
            skipped_items[index] = {
                "identifier": identifier,
                "status": "skipped",
                "previous_status": entry.get("status"),
                "path": entry.get("path"),
            }
            continue
        pending.append((index, identifier))

    # 并发 worker 共享同一份限速状态（_last_request_at），但 _source_errors 各自隔离
    cfg.setdefault("_last_request_at", {})
    results: dict[int, dict] = {}
    completed = 0

    def run_one(identifier: str) -> dict:
        worker_cfg = dict(cfg)
        try:
            return fetch_one(identifier, worker_cfg, out_dir, force=force)
        except (OSError, ValueError, RuntimeError) as exc:
            return {"status": "error", "identifier": public_url(identifier),
                    "reason": type(exc).__name__}

    def record_result(index: int, identifier: str, result: dict) -> None:
        nonlocal completed
        results[index] = result
        try:
            append_jsonl(state_path, batch_state_record(identifier, result))
        except OSError as exc:
            print(f"[warn] 无法写入断点文件 {state_path}: {exc}", file=sys.stderr)
        with _STDERR_LOCK:
            completed += 1
            print(f"[{completed}/{len(pending)}] {result.get('status')}  {identifier}",
                  file=sys.stderr, flush=True)

    effective_workers = min(workers, max(1, len(pending)))
    if not pending:
        print(f"[batch] 无可执行条目（--resume 跳过 {len(skipped_items)} 条）", file=sys.stderr)
    if effective_workers <= 1 or not pending:
        for index, identifier in pending:
            record_result(index, identifier, run_one(identifier))
    else:
        with ThreadPoolExecutor(max_workers=effective_workers,
                                thread_name_prefix="paper-oa") as pool:
            futures = {pool.submit(run_one, identifier): (index, identifier)
                       for index, identifier in pending}
            for future in as_completed(futures):
                index, identifier = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # pragma: no cover - 防御性兜底
                    result = {"status": "error", "identifier": public_url(identifier),
                              "reason": type(exc).__name__}
                record_result(index, identifier, result)

    ordered = [results.get(index) or skipped_items.get(index) or {"status": "error"}
               for index in range(len(identifiers))]
    statuses = [str(results[index].get("status") or "error") for index in sorted(results)]
    downloaded = sum(1 for status in statuses if status == "downloaded")
    exists = sum(1 for status in statuses if status == "exists")
    pending_verification = sum(1 for status in statuses if status == "saved_pending_verification")
    no_oa = sum(1 for status in statuses if status == "no_oa")
    unavailable = sum(1 for status in statuses if status == "source_unavailable")
    failed = sum(1 for status in statuses if status not in
                 ("downloaded", "exists", "saved_pending_verification", "no_oa", "source_unavailable"))
    incomplete = len(pending) - downloaded - exists
    summary = {
        "status": "ok" if incomplete == 0 else "partial",
        "total": len(identifiers),
        "downloaded": downloaded,
        "exists": exists,
        "no_oa": no_oa,
        "source_unavailable": unavailable,
        "pending_verification": pending_verification,
        "failed": failed,
        "skipped": len(skipped_items),
        "incomplete": incomplete,
        "workers": effective_workers,
        "state": str(state_path),
        "results": ordered,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return EXIT_OK if incomplete == 0 else EXIT_PARTIAL


def import_archive_name(args, source: Path) -> str:
    """import-file 归档文件名：--name 优先；否则标题 / DOI_slug / paper，绝不用 nd_anon 前缀。"""
    raw_name = str(getattr(args, "name", "") or "").strip()
    if raw_name:
        explicit = safe_filename(raw_name, 80)
        return explicit if explicit.lower().endswith(".pdf") else explicit + ".pdf"
    title = clean_text(getattr(args, "title", "") or "")
    authors = split_authors(getattr(args, "authors", "") or "")
    year = getattr(args, "year", None)
    doi = normalize_doi(getattr(args, "doi", "") or "")
    if title:
        if year and authors:
            return (f"{year}_{safe_filename(authors[0].split()[-1], 24)}_"
                    f"{safe_filename(title, 70)}.pdf")
        return safe_filename(title, 80) + ".pdf"
    if doi:
        return doi_slug(doi) + ".pdf"
    return "paper.pdf"


def cmd_import_file(args, cfg: dict) -> int:
    """核验并登记用户/浏览器已下载的 PDF（不联网）。

    默认输出单行 JSON（``--pretty`` 保留多行缩进），便于 ``json.loads(stdout)``。
    """
    pretty = bool(getattr(args, "pretty", False))
    source = Path(args.file).expanduser().resolve()
    doi = normalize_doi(getattr(args, "doi", "") or "") or ""
    title = clean_text(getattr(args, "title", "") or "")
    validation = check_file(source, doi, title, cfg["max_pdf_mb"])
    if validation.get("valid") is False:
        emit_json({"status": "invalid_pdf", "file": str(source), "verification": validation}, pretty)
        return EXIT_USAGE
    authors = split_authors(getattr(args, "authors", "") or "")
    year = getattr(args, "year", None)
    venue = clean_text(getattr(args, "venue", "") or "")
    version_type = getattr(args, "version_type", None) or "unknown"
    source_type = getattr(args, "source_type", None) or "local"
    out = Path(args.out or cfg["download_dir"]).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = out / "manifest.jsonl"
    existing = next((record for record in reversed(read_manifest(manifest))
                     if record.get("sha256") == validation["sha256"]
                     and record_file_valid(record, manifest)), None)
    if existing:
        # 同一文件已登记：复用原路径，不重复复制
        destination = record_path(existing, manifest)
    elif source.parent == out and not str(getattr(args, "name", "") or "").strip():
        destination = source
    else:
        destination, handle = open_exclusive_destination(out, import_archive_name(args, source))
        with handle, source.open("rb") as src:
            shutil.copyfileobj(src, handle)
    status = "downloaded" if verified(validation) else "saved_pending_verification"
    record = {
        "status": status,
        "identifier": doi or public_url(getattr(args, "source_url", "") or ""),
        "doi": doi or None,
        "title": title,
        "authors": authors,
        "year": year,
        "venue": venue,
        "source": source_type,
        "source_type": source_type,
        "version": version_type,
        "url": public_url(getattr(args, "source_url", "") or ""),
        "path": str(destination),
        "sha256": validation["sha256"],
        "bytes": validation["bytes"],
        "verification": validation,
        "retrieved_at": now_iso(),
        "license": None,
    }
    append_jsonl(manifest, record)
    payload = dict(record)
    payload["manifest"] = str(manifest)
    emit_json(payload, pretty)
    return EXIT_OK if status == "downloaded" else EXIT_PARTIAL


def cmd_organize(args, cfg: dict) -> int:
    target = Path(args.dir).expanduser()
    manifest = target / "manifest.jsonl"
    records = read_manifest(manifest)
    # Later verification supersedes earlier pending records of the same local file.
    records = list({str(record_path(r, manifest)): r for r in records}.values())
    if not records:
        print(json.dumps({"status": "empty", "message": f"没有找到 {manifest}"}, ensure_ascii=False))
        return EXIT_UNAVAILABLE
    index_path = target / "index.csv"
    fields = ["status", "doi", "year", "authors", "title", "venue", "source", "source_type",
              "version", "license", "bytes", "path", "retrieved_at", "sha256"]
    missing = []
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in sorted(records, key=lambda r: (str(r.get("year") or ""), r.get("title") or ""),
                             reverse=True):
            writer.writerow({**record, "authors": "; ".join(record.get("authors") or [])})
            file_path = record_path(record, manifest)
            if record.get("path") and not file_path.exists():
                missing.append(str(file_path))
    payload = {
        "status": "missing_files" if missing else "ok",
        "dir": str(target),
        "records": len(records),
        "index": str(index_path),
        "missing_files": missing,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return EXIT_PARTIAL if missing else EXIT_OK


class CliArgumentParser(argparse.ArgumentParser):
    """argparse 默认用 exit 2 表示参数错误；本工具统一用 1（见模块 docstring）。"""

    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: 参数错误: {message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = CliArgumentParser(
        description="OA 论文检索 / OA 全文定位 / PDF 下载归档（纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("常用：")[-1] if "常用：" in __doc__ else "",
    )
    parser.add_argument("--config", help="config.json 路径（默认 skill 目录/config/config.json）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="检索候选论文")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.add_argument("--oa-only", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_oa = sub.add_parser("oa", help="解析 OA 全文位置")
    p_oa.add_argument("identifier")
    p_oa.set_defaults(func=cmd_oa)

    p_fetch = sub.add_parser("fetch", help="下载 OA PDF")
    p_fetch.add_argument("identifier")
    p_fetch.add_argument("--out", help="输出目录（默认 config.download_dir）")
    p_fetch.add_argument("--force", action="store_true", help="忽略清单里已有的 DOI")
    p_fetch.set_defaults(func=cmd_fetch)

    p_batch = sub.add_parser("batch", help="批量下载（每行一个 DOI/标识）")
    p_batch.add_argument("file")
    p_batch.add_argument("--out")
    p_batch.add_argument("--force", action="store_true")
    p_batch.add_argument("--workers", type=int, default=BATCH_DEFAULT_WORKERS,
                         help="并发线程数（默认 3；1 表示串行）")
    p_batch.add_argument("--resume", action="store_true",
                         help="按断点文件续跑，跳过已下载/已存在/待核验条目")
    p_batch.add_argument("--state", help="断点文件（默认 <out>/batch-state.jsonl）")
    p_batch.set_defaults(func=cmd_batch)

    p_organize = sub.add_parser("organize", help="由 manifest.jsonl 生成 index.csv")
    p_organize.add_argument("dir")
    p_organize.set_defaults(func=cmd_organize)

    p_import = sub.add_parser("import-file", help="核验并登记浏览器或人工已下载的 PDF（不联网）")
    p_import.add_argument("file")
    p_import.add_argument("--doi", default="")
    p_import.add_argument("--title", default="", help="已确认的目标论文题名（缺省时按 DOI 生成文件名）")
    p_import.add_argument("--source-url", default="")
    p_import.add_argument("--name", default="", help="归档文件名（自动补 .pdf）")
    p_import.add_argument("--authors", default="", help="作者，分号/逗号分隔，可省略")
    p_import.add_argument("--year", type=int, default=None, help="出版年份，可省略")
    p_import.add_argument("--venue", default="", help="期刊/会议名，可省略")
    p_import.add_argument("--version-type", default="unknown",
                          choices=["preprint", "accepted", "published", "unknown"],
                          help="文献版本（默认 unknown）")
    p_import.add_argument("--source-type", default="local",
                          choices=["publisher", "repository", "oa", "local"],
                          help="来源类型（默认 local）")
    p_import.add_argument("--pretty", action="store_true",
                          help="多行缩进 JSON（默认单行，便于逐行解析）")
    p_import.add_argument("--out")
    p_import.set_defaults(func=cmd_import_file)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.setrecursionlimit(10000)
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:  # --help -> 0；参数错误 -> 1
        return int(exc.code or 0)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "config_error", "reason": str(exc)}, ensure_ascii=False))
        return EXIT_USAGE
    if not cfg.get("email") and args.command in ("search", "oa", "fetch", "batch"):
        print("[warn] 建议在 config.json 填写 email（Crossref/Unpaywall 礼貌标识，能提高成功率）",
              file=sys.stderr)
    if args.command == "search" and not 1 <= args.limit <= 100:
        print(json.dumps({"status": "invalid_argument", "reason": "--limit 必须为 1..100"}))
        return EXIT_USAGE
    try:
        return args.func(args, cfg)
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "reason": type(exc).__name__}, ensure_ascii=False))
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
