"""Industry-Standard Online Update Service (v1.2.0):
- Privilege separation: companion-core NEVER touches docker.sock; communicates with a loopback host updater
  daemon using HMAC-SHA256 signed requests + 60s anti-replay timestamps.
- Supports BOTH current local repo mode and future GitHub public repo mode (`UPDATE_GITHUB_REPO=owner/repo`).
- Tracks UpdateJob audit trail and triggers AlertService notifications on failure or auto-rollback.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_service import AlertService
from app.models.db import UpdateJob


def sign_updater_payload(secret: str, timestamp: str, body_bytes: bytes) -> str:
    """Compute HMAC-SHA256 signature over '<timestamp>.<raw_body>'."""
    msg = timestamp.encode("utf-8") + b"." + body_bytes
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify_updater_signature(
    secret: str,
    timestamp: str | None,
    signature: str | None,
    body_bytes: bytes,
    max_skew_seconds: int = 60,
) -> bool:
    """Verify HMAC-SHA256 signature with strict timestamp anti-replay protection."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > max_skew_seconds:
        return False
    expected = sign_updater_payload(secret, timestamp, body_bytes)
    return hmac.compare_digest(expected, signature.strip())


def _normalize_ver(v: str) -> tuple[int, ...]:
    cleaned = v.strip().lstrip("vV").split("-")[0]
    parts = []
    for p in cleaned.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts or [0])


class UpdateService:
    """Manages version checks (GitHub Releases / Local Repo) and signed host updater triggers."""

    def __init__(
        self,
        current_version: str,
        current_commit: str = "",
        github_repo: str = "",
        git_branch: str = "main",
        updater_url: str = "",
        updater_secret: str = "",
        alert_service: AlertService | None = None,
        http_get: Callable[[str, dict[str, str]], Awaitable[dict[str, Any]]] | None = None,
        http_post: Callable[[str, dict[str, str], bytes], Awaitable[dict[str, Any]]] | None = None,
    ) -> None:
        self.current_version = current_version
        self.current_commit = current_commit
        self.github_repo = github_repo.strip().strip("/")
        self.git_branch = git_branch.strip() or "main"
        self.updater_url = updater_url.rstrip("/")
        self.updater_secret = updater_secret
        self.alert_service = alert_service
        self._http_get = http_get
        self._http_post = http_post

    @property
    def mode(self) -> str:
        return "github_public" if self.github_repo else "local_repo"

    async def check_for_updates(self) -> dict[str, Any]:
        """Check GitHub public Releases/Commits if configured; otherwise return local_repo readiness."""
        base = {
            "mode": self.mode,
            "current_version": self.current_version,
            "current_commit": self.current_commit or "unknown",
            "github_repo": self.github_repo,
            "git_branch": self.git_branch,
            "updater_configured": bool(self.updater_url and self.updater_secret),
            "update_available": False,
            "latest_version": self.current_version,
            "release_notes": "",
            "release_url": "",
        }
        if not self.github_repo:
            base["message"] = "当前处于本地仓库模式：可直接点击「触发更新」拉取本地/运维推送的最新提交；后期公开仓库后在 .env 配置 UPDATE_GITHUB_REPO 即可自动检测 GitHub 新版本。"
            return base

        headers = {"Accept": "application/vnd.github+json", "User-Agent": "CompanionSystem-Updater/1.2"}
        # 1. Try latest GitHub Release first
        rel_url = f"https://api.github.com/repos/{self.github_repo}/releases/latest"
        try:
            data = await self._get_json(rel_url, headers)
            tag = str(data.get("tag_name") or "").strip()
            if tag:
                latest_norm = _normalize_ver(tag)
                curr_norm = _normalize_ver(self.current_version)
                base["latest_version"] = tag
                base["update_available"] = latest_norm > curr_norm
                base["release_notes"] = str(data.get("body") or "")[:1500]
                base["release_url"] = str(data.get("html_url") or "")
                base["target_ref"] = tag
                return base
        except Exception:
            pass

        # 2. Fallback to latest commit on configured branch
        commit_url = f"https://api.github.com/repos/{self.github_repo}/commits/{self.git_branch}"
        try:
            cdata = await self._get_json(commit_url, headers)
            sha = str(cdata.get("sha") or "")[:12]
            msg = str((cdata.get("commit") or {}).get("message") or "").splitlines()[0]
            base["latest_version"] = sha or self.current_version
            base["update_available"] = bool(sha and self.current_commit and not sha.startswith(self.current_commit[:7]))
            base["release_notes"] = msg
            base["release_url"] = str(cdata.get("html_url") or "")
            base["target_ref"] = self.git_branch
            return base
        except Exception as exc:
            base["error"] = f"无法连接 GitHub 检查更新：{exc}"
            return base

    async def trigger_update(
        self,
        session: AsyncSession,
        triggered_by: str,
        target_ref: str | None = None,
    ) -> UpdateJob:
        """Create audited UpdateJob and send HMAC-SHA256 signed trigger to host updater daemon."""
        if not self.updater_url or not self.updater_secret:
            raise ValueError("未配置宿主机更新服务（请在 .env 设置 UPDATE_AGENT_URL 与 UPDATE_WEBHOOK_SECRET）")

        # Prevent concurrent in-flight update jobs
        active_stmt = select(UpdateJob).where(UpdateJob.status.in_(("pending", "running"))).limit(1)
        if (await session.execute(active_stmt)).scalar_one_or_none() is not None:
            raise ValueError("已有正在执行中的更新任务，请等待其完成后再试")

        ref = (target_ref or self.git_branch).strip()
        job = UpdateJob(
            job_uid=f"upd_{uuid.uuid4().hex[:12]}",
            triggered_by=triggered_by,
            mode=self.mode,
            target_ref=ref,
            from_version=self.current_version,
            from_commit=self.current_commit,
            status="pending",
            log_summary="已签发更新指令，等待宿主机更新守护进程接管...",
        )
        session.add(job)
        await session.flush()

        payload = {
            "job_uid": job.job_uid,
            "target_ref": ref,
            "github_repo": self.github_repo,
            "branch": self.git_branch,
        }
        body_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ts = str(int(time.time()))
        sig = sign_updater_payload(self.updater_secret, ts, body_bytes)
        headers = {
            "Content-Type": "application/json",
            "X-Updater-Timestamp": ts,
            "X-Updater-Signature": sig,
        }

        try:
            resp = await self._post_raw(f"{self.updater_url}/trigger", headers, body_bytes)
            job.status = "running"
            job.log_summary = str(resp.get("message") or "宿主机更新进程已受理，正在拉取代码并重建容器...")
        except Exception as exc:
            job.status = "failed"
            job.log_summary = f"调用宿主机更新守护进程失败：{exc}"
            job.finished_at = datetime.now(timezone.utc)
        await session.flush()
        return job

    async def handle_callback(self, session: AsyncSession, payload: dict[str, Any]) -> UpdateJob:
        """Record completion or auto-rollback result reported back by the host updater daemon."""
        job_uid = str(payload.get("job_uid") or "")
        stmt = select(UpdateJob).where(UpdateJob.job_uid == job_uid)
        job = (await session.execute(stmt)).scalar_one_or_none()
        if job is None:
            raise ValueError(f"UpdateJob not found: {job_uid}")

        status = str(payload.get("status") or "failed")
        job.status = status
        job.rolled_back = bool(payload.get("rolled_back") or status == "rolled_back")
        if payload.get("from_commit"):
            job.from_commit = str(payload["from_commit"])[:40]
        if payload.get("to_commit"):
            job.to_commit = str(payload["to_commit"])[:40]
        job.log_summary = str(payload.get("log_summary") or "")[:2000]
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

        if self.alert_service is not None and status in ("rolled_back", "failed"):
            await self.alert_service.notify(
                session=session,
                severity="critical",
                category="online_update",
                title=f"系统在线更新{'已自动回滚' if job.rolled_back else '失败'} ({job.job_uid})",
                detail=f"目标：{job.target_ref}，旧提交：{job.from_commit}，摘要：{job.log_summary}",
                bypass_cooldown=True,
            )
        return job

    async def _get_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        if self._http_get is not None:
            return await self._http_get(url, headers)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def _post_raw(self, url: str, headers: dict[str, str], body: bytes) -> dict[str, Any]:
        if self._http_post is not None:
            return await self._http_post(url, headers, body)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, content=body)
            resp.raise_for_status()
            return resp.json()
