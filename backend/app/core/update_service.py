"""Industry-Standard Online Update Service (v1.9.0):
- Privilege separation: companion-core NEVER touches docker.sock; communicates with a loopback host updater
  daemon using HMAC-SHA256 signed requests + 60s anti-replay timestamps.
- Supports BOTH current local repo mode and GitHub public repo mode (`UPDATE_GITHUB_REPO=owner/repo`).
- Three-tier update workflow:
  * Tier 1: Auto-allow business code and safe Compose changes (companion-core env, mem_limit, logging, etc.).
  * Tier 2: Protected file / unsafe Compose / updater self-changes return exact git diff and require a
    single-use HMAC confirmation token issued after TOTP (or admin password) verification.
  * Tier 3: Optional scheduled unattended auto-update at off-peak hours (default 04:00) strictly limited to
    Tier 1 + same-minor patch releases; Tier 2 or cross-version updates send an alert notification instead.
- Tracks UpdateJob audit trail and triggers AlertService notifications on failure or auto-rollback.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_service import AlertService
from app.models.db import SystemConfig, UpdateJob

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("pending", "running")
FINAL_STATUSES = ("succeeded", "rolled_back", "failed", "confirmation_required")
AUTO_UPDATE_SCOPE_TYPE = "global"
AUTO_UPDATE_SCOPE_ID = "auto_update"


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
    """Verify the signature and its 60-second freshness window."""
    if not secret or not timestamp or not signature:
        return False
    if not re.fullmatch(r"[0-9]{1,12}", timestamp) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return False
    if abs(int(time.time()) - int(timestamp)) > max_skew_seconds:
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


def is_patch_upgrade(current_version: str, target_version: str) -> bool:
    """Return True only when target_version is a semver in the same major.minor line (>= current)."""
    if not current_version or not target_version:
        return False
    cleaned = target_version.strip()
    if re.fullmatch(r"[0-9a-f]{7,40}", cleaned):
        return False
    if not re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+].*)?", cleaned):
        return False
    curr = _normalize_ver(current_version)
    targ = _normalize_ver(cleaned)
    if len(curr) < 2 or len(targ) < 2:
        return False
    return targ[:2] == curr[:2] and targ >= curr


class UpdateService:
    """Manages version checks (GitHub Releases / Local Repo), 3-tier inspection, and signed host updater triggers."""

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
        self._latest_confirmations: dict[str, dict[str, Any]] = {}

    @property
    def mode(self) -> str:
        return "github_public" if self.github_repo else "local_repo"

    def _validate_target_ref(self, target_ref: str | None) -> str:
        ref = self.git_branch if target_ref is None else target_ref
        if not isinstance(ref, str) or (
            ref != self.git_branch and not re.fullmatch(r"(?:[0-9a-f]{40}|v[0-9]+\.[0-9]+\.[0-9]+)", ref)
        ):
            raise ValueError("请先检查可用的更新版本")
        return ref

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
            if not base["updater_configured"]:
                base["message"] = "宿主机更新服务尚未配置。"
                return base
            try:
                status = await self._get_host("/check")
                base.update(status)
                target = str(status.get("target_ref") or "")
                base["latest_version"] = target[:12] or self.current_version
                base["message"] = "本地更新源已同步。" if not status["update_available"] else "本地更新源有新提交。"
            except (httpx.HTTPError, ValueError, KeyError):
                base["error"] = "暂时无法检查本地更新源。"
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
        except Exception as exc:
            logger.debug("Release lookup failed for %s, falling back to branch HEAD [%s]: %s", rel_url, type(exc).__name__, exc)

        # 2. Fallback to latest commit on configured branch
        commit_url = f"https://api.github.com/repos/{self.github_repo}/commits/{self.git_branch}"
        try:
            cdata = await self._get_json(commit_url, headers)
            full_sha = str(cdata.get("sha") or "")
            sha = full_sha[:12]
            msg, _, _ = str((cdata.get("commit") or {}).get("message") or "").partition("\n")
            effective_commit = self.current_commit
            if not effective_commit and base["updater_configured"]:
                try:
                    host_status = await self._get_host("/check")
                    effective_commit = str(host_status.get("current_commit") or "")
                    if effective_commit:
                        base["current_commit"] = effective_commit
                except Exception as exc:
                    logger.debug("Host /check fallback commit lookup failed [%s]: %s", type(exc).__name__, exc)
            base["latest_version"] = sha or self.current_version
            base["update_available"] = bool(sha and effective_commit and not sha.startswith(effective_commit[:7]))
            base["release_notes"] = msg
            base["release_url"] = str(cdata.get("html_url") or "")
            base["target_ref"] = full_sha
            return base
        except Exception as exc:
            base["error"] = f"无法连接 GitHub 检查更新：{exc}"
            return base

    async def inspect_update(self, target_ref: str | None = None) -> dict[str, Any]:
        """Ask host updater daemon to classify candidate update as Tier 1 (auto) or Tier 2 (diff confirmation)."""
        if not self.updater_url or not self.updater_secret:
            raise ValueError("未配置宿主机更新服务（请在 .env 设置 UPDATE_AGENT_URL 与 UPDATE_WEBHOOK_SECRET）")
        ref = self._validate_target_ref(target_ref)
        body_bytes = json.dumps({"target_ref": ref}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return await self._post_raw(f"{self.updater_url}/inspect", self._host_headers(body_bytes), body_bytes)

    def issue_confirmation_token(
        self,
        admin_username: str,
        target_ref: str,
        to_commit: str,
        diff_sha256: str,
        ttl_seconds: int = 300,
    ) -> dict[str, Any]:
        """Issue a short-lived, single-use HMAC confirmation token bound to exact (to_commit, diff_sha256)."""
        if not self.updater_secret:
            raise ValueError("未配置宿主机更新密钥")
        if not re.fullmatch(r"[0-9a-f]{40}", to_commit or ""):
            raise ValueError("无效的目标提交哈希")
        if not re.fullmatch(r"[0-9a-f]{64}", diff_sha256 or ""):
            raise ValueError("无效的 Diff 摘要")
        claims = {
            "jti": uuid.uuid4().hex[:16],
            "sub": admin_username,
            "target_ref": target_ref,
            "to_commit": to_commit,
            "diff_sha256": diff_sha256,
            "exp": int(time.time()) + ttl_seconds,
        }
        payload_b64 = base64.urlsafe_b64encode(
            json.dumps(claims, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        sig = hmac.new(
            self.updater_secret.encode("utf-8"),
            b"confirm:" + payload_b64.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return {"confirm_token": f"{payload_b64}.{sig}", "expires_in": ttl_seconds}

    async def trigger_update(
        self,
        session: AsyncSession,
        triggered_by: str,
        target_ref: str | None = None,
        confirm_token: str | None = None,
    ) -> UpdateJob:
        """Create audited UpdateJob and send HMAC-SHA256 signed trigger to host updater daemon."""
        if not self.updater_url or not self.updater_secret:
            raise ValueError("未配置宿主机更新服务（请在 .env 设置 UPDATE_AGENT_URL 与 UPDATE_WEBHOOK_SECRET）")

        active = await self.reconcile_active_job(session)
        if active is not None and active.status in ACTIVE_STATUSES:
            raise ValueError("已有正在执行中的更新任务，请等待其完成后再试")

        ref = self._validate_target_ref(target_ref)
        job = UpdateJob(
            job_uid=f"upd_{uuid.uuid4().hex[:12]}",
            triggered_by=triggered_by,
            mode=self.mode,
            target_ref=ref,
            from_version=self.current_version,
            from_commit=self.current_commit,
            status="running",
            log_summary="等待宿主机执行更新。",
        )
        session.add(job)
        try:
            # A fast no-op or failed update can call back before the trigger response returns.
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise ValueError("已有正在执行中的更新任务，请等待其完成后再试") from exc

        payload: dict[str, Any] = {"job_uid": job.job_uid, "target_ref": ref}
        if confirm_token:
            payload["confirm_token"] = confirm_token
        body_bytes = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            await self._post_raw(f"{self.updater_url}/trigger", self._host_headers(body_bytes), body_bytes)
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                await self.handle_callback(session, {"job_uid": job.job_uid, "status": "failed",
                                                     "log_summary": "宿主机拒绝了更新请求。"})
        except (httpx.RequestError, ValueError):
            # The host may have received the request. Reconcile using its durable job record.
            pass
        await session.refresh(job)
        return job

    async def reconcile_active_job(self, session: AsyncSession) -> UpdateJob | None:
        stmt = select(UpdateJob).where(UpdateJob.status.in_(ACTIVE_STATUSES)).limit(1)
        active = (await session.execute(stmt)).scalar_one_or_none()
        if active is None or not self.updater_url or not self.updater_secret:
            return active
        try:
            result = await self._get_host(f"/result/{active.job_uid}")
        except httpx.HTTPStatusError as exc:
            created = active.created_at.replace(tzinfo=timezone.utc)
            if exc.response.status_code == 404 and (datetime.now(timezone.utc) - created).total_seconds() > 60:
                return await self.handle_callback(session, {"job_uid": active.job_uid, "status": "failed",
                                                            "log_summary": "宿主机没有受理该任务。"})
        except (httpx.RequestError, ValueError):
            pass
        else:
            if result.get("job_uid") == active.job_uid and result.get("status") in FINAL_STATUSES:
                return await self.handle_callback(session, result)
        return active

    async def handle_callback(self, session: AsyncSession, payload: dict[str, Any]) -> UpdateJob:
        """Record completion or auto-rollback result reported back by the host updater daemon."""
        job_uid = str(payload.get("job_uid") or "")
        stmt = select(UpdateJob).where(UpdateJob.job_uid == job_uid).with_for_update().execution_options(populate_existing=True)
        job = (await session.execute(stmt)).scalar_one_or_none()
        if job is None:
            raise ValueError(f"UpdateJob not found: {job_uid}")

        if job.status in FINAL_STATUSES:
            return job
        status = payload.get("status")
        if status not in FINAL_STATUSES:
            raise ValueError("无效的任务结果")
        job.status = status
        job.rolled_back = bool(payload.get("rolled_back") or status == "rolled_back")
        if payload.get("from_commit"):
            job.from_commit = str(payload["from_commit"])[:40]
        if payload.get("to_commit"):
            job.to_commit = str(payload["to_commit"])[:40]
        job.log_summary = str(payload.get("log_summary") or "")[:2000]
        job.finished_at = datetime.now(timezone.utc)
        if status == "confirmation_required":
            self._latest_confirmations[job.job_uid] = {
                "job_uid": job.job_uid,
                "target_ref": job.target_ref,
                "from_commit": job.from_commit,
                "to_commit": job.to_commit,
                "requires_confirmation": True,
                "protected_files": list(payload.get("protected_files") or []),
                "reasons": list(payload.get("reasons") or []),
                "diff": str(payload.get("diff") or ""),
                "diff_sha256": str(payload.get("diff_sha256") or ""),
            }
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
        elif self.alert_service is not None and status == "succeeded" and job.triggered_by == "auto_scheduler":
            await self.alert_service.notify(
                session=session,
                severity="info",
                category="online_update",
                title=f"系统凌晨自动补丁更新成功 ({job.job_uid})",
                detail=f"目标：{job.target_ref}，新提交：{job.to_commit or '-'}，摘要：{job.log_summary}",
                bypass_cooldown=True,
            )
        return job

    def get_confirmation_detail(self, job_uid: str) -> dict[str, Any] | None:
        return self._latest_confirmations.get(job_uid)

    async def _load_auto_config_row(self, session: AsyncSession) -> tuple[SystemConfig, dict[str, Any]]:
        stmt = select(SystemConfig).where(
            SystemConfig.scope_type == AUTO_UPDATE_SCOPE_TYPE,
            SystemConfig.scope_id == AUTO_UPDATE_SCOPE_ID,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        defaults: dict[str, Any] = {
            "auto_update_enabled": False,
            "auto_update_hour": 4,
            "auto_update_minute": 0,
            "last_checked_date": None,
            "last_notified_ref": "",
            "last_auto_update_at": None,
            "last_auto_update_status": "",
            "last_auto_update_message": "",
        }
        if row is None:
            row = SystemConfig(
                scope_type=AUTO_UPDATE_SCOPE_TYPE,
                scope_id=AUTO_UPDATE_SCOPE_ID,
                config_json=defaults,
            )
            session.add(row)
            await session.flush()
            return row, dict(defaults)
        merged = {**defaults, **(row.config_json or {})}
        return row, merged

    async def get_auto_update_config(self, session: AsyncSession) -> dict[str, Any]:
        _, cfg = await self._load_auto_config_row(session)
        return cfg

    async def save_auto_update_config(self, session: AsyncSession, payload: dict[str, Any]) -> dict[str, Any]:
        row, cfg = await self._load_auto_config_row(session)
        if "auto_update_enabled" in payload:
            cfg["auto_update_enabled"] = bool(payload["auto_update_enabled"])
        if "auto_update_hour" in payload:
            hour = int(payload["auto_update_hour"])
            if not 0 <= hour <= 23:
                raise ValueError("auto_update_hour 必须在 0 到 23 之间")
            cfg["auto_update_hour"] = hour
        if "auto_update_minute" in payload:
            minute = int(payload["auto_update_minute"])
            if not 0 <= minute <= 59:
                raise ValueError("auto_update_minute 必须在 0 到 59 之间")
            cfg["auto_update_minute"] = minute
        row.config_json = cfg
        await session.flush()
        return cfg

    async def run_scheduled_auto_update(
        self,
        session: AsyncSession,
        local_dt: datetime,
    ) -> dict[str, Any] | None:
        """Tier 3: At off-peak hour, auto-update only Tier 1 same-minor patch releases; alert otherwise."""
        if not self.updater_url or not self.updater_secret:
            return None
        row, cfg = await self._load_auto_config_row(session)
        if not cfg.get("auto_update_enabled"):
            return None

        today_str = local_dt.date().isoformat()
        target_hhmm = (int(cfg.get("auto_update_hour", 4)), int(cfg.get("auto_update_minute", 0)))
        if (local_dt.hour, local_dt.minute) < target_hhmm or cfg.get("last_checked_date") == today_str:
            return None

        active = await self.reconcile_active_job(session)
        if active is not None and active.status in ACTIVE_STATUSES:
            return None

        cfg["last_checked_date"] = today_str
        check_res = await self.check_for_updates()
        if check_res.get("error") or not check_res.get("update_available"):
            cfg["last_auto_update_status"] = "up_to_date" if not check_res.get("error") else "check_error"
            cfg["last_auto_update_message"] = str(check_res.get("error") or "当前已是最新版本")
            row.config_json = cfg
            await session.flush()
            return {"action": "noop", "reason": cfg["last_auto_update_message"]}

        target_ref = str(check_res.get("target_ref") or self.git_branch)
        try:
            inspect_res = await self.inspect_update(target_ref)
        except Exception as exc:
            cfg["last_auto_update_status"] = "inspect_error"
            cfg["last_auto_update_message"] = f"宿主机更新预检失败：{exc}"
            row.config_json = cfg
            await session.flush()
            return {"action": "error", "reason": cfg["last_auto_update_message"]}

        if not inspect_res.get("update_available", True):
            cfg["last_auto_update_status"] = "up_to_date"
            cfg["last_auto_update_message"] = "宿主机已是最新提交"
            row.config_json = cfg
            await session.flush()
            return {"action": "noop", "reason": cfg["last_auto_update_message"]}

        candidate_ver = str(inspect_res.get("target_version") or check_res.get("latest_version") or "")
        tier1_safe = not bool(inspect_res.get("requires_confirmation"))
        patch_ok = is_patch_upgrade(self.current_version, candidate_ver)

        if tier1_safe and patch_ok:
            job = await self.trigger_update(session, triggered_by="auto_scheduler", target_ref=target_ref)
            cfg["last_auto_update_at"] = local_dt.isoformat()
            cfg["last_auto_update_status"] = job.status
            cfg["last_auto_update_message"] = f"已触发第一级自动补丁更新（{candidate_ver or target_ref[:12]}）"
            row.config_json = cfg
            await session.flush()
            return {"action": "triggered", "job_uid": job.job_uid}

        reasons: list[str] = []
        if not tier1_safe:
            files = ", ".join(inspect_res.get("protected_files") or []) or "受保护配置"
            reasons.append(f"涉及第二级受保护配置变更（{files}）")
        if not patch_ok:
            reasons.append(f"涉及跨版本升级（当前 v{self.current_version} → 目标 {candidate_ver or target_ref[:12]}）")
        reason_text = "；".join(reasons)

        commit_key = str(inspect_res.get("to_commit") or target_ref)
        if cfg.get("last_notified_ref") != commit_key and self.alert_service is not None:
            await self.alert_service.notify(
                session=session,
                severity="warning",
                category="online_update",
                title="发现需人工确认的系统更新",
                detail=f"{reason_text}。未自动安装，请登录管理后台「运维、插件与更新」查看 Diff 并二次确认。",
                bypass_cooldown=True,
            )
            cfg["last_notified_ref"] = commit_key

        cfg["last_auto_update_status"] = "pending_confirmation"
        cfg["last_auto_update_message"] = reason_text
        row.config_json = cfg
        await session.flush()
        return {"action": "notified", "reason": reason_text}

    def _host_headers(self, body: bytes) -> dict[str, str]:
        stamp = str(int(time.time()))
        return {
            "Content-Type": "application/json",
            "X-Updater-Timestamp": stamp,
            "X-Updater-Signature": sign_updater_payload(self.updater_secret, stamp, body),
        }

    async def _get_host(self, path: str) -> dict[str, Any]:
        headers = self._host_headers(path.encode())
        if self._http_get is not None:
            return await self._http_get(f"{self.updater_url}{path}", headers)
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            response = await client.get(f"{self.updater_url}{path}", headers=headers)
        if not verify_updater_signature(self.updater_secret, response.headers.get("X-Updater-Timestamp"),
                                        response.headers.get("X-Updater-Signature"), response.content):
            raise ValueError("Invalid host response signature")
        response.raise_for_status()
        return response.json()

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
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await client.post(url, headers=headers, content=body)
            if not verify_updater_signature(self.updater_secret, resp.headers.get("X-Updater-Timestamp"),
                                            resp.headers.get("X-Updater-Signature"), resp.content):
                raise ValueError("Invalid host response signature")
            resp.raise_for_status()
            return resp.json()
