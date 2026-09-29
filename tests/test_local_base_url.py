"""`validate_local_base_url` decides loopback by parsing the address (docs/00 §6.5)."""

from tests import hermetic  # noqa: F401 — throwaway state dir; see tests/hermetic.py

import unittest

from engine.providers import ProviderError, validate_local_base_url


class TestLocalBaseUrl(unittest.TestCase):
    def test_literal_loopback_addresses_pass(self) -> None:
        for url in (
            "http://localhost:11434",
            "http://127.0.0.1:11434",
            "http://127.255.255.254:11434",
            "http://[::1]:11434",
        ):
            with self.subTest(url=url):
                validate_local_base_url(url)

    def test_a_hostname_that_merely_starts_with_127_is_refused(self) -> None:
        # `startswith("127.")` read this as loopback; it is a DNS name that
        # resolves wherever its owner says, so the "local" provider would send
        # prompts and keys off the machine.
        for url in (
            "http://127.evil.example:11434",
            "http://127.0.0.1.evil.example:11434",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ProviderError) as caught:
                    validate_local_base_url(url)
                self.assertEqual("invalid_base_url", caught.exception.code)

    def test_non_loopback_and_non_http_are_still_refused(self) -> None:
        for url in ("http://192.168.1.5:11434", "http://10.0.0.1", "https://localhost"):
            with self.subTest(url=url):
                with self.assertRaises(ProviderError):
                    validate_local_base_url(url)


if __name__ == "__main__":
    unittest.main()
