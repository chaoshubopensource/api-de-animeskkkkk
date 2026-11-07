"""
proxx.py — proxy de streaming pro CDN PX (pixel-sus) com bypass Cloudflare.
Deploy: Railway / Render / Koyeb / Fly (ou roda local: python proxx.py)

Endpoints:
  GET /health            -> {ok:true}
  GET /probe?u=<url>     -> diagnostico (status, content-type, cf-mitigated, cookies)
  GET /px?u=<url>        -> stream do mp4 (Range passthrough, pronto pro <video>)
  GET /solve?u=<url>     -> tenta resolver o desafio com cloudscraper e guarda cookie

Allowlist: so proxieia hosts em PX_ALLOW (padrao: pixel-sus-4k-image.com)
pra nao virar proxy aberto e o app nao levar ban.
"""
import os
import threading
from urllib.parse import urlparse

import requests
import cloudscraper
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ALLOW = {d.strip() for d in os.environ.get(
    "PX_ALLOW", "pixel-sus-4k-image.com").split(",") if d.strip()}

_lock = threading.Lock()
_scrap = None


def scraper():
    """Sessao cloudscraper global — o cookie jar persiste entre requests."""
    global _scrap
    with _lock:
        if _scrap is None:
            _scrap = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True},
                delay=8,
            )
            _scrap.headers.update({"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
        return _scrap


def allowed(u: str) -> bool:
    try:
        h = urlparse(u).hostname or ""
        return any(h == d or h.endswith("." + d) for d in ALLOW)
    except Exception:
        return False


def is_challenge(r) -> bool:
    ct = r.headers.get("content-type") or ""
    return r.status_code in (403, 503) or "text/html" in ct


def range_hdr():
    rg = request.headers.get("range")
    return {"Range": rg} if rg else {}


@app.get("/")
def root():
    return jsonify(ok=True, svc="proxx", allow=sorted(ALLOW))


@app.get("/health")
def health():
    return jsonify(ok=True)


@app.get("/probe")
def probe():
    u = request.args.get("u", "")
    if not allowed(u):
        return jsonify(ok=False, error="host fora da allowlist"), 403
    try:
        r = scraper().get(u, timeout=25, headers={"Referer": "https://www.google.com/"})
        return jsonify(
            ok=not is_challenge(r),
            status=r.status_code,
            contentType=r.headers.get("content-type"),
            cfMitigated=r.headers.get("cf-mitigated"),
            server=r.headers.get("server"),
            cookies=len(scraper().cookies),
        )
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502


@app.get("/solve")
def solve():
    u = request.args.get("u") or "https://cdn-s01.pixel-sus-4k-image.com/"
    if not allowed(u):
        return jsonify(ok=False, error="host fora da allowlist"), 403
    try:
        s = scraper()
        r = s.get(u, timeout=40)
        ok = not is_challenge(r)
        return jsonify(ok=ok, status=r.status_code, contentType=r.headers.get("content-type"),
                       cookies=len(s.cookies), cfMitigated=r.headers.get("cf-mitigated"))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502


@app.get("/px")
def px():
    u = request.args.get("u", "")
    if not allowed(u):
        return jsonify(ok=False, error="host fora da allowlist"), 403
    try:
        r = scraper().get(u, timeout=40, stream=True, headers=dict(
            {"Referer": "https://www.google.com/", "Accept": "*/*"}, **range_hdr()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502

    if is_challenge(r):
        info = jsonify(ok=False, status=r.status_code,
                       note="desafio cf ativo nesse IP — tenta /solve",
                       cfMitigated=r.headers.get("cf-mitigated"))
        info.status_code = 502
        r.close()
        return info

    def gen():
        try:
            for chunk in r.iter_content(256 * 1024):
                if chunk:
                    yield chunk
        finally:
            r.close()

    resp = Response(gen(), status=r.status_code)
    for k in ("content-type", "content-length", "content-range"):
        if r.headers.get(k):
            resp.headers[k] = r.headers[k]
    resp.headers["Accept-Ranges"] = "bytes"
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Cache-Control"] = "no-store"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    try:
        from waitress import serve
        print(f"[proxx] waitress na porta {port}")
        serve(app, host="0.0.0.0", port=port, threads=12)
    except ImportError:
        print(f"[proxx] flask dev na porta {port}")
        app.run(host="0.0.0.0", port=port, threaded=True)
