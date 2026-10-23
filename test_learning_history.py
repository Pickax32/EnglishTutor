"""Offline learning-history/review tests. Run in a new Python process.

Only test authentication values are installed; Windows system variables remain.
Every socket connection is blocked before importing server. Redis is replaced by
a versioned WATCH/MULTI fake and OpenAI is mocked, so this suite cannot use the
actual Redis database or incur OpenAI charges. No production environment value
or exception message is printed by this program.
"""

import copy
import hashlib
import importlib
import json
import os
import socket
import sys
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from redis.exceptions import WatchError
from werkzeug.security import generate_password_hash


class FakeRedis:
    """Test-only transaction fake with optimistic locking, not production storage."""

    def __init__(self):
        self.values = {}
        self.versions = {}
        self.lock = threading.RLock()
        self.commands = []
        self.unavailable = False
        self.force_watch_retries = 0

    def get(self, key):
        with self.lock:
            if self.unavailable:
                raise ConnectionError("test-only storage outage")
            self.commands.append(("get", key))
            return self.values.get(key)

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.watched = {}
        self.queued = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def watch(self, key):
        with self.client.lock:
            if self.client.unavailable:
                raise ConnectionError("test-only storage outage")
            self.watched[key] = self.client.versions.get(key, 0)
            self.client.commands.append(("watch", key))

    def get(self, key):
        return self.client.get(key)

    def multi(self):
        self.client.commands.append(("multi",))

    def set(self, key, value):
        self.queued.append((key, value))

    def execute(self):
        with self.client.lock:
            if self.client.unavailable:
                raise ConnectionError("test-only storage outage")
            if self.client.force_watch_retries:
                self.client.force_watch_retries -= 1
                raise WatchError()
            if any(self.client.versions.get(key, 0) != version for key, version in self.watched.items()):
                raise WatchError()
            for key, value in self.queued:
                self.client.values[key] = value
                self.client.versions[key] = self.client.versions.get(key, 0) + 1
                self.client.commands.append(("set", key))
            return [True] * len(self.queued)


SERVER = None
STORE_MODULE = None
NETWORK_GUARDS = []


def setUpModule():
    global SERVER, STORE_MODULE
    for target in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
        guard = patch(target, side_effect=AssertionError("External network is blocked in offline tests."))
        NETWORK_GUARDS.append(guard)
        guard.start()
    test_env = {
        "APP_ENV": "development", "RENDER": "false", "RATELIMIT_STORAGE_URI": "",
        "APP_PASSWORD_HASH": generate_password_hash("offline-test-password", method="pbkdf2:sha256:1"),
        "FLASK_SECRET_KEY": "offline-test-session-key-not-for-production",
        "OPENAI_API_KEY": "offline-test-key-not-for-production",
    }
    # Preserve SystemRoot, WINDIR, PATH and all existing Windows system settings.
    with patch.dict(os.environ, test_env, clear=False):
        import learning_store
        SERVER = importlib.import_module("server")
        STORE_MODULE = learning_store
    SERVER.app.config.update(TESTING=True)
    SERVER.limiter.enabled = False


def tearDownModule():
    for guard in reversed(NETWORK_GUARDS):
        guard.stop()


def new_id():
    return uuid.uuid4().hex


def response(payload):
    return SimpleNamespace(status="completed", output=[], output_text=json.dumps(payload), error=None, incomplete_details=None)


class LearningCase(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.store = STORE_MODULE.LearningStore(self.redis)
        self.store_patch = patch.object(SERVER, "learning_store", self.store)
        self.store_patch.start()
        self.addCleanup(self.store_patch.stop)
        self.client = SERVER.app.test_client()
        self.headers = {"X-CSRF-Token": "offline-csrf-token"}
        self.authenticate(self.client)
        self.openai_patch = patch.object(SERVER, "OpenAI")
        self.ai = self.openai_patch.start()
        self.addCleanup(self.openai_patch.stop)
        self.create_ai = self.ai.return_value.__enter__.return_value.responses.create
        self.create_ai.return_value = response({"reply": "That sounds fun! What did you enjoy?", "correction": None})

    def authenticate(self, client):
        with client.session_transaction() as state:
            state["authenticated"] = True
            state["csrf_token"] = "offline-csrf-token"

    def create(self, request_id=None):
        result = self.client.post("/api/learning", json={"request_id": request_id} if request_id else {}, headers=self.headers)
        self.assertEqual(result.status_code, 201)
        return result.json["session"]["id"]

    def chat(self, identifier, turn_id=None, text="I like travel.", **extras):
        turn_id = turn_id or new_id()
        payload = {"messages": [{"role": "user", "content": text}], "learning_id": identifier, "turn_id": turn_id, **extras}
        return self.client.post("/api/chat", json=payload, headers=self.headers), turn_id

    def commit(self, identifier, turn_id):
        return self.client.post(f"/api/learning/{identifier}/turns/{turn_id}/commit", json={}, headers=self.headers)

    def cancel(self, identifier, turn_id):
        return self.client.post(f"/api/learning/{identifier}/turns/{turn_id}/cancel", json={}, headers=self.headers)

    def saved(self, text="I like travel.", correction=None):
        identifier = self.create()
        if correction:
            self.create_ai.return_value = response({"reply": "That sounds fun! What did you enjoy?", "correction": correction})
        result, turn_id = self.chat(identifier, text=text)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.commit(identifier, turn_id).status_code, 200)
        return identifier, turn_id

    def finish(self, identifier):
        return self.client.post(f"/api/learning/{identifier}/finish", json={}, headers=self.headers)

    def feedback_response(self):
        return response({
            "expressions": [{"expression": "I enjoy travelling.", "explanation": "여행을 좋아한다고 자연스럽게 말해요.", "source": "travel"}],
            "vocabulary": [{"expression": "travel", "explanation": "여행을 뜻하는 단어예요.", "source": "travel"}],
        })

    def test_create_and_list(self):
        identifier = self.create()
        result = self.client.get("/api/learning")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["sessions"][0]["id"], identifier)
        self.assertFalse(result.json["limits"]["automatic_expiration"])
        self.assertEqual(result.json["limits"]["max_account_bytes"], 8 * 1024 * 1024)

    def test_create_idempotent_request(self):
        identifier = new_id()
        self.assertEqual(self.create(identifier), identifier)
        self.assertEqual(self.create(identifier), identifier)
        self.assertEqual(len(self.store.list_sessions()), 1)

    def test_create_invalid_request_id(self):
        result = self.client.post("/api/learning", json={"request_id": "other-account"}, headers=self.headers)
        self.assertEqual(result.status_code, 400)

    def test_create_unknown_field(self):
        result = self.client.post("/api/learning", json={"account_id": "untrusted"}, headers=self.headers)
        self.assertEqual(result.status_code, 400)

    def test_large_create_body(self):
        result = self.client.post("/api/learning", json={"request_id": "x" * 9000}, headers=self.headers)
        self.assertEqual(result.status_code, 413)

    def test_detail_missing(self):
        self.assertEqual(self.client.get("/api/learning/" + new_id()).status_code, 404)

    def test_detail_invalid_identifier(self):
        self.assertEqual(self.client.get("/api/learning/other-user").status_code, 400)

    def test_chat_stages_without_saving(self):
        identifier = self.create()
        result, turn_id = self.chat(identifier)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["learning"]["state"], "pending")
        detail = self.store.get_session(identifier)
        self.assertEqual(detail["turns"], [])
        self.assertEqual(detail["pending_turn_id"], turn_id)
        self.assertEqual(detail["pending_state"], "ready")

    def test_commit_persists_canonical_turn(self):
        identifier, turn_id = self.saved()
        turn = self.store.get_session(identifier)["turns"][0]
        self.assertEqual(turn["id"], turn_id)
        self.assertEqual(turn["user"], "I like travel.")
        self.assertEqual(turn["settings"], {"difficulty": "intermediate", "topic": "free"})
        self.assertIn("+00:00", turn["created_at"])
        self.assertEqual(self.create_ai.call_count, 1)

    def test_commit_duplicate_is_idempotent(self):
        identifier, turn_id = self.saved()
        self.assertEqual(self.commit(identifier, turn_id).status_code, 200)
        self.assertEqual(len(self.store.get_session(identifier)["turns"]), 1)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_duplicate_chat_returns_staged_without_new_api(self):
        identifier = self.create()
        first, turn_id = self.chat(identifier)
        second, _ = self.chat(identifier, turn_id)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json, second.json)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_duplicate_chat_returns_committed_without_new_api(self):
        identifier, turn_id = self.saved()
        result, _ = self.chat(identifier, turn_id)
        self.assertEqual(result.json["learning"]["state"], "committed")
        self.assertEqual(self.create_ai.call_count, 1)

    def test_same_turn_id_different_text_rejected(self):
        identifier, turn_id = self.saved()
        result, _ = self.chat(identifier, turn_id, "Different message")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_same_turn_id_different_settings_rejected(self):
        identifier, turn_id = self.saved()
        result, _ = self.chat(identifier, turn_id, settings={"difficulty": "beginner", "topic": "daily"})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_pending_prevents_new_paid_chat(self):
        identifier = self.create()
        self.chat(identifier)
        result, _ = self.chat(identifier)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_cancel_staged_discards_raw_turn(self):
        identifier = self.create()
        _, turn_id = self.chat(identifier)
        self.assertEqual(self.cancel(identifier, turn_id).json["state"], "cancelled")
        self.assertEqual(self.store.get_session(identifier)["turns"], [])
        raw = json.loads(self.redis.values[STORE_MODULE.ACCOUNT_KEY])
        self.assertNotIn("turn", raw["sessions"][identifier]["attempts"][turn_id])
        self.assertEqual(self.commit(identifier, turn_id).status_code, 409)

    def test_cancel_before_claim_prevents_paid_request(self):
        identifier, turn_id = self.create(), new_id()
        self.assertEqual(self.cancel(identifier, turn_id).status_code, 200)
        self.assertEqual(self.chat(identifier, turn_id)[0].status_code, 409)
        self.create_ai.assert_not_called()

    def test_cancel_while_api_running_prevents_late_stage(self):
        identifier, turn_id = self.create(), new_id()

        def delayed_response(**_kwargs):
            self.store.cancel_turn(identifier, turn_id)
            return response({"reply": "Nice! Where next?", "correction": None})

        self.create_ai.side_effect = delayed_response
        self.assertEqual(self.chat(identifier, turn_id)[0].status_code, 409)
        self.assertEqual(self.store.get_session(identifier)["turns"], [])
        self.assertIsNone(self.store.get_session(identifier)["pending_turn_id"])

    def test_cancel_after_commit_reports_saved_state(self):
        identifier, turn_id = self.saved()
        self.assertEqual(self.cancel(identifier, turn_id).json["state"], "committed")
        self.assertEqual(len(self.store.get_session(identifier)["turns"]), 1)

    def test_failed_chat_never_saved_or_repeated(self):
        identifier, turn_id = self.create(), new_id()
        self.create_ai.side_effect = RuntimeError("test-only API error")
        self.assertEqual(self.chat(identifier, turn_id)[0].status_code, 502)
        self.assertEqual(self.chat(identifier, turn_id)[0].status_code, 409)
        self.assertEqual(self.store.get_session(identifier)["turns"], [])
        self.assertEqual(self.create_ai.call_count, 1)

    def test_bad_ai_reply_not_staged(self):
        identifier = self.create()
        self.create_ai.return_value = response({"reply": " ", "correction": None})
        result, _ = self.chat(identifier)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.store.get_session(identifier)["turns"], [])

    def test_unpaired_surrogate_user_rejected_before_paid_call(self):
        identifier = self.create()
        result, _ = self.chat(identifier, text="invalid\ud800")
        self.assertEqual(result.status_code, 400)
        self.create_ai.assert_not_called()

    def test_unpaired_surrogate_reply_safely_rejected(self):
        identifier = self.create()
        self.create_ai.return_value = response({"reply": "invalid\ud800", "correction": None})
        result, _ = self.chat(identifier)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.store.get_session(identifier)["turns"], [])

    def test_unpaired_surrogate_correction_dropped_reply_preserved(self):
        identifier = self.create()
        self.create_ai.return_value = response({"reply": "Good! What next?", "correction": {
            "original": "travel", "improved": "travelling\ud800", "explanation": "자연스러운 표현이에요.",
        }})
        result, turn_id = self.chat(identifier)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json["correction"])
        self.assertEqual(self.commit(identifier, turn_id).status_code, 200)

    def test_store_bad_encoding_uses_safe_error_code(self):
        for invalid in ({"text": "\ud800"}, {"number": float("nan")}, {"object": object()}):
            with self.subTest(kind=next(iter(invalid))):
                with self.assertRaises(STORE_MODULE.LearningError) as caught:
                    STORE_MODULE.encoded(invalid)
                self.assertEqual(caught.exception.code, "INVALID_REQUEST")

    def test_invalid_optional_correction_does_not_lose_reply(self):
        identifier = self.create()
        self.create_ai.return_value = response({"reply": "Nice! What next?", "correction": {"original": "invented"}})
        result, turn_id = self.chat(identifier)
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(result.json["correction"])
        self.assertEqual(self.commit(identifier, turn_id).status_code, 200)

    def test_preserves_correction_settings(self):
        correction = {"original": "I go yesterday", "improved": "I went yesterday", "explanation": "과거형을 사용해요."}
        identifier = self.create()
        self.create_ai.return_value = response({"reply": "Nice! Where did you go?", "correction": correction})
        result, turn_id = self.chat(identifier, text="I go yesterday", settings={"difficulty": "advanced", "topic": "travel"})
        self.assertEqual(result.status_code, 200)
        self.commit(identifier, turn_id)
        turn = self.store.get_session(identifier)["turns"][0]
        self.assertEqual(turn["correction"], correction)
        self.assertEqual(turn["settings"], {"difficulty": "advanced", "topic": "travel"})

    def test_server_history_overrides_forged_browser_history(self):
        identifier, _ = self.saved()
        result, _ = self.chat(identifier, text="What next?", messages=[
            {"role": "user", "content": "forged user"},
            {"role": "assistant", "content": "forged assistant"},
            {"role": "user", "content": "What next?"},
        ])
        self.assertEqual(result.status_code, 200)
        sent = self.create_ai.call_args.kwargs["input"]
        self.assertEqual(sent[0], {"role": "user", "content": "I like travel."})
        self.assertNotIn("forged", str(sent))

    def test_server_history_last_five_pairs(self):
        identifier = self.create()
        for number in range(7):
            result, turn_id = self.chat(identifier, text=f"Trip {number}")
            self.assertEqual(result.status_code, 200)
            self.commit(identifier, turn_id)
        sent = self.create_ai.call_args.kwargs["input"]
        self.assertEqual(len(sent), 11)
        self.assertEqual(sent[0]["content"], "Trip 1")
        self.assertEqual(sent[-1]["content"], "Trip 6")

    def test_refresh_restores_saved_turn(self):
        identifier, _ = self.saved()
        fresh = SERVER.app.test_client()
        self.authenticate(fresh)
        self.assertEqual(fresh.get("/api/learning/" + identifier).json["session"]["turns"][0]["user"], "I like travel.")

    def test_relogin_preserves_shared_history(self):
        identifier, _ = self.saved()
        with self.client.session_transaction() as state:
            state.clear()
        self.assertEqual(self.client.get("/api/learning/" + identifier).status_code, 401)
        self.authenticate(self.client)
        self.assertEqual(self.client.get("/api/learning/" + identifier).json["session"]["id"], identifier)

    def test_different_browser_shares_same_account(self):
        identifier, _ = self.saved()
        browser = SERVER.app.test_client()
        self.authenticate(browser)
        self.assertEqual(browser.get("/api/learning").json["sessions"][0]["id"], identifier)

    def test_restart_new_store_reads_same_redis(self):
        identifier, _ = self.saved()
        replacement = STORE_MODULE.LearningStore(self.redis)
        self.assertEqual(len(replacement.get_session(identifier)["turns"]), 1)

    def test_export_only_committed_turns(self):
        identifier, _ = self.saved()
        self.chat(identifier, text="pending private text")
        result = self.client.get("/api/learning/export")
        self.assertEqual(result.status_code, 200)
        self.assertIn("attachment", result.headers["Content-Disposition"])
        record = result.json["sessions"][0]
        self.assertEqual(len(record["turns"]), 1)
        self.assertNotIn("pending private text", result.get_data(as_text=True))
        self.assertNotIn("attempts", record)
        self.assertNotIn("request_hash", result.get_data(as_text=True))

    def test_delete_idempotent(self):
        identifier, _ = self.saved()
        for _ in range(2):
            self.assertEqual(self.client.delete("/api/learning/" + identifier, json={}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/learning/" + identifier).status_code, 404)

    def test_delete_during_api_never_recreates_record(self):
        identifier = self.create()

        def delete_while_running(**_kwargs):
            self.store.delete(identifier)
            return response({"reply": "Great! Why?", "correction": None})

        self.create_ai.side_effect = delete_while_running
        self.assertEqual(self.chat(identifier)[0].status_code, 404)
        self.assertEqual(self.store.list_sessions(), [])

    def test_redis_outage_before_chat_blocks_openai(self):
        identifier = self.create()
        self.redis.unavailable = True
        self.assertEqual(self.chat(identifier)[0].status_code, 503)
        self.create_ai.assert_not_called()

    def test_redis_outage_after_paid_reply_keeps_reply_unsaved(self):
        identifier = self.create()

        def fail_storage(**_kwargs):
            self.redis.unavailable = True
            return response({"reply": "Good! Why?", "correction": None})

        self.create_ai.side_effect = fail_storage
        result, _ = self.chat(identifier)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["reply"], "Good! Why?")
        self.assertEqual(result.json["learning"]["state"], "save_failed")
        self.assertEqual(self.create_ai.call_count, 1)
        self.redis.unavailable = False
        self.assertEqual(self.store.get_session(identifier)["turns"], [])

    def test_commit_outage_never_reports_saved(self):
        identifier = self.create()
        _, turn_id = self.chat(identifier)
        self.redis.unavailable = True
        result = self.commit(identifier, turn_id)
        self.assertEqual(result.status_code, 503)
        self.assertNotIn("saved", result.json)
        self.redis.unavailable = False
        self.assertEqual(self.commit(identifier, turn_id).status_code, 200)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_missing_history_uri_fails_without_api(self):
        with patch.object(SERVER, "learning_store", STORE_MODULE.LearningStore(None)):
            self.assertEqual(self.client.post("/api/learning", json={}, headers=self.headers).status_code, 503)
        self.create_ai.assert_not_called()

    def test_single_namespace_and_no_expiration_commands(self):
        identifier, turn_id = self.saved()
        self.cancel(identifier, turn_id)
        self.assertEqual(set(self.redis.values), {STORE_MODULE.ACCOUNT_KEY})
        self.assertTrue(STORE_MODULE.ACCOUNT_KEY.startswith("EnglishTutor:learning:v1:"))
        self.assertNotEqual(STORE_MODULE.ACCOUNT_KEY, "EnglishTutor")
        self.assertTrue(all(command[0] in ("get", "watch", "multi", "set") for command in self.redis.commands))

    def test_watch_conflict_retries_only_storage(self):
        self.redis.force_watch_retries = 2
        self.create()
        self.assertEqual(len(self.store.list_sessions()), 1)
        self.create_ai.assert_not_called()

    def test_watch_exhaustion_safe_conflict(self):
        self.redis.force_watch_retries = 20
        result = self.client.post("/api/learning", json={}, headers=self.headers)
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.redis.values, {})

    def test_max_sessions_does_not_remove_old_records(self):
        with patch.object(STORE_MODULE, "MAX_SESSIONS", 2):
            identifiers = [self.create(), self.create()]
            result = self.client.post("/api/learning", json={}, headers=self.headers)
            self.assertEqual(result.status_code, 409)
            self.assertEqual({item["id"] for item in self.store.list_sessions()}, set(identifiers))

    def test_max_attempts_blocks_before_paid_request(self):
        with patch.object(STORE_MODULE, "MAX_TURNS", 2):
            identifier = self.create()
            for _ in range(2):
                self.cancel(identifier, new_id())
            self.assertEqual(self.chat(identifier)[0].status_code, 409)
            self.create_ai.assert_not_called()

    def test_account_capacity_reserved_before_paid_request(self):
        identifier = self.create()
        with patch.object(STORE_MODULE, "MAX_ACCOUNT_BYTES", 1000):
            self.assertEqual(self.chat(identifier)[0].status_code, 409)
        self.create_ai.assert_not_called()

    def test_corrupted_store_never_overwritten(self):
        self.redis.values[STORE_MODULE.ACCOUNT_KEY] = b"not json"
        before = self.redis.values[STORE_MODULE.ACCOUNT_KEY]
        self.assertEqual(self.client.post("/api/learning", json={}, headers=self.headers).status_code, 503)
        self.assertEqual(self.redis.values[STORE_MODULE.ACCOUNT_KEY], before)

    def test_oversized_store_never_overwritten(self):
        self.redis.values[STORE_MODULE.ACCOUNT_KEY] = b" " * (STORE_MODULE.MAX_ACCOUNT_BYTES + 1)
        self.assertEqual(self.client.get("/api/learning").status_code, 503)

    def test_concurrent_claim_only_one_winner(self):
        identifier = self.create()

        def claim(_number):
            try:
                return self.store.claim_turn(identifier, new_id(), "test-digest")["claimed"]
            except STORE_MODULE.LearningError:
                return False

        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(claim, range(6))), 1)

    def test_concurrent_commit_no_duplicate(self):
        identifier = self.create()
        _, turn_id = self.chat(identifier)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.commit_turn(identifier, turn_id), range(6)))
        self.assertTrue(all(len(result["turns"]) == 1 for result in results))
        self.assertEqual(len(self.store.get_session(identifier)["turns"]), 1)

    def test_finish_empty_refused_without_paid_call(self):
        self.assertEqual(self.finish(self.create()).status_code, 409)
        self.create_ai.assert_not_called()

    def test_finish_pending_refused_without_additional_paid_call(self):
        identifier = self.create()
        self.chat(identifier)
        self.assertEqual(self.finish(identifier).status_code, 409)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_finish_saves_feedback_one_paid_call(self):
        identifier, _ = self.saved()
        self.create_ai.reset_mock()
        self.create_ai.return_value = self.feedback_response()
        result = self.finish(identifier)
        self.assertEqual(result.status_code, 200)
        record = result.json["session"]
        self.assertEqual(record["status"], "finished")
        self.assertEqual(record["feedback_status"], "complete")
        self.assertEqual(record["feedback"]["vocabulary"][0]["expression"], "travel")
        self.assertEqual(self.create_ai.call_count, 1)
        self.assertEqual(self.create_ai.call_args.kwargs["max_output_tokens"], 2500)
        self.assertFalse(self.create_ai.call_args.kwargs["store"])

    def test_finish_repeat_uses_cached_feedback(self):
        identifier, _ = self.saved()
        self.create_ai.return_value = self.feedback_response()
        first = self.finish(identifier)
        second = self.finish(identifier)
        self.assertEqual(first.json, second.json)
        self.assertEqual(self.create_ai.call_count, 2)  # one chat + one review

    def test_finish_running_returns_pending_without_api(self):
        identifier, _ = self.saved()
        self.store.claim_feedback(identifier)
        self.assertEqual(self.finish(identifier).status_code, 202)
        self.assertEqual(self.create_ai.call_count, 1)

    def test_finish_invalid_ai_retains_turns_and_no_retry(self):
        identifier, _ = self.saved()
        self.create_ai.return_value = response({"expressions": "bad", "vocabulary": []})
        first = self.finish(identifier)
        self.assertEqual(first.status_code, 502)
        self.assertEqual(first.json["session"]["feedback_status"], "failed")
        self.assertEqual(len(first.json["session"]["turns"]), 1)
        self.assertEqual(self.finish(identifier).status_code, 409)
        self.assertEqual(self.create_ai.call_count, 2)

    def test_finish_exception_retains_corrections(self):
        correction = {"original": "I go yesterday", "improved": "I went yesterday", "explanation": "과거형을 사용해요."}
        identifier, _ = self.saved("I go yesterday", correction)
        self.create_ai.side_effect = RuntimeError("test-only review failure")
        self.assertEqual(self.finish(identifier).status_code, 502)
        self.assertEqual(self.store.get_session(identifier)["turns"][0]["correction"], correction)

    def test_finish_storage_failure_remains_sticky_no_retry(self):
        identifier, _ = self.saved()

        def paid_then_outage(**_kwargs):
            self.redis.unavailable = True
            return self.feedback_response()

        self.create_ai.side_effect = paid_then_outage
        self.assertEqual(self.finish(identifier).status_code, 503)
        self.redis.unavailable = False
        self.assertEqual(self.finish(identifier).status_code, 202)
        self.assertEqual(self.create_ai.call_count, 2)

    def test_finish_redis_outage_before_claim_no_paid_call(self):
        identifier, _ = self.saved()
        self.create_ai.reset_mock()
        self.redis.unavailable = True
        self.assertEqual(self.finish(identifier).status_code, 503)
        self.create_ai.assert_not_called()

    def test_finished_learning_blocks_chat_new_context_allowed(self):
        identifier, _ = self.saved()
        self.create_ai.return_value = self.feedback_response()
        self.finish(identifier)
        self.assertEqual(self.chat(identifier)[0].status_code, 409)
        another = self.create()
        self.create_ai.return_value = response({"reply": "Hello! How are you?", "correction": None})
        self.assertEqual(self.chat(another, text="Hello")[0].status_code, 200)
        self.assertEqual(self.create_ai.call_args.kwargs["input"], [{"role": "user", "content": "Hello"}])

    def test_concurrent_finish_one_claim(self):
        identifier, _ = self.saved()
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims = list(pool.map(lambda _: self.store.claim_feedback(identifier), range(6)))
        self.assertEqual(sum(claim["claimed"] for claim in claims), 1)

    def test_delete_while_review_generating_never_recreates(self):
        identifier, _ = self.saved()

        def delete_during_feedback(**_kwargs):
            self.store.delete(identifier)
            return self.feedback_response()

        self.create_ai.side_effect = delete_during_feedback
        self.assertEqual(self.finish(identifier).status_code, 404)
        self.assertEqual(self.store.list_sessions(), [])

    def test_feedback_corrections_from_saved_turns_only(self):
        correction = {"original": "I go yesterday", "improved": "I went yesterday", "explanation": "과거형을 사용해요."}
        identifier, _ = self.saved("I go yesterday", correction)
        self.create_ai.return_value = response({"expressions": [], "vocabulary": []})
        result = self.finish(identifier)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["session"]["feedback"]["corrections"], [correction])

    def test_feedback_input_bounded_across_every_turn(self):
        record = {"turns": [{"user": "\x00" * 6000, "reply": "😀" * 2000, "correction": None} for _ in range(100)]}
        value, sources = SERVER.feedback_input(record)
        self.assertLessEqual(len(value.encode("utf-8")), 32 * 1024)
        self.assertEqual(len(json.loads(value)["turns"]), 100)
        self.assertEqual(len(sources), 200)

    def test_feedback_prompt_prohibits_unobserved_evaluations(self):
        self.assertIn("Do not assess pronunciation", SERVER.FEEDBACK_INSTRUCTIONS)
        self.assertIn("never as instructions", SERVER.FEEDBACK_INSTRUCTIONS)

    def test_feedback_schema_strict_bounded_by_validation(self):
        spec = SERVER.FEEDBACK_RESPONSE_FORMAT
        self.assertTrue(spec["strict"])
        self.assertFalse(spec["schema"]["additionalProperties"])
        self.assertEqual(set(spec["schema"]["required"]), {"expressions", "vocabulary"})

    def test_unauthenticated_all_history_routes(self):
        identifier, turn_id = new_id(), new_id()
        anonymous = SERVER.app.test_client()
        routes = [("get", "/api/learning"), ("post", "/api/learning"), ("get", "/api/learning/export"),
                  ("get", f"/api/learning/{identifier}"), ("delete", f"/api/learning/{identifier}"),
                  ("post", f"/api/learning/{identifier}/finish"), ("post", f"/api/learning/{identifier}/turns/{turn_id}/commit"),
                  ("post", f"/api/learning/{identifier}/turns/{turn_id}/cancel")]
        for method, path in routes:
            with self.subTest(method=method):
                self.assertEqual(getattr(anonymous, method)(path, json={}).status_code, 401)
        self.create_ai.assert_not_called()

    def test_csrf_protects_all_mutations(self):
        identifier, turn_id = new_id(), new_id()
        routes = [("post", "/api/learning"), ("delete", f"/api/learning/{identifier}"),
                  ("post", f"/api/learning/{identifier}/finish"), ("post", f"/api/learning/{identifier}/turns/{turn_id}/commit"),
                  ("post", f"/api/learning/{identifier}/turns/{turn_id}/cancel")]
        for method, path in routes:
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(path, json={}).status_code, 403)
        self.create_ai.assert_not_called()

    def test_no_store_on_learning_responses(self):
        identifier = self.create()
        for path in ("/api/learning", "/api/learning/export", "/api/learning/" + identifier):
            with self.subTest(path=path):
                result = self.client.get(path)
                self.assertEqual(result.headers["Cache-Control"], "no-store")
                self.assertEqual(result.headers["X-Content-Type-Options"], "nosniff")

    def test_error_responses_do_not_echo_exception_details(self):
        with patch.object(self.store, "create", side_effect=STORE_MODULE.LearningError("STORAGE_UNAVAILABLE")):
            result = self.client.post("/api/learning", json={}, headers=self.headers)
        self.assertNotIn("redis://", result.get_data(as_text=True))
        self.assertNotIn("test-only", result.get_data(as_text=True))

    def test_existing_health_unchanged(self):
        result = self.client.get("/api/health")
        self.assertEqual(result.json, {"status": "ok", "service": "EnglishTutor"})

    def test_old_chat_contract_kept(self):
        result = self.client.post("/api/chat", json={"messages": [{"role": "user", "content": "Hello"}]}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(set(result.json), {"reply", "correction"})
        self.assertEqual(self.store.list_sessions(), [])

    def test_chat_rejects_large_body_before_openai(self):
        result = self.client.post("/api/chat", json={"messages": [], "extra": "x" * (1024 * 1024)}, headers=self.headers)
        self.assertEqual(result.status_code, 413)
        self.create_ai.assert_not_called()

    def test_windows_system_environment_preserved(self):
        before = {key: os.environ.get(key) for key in ("SystemRoot", "WINDIR", "PATH")}
        with patch.dict(os.environ, {"APP_ENV": "production"}, clear=False):
            self.assertEqual({key: os.environ.get(key) for key in before}, before)

    def test_tls_client_uses_verified_redis_without_connection(self):
        with patch.object(STORE_MODULE.redis.Redis, "from_url") as factory:
            client = STORE_MODULE.redis_client_from_uri("rediss://offline-test.invalid:6380/0")
            self.assertIs(client, factory.return_value)
            options = factory.call_args.kwargs
            self.assertEqual(options["ssl_cert_reqs"], "required")
            self.assertTrue(options["ssl_check_hostname"])
            self.assertEqual(options["socket_timeout"], 3)
            self.assertEqual(options["socket_connect_timeout"], 3)

    def test_feedback_finish_rate_limit_blocks_third_request(self):
        identifier, _ = self.saved()
        self.create_ai.return_value = self.feedback_response()
        SERVER.limiter.enabled = True
        SERVER.limiter.reset()
        try:
            self.assertEqual(self.finish(identifier).status_code, 200)
            self.assertEqual(self.finish(identifier).status_code, 200)
            self.assertEqual(self.finish(identifier).status_code, 429)
            self.assertEqual(self.create_ai.call_count, 2)
        finally:
            SERVER.limiter.enabled = False

    def test_real_openai_sdk_feedback_request_with_mock_transport(self):
        import httpx
        from openai import OpenAI as RealOpenAI

        identifier, _ = self.saved()
        intercepted = []

        def transport_handler(outgoing):
            payload = json.loads(outgoing.content)
            intercepted.append(payload)
            text = json.dumps({"expressions": [], "vocabulary": []})
            return httpx.Response(200, json={
                "id": "resp_offline_test", "object": "response", "created_at": 1,
                "status": "completed", "error": None, "incomplete_details": None,
                "model": "gpt-5.4-mini", "output": [{"type": "message", "id": "msg_offline_test", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            })

        def client_factory(**kwargs):
            return RealOpenAI(api_key="offline-test-not-an-api-key", http_client=httpx.Client(transport=httpx.MockTransport(transport_handler)), **kwargs)

        with patch.object(SERVER, "OpenAI", side_effect=client_factory):
            result = self.finish(identifier)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(intercepted), 1)
        payload = intercepted[0]
        self.assertEqual(payload["text"]["format"], SERVER.FEEDBACK_RESPONSE_FORMAT)
        self.assertEqual(payload["model"], "gpt-5.4-mini")
        self.assertEqual(payload["max_output_tokens"], 2500)
        self.assertFalse(payload["store"])

    def test_bad_uri_cannot_disable_tls_or_enable_fallback(self):
        for uri in ("", "memory://", "https://offline.invalid", "rediss://offline.invalid?ssl_cert_reqs=none", "redis://offline.invalid#x", "redis://offline.invalid:99999"):
            with self.subTest(kind=uri.split(":", 1)[0]):
                with patch.object(STORE_MODULE.redis.Redis, "from_url") as factory:
                    self.assertIsNone(STORE_MODULE.redis_client_from_uri(uri))
                    factory.assert_not_called()


def _feedback_bad_case(name, payload=None, **overrides):
    def test(self):
        identifier, _ = self.saved()
        value = response(payload if payload is not None else {"expressions": [], "vocabulary": []})
        for key, item in overrides.items():
            setattr(value, key, item)
        self.create_ai.return_value = value
        self.assertEqual(self.finish(identifier).status_code, 502)
        self.assertEqual(len(self.store.get_session(identifier)["turns"]), 1)
        self.assertEqual(self.finish(identifier).status_code, 409)
        self.assertEqual(self.create_ai.call_count, 2)
    test.__name__ = "test_bad_feedback_" + name
    setattr(LearningCase, test.__name__, test)


def _corrupt_case(name, corrupt):
    def test(self):
        identifier, _ = self.saved()
        document = json.loads(self.redis.values[STORE_MODULE.ACCOUNT_KEY])
        corrupt(document["sessions"][identifier])
        raw = json.dumps(document).encode("utf-8")
        self.redis.values[STORE_MODULE.ACCOUNT_KEY] = raw
        self.assertEqual(self.client.get("/api/learning/" + identifier).status_code, 503)
        self.assertEqual(self.redis.values[STORE_MODULE.ACCOUNT_KEY], raw)
    test.__name__ = "test_corrupt_nested_" + name
    setattr(LearningCase, test.__name__, test)


for _name, _corrupt in (
    ("unknown_private_field", lambda record: record.update(unknown="private")),
    ("missing_date", lambda record: record.pop("created_at")),
    ("bad_title", lambda record: record.update(title=123)),
    ("bad_feedback_status", lambda record: record.update(feedback_status="unsafe")),
    ("bad_settings", lambda record: record["turns"][0].update(settings={"difficulty": "unsafe", "topic": "free"})),
    ("too_long_user", lambda record: record["turns"][0].update(user="x" * 6001)),
    ("surrogate_user", lambda record: record["turns"][0].update(user="invalid\ud800")),
    ("too_long_reply", lambda record: record["turns"][0].update(reply="x" * 2001)),
    ("bad_turn_id", lambda record: record["turns"][0].update(id="other-owner")),
    ("bad_correction", lambda record: record["turns"][0].update(correction={"original": "invented", "improved": "fake", "explanation": "가짜"})),
    ("bad_attempt", lambda record: record["attempts"].update({new_id(): {"state": "unsafe"}})),
    ("duplicate_turn", lambda record: record["turns"].append(copy.deepcopy(record["turns"][0]))),
    ("unvalidated_feedback", lambda record: record.update(status="finished", feedback_status="complete", feedback={"corrections": [], "expressions": [{"expression": "word", "explanation": "English"}], "vocabulary": []})),
):
    _corrupt_case(_name, _corrupt)


for _name, _payload, _overrides in (
    ("root_array", [], {}),
    ("unknown_field", {"expressions": [], "vocabulary": [], "assessment": "bad"}, {}),
    ("missing_field", {"expressions": []}, {}),
    ("too_many", {"expressions": [{"expression": "word", "explanation": "단어", "source": "travel"}] * 4, "vocabulary": []}, {}),
    ("unanchored_source", {"expressions": [{"expression": "word", "explanation": "단어", "source": "invented source"}], "vocabulary": []}, {}),
    ("blank_expression", {"expressions": [{"expression": " ", "explanation": "단어", "source": "travel"}], "vocabulary": []}, {}),
    ("long_expression", {"expressions": [{"expression": "x" * 241, "explanation": "단어", "source": "travel"}], "vocabulary": []}, {}),
    ("long_explanation", {"expressions": [{"expression": "travel", "explanation": "한" * 401, "source": "travel"}], "vocabulary": []}, {}),
    ("not_korean", {"expressions": [{"expression": "travel", "explanation": "English only", "source": "travel"}], "vocabulary": []}, {}),
    ("surrogate_expression", {"expressions": [{"expression": "travel\ud800", "explanation": "여행", "source": "travel"}], "vocabulary": []}, {}),
    ("surrogate_explanation", {"expressions": [{"expression": "travel", "explanation": "여행\ud800", "source": "travel"}], "vocabulary": []}, {}),
    ("surrogate_source", {"expressions": [{"expression": "travel", "explanation": "여행", "source": "travel\ud800"}], "vocabulary": []}, {}),
    ("null_item", {"expressions": [None], "vocabulary": []}, {}),
    ("not_list", {"expressions": {}, "vocabulary": []}, {}),
    ("refusal", None, {"output": [{"content": [{"type": "refusal"}]}]}),
    ("incomplete", None, {"status": "incomplete"}),
    ("api_error", None, {"error": {"code": "test-only"}}),
    ("incomplete_details", None, {"incomplete_details": {"reason": "max_output_tokens"}}),
    ("raw_json", None, {"output_text": "not json"}),
    ("oversized_json", None, {"output_text": "x" * (32 * 1024 + 1)}),
    ("non_json_constant", None, {"output_text": '{"expressions":NaN,"vocabulary":[]}'}),
):
    _feedback_bad_case(_name, _payload, **_overrides)


class SafeTestResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        # Never expose traceback source, local variables, or exception messages.
        exc_type, _value, tb = err
        location = "unknown"
        while tb:
            location = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
            tb = tb.tb_next
        return f"{type(test).__name__}.{test._testMethodName}: {exc_type.__name__} at {location}\n"


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2, resultclass=SafeTestResult).run(suite)
    print("PASS" if result.wasSuccessful() else "FAIL")
    raise SystemExit(0 if result.wasSuccessful() else 1)
