#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import subprocess
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, JSONResponse, FileResponse

app = FastAPI(title="NXP AutoNews Service", version="1.1")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(BASE_DIR, "scripts")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

PYTHON_BIN = os.path.join(BASE_DIR, "venv", "bin", "python3")

# ---------- Scripts ----------
FETCHER_PATH = os.path.join(SCRIPTS_DIR, "fetcher.py")
PROCESSOR_PATH = os.path.join(SCRIPTS_DIR, "processor.py")
PROCESSOR2_PATH = os.path.join(SCRIPTS_DIR, "processor2.py")

# ---------- Logs + PID ----------
FETCHER_LOG = os.path.join(LOG_DIR, "fetcher.log")
PROCESSOR_LOG = os.path.join(LOG_DIR, "processor.log")
PROCESSOR2_LOG = os.path.join(LOG_DIR, "processor2.log")

FETCHER_PID_FILE = os.path.join(LOG_DIR, "fetcher.pid")
PROCESSOR_PID_FILE = os.path.join(LOG_DIR, "processor.pid")
PROCESSOR2_PID_FILE = os.path.join(LOG_DIR, "processor2.pid")


# =========================
# Helpers
# =========================
def _is_running(pid: int) -> bool:
    """
    True if PID exists and is not a zombie.
    """
    try:
        os.kill(pid, 0)
    except Exception:
        return False

    # If /proc exists, ensure not zombie
    stat_path = f"/proc/{pid}/stat"
    try:
        if os.path.exists(stat_path):
            with open(stat_path, "r") as f:
                stat = f.read()
            # field 3 is state, 'Z' means zombie
            parts = stat.split()
            if len(parts) >= 3 and parts[2] == "Z":
                return False
    except Exception:
        pass

    return True


def _read_pid(pid_file: str):
    try:
        if not os.path.exists(pid_file):
            return None
        with open(pid_file, "r") as f:
            s = (f.read() or "").strip()
        return int(s) if s else None
    except Exception:
        return None


def _write_pid(pid_file: str, pid: int) -> None:
    with open(pid_file, "w") as f:
        f.write(str(pid))


def _tail(path: str, lines: int = 200) -> str:
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "rb") as f:
            data = f.read()
        out_lines = data.splitlines()[-max(1, min(int(lines), 5000)):]
        return b"\n".join(out_lines).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _start(script_path: str, pid_file: str, log_path: str):
    if not os.path.exists(script_path):
        raise HTTPException(status_code=500, detail="Script not found: %s" % script_path)

    # if pid exists and still running => do not restart
    pid = _read_pid(pid_file)
    if pid and _is_running(pid):
        return {"status": "already_running", "pid": pid, "log": log_path}

    # open log in BINARY append mode
    logf = open(log_path, "ab", buffering=0)
    logf.write(("\n\n===== RUN START %s : %s =====\n" %
                (time.strftime("%Y-%m-%d %H:%M:%S"), os.path.basename(script_path))).encode("utf-8"))
    logf.flush()

    p = subprocess.Popen(
        [PYTHON_BIN, "-u", script_path],
        cwd=BASE_DIR,
        stdout=logf,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )

    _write_pid(pid_file, p.pid)
    return {"status": "started", "pid": p.pid, "log": log_path}


def _wait_pid(pid_file: str, timeout_seconds: int, poll_seconds: int = 5) -> bool:
    """
    Wait until pid exits. Returns True if finished, False if timeout.
    """
    start = time.time()
    while True:
        pid = _read_pid(pid_file)
        if not pid:
            return True
        if not _is_running(pid):
            return True
        if (time.time() - start) >= timeout_seconds:
            return False
        time.sleep(poll_seconds)


# =========================
# Health
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}


# =========================
# Run endpoints
# =========================
@app.post("/run-fetcher")
def run_fetcher():
    return _start(FETCHER_PATH, FETCHER_PID_FILE, FETCHER_LOG)


@app.post("/run-processor")
def run_processor():
    matched = os.path.join(DATA_DIR, "matched_news.json")
    if not os.path.exists(matched):
        raise HTTPException(status_code=400, detail="matched_news.json missing. Run /run-fetcher first.")
    return _start(PROCESSOR_PATH, PROCESSOR_PID_FILE, PROCESSOR_LOG)


@app.post("/run-processor2")
def run_processor2():
    processed = os.path.join(DATA_DIR, "processed_news.json")
    if not os.path.exists(processed):
        raise HTTPException(status_code=400, detail="processed_news.json missing. Run /run-processor first.")
    return _start(PROCESSOR2_PATH, PROCESSOR2_PID_FILE, PROCESSOR2_LOG)


# =========================
# Status endpoints
# =========================
@app.get("/fetcher-status")
def fetcher_status():
    pid = _read_pid(FETCHER_PID_FILE)
    return {"running": bool(pid and _is_running(pid)), "pid": pid, "log": FETCHER_LOG}


@app.get("/processor-status")
def processor_status():
    pid = _read_pid(PROCESSOR_PID_FILE)
    return {"running": bool(pid and _is_running(pid)), "pid": pid, "log": PROCESSOR_LOG}


@app.get("/processor2-status")
def processor2_status():
    pid = _read_pid(PROCESSOR2_PID_FILE)
    return {"running": bool(pid and _is_running(pid)), "pid": pid, "log": PROCESSOR2_LOG}


# =========================
# Logs + Files
# =========================
@app.get("/files")
def list_files():
    files = []
    for name in sorted(os.listdir(DATA_DIR)):
        p = os.path.join(DATA_DIR, name)
        if os.path.isfile(p):
            files.append(name)
    return {"files": files}


@app.get("/logs/{name}")
def read_log(name: str, lines: int = 200):
    safe = os.path.basename(name)
    path = os.path.join(LOG_DIR, safe)
    return PlainTextResponse(_tail(path, lines=lines))


@app.get("/data/{name}")
def get_data_file(name: str):
    """
    Download or view files from /data safely.
    """
    safe = os.path.basename(name)
    path = os.path.join(DATA_DIR, safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found: %s" % safe)

    # allow only common outputs
    if not (safe.endswith(".json") or safe.endswith(".txt") or safe.endswith(".log")):
        raise HTTPException(status_code=400, detail="Only .json/.txt/.log allowed")

    return FileResponse(path)


@app.get("/final-news")
def final_news():
    """
    Returns final_news.json for frontend/JSP.
    """
    path = os.path.join(DATA_DIR, "final_news.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="final_news.json not found. Run processor2 first.")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = f.read()
        # return raw JSON
        return JSONResponse(content=__import__("json").loads(data))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to read final_news.json: %s" % str(e))


# =========================
# Pipeline endpoints (useful for CRON)
# =========================
@app.post("/run-pipeline")
def run_pipeline(
    clean: bool = True,
    fetcher_timeout_sec: int = 3600,
    processor_timeout_sec: int = 7200,
    processor2_timeout_sec: int = 7200,
):
    """
    Runs fetcher -> processor -> processor2 sequentially (blocking).
    Best for cron: single call and you get one JSON response when done.

    clean=true will delete old artifacts so processor doesn't read stale json.
    """

    if clean:
        # remove outputs so every run is fresh
        cleanup = [
            os.path.join(DATA_DIR, "matched_news.json"),
            os.path.join(DATA_DIR, "raw_news_data.txt"),
            os.path.join(DATA_DIR, "processed_news.json"),
            os.path.join(DATA_DIR, "processed_news.txt"),
            os.path.join(DATA_DIR, "rejected_news.json"),
            os.path.join(DATA_DIR, "rejected_news.txt"),
            os.path.join(DATA_DIR, "final_news.json"),
            os.path.join(DATA_DIR, "duplicates.json"),
            os.path.join(DATA_DIR, "rejected2.json"),
        ]
        for p in cleanup:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

        # clear pid files (safe)
        for pidf in [FETCHER_PID_FILE, PROCESSOR_PID_FILE, PROCESSOR2_PID_FILE]:
            try:
                if os.path.exists(pidf):
                    os.remove(pidf)
            except Exception:
                pass

    # 1) fetcher
    r1 = _start(FETCHER_PATH, FETCHER_PID_FILE, FETCHER_LOG)
    ok = _wait_pid(FETCHER_PID_FILE, fetcher_timeout_sec, poll_seconds=5)
    if not ok:
        raise HTTPException(status_code=500, detail="Fetcher timeout. See logs/fetcher.log")

    # 2) processor
    matched = os.path.join(DATA_DIR, "matched_news.json")
    if not os.path.exists(matched):
        raise HTTPException(status_code=500, detail="Fetcher finished but matched_news.json not found. Check fetcher.log")

    r2 = _start(PROCESSOR_PATH, PROCESSOR_PID_FILE, PROCESSOR_LOG)
    ok = _wait_pid(PROCESSOR_PID_FILE, processor_timeout_sec, poll_seconds=5)
    if not ok:
        raise HTTPException(status_code=500, detail="Processor timeout. See logs/processor.log")

    # 3) processor2
    processed = os.path.join(DATA_DIR, "processed_news.json")
    if not os.path.exists(processed):
        raise HTTPException(status_code=500, detail="Processor finished but processed_news.json not found. Check processor.log")

    r3 = _start(PROCESSOR2_PATH, PROCESSOR2_PID_FILE, PROCESSOR2_LOG)
    ok = _wait_pid(PROCESSOR2_PID_FILE, processor2_timeout_sec, poll_seconds=5)
    if not ok:
        raise HTTPException(status_code=500, detail="Processor2 timeout. See logs/processor2.log")

    final_path = os.path.join(DATA_DIR, "final_news.json")
    return {
        "status": "done",
        "fetcher": r1,
        "processor": r2,
        "processor2": r3,
        "final_news_exists": os.path.exists(final_path),
        "final_news_path": final_path,
    }
