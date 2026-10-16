"""Tests that build the Lock on a real HAP-python driver, not stand-ins."""
import asyncio

import pytest

from main import configure_hap_accessory


class RecordingService:
    on_unlock_shell_command = "unlock-cmd"

    def __init__(self):
        self.unlocks = []
        self.on_endpoint_authenticated = None
        self.on_unlock_shell_command_complete = None

    def run_unlock_shell_command(self, reason, **kwargs):
        self.unlocks.append((reason, kwargs))
        return True

    def call_if_unlock_running(self, action):
        return False

    def call_if_unlock_idle(self, action):
        return False


@pytest.fixture
def hap(tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    service = RecordingService()
    driver, lock = configure_hap_accessory(
        {"port": 0, "persist": str(tmp_path / "hap.state")}, service
    )
    yield driver, lock, service
    asyncio.set_event_loop(None)
    loop.close()


def test_lock_builds_on_real_driver(hap):
    driver, lock, _ = hap

    assert driver.accessory is lock
    assert lock.lock_target_state.value == 0


def test_homekit_unlock_write_logs_sender_address(hap):
    driver, lock, service = hap
    iid = lock.iid_manager.get_iid(lock.lock_target_state)

    driver.set_characteristics(
        {"characteristics": [{"aid": lock.aid, "iid": iid, "value": 0}]},
        ("192.168.2.30", 49152),
    )

    assert service.unlocks == [
        (
            "home-unlock",
            {
                "actor": "home-app",
                "actor_type": "homekit-client",
                "source": "homekit",
                "details": {"client_address": "192.168.2.30:49152"},
            },
        )
    ]
    assert lock._lock_target_sender is None
