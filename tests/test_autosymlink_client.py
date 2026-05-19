import os
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path


os.environ.setdefault("PROCESSED_DIR", "/tmp/pikpak-bridge-tests")
os.environ.setdefault("ALIST_HOST", "http://127.0.0.1:5244")
os.environ.setdefault("ALIST_USERNAME", "admin")
os.environ.setdefault("ALIST_PASSWORD", "password")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    fake_requests = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(RequestException=Exception)
    )
    sys.modules["requests"] = fake_requests

import autosymlink_client
import webhook


class FakeResponse:
    def __init__(self, status_code=204, text="ok"):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append(
            {
                "url": url,
                "json": json,
                "headers": headers,
                "timeout": timeout,
            }
        )
        return self.response


class FakeHandler:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def read_json_payload(self):
        if self.error:
            raise self.error
        return self.payload


class FakeThread:
    started = []

    def __init__(self, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon

    def start(self):
        self.started.append(self)


def reset_autosymlink_pending_job():
    with autosymlink_client.AUTOSYMLINK_SCHEDULE_LOCK:
        autosymlink_client.AUTOSYMLINK_PENDING_JOB = None


class AutoSymlinkDecisionTests(unittest.TestCase):
    def test_first_episode_triggers_refresh(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "1", "totalEpisodeNumber": "12"}
        )

        self.assertTrue(decision.should_refresh)
        self.assertEqual(decision.trigger, "first_episode")
        self.assertEqual(decision.episode, 1)
        self.assertEqual(decision.total_episode_number, 12)

    def test_final_episode_triggers_refresh(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": 12, "totalEpisodeNumber": 12}
        )

        self.assertTrue(decision.should_refresh)
        self.assertEqual(decision.trigger, "final_episode")

    def test_single_episode_only_schedules_once(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "1", "totalEpisodeNumber": "1"}
        )

        self.assertTrue(decision.should_refresh)
        self.assertEqual(decision.trigger, "first_episode")

    def test_middle_episode_triggers_refresh(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "6", "totalEpisodeNumber": "12"}
        )

        self.assertTrue(decision.should_refresh)
        self.assertFalse(decision.ignored)
        self.assertEqual(decision.trigger, "episode")
        self.assertEqual(decision.reason, "episode")

    def test_fractional_episode_is_ignored(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "1.5", "totalEpisodeNumber": "12"}
        )

        self.assertFalse(decision.should_refresh)
        self.assertIn("integer", decision.reason)

    def test_zero_total_triggers_unknown_total_refresh(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "2", "totalEpisodeNumber": "0"}
        )

        self.assertTrue(decision.should_refresh)
        self.assertFalse(decision.ignored)
        self.assertEqual(decision.total_episode_number, None)
        self.assertEqual(decision.trigger, "unknown_total_episode")
        self.assertEqual(decision.total_episode_number_warning, None)

    def test_missing_total_still_triggers_refresh_with_warning(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "2"}
        )

        self.assertTrue(decision.should_refresh)
        self.assertFalse(decision.ignored)
        self.assertEqual(decision.trigger, "unknown_total_episode")
        self.assertEqual(decision.total_episode_number_warning, "totalEpisodeNumber missing")

    def test_invalid_total_still_triggers_refresh_with_warning(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "2", "totalEpisodeNumber": "abc"}
        )

        self.assertTrue(decision.should_refresh)
        self.assertFalse(decision.ignored)
        self.assertEqual(decision.trigger, "unknown_total_episode")
        self.assertEqual(decision.total_episode_number_warning, "totalEpisodeNumber invalid")

    def test_nan_episode_is_ignored(self):
        decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "NaN", "totalEpisodeNumber": "12"}
        )

        self.assertFalse(decision.should_refresh)
        self.assertIn("invalid", decision.reason)


class AutoSymlinkScheduleTests(unittest.TestCase):
    def setUp(self):
        reset_autosymlink_pending_job()
        FakeThread.started = []

    def tearDown(self):
        reset_autosymlink_pending_job()
        FakeThread.started = []

    def test_pending_refresh_merges_next_webhook(self):
        first_decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "1", "totalEpisodeNumber": "0"}
        )
        next_decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "2", "totalEpisodeNumber": "0"}
        )

        with patch.object(autosymlink_client, "Thread", FakeThread):
            first = autosymlink_client.schedule_autosymlink_refresh(
                {"title": "Anime", "season": 1},
                first_decision,
            )
            second = autosymlink_client.schedule_autosymlink_refresh(
                {"title": "Anime", "season": 1},
                next_decision,
            )

        self.assertTrue(first["scheduled"])
        self.assertFalse(first["merged"])
        self.assertFalse(second["scheduled"])
        self.assertTrue(second["merged"])
        self.assertEqual(first["job"], second["job"])
        self.assertEqual(len(FakeThread.started), 1)

    def test_running_refresh_allows_next_pending_job(self):
        first_decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "1", "totalEpisodeNumber": "0"}
        )
        next_decision = autosymlink_client.evaluate_ani_rss_autosymlink_payload(
            {"episode": "2", "totalEpisodeNumber": "0"}
        )

        with patch.object(autosymlink_client, "Thread", FakeThread):
            first = autosymlink_client.schedule_autosymlink_refresh(
                {"title": "Anime", "season": 1},
                first_decision,
            )
            autosymlink_client.mark_autosymlink_refresh_running(first["job"])
            second = autosymlink_client.schedule_autosymlink_refresh(
                {"title": "Anime", "season": 1},
                next_decision,
            )

        self.assertTrue(first["scheduled"])
        self.assertTrue(second["scheduled"])
        self.assertFalse(second["merged"])
        self.assertNotEqual(first["job"], second["job"])
        self.assertEqual(len(FakeThread.started), 2)


class AutoSymlinkRequestTests(unittest.TestCase):
    def setUp(self):
        self.original = {
            "base_url": autosymlink_client.AUTOSYMLINK_BASE_URL,
            "api_key": autosymlink_client.AUTOSYMLINK_API_KEY,
            "cookie": autosymlink_client.AUTOSYMLINK_COOKIE,
            "task_uuid": autosymlink_client.AUTOSYMLINK_TASK_UUID,
            "body_json": autosymlink_client.AUTOSYMLINK_REQUEST_BODY_JSON,
            "timeout": autosymlink_client.AUTOSYMLINK_REQUEST_TIMEOUT_SECONDS,
        }
        autosymlink_client.AUTOSYMLINK_BASE_URL = "http://autos.example:8095"
        autosymlink_client.AUTOSYMLINK_API_KEY = "secret-for-test"
        autosymlink_client.AUTOSYMLINK_COOKIE = "authenticated=true"
        autosymlink_client.AUTOSYMLINK_TASK_UUID = "db3bc5a7-2864-4e78-8131-636c8d1b5e0c"
        autosymlink_client.AUTOSYMLINK_REQUEST_BODY_JSON = '{"mode":"manual"}'
        autosymlink_client.AUTOSYMLINK_REQUEST_TIMEOUT_SECONDS = 9

    def tearDown(self):
        autosymlink_client.AUTOSYMLINK_BASE_URL = self.original["base_url"]
        autosymlink_client.AUTOSYMLINK_API_KEY = self.original["api_key"]
        autosymlink_client.AUTOSYMLINK_COOKIE = self.original["cookie"]
        autosymlink_client.AUTOSYMLINK_TASK_UUID = self.original["task_uuid"]
        autosymlink_client.AUTOSYMLINK_REQUEST_BODY_JSON = self.original["body_json"]
        autosymlink_client.AUTOSYMLINK_REQUEST_TIMEOUT_SECONDS = self.original["timeout"]

    def test_trigger_posts_to_autosymlink_sync_endpoint(self):
        session = FakeSession(FakeResponse(status_code=200, text="queued"))

        ok = autosymlink_client.trigger_autosymlink_refresh_once(
            {"title": "Test", "episode": 1, "total_episode_number": 12},
            session=session,
        )

        self.assertTrue(ok)
        self.assertEqual(len(session.calls), 1)
        call = session.calls[0]
        self.assertEqual(
            call["url"],
            "http://autos.example:8095/common_tools/add_sync_task/db3bc5a7-2864-4e78-8131-636c8d1b5e0c",
        )
        self.assertEqual(call["json"], {"mode": "manual"})
        self.assertEqual(call["headers"]["X-API-Key"], "secret-for-test")
        self.assertEqual(call["headers"]["Authorization"], "Bearer secret-for-test")
        self.assertEqual(call["headers"]["Cookie"], "authenticated=true")
        self.assertEqual(call["timeout"], 9)

    def test_missing_config_does_not_call_autosymlink(self):
        autosymlink_client.AUTOSYMLINK_BASE_URL = ""
        session = FakeSession(FakeResponse(status_code=200))

        ok = autosymlink_client.trigger_autosymlink_refresh_once({}, session=session)

        self.assertFalse(ok)
        self.assertEqual(session.calls, [])


class AutoSymlinkWebhookTests(unittest.TestCase):
    def setUp(self):
        self.original_scheduler = webhook.schedule_autosymlink_refresh
        self.original_writer = webhook.write_json_response
        self.schedules = []
        self.responses = []

        def fake_scheduler(payload, decision):
            self.schedules.append((payload, decision))
            return {
                "job": "test-job",
                "delay_seconds": 75,
                "scheduled": True,
                "merged": False,
            }

        def fake_writer(handler, status_code, payload):
            self.responses.append((status_code, payload))

        webhook.schedule_autosymlink_refresh = fake_scheduler
        webhook.write_json_response = fake_writer

    def tearDown(self):
        webhook.schedule_autosymlink_refresh = self.original_scheduler
        webhook.write_json_response = self.original_writer

    def handle_payload(self, payload=None, error=None):
        handler = FakeHandler(payload=payload, error=error)
        webhook.WebhookRequestHandler.handle_ani_rss_autosymlink(handler)
        self.assertEqual(len(self.responses), 1)
        return self.responses[0]

    def test_webhook_schedules_first_episode(self):
        status, payload = self.handle_payload(
            {"episode": "1", "totalEpisodeNumber": "12", "title": "Anime"}
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ignored"])
        self.assertTrue(payload["scheduled"])
        self.assertEqual(payload["trigger"], "first_episode")
        self.assertEqual(len(self.schedules), 1)

    def test_webhook_schedules_middle_episode(self):
        status, payload = self.handle_payload(
            {"episode": "6", "totalEpisodeNumber": "12", "title": "Anime"}
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ignored"])
        self.assertTrue(payload["scheduled"])
        self.assertEqual(payload["reason"], "episode")
        self.assertEqual(len(self.schedules), 1)

    def test_webhook_reports_merged_refresh(self):
        def fake_merged_scheduler(payload, decision):
            self.schedules.append((payload, decision))
            return {
                "job": "test-job",
                "delay_seconds": 75,
                "scheduled": False,
                "merged": True,
            }

        webhook.schedule_autosymlink_refresh = fake_merged_scheduler

        status, payload = self.handle_payload(
            {"episode": "2", "totalEpisodeNumber": "0", "title": "Anime"}
        )

        self.assertEqual(status, 200)
        self.assertFalse(payload["ignored"])
        self.assertFalse(payload["scheduled"])
        self.assertTrue(payload["merged"])
        self.assertEqual(payload["reason"], "merged into pending refresh")
        self.assertEqual(len(self.schedules), 1)

    def test_webhook_rejects_invalid_json(self):
        status, payload = self.handle_payload(
            error=webhook.WebhookRequestError(400, "invalid json")
        )

        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])


if __name__ == "__main__":
    unittest.main()
