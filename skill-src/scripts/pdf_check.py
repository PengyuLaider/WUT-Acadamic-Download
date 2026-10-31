"""Local PDF checks shared by downloaders; no browser or network access."""
from __future__ import annotations
import argparse
import hashlib
import io
import json
import re
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    print(json.dumps({"status": "unsupported_runtime", "reason": "Python 3.10+ required"}))
    raise SystemExit(2)


def verified(result: dict) -> bool:
    return result.get("valid") is True and result.get("identity") == "matched" and not result.get("review_required")


def check_bytes(body: bytes, doi: str = "", title: str = "") -> dict:
    result = {"valid": False, "identity": "unverified", "parser": "unavailable",
              "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-4096:]:
        return {**result, "reason": "missing_pdf_header_or_eof"}
    try:
        from pypdf import PdfReader
    except ImportError:
        return {**result, "valid": None, "reason": "install_pypdf_or_check_manually"}
    result["parser"] = "pypdf"
    try:
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted:
            return {**result, "valid": None, "reason": "encrypted_pdf_check_manually"}
        if not len(reader.pages):
            return {**result, "reason": "empty_pdf"}
        # Resolve every page to detect broken page trees, then inspect front matter only.
        for page in reader.pages:
            _ = page.mediabox
        text = "\n".join(page.extract_text() or "" for page in reader.pages[:2])
        result.update(valid=True, pages=len(reader.pages))
        if len(reader.pages) == 1:
            result["review_required"] = ["single_page_confirm_full_text"]
        normalized = re.sub(r"\s+", "", text).casefold()
        expected = re.sub(r"\s+", "", doi).casefold().removeprefix("https://doi.org/")
        # A DOI match alone could be a citation; require the expected title too.
        norm_title = re.sub(r"[\W_]", "", title).casefold()
        norm_text = re.sub(r"[\W_]", "", text).casefold()
        title_match = len(norm_title) >= 12 and norm_title in norm_text
        doi_match = bool(expected and expected in normalized)
        result.update(doi_match=doi_match, title_match=title_match)
        if title_match and (not expected or doi_match):
            result["identity"] = "matched"
        result["reason"] = "checked" if result["identity"] == "matched" else "confirm_article_identity"
    except Exception as exc:
        result.update(valid=False, reason="pdf_parse_failed", detail=type(exc).__name__)
    return result


def check_file(path: str | Path, doi: str = "", title: str = "", max_mb: float = 120) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"valid": False, "identity": "unverified", "reason": "missing_file"}
    if path.stat().st_size > max_mb * 1024 * 1024:
        return {"valid": False, "identity": "unverified", "reason": "too_large"}
    return check_bytes(path.read_bytes(), doi, title)


def main():
    parser = argparse.ArgumentParser(description="本地 PDF 完整性和篇目核验，不联网")
    parser.add_argument("file")
    parser.add_argument("--doi", default="")
    parser.add_argument("--title", default="")
    args = parser.parse_args()
    result = check_file(args.file, args.doi, args.title)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if verified(result) else 3


if __name__ == "__main__":
    raise SystemExit(main())
