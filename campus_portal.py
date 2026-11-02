"""Local-only configuration portal. Never logs or returns credential values."""
from __future__ import annotations
import argparse
import ctypes
import ctypes.wintypes as wintypes
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui"
DEFAULT_LOGIN_URL = "https://webvpn.whut.edu.cn/login?cas_login=true"
SERVICE_ID = "campus-papers-config-ui-v2"


def encrypted_path(path: Path) -> Path:
    """登录配置.private.json -> 登录配置.private.dpapi（同目录）"""
    if path.suffix == ".json":
        return path.with_suffix(".dpapi")
    return path.with_name(path.name + ".dpapi")


# ---------------------------- Windows DPAPI（可选，默认开启） ----------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def dpapi_available() -> bool:
    return os.name == "nt"


def dpapi_protect(data: bytes) -> bytes:
    if not dpapi_available():
        raise OSError("DPAPI requires Windows")
    in_blob, keep = _blob(data)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def dpapi_unprotect(blob: bytes) -> bytes:
    if not dpapi_available():
        raise OSError("DPAPI requires Windows")
    in_blob, keep = _blob(blob)
    out = _Blob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def read_config(path: Path) -> dict:
    plain, secret = path, encrypted_path(path)
    data = None
    try:
        if secret.exists():
            data = json.loads(dpapi_unprotect(secret.read_bytes()).decode("utf-8"))
        elif plain.exists():
            data = json.loads(plain.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("配置文件无法读取或格式无效，请检查原文件。") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("配置文件无法读取或格式无效，请检查原文件。")
    for key in ("username", "password"):
        if key in data and not isinstance(data[key], str):
            raise ValueError("配置文件无法读取或格式无效，请检查原文件。")
    for key in ("enabled", "auto_submit"):
        if key in data and not isinstance(data[key], bool):
            raise ValueError("配置文件无法读取或格式无效，请检查原文件。")
    return data


def public_status(path: Path, demo: bool = False) -> dict:
    if demo:
        return {"status": "credentials_missing", "has_username": False, "has_password": False,
                "auto_submit": False, "enabled": True, "demo": True, "protected": False}
    try:
        data = read_config(path)
        user, password = bool(data.get("username", "").strip()), bool(data.get("password", ""))
        status = "disabled" if data.get("enabled") is False else "ready" if user and password else "credentials_missing"
        return {"status": status, "has_username": user, "has_password": password,
                "auto_submit": data.get("auto_submit", False), "enabled": data.get("enabled", True),
                "demo": False, "protected": encrypted_path(path).exists()}
    except ValueError:
        return {"status": "config_error", "has_username": False, "has_password": False,
                "auto_submit": False, "enabled": False, "demo": False, "protected": False}


def write_private_config(path: Path, data: dict, encrypt: bool = False) -> str:
    """写入私密配置；encrypt=True 时用 Windows DPAPI 加密为 <name>.dpapi 并删除明文。

    返回 'dpapi' 或 'plaintext'。加密失败时回退明文，调用方应在响应里提示。
    """
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if encrypt and dpapi_available():
        try:
            blob = dpapi_protect(payload.encode("utf-8"))
            target = encrypted_path(path)
            temporary = target.with_name(target.name + "." + secrets.token_hex(8) + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    try:
                        os.chmod(temporary, 0o600)
                    except OSError:
                        pass
                    stream.write(blob)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            # 加密文件写入成功后移除明文，避免磁盘上同时存在明文密码
            try:
                if path.exists() and path != target:
                    path.unlink()
            except OSError:
                pass
            return "dpapi"
        except (OSError, ValueError):
            raise OSError("credential_encryption_failed_no_plaintext_written") from None
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return "plaintext"


def render_page(token: str, demo: bool = False, asset_prefix: str = "/assets/") -> str:
    return (UI / "index.html").read_text(encoding="utf-8").replace("{{CSRF_TOKEN}}", token).replace(
        "{{DEMO}}", "true" if demo else "false").replace("{{ASSET_PREFIX}}", asset_prefix)


class PortalServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, address, config: Path, demo=False, encrypt: bool = False):
        self.config_path = config.resolve()
        self.demo = demo
        self.encrypt = encrypt
        self.csrf_token = secrets.token_urlsafe(32)
        self.write_lock = threading.Lock()
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "CampusPortal/2"
    def log_message(self, *args):
        pass

    def _host_ok(self):
        port = self.server.server_port
        return self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin == "http://" + self.headers.get("Host", "")

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        for key, value in {"Content-Type": content_type, "Content-Length": str(len(data)),
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Content-Security-Policy":
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'; form-action 'self'"}.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status, value):
        self._send(status, json.dumps(value, ensure_ascii=False))

    def do_GET(self):
        if not self._host_ok():
            self._json(403, {"error": "主机地址无效。"}); return
        resource = urlsplit(self.path).path
        if resource in ("/", "/index.html"):
            self._send(200, render_page(self.server.csrf_token, self.server.demo), "text/html; charset=utf-8")
        elif resource == "/status":
            self._json(200, {"service": SERVICE_ID, "credential_values_returned": False,
                            **public_status(self.server.config_path, self.server.demo)})
        elif resource in ("/assets/app.css", "/assets/app.js", "/assets/mark.svg", "/assets/i18n.js"):
            extension = Path(resource).suffix
            content_type = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}[extension]
            self._send(200, (UI / Path(resource).name).read_bytes(), content_type)
        elif resource == "/favicon.ico":
            self._send(204, b"")
        else:
            self._json(404, {"error": "页面不存在。"})

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            self._json(403, {"error": "请求来源无效。"}); return
        resource = urlsplit(self.path).path
        if resource == "/quit":
            if self.headers.get("X-Campus-UI") != "shutdown":
                self._json(403, {"error": "关闭请求无效。"}); return
            self._json(200, {"status": "stopped"})
            threading.Thread(target=self.server.shutdown, daemon=True).start(); return
        if resource != "/save":
            self._json(404, {"error": "页面不存在。"}); return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode("utf-8"), self.server.csrf_token.encode("ascii")):
            self._json(403, {"error": "页面已过期，请刷新后重试。"}); return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "请求格式无效。"}); return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError()
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict) or not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str) or not isinstance(data.get("auto_submit"), bool):
                raise ValueError()
            if len(data["username"]) > 128 or len(data["password"]) > 2048 or any(c in data["username"] for c in "\r\n\0") or "\0" in data["password"]:
                raise ValueError()
        except (ValueError, UnicodeError):
            self._json(400, {"error": "请检查账号、密码及选项的格式。"}); return
        if self.server.demo:
            self._json(200, {"status": "ready", "demo": True, "has_username": True,
                            "has_password": True, "auto_submit": data["auto_submit"]}); return
        try:
            with self.server.write_lock:
                previous = read_config(self.server.config_path)
                user = data["username"].strip() or previous.get("username", "")
                password = data["password"] or previous.get("password", "")
                if previous.get("username") and data["username"].strip() and data["username"].strip() != previous["username"] and not data["password"]:
                    self._json(400, {"error": "修改账号时请同时填写对应的密码。", "fields": ["password"]}); return
                if not user or not password:
                    self._json(400, {"error": "首次配置需要填写账号和密码。", "fields": [k for k, v in (("username", user), ("password", password)) if not v]}); return
                config = {**previous, "enabled": True, "username": user, "password": password,
                          "auto_submit": data["auto_submit"], "login_url": previous.get("login_url") or DEFAULT_LOGIN_URL}
                mode = write_private_config(self.server.config_path, config, encrypt=self.server.encrypt)
            self._json(200, {"credential_values_returned": False, "encrypted": mode,
                            **public_status(self.server.config_path)})
        except ValueError as error:
            self._json(409, {"error": str(error)})
        except OSError:
            self._json(500, {"error": "保存失败，请检查文件是否只读或被其他程序占用。"})


def main():
    parser = argparse.ArgumentParser(description="校园文献助手 · 本机账号配置")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--demo", action="store_true", help="纯演示，不读取或保存真实配置")
    parser.add_argument("--no-dpapi", action="store_true", help="不加密，明文写入（不推荐）")
    parser.add_argument("--config", type=Path, default=ROOT / "登录配置.private.json")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须为 0..65535")
    encrypt = (not args.no_dpapi) and dpapi_available()
    server = PortalServer(("127.0.0.1", args.port), args.config, args.demo, encrypt=encrypt)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Campus Portal: {url}" + (" [DEMO]" if args.demo else "") + (" [DPAPI]" if encrypt else ""), flush=True)
    if not args.no_open:
        threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
