#!/usr/bin/env python3
"""Host-side Companion System updater. Uses Linux, Git, Docker and Python's standard library.

Three-tier update policy:
- Tier 1 (Auto-allow): Business code & safe docker-compose.yml changes (companion-core env, mem_limit, logging, etc.).
- Tier 2 (Diff + Confirmation): Protected infrastructure files (Dockerfile, unsafe Compose changes, updater scripts)
  require WebUI diff review and a single-use HMAC confirmation token signed after TOTP/password verification.
- Controlled self-update: When ops/updater/host_updater.py changes, backup old script, verify syntax with
  `python3 -m py_compile`, replace after Core health check passes, and trigger a clean systemd restart.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.override.yml",
    ".dockerignore",
    ".env",
    ".gitattributes",
    "ops/updater/host_updater.py",
    "ops/updater/companion-updater.service",
    "ops/updater/install_updater.sh",
)
SAFE_COMPOSE_CORE_KEYS = frozenset({
    "environment",
    "mem_limit",
    "memswap_limit",
    "cpus",
    "cpu_shares",
    "logging",
    "stop_grace_period",
    "restart",
    "healthcheck",
    "labels",
})
MAX_DIFF_BYTES = 32768
LOCK = threading.Lock()
CHECK_CACHE_TTL_SECONDS = 30.0
_CHECK_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_SERVER_REF: ThreadingHTTPServer | None = None


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


def git_optional(*args: str) -> str:
    try:
        return git(*args)
    except RuntimeError:
        return ""


def compose(*args: str, revision: str = "", stdout=None) -> str:
    files = ["-f", "docker-compose.yml"]
    if (REPO / "docker-compose.override.yml").exists():
        files += ["-f", "docker-compose.override.yml"]
    return run("docker", "compose", "--env-file", ".env", *files, *args, revision=revision, stdout=stdout)


# ---------------------------------------------------------------------------
# Pure-Stdlib Compose YAML Subset Parser & Semantic Diff Policy (Tier 1 vs 2)
# ---------------------------------------------------------------------------

def _strip_yaml_comment(line: str) -> str:
    in_single = False
    in_double = False
    for idx, ch in enumerate(line):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _parse_yaml_scalar(raw: str) -> Any:
    val = raw.strip()
    if val == "":
        return ""
    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
        if len(val) >= 2:
            if val[0] == '"':
                try:
                    return json.loads(val)
                except ValueError:
                    return val[1:-1]
            return val[1:-1].replace("''", "'")
    if val.startswith(("&", "*", "<<")):
        raise ValueError("Unsupported YAML anchor/alias/merge")
    low = val.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
        return None
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        if not inner:
            return []
        items: list[Any] = []
        buf = ""
        in_s = in_d = False
        for ch in inner:
            if ch == "'" and not in_d:
                in_s = not in_s
            elif ch == '"' and not in_s:
                in_d = not in_d
            if ch == "," and not in_s and not in_d:
                items.append(_parse_yaml_scalar(buf))
                buf = ""
            else:
                buf += ch
        if buf.strip():
            items.append(_parse_yaml_scalar(buf))
        return items
    if val.startswith("{") and val.endswith("}"):
        if val == "{}":
            return {}
        try:
            return json.loads(val)
        except ValueError as exc:
            raise ValueError("Unsupported inline YAML mapping") from exc
    if re.fullmatch(r"-?[0-9]+", val):
        return int(val)
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", val):
        return float(val)
    return val


def _split_yaml_kv(text: str) -> tuple[str, str] | None:
    in_s = in_d = False
    brace_depth = 0
    for idx, ch in enumerate(text):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch == "{":
                brace_depth += 1
            elif ch == "}" and brace_depth > 0:
                brace_depth -= 1
            elif ch == ":" and brace_depth == 0:
                if idx + 1 == len(text) or text[idx + 1].isspace():
                    key = text[:idx].strip().strip("'\"")
                    rest = text[idx + 1:].strip()
                    return key, rest
    return None


def _parse_compose_yaml(text: str) -> dict[str, Any]:
    """Minimal, fail-closed YAML subset parser for docker-compose.yml."""
    raw_lines = text.splitlines()
    tokens: list[tuple[int, str]] = []
    idx = 0
    while idx < len(raw_lines):
        raw = raw_lines[idx]
        stripped = _strip_yaml_comment(raw)
        if not stripped.strip():
            idx += 1
            continue
        if stripped.lstrip().startswith("---"):
            raise ValueError("Multi-document YAML not allowed")
        if "\t" in raw[:len(raw) - len(raw.lstrip(" \t"))]:
            raise ValueError("Tab indentation not supported")
        indent = len(stripped) - len(stripped.lstrip(" "))
        content = stripped.lstrip(" ")
        tokens.append((indent, content))
        idx += 1

    if not tokens:
        return {}

    def parse_block(pos: int, min_indent: int) -> tuple[Any, int]:
        if pos >= len(tokens):
            return {}, pos
        base_indent, first = tokens[pos]
        if base_indent < min_indent:
            return {}, pos
        if first == "-" or first.startswith("- "):
            arr: list[Any] = []
            while pos < len(tokens):
                ind, line = tokens[pos]
                if ind < base_indent or (ind == base_indent and not (line == "-" or line.startswith("- "))):
                    break
                if ind > base_indent:
                    raise ValueError("Unexpected indentation inside list")
                rest = line[1:].strip() if line == "-" else line[2:].strip()
                pos += 1
                if not rest:
                    child, pos = parse_block(pos, base_indent + 1)
                    arr.append(child)
                else:
                    kv = _split_yaml_kv(rest)
                    if kv is not None and not rest.startswith(('"', "'")):
                        k, v_raw = kv
                        item_dict: dict[str, Any] = {}
                        if v_raw in ("|", ">", "|-", ">-"):
                            lines_blk: list[str] = []
                            blk_ind = None
                            while pos < len(tokens) and tokens[pos][0] > base_indent:
                                if blk_ind is None:
                                    blk_ind = tokens[pos][0]
                                lines_blk.append(tokens[pos][1])
                                pos += 1
                            item_dict[k] = "\n".join(lines_blk)
                        elif v_raw == "":
                            sub, pos = parse_block(pos, base_indent + 2)
                            item_dict[k] = sub
                        else:
                            item_dict[k] = _parse_yaml_scalar(v_raw)
                        if pos < len(tokens) and tokens[pos][0] > base_indent:
                            extra, pos = parse_block(pos, base_indent + 1)
                            if not isinstance(extra, dict):
                                raise ValueError("Expected dict continuation in list item")
                            item_dict.update(extra)
                        arr.append(item_dict)
                    else:
                        arr.append(_parse_yaml_scalar(rest))
            return arr, pos

        mapping: dict[str, Any] = {}
        while pos < len(tokens):
            ind, line = tokens[pos]
            if ind < base_indent:
                break
            if ind > base_indent:
                raise ValueError("Unexpected indentation inside mapping")
            if line == "-" or line.startswith("- "):
                break
            kv = _split_yaml_kv(line)
            if kv is None:
                raise ValueError(f"Invalid YAML mapping line: {line}")
            k, v_raw = kv
            if k == "<<":
                raise ValueError("YAML merge key not supported")
            pos += 1
            if v_raw in ("|", ">", "|-", ">-"):
                lines_blk = []
                while pos < len(tokens) and tokens[pos][0] > base_indent:
                    lines_blk.append(tokens[pos][1])
                    pos += 1
                mapping[k] = "\n".join(lines_blk)
            elif v_raw == "":
                if pos < len(tokens) and (
                    tokens[pos][0] > base_indent
                    or (tokens[pos][0] == base_indent and (tokens[pos][1] == "-" or tokens[pos][1].startswith("- ")))
                ):
                    sub, pos = parse_block(pos, base_indent)
                    mapping[k] = sub
                else:
                    mapping[k] = None
            else:
                mapping[k] = _parse_yaml_scalar(v_raw)
        return mapping, pos

    parsed, final_pos = parse_block(0, 0)
    if final_pos != len(tokens) or not isinstance(parsed, dict):
        raise ValueError("Top-level docker-compose.yml must be a mapping")
    return parsed


def _normalize_env_field(val: Any) -> Any:
    if isinstance(val, list):
        env_dict: dict[str, str | None] = {}
        for item in val:
            s = str(item)
            if "=" in s:
                k, v = s.split("=", 1)
                env_dict[k] = v
            else:
                env_dict[s] = None
        return env_dict
    return val


def evaluate_compose_change(old_text: str, new_text: str) -> tuple[bool, str]:
    """Return (is_safe_tier1, human_reason). Fails closed on any non-safe or unparseable change."""
    if not old_text.strip() or not new_text.strip():
        return False, "docker-compose.yml 新增或清空，需二次确认"
    try:
        old_doc = _parse_compose_yaml(old_text)
        new_doc = _parse_compose_yaml(new_text)
    except ValueError as exc:
        return False, f"docker-compose.yml 语法解析受限（{exc}），需二次确认"

    if set(old_doc.keys()) != set(new_doc.keys()):
        return False, "docker-compose.yml 顶层结构有变更，需二次确认"
    for top_key in old_doc:
        if top_key != "services" and old_doc[top_key] != new_doc[top_key]:
            return False, f"docker-compose.yml 顶层 `{top_key}` 定义有变更，需二次确认"

    old_svcs = old_doc.get("services")
    new_svcs = new_doc.get("services")
    if not isinstance(old_svcs, dict) or not isinstance(new_svcs, dict):
        return False, "docker-compose.yml services 结构异常，需二次确认"
    if set(old_svcs.keys()) != set(new_svcs.keys()):
        return False, "docker-compose.yml 新增或删除了容器服务，需二次确认"

    for svc_name in old_svcs:
        if svc_name != "companion-core" and old_svcs[svc_name] != new_svcs[svc_name]:
            return False, f"docker-compose.yml 修改了 `{svc_name}` 服务定义，需二次确认"

    old_core = old_svcs.get("companion-core")
    new_core = new_svcs.get("companion-core")
    if not isinstance(old_core, dict) or not isinstance(new_core, dict):
        return False, "docker-compose.yml 缺少 companion-core 定义，需二次确认"

    changed_keys: list[str] = []
    for k in sorted(set(old_core.keys()) | set(new_core.keys())):
        old_v = old_core.get(k)
        new_v = new_core.get(k)
        if k == "environment":
            old_v = _normalize_env_field(old_v)
            new_v = _normalize_env_field(new_v)
        if old_v != new_v:
            if k not in SAFE_COMPOSE_CORE_KEYS:
                return False, f"docker-compose.yml 修改了 companion-core 受保护字段 `{k}`，需二次确认"
            changed_keys.append(k)

    if not changed_keys:
        return True, "docker-compose.yml 仅格式或注释变化，安全放行"
    return True, f"docker-compose.yml 仅调整 companion-core 安全字段 ({', '.join(changed_keys)})，自动放行"


def _extract_target_version(target: str, commit: str) -> str:
    if RELEASE_TAG.fullmatch(target):
        return target.lstrip("v")
    cfg_src = git_optional("show", f"{commit}:backend/app/config.py")
    m = re.search(r'app_version\s*:\s*str\s*=\s*["\']([0-9]+\.[0-9]+\.[0-9]+[^"\']*)["\']', cfg_src)
    return m.group(1) if m else ""


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


def inspect_candidate(target: str = "") -> dict[str, Any]:
    """Fetch candidate commit and classify it as Tier 1 (auto) or Tier 2 (requires confirmation)."""
    previous, commit = candidate(target)
    target_version = _extract_target_version(target, commit)
    if previous == commit:
        return {
            "from_commit": previous,
            "to_commit": commit,
            "target_version": target_version,
            "update_available": False,
            "tier": 1,
            "requires_confirmation": False,
            "changed_files": [],
            "protected_files": [],
            "reasons": [],
            "safe_notes": [],
            "diff": "",
            "diff_sha256": "",
            "updater_self_changed": False,
        }

    changed_raw = git("diff", "--name-only", previous, commit)
    changed_files = [line.strip() for line in changed_raw.splitlines() if line.strip()]
    changed_set = set(changed_files)
    tier2_files: list[str] = []
    reasons: list[str] = []
    safe_notes: list[str] = []

    for path in PROTECTED_FILES:
        if path not in changed_set:
            continue
        if path == "docker-compose.yml":
            old_compose = git_optional("show", f"{previous}:docker-compose.yml")
            new_compose = git_optional("show", f"{commit}:docker-compose.yml")
            is_safe, reason = evaluate_compose_change(old_compose, new_compose)
            if is_safe:
                safe_notes.append(reason)
            else:
                tier2_files.append(path)
                reasons.append(reason)
        else:
            tier2_files.append(path)
            reasons.append(f"受保护文件变更：{path}")

    full_diff = ""
    diff_sha256 = ""
    diff_preview = ""
    if tier2_files:
        full_diff = git("diff", previous, commit, "--", *tier2_files)
        diff_sha256 = hashlib.sha256(full_diff.encode("utf-8")).hexdigest()
        if len(full_diff.encode("utf-8")) > MAX_DIFF_BYTES:
            diff_preview = full_diff.encode("utf-8")[:MAX_DIFF_BYTES].decode("utf-8", errors="ignore") + "\n... [diff truncated]"
        else:
            diff_preview = full_diff

    return {
        "from_commit": previous,
        "to_commit": commit,
        "target_version": target_version,
        "update_available": True,
        "tier": 2 if tier2_files else 1,
        "requires_confirmation": bool(tier2_files),
        "changed_files": changed_files,
        "protected_files": tier2_files,
        "reasons": reasons,
        "safe_notes": safe_notes,
        "diff": diff_preview,
        "diff_sha256": diff_sha256,
        "updater_self_changed": "ops/updater/host_updater.py" in changed_set,
    }


def _consumed_tokens_path() -> Path:
    return STATE / "consumed_tokens.json"


def verify_and_consume_confirm_token(token: str, to_commit: str, diff_sha256: str) -> str:
    """Verify HMAC-SHA256 one-time confirmation token bound to (to_commit, diff_sha256)."""
    if not token or "." not in token:
        raise ValueError("缺少有效的二次确认令牌")
    payload_b64, sig_hex = token.split(".", 1)
    if not re.fullmatch(r"[0-9a-f]{64}", sig_hex):
        raise ValueError("二次确认令牌签名格式无效")
    expected_sig = hmac.new(SECRET.encode(), b"confirm:" + payload_b64.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig_hex):
        raise ValueError("二次确认令牌签名无效")
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("二次确认令牌载荷损坏") from exc

    now = int(time.time())
    exp = int(claims.get("exp", 0))
    jti = str(claims.get("jti") or "")
    if not jti or exp < now or exp > now + 3600:
        raise ValueError("二次确认令牌已过期，请重新检查并验证")
    if claims.get("to_commit") != to_commit or claims.get("diff_sha256") != diff_sha256:
        raise ValueError("上游提交或变更 Diff 已发生变化，请重新确认")

    token_file = _consumed_tokens_path()
    consumed: dict[str, int] = {}
    if token_file.exists():
        try:
            raw = json.loads(token_file.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                consumed = {str(k): int(v) for k, v in raw.items() if int(v) >= now}
        except (OSError, ValueError):
            consumed = {}
    if jti in consumed:
        raise ValueError("二次确认令牌已被使用，请重新验证签发")
    consumed[jti] = exp
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE, delete=False, encoding="utf-8") as tmp:
        json.dump(consumed, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, token_file)
    return str(claims.get("sub") or "admin")


def stage_and_verify_updater_script(job_uid: str, commit: str) -> Path | None:
    """Extract candidate host_updater.py into STATE and validate syntax via `python3 -m py_compile`."""
    source = git_optional("show", f"{commit}:ops/updater/host_updater.py")
    if not source.strip():
        candidate_path = REPO / "ops" / "updater" / "host_updater.py"
        if not candidate_path.exists():
            return None
        source = candidate_path.read_text(encoding="utf-8")
    staged = STATE / f"{job_uid}.host_updater.py.staged"
    staged.write_text(source, encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "-B", "-m", "py_compile", str(staged)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    if res.returncode != 0:
        err = res.stderr.decode(errors="ignore").strip()
        raise ValueError(f"更新器脚本语法校验失败：{err or res.returncode}")
    return staged


def apply_updater_self_update(staged_path: Path, target_script: Path | None = None) -> tuple[bool, str]:
    """Backup current host_updater.py to STATE and atomically replace target_script."""
    target = (target_script or Path(__file__).resolve())
    backup_path = STATE / "host_updater.py.bak"
    if target.exists():
        shutil.copy2(target, backup_path)
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=target.parent, delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(staged_path.read_text(encoding="utf-8"))
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp.name, 0o644)
        os.replace(tmp.name, target)
        return True, "更新器脚本已校验并覆盖，即将由 systemd 平滑重启生效"
    except OSError as exc:
        logger.warning("Updater self-replace failed (%s); staged copy retained at %s", exc, staged_path)
        return False, f"更新器脚本已校验暂存，但覆盖受限（{exc}），请核对 systemd ReadWritePaths"


def trigger_self_restart() -> None:
    """Exit non-zero after a brief grace delay so systemd Restart=on-failure reloads the new script."""
    def _exit_soon() -> None:
        time.sleep(0.6)
        if _SERVER_REF is not None:
            try:
                _SERVER_REF.shutdown()
            except Exception:
                pass
        os._exit(75)

    threading.Thread(target=_exit_soon, daemon=True).start()


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
    keys = (
        "job_uid", "status", "rolled_back", "from_commit", "to_commit", "log_summary",
        "requires_confirmation", "protected_files", "diff_sha256", "reasons", "diff",
    )
    out: dict[str, Any] = {}
    for key in keys:
        if key in job or key in ("job_uid", "status", "rolled_back", "from_commit", "to_commit", "log_summary"):
            out[key] = job.get(key, "")
    return out


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


def upgrade(job: dict, target: str, confirm_token: str = "") -> None:
    self_restart_needed = False
    try:
        if git("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("部署仓库的受跟踪文件有未提交修改")
        inspection = inspect_candidate(target)
        previous = inspection["from_commit"]
        commit = inspection["to_commit"]
        job.update(from_commit=previous, to_commit=commit)
        if not health(previous, 6):
            raise ValueError("当前运行版本与部署仓库不一致，或 Core 未就绪")
        if previous == commit:
            job.update(status="succeeded", log_summary="已是最新提交，Core 未重建。")
            return
        if inspection["requires_confirmation"]:
            if not confirm_token:
                job.update(
                    status="confirmation_required",
                    rolled_back=False,
                    requires_confirmation=True,
                    protected_files=inspection["protected_files"],
                    diff=inspection["diff"],
                    diff_sha256=inspection["diff_sha256"],
                    reasons=inspection["reasons"],
                    log_summary="检测到受保护配置或更新器变更，请在 WebUI 查看 Diff 并完成二次确认。",
                )
                return
            verify_and_consume_confirm_token(confirm_token, commit, inspection["diff_sha256"])

        staged_updater: Path | None = None
        if inspection["updater_self_changed"]:
            staged_updater = stage_and_verify_updater_script(job["job_uid"], commit)

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

        notes = list(inspection.get("safe_notes") or [])
        if staged_updater is not None:
            replaced, self_msg = apply_updater_self_update(staged_updater)
            notes.append(self_msg)
            self_restart_needed = replaced
        summary = "新提交健康检查通过。"
        if notes:
            summary += " " + "；".join(notes)
        job.update(status="succeeded", log_summary=summary)
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
        if self_restart_needed:
            trigger_self_restart()


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
        if self.path not in ("/trigger", "/inspect"):
            self.reply(404, {"error": "not_found"})
            return
        length = self.headers.get("Content-Length", "")
        if not re.fullmatch(r"[0-9]{1,5}", length) or not 0 < int(length) <= 8192:
            self.reply(413, {"error": "invalid_body_length"})
            return
        body = self.rfile.read(int(length))
        if not self.authenticated(body):
            self.reply(403, {"error": "invalid_signature"})
            return

        if self.path == "/inspect":
            try:
                data = json.loads(body)
                target = data.get("target_ref", "") if isinstance(data, dict) else ""
                if not isinstance(target, str):
                    raise ValueError
                if target not in ("", BRANCH) and not COMMIT.fullmatch(target) and not RELEASE_TAG.fullmatch(target):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                self.reply(400, {"error": "invalid_target"})
                return
            if not LOCK.acquire(blocking=False):
                self.reply(409, {"error": "update_in_progress"})
                return
            try:
                info = inspect_candidate(target)
                self.reply(200, info)
            except ValueError as exc:
                self.reply(400, {"error": str(exc)})
            except (RuntimeError, subprocess.TimeoutExpired):
                self.reply(503, {"error": "source_unavailable"})
            finally:
                LOCK.release()
            return

        try:
            data = json.loads(body)
            job_uid = data["job_uid"]
            target = data.get("target_ref", "")
            confirm_token = data.get("confirm_token", "") or ""
            if not JOB_ID.fullmatch(job_uid) or not isinstance(target, str) or not isinstance(confirm_token, str):
                raise ValueError
            if len(confirm_token) > 2048:
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
            args = (job, target, confirm_token) if confirm_token else (job, target)
            threading.Thread(target=upgrade, args=args, daemon=True).start()
        except Exception:
            LOCK.release()
            raise
        self.reply(202, {"ok": True, "job_uid": job_uid})


def main():
    global _SERVER_REF
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
    server = ThreadingHTTPServer((BIND_HOST, port), Handler)
    _SERVER_REF = server
    server.serve_forever()


if __name__ == "__main__":
    main()
