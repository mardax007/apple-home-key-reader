from accessory import Lock


class FakeCharacteristic:
    def __init__(self):
        self.values = []

    def set_value(self, value, should_notify=False):
        self.values.append((value, should_notify))


class FakeService:
    def __init__(self, command="unlock-cmd", started=True, running=False):
        self.on_unlock_shell_command = command
        self.started = started
        self.running = running
        self.calls = []

    def run_unlock_shell_command(self, reason, **kwargs):
        command = self.on_unlock_shell_command
        self.calls.append((command, reason, kwargs))
        return self.started

    def call_if_unlock_running(self, action):
        if not self.running:
            return False
        action()
        return True

    def call_if_unlock_idle(self, action):
        if self.running:
            return False
        action()
        return True


class FakeLock:
    set_lock_target_state = Lock.set_lock_target_state
    on_endpoint_authenticated = Lock.on_endpoint_authenticated
    _on_unlock_shell_command_complete = Lock._on_unlock_shell_command_complete
    _set_lock_state = Lock._set_lock_state
    _capture_lock_target_sender = Lock._capture_lock_target_sender
    _format_client_address = staticmethod(Lock._format_client_address)

    def __init__(self, service, current_state):
        self.service = service
        self._lock_current_state = current_state
        self._lock_target_state = current_state
        self._lock_target_sender = None
        self.lock_current_state = FakeCharacteristic()
        self.lock_target_state = FakeCharacteristic()


def test_set_lock_target_state_runs_unlock_command_for_home_unlock():
    lock = FakeLock(service=FakeService(), current_state=1)

    result = lock.set_lock_target_state(0)

    assert result == 0
    assert lock.lock_current_state.values == [(0, True)]
    assert lock.service.calls == [
        (
            "unlock-cmd",
            "home-unlock",
            {
                "actor": "home-app",
                "actor_type": "homekit-client",
                "source": "homekit",
                "details": None,
            },
        )
    ]


def test_set_lock_target_state_does_not_run_unlock_command_for_lock():
    lock = FakeLock(service=FakeService(), current_state=0)

    result = lock.set_lock_target_state(1)

    assert result == 1
    assert lock.lock_current_state.values == [(1, True)]
    assert lock.service.calls == []


def test_set_lock_target_state_logs_homekit_client_address():
    lock = FakeLock(service=FakeService(), current_state=1)
    lock._lock_target_sender = ("192.168.2.30", 49152)

    lock.set_lock_target_state(0)

    assert lock.service.calls[0][2]["details"] == {"client_address": "192.168.2.30:49152"}


def test_capture_lock_target_sender_exposes_address_only_during_write():
    lock = FakeLock(service=FakeService(), current_state=1)
    seen = []

    def original_client_update_value(value, sender_client_addr=None):
        seen.append((value, sender_client_addr, lock._lock_target_sender))
        return "response"

    lock.lock_target_state.client_update_value = original_client_update_value
    lock._capture_lock_target_sender()

    result = lock.lock_target_state.client_update_value(0, ("10.0.0.5", 1234))

    assert result == "response"
    assert seen == [(0, ("10.0.0.5", 1234), ("10.0.0.5", 1234))]
    assert lock._lock_target_sender is None


def test_set_lock_target_state_relocks_when_unlock_was_skipped_and_nothing_runs():
    lock = FakeLock(service=FakeService(started=False, running=False), current_state=1)

    result = lock.set_lock_target_state(0)

    assert result == 1
    assert lock.lock_target_state.values == [(1, True)]
    assert lock.lock_current_state.values == [(0, True), (1, True)]


def test_set_lock_target_state_waits_for_running_unlock_to_relock():
    lock = FakeLock(service=FakeService(started=False, running=True), current_state=1)

    result = lock.set_lock_target_state(0)

    assert result == 0
    assert lock.lock_target_state.values == []


def test_set_lock_target_state_keeps_virtual_lock_unlocked_without_command():
    lock = FakeLock(service=FakeService(command=None, started=False), current_state=1)

    result = lock.set_lock_target_state(0)

    assert result == 0
    assert lock.lock_target_state.values == []


def test_endpoint_authentication_shows_unlocked_while_unlock_runs():
    lock = FakeLock(service=FakeService(running=True), current_state=1)

    lock.on_endpoint_authenticated("endpoint")

    assert (lock._lock_target_state, lock._lock_current_state) == (0, 0)
    assert lock.lock_target_state.values == [(0, True)]
    assert lock.lock_current_state.values == [(0, True)]


def test_endpoint_authentication_does_not_toggle_to_locked_on_second_tap():
    lock = FakeLock(service=FakeService(running=True), current_state=0)

    lock.on_endpoint_authenticated("endpoint")

    assert (lock._lock_target_state, lock._lock_current_state) == (0, 0)


def test_endpoint_authentication_leaves_state_when_no_unlock_runs():
    lock = FakeLock(service=FakeService(running=False), current_state=1)

    lock.on_endpoint_authenticated("endpoint")

    assert (lock._lock_target_state, lock._lock_current_state) == (1, 1)
    assert lock.lock_target_state.values == []


def test_endpoint_authentication_toggles_virtual_lock_without_command():
    lock = FakeLock(service=FakeService(command=None), current_state=1)

    lock.on_endpoint_authenticated("endpoint")
    assert (lock._lock_target_state, lock._lock_current_state) == (0, 0)

    lock.on_endpoint_authenticated("endpoint")
    assert (lock._lock_target_state, lock._lock_current_state) == (1, 1)


def test_unlock_command_completion_relocks():
    lock = FakeLock(service=FakeService(), current_state=0)

    lock._on_unlock_shell_command_complete()

    assert (lock._lock_target_state, lock._lock_current_state) == (1, 1)
    assert lock.lock_target_state.values == [(1, True)]
    assert lock.lock_current_state.values == [(1, True)]


def test_format_client_address():
    assert Lock._format_client_address(None) is None
    assert Lock._format_client_address(("fe80::1", 5000, 0, 2)) == "fe80::1:5000"
    assert Lock._format_client_address("hub") == "hub"
