#!/usr/bin/env python3
"""
ddddocr HTTP shim for 尚香书苑 / Discuz captchas.

Why this exists
---------------
The check-in script calls an OCR endpoint in this shape:

    POST /ocr
    Content-Type: application/x-www-form-urlencoded

    image=<base64 png/jpeg>

and expects:

    {"code": 200, "data": "CJET"}

The stock `python -m ddddocr api` server does not speak that dialect
(it takes JSON and returns {"result": ...}), so this small FastAPI app
wraps ddddocr and translates both directions.  It also accepts JSON,
multipart file uploads (handy for `curl -F image=@captcha.png`) and an
optional shared token.

Environment
-----------
OCR_TOKEN       Optional shared secret.  When set, requests must send
                X-OCR-Token: <token>, Authorization: Bearer <token>,
                a `token` form field, or ?token=<token>.
OCR_BETA        "1" to load ddddocr's beta model (default "0").
OCR_UPPERCASE   "1" to upper-case the result (default "1"; Discuz
                captchas are upper-case and the check is case-insensitive).
OCR_MAX_SIDE    Optional max image side in pixels (default 0 = no resize).
PORT            Listen port when run directly (default 8000).
"""

from __future__ import annotations

import base64
import binascii
import hmac
import io
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import ddddocr
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("sxsy-ocr")

OCR_TOKEN = os.getenv("OCR_TOKEN", "").strip()
OCR_BETA = os.getenv("OCR_BETA", "0").strip().lower() in {"1", "true", "yes", "on"}
OCR_UPPERCASE = os.getenv("OCR_UPPERCASE", "1").strip().lower() in {"1", "true", "yes", "on"}
OCR_MAX_SIDE = int(os.getenv("OCR_MAX_SIDE", "0") or 0)

_engine: Optional[ddddocr.DdddOcr] = None
_engine_error: Optional[str] = None
_infer_lock = threading.Lock()


def _new_engine() -> ddddocr.DdddOcr:
    kwargs: dict[str, Any] = {"show_ad": False}
    try:
        kwargs["beta"] = OCR_BETA
        return ddddocr.DdddOcr(**kwargs)
    except TypeError:
        # Older ddddocr releases do not know the beta argument.
        kwargs.pop("beta", None)
        return ddddocr.DdddOcr(**kwargs)


def _warm_up() -> None:
    """Force model/ONNX initialisation before the first real request."""
    assert _engine is not None
    buf = io.BytesIO()
    Image.new("RGB", (100, 30), "white").save(buf, format="PNG")
    try:
        _engine.classification(buf.getvalue())
    except Exception:  # warm-up is best effort
        log.exception("warm-up inference failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    global _engine, _engine_error
    started = time.time()
    try:
        _engine = _new_engine()
        _warm_up()
        log.info(
            "ddddocr ready (beta=%s, uppercase=%s, init=%.2fs)",
            OCR_BETA,
            OCR_UPPERCASE,
            time.time() - started,
        )
    except Exception as exc:  # pragma: no cover - startup failure path
        _engine_error = f"{type(exc).__name__}: {exc}"
        log.exception("failed to initialise ddddocr")
    yield


app = FastAPI(
    title="sxsy ddddocr shim",
    description="Form/JSON base64 in, {code:200,data:'...'} out.",
    version="1.0.0",
    lifespan=lifespan,
)


def _fail(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"code": status, "message": message}, status_code=status)


def _token_from_request(request: Request, form_token: Optional[str]) -> str:
    supplied = request.headers.get("x-ocr-token", "").strip()
    if not supplied:
        auth = request.headers.get("authorization", "").strip()
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
    if not supplied and form_token:
        supplied = str(form_token).strip()
    if not supplied:
        supplied = request.query_params.get("token", "").strip()
    return supplied


def _decode_base64(value: str) -> bytes:
    text = value.strip()
    if text.startswith("data:"):
        # data:image/png;base64,xxxx
        _, _, text = text.partition(",")
    # Some clients send '+' as a literal space in form bodies.
    text = text.replace(" ", "+").replace("\n", "").replace("\r", "")
    text = text.translate(str.maketrans("-_", "+/"))
    text += "=" * (-len(text) % 4)
    return base64.b64decode(text, validate=False)


def _normalise_image(raw: bytes) -> bytes:
    """Validate and re-encode to PNG; also gives us a clear error for junk."""
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        if OCR_MAX_SIDE and max(im.size) > OCR_MAX_SIDE:
            ratio = OCR_MAX_SIDE / float(max(im.size))
            im = im.resize(
                (max(1, int(im.width * ratio)), max(1, int(im.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        out = io.BytesIO()
        im.save(out, format="PNG", optimize=True)
        return out.getvalue()


async def _read_image(request: Request) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
    """Return (image_bytes, form_token, error_message)."""
    content_type = request.headers.get("content-type", "").lower()

    if "application/json" in content_type:
        try:
            payload = await request.json()
        except Exception:
            return None, None, "invalid JSON body"
        if not isinstance(payload, dict):
            return None, None, "JSON body must be an object"
        value = payload.get("image") or payload.get("img") or payload.get("file")
        form_token = payload.get("token")
        if isinstance(value, str):
            try:
                return _decode_base64(value), str(form_token or ""), None
            except (binascii.Error, ValueError) as exc:
                return None, None, f"invalid base64: {exc}"
        return None, None, "missing image"

    if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
        try:
            form = await request.form()
        except Exception as exc:
            return None, None, f"invalid form body: {exc}"
        value = form.get("image") or form.get("img") or form.get("file")
        form_token = form.get("token")
        if value is None:
            return None, None, "missing image"
        if hasattr(value, "read"):  # UploadFile
            raw = await value.read()
            return raw or None, str(form_token or ""), None
        if isinstance(value, str):
            try:
                return _decode_base64(value), str(form_token or ""), None
            except (binascii.Error, ValueError) as exc:
                return None, None, f"invalid base64: {exc}"
        return None, None, "unsupported image field"

    # Last resort: treat the raw body as base64 text.
    raw_body = await request.body()
    if not raw_body:
        return None, None, "empty body"
    try:
        return _decode_base64(raw_body.decode("ascii", "ignore")), "", None
    except (binascii.Error, ValueError) as exc:
        return None, None, f"invalid base64: {exc}"


@app.get("/health")
async def health() -> JSONResponse:
    ok = _engine is not None and _engine_error is None
    body = {
        "status": "ok" if ok else "error",
        "engine": "ddddocr",
        "beta": OCR_BETA,
        "uppercase": OCR_UPPERCASE,
        "error": _engine_error,
    }
    return JSONResponse(body, status_code=200 if ok else 503)


@app.post("/ocr")
async def ocr(request: Request) -> JSONResponse:
    if _engine is None:
        return _fail(_engine_error or "OCR engine not initialised", 503)

    try:
        content_length = int(request.headers.get("content-length") or 0)
    except ValueError:
        content_length = 0
    if content_length > 2_000_000:
        return _fail("image too large", 413)

    started = time.perf_counter()
    image, form_token, error = await _read_image(request)
    if error:
        return _fail(error, 422)

    if OCR_TOKEN:
        supplied = _token_from_request(request, form_token)
        if not hmac.compare_digest(
            supplied.encode("utf-8", "ignore"), OCR_TOKEN.encode("utf-8")
        ):
            return _fail("unauthorized", 401)

    try:
        image = _normalise_image(image or b"")
    except Exception as exc:
        return _fail(f"unsupported image: {exc}", 422)

    try:
        with _infer_lock:
            text = _engine.classification(image)
    except Exception as exc:  # pragma: no cover - inference failure path
        log.exception("inference failed")
        return _fail(f"inference failed: {exc}", 500)

    text = (text or "").strip()
    if OCR_UPPERCASE:
        text = text.upper()

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    log.info("ocr ok in %.1fms: %r", elapsed_ms, text)
    return JSONResponse(
        {
            "code": 200,
            "data": text,
            "message": "success",
            "elapsed_ms": elapsed_ms,
        }
    )


@app.get("/")
async def index() -> dict[str, Any]:
    return {
        "service": "sxsy ddddocr shim",
        "endpoints": {"ocr": "POST /ocr", "health": "GET /health"},
        "auth": "OCR_TOKEN" if OCR_TOKEN else "none",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
    )
