from __future__ import annotations

import base64
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import write_pipeline
from onprem_runner.config import load_config
from onprem_runner.logs import LogLine
from onprem_runner.models import NotifierConfig, RunResult, StepResult
from onprem_runner.notifiers import EmailNotifier
from onprem_runner.runner import Runner
from onprem_runner.secrets_store import (
    DpapiCipher,
    SecretError,
    SecretStore,
    referenced_secrets,
)


class ReverseCipher:
    """Test cipher: not secure, but proves values are transformed on disk."""

    def encrypt(self, data: bytes) -> bytes:
        return b"enc:" + data[::-1]

    def decrypt(self, data: bytes) -> bytes:
        assert data.startswith(b"enc:")
        return data[4:][::-1]


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "data" / "secrets.json", ReverseCipher())


# -- store ---------------------------------------------------------------------------


def test_set_get_list_delete(store: SecretStore) -> None:
    assert store.names() == []
    store.set("DB_PASSWORD", "s3cr3t!")
    store.set("API_KEY", "k-123456")
    assert store.names() == ["API_KEY", "DB_PASSWORD"]
    assert store.get("DB_PASSWORD") == "s3cr3t!"
    assert store.exists("API_KEY")
    assert store.delete("API_KEY") is True
    assert store.delete("API_KEY") is False
    assert store.names() == ["DB_PASSWORD"]


def test_values_are_not_written_in_clear(store: SecretStore) -> None:
    store.set("TOKEN", "plain-text-value")
    raw = store.path.read_text(encoding="utf-8")
    assert "plain-text-value" not in raw
    assert "TOKEN" in raw  # names stay readable


def test_values_survive_a_new_store_instance(store: SecretStore) -> None:
    store.set("TOKEN", "abc-123")
    reopened = SecretStore(store.path, ReverseCipher())
    assert reopened.get("TOKEN") == "abc-123"


def test_replace_overwrites_the_value(store: SecretStore) -> None:
    store.set("TOKEN", "old-value")
    store.set("TOKEN", "new-value")
    assert store.get("TOKEN") == "new-value"
    assert store.names() == ["TOKEN"]


@pytest.mark.parametrize("name", ["", "1ABC", "has space", "dash-name", "a" * 65, "ñame"])
def test_invalid_names_are_rejected(store: SecretStore, name: str) -> None:
    with pytest.raises(SecretError, match="name"):
        store.set(name, "value")


def test_empty_value_is_rejected(store: SecretStore) -> None:
    with pytest.raises(SecretError, match="empty"):
        store.set("TOKEN", "")


def test_unicode_values_round_trip(store: SecretStore) -> None:
    store.set("TOKEN", "contraseña-日本語-\U0001f511")
    assert store.get("TOKEN") == "contraseña-日本語-\U0001f511"


def test_missing_secret_is_a_clear_error(store: SecretStore) -> None:
    with pytest.raises(SecretError, match="'NOPE' is not set"):
        store.get("NOPE")
    with pytest.raises(SecretError, match="NOPE"):
        store.expand("x ${secret:NOPE} y")


def test_corrupted_file_is_reported_not_silently_emptied(store: SecretStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(SecretError, match="unreadable"):
        store.names()
    with pytest.raises(SecretError):
        store.set("TOKEN", "value")  # must not overwrite a file it cannot read


def test_secret_that_cannot_be_decrypted_explains_why(store: SecretStore) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    garbage = base64.b64encode(b"not-encrypted").decode()
    store.path.write_text(
        f'{{"version": 1, "secrets": {{"TOKEN": "{garbage}"}}}}', encoding="utf-8"
    )
    with pytest.raises(SecretError, match="another Windows user"):
        store.get("TOKEN")


def test_expand_replaces_every_reference(store: SecretStore) -> None:
    store.set("USER", "ana")
    store.set("PASS", "p4ss-word")
    assert (
        store.expand("postgres://${secret:USER}:${secret:PASS}@host")
        == "postgres://ana:p4ss-word@host"
    )
    assert store.expand("no references here") == "no references here"
    assert store.expand("${NOT_A_SECRET} stays") == "${NOT_A_SECRET} stays"


def test_redactor_masks_values_longest_first_and_skips_short_ones(store: SecretStore) -> None:
    store.set("LONG", "abcdef-123456")
    store.set("PART", "abcdef")  # a prefix of LONG
    store.set("TINY", "x1")  # too short to mask without wrecking normal text
    redact = store.redactor()
    assert redact("token=abcdef-123456!") == "token=***!"  # not "***-123456"
    assert redact("only abcdef here") == "only *** here"
    assert redact("x1 is untouched") == "x1 is untouched"


def test_referenced_secrets_finds_nested_references() -> None:
    config = {
        "env": {"A": "${secret:ONE}", "B": "plain"},
        "args": ["--key", "${secret:TWO}", "${HOME}"],
        "n": 5,
    }
    assert referenced_secrets(config) == {"ONE", "TWO"}
    assert referenced_secrets("nothing") == set()


# -- Windows DPAPI (real) --------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists only on Windows")
def test_dpapi_round_trip_and_ciphertext_is_opaque() -> None:
    cipher = DpapiCipher()
    secret = b"very-secret-value-123"
    encrypted = cipher.encrypt(secret)
    assert secret not in encrypted
    assert cipher.decrypt(encrypted) == secret
    assert cipher.decrypt(cipher.encrypt(b"x" * 100_000)) == b"x" * 100_000


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists only on Windows")
def test_dpapi_rejects_tampered_data() -> None:
    cipher = DpapiCipher()
    encrypted = bytearray(cipher.encrypt(b"value-to-protect"))
    encrypted[len(encrypted) // 2] ^= 0xFF
    with pytest.raises(SecretError, match="DPAPI"):
        cipher.decrypt(bytes(encrypted))


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI exists only on Windows")
def test_store_with_real_dpapi(tmp_path: Path) -> None:
    real = SecretStore(tmp_path / "secrets.json")
    real.set("DB_PASSWORD", "hunter2-hunter2")
    assert "hunter2" not in real.path.read_text(encoding="utf-8")
    assert SecretStore(real.path).get("DB_PASSWORD") == "hunter2-hunter2"


@pytest.mark.skipif(sys.platform == "win32", reason="checks the non-Windows refusal")
def test_dpapi_refuses_to_pretend_on_other_systems() -> None:
    with pytest.raises(SecretError, match="Windows"):
        DpapiCipher()


# -- using secrets in a run ---------------------------------------------------------------


@pytest.fixture
def printer(project: Path) -> Path:
    (project / "scripts" / "show.py").write_text(
        "import os, sys\n"
        "print('token is', os.environ.get('TOKEN'))\n"
        "print('arg is', sys.argv[1] if len(sys.argv) > 1 else '-')\n"
        "print('boom ' + os.environ.get('TOKEN', ''), file=sys.stderr)\n",
        encoding="utf-8",
    )
    return project


def run_with(
    project: Path, store: SecretStore | None, body: str
) -> tuple[RunResult, list[LogLine]]:
    lines: list[LogLine] = []
    runner = Runner(
        load_config(write_pipeline(project, body)), project, secrets=store, on_log=lines.append
    )
    return runner.run(), lines


def test_secret_reaches_the_script_but_never_the_logs(printer: Path, store: SecretStore) -> None:
    store.set("API_TOKEN", "tok-9f8e7d6c")
    result, lines = run_with(
        printer,
        store,
        "name: p\nsteps:\n  - id: s\n    type: python\n    script: scripts/show.py\n"
        "    args: ['${secret:API_TOKEN}']\n    env: {TOKEN: '${secret:API_TOKEN}'}\n",
    )
    step = result.steps[0]
    assert step.status == "success"
    assert "tok-9f8e7d6c" not in step.stdout + step.stderr  # stored output is masked
    assert "token is ***" in step.stdout
    assert "arg is ***" in step.stdout
    assert all("tok-9f8e7d6c" not in line.message for line in lines)  # live log is masked
    assert any(line.message == "boom ***" and line.stream == "stderr" for line in lines)


def test_secret_in_a_shell_command(printer: Path, store: SecretStore) -> None:
    store.set("WORD", "opensesame")
    result, _ = run_with(
        printer,
        store,
        "name: p\nsteps:\n  - {id: s, type: shell, command: 'echo ${secret:WORD}'}\n",
    )
    assert result.steps[0].status == "success"
    assert result.steps[0].stdout.strip() == "***"


def test_missing_secret_fails_the_step_without_running_it(
    printer: Path, store: SecretStore
) -> None:
    result, _ = run_with(
        printer,
        store,
        "name: p\nsteps:\n  - id: s\n    type: python\n    script: scripts/show.py\n"
        "    env: {TOKEN: '${secret:MISSING}'}\n",
    )
    assert result.steps[0].status == "failed"
    assert "MISSING" in result.steps[0].stderr
    assert "token is" not in result.steps[0].stdout  # the script never ran


def test_secret_reference_without_a_store_fails_clearly(printer: Path) -> None:
    result, _ = run_with(
        printer,
        None,
        "name: p\nsteps:\n  - {id: s, type: shell, command: 'echo ${secret:X}'}\n",
    )
    assert result.steps[0].status == "failed"
    assert "no secret store" in result.steps[0].stderr


def test_secrets_added_after_runner_creation_are_masked_in_the_next_run(
    printer: Path, store: SecretStore
) -> None:
    runner = Runner(
        load_config(
            write_pipeline(
                printer, "name: p\nsteps:\n  - {id: s, type: shell, command: 'echo late-value'}\n"
            )
        ),
        printer,
        secrets=store,
    )
    assert runner.run().steps[0].stdout.strip() == "late-value"
    store.set("NEW", "late-value")
    assert runner.run().steps[0].stdout.strip() == "***"


# -- notifier -------------------------------------------------------------------------------


class FakeSMTP:
    logins: list[tuple[str, str]] = []  # noqa: RUF012

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def starttls(self) -> None:
        pass

    def login(self, user: str, password: str) -> None:
        FakeSMTP.logins.append((user, password))

    def send_message(self, message: Any) -> None:
        pass


def test_email_password_is_resolved_from_the_secret_store(
    store: SecretStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    FakeSMTP.logins.clear()
    store.set("SMTP_PASSWORD", "mail-pass-1")
    notifier = EmailNotifier(
        NotifierConfig.model_validate(
            {
                "type": "email",
                "smtp_host": "smtp.test",
                "username": "bot@test",
                "password": "${secret:SMTP_PASSWORD}",
                "to": ["ops@test"],
            }
        ),
        tmp_path,
    )
    notifier.expand = store.expand
    now = datetime.now()
    notifier.notify(RunResult("r1", "demo", now, now, [StepResult("a", "shell", "success", 0)]))
    assert FakeSMTP.logins == [("bot@test", "mail-pass-1")]


def test_the_store_can_be_created_where_dpapi_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine must start on any OS; only touching a secret needs the cipher."""

    class NoDpapi:
        def __init__(self) -> None:
            raise SecretError("encrypted secrets need Windows (DPAPI); not available on this OS")

    monkeypatch.setattr("onprem_runner.secrets_store.DpapiCipher", NoDpapi)
    store = SecretStore(tmp_path / "secrets.json")
    assert store.names() == []  # reading the (empty) list needs no cipher
    assert store.redactor()("nothing to hide") == "nothing to hide"
    with pytest.raises(SecretError, match="Windows"):
        store.set("TOKEN", "value-123")
