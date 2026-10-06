from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest

from onprem_runner.models import NotifierConfig, RunResult, StepResult
from onprem_runner.notifiers import EmailNotifier


class FakeSMTP:
    instances: ClassVar[list[FakeSMTP]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent: EmailMessage | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}")

    def send_message(self, message: EmailMessage) -> None:
        self.sent = message


def make_result(status: str) -> RunResult:
    now = datetime.now()
    return RunResult(
        run_id="r1",
        pipeline="demo",
        started_at=now,
        finished_at=now,
        steps=[StepResult(id="a", type="python", status=status, exit_code=0)],  # type: ignore[arg-type]
    )


def test_email_is_sent_with_starttls_and_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    FakeSMTP.instances.clear()
    config = NotifierConfig(
        type="email",
        when="failure",
        smtp_host="smtp.test",  # type: ignore[call-arg]
        username="bot@test",
        password="secret",
        to=["ops@test"],
    )
    notifier = EmailNotifier(config, tmp_path)
    assert not notifier.should_notify(make_result("success"))
    result = make_result("failed")
    assert notifier.should_notify(result)
    notifier.notify(result)

    server = FakeSMTP.instances[0]
    assert server.calls == ["starttls", "login:bot@test"]
    assert server.sent is not None
    assert server.sent["Subject"] == "[FAILED] demo (r1)"
    assert server.sent["To"] == "ops@test"
