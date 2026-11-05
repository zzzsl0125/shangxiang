#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
尚香书苑 自动签到（Discuz! X3.5 + sx_signin 1.1.5）

作者: 临渊（原始脚本）/ 维护: 2026-11
入口: https://sxsy.org/ 发布页 -> latestDomain

变量:
  sxsy            必填。Cookie，或“邮箱&密码”；多账号用换行分隔。
  DDDD_OCR_URL    可选但强烈建议。OCR 接口，form 提交 image=<base64>，
                  返回 {"code":200,"data":"ABCD"}（也兼容 ddddocr 官方
                  {"result":"ABCD"} 和 JSON 提交）。
  DDDD_OCR_TOKEN  可选。OCR 服务的共享令牌，放在 X-OCR-Token 头里。
  SXSY_HOST       可选。强制域名，例如 sxsy33.com；不填则自动从发布页读取。
  SXSY_SCHEME     可选。默认 https；本地 mock 测试可设 http。
  SXSY_VERIFY_SSL 可选。默认 1；本地自签名测试可设 0。
  SXSY_MAX_ATTEMPTS 可选。签到验证码最多尝试次数，默认 6。

说明:
  * 网站已从 k_misign 换成 sx_signin 插件，签到接口和验证码都变了。
  * 新版签到即使使用 Cookie 登录，也要过一次 4 位图形验证码，所以
    DDDD_OCR_URL 不是“只有邮箱密码登录才需要”，Cookie 登录同样需要。
  * 脚本不会把 Cookie 写入文件，也不会打印 Cookie。

用法:
  python 尚香书苑.py                 # 正常签到
  python 尚香书苑.py --diagnose      # 只体检：域名、登录态、插件、验证码配置
  python 尚香书苑.py --dry-run       # 识别签到验证码但不提交
  python 尚香书苑.py --host sxsy33.com
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import random
import re
import sys
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

import requests

DEFAULT_HOST = "sxsy33.com"
PUBLISH_URL = "https://sxsy.org/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
)

SIGNIN_PAGE = "/plugin.php?id=sx_signin:index"
SIGNIN_AJAX = "/plugin.php?id=sx_signin:ajax"
SIGNIN_CAPTCHA = "/plugin.php?id=sx_signin:captcha"
LOGIN_PAGE = (
    "/member.php?mod=logging&action=login&infloat=yes&frommessage"
    "&inajax=1&ajaxtarget=messagelogin"
)

log = logging.getLogger("sxsy")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value or "").strip()


def normalise_host(value: str) -> str:
    value = (value or "").strip()
    if "://" in value:
        value = urlparse(value).netloc
    return value.split("/", 1)[0].strip().lower().rstrip(".")


def json_from_page(html: str) -> Optional[Dict[str, Any]]:
    """Extract `window.SIGNIN = {...};` without choking on nested braces."""
    marker = "window.SIGNIN"
    pos = html.find(marker)
    if pos < 0:
        return None
    brace = html.find("{", pos)
    if brace < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(html[brace:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def format_signin_data(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    parts: List[str] = []
    for key, label in (
        ("credit", "金钱 +"),
        ("today_credit", "今日 +"),
        ("streak", "连续 "),
        ("total", "累计 "),
    ):
        if key in data and data[key] not in (None, ""):
            if key == "streak":
                parts.append(f"连续 {data[key]} 天")
            elif key == "total":
                parts.append(f"累计 {data[key]} 天")
            else:
                parts.append(f"{label}{data[key]}")
    return "，".join(parts)


# --------------------------------------------------------------------------
# 主任务
# --------------------------------------------------------------------------

class AutoTask:
    def __init__(self, site_name: str = "尚香书苑") -> None:
        self.site_name = site_name
        self.scheme = (os.getenv("SXSY_SCHEME") or "https").strip().lower()
        if self.scheme not in {"http", "https"}:
            self.scheme = "https"
        self.verify_ssl = env_bool("SXSY_VERIFY_SSL", True)
        self.timeout: Tuple[float, float] = (10.0, 30.0)
        self.ocr_url = self._normalise_ocr_url(os.getenv("DDDD_OCR_URL") or "")
        self.ocr_token = (os.getenv("DDDD_OCR_TOKEN") or "").strip()
        try:
            self.max_attempts = max(1, int(os.getenv("SXSY_MAX_ATTEMPTS") or "6"))
        except ValueError:
            self.max_attempts = 6
        self.dry_run = False
        self.host: Optional[str] = None
        self._captcha_dir = (os.getenv("SXSY_CAPTCHA_DIR") or "").strip()

    # ---------------- HTTP ----------------

    def _base(self, host: str) -> str:
        return f"{self.scheme}://{host}"

    def _abs(self, host: str, url: str) -> str:
        if not url:
            return self._base(host) + "/"
        if re.match(r"^https?://", url, re.I):
            return url
        return urljoin(self._base(host) + "/", url.lstrip("/"))

    def new_session(self) -> requests.Session:
        session = requests.Session()
        session.verify = self.verify_ssl
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )
        return session

    @staticmethod
    def _html(resp: requests.Response) -> str:
        # Discuz 站点是 UTF-8；不要依赖 requests 猜编码，否则中文登录提示会乱码。
        return resp.content.decode("utf-8", errors="replace")

    @staticmethod
    def _is_challenge(resp: requests.Response, text: str) -> bool:
        if resp.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        head = (text or "")[:4000].lower()
        return (
            "just a moment" in head
            or "cf-challenge" in head
            or "enable javascript and cookies" in head
        )

    def _get(self, session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        return session.get(url, **kwargs)

    def _post(self, session: requests.Session, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        return session.post(url, **kwargs)

    # ---------------- 域名 ----------------

    def _extract_hosts(self, html: str) -> List[str]:
        candidates: List[str] = []

        def add(value: str) -> None:
            host = normalise_host(value)
            if not host:
                return
            if host.startswith("www."):
                host = host[4:]
            if not re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", host):
                return
            if host in {"sxsy.org", "t.me", "telegram.org", "github.com", "x.com",
                        "twitter.com", "facebook.com", "youtube.com", "cloudflare.com"}:
                return
            if host not in candidates:
                candidates.append(host)

        # 1) 现在发布页把域名写在 JS 配置里：latestDomain:"sxsy33.com"
        for pattern in (
            r'latestDomain\s*:\s*["\']([^"\']+)',
            r'websiteUrl\s*:\s*["\'](?:https?://)?([^"\'/]+)',
        ):
            for match in re.finditer(pattern, html, re.I):
                add(match.group(1))

        # 2) 旧版发布页的链接；过滤 Telegram 等非论坛链接
        for match in re.finditer(r'href\s*=\s*["\']https?://([^"\'/]+)', html, re.I):
            add(match.group(1))

        # 3) 兜底：页面里任何 sxsy 域名
        for match in re.finditer(r"https?://(sxsy\d*\.[a-z]{2,})", html, re.I):
            add(match.group(1))

        return candidates

    def _host_alive(self, host: str) -> bool:
        try:
            session = self.new_session()
            resp = self._get(session, self._base(host) + "/")
            text = self._html(resp)
            if self._is_challenge(resp, text):
                log.warning("[域名检测] %s 返回 Cloudflare 挑战页", host)
                return False
            if resp.status_code >= 500:
                log.warning("[域名检测] %s HTTP %s", host, resp.status_code)
                return False
            if resp.status_code not in (200, 301, 302, 303, 307, 308):
                log.warning("[域名检测] %s HTTP %s", host, resp.status_code)
                return False
            if any(mark in text for mark in ("尚香书苑", "discuz_uid", "sx_signin", "登录")):
                return True
            log.warning("[域名检测] %s 页面内容不像论坛（%s 字节）", host, len(text))
            return False
        except requests.RequestException as exc:
            log.warning("[域名检测] %s 连接失败: %s", host, exc)
            return False

    def get_host(self) -> str:
        override = normalise_host(os.getenv("SXSY_HOST") or "")
        if override:
            log.info("[获取host] 使用 SXSY_HOST 指定域名: %s", override)
            self.host = override
            return override

        candidates: List[str] = []
        try:
            session = self.new_session()
            resp = self._get(
                session,
                PUBLISH_URL + "?t=" + str(int(time.time())),
                headers={
                    "Host": "sxsy.org",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                },
            )
            text = self._html(resp)
            if self._is_challenge(resp, text):
                log.warning("[获取host] 发布页返回 Cloudflare 挑战页")
            else:
                candidates = self._extract_hosts(text)
                log.info("[获取host] 发布页解析到候选域名: %s", candidates or "无")
        except requests.RequestException as exc:
            log.warning("[获取host] 访问发布页失败: %s", exc)

        if not candidates:
            candidates = [DEFAULT_HOST]

        for host in candidates:
            if self._host_alive(host):
                log.info("[获取host] 使用域名: %s", host)
                self.host = host
                return host

        fallback = candidates[0] if candidates else DEFAULT_HOST
        log.warning("[获取host] 所有候选域名都不可用，最后尝试: %s", fallback)
        self.host = fallback
        return fallback

    # ---------------- Cookie / 账号 ----------------

    @staticmethod
    def set_cookies(session: requests.Session, cookie_text: str) -> None:
        text = (cookie_text or "").strip()
        if text.lower().startswith("cookie:"):
            text = text.split(":", 1)[1]
        for part in re.split(r"[;\n]+", text):
            if "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip()
            if name:
                session.cookies.set(name, value.strip())

    def check_env(self) -> List[Dict[str, Any]]:
        raw = os.getenv("sxsy") or os.getenv("SXSY") or ""
        if not raw.strip():
            log.error("[检查环境变量] 没有找到 sxsy（或 SXSY）")
            return []

        accounts: List[Dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#"):
                continue
            if "&" in line:
                email, password = line.split("&", 1)
                accounts.append(
                    {"email": email.strip(), "password": password, "cookie": None}
                )
            else:
                accounts.append({"email": None, "password": None, "cookie": line})
        return accounts

    # ---------------- 页面解析 ----------------

    @staticmethod
    def _uid(html: str) -> int:
        match = re.search(r"discuz_uid\s*=\s*['\"]?(\d+)", html)
        if not match:
            return 0
        try:
            return int(match.group(1))
        except ValueError:
            return 0

    def _get_signin_page(self, session: requests.Session, host: str) -> Optional[str]:
        url = self._base(host) + SIGNIN_PAGE
        try:
            resp = self._get(
                session,
                url,
                headers={"Referer": self._base(host) + "/"},
            )
        except requests.RequestException as exc:
            log.error("[签到页] 请求失败: %s", exc)
            return None
        text = self._html(resp)
        if self._is_challenge(resp, text):
            log.error(
                "[签到页] 被 Cloudflare 拦截（HTTP %s）。GitHub runner 的 IP 可能被风控，"
                "建议改用 VPS 运行签到脚本。",
                resp.status_code,
            )
            return None
        if resp.status_code >= 400:
            log.error("[签到页] HTTP %s: %s", resp.status_code, url)
            return None
        return text

    def _parse_signin(self, html: str) -> Dict[str, Any]:
        data = json_from_page(html) or {}
        formhash = str(data.get("formhash") or "").strip()
        if not formhash:
            match = (
                re.search(r'name="formhash"\s+value="([a-zA-Z0-9]{8})"', html)
                or re.search(r"formhash\s*[=:]\s*['\"]([a-zA-Z0-9]{8})", html)
                or re.search(r"formhash=([a-zA-Z0-9]{8})", html)
            )
            formhash = match.group(1) if match else ""

        ajax = str(data.get("ajax") or SIGNIN_AJAX)
        captcha_url = str(
            data.get("captchaUrl") or data.get("captcha_url") or SIGNIN_CAPTCHA
        )

        status_match = re.search(
            r'id="signin-status-text"[^>]*>(.*?)</p>', html, re.S | re.I
        )
        status_text = strip_tags(status_match.group(1)) if status_match else ""

        button_match = re.search(
            r"<button\b[^>]*\bid=\"signin-btn\"[^>]*>", html, re.S | re.I
        )
        button_disabled = bool(
            button_match and re.search(r"\bdisabled\b", button_match.group(0), re.I)
        )

        signed = bool(data.get("signed") is True or button_disabled)
        if not signed and status_text:
            if "已签到" in status_text or ("已签" in status_text and "未" not in status_text):
                signed = True

        captcha_required = bool(
            data.get("captcha") is True or 'id="signin-captcha"' in html
        ) and not signed

        return {
            "formhash": formhash,
            "ajax": ajax,
            "captcha_url": captcha_url,
            "signed": signed,
            "status_text": status_text,
            "captcha_required": captcha_required,
            "has_button": bool(button_match),
            "has_signin_json": bool(data),
        }

    # ---------------- OCR ----------------

    @staticmethod
    def _normalise_ocr_url(raw: str) -> str:
        url = (raw or "").strip()
        if not url:
            return ""
        if not re.match(r"^https?://", url, re.I):
            url = "http://" + url
        parsed = urlparse(url)
        if parsed.path in ("", "/"):
            url = url.rstrip("/") + "/ocr"
        elif parsed.path.endswith("/"):
            url = url.rstrip("/")
        return url

    @staticmethod
    def _extract_ocr_text(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("data", "result", "text"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                for sub in ("result", "text", "data"):
                    sub_value = value.get(sub)
                    if isinstance(sub_value, str) and sub_value.strip():
                        return sub_value.strip()
        return ""

    def ocr(self, image: bytes) -> str:
        if not self.ocr_url:
            log.error(
                "[OCR] 未配置 DDDD_OCR_URL。新版签到需要图形验证码，"
                "Cookie 登录也必须配置 OCR。"
            )
            return ""
        if not image:
            return ""

        image_b64 = base64.b64encode(image).decode("ascii")
        headers = {"Accept": "application/json"}
        if self.ocr_token:
            headers["X-OCR-Token"] = self.ocr_token

        last_error = ""
        # 先按老脚本的 form 方式提交；失败再按 ddddocr 官方 JSON 方式重试。
        for mode in ("form", "json"):
            try:
                if mode == "form":
                    resp = requests.post(
                        self.ocr_url,
                        data={"image": image_b64},
                        headers=headers,
                        timeout=(5.0, 30.0),
                    )
                else:
                    resp = requests.post(
                        self.ocr_url,
                        json={"image": image_b64},
                        headers=headers,
                        timeout=(5.0, 30.0),
                    )
            except requests.RequestException as exc:
                last_error = f"{mode}: {exc}"
                continue

            text = ""
            try:
                payload = resp.json()
                text = self._extract_ocr_text(payload)
            except ValueError:
                last_error = f"{mode}: HTTP {resp.status_code}, 非 JSON 响应 {resp.text[:160]!r}"

            if resp.status_code >= 400 and not text:
                last_error = f"{mode}: HTTP {resp.status_code}, {resp.text[:160]!r}"
                continue

            text = re.sub(r"\s+", "", text).upper()
            if text:
                if len(text) == 4:
                    log.info("[OCR] 识别结果: %s", text)
                    return text
                log.warning("[OCR] 识别结果长度不是 4: %r，换一张重试", text)
                return ""

        if last_error:
            log.error("[OCR] 识别失败: %s", last_error)
        return ""

    def ocr_health(self) -> None:
        if not self.ocr_url:
            log.warning("[OCR体检] 未配置 DDDD_OCR_URL")
            return
        parsed = urlparse(self.ocr_url)
        path = parsed.path.rsplit("/", 1)[0] + "/health"
        health_url = urlunparse(parsed._replace(path=path, query="", fragment=""))
        headers = {}
        if self.ocr_token:
            headers["X-OCR-Token"] = self.ocr_token
        try:
            resp = requests.get(health_url, headers=headers, timeout=(5.0, 15.0))
            log.info("[OCR体检] %s -> HTTP %s %s", health_url, resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            log.warning("[OCR体检] %s 不可达: %s", health_url, exc)

    # ---------------- 验证码 ----------------

    def _fetch_captcha(
        self, session: requests.Session, host: str, captcha_url: str, referer: str
    ) -> Optional[bytes]:
        url = self._abs(host, captcha_url)
        try:
            resp = self._get(
                session,
                url,
                params={"t": str(int(time.time() * 1000))},
                headers={
                    "Referer": referer,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
        except requests.RequestException as exc:
            log.warning("[验证码] 获取图片失败: %s", exc)
            return None

        content = resp.content or b""
        content_type = resp.headers.get("Content-Type", "")
        if resp.status_code != 200 or len(content) < 100:
            log.warning(
                "[验证码] 图片异常: HTTP %s, %s 字节, %s",
                resp.status_code,
                len(content),
                content_type,
            )
            return None
        if "text/html" in content_type.lower() or content[:200].lstrip().startswith(b"<"):
            snippet = content[:200].decode("utf-8", "replace")
            log.warning("[验证码] 返回了 HTML 而不是图片: %s", snippet)
            return None

        if self._captcha_dir:
            try:
                os.makedirs(self._captcha_dir, exist_ok=True)
                path = os.path.join(
                    self._captcha_dir,
                    f"captcha_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png",
                )
                with open(path, "wb") as handle:
                    handle.write(content)
                log.info("[验证码] 已保存调试图片: %s", path)
            except OSError as exc:
                log.warning("[验证码] 保存调试图片失败: %s", exc)

        return content

    # ---------------- 签到 ----------------

    def signin(self, session: requests.Session, host: str) -> bool:
        html = self._get_signin_page(session, host)
        if html is None:
            return False

        uid = self._uid(html)
        if not uid:
            log.error("[签到] 未登录（discuz_uid=0），Cookie 可能已失效或不属于当前域名")
            return False

        cfg = self._parse_signin(html)
        log.info(
            "[签到] uid=%s，状态=%s，签到按钮=%s，签到验证码=%s，formhash=%s",
            uid,
            cfg["status_text"] or ("已签到" if cfg["signed"] else "未知"),
            "有" if cfg["has_button"] else "无",
            "有" if cfg["captcha_required"] else "无",
            cfg["formhash"] or "缺失",
        )

        if cfg["signed"]:
            log.info("[签到] 今日已签到，无需重复签到")
            return True

        if not cfg["has_signin_json"] and not cfg["has_button"]:
            log.warning(
                "[签到] 页面里没有 window.SIGNIN，也没有签到按钮。"
                "可能是用户组不允许签到，或插件页面改版。"
            )

        if not cfg["formhash"]:
            log.error("[签到] 没找到 formhash，无法提交签到")
            return False

        if cfg["captcha_required"] and not self.ocr_url:
            log.error(
                "[签到] 该账号需要图形验证码，但未配置 DDDD_OCR_URL，无法完成签到"
            )
            return False

        ajax_url = self._abs(host, cfg["ajax"])
        referer = self._base(host) + SIGNIN_PAGE
        attempts = self.max_attempts if cfg["captcha_required"] else 1

        for attempt in range(1, attempts + 1):
            captcha = ""
            if cfg["captcha_required"]:
                image = self._fetch_captcha(
                    session, host, cfg["captcha_url"], referer
                )
                if not image:
                    time.sleep(1.0)
                    continue
                captcha = self.ocr(image)
                if not captcha:
                    time.sleep(0.5)
                    continue

            if self.dry_run:
                log.info(
                    "[dry-run] 已拿到 formhash 和验证码 %s，不提交签到请求",
                    captcha or "(无验证码)",
                )
                return True

            payload = {"op": "signin", "formhash": cfg["formhash"]}
            if captcha:
                payload["seccode"] = captcha

            try:
                resp = self._post(
                    session,
                    ajax_url,
                    data=payload,
                    headers={
                        "Referer": referer,
                        "Origin": self._base(host),
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json, text/javascript, */*; q=0.01",
                    },
                )
            except requests.RequestException as exc:
                log.warning("[签到] 第 %s/%s 次请求失败: %s", attempt, attempts, exc)
                time.sleep(1.0)
                continue

            raw = self._html(resp)
            try:
                result = resp.json()
            except ValueError:
                log.warning(
                    "[签到] 第 %s/%s 次返回非 JSON（HTTP %s）: %s",
                    attempt,
                    attempts,
                    resp.status_code,
                    strip_tags(raw)[:200],
                )
                # 登录态掉了 / formhash 过期时，重新拿一次页面
                refreshed = self._get_signin_page(session, host)
                if refreshed is None:
                    return False
                new_cfg = self._parse_signin(refreshed)
                if new_cfg["signed"]:
                    log.info("[签到] 页面显示已签到")
                    return True
                cfg = new_cfg
                continue

            if not isinstance(result, dict):
                log.warning("[签到] 返回结构异常: %r", result)
                continue

            try:
                code = int(result.get("code"))
            except (TypeError, ValueError):
                code = -1
            message = str(result.get("msg") or result.get("message") or "")
            data = result.get("data")

            if code == 0:
                detail = format_signin_data(data)
                log.info("[签到] 成功%s", f"：{detail}" if detail else "")
                return True

            if code == 2:
                log.info("[签到] %s（按已签到处理）", message or "今日已签到")
                return True

            if code == 11:
                log.warning(
                    "[签到] 第 %s/%s 次验证码错误，换一张重试",
                    attempt,
                    attempts,
                )
                time.sleep(0.4)
                continue

            if "已签到" in message or "已签" in message:
                log.info("[签到] %s", message)
                return True

            if (
                "请先登录" in message
                or "未登录" in message
                or ("登录" in message and "失效" in message)
            ):
                log.error("[签到] 登录态失效: %s", message)
                return False

            log.warning("[签到] 第 %s/%s 次失败: code=%s, msg=%s", attempt, attempts, code, message)
            if "formhash" in message or "来路" in message:
                refreshed = self._get_signin_page(session, host)
                if refreshed is None:
                    return False
                cfg = self._parse_signin(refreshed)
                if cfg["signed"]:
                    return True
            time.sleep(0.8)

        log.error("[签到] 已尝试 %s 次仍未成功", attempts)
        return False

    # ---------------- 邮箱密码登录 ----------------

    def _get_login_page(
        self, session: requests.Session, host: str
    ) -> Optional[Dict[str, Any]]:
        try:
            resp = self._get(session, self._base(host) + LOGIN_PAGE)
        except requests.RequestException as exc:
            log.error("[登录] 获取登录页失败: %s", exc)
            return None

        html = self._html(resp)
        if self._is_challenge(resp, html):
            log.error("[登录] 登录页被 Cloudflare 拦截")
            return None

        uid = self._uid(html)
        formhash_match = re.search(
            r'name="formhash"\s+value="([a-zA-Z0-9]{8})"', html
        )
        seccode_match = (
            re.search(r"seccode_([a-zA-Z0-9]{6})", html)
            or re.search(r"idhash=([a-zA-Z0-9]{6})", html)
        )
        loginhash_match = (
            re.search(r"main_messaqge_([a-zA-Z0-9]{5})", html)
            or re.search(r"loginhash=([a-zA-Z0-9]+)", html)
        )

        return {
            "html": html,
            "uid": uid,
            "formhash": formhash_match.group(1) if formhash_match else "",
            "seccodehash": seccode_match.group(1) if seccode_match else "",
            "loginhash": loginhash_match.group(1) if loginhash_match else "",
        }

    def _fetch_login_captcha(
        self, session: requests.Session, host: str, seccodehash: str
    ) -> Optional[bytes]:
        url = (
            self._base(host)
            + f"/misc.php?mod=seccode&update={random.randint(10000, 99999)}"
            + f"&idhash={seccodehash}"
        )
        try:
            resp = self._get(
                session,
                url,
                headers={
                    "Referer": self._base(host) + "/member.php?mod=logging&action=login",
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
        except requests.RequestException as exc:
            log.warning("[登录验证码] 获取失败: %s", exc)
            return None

        content = resp.content or b""
        if resp.status_code != 200 or len(content) < 100:
            log.warning(
                "[登录验证码] 图片异常: HTTP %s, %s 字节",
                resp.status_code,
                len(content),
            )
            return None
        if "text/html" in resp.headers.get("Content-Type", "").lower():
            return None
        return content

    def login(
        self, session: requests.Session, host: str, email: str, password: str
    ) -> bool:
        if not self.ocr_url:
            log.error(
                "[登录] 邮箱密码登录需要 DDDD_OCR_URL 识别登录验证码；"
                "未配置时请改用 Cookie。"
            )
            return False

        for page_attempt in range(1, 4):
            info = self._get_login_page(session, host)
            if info is None:
                return False
            if info["uid"]:
                log.info("[登录] 当前会话已经是登录状态，uid=%s", info["uid"])
                return True
            if not info["formhash"]:
                log.error("[登录] 没找到 formhash，登录页结构可能变了")
                return False

            captcha = ""
            if info["seccodehash"]:
                for captcha_attempt in range(1, 4):
                    image = self._fetch_login_captcha(
                        session, host, info["seccodehash"]
                    )
                    if image:
                        captcha = self.ocr(image)
                        if captcha:
                            break
                    log.warning(
                        "[登录] 验证码第 %s/3 次识别失败，换一张",
                        captcha_attempt,
                    )
                    time.sleep(0.5)
                if not captcha:
                    log.warning("[登录] 连续 3 次没识别出验证码，重新打开登录页")
                    continue
            else:
                log.info("[登录] 登录页没有验证码，直接提交")

            payload = {
                "formhash": info["formhash"],
                "referer": self._base(host) + "/",
                "loginfield": "email",
                "username": email,
                "password": password,
                "questionid": "0",
                "answer": "",
                "seccodehash": info["seccodehash"],
                "seccodemodid": "member::logging",
                "seccodeverify": captcha,
                "cookietime": "2592000",
            }
            url = (
                self._base(host)
                + "/member.php?mod=logging&action=login&loginsubmit=yes"
                + f"&loginhash={info['loginhash']}&inajax=1"
            )
            try:
                resp = self._post(
                    session,
                    url,
                    data=payload,
                    headers={
                        "Referer": self._base(host) + "/member.php?mod=logging&action=login",
                        "Origin": self._base(host),
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
            except requests.RequestException as exc:
                log.error("[登录] 提交失败: %s", exc)
                continue

            text = self._html(resp)
            if self._login_succeeded(session, host):
                log.info("[登录] 成功: %s", email)
                return True

            if "验证码" in text or "seccode" in text.lower():
                log.warning("[登录] 验证码未通过，重新登录（第 %s/3 轮）", page_attempt)
                time.sleep(1.0)
                continue

            snippet = strip_tags(text)[:300]
            log.error("[登录] 失败: %s", snippet or f"HTTP {resp.status_code}")
            if any(
                mark in text
                for mark in ("密码", "用户名", "安全提问", "登录失败", "抱歉", "不存在")
            ):
                return False

        log.error("[登录] 多轮尝试后仍未登录成功")
        return False

    def _login_succeeded(self, session: requests.Session, host: str) -> bool:
        html = self._get_signin_page(session, host)
        return bool(html and self._uid(html) > 0)

    # ---------------- 单账号 / 总流程 ----------------

    def run_account(self, account: Dict[str, Any], host: str, index: int) -> bool:
        session = self.new_session()
        if account.get("cookie"):
            log.info("[账号%s] 使用 Cookie 登录", index)
            self.set_cookies(session, account["cookie"])
            html = self._get_signin_page(session, host)
            if html is None:
                return False
            uid = self._uid(html)
            if not uid:
                log.error(
                    "[账号%s] Cookie 无效（discuz_uid=0）。"
                    "如果网站换过域名，请在当前域名重新登录后重新复制 Cookie。",
                    index,
                )
                return False
            log.info("[账号%s] Cookie 有效，uid=%s", index, uid)
        else:
            log.info("[账号%s] 使用邮箱密码登录: %s", index, account.get("email"))
            if not self.login(session, host, account["email"], account["password"]):
                return False

        return self.signin(session, host)

    def run(self) -> int:
        started = time.time()
        log.info("========== %s 开始 ==========", self.site_name)
        accounts = self.check_env()
        if not accounts:
            log.error("没有可用账号，退出")
            return 1

        host = self.get_host()
        log.info("[运行] 站点: %s://%s，账号数: %s", self.scheme, host, len(accounts))
        if not self.ocr_url:
            log.warning(
                "[运行] 未配置 DDDD_OCR_URL。新版签到需要图形验证码，"
                "Cookie 登录也会在签到环节失败。"
            )

        failures = 0
        for index, account in enumerate(accounts, 1):
            log.info("---------- 账号%s 开始 ----------", index)
            try:
                ok = self.run_account(account, host, index)
            except Exception as exc:  # noqa: BLE001 - 单个账号失败不能拖垮其他账号
                ok = False
                log.error("[账号%s] 未捕获异常: %s", index, exc)
                log.debug("%s", traceback.format_exc())
            if ok:
                log.info("[账号%s] 任务完成", index)
            else:
                failures += 1
                log.error("[账号%s] 任务失败", index)
            log.info("---------- 账号%s 结束 ----------", index)

        elapsed = time.time() - started
        log.info(
            "========== %s 结束：成功 %s，失败 %s，用时 %.1fs ==========",
            self.site_name,
            len(accounts) - failures,
            failures,
            elapsed,
        )
        return 0 if failures == 0 else 1

    def diagnose(self) -> int:
        log.info("========== 体检模式（不会签到） ==========")
        host = self.get_host()
        log.info("[体检] 站点: %s://%s", self.scheme, host)
        self.ocr_health()

        accounts = self.check_env()
        if not accounts:
            return 1

        bad = 0
        for index, account in enumerate(accounts, 1):
            log.info("---------- 账号%s 体检 ----------", index)
            session = self.new_session()
            if account.get("cookie"):
                self.set_cookies(session, account["cookie"])
            else:
                info = self._get_login_page(session, host)
                if info is None:
                    bad += 1
                    continue
                log.info(
                    "[体检] 邮箱密码账号，登录页 formhash=%s，验证码=%s，loginhash=%s",
                    info["formhash"] or "缺失",
                    "有" if info["seccodehash"] else "无",
                    info["loginhash"] or "缺失",
                )
                continue

            html = self._get_signin_page(session, host)
            if html is None:
                bad += 1
                continue
            uid = self._uid(html)
            cfg = self._parse_signin(html)
            log.info(
                "[体检] Cookie 账号 uid=%s，已签到=%s，状态=%r，"
                "签到按钮=%s，签到验证码=%s，formhash=%s，ajax=%s",
                uid,
                cfg["signed"],
                cfg["status_text"],
                "有" if cfg["has_button"] else "无",
                "有" if cfg["captcha_required"] else "无",
                cfg["formhash"] or "缺失",
                cfg["ajax"],
            )
            if not uid:
                bad += 1
                log.error("[体检] Cookie 无效")
        return 0 if bad == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="尚香书苑自动签到")
    parser.add_argument("--host", help="强制域名，例如 sxsy33.com")
    parser.add_argument(
        "--diagnose", action="store_true", help="只体检域名、登录态、插件和 OCR，不签到"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="识别签到验证码但不提交签到"
    )
    parser.add_argument(
        "--max-attempts", type=int, default=None, help="签到验证码最多尝试次数"
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s\t- %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if args.host:
        os.environ["SXSY_HOST"] = args.host
    if args.max_attempts:
        os.environ["SXSY_MAX_ATTEMPTS"] = str(args.max_attempts)

    task = AutoTask()
    task.dry_run = args.dry_run
    try:
        if args.diagnose:
            return task.diagnose()
        return task.run()
    except KeyboardInterrupt:
        log.warning("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
