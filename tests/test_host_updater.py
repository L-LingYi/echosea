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
    monkeypatch.setattr(updater, "health", lambda revision, seconds=45: revision == previous)
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
