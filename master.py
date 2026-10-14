"""
The Qari App - MASTER LINK GATEWAY
One public address that fronts 2, 3 or more identical backends.

 - Spreads requests across all healthy backends (round robin)
 - If a backend is down, slow or returns 5xx, the next one is used instantly
 - If a backend dies in the middle of an audio stream, the stream is resumed
   from the exact byte on another backend (HTTP Range)
 - Background health checks also keep free Render services awake
 - Status page: /_master/status

Environment variables (set on the master Render service):
  BACKENDS      comma-separated list of backend base URLs   (required)
  HEALTH_PATH   path used for health checks                 (default "/")
  CHECK_EVERY   seconds between health checks               (default 25)
  CONNECT_TO    connect timeout seconds                     (default 6)
  READ_TO       read timeout seconds                        (default 60)
  COOLDOWN      seconds a failed backend is skipped         (default 30)
  PORT          set by Render automatically
"""
import os
import re
import threading
import time
import itertools

import requests
from flask import Flask, Response, jsonify, request

BACKENDS = [
    b.strip().rstrip("/")
    for b in os.environ.get(
        "BACKENDS",
        "https://khudem-al-quran.onrender.com,"
        "https://theqariapp-6wrt.onrender.com",
    ).split(",")
    if b.strip()
]
HEALTH_PATH = os.environ.get("HEALTH_PATH", "/")
CHECK_EVERY = int(os.environ.get("CHECK_EVERY", "25"))
CONNECT_TO = float(os.environ.get("CONNECT_TO", "6"))
READ_TO = float(os.environ.get("READ_TO", "60"))
COOLDOWN = int(os.environ.get("COOLDOWN", "30"))

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "content-encoding",
}

app = Flask(__name__)
lock = threading.Lock()
state = {
    b: {"healthy": True, "down_until": 0.0, "fails": 0, "served": 0,
        "last_check": None, "last_ms": None, "last_error": None}
    for b in BACKENDS
}
rr = itertools.count()
http = requests.Session()


# ----------------------------------------------------------------- health
def mark_bad(b, why):
    with lock:
        s = state[b]
        s["healthy"] = False
        s["fails"] += 1
        s["down_until"] = time.time() + COOLDOWN
        s["last_error"] = str(why)[:200]


def mark_good(b, ms=None):
    with lock:
        s = state[b]
        s["healthy"] = True
        s["down_until"] = 0.0
        s["last_error"] = None
        if ms is not None:
            s["last_ms"] = ms


def ordered_backends(exclude=()):
    """Healthy backends first, rotated for load sharing; cooling-down ones last."""
    now = time.time()
    with lock:
        ok = [b for b in BACKENDS if b not in exclude and state[b]["down_until"] <= now]
        bad = [b for b in BACKENDS if b not in exclude and state[b]["down_until"] > now]
    if ok:
        k = next(rr) % len(ok)
        ok = ok[k:] + ok[:k]
    return ok + bad  # last resort: even "bad" ones are tried if nothing else


def health_loop():
    while True:
        for b in BACKENDS:
            t0 = time.time()
            try:
                r = http.get(b + HEALTH_PATH, timeout=(CONNECT_TO, 30))
                if r.status_code < 500:
                    mark_good(b, int((time.time() - t0) * 1000))
                else:
                    mark_bad(b, f"health HTTP {r.status_code}")
            except Exception as e:
                mark_bad(b, e)
            with lock:
                state[b]["last_check"] = time.strftime("%H:%M:%S")
        time.sleep(CHECK_EVERY)


# ------------------------------------------------------------------ proxy
def clean_headers(h):
    return {k: v for k, v in h.items() if k.lower() not in HOP_BY_HOP}


def fetch(b, path, headers, body):
    url = b + "/" + path
    return http.request(
        request.method, url,
        params=request.args if False else None,  # query is already in `path`
        headers=headers, data=body, stream=True,
        allow_redirects=False, timeout=(CONNECT_TO, READ_TO),
    )


def parse_range_start(resp):
    """Return (start, end) byte positions of this response, or None."""
    cr = resp.headers.get("Content-Range", "")
    m = re.match(r"bytes (\d+)-(\d+)/", cr)
    if m:
        return int(m.group(1)), int(m.group(2))
    cl = resp.headers.get("Content-Length")
    if resp.status_code == 200 and cl and cl.isdigit():
        return 0, int(cl) - 1
    return None


def stream_with_resume(first_resp, first_backend, path, headers, span):
    """Yield the body; if the backend dies mid-stream, continue on another."""
    start, end = span
    sent = 0
    resp, backend = first_resp, first_backend
    tried = {backend}
    while True:
        try:
            for chunk in resp.iter_content(64 * 1024):
                if chunk:
                    sent += len(chunk)
                    yield chunk
            return  # finished normally
        except Exception as e:
            mark_bad(backend, f"stream broke: {e}")
            try:
                resp.close()
            except Exception:
                pass
            if start + sent > end:
                return
            resp = None
            for b in ordered_backends(exclude=tried):
                h = dict(headers)
                h["Range"] = f"bytes={start + sent}-{end}"
                try:
                    r = fetch(b, path, h, None)
                    if r.status_code == 206:
                        resp, backend = r, b
                        tried.add(b)
                        break
                    r.close()
                except Exception as e2:
                    mark_bad(b, e2)
            if resp is None:
                return  # nothing left to resume from


@app.route("/_master/status")
def status():
    with lock:
        return jsonify({
            "master": "ok",
            "backends": state,
            "healthy_count": sum(1 for s in state.values() if s["down_until"] <= time.time()),
            "total": len(BACKENDS),
        })


@app.route("/", defaults={"path": ""}, methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"])
@app.route("/<path:path>", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS"])
def proxy(path):
    full = path + (("?" + request.query_string.decode()) if request.query_string else "")
    headers = clean_headers(request.headers)
    headers["Accept-Encoding"] = "identity"  # keep byte positions exact for resume
    body = request.get_data() if request.method not in ("GET", "HEAD") else None

    last_err = "no backends configured"
    for b in ordered_backends():
        try:
            r = fetch(b, full, headers, body)
        except Exception as e:
            mark_bad(b, e)
            last_err = str(e)
            continue
        if r.status_code >= 500:
            mark_bad(b, f"HTTP {r.status_code}")
            last_err = f"{b} -> HTTP {r.status_code}"
            r.close()
            continue

        mark_good(b)
        with lock:
            state[b]["served"] += 1

        out_headers = clean_headers(r.headers)
        out_headers["X-Served-By"] = b.split("//")[-1]
        loc = out_headers.get("Location")
        if loc and loc.startswith(b):  # keep redirects on the master address
            out_headers["Location"] = loc[len(b):] or "/"

        span = parse_range_start(r) if request.method == "GET" and r.status_code in (200, 206) else None
        if span:
            if r.headers.get("Content-Length"):
                out_headers["Content-Length"] = r.headers["Content-Length"]
            body_iter = stream_with_resume(r, b, full, headers, span)
        else:
            body_iter = r.iter_content(64 * 1024)
        return Response(body_iter, status=r.status_code, headers=out_headers)

    return Response(f"All backends are unavailable. Last error: {last_err}",
                    status=503, headers={"Retry-After": "10"})


if __name__ == "__main__":
    threading.Thread(target=health_loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    print("Master link running. Backends:", BACKENDS)
    app.run(host="0.0.0.0", port=port, threaded=True)
