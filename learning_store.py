"""Bounded, permanent learning records in a Redis namespace separate from rate limits.

One shared login account has one JSON document. WATCH/MULTI makes every change
atomic across workers; no TTL, EXPIRE, SCAN, flush, or rate-limit key is used.
Only a browser-acknowledged, server-validated turn enters the permanent history.
"""

import copy
import json
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import redis
from redis.backoff import NoBackoff
from redis.exceptions import WatchError
from redis.retry import Retry

ACCOUNT_KEY = "EnglishTutor:learning:v1:account:shared"
MAX_SESSIONS = 100
MAX_TURNS = 100
MAX_ACCOUNT_BYTES = 8 * 1024 * 1024
MAX_TURN_BYTES = 128 * 1024
MAX_FEEDBACK_BYTES = 192 * 1024
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class LearningError(Exception):
    """A safe code, never a Redis exception/URL or learning text."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def valid_id(value):
    return isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise LearningError("INVALID_REQUEST") from None


def redis_client_from_uri(uri):
    # Missing/invalid history storage never silently becomes process memory.
    if not isinstance(uri, str) or not uri.strip():
        return None
    uri = uri.strip()
    try:
        parsed = urlsplit(uri)
        if (
            parsed.scheme not in ("redis", "rediss")
            or not parsed.hostname
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in uri)
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or (
                parsed.path not in ("", "/")
                and not (parsed.path.startswith("/") and parsed.path[1:].isascii() and parsed.path[1:].isdigit())
            )
        ):
            return None
        options = {
            "socket_connect_timeout": 3,
            "socket_timeout": 3,
            "retry": Retry(NoBackoff(), 0),
            "retry_on_timeout": False,
            "decode_responses": False,
        }
        if parsed.scheme == "rediss":
            options.update(ssl_cert_reqs="required", ssl_check_hostname=True)
        # from_url constructs a client only. A connection is made on a request.
        return redis.Redis.from_url(uri, **options)
    except Exception:
        return None


class LearningStore:
    def __init__(self, client):
        self.client = client

    @staticmethod
    def limits():
        return {
            "max_sessions": MAX_SESSIONS,
            "max_turns_per_session": MAX_TURNS,
            "max_attempts_per_session": MAX_TURNS,
            "max_account_bytes": MAX_ACCOUNT_BYTES,
            "automatic_expiration": False,
        }

    @staticmethod
    def _valid_turn(turn):
        if (
            not isinstance(turn, dict)
            or set(turn) != {"id", "turn_id", "user", "reply", "correction", "settings", "created_at"}
            or not valid_id(turn.get("id")) or turn.get("turn_id") != turn["id"]
            or not isinstance(turn.get("user"), str) or not 1 <= len(turn["user"]) <= 6000
            or not isinstance(turn.get("reply"), str) or not 1 <= len(turn["reply"]) <= 2000
            or not turn["user"].strip() or not turn["reply"].strip()
            or not isinstance(turn.get("created_at"), str) or len(turn["created_at"]) > 40
            or not isinstance(turn.get("settings"), dict) or set(turn["settings"]) != {"difficulty", "topic"}
            or turn["settings"]["difficulty"] not in ("beginner", "intermediate", "advanced")
            or turn["settings"]["topic"] not in ("free", "daily", "travel", "business")
        ):
            return False
        correction = turn["correction"]
        if correction is None:
            return True
        return (
            isinstance(correction, dict) and set(correction) == {"original", "improved", "explanation"}
            and all(isinstance(correction[key], str) and correction[key].strip() for key in correction)
            and len(correction["original"]) <= 6000 and len(correction["improved"]) <= 6000
            and len(correction["explanation"]) <= 400 and correction["original"] in turn["user"]
            and correction["original"] != correction["improved"]
            and any("\uac00" <= char <= "\ud7a3" for char in correction["explanation"])
        )

    @staticmethod
    def _valid_feedback(feedback, turns):
        if not isinstance(feedback, dict) or set(feedback) != {"corrections", "expressions", "vocabulary"}:
            return False
        if any(not isinstance(value, list) or len(value) > 3 for value in feedback.values()):
            return False
        actual = [turn["correction"] for turn in turns if turn["correction"]]
        if any(correction not in actual for correction in feedback["corrections"]):
            return False
        for category in ("expressions", "vocabulary"):
            for item in feedback[category]:
                if (
                    not isinstance(item, dict) or set(item) != {"expression", "explanation"}
                    or any(not isinstance(value, str) or not value.strip() for value in item.values())
                    or len(item["expression"]) > 240 or len(item["explanation"]) > 400
                    or not any("\uac00" <= char <= "\ud7a3" for char in item["explanation"])
                ):
                    return False
        try:
            return len(encoded(feedback)) <= MAX_FEEDBACK_BYTES
        except LearningError:
            return False

    def _decode(self, raw):
        if raw is None:
            return {"version": 1, "sessions": {}}
        if len(raw) > MAX_ACCOUNT_BYTES:
            raise LearningError("STORAGE_UNAVAILABLE")
        document = json.loads(raw)
        try:
            # Escaped lone surrogates/NaN can otherwise survive json.loads and
            # later fail while serializing an unrelated update or export.
            encoded(document)
        except LearningError:
            raise LearningError("STORAGE_UNAVAILABLE") from None
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "sessions"}
            or document.get("version") != 1
            or not isinstance(document.get("sessions"), dict)
            or len(document["sessions"]) > MAX_SESSIONS
        ):
            raise LearningError("STORAGE_UNAVAILABLE")
        for identifier, record in document["sessions"].items():
            if (
                not valid_id(identifier)
                or not isinstance(record, dict)
                or set(record) != {"id", "status", "title", "created_at", "updated_at", "turns", "attempts", "feedback", "feedback_status"}
                or record.get("id") != identifier
                or record.get("status") not in ("active", "finished")
                or not isinstance(record.get("turns"), list)
                or not isinstance(record.get("attempts"), dict)
                or len(record["turns"]) > MAX_TURNS
                or len(record["attempts"]) > MAX_TURNS
                or not isinstance(record.get("title"), str) or len(record["title"]) > 80
                or any(not isinstance(record.get(key), str) or len(record[key]) > 40 for key in ("created_at", "updated_at"))
                or record.get("feedback_status") not in ("none", "generating", "complete", "failed")
                or (record["status"] == "active") != (record["feedback_status"] == "none")
                or any(not self._valid_turn(turn) for turn in record["turns"])
            ):
                raise LearningError("STORAGE_UNAVAILABLE")
            turn_ids = {turn["id"] for turn in record["turns"]}
            if len(turn_ids) != len(record["turns"]):
                raise LearningError("STORAGE_UNAVAILABLE")
            pending_count = 0
            committed_ids = set()
            for turn_id, attempt in record["attempts"].items():
                if (
                    not valid_id(turn_id) or not isinstance(attempt, dict)
                    or set(attempt) - {"state", "request_hash", "turn"}
                    or attempt.get("state") not in ("running", "ready", "committed", "cancelled", "failed")
                    or ("request_hash" in attempt and (not isinstance(attempt["request_hash"], str) or len(attempt["request_hash"]) > 64))
                    or (attempt["state"] != "cancelled" and "request_hash" not in attempt)
                ):
                    raise LearningError("STORAGE_UNAVAILABLE")
                if attempt["state"] in ("running", "ready"):
                    pending_count += 1
                if attempt["state"] == "ready":
                    if not self._valid_turn(attempt.get("turn")) or attempt["turn"]["id"] != turn_id:
                        raise LearningError("STORAGE_UNAVAILABLE")
                elif "turn" in attempt:
                    raise LearningError("STORAGE_UNAVAILABLE")
                if attempt["state"] == "committed":
                    committed_ids.add(turn_id)
            if pending_count > 1 or committed_ids != turn_ids:
                raise LearningError("STORAGE_UNAVAILABLE")
            if record["feedback_status"] == "complete":
                if not self._valid_feedback(record["feedback"], record["turns"]):
                    raise LearningError("STORAGE_UNAVAILABLE")
            elif record["feedback"] is not None:
                raise LearningError("STORAGE_UNAVAILABLE")
        return document

    def _read(self):
        if self.client is None:
            raise LearningError("STORAGE_UNAVAILABLE")
        try:
            return self._decode(self.client.get(ACCOUNT_KEY))
        except LearningError:
            raise
        except Exception:
            raise LearningError("STORAGE_UNAVAILABLE") from None

    @staticmethod
    def _reserved_bytes(document):
        # Claims in different sessions share the same account-wide reservation.
        # Other creates/turns cannot consume capacity promised to an in-flight call.
        reserved = 0
        for record in document["sessions"].values():
            for attempt in record["attempts"].values():
                if attempt["state"] == "running":
                    reserved += MAX_TURN_BYTES + 1024
                elif attempt["state"] == "ready":
                    reserved += 1024
            if record["feedback_status"] == "generating":
                reserved += MAX_FEEDBACK_BYTES + 1024
        return reserved

    def _change(self, change):
        if self.client is None:
            raise LearningError("STORAGE_UNAVAILABLE")
        try:
            for _ in range(8):
                try:
                    with self.client.pipeline() as pipe:
                        pipe.watch(ACCOUNT_KEY)
                        document = self._decode(pipe.get(ACCOUNT_KEY))
                        result = change(document)
                        value = encoded(document)
                        if len(value) + self._reserved_bytes(document) > MAX_ACCOUNT_BYTES:
                            raise LearningError("LIMIT_REACHED")
                        pipe.multi()
                        # No expiry: explicit deletion is the only removal path.
                        pipe.set(ACCOUNT_KEY, value)
                        pipe.execute()
                        return result
                except WatchError:
                    # Retry only a Redis transaction, never a paid API call.
                    continue
            raise LearningError("CONFLICT")
        except LearningError:
            raise
        except Exception:
            raise LearningError("STORAGE_UNAVAILABLE") from None

    @staticmethod
    def _session(document, identifier):
        if not valid_id(identifier):
            raise LearningError("INVALID_REQUEST")
        record = document["sessions"].get(identifier)
        if record is None:
            raise LearningError("NOT_FOUND")
        return record

    @staticmethod
    def public(record):
        result = {key: copy.deepcopy(value) for key, value in record.items() if key != "attempts"}
        pending = next(
            ((key, value["state"]) for key, value in record["attempts"].items() if value["state"] in ("running", "ready")),
            (None, None),
        )
        result.update(pending_turn_id=pending[0], pending_state=pending[1])
        # A ready turn can be recovered after a refresh, but is not a saved turn.
        if pending[1] == "ready":
            result["pending_turn"] = copy.deepcopy(record["attempts"][pending[0]]["turn"])
        return result

    def list_sessions(self):
        records = self._read()["sessions"].values()
        summaries = []
        for record in records:
            public = self.public(record)
            public.pop("turns")
            public.pop("feedback", None)
            public.pop("pending_turn", None)
            public["turn_count"] = len(record["turns"])
            summaries.append(public)
        return sorted(summaries, key=lambda item: (item["created_at"], item["id"]), reverse=True)

    def get_session(self, identifier):
        return self.public(self._session(self._read(), identifier))

    def export(self):
        records = self._read()["sessions"].values()
        # Unacknowledged turns and internal idempotency tombstones are not exports.
        sessions = []
        for record in records:
            public = self.public(record)
            for key in ("pending_turn", "pending_turn_id", "pending_state"):
                public.pop(key, None)
            sessions.append(public)
        return {"version": 1, "exported_at": utc_now(), "sessions": sessions}

    def create(self, request_id=None):
        if request_id is not None and not valid_id(request_id):
            raise LearningError("INVALID_REQUEST")
        identifier = request_id or uuid.uuid4().hex
        timestamp = utc_now()

        def change(document):
            if identifier in document["sessions"]:
                return self.public(document["sessions"][identifier])
            if len(document["sessions"]) >= MAX_SESSIONS:
                raise LearningError("LIMIT_REACHED")
            record = {
                "id": identifier, "status": "active", "title": "새 학습",
                "created_at": timestamp, "updated_at": timestamp,
                "turns": [], "attempts": {}, "feedback": None, "feedback_status": "none",
            }
            document["sessions"][identifier] = record
            return self.public(record)

        return self._change(change)

    def delete(self, identifier):
        def change(document):
            if not valid_id(identifier):
                raise LearningError("INVALID_REQUEST")
            document["sessions"].pop(identifier, None)
            return True

        return self._change(change)

    def claim_turn(self, identifier, turn_id, request_hash):
        if not valid_id(turn_id):
            raise LearningError("INVALID_REQUEST")

        def change(document):
            record = self._session(document, identifier)
            previous = record["attempts"].get(turn_id)
            if previous:
                if previous.get("request_hash") != request_hash:
                    raise LearningError("CONFLICT")
                if previous["state"] == "ready":
                    return {"claimed": False, "state": "pending", "turn": copy.deepcopy(previous["turn"])}
                if previous["state"] == "committed":
                    turn = next(turn for turn in record["turns"] if turn["id"] == turn_id)
                    return {"claimed": False, "state": "committed", "turn": copy.deepcopy(turn)}
                raise LearningError("CONFLICT")
            if record["status"] != "active" or any(
                value["state"] in ("running", "ready") for value in record["attempts"].values()
            ):
                raise LearningError("CONFLICT")
            if len(record["attempts"]) >= MAX_TURNS or len(record["turns"]) >= MAX_TURNS:
                raise LearningError("LIMIT_REACHED")
            # Reserve room before spending money; the final exact bound is checked too.
            if len(encoded(document)) + MAX_TURN_BYTES > MAX_ACCOUNT_BYTES:
                raise LearningError("LIMIT_REACHED")
            record["attempts"][turn_id] = {"state": "running", "request_hash": request_hash}
            record["updated_at"] = utc_now()
            history = []
            for turn in record["turns"][-5:]:
                history.extend(({"role": "user", "content": turn["user"]}, {"role": "assistant", "content": turn["reply"]}))
            previous_topic = record["turns"][-1]["settings"]["topic"] if record["turns"] else None
            return {"claimed": True, "history": history, "previous_topic": previous_topic}

        return self._change(change)

    def stage_turn(self, identifier, turn_id, turn):
        if not self._valid_turn(turn) or turn["id"] != turn_id:
            raise LearningError("INVALID_REQUEST")
        if len(encoded(turn)) > MAX_TURN_BYTES:
            raise LearningError("LIMIT_REACHED")

        def change(document):
            record = self._session(document, identifier)
            attempt = record["attempts"].get(turn_id)
            if record["status"] != "active" or not attempt or attempt["state"] != "running":
                raise LearningError("CONFLICT")
            attempt.update(state="ready", turn=copy.deepcopy(turn))
            record["updated_at"] = utc_now()
            return True

        return self._change(change)

    def fail_turn(self, identifier, turn_id):
        def change(document):
            record = self._session(document, identifier)
            attempt = record["attempts"].get(turn_id)
            if attempt and attempt["state"] == "running":
                attempt["state"] = "failed"
            return True

        return self._change(change)

    def commit_turn(self, identifier, turn_id):
        if not valid_id(turn_id):
            raise LearningError("INVALID_REQUEST")

        def change(document):
            record = self._session(document, identifier)
            attempt = record["attempts"].get(turn_id)
            if attempt and attempt["state"] == "committed":
                return self.public(record)
            if record["status"] != "active" or not attempt or attempt["state"] != "ready":
                raise LearningError("CONFLICT")
            turn = attempt.pop("turn")
            record["turns"].append(turn)
            attempt["state"] = "committed"
            if len(record["turns"]) == 1:
                record["title"] = turn["user"][:80]
            record["updated_at"] = utc_now()
            return self.public(record)

        return self._change(change)

    def cancel_turn(self, identifier, turn_id):
        if not valid_id(turn_id):
            raise LearningError("INVALID_REQUEST")

        def change(document):
            record = self._session(document, identifier)
            attempt = record["attempts"].get(turn_id)
            if attempt and attempt["state"] == "committed":
                # A cancellation arriving after atomic commit cannot erase a saved turn.
                return "committed"
            if attempt is None:
                if len(record["attempts"]) >= MAX_TURNS:
                    raise LearningError("LIMIT_REACHED")
                record["attempts"][turn_id] = {"state": "cancelled"}
            else:
                attempt.pop("turn", None)
                attempt["state"] = "cancelled"
            record["updated_at"] = utc_now()
            return "cancelled"

        return self._change(change)

    def claim_feedback(self, identifier):
        def change(document):
            record = self._session(document, identifier)
            if record["feedback_status"] != "none":
                return {"claimed": False, "session": self.public(record)}
            if not record["turns"] or any(
                attempt["state"] in ("running", "ready") for attempt in record["attempts"].values()
            ):
                raise LearningError("CONFLICT")
            if len(encoded(document)) + MAX_FEEDBACK_BYTES > MAX_ACCOUNT_BYTES:
                raise LearningError("LIMIT_REACHED")
            # Sticky claim: worker crash/storage outage cannot cause another paid attempt.
            record.update(status="finished", feedback_status="generating", updated_at=utc_now())
            return {"claimed": True, "session": self.public(record)}

        return self._change(change)

    def save_feedback(self, identifier, feedback):
        if len(encoded(feedback)) > MAX_FEEDBACK_BYTES:
            raise LearningError("LIMIT_REACHED")

        def change(document):
            record = self._session(document, identifier)
            if record["feedback_status"] != "generating":
                raise LearningError("CONFLICT")
            if not self._valid_feedback(feedback, record["turns"]):
                raise LearningError("INVALID_REQUEST")
            record.update(feedback=copy.deepcopy(feedback), feedback_status="complete", updated_at=utc_now())
            return self.public(record)

        return self._change(change)

    def fail_feedback(self, identifier):
        def change(document):
            record = self._session(document, identifier)
            if record["feedback_status"] == "generating":
                record.update(feedback_status="failed", updated_at=utc_now())
            return self.public(record)

        return self._change(change)
