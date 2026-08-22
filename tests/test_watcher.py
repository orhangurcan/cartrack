import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

import watcher


def make_search_url(action: str, sort: str = "MobileModifiedDate") -> str:
    payload = {"type": "car", "action": action, "sort": sort}
    encoded = quote(json.dumps(payload, separators=(",", ":")))
    return f"https://car.encar.com/list/car?page=1&search={encoded}"


def sample_spec(minimum: int = 0) -> watcher.SearchSpec:
    return watcher.SearchSpec(
        key="sample",
        label="Sample",
        action="(And.Test)",
        sort="MobileModifiedDate",
        min_expected_results=minimum,
    )


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = os.environ.get("SEARCHES_JSON")

    def tearDown(self) -> None:
        if self.original is None:
            os.environ.pop("SEARCHES_JSON", None)
        else:
            os.environ["SEARCHES_JSON"] = self.original

    def test_v2_preserves_search_action_and_accepts_zero_minimum(self) -> None:
        url = make_search_url("(And.Price.range(..1111).Mileage.range(..99999))")
        os.environ["SEARCHES_JSON"] = json.dumps(
            {
                "format_version": 2,
                "timezone": "Europe/Berlin",
                "searches": [{"key": "sample", "label": "Sample", "search_url": url}],
            }
        )
        config = watcher.load_config()
        self.assertEqual(config.searches[0].min_expected_results, 0)
        self.assertIn("Price.range(..1111)", config.searches[0].action)

    def test_v1_compatibility_migration_is_isolated(self) -> None:
        url = make_search_url("(And.Price.range(..1111).Test)")
        os.environ["SEARCHES_JSON"] = json.dumps(
            {"searches": [{"key": "legacy", "label": "Legacy", "search_url": url}]}
        )
        config = watcher.load_config()
        self.assertIn("Price.range(..1211)", config.searches[0].action)

    def test_duplicate_search_keys_are_rejected(self) -> None:
        url = make_search_url("(And.Test)")
        os.environ["SEARCHES_JSON"] = json.dumps(
            {
                "format_version": 2,
                "searches": [
                    {"key": "same", "label": "One", "search_url": url},
                    {"key": "same", "label": "Two", "search_url": url},
                ],
            }
        )
        with self.assertRaises(RuntimeError):
            watcher.load_config()

    def test_non_encar_hosts_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            watcher.decode_search_url("https://example.com/?search=%7B%7D")

    def test_fingerprint_ignores_label_timezone_and_minimum(self) -> None:
        first = watcher.AppConfig(searches=(sample_spec(),), timezone=ZoneInfo("Europe/Berlin"))
        second = watcher.AppConfig(
            searches=(
                watcher.SearchSpec(
                    key="sample",
                    label="Different label",
                    action="(And.Test)",
                    sort="MobileModifiedDate",
                    min_expected_results=99,
                ),
            ),
            timezone=ZoneInfo("UTC"),
        )
        self.assertEqual(watcher.config_fingerprint(first), watcher.config_fingerprint(second))


class StateTests(unittest.TestCase):
    def test_encrypted_state_round_trip_and_not_plaintext(self) -> None:
        state = watcher.default_state("fingerprint")
        state["searches"]["private-key"] = {"initialized": True, "seen": ["abc"]}
        payload = watcher.encrypt_state(state, "secret")
        self.assertNotIn(b"private-key", payload)
        self.assertNotIn(b"abc", payload)
        self.assertEqual(watcher.decrypt_state(payload, "secret"), state)

    def test_wrong_state_secret_fails_authentication(self) -> None:
        payload = watcher.encrypt_state(watcher.default_state("fp"), "secret-a")
        with self.assertRaises(RuntimeError):
            watcher.decrypt_state(payload, "secret-b")

    def test_digest_is_stable_and_keyed(self) -> None:
        key_a = watcher.derive_key("secret-a", "listing")
        key_b = watcher.derive_key("secret-b", "listing")
        first = watcher.digest_id("12345678", key_a)
        self.assertEqual(first, watcher.digest_id("12345678", key_a))
        self.assertNotEqual(first, watcher.digest_id("12345678", key_b))


class SearchTests(unittest.TestCase):
    @patch("watcher.api_get")
    def test_zero_results_are_valid_by_default(self, api_get) -> None:
        api_get.return_value = {"Count": 0, "SearchResults": []}
        self.assertEqual(watcher.fetch_search(requests.Session(), sample_spec()), [])

    @patch("watcher.api_get")
    def test_configured_minimum_can_reject_low_count(self, api_get) -> None:
        api_get.return_value = {"Count": 0, "SearchResults": []}
        with self.assertRaises(RuntimeError):
            watcher.fetch_search(requests.Session(), sample_spec(minimum=1))


class ProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timezone = ZoneInfo("Europe/Berlin")
        self.hmac_key = watcher.derive_key("test-secret", "listing")

    @patch("watcher.fetch_search")
    def test_zero_result_first_watch_initializes_empty_baseline(self, fetch_search) -> None:
        fetch_search.return_value = []
        state = watcher.default_state("fingerprint")
        count, failed, changed = watcher.process_search(
            requests.Session(), state, self.hmac_key, sample_spec(), 1, self.timezone, False
        )
        self.assertEqual(count, 0)
        self.assertFalse(failed)
        self.assertTrue(changed)
        self.assertTrue(state["searches"]["sample"]["initialized"])
        self.assertEqual(state["searches"]["sample"]["seen"], [])

    @patch("watcher.fetch_search")
    def test_first_listing_after_empty_baseline_is_notified(self, fetch_search) -> None:
        fetch_search.return_value = [{"Id": 2001}]
        state = watcher.default_state("fingerprint")
        state["searches"]["sample"] = {
            "initialized": True,
            "seen": [],
            "fetch_failing": False,
            "delivery_failing": False,
        }
        with patch("watcher.send_telegram") as send:
            count, failed, changed = watcher.process_search(
                requests.Session(), state, self.hmac_key, sample_spec(), 1, self.timezone, False
            )
        self.assertEqual(count, 1)
        self.assertFalse(failed)
        self.assertTrue(changed)
        send.assert_called_once()

    @patch("watcher.fetch_search")
    def test_failed_notification_keeps_listing_unseen_for_retry(self, fetch_search) -> None:
        fetch_search.return_value = [{"Id": 2001}, {"Id": 2002}]
        old_hash = watcher.digest_id(2001, self.hmac_key)
        state = watcher.default_state("fingerprint")
        state["searches"]["sample"] = {
            "initialized": True,
            "seen": [old_hash],
            "fetch_failing": False,
            "delivery_failing": False,
        }
        with patch("watcher.send_telegram", side_effect=RuntimeError("delivery failed")):
            _, failed, changed = watcher.process_search(
                requests.Session(), state, self.hmac_key, sample_spec(), 1, self.timezone, False
            )
        self.assertTrue(failed)
        self.assertTrue(changed)
        self.assertNotIn(watcher.digest_id(2002, self.hmac_key), state["searches"]["sample"]["seen"])

    @patch("watcher.fetch_search")
    def test_unchanged_success_does_not_mutate_state(self, fetch_search) -> None:
        fetch_search.return_value = [{"Id": 3001}]
        existing = watcher.digest_id(3001, self.hmac_key)
        state = watcher.default_state("fingerprint")
        state["searches"]["sample"] = {
            "initialized": True,
            "seen": [existing],
            "fetch_failing": False,
            "delivery_failing": False,
        }
        _, failed, changed = watcher.process_search(
            requests.Session(), state, self.hmac_key, sample_spec(), 1, self.timezone, False
        )
        self.assertFalse(failed)
        self.assertFalse(changed)


class TelegramTests(unittest.TestCase):
    def test_retry_after_is_read_from_429_payload(self) -> None:
        response = Mock()
        response.json.return_value = {"parameters": {"retry_after": 3}}
        self.assertEqual(watcher.telegram_retry_after(response), 3.0)


if __name__ == "__main__":
    unittest.main()
