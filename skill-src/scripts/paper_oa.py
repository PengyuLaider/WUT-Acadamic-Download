#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OA 论文检索 / OA 全文定位 / PDF 下载归档（纯标准库，无需 pip 安装）。

给 DSH skill「campus-library-papers」使用。核心原则：
- 只走合法 OA 渠道（Unpaywall / OpenAlex / Semantic Scholar / arXiv / Europe PMC），
  不访问 Sci-Hub 等盗版镜像，不绕过付费墙。
- 所有命令默认输出结构化 JSON 到 stdout（stderr 只放警告），便于 agent 解析后决定下一步。
- 下载的 PDF 先校验 %PDF 魔数，再落到 papers/ 目录，并写 manifest.jsonl 去重。

子命令：
  search   "关键词或标题"                 检索候选论文（OpenAlex/Crossref/S2/arXiv）
  oa       <DOI|arxiv:ID|pmid:ID|PMCID>    解析 OA 全文位置（不下载）
  fetch    <DOI|arxiv:ID|pmid:ID|URL>      下载最佳 OA PDF 到 papers/，写清单
  batch    <文件，每行一个 DOI/标识>        批量 fetch
  organize <目录>                          重建 manifest.jsonl 对应的 index.csv

常用（所有命令默认输出 JSON，无 --json 参数）：
  python paper_oa.py search "graph neural network drug discovery" --limit 8
  python paper_oa.py fetch 10.1038/nature12373
  python paper_oa.py batch dois.txt --out papers/2025-综述
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
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
    "request_interval_seconds": 0.4,  # API 礼貌间隔
    "max_pdf_mb": 120,
    "sources": ["openalex", "crossref", "semanticscholar", "arxiv"],
    "user_agent": DEFAULT_USER_AGENT,
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path: str | None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
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
                if isinstance(data, dict):
                    cfg.update({k: v for k, v in data.items() if v is not None})
                break
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[warn] 读取配置失败 {p}: {exc}", file=sys.stderr)
    return cfg


def ua(cfg: dict) -> str:
    email = cfg.get("email") or "unknown@example.com"
    return (cfg.get("user_agent") or DEFAULT_USER_AGENT).format(email=email)


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
        req = request.Request(url, headers=headers, method="GET")
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                body = resp.read(max_bytes) if max_bytes else resp.read()
                return {
                    "status": resp.status,
                    "url": resp.geturl(),
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                    "body": body,
                }
        except error.HTTPError as exc:
            body = b""
            try:
                body = exc.read(max_bytes) if max_bytes else exc.read()
            except Exception:
                pass
            result = {
                "status": exc.code,
                "url": url,
                "headers": {k.lower(): v for k, v in (exc.headers or {}).items()},
                "body": body,
            }
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_exc = exc
                continue
            return result
        except (error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise RuntimeError(f"请求失败 {url}: {exc}") from exc
    raise RuntimeError(f"请求失败 {url}: {last_exc}")


def http_json(url: str, cfg: dict, *, timeout_note: str = "") -> dict | list | None:
    try:
        res = http_get(url, cfg, accept="application/json")
    except RuntimeError as exc:
        print(f"[warn] {timeout_note or 'API'} 请求失败: {exc}", file=sys.stderr)
        return None
    if res["status"] != 200:
        print(f"[warn] {timeout_note or 'API'} HTTP {res['status']}: {url}", file=sys.stderr)
        return None
    try:
        return json.loads(res["body"].decode("utf-8", "replace"))
    except json.JSONDecodeError:
        print(f"[warn] JSON 解析失败: {url}", file=sys.stderr)
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
    doi = doi.strip().rstrip(".,;)")
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
        return "arxiv", raw.split(":", 1)[1].strip()
    if ARXIV_ID_RE.match(raw):
        return "arxiv", raw
    if low.startswith("pmid:"):
        return "pmid", re.sub(r"\D", "", raw)
    if low.startswith("pmcid:"):
        return "pmcid", "PMC" + re.sub(r"\D", "", raw)
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
            oa_url=best.get("url_for_pdf") or best.get("url") or oa.get("oa_url"),
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
        if not isinstance(loc, dict):
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
    if isinstance(best, dict) and (best.get("pdf_url") or best.get("landing_page_url")):
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
        if not pmcid_found:
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


def resolve_oa(identifier: str, cfg: dict) -> dict:
    """解析标识符 -> {kind, id, metadata, locations}。"""
    kind, value = classify_identifier(identifier)
    locations: list[dict] = []
    metadata: dict = {}
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
        metadata = merge_metadata({"title": f"arXiv:{arxiv_id}", "authors": [], "year": None,
                                   "venue": "arXiv", "doi": None}, arxiv_meta(arxiv_id, cfg))
    elif pmid or pmcid:
        locations += europepmc_locations(None, pmid, pmcid, cfg)
        for loc in locations:
            if loc.get("url"):
                metadata = {"title": "", "authors": [], "year": None, "venue": "Europe PMC",
                            "doi": None}
                break

    return {"kind": kind, "id": value, "metadata": metadata, "locations": dedupe_locations(locations)}


def dedupe_locations(locations: list[dict]) -> list[dict]:
    seen: set[str] = set()
    ordered = sorted(locations, key=lambda loc: (not loc.get("is_best"), not loc.get("pdf")))
    result = []
    for loc in ordered:
        key = (loc.get("pdf") or loc.get("url") or "").lower()
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
        r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.I):
            candidates.append(parse.urljoin(page_url, match.group(1)))
    return candidates[:5]


def looks_like_pdf(body: bytes) -> bool:
    return len(body) > 1024 and body[:5] == b"%PDF-"


def try_download_pdf(url: str, cfg: dict) -> dict:
    """尝试把一个 URL 变成 PDF 字节；失败返回 {ok:False, reason}。"""
    max_bytes = int(cfg.get("max_pdf_mb", 120)) * 1024 * 1024
    try:
        res = http_get(url, cfg, accept="application/pdf,*/*", max_bytes=max_bytes + 1)
    except RuntimeError as exc:
        return {"ok": False, "reason": str(exc), "tried": url}
    if res["status"] != 200:
        return {"ok": False, "reason": f"HTTP {res['status']}", "tried": url}
    body = res["body"]
    ctype = (res["headers"].get("content-type") or "").lower()
    if len(body) > max_bytes:
        return {"ok": False, "reason": f"文件超过大小上限（{max_bytes} 字节，见 config.max_pdf_mb）",
                "tried": url}
    if looks_like_pdf(body):
        return {"ok": True, "body": body[:max_bytes], "url": res["url"], "content_type": ctype}
    html = body.decode("utf-8", "replace")
    if "citation_pdf_url" in html or ".pdf" in html.lower():
        for candidate in extract_pdf_url_from_html(html, res["url"]):
            try:
                sub = http_get(candidate, cfg, accept="application/pdf,*/*", max_bytes=max_bytes + 1)
            except RuntimeError:
                continue
            if sub["status"] == 200 and looks_like_pdf(sub["body"]):
                return {"ok": True, "body": sub["body"][:max_bytes], "url": sub["url"],
                        "content_type": (sub["headers"].get("content-type") or "").lower()}
    return {"ok": False, "reason": f"不是 PDF（Content-Type={ctype or 'unknown'}）", "tried": url}


def build_pdf_name(metadata: dict, identifier: str) -> str:
    year = metadata.get("year") or "nd"
    authors = metadata.get("authors") or []
    surname = safe_filename(authors[0].split()[-1], 24) if authors else "anon"
    title = metadata.get("title") or identifier
    return f"{year}_{surname}_{safe_filename(title, 70)}.pdf"


def read_manifest(manifest: Path) -> list[dict]:
    if not manifest.is_file():
        return []
    records = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def norm_identifier(value: str) -> str:
    """把 DOI / arXiv / PMID 的各种写法归一化，用于清单去重比较。"""
    text = (value or "").strip()
    text = re.sub(r"^(arxiv:|doi:|pmid:|pmcid:)\s*", "", text, flags=re.I)
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^https?://arxiv\.org/(abs|pdf)/", "", text, flags=re.I)
    text = re.sub(r"^https?://", "", text, flags=re.I)
    return text.rstrip("/").lower()


def find_existing(manifest: Path, doi: str | None, identifier: str | None = None) -> dict | None:
    normalized = norm_identifier(identifier) if identifier else ""
    for record in read_manifest(manifest):
        if doi and (record.get("doi") or "").lower() == doi.lower():
            return record
        if normalized:
            recorded = norm_identifier(record.get("identifier") or "")
            if recorded and recorded == normalized:
                return record
    return None


def fetch_one(identifier: str, cfg: dict, out_dir: Path, *, force: bool = False) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "manifest.jsonl"
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
    if not locations:
        return {
            "status": "no_oa",
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

    errors = []
    for loc in locations:
        for key in ("pdf", "url"):
            target = loc.get(key)
            if not target:
                continue
            outcome = try_download_pdf(target, cfg)
            if not outcome.get("ok"):
                errors.append({"url": target, "reason": outcome.get("reason")})
                continue
            digest = hashlib.sha256(outcome["body"]).hexdigest()
            if not force:
                for old in read_manifest(manifest):
                    if old.get("sha256") == digest:
                        return {
                            "status": "exists",
                            "sha256": digest,
                            "path": old.get("path"),
                            "title": old.get("title") or metadata.get("title"),
                            "note": "内容与已下载文件相同（sha256 一致），未重复保存",
                            "record": old,
                        }
            filename = build_pdf_name(metadata, resolved["id"])
            destination = out_dir / filename
            counter = 2
            while destination.exists():
                destination = out_dir / f"{Path(filename).stem}_{counter}.pdf"
                counter += 1
            destination.write_bytes(outcome["body"])
            record = {
                "doi": doi,
                "title": metadata.get("title") or "",
                "authors": metadata.get("authors") or [],
                "year": metadata.get("year"),
                "venue": metadata.get("venue") or "",
                "identifier": identifier,
                "status": "downloaded",
                "source": loc.get("source"),
                "license": loc.get("license"),
                "version": loc.get("version"),
                "url": outcome.get("url"),
                "path": str(destination),
                "bytes": destination.stat().st_size,
                "sha256": digest,
                "retrieved_at": now_iso(),
            }
            with manifest.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            return record
    return {
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


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------
def cmd_search(args, cfg: dict) -> int:
    results = search(args.query, cfg, args.limit, oa_only=args.oa_only)
    payload = {"status": "ok", "query": args.query, "count": len(results), "results": results}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_oa(args, cfg: dict) -> int:
    resolved = resolve_oa(args.identifier, cfg)
    if resolved.get("kind") == "unknown":
        print(json.dumps({
            "status": "invalid_identifier",
            "identifier": args.identifier,
            "reason": "无法识别为 DOI / arXiv ID / PMID / PMCID / URL",
            "examples": ["10.1038/nature12373", "arxiv:1706.03762", "pmid:12345678",
                         "https://arxiv.org/pdf/1706.03762"],
        }, ensure_ascii=False, indent=2))
        return 2
    payload = {"status": "ok" if resolved["locations"] else "no_oa", **resolved}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if resolved["locations"] else 2


def cmd_fetch(args, cfg: dict) -> int:
    out_dir = Path(args.out or cfg.get("download_dir") or "papers").expanduser()
    result = fetch_one(args.identifier, cfg, out_dir, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"downloaded": 0, "exists": 0, "no_oa": 2,
            "download_failed": 1, "invalid_identifier": 2}.get(result["status"], 1)


def cmd_batch(args, cfg: dict) -> int:
    source = Path(args.file)
    if not source.is_file():
        print(json.dumps({"status": "error", "message": f"文件不存在: {source}"}, ensure_ascii=False))
        return 1
    identifiers = [
        line.strip() for line in source.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    out_dir = Path(args.out or cfg.get("download_dir") or "papers").expanduser()
    results = []
    for index, identifier in enumerate(identifiers, start=1):
        result = fetch_one(identifier, cfg, out_dir, force=args.force)
        results.append(result)
        print(f"[{index}/{len(identifiers)}] {result.get('status')}  {identifier}", file=sys.stderr)
        if result["status"] in ("downloaded", "exists"):
            polite_sleep(cfg)
    summary = {
        "status": "ok",
        "total": len(results),
        "downloaded": sum(1 for r in results if r.get("status") == "downloaded"),
        "exists": sum(1 for r in results if r.get("status") == "exists"),
        "no_oa": sum(1 for r in results if r.get("status") == "no_oa"),
        "failed": sum(1 for r in results if r.get("status") == "download_failed"),
        "results": results,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_organize(args, cfg: dict) -> int:
    target = Path(args.dir).expanduser()
    manifest = target / "manifest.jsonl"
    records = read_manifest(manifest)
    if not records:
        print(json.dumps({"status": "empty", "message": f"没有找到 {manifest}"}, ensure_ascii=False))
        return 2
    index_path = target / "index.csv"
    fields = ["doi", "year", "authors", "title", "venue", "source", "license",
              "bytes", "path", "retrieved_at", "sha256"]
    missing = []
    with index_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in sorted(records, key=lambda r: (r.get("year") or 0, r.get("title") or ""),
                             reverse=True):
            writer.writerow({**record, "authors": "; ".join(record.get("authors") or [])})
            file_path = Path(record.get("path") or "")
            if record.get("path") and not file_path.exists():
                missing.append(str(file_path))
    payload = {
        "status": "ok",
        "dir": str(target),
        "records": len(records),
        "index": str(index_path),
        "missing_files": missing,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
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
    p_batch.set_defaults(func=cmd_batch)

    p_organize = sub.add_parser("organize", help="由 manifest.jsonl 生成 index.csv")
    p_organize.add_argument("dir")
    p_organize.set_defaults(func=cmd_organize)
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.setrecursionlimit(10000)
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    if not cfg.get("email") and args.command in ("search", "oa", "fetch", "batch"):
        print("[warn] 建议在 config.json 填写 email（Crossref/Unpaywall 礼貌标识，能提高成功率）",
              file=sys.stderr)
    return args.func(args, cfg)


if __name__ == "__main__":
    sys.exit(main())
