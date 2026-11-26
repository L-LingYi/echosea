#!/usr/bin/env python3
"""Host-side Companion System updater. Uses Linux, Git, Docker and Python's standard library."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger("companion-updater")
REPO = Path(os.environ.get("COMPANION_REPO_DIR", "/opt/companion-system")).resolve()
STATE = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/companion-updater"))
SOURCE = os.environ.get("UPDATER_GIT_SOURCE", "")
BRANCH = os.environ.get("UPDATER_GIT_BRANCH", "main")
BIND_HOST = os.environ.get("UPDATER_BIND_HOST", "127.0.0.1")
CORE_URL = os.environ.get("COMPANION_CORE_URL", "http://127.0.0.1:8000").rstrip("/")
SECRET = os.environ.get("UPDATE_WEBHOOK_SECRET", "")
JOB_ID = re.compile(r"upd_[0-9a-f]{12}")
COMMIT = re.compile(r"[0-9a-f]{40}")
RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
GITHUB_SOURCE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git")
PROTECTED_FILES = (
    "Dockerfile", "docker-compose.yml", "docker-compose.override.yml", ".dockerignore", ".env", ".gitattributes",
)
LOCK = threading.Lock()
CHECK_CACHE_TTL_SECONDS = 30.0
_CHECK_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}


def invalidate_check_cache() -> None:
    _CHECK_CACHE["ts"] = 0.0
    _CHECK_CACHE["data"] = None


def sign(timestamp: str, body: bytes) -> str:
    return hmac.new(SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def run(*args: str, revision: str = "", stdout=None) -> str:
    result = subprocess.run(
        args, cwd=REPO, env={**os.environ, "APP_GIT_COMMIT": revision, "GIT_TERMINAL_PROMPT": "0"},
        stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE, timeout=900,
    )
    if result.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed ({result.returncode})")
    return result.stdout.decode().strip() if stdout is None else ""


def git(*args: str) -> str:
    return run("git", "-c", "core.hooksPath=/dev/null", *args)


def compose(*args: str, revision: str = "", stdout=None) -> str:
    files = ["-f", "docker-compose.yml"]
    if (REPO / "docker-compose.override.yml").exists():
        files += ["-f", "docker-compose.override.yml"]
    return run("docker", "compose", "--env-file", ".env", *files, *args, revision=revision, stdout=stdout)


def candidate(target: str = "") -> tuple[str, str]:
    previous = git("rev-parse", "HEAD")
    git("fetch", "--no-tags", "--no-recurse-submodules", "--", SOURCE, f"refs/heads/{BRANCH}")
    latest = git("rev-parse", "FETCH_HEAD^{commit}")
    if RELEASE_TAG.fullmatch(target) and GITHUB_SOURCE.fullmatch(SOURCE):
        git("fetch", "--no-tags", "--no-recurse-submodules", "--", SOURCE, f"refs/tags/{target}")
        commit = git("rev-parse", "FETCH_HEAD^{commit}")
        git("merge-base", "--is-ancestor", commit, latest)
    else:
        commit = latest
        if target and target not in (BRANCH, commit):
            raise ValueError("候选提交已变化，请重新检查更新")
    git("merge-base", "--is-ancestor", previous, commit)
    return previous, commit


def health(revision: str, seconds: int = 45) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{CORE_URL}/healthz", timeout=2) as response:
                state = json.loads(response.read(8192))
            if state.get("status") == "ok" and state.get("commit") == revision:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def active_container() -> tuple[str, str]:
    values = run("docker", "inspect", "--format", "{{.Image}} {{.Config.Image}}", "companion-core")
    return tuple(values.split())


def backup_database(job_uid: str) -> None:
    backup = STATE / f"{job_uid}.dump"
    with backup.open("xb") as dump:
        compose("exec", "-T", "postgres", "sh", "-ec",
                'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"', stdout=dump)
    with backup.open("rb") as dump:
        result = subprocess.run(
            ["docker", "exec", "-i", "companion-postgres", "pg_restore", "--list"],
            stdin=dump, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60,
        )
    if result.returncode:
        raise RuntimeError("PostgreSQL backup verification failed")


def save(job: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE, delete=False, encoding="utf-8") as output:
        json.dump(job, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(output.name, STATE / f"{job['job_uid']}.json")


def public_result(job: dict) -> dict:
    return {key: job.get(key, "") for key in
            ("job_uid", "status", "rolled_back", "from_commit", "to_commit", "log_summary")}


def callback(job: dict) -> None:
    body = json.dumps(public_result(job), ensure_ascii=False, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    request = urllib.request.Request(
        f"{CORE_URL}/api/system/update/callback", data=body,
        headers={"Content-Type": "application/json", "X-Updater-Timestamp": stamp,
                 "X-Updater-Signature": sign(stamp, body)},
    )
    try:
        with urllib.request.urlopen(request, timeout=8):
            pass
    except (OSError, ValueError):
        logger.warning("Callback unavailable for %s. Stored result can be retrieved by Core.", job["job_uid"])


def restore(job: dict) -> None:
    previous = job["from_commit"]
    git("reset", "--keep", previous)
    run("docker", "tag", job["rollback_image"], job["service_image"])
    try:
        same_container_image = active_container()[0] == job["previous_image"]
    except RuntimeError:
        same_container_image = False
    if not same_container_image or not health(previous, 3):
        compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", "companion-core", revision=previous)
    if not health(previous):
        raise RuntimeError("Previous revision did not recover")


def finish_failure(job: dict, reason: str) -> None:
    if job.get("rollback_image"):
        try:
            restore(job)
            job.update(status="rolled_back", rolled_back=True, log_summary=f"{reason}。已恢复上一提交及镜像。")
            return
        except Exception:
            logger.exception("Rollback failed for %s", job["job_uid"])
            reason += "。回滚失败，需要人工介入"
    job.update(status="failed", rolled_back=False, log_summary=reason)


def upgrade(job: dict, target: str) -> None:
    try:
        if git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("部署仓库的受跟踪文件有未提交修改")
        previous, commit = candidate(target)
        job.update(from_commit=previous, to_commit=commit)
        if not health(previous, 6):
            raise ValueError("当前运行版本与部署仓库不一致，或 Core 未就绪")
        if previous == commit:
            job.update(status="succeeded", log_summary="已是最新提交，Core 未重建。")
            return
        if git("diff", "--name-only", previous, commit, "--", *PROTECTED_FILES):
            raise ValueError("部署配置或构建定义有变更，需要宿主机复核")
        backup_database(job["job_uid"])
        previous_image, service_image = active_container()
        rollback_image = f"companion-updater-rollback:{previous}"
        run("docker", "tag", previous_image, rollback_image)
        job.update(previous_image=previous_image, service_image=service_image, rollback_image=rollback_image)
        save(job)
        git("merge", "--ff-only", commit)
        compose("config", "--quiet", revision=commit)
        compose("build", "companion-core", revision=commit)
        compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", "companion-core", revision=commit)
        if not health(commit):
            raise ValueError("新提交的 /healthz 未通过")
        job.update(status="succeeded", log_summary="新提交健康检查通过。")
    except (ValueError, RuntimeError) as exc:
        logger.warning("Update %s: %s", job["job_uid"], exc)
        finish_failure(job, str(exc))
    except Exception:
        logger.exception("Update %s failed", job["job_uid"])
        finish_failure(job, "宿主机执行更新失败")
    finally:
        try:
            save(job)
        finally:
            LOCK.release()
        callback(job)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def reply(self, code: int, payload: dict) -> None:
        wire = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        stamp = str(int(time.time()))
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(wire)))
            self.send_header("X-Updater-Timestamp", stamp)
            self.send_header("X-Updater-Signature", sign(stamp, wire))
            self.end_headers()
            self.wfile.write(wire)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError) as exc:
            logger.debug("Updater HTTP reply dropped by client: %s", exc)

    def authenticated(self, body: bytes) -> bool:
        stamp = self.headers.get("X-Updater-Timestamp", "")
        signature = self.headers.get("X-Updater-Signature", "")
        return (
            bool(re.fullmatch(r"[0-9]{1,12}", stamp))
            and bool(re.fullmatch(r"[0-9a-f]{64}", signature))
            and abs(int(time.time()) - int(stamp)) <= 60
            and hmac.compare_digest(sign(stamp, body), signature)
        )

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"ok": True, "busy": LOCK.locked()})
            return
        if not self.authenticated(self.path.encode()):
            self.reply(403, {"error": "invalid_signature"})
            return
        if self.path.startswith("/result/"):
            job_uid = self.path.removeprefix("/result/")
            if JOB_ID.fullmatch(job_uid):
                path = STATE / f"{job_uid}.json"
                if path.exists():
                    self.reply(200, public_result(json.loads(path.read_text(encoding="utf-8"))))
                    return
            self.reply(404, {"error": "unknown_job"})
        elif self.path == "/check":
            now_mono = time.monotonic()
            cached = _CHECK_CACHE.get("data")
            if cached is not None and (now_mono - float(_CHECK_CACHE.get("ts", 0.0))) < CHECK_CACHE_TTL_SECONDS:
                self.reply(200, cached)
                return
            if not LOCK.acquire(blocking=False):
                self.reply(409, {"error": "update_in_progress"})
                return
            try:
                previous, commit = candidate()
                payload = {"current_commit": previous, "target_ref": commit, "update_available": previous != commit}
                _CHECK_CACHE["data"] = payload
                _CHECK_CACHE["ts"] = time.monotonic()
                self.reply(200, payload)
            except (ValueError, RuntimeError, subprocess.TimeoutExpired):
                self.reply(503, {"error": "source_unavailable"})
            finally:
                LOCK.release()
        else:
            self.reply(404, {"error": "not_found"})

    def do_POST(self):
        if self.path != "/trigger":
            self.reply(404, {"error": "not_found"})
            return
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,4}", length) or not 0 < int(length) <= 4096:
            self.reply(413, {"error": "invalid_body_length"})
            return
        body = self.rfile.read(int(length))
        if not self.authenticated(body):
            self.reply(403, {"error": "invalid_signature"})
            return
        try:
            data = json.loads(body)
            job_uid = data["job_uid"]
            target = data.get("target_ref", "")
            if not JOB_ID.fullmatch(job_uid) or not isinstance(target, str):
                raise ValueError
            if target not in ("", BRANCH) and not COMMIT.fullmatch(target) and not RELEASE_TAG.fullmatch(target):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            self.reply(400, {"error": "invalid_job"})
            return
        if not LOCK.acquire(blocking=False):
            self.reply(409, {"error": "update_in_progress"})
            return
        if (STATE / f"{job_uid}.json").exists():
            LOCK.release()
            self.reply(409, {"error": "duplicate_job"})
            return
        job = {"job_uid": job_uid, "status": "running", "rolled_back": False}
        invalidate_check_cache()
        try:
            save(job)
            threading.Thread(target=upgrade, args=(job, target), daemon=True).start()
        except Exception:
            LOCK.release()
            raise
        self.reply(202, {"ok": True, "job_uid": job_uid})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not re.fullmatch(r"[0-9a-f]{64}", SECRET):
        raise SystemExit("A random 32-byte hexadecimal HMAC key is required")
    if not REPO.is_dir() or not STATE.is_dir() or BIND_HOST in ("0.0.0.0", "::"):
        raise SystemExit("Explicit private bind and existing deployment/state directories are required")
    if not GITHUB_SOURCE.fullmatch(SOURCE) and not (Path(SOURCE).is_absolute() and Path(SOURCE).is_dir()):
        raise SystemExit("Configure a local Git source path or an approved HTTPS GitHub repository")
    git("check-ref-format", "refs/heads/" + BRANCH)
    for path in STATE.glob("upd_*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("status") == "running":
            if job.get("to_commit") and git("rev-parse", "HEAD") == job["to_commit"] and health(job["to_commit"], 4):
                job.update(status="succeeded", log_summary="守护进程重启后确认新提交已就绪。")
            else:
                finish_failure(job, "更新过程中守护进程重启")
            save(job)
    port = int(os.environ.get("UPDATER_BIND_PORT", "9099"))
    logger.info("Listening on %s:%d", BIND_HOST, port)
    ThreadingHTTPServer((BIND_HOST, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
