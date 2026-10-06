from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from onprem_runner.models import RunResult
from onprem_runner.notifiers.base import Notifier
from onprem_runner.notifiers.render import render_text


class EmailParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    smtp_host: str
    smtp_port: int = 587
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = None
    password: str | None = None
    from_addr: str | None = None
    to: list[str] = Field(min_length=1)
    subject: str = "[{status}] {pipeline} ({run_id})"
    timeout: float = 30.0


class EmailNotifier(Notifier):
    """Send the run summary by SMTP. Put credentials in ``${ENV_VAR}`` references."""

    params_model = EmailParams
    params: EmailParams

    def notify(self, result: RunResult) -> None:
        p = self.params
        message = EmailMessage()
        message["Subject"] = p.subject.format(
            status=result.status.upper(), pipeline=result.pipeline, run_id=result.run_id
        )
        message["From"] = p.from_addr or p.username or "onprem-runner@localhost"
        message["To"] = ", ".join(p.to)
        message.set_content(render_text(result))

        smtp_cls = smtplib.SMTP_SSL if p.security == "ssl" else smtplib.SMTP
        with smtp_cls(p.smtp_host, p.smtp_port, timeout=p.timeout) as server:
            if p.security == "starttls":
                server.starttls()
            if p.username and p.password:
                server.login(p.username, p.password)
            server.send_message(message)
