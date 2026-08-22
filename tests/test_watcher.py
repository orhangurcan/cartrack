import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import watcher


def make_search_url(action: str, sort: str = "MobileModifiedDate") -> str:
    payload = {"type": "car", "action": action, "sort": sort}
    encoded = quote(json.dumps(payload, separators=(",", ":")))
    return f"https://car.encar.com/list/car?page=1&search={encoded}"


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


if __name__ == "__main__":
    unittest.main()
