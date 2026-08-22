import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

import watcher


def make_search_url(action: str, sort: str = "MobileModifiedDate") -> str:
    payload = {"type": "car", "action": action, "sort": sort}
    encoded = quote(json.dumps(payload, separators=(",", ":")))
    return f"https://car.encar.com/list/car?page=1&search={encoded}"


def sample_spec() -> watcher.SearchSpec:
    return watcher.SearchSpec(
        key="sample",
        label="Sample",
        action="(And.Test)",
        sort="MobileModifiedDate",
        min_expected_results=1,
    )


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original = os.environ.get("SEARCHES_JSON")

    def tearDown(self) -> None:
        if self.original is None:
            os.environ.pop("SEARCHES_JSON", None)
        else:
            os.environ["SEARCHES_JSON"] = self.original

    def test_v2_preserves_search_action(self) -> None:
        url = make_search_url("(And.Price.range(..1111).Mileage.range(..99999))")
        os.environ["SEARCHES_JSON"] = json.dumps(
            {
                "format_version": 2,
                "timezone": "Europe/Berlin",
                "searches": [
                    {
                        "key": "sample",
                        "label": "Sample",
                        "search_url": url,
                        "min_expected_results": 1,
                    }
                ],
            }
        )

        config, _ = watcher.load_config()
        self.assertEqual(len(config.searches), 1)
        self.assertIn("Price.range(..1111)", config.searches[0].action)
        self.assertIn("Mileage.range(..99999)", config.searches[0].action)

    def test_v1_compatibility_migration_is_isolated(self) -> None:
        url = make_search_url("(And.Price.range(..1111).Test)")
        os.environ["SEARCHES_JSON"] = json.dumps(
            {
                "searches": [
                    {"key": "legacy", "label": "Legacy", "search_url": url}
                ]
            }
        )

        config, _ = watcher.load_config()
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


class StateTests(unittest.TestCase):
    def test_digest_is_stable_and_keyed(self) -> None:
        key_a = watcher.derive_state_key("config-a")
        key_b = watcher.derive_state_key("config-b")
        first = watcher.digest_id("12345678", key_a)
        second = watcher.digest_id("12345678", key_a)
        other = watcher.digest_id("12345678", key_b)

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(first), 64)

    def test_state_resets_when_fingerprint_changes(self) -> None:
        original_state_file = watcher.STATE_FILE
        try:
            with tempfile.TemporaryDirectory() as directory:
                watcher.STATE_FILE = Path(directory) / "state.json"
                watcher.save_state(watcher.default_state("old"))
                state = watcher.load_state("new")
                self.assertEqual(state["config_fingerprint"], "new")
                self.assertEqual(state["searches"], {})
        finally:
            watcher.STATE_FILE = original_state_file


class ProcessingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timezone = ZoneInfo("Europe/Berlin")
        self.state_key = watcher.derive_state_key("test-config")

    @patch("watcher.fetch_search")
    def test_first_watch_creates_baseline_without_notifications(self, fetch_search) -> None:
        fetch_search.return_value = [{"Id": 1001}, {"Id": 1002}]
        state = watcher.default_state("fingerprint")

        with patch("watcher.send_telegram") as send_telegram:
            count, failed = watcher.process_search(
                session=requests.Session(),
                state=state,
                state_key=self.state_key,
                spec=sample_spec(),
                index=1,
                timezone=self.timezone,
                dry_run=False,
            )

        self.assertEqual(count, 2)
        self.assertFalse(failed)
        self.assertTrue(state["searches"]["s1"]["initialized"])
        self.assertEqual(len(state["searches"]["s1"]["seen"]), 2)
        send_telegram.assert_not_called()

    @patch("watcher.fetch_search")
    def test_failed_notification_keeps_listing_unseen_for_retry(self, fetch_search) -> None:
        fetch_search.return_value = [{"Id": 2001}, {"Id": 2002}]
        state = watcher.default_state("fingerprint")
        old_hash = watcher.digest_id(2001, self.state_key)
        state["searches"]["s1"] = {
            "initialized": True,
            "seen": [old_hash],
            "is_failing": False,
            "last_error": None,
            "last_success": None,
            "last_result_count": 1,
        }

        with patch(
            "watcher.send_telegram",
            side_effect=requests.RequestException("delivery failed"),
        ):
            count, failed = watcher.process_search(
                session=requests.Session(),
                state=state,
                state_key=self.state_key,
                spec=sample_spec(),
                index=1,
                timezone=self.timezone,
                dry_run=False,
            )

        new_hash = watcher.digest_id(2002, self.state_key)
        self.assertEqual(count, 2)
        self.assertTrue(failed)
        self.assertIn(old_hash, state["searches"]["s1"]["seen"])
        self.assertNotIn(new_hash, state["searches"]["s1"]["seen"])


if __name__ == "__main__":
    unittest.main()
