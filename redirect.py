import os
import time
import requests
from flask import Flask, redirect, request

BACKENDS = [
    b.strip().rstrip("/")
    for b in os.environ.get(
        "BACKENDS",
        "https://theqariapp-6wrt.onrender.com,https://khudem-al-quran.onrender.com",
    ).split(",")
    if b.strip()
]
CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "60"))
CHECK_TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "8"))

app = Flask(__name__)
_cache = {"target": None, "at": 0.0}


def pick_backend():
    """Return the first backend that responds. Results are cached briefly."""
    now = time.time()
    if _cache["target"] and now - _cache["at"] < CACHE_SECONDS:
        return _cache["target"]

    for b in BACKENDS:
        try:
            r = requests.get(b + "/", timeout=CHECK_TIMEOUT)
            if r.status_code < 500:
                _cache.update(target=b, at=now)
                return b
        except Exception:
            continue

    # Nothing answered. Fall back to the first backend so the user still gets a page.
    return BACKENDS[0]


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def go(path):
    target = pick_backend()
    url = f"{target}/{path}"
    if request.query_string:
        url += "?" + request.query_string.decode()
    return redirect(url, code=302)


@app.route("/_redirect/status")
def status():
    return {"backends": BACKENDS, "current": _cache["target"]}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)