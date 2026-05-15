import signal

import main


class FakeLogger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(message)


class FakeStoppable:
    def __init__(self):
        self.calls = 0

    def stop(self):
        self.calls += 1


def test_shutdown_handler_is_idempotent_for_repeated_signals():
    logger = FakeLogger()
    service = FakeStoppable()
    driver = FakeStoppable()
    handler = main.create_shutdown_handler(logger, service, driver)

    handler(signal.SIGTERM, None)
    handler(signal.SIGTERM, None)

    assert service.calls == 1
    assert driver.calls == 1
    assert logger.messages == [
        f"SIGNAL {signal.SIGTERM}",
        f"SIGNAL {signal.SIGTERM} ignored (shutdown already in progress)",
    ]


def test_register_shutdown_signals_registers_both_handlers(monkeypatch):
    captured = []

    def fake_signal(sig, handler):
        captured.append((sig, handler))

    handler = object()
    monkeypatch.setattr(main.signal, "signal", fake_signal)

    main.register_shutdown_signals(handler)

    assert captured == [(signal.SIGINT, handler), (signal.SIGTERM, handler)]
