"""
proxx.py v4 — proxy de streaming pro CDN PX (pixel-sus) com bypass Cloudflare.

v4: cloudscraper no /px (fingerprint TLS de Chrome) + Referer sushianimes como
    padrão (confirmado empiricamente: residencial + esse referer = 200).
    PX_REFERER continua como override (ex.: PX_REFERER=https://outro/ ou none).

v3: headers de browser + Referer configurável.
v2: integração com FlareSolverr (cf_clearance).

Endpoints:
  GET /health            -> {ok, flare, cookies, idade}
  GET /probe?u=<url>     -> diagnostico (status, content-type, cf-mitigated)
  GET /px?u=<url>        -> stream do mp4 (Range passthrough, pronto pro <video>)
  GET /solve?u=<url>     -> força resolução via FlareSolverr (ou cloudscraper)
  GET /reset             -> limpa cookies guardados

Allowlist: só proxieia hosts em PX_ALLOW (padrão: pixel-sus-4k-image.com).
"""
import os
import threading
import time
from urllib.parse import urlparse

import requests
import cloudscraper
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

ALLOW = {d.strip() for d in os.environ.get(
    "PX_ALLOW", "pixel-sus-4k-image.com").split(",") if d.strip()}

FLARE = os.environ.get("FLARE_URL", "").rstrip("/")
# Referer que vai pro CDN. PADRÃO = sushianimes.com.br (whitelist do pixel-sus).
# Pra trocar: PX_REFERER=https://outro-site/ ; PX_REFERER=none = sem referer.
PX_REFERER = os.environ.get("PX_REFERER", "https://sushianimes.com.br/").strip()

_state = {"cookies": {}, "ua": UA, "ts": 0.0}
_lock = threading.Lock()
_scrap = None


def scraper():
    """Sessão cloudscraper global (fingerprint TLS de Chrome)."""
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


def flare_solve(u: str):
    """Pede pro FlareSolverr abrir a URL num browser e devolver cf_clearance."""
    if not FLARE:
        return False
    try:
        r = requests.post(
            FLARE + "/v1",
            json={"cmd": "request.get", "url": u, "maxTimeout": 55000},
            timeout=90,
        )
        j = r.json()
        if j.get("status") == "ok" and j.get("solution"):
            sol = j["solution"]
            cks = {c["name"]: c["value"] for c in sol.get("cookies", [])}
            with _lock:
                _state["cookies"] = cks
                _state["ua"] = sol.get("userAgent") or UA
                _state["ts"] = time.time()
            print(f"[proxx] FlareSolverr resolveu! cookies: {list(cks)}", flush=True)
            return "cf_clearance" in cks or bool(cks)
    except Exception as e:
        print(f"[proxx] flare erro: {str(e)[:160]}", flush=True)
    return False


def session_get(u, range_val=None, stream=False, timeout=40):
    """Request com cloudscraper (TLS Chrome) + cookies + Referer sushianimes."""
    s = scraper()   # cloudscraper em vez de requests puro
    with _lock:
        ua = _state["ua"] or UA
        cks = dict(_state["cookies"])
    s.headers.update({
        "User-Agent": ua,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    if PX_REFERER and PX_REFERER.lower() != "none":
        s.headers.update({
            "Referer": PX_REFERER,
            "Sec-Fetch-Dest": "video",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Site": "cross-site",
        })
    else:
        s.headers.update({
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        })
    for k, v in cks.items():
        s.cookies.set(k, v, domain="." + urlparse(u).hostname.split(".", 1)[-1])
    h = {}
    if range_val:
        h["Range"] = range_val
    return s.get(u, headers=h, timeout=timeout, stream=stream)


@app.get("/")
def root():
    return jsonify(ok=True, svc="proxx", v=4, allow=sorted(ALLOW),
                   flare=bool(FLARE), referer=(PX_REFERER or "(sem)"))


@app.get("/health")
def health():
    with _lock:
        n = len(_state["cookies"])
        ts = _state["ts"]
    return jsonify(ok=True, flare=bool(FLARE), cookies=n,
                   idade=int(time.time() - ts) if ts else None)


@app.get("/probe")
def probe():
    u = request.args.get("u", "")
    if not allowed(u):
        return jsonify(ok=False, error="host fora da allowlist"), 403
    try:
        r = session_get(u, timeout=25)
        ok = not is_challenge(r)
        out = dict(ok=ok, status=r.status_code,
                   contentType=r.headers.get("content-type"),
                   cfMitigated=r.headers.get("cf-mitigated"),
                   server=r.headers.get("server"),
                   cookies=len(_state["cookies"]))
        r.close()
        return jsonify(out)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502


@app.get("/solve")
def solve():
    u = request.args.get("u") or "https://cdn-s01.pixel-sus-4k-image.com/"
    if not allowed(u):
        return jsonify(ok=False, error="host fora da allowlist"), 403
    if flare_solve(u):
        return jsonify(ok=True, via="flaresolverr", cookies=len(_state["cookies"]))
    try:
        r = scraper().get(u, timeout=40)
        if not is_challenge(r):
            return jsonify(ok=True, via="cloudscraper",
                           cookies=len(scraper().cookies))
        return jsonify(ok=False, via="cloudscraper", status=r.status_code,
                       cfMitigated=r.headers.get("cf-mitigated"),
                       note="define FLARE_URL com um FlareSolverr rodando")
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502


@app.get("/reset")
def reset():
    with _lock:
        _state["cookies"] = {}
        _state["ts"] = 0.0
    return jsonify(ok=True)


@app.get("/px")
def px():
    u = request.args.get("u", "")
    if not allowed(u):
        return jsonify(ok=False, error="host fora da allowlist"), 403
    rg = request.headers.get("range")

    r = None
    try:
        r = session_get(u, range_val=rg, stream=True)
        if is_challenge(r):
            r.close()
            flare_solve(u)
            r = session_get(u, range_val=rg, stream=True)
            if is_challenge(r):
                info = jsonify(ok=False, status=r.status_code,
                               note="desafio cf ativo — chama /solve (precisa FLARE_URL)",
                               cfMitigated=r.headers.get("cf-mitigated"))
                info.status_code = 502
                r.close()
                return info
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200]), 502

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
        print(f"[proxx] waitress na porta {port} (flare={bool(FLARE)})", flush=True)
        serve(app, host="0.0.0.0", port=port, threads=12)
    except ImportError:
        print(f"[proxx] flask dev na porta {port} (flare={bool(FLARE)})", flush=True)
        app.run(host="0.0.0.0", port=port, threaded=True)
