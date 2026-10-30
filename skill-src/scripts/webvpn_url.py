#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把普通网址转换成学校 WebVPN（网瑞达/深信服 wrdvpn 系）访问地址。

原理（已用武汉理工大学真实链接逐字节验证）：
    https://webvpn.<学校域名>/<http|https>/<token>/<path>?<query>
    token = hex("wrdvpnisthebest!") + hex(AES-128-CFB(host, key=iv="wrdvpnisthebest!"))
    即 token 前 32 个 hex 字符固定为 key 的 ASCII 十六进制（"77726476706e69737468656265737421"），
    其后是 host 的 AES-128-CFB 密文。

只用标准库实现（纯 Python AES-128），不依赖 pycryptodome / cryptography。
联网本身仍需要 WebVPN 的登录 Cookie（浏览器登录后复制，见 vpn-webvpn.md）。

用法：
    python webvpn_url.py https://kns.cnki.net/kcms2/article/abstract?v=xxx
    python webvpn_url.py --base https://webvpn.whut.edu.cn https://www.cnki.net/
    python webvpn_url.py --selftest          # 用 WHUT 真实 token 自检算法
    python webvpn_url.py --json <url>        # 输出 JSON（含 token/host/path）
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import urlsplit

# Windows 控制台默认 GBK，统一走 UTF-8，避免中文输出乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

_KEY = b"wrdvpnisthebest!"
_IV = b"wrdvpnisthebest!"
# token 前缀 = hex(key) = "77726476706e69737468656265737421"（32 个 hex 字符）
_PREFIX_HEX = _KEY.hex()

# 武汉理工大学给定真实样本：host -> token（用于自检/校验机器是否用同一套网关）
KNOWN = {
    "https://webvpn.whut.edu.cn": {
        "wisdom.chaoxing.com": (
            "77726476706e69737468656265737421"
            "e7fe5298283d2653760986b4915b247bfa5069"
        )
    }
}
SAMPLES = [
    (
        "https://webvpn.whut.edu.cn",
        "wisdom.chaoxing.com",
        "77726476706e69737468656265737421"
        "e7fe5298283d2653760986b4915b247bfa5069",
    )
]


# ---------------------------------------------------------------------------
# 纯 Python AES-128 + CFB-128（只做加密，够用；带自检）
# ---------------------------------------------------------------------------
def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


def _build_sbox() -> list[int]:
    sbox = [0] * 256
    for x in range(256):
        inv = 0
        if x:
            for y in range(1, 256):
                if _gmul(x, y) == 1:
                    inv = y
                    break
        s = inv
        for shift in range(1, 5):
            s ^= ((inv << shift) | (inv >> (8 - shift))) & 0xFF
        sbox[x] = s ^ 0x63
    return sbox


SBOX = _build_sbox()


def _expand_key(key: bytes) -> list[list[int]]:
    if len(key) != 16:
        raise ValueError("AES-128 key must be 16 bytes")
    w = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        temp = w[i - 1][:]
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _gmul(rcon, 2)
        w.append([w[i - 4][j] ^ temp[j] for j in range(4)])
    return w


def _add_round_key(state: list[int], rk: list[int]) -> None:
    for i in range(16):
        state[i] ^= rk[i]


def _sub_bytes(state: list[int]) -> None:
    for i in range(16):
        state[i] = SBOX[state[i]]


def _shift_rows(state: list[int]) -> None:
    src = state[:]
    for r in range(4):
        for c in range(4):
            state[r + 4 * c] = src[r + 4 * ((c + r) % 4)]


def _mix_columns(state: list[int]) -> None:
    for c in range(4):
        i = 4 * c
        a0, a1, a2, a3 = state[i:i + 4]
        state[i + 0] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        state[i + 1] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        state[i + 2] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        state[i + 3] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)


def _encrypt_block(block: bytes, w: list[list[int]]) -> bytes:
    state = list(block)
    _add_round_key(state, [b for word in w[0:4] for b in word])
    for r in range(1, 10):
        _sub_bytes(state)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, [b for word in w[4 * r:4 * r + 4] for b in word])
    _sub_bytes(state)
    _shift_rows(state)
    _add_round_key(state, [b for word in w[40:44] for b in word])
    return bytes(state)


def aes_cfb_encrypt(data: bytes, key: bytes = _KEY, iv: bytes = _IV) -> bytes:
    """AES-128-CFB（128-bit 段，等价 CryptoJS.mode.CFB + NoPadding）。"""
    w = _expand_key(key)
    out = bytearray()
    prev = iv
    for start in range(0, len(data), 16):
        chunk = data[start:start + 16]
        stream = _encrypt_block(prev, w)
        ct = bytes(x ^ y for x, y in zip(chunk, stream))
        out += ct
        prev = ct  # CFB-128：上一段密文作为下一段 IV
    return bytes(out)


def host_token(host: str) -> str:
    """host（不含 scheme/path，含非默认端口）-> WebVPN token。"""
    return _PREFIX_HEX + aes_cfb_encrypt(host.encode("utf-8")).hex()


def host_for_token(parts) -> str:
    """从 urlsplit 结果取 token 用的 host：小写 hostname，非默认端口拼成 host:port。"""
    host = (parts.hostname or "").lower()
    if parts.port and not (
        (parts.scheme == "http" and parts.port == 80)
        or (parts.scheme == "https" and parts.port == 443)
    ):
        host += f":{parts.port}"
    return host


def to_webvpn(url: str, base: str = "https://webvpn.whut.edu.cn") -> str:
    """普通 URL -> WebVPN URL。base 形如 https://webvpn.whut.edu.cn。"""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"需要 http(s) URL：{url!r}")
    scheme = parts.scheme
    host = host_for_token(parts)
    path = parts.path or "/"
    query = f"?{parts.query}" if parts.query else ""
    return f"{base.rstrip('/')}/{scheme}/{host_token(host)}{path}{query}"


def selftest() -> int:
    ok = True
    for base, host, expected in SAMPLES:
        actual = host_token(host)
        if actual == expected:
            print(f"[OK]   {host} -> {actual}")
        else:
            ok = False
            print(f"[FAIL] {host}\n       expected {expected}\n       actual   {actual}")
    # 回归样本：确认转换后的完整 URL 与用户提供的真实 WebVPN 链接一致
    sample_url = (
        "https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html"
        "?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001"
    )
    expected_url = (
        "https://webvpn.whut.edu.cn/https/77726476706e697374686562657374"
        "21e7fe5298283d2653760986b4915b247bfa5069/newwisdom/doordatabase/database.html"
        "?pageId=527321&wfwfid=25279&websiteId=285586&sw=&uid=46691481&dataType=100001"
    )
    actual_url = to_webvpn(sample_url, "https://webvpn.whut.edu.cn")
    if actual_url == expected_url:
        print("[OK]   完整 URL 重写与真实样本一致")
    else:
        ok = False
        print(f"[FAIL] URL 重写不一致：\n expected {expected_url}\n actual   {actual_url}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="普通网址 -> 学校 WebVPN 网址（wrdvpn token 算法，纯标准库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python webvpn_url.py https://kns.cnki.net/kcms2/article/abstract?v=xxx\n"
               "  python webvpn_url.py --selftest",
    )
    ap.add_argument("url", nargs="?", help="要重写的 http(s) 网址")
    ap.add_argument("--base", default="https://webvpn.whut.edu.cn",
                    help="WebVPN 网关地址（默认武汉理工大学）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--selftest", action="store_true", help="用真实样本自检算法")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.url:
        ap.print_help()
        return 2

    try:
        rewritten = to_webvpn(args.url, args.base)
        original = urlsplit(args.url.strip())
        host = host_for_token(original)  # 与 URL 里 token 使用同一 host（含非默认端口）
        token = host_token(host)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("用法：python webvpn_url.py [--base https://webvpn.<学校域名>] <http(s) URL>", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "source": args.url,
            "webvpn": rewritten,
            "gateway": args.base.rstrip("/"),
            "scheme": original.scheme,
            "host": host,
            "token": token,
            "path": (original.path or "/") + ("?" + original.query if original.query else ""),
        }, ensure_ascii=False, indent=2))
    else:
        print(rewritten)
    return 0


if __name__ == "__main__":
    sys.exit(main())
