"""Regression tests for the bundled Steam Skill request recovery."""

from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path
from unittest import TestCase, mock


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / ".k_agent/content/skills/steam-daily-deals/scripts/fetch_steam_deals.py"
)


def _load_skill_module():
    spec = importlib.util.spec_from_file_location("steam_daily_deals_script", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


class SteamDailyDealsRequestTests(TestCase):
    def setUp(self):
        self.skill = _load_skill_module()

    def test_retries_incomplete_response_then_returns_json(self):
        responses = [
            http.client.IncompleteRead(b'{"partial":', 10),
            _Response({"specials": {"items": []}}),
        ]
        with mock.patch.object(
            self.skill.urllib.request, "urlopen", side_effect=responses
        ) as urlopen, mock.patch.object(self.skill.time, "sleep") as sleep:
            result = self.skill.fetch_json("https://store.steampowered.com/test")

        self.assertEqual(result, {"specials": {"items": []}})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once()

    def test_stops_after_bounded_number_of_attempts(self):
        failure = http.client.RemoteDisconnected("connection closed")
        with mock.patch.object(
            self.skill.urllib.request, "urlopen", side_effect=failure
        ) as urlopen, mock.patch.object(self.skill.time, "sleep") as sleep:
            result = self.skill.fetch_json("https://store.steampowered.com/test")

        self.assertIsNone(result)
        self.assertEqual(urlopen.call_count, self.skill.REQUEST_MAX_ATTEMPTS)
        self.assertEqual(sleep.call_count, self.skill.REQUEST_MAX_ATTEMPTS - 1)
