import json
import subprocess

from service import Service
import service as service_module


class FakeCLF:
    pass


class FakeRepository:
    pass


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def make_service(tmp_path, monkeypatch, started=True):
    clock = FakeClock()
    monkeypatch.setattr(service_module.time, "monotonic", clock)
    access_log = tmp_path / "access.log.jsonl"
    service = Service(
        FakeCLF(),
        FakeRepository(),
        on_unlock_shell_command="python opendeur.py",
        access_log_path=str(access_log),
    )
    runs = []

    def fake_run(command, reason, on_exit=None, timeout_seconds=None):
        runs.append(
            {
                "command": command,
                "reason": reason,
                "on_exit": on_exit,
                "timeout_seconds": timeout_seconds,
            }
        )
        return started

    monkeypatch.setattr(service, "_run_shell_command", fake_run)
    return service, clock, runs, access_log


def read_log(access_log):
    return [json.loads(line) for line in access_log.read_text().splitlines()]


def test_unlock_run_is_started_with_kill_timeout(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")

    assert runs[0]["timeout_seconds"] == Service.UNLOCK_MAX_RUN_SECONDS


def test_second_unlock_is_skipped_while_first_still_runs(tmp_path, monkeypatch):
    service, clock, runs, access_log = make_service(tmp_path, monkeypatch)

    assert service.run_unlock_shell_command("known-nfc", actor="card", source="nfc") is True
    clock.now += 1
    assert service.run_unlock_shell_command("homekey-authenticated", actor="phone", source="homekey") is False

    assert [run["reason"] for run in runs] == ["known-nfc"]
    entries = read_log(access_log)
    assert entries[1]["event_type"] == "door_unlock_skipped"
    assert entries[1]["details"]["skipped"] == "already-running"
    assert entries[1]["details"]["command_started"] is False


def test_repeat_window_starts_when_the_run_ends(tmp_path, monkeypatch):
    service, clock, runs, access_log = make_service(tmp_path, monkeypatch)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    clock.now += 6.5  # servo run time, longer than the repeat window
    runs[0]["on_exit"]()
    clock.now += Service.UNLOCK_REPEAT_SUPPRESS_SECONDS - 1

    assert service.run_unlock_shell_command("known-nfc", actor="card", source="nfc") is False
    assert read_log(access_log)[1]["details"]["skipped"] == "repeat-trigger"
    assert len(runs) == 1


def test_same_actor_can_unlock_again_after_window(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    clock.now += 6.5
    runs[0]["on_exit"]()
    clock.now += Service.UNLOCK_REPEAT_SUPPRESS_SECONDS

    assert service.run_unlock_shell_command("known-nfc", actor="card", source="nfc") is True
    assert len(runs) == 2


def test_other_actor_can_unlock_right_after_previous_run_ended(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    runs[0]["on_exit"]()
    clock.now += 1

    assert service.run_unlock_shell_command("home-unlock", actor="home-app", source="homekit") is True
    assert len(runs) == 2


def test_failed_start_releases_slot_without_blocking_a_retry(tmp_path, monkeypatch):
    service, clock, runs, access_log = make_service(tmp_path, monkeypatch, started=False)

    assert service.run_unlock_shell_command("known-nfc", actor="card", source="nfc") is False
    assert service.is_unlock_command_running() is False
    entries = read_log(access_log)
    assert entries[0]["event_type"] == "door_unlocked"
    assert "skipped" not in entries[0]["details"]

    clock.now += 1
    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    assert len(runs) == 2


def test_slot_expires_if_exit_is_never_reported(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    clock.now += Service.UNLOCK_SLOT_EXPIRY_SECONDS - 1
    assert service.is_unlock_command_running() is True

    clock.now += 1
    assert service.is_unlock_command_running() is False
    assert service.run_unlock_shell_command("home-unlock", actor="home-app", source="homekit") is True


def test_stale_exit_does_not_release_newer_run_or_relock(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)
    completed = []
    service.on_unlock_shell_command_complete = lambda: completed.append(True)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    clock.now += Service.UNLOCK_SLOT_EXPIRY_SECONDS
    service.run_unlock_shell_command("home-unlock", actor="home-app", source="homekit")

    runs[0]["on_exit"]()  # the old, expired run finally exits

    assert completed == []
    assert service.is_unlock_command_running() is True
    clock.now += 1
    assert service.run_unlock_shell_command("known-nfc", actor="other", source="nfc") is False

    runs[1]["on_exit"]()
    assert completed == [True]
    assert service.is_unlock_command_running() is False


def test_unlock_exit_releases_slot_and_notifies_for_any_reason(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)
    completed = []
    service.on_unlock_shell_command_complete = lambda: completed.append(True)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    assert service.is_unlock_command_running() is True

    runs[0]["on_exit"]()

    assert service.is_unlock_command_running() is False
    assert completed == [True]


def test_completion_callback_runs_while_slot_is_held(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)
    lock_held = []
    service.on_unlock_shell_command_complete = lambda: lock_held.append(
        service._unlock_slot_lock.locked()
    )

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    runs[0]["on_exit"]()

    assert lock_held == [True]


def test_call_if_unlock_running_and_idle(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)
    calls = []

    assert service.call_if_unlock_running(lambda: calls.append("running")) is False
    assert service.call_if_unlock_idle(lambda: calls.append("idle")) is True

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")

    assert service.call_if_unlock_running(lambda: calls.append("running")) is True
    assert service.call_if_unlock_idle(lambda: calls.append("idle")) is False
    assert calls == ["idle", "running"]


def test_repeat_window_entries_are_pruned(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)

    for index in range(3):
        service.run_unlock_shell_command("known-nfc", actor=f"card-{index}", source="nfc")
        runs[-1]["on_exit"]()
        clock.now += Service.UNLOCK_REPEAT_SUPPRESS_SECONDS
    service.run_unlock_shell_command("known-nfc", actor="card-3", source="nfc")

    assert list(service._last_unlock_exit_at_by_actor) == []


def test_wait_for_shell_command_exit_runs_exit_handler_even_when_wait_fails():
    service = Service(FakeCLF(), FakeRepository())
    called = []

    class BrokenProcess:
        @staticmethod
        def wait():
            raise OSError("gone")

    service._wait_for_shell_command_exit(
        BrokenProcess(), "known-nfc", ["cmd"], lambda: called.append(True)
    )

    assert called == [True]


def test_wait_for_shell_command_exit_kills_command_after_timeout():
    service = Service(FakeCLF(), FakeRepository())
    called = []

    class HangingProcess:
        def __init__(self):
            self.killed = False

        def wait(self, timeout=None):
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired(cmd="cmd", timeout=timeout)
            return -9

        def kill(self):
            self.killed = True

    process = HangingProcess()
    service._wait_for_shell_command_exit(
        process, "known-nfc", ["cmd"], lambda: called.append(True), timeout_seconds=30
    )

    assert process.killed is True
    assert called == [True]


def test_run_shell_command_with_exit_handler_uses_own_thread(monkeypatch):
    service = Service(FakeCLF(), FakeRepository())
    started_threads = []

    class FakeProcess:
        @staticmethod
        def wait(timeout=None):
            return 0

    class RecordingThread:
        def __init__(self, target, args, name, daemon):
            started_threads.append({"name": name, "daemon": daemon, "args": args})
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    class FailingPool:
        @staticmethod
        def submit(*args, **kwargs):
            raise AssertionError("exit handler must not use the shared pool")

    called = []
    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(service_module, "Thread", RecordingThread)
    service._shell_command_monitor_pool = FailingPool()

    assert service._run_shell_command(
        "echo hi", "known-nfc", on_exit=lambda: called.append(True), timeout_seconds=30
    ) is True
    assert called == [True]
    assert started_threads[0]["daemon"] is True
    assert started_threads[0]["args"][-1] == 30


def test_exit_after_slot_expired_still_relocks_when_no_run_owns_door(tmp_path, monkeypatch):
    service, clock, runs, _ = make_service(tmp_path, monkeypatch)
    completed = []
    service.on_unlock_shell_command_complete = lambda: completed.append(True)

    service.run_unlock_shell_command("known-nfc", actor="card", source="nfc")
    clock.now += Service.UNLOCK_SLOT_EXPIRY_SECONDS
    runs_before = len(runs)
    # A newer run claims and then fails to start, leaving no run owning the door
    monkeypatch.setattr(
        service,
        "_run_shell_command",
        lambda *a, **k: False,
    )
    service.run_unlock_shell_command("home-unlock", actor="home-app", source="homekit")

    runs[runs_before - 1]["on_exit"]()

    assert completed == [True]


def test_monitor_thread_failure_kills_unlock_command_and_reports_not_started(monkeypatch):
    service = Service(FakeCLF(), FakeRepository())
    killed = []

    class FakeProcess:
        pid = 4321

    class BrokenThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(service_module.subprocess, "Popen", lambda *a, **k: FakeProcess())
    monkeypatch.setattr(service_module, "Thread", BrokenThread)
    monkeypatch.setattr(Service, "_kill_process_group", staticmethod(killed.append))

    assert service._run_shell_command("echo hi", "known-nfc", on_exit=lambda: None) is False
    assert len(killed) == 1


def test_kill_process_group_uses_killpg_when_available(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321

        def kill(self):
            calls.append("kill")

    monkeypatch.setattr(service_module.os, "killpg", lambda pid, sig: calls.append(("killpg", pid)), raising=False)
    monkeypatch.setattr(service_module.signal, "SIGKILL", 9, raising=False)

    Service._kill_process_group(FakeProcess())

    assert calls == [("killpg", 4321)]


def test_kill_process_group_ignores_already_exited_process(monkeypatch):
    class FakeProcess:
        pid = 4321

    def gone(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(service_module.os, "killpg", gone, raising=False)
    monkeypatch.setattr(service_module.signal, "SIGKILL", 9, raising=False)

    Service._kill_process_group(FakeProcess())
