"""In-memory stand-in for the Security.framework generic-password backend."""

from __future__ import annotations

import threading
import time

from supersplat.errors import CredentialError


class FakeKeychain:
    """Records which calls read the secret; never touches the real Keychain."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[str] = []
        self.fail_with: CredentialError | None = None
        # Reading the secret can wait on a macOS access prompt. block_reads()
        # makes find() wait until answer_prompt(); read_delay makes it slow.
        self.read_delay = 0.0
        self.read_started = threading.Event()
        self.read_threads: list[str] = []
        self._prompt_answered = threading.Event()
        self._prompt_answered.set()

    def block_reads(self) -> None:
        self._prompt_answered.clear()

    def answer_prompt(self) -> None:
        self._prompt_answered.set()

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def exists(self, service: str, account: str) -> bool:
        self.calls.append("exists")
        self._maybe_fail()
        return (service, account) in self.items

    def find(self, service: str, account: str) -> str | None:
        self.calls.append("find")
        self.read_threads.append(threading.current_thread().name)
        self.read_started.set()
        if not self._prompt_answered.wait(30):
            raise AssertionError("FakeKeychain read stayed blocked; call answer_prompt().")
        time.sleep(self.read_delay)
        self._maybe_fail()
        return self.items.get((service, account))

    def store(self, service: str, account: str, secret: str) -> None:
        self.calls.append("store")
        self._maybe_fail()
        self.items[(service, account)] = secret

    def delete(self, service: str, account: str) -> bool:
        self.calls.append("delete")
        self._maybe_fail()
        return self.items.pop((service, account), None) is not None
