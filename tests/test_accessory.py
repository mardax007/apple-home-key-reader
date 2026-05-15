from accessory import Lock


class FakeCharacteristic:
    def __init__(self):
        self.values = []

    def set_value(self, value, should_notify=False):
        self.values.append((value, should_notify))


class FakeService:
    def __init__(self, command="unlock-cmd"):
        self.on_known_nfc_shell_command = command
        self.calls = []

    def run_unlock_shell_command(self, reason):
        command = self.on_known_nfc_shell_command
        self.calls.append((command, reason))


class FakeLock:
    set_lock_target_state = Lock.set_lock_target_state


def test_set_lock_target_state_runs_unlock_command_for_home_unlock():
    lock = FakeLock()
    lock.service = FakeService()
    lock._lock_current_state = 1
    lock._lock_target_state = 1
    lock.lock_current_state = FakeCharacteristic()

    result = lock.set_lock_target_state(0)

    assert result == 0
    assert lock.lock_current_state.values == [(0, True)]
    assert lock.service.calls == [("unlock-cmd", "home-unlock")]


def test_set_lock_target_state_does_not_run_unlock_command_for_lock():
    lock = FakeLock()
    lock.service = FakeService()
    lock._lock_current_state = 0
    lock._lock_target_state = 0
    lock.lock_current_state = FakeCharacteristic()

    result = lock.set_lock_target_state(1)

    assert result == 1
    assert lock.lock_current_state.values == [(1, True)]
    assert lock.service.calls == []
