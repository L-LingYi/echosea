"""Exercise the host-only paths missed by the Core's mocked updater tests."""
import importlib.util
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest


@pytest.fixture
def updater(tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / "ops" / "updater" / "host_updater.py"
    spec = importlib.util.spec_from_file_location("host_updater", source)
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "STATE", tmp_path)
    monkeypatch.setattr(agent, "SECRET", "a" * 64)
    return agent


def test_signed_request_is_consumed_once_inside_the_freshness_window(updater, monkeypatch):
    finished = threading.Event()
    jobs = []

    def complete(job, target):
        jobs.append(job["job_uid"])
        job["status"] = "succeeded"
        updater.save(job)
        updater.LOCK.release()
        finished.set()

    monkeypatch.setattr(updater, "upgrade", complete)
    with updater.ThreadingHTTPServer(("127.0.0.1", 0), updater.Handler) as server:
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            body = b'{"job_uid":"upd_1234567890ab","target_ref":"main"}'
            stamp = str(int(time.time()))
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/trigger", data=body,
                headers={"X-Updater-Timestamp": stamp, "X-Updater-Signature": updater.sign(stamp, body)},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                assert response.status == 202
            assert finished.wait(3)
            with pytest.raises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request, timeout=3)
            assert rejected.value.code == 409
            assert json.load(rejected.value)["error"] == "duplicate_job"
            assert jobs == ["upd_1234567890ab"]
        finally:
            server.shutdown()
            serving.join(3)


def test_failed_candidate_restores_old_image_without_rebuilding_it(updater, monkeypatch):
    previous, candidate = "a" * 40, "b" * 40
    active_image = ["sha256:old"]
    commands = []
    job = {"job_uid": "upd_1234567890ab", "status": "running"}

    def git(*args):
        commands.append(args)
        return ""

    def compose(*args, revision):
        commands.append((*args, revision))
        if args[0] == "up":
            active_image[0] = "sha256:old" if revision == previous else "sha256:new"

    monkeypatch.setattr(updater, "candidate", lambda target: (previous, candidate))
    monkeypatch.setattr(updater, "git", git)
    monkeypatch.setattr(updater, "compose", compose)
    monkeypatch.setattr(updater, "health", lambda revision, seconds=45, allow_empty_commit=False: revision == previous)
    monkeypatch.setattr(updater, "backup_database", lambda _: None)
    monkeypatch.setattr(updater, "active_container", lambda: (active_image[0], "core:latest"))
    monkeypatch.setattr(updater, "run", lambda *args: commands.append(args))
    monkeypatch.setattr(updater, "callback", lambda _: None)
    updater.LOCK.acquire()
    updater.upgrade(job, candidate)

    assert job["status"] == "rolled_back"
    assert job["rolled_back"] is True
    assert active_image[0] == "sha256:old"
    assert ("reset", "--keep", previous) in commands
    assert ("docker", "tag", f"companion-updater-rollback:{previous}", "core:latest") in commands
    assert [command for command in commands if command[0] == "build"] == [("build", "companion-core", candidate)]
    assert not updater.LOCK.locked()


def test_compose_semantic_diff_tier1_vs_tier2(updater):
    base = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")

    # Tier 1 safe: adding env var + tweaking mem_limit & logging on companion-core
    safe_env = base.replace(
        "TZ: ${TZ:-Asia/Shanghai}",
        "TZ: ${TZ:-Asia/Shanghai}\n      CUSTOM_FLAG: \"1\"",
        1,
    ).replace("mem_limit: 448m", "mem_limit: 512m", 1)
    ok, reason = updater.evaluate_compose_change(base, safe_env)
    assert ok is True
    assert "companion-core" in reason

    # Tier 2 unsafe: changing volumes on companion-core
    unsafe_vol = base.replace("sticker_data:/app/data", "/:/host_root")
    ok, reason = updater.evaluate_compose_change(base, unsafe_vol)
    assert ok is False
    assert "volumes" in reason

    # Tier 2 unsafe: changing postgres service
    unsafe_pg = base.replace("shared_buffers=128MB", "shared_buffers=256MB")
    ok, reason = updater.evaluate_compose_change(base, unsafe_pg)
    assert ok is False
    assert "postgres" in reason

    # Tier 2 unsafe: YAML anchors / unsupported syntax fails closed
    ok, _ = updater.evaluate_compose_change(base, "services:\n  companion-core: &x\n    image: foo\n")
    assert ok is False


def test_tier2_confirmation_token_single_use_and_inspect(updater, monkeypatch, tmp_path):
    import base64
    import hashlib
    import hmac

    previous, commit = "a" * 40, "b" * 40
    diff_text = "diff --git a/Dockerfile b/Dockerfile\n+ENV TEST=1\n"
    diff_sha = hashlib.sha256(diff_text.encode("utf-8")).hexdigest()

    def fake_git(*args):
        if args == ("status", "--porcelain", "--untracked-files=no"):
            return ""
        if args[:3] == ("diff", "--name-only", previous):
            return "Dockerfile\nbackend/app/main.py"
        if args[:3] == ("diff", previous, commit):
            return diff_text
        return ""

    monkeypatch.setattr(updater, "candidate", lambda target="": (previous, commit))
    monkeypatch.setattr(updater, "git", fake_git)
    monkeypatch.setattr(updater, "git_optional", lambda *args: 'app_version: str = "1.8.2"' if "config.py" in str(args) else "")
    monkeypatch.setattr(updater, "health", lambda revision, seconds=45, allow_empty_commit=False: True)

    # 1. /inspect endpoint returns Tier 2 diff & sha256
    with updater.ThreadingHTTPServer(("127.0.0.1", 0), updater.Handler) as server:
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        try:
            body = json.dumps({"target_ref": commit}).encode("utf-8")
            stamp = str(int(time.time()))
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/inspect",
                data=body,
                headers={"X-Updater-Timestamp": stamp, "X-Updater-Signature": updater.sign(stamp, body)},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            assert info["requires_confirmation"] is True
            assert info["protected_files"] == ["Dockerfile"]
            assert info["diff_sha256"] == diff_sha
            assert info["target_version"] == "1.8.2"
        finally:
            server.shutdown()
            serving.join(3)

    # 2. Tier 2 without confirm_token -> confirmation_required (no DB backup / no build)
    called_backup = []
    monkeypatch.setattr(updater, "backup_database", lambda uid: called_backup.append(uid))
    monkeypatch.setattr(updater, "callback", lambda _: None)
    job1 = {"job_uid": "upd_111111111111", "status": "running", "rolled_back": False}
    updater.LOCK.acquire()
    updater.upgrade(job1, commit)
    assert job1["status"] == "confirmation_required"
    assert job1["requires_confirmation"] is True
    assert job1["diff_sha256"] == diff_sha
    assert called_backup == []

    # 3. Tier 2 WITH valid confirm_token -> executes & consumes jti (replay rejected)
    claims = {
        "jti": "nonce_once_1",
        "sub": "admin",
        "target_ref": commit,
        "to_commit": commit,
        "diff_sha256": diff_sha,
        "exp": int(time.time()) + 300,
    }
    b64 = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).decode()
    sig = hmac.new(updater.SECRET.encode(), b"confirm:" + b64.encode(), hashlib.sha256).hexdigest()
    token = f"{b64}.{sig}"

    monkeypatch.setattr(updater, "active_container", lambda: ("sha256:old", "core:latest"))
    monkeypatch.setattr(updater, "run", lambda *args: "")
    monkeypatch.setattr(updater, "compose", lambda *args, revision="": "")
    job2 = {"job_uid": "upd_222222222222", "status": "running", "rolled_back": False}
    updater.LOCK.acquire()
    updater.upgrade(job2, commit, token)
    assert job2["status"] == "succeeded"
    assert called_backup == ["upd_222222222222"]

    # Replay same one-time token -> rejected
    job3 = {"job_uid": "upd_333333333333", "status": "running", "rolled_back": False}
    updater.LOCK.acquire()
    updater.upgrade(job3, commit, token)
    assert job3["status"] == "failed"
    assert "已被使用" in job3["log_summary"]


def test_updater_self_update_syntax_check_backup_and_restart(updater, monkeypatch, tmp_path):
    target_script = tmp_path / "host_updater.py"
    target_script.write_text("# old updater\nVERSION = 1\n", encoding="utf-8")

    # 1. Invalid syntax aborts before build
    staged_bad = tmp_path / "bad.py"
    with pytest.raises(ValueError, match="语法校验失败"):
        monkeypatch.setattr(updater, "git_optional", lambda *args: "def broken(:\n")
        updater.stage_and_verify_updater_script("upd_bad", "b" * 40)
    assert not staged_bad.exists()

    # 2. Valid syntax backs up old script, replaces target, and triggers restart hook
    restarted = []
    monkeypatch.setattr(updater, "git_optional", lambda *args: "# new updater\nVERSION = 2\n")
    staged = updater.stage_and_verify_updater_script("upd_ok", "b" * 40)
    assert staged is not None and staged.exists()
    replaced, msg = updater.apply_updater_self_update(staged, target_script=target_script)
    assert replaced is True
    assert "平滑重启" in msg
    assert (tmp_path / "host_updater.py.bak").read_text(encoding="utf-8") == "# old updater\nVERSION = 1\n"
    assert "VERSION = 2" in target_script.read_text(encoding="utf-8")

    monkeypatch.setattr(updater, "trigger_self_restart", lambda: restarted.append(True))
    updater.trigger_self_restart()
    assert restarted == [True]


def test_health_allows_empty_commit_only_on_preflight(updater, monkeypatch):
    class _FakeResp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode("utf-8")

        def read(self, _n=-1):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    # Manually restarted container without APP_GIT_COMMIT -> /healthz omits "commit"
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResp({"status": "ok", "version": "1.9.2"}))
    assert updater.health("a" * 40, seconds=1, allow_empty_commit=True) is True
    assert updater.health("a" * 40, seconds=1, allow_empty_commit=False) is False

    # Mismatched non-empty commit is still rejected even when allow_empty_commit=True
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: _FakeResp({"status": "ok", "commit": "c" * 40}))
    assert updater.health("a" * 40, seconds=1, allow_empty_commit=True) is False
    assert updater.health("c" * 40, seconds=1, allow_empty_commit=False) is True
