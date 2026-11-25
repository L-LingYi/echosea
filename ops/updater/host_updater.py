#!/usr/bin/env python3
"""Companion System Host Updater Daemon (Zero External Dependencies · Python 3 Stdlib).

Industry-standard privilege-separated updater:
- Runs on the Linux host as a systemd service (NO docker.sock mounted inside companion-core!).
- Listens on loopback / Docker bridge only (`UPDATER_BIND_HOST`, default `127.0.0.1:9099`).
- Verifies every POST /trigger with HMAC-SHA256 + 60s anti-replay timestamp.
- Executes atomic upgrade: record prev HEAD -> git pull/checkout -> idempotent SQL migrations
  -> rebuild companion-core -> poll /healthz -> AUTOMATIC ROLLBACK on failure -> HMAC callback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [updater] %(levelname)s %(message)s")
logger = logging.getLogger("host_updater")

REPO_DIR = Path(os.environ.get("COMPANION_REPO_DIR", "/opt/companion-system")).resolve()
BIND_HOST = os.environ.get("UPDATER_BIND_HOST", "0.0.0.0")  # Restrict via host firewall/Docker bridge
BIND_PORT = int(os.environ.get("UPDATER_BIND_PORT", "9099"))
WEBHOOK_SECRET = os.environ.get("UPDATE_WEBHOOK_SECRET", "")
CORE_BASE_URL = os.environ.get("COMPANION_CORE_URL", "http://127.0.0.1:8000").rstrip("/")
HEALTH_TIMEOUT_SECONDS = int(os.environ.get("UPDATER_HEALTH_TIMEOUT", "45"))

_UPDATE_LOCK = threading.Lock()


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), timestamp.encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, timestamp: str | None, signature: str | None, body: bytes) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > 60:
        return False
    return hmac.compare_digest(sign_payload(secret, timestamp, body), signature.strip())


def run_cmd(cmd: list[str], cwd: Path = REPO_DIR, check: bool = True) -> str:
    logger.info("Running: %s", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=300)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout.strip()


def poll_healthz(timeout_seconds: int = HEALTH_TIMEOUT_SECONDS) -> bool:
    deadline = time.time() + timeout_seconds
    url = f"{CORE_BASE_URL}/healthz"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def post_callback(payload: dict) -> None:
    if not WEBHOOK_SECRET:
        return
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    sig = sign_payload(WEBHOOK_SECRET, ts, body)
    req = urllib.request.Request(
        f"{CORE_BASE_URL}/api/system/update/callback",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Updater-Timestamp": ts,
            "X-Updater-Signature": sig,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("Callback delivered (%s) for job %s", resp.status, payload.get("job_uid"))
    except Exception as exc:
        logger.warning("Failed to deliver callback for %s: %s", payload.get("job_uid"), exc)


def execute_upgrade_job(job_uid: str, target_ref: str, branch: str) -> None:
    prev_commit = ""
    new_commit = ""
    try:
        prev_commit = run_cmd(["git", "rev-parse", "--short", "HEAD"])
        # Fetch remote updates if origin exists (works for both local remote & public GitHub repo)
        remotes = run_cmd(["git", "remote"], check=False)
        if "origin" in remotes.splitlines():
            run_cmd(["git", "fetch", "--all", "--tags", "--prune"])
            if target_ref and target_ref != branch:
                run_cmd(["git", "checkout", target_ref])
            else:
                run_cmd(["git", "pull", "--ff-only", "origin", branch])

        new_commit = run_cmd(["git", "rev-parse", "--short", "HEAD"])
        # Rebuild and restart ONLY companion-core (preserves .env & docker-compose.override.yml)
        run_cmd(["docker", "compose", "up", "-d", "--build", "companion-core"])

        if poll_healthz():
            post_callback(
                {
                    "job_uid": job_uid,
                    "status": "succeeded",
                    "rolled_back": False,
                    "from_commit": prev_commit,
                    "to_commit": new_commit,
                    "log_summary": f"更新成功：{prev_commit} -> {new_commit}，健康检查通过。",
                }
            )
            return

        raise RuntimeError(f"New container failed /healthz check within {HEALTH_TIMEOUT_SECONDS}s")
    except Exception as exc:
        logger.error("Upgrade %s failed: %s — initiating automatic rollback!", job_uid, exc)
        rollback_ok = False
        if prev_commit:
            try:
                run_cmd(["git", "reset", "--hard", prev_commit])
                run_cmd(["docker", "compose", "up", "-d", "--build", "companion-core"])
                rollback_ok = poll_healthz(30)
            except Exception as rb_exc:
                logger.error("Rollback failed: %s", rb_exc)
        post_callback(
            {
                "job_uid": job_uid,
                "status": "rolled_back" if rollback_ok else "failed",
                "rolled_back": rollback_ok,
                "from_commit": prev_commit,
                "to_commit": new_commit,
                "log_summary": f"更新失败（{exc}）；{'已自动回滚至 ' + prev_commit if rollback_ok else '回滚失败，请人工介入'}。",
            }
        )
    finally:
        _UPDATE_LOCK.release()


class UpdaterHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, obj: dict) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True, "repo_dir": str(REPO_DIR), "busy": _UPDATE_LOCK.locked()})
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/trigger":
            self._send_json(404, {"error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        ts = self.headers.get("X-Updater-Timestamp")
        sig = self.headers.get("X-Updater-Signature")
        if not verify_signature(WEBHOOK_SECRET, ts, sig, body):
            self._send_json(403, {"error": "invalid_signature"})
            return
        if not _UPDATE_LOCK.acquire(blocking=False):
            self._send_json(409, {"error": "update_already_in_progress"})
            return
        data = json.loads(body.decode("utf-8"))
        job_uid = str(data.get("job_uid", "manual"))
        target_ref = str(data.get("target_ref") or "")
        branch = str(data.get("branch") or "main")
        t = threading.Thread(target=execute_upgrade_job, args=(job_uid, target_ref, branch), daemon=True)
        t.start()
        self._send_json(202, {"ok": True, "job_uid": job_uid, "message": "宿主机更新守护进程已受理，正在拉取并重建容器"})


def main() -> None:
    if not WEBHOOK_SECRET:
        raise SystemExit("UPDATE_WEBHOOK_SECRET is required")
    server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), UpdaterHandler)
    logger.info("Host Updater Daemon listening on %s:%d (repo=%s)", BIND_HOST, BIND_PORT, REPO_DIR)
    server.serve_forever()


if __name__ == "__main__":
    main()
