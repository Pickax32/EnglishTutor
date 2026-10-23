"""실제 Redis를 사용하는 /api/chat 통합 테스트입니다. 작성만으로 실행되지 않습니다.

필수 환경변수: APP_ENV=production, RATELIMIT_STORAGE_URI.
Upstash의 Redis TCP/TLS 연결 URI를 사용하며 REST URL/토큰은 사용하지 않습니다.
로그인 해시·세션 키·가짜 OpenAI 키는 테스트 내부에서만 생성합니다.

Redis 저장소와 Lua 제한 검사는 실제로 실행하며 OpenAI만 Mock으로 대체합니다.
테스트마다 고유 접두사를 사용하고 기록한 키만 finally에서 정리합니다.
강제 종료나 Redis 장애로 정리하지 못한 키는 기존 제한의 TTL로 만료됩니다.
키 형식은 현재 moving-window 구현을 기준으로 검증합니다.
참고: https://limits.readthedocs.io/en/stable/api.html#limits.storage.RedisStorage
"""

from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit
import warnings


BASE_DIR = Path(__file__).resolve().parent
MOCK_REPLY = "Hello! What would you like to talk about?"


class SafetyStop(BaseException):
    """서버의 except Exception을 통과하여 테스트를 중단합니다."""


def require(condition, check):
    # Redis 객체나 URI의 repr가 unittest 실패 메시지에 들어가지 않게 합니다.
    if not condition:
        error = SafetyStop()
        error.diagnostic_check = check
        raise error


class SafeDiagnostics:
    """메시지·소스·지역변수 없이 예외 종류와 위치만 보관합니다."""

    def __init__(self):
        self.stage = "테스트 준비"
        self.failures = []
        self.environment_checks = {}

    def record(self, error, stage=None):
        stage = stage or self.stage
        if isinstance(error, SafetyStop):
            stage = getattr(error, "diagnostic_check", stage)
        details = []
        seen = set()
        current = error
        for _ in range(6):
            if current is None or id(current) in seen:
                break
            seen.add(id(current))
            location = "위치 정보 없음"
            frame = current.__traceback__
            while frame is not None:
                # require 내부의 raise 대신 실제 검사 호출 줄을 표시합니다.
                if frame.tb_frame.f_code is not require.__code__:
                    filename = Path(frame.tb_frame.f_code.co_filename).name
                    location = f"{filename}:{frame.tb_lineno}"
                frame = frame.tb_next
            kind = type(current)
            details.append((f"{kind.__module__}.{kind.__qualname__}", location))
            # from None으로 숨긴 원인도 종류와 위치만 확인합니다.
            current = current.__cause__ if current.__cause__ is not None else current.__context__
        entry = (stage, tuple(details))
        if entry not in self.failures:
            self.failures.append(entry)

    @contextmanager
    def step(self, stage):
        previous = self.stage
        self.stage = stage
        try:
            yield
        except BaseException as error:
            self.record(error)
            raise
        finally:
            self.stage = previous

    def render(self):
        lines = []
        for number, (stage, details) in enumerate(self.failures, start=1):
            lines.append(f"실패 단계 {number}: {stage}")
            for index, (kind, location) in enumerate(details):
                label = "예외" if index == 0 else "원인/관련 예외"
                lines.append(f"  {label} 종류: {kind}")
                lines.append(f"  발생 위치: {location}")
        for name, passed in self.environment_checks.items():
            lines.append(f"환경변수 {name}: {'PASS' if passed else 'FAIL'}")
        return "\n".join(lines)


class SafeTestResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        diagnostics = getattr(test, "diagnostics", None)
        if diagnostics is None:
            diagnostics = SafeDiagnostics()
        if not diagnostics.failures:
            diagnostics.record(err[1], "unittest 준비 또는 실행")
        return diagnostics.render()


class RedisRateLimitTest(unittest.TestCase):
    def test_six_authenticated_requests(self):
        self.diagnostics = SafeDiagnostics()
        self.statuses = []
        self.cleanup_failed = False
        failure = False
        previous_logging = logging.root.manager.disable
        try:
            # 라이브러리의 출력·로그·예외 상세를 터미널에 전달하지 않습니다.
            logging.disable(logging.CRITICAL)
            with warnings.catch_warnings(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                warnings.simplefilter("ignore")
                self.exercise_real_redis()
        except BaseException as error:
            failure = True
            if not self.diagnostics.failures:
                self.diagnostics.record(error)
        finally:
            logging.disable(previous_logging)

        # 출력은 직접 만든 상태 코드와 고정된 메시지만 허용합니다.
        for number, status in enumerate(self.statuses, start=1):
            print(f"요청 {number}: HTTP {status}", flush=True)
        if self.cleanup_failed:
            print("테스트 키 정리: FAIL (남은 키는 기존 TTL에 따라 만료됩니다.)", flush=True)
        if failure:
            # 다른 unittest 실행기로 불러와도 안전한 진단만 전달합니다.
            raise AssertionError(self.diagnostics.render()) from None
        print("실제 Redis 저장·한도 검사: PASS", flush=True)
        print("OpenAI Mock 호출: 5회 (6번째 요청에서 증가 없음)", flush=True)
        print("테스트 키 정리: PASS", flush=True)

    def exercise_real_redis(self):
        diagnostics = self.diagnostics
        diagnostics.stage = "외부 환경변수 사전 검사"
        diagnostics.environment_checks.update({
            "APP_ENV 설정 여부": bool(os.environ.get("APP_ENV", "").strip()),
            "APP_ENV production 조건": os.environ.get("APP_ENV", "").strip().lower() == "production",
            "RATELIMIT_STORAGE_URI 설정 여부": bool(os.environ.get("RATELIMIT_STORAGE_URI", "").strip()),
        })
        require(os.environ.get("APP_ENV", "").strip().lower() == "production", "APP_ENV production 조건")
        diagnostics.stage = "Redis URI 읽기 및 구문 해석"
        storage_uri = os.environ.get("RATELIMIT_STORAGE_URI", "").strip()
        parsed = urlsplit(storage_uri)
        require(parsed.scheme in ("redis", "rediss") and bool(parsed.hostname), "Redis URI 스킴·호스트 검사")
        require(not parsed.query and not parsed.fragment, "Redis URI query·fragment 제외 검사")
        require("server" not in sys.modules, "기존 server 모듈 로딩 여부 검사")

        diagnostics.stage = "테스트 전용 키 접두사 생성"
        prefix = "EnglishTutor:redis-integration:" + secrets.token_hex(24)
        owned_prefix = prefix + ":"
        touched_keys = set()
        limit_keys = {}
        blocked_calls = []
        observer = None
        storage = None

        def block_openai_http(*_args, **_kwargs):
            blocked_calls.append(True)
            error = SafetyStop()
            error.diagnostic_check = "실제 HTTP 통신 또는 서버 실행 차단"
            raise error

        with ExitStack() as stack:
            diagnostics.stage = "의존성 로딩: flask"
            import flask
            diagnostics.stage = "의존성 로딩: flask_limiter"
            import flask_limiter
            diagnostics.stage = "의존성 로딩: httpx"
            import httpx
            diagnostics.stage = "의존성 로딩: openai"
            import openai
            diagnostics.stage = "의존성 로딩: redis"
            import redis
            diagnostics.stage = "의존성 로딩: limits.storage"
            from limits.storage import MemoryStorage, RedisStorage
            diagnostics.stage = "의존성 로딩: werkzeug.security"
            from werkzeug.security import generate_password_hash

            # Redis의 TCP/TLS 통신은 허용하고 SDK의 HTTP 통신은 먼저 차단합니다.
            diagnostics.stage = "HTTP 통신·서버 실행 차단 설치"
            stack.enter_context(patch.object(httpx.Client, "send", side_effect=block_openai_http))
            stack.enter_context(patch.object(httpx.AsyncClient, "send", side_effect=block_openai_http))
            stack.enter_context(patch.object(flask.Flask, "run", side_effect=block_openai_http))
            stack.enter_context(patch.object(openai, "AsyncOpenAI", side_effect=block_openai_http))

            diagnostics.stage = "OpenAI Mock 구성 및 설치"
            mock_client = MagicMock(spec_set=["responses", "__enter__", "__exit__"])
            mock_client.responses = MagicMock(spec_set=["create"])
            mock_client.__enter__.return_value = mock_client
            mock_client.__exit__.return_value = False
            mock_client.responses.create.return_value = SimpleNamespace(
                status="completed", output=[],
                output_text=json.dumps({"reply": MOCK_REPLY, "correction": None}),
            )
            mock_openai = MagicMock(name="OpenAI", return_value=mock_client)
            stack.enter_context(patch.object(openai, "OpenAI", mock_openai))

            # 실제 API 키·로그인 설정을 이어받지 않고 Redis URI만 사용합니다.
            diagnostics.stage = "테스트 전용 인증 환경변수 생성 및 적용"
            test_env = {
                "APP_ENV": "production",
                "RATELIMIT_STORAGE_URI": storage_uri,
                "APP_PASSWORD_HASH": generate_password_hash(secrets.token_urlsafe(24)),
                "FLASK_SECRET_KEY": secrets.token_hex(32),
                "OPENAI_API_KEY": "test-only-not-a-real-openai-key",
                "RENDER": "false",
            }
            stack.enter_context(patch.dict(os.environ, test_env, clear=False))
            diagnostics.environment_checks.update({
                "APP_PASSWORD_HASH 테스트 설정 여부": bool(os.environ.get("APP_PASSWORD_HASH")),
                "FLASK_SECRET_KEY 테스트 설정 여부": bool(os.environ.get("FLASK_SECRET_KEY")),
                "OPENAI_API_KEY 테스트 설정 여부": bool(os.environ.get("OPENAI_API_KEY")),
            })

            diagnostics.stage = "테스트 키 접두사 주입 준비"
            real_limiter = flask_limiter.Limiter

            def isolated_limiter(*args, **kwargs):
                # 실제 Limiter를 만들고 Redis 키 접두사만 테스트 전용으로 바꿉니다.
                # 제한 수치·strategy·URI·인증 처리는 변경하지 않습니다.
                with diagnostics.step("Flask-Limiter 초기화 및 테스트 접두사 주입"):
                    options = dict(kwargs.get("storage_options") or {})
                    options["key_prefix"] = prefix
                    kwargs["storage_options"] = options
                    return real_limiter(*args, **kwargs)

            stack.enter_context(patch.object(flask_limiter, "Limiter", new=isolated_limiter))
            real_acquire = RedisStorage.acquire_entry

            def tracked_acquire(current_storage, key, limit, expiry, amount=1):
                # 실제 Lua 호출을 그대로 실행하면서 정확한 키만 기록합니다.
                with diagnostics.step("실제 Redis Lua 요청 제한 검사"):
                    require(current_storage.key_prefix == prefix, "Redis 저장소의 테스트 접두사 검사")
                    full_key = current_storage.prefixed_key(key)
                    require(isinstance(full_key, str) and full_key.startswith(owned_prefix), "Redis 키의 테스트 범위 검사")
                    touched_keys.add(full_key)
                    limit_keys[(limit, expiry)] = full_key
                    return real_acquire(current_storage, key, limit, expiry, amount=amount)

            stack.enter_context(patch.object(RedisStorage, "acquire_entry", new=tracked_acquire))

            # 독립된 연결로 Redis 데이터를 읽어 메모리 저장소가 아님을 검증합니다.
            diagnostics.stage = "관찰용 Redis 연결 옵션 구성"
            observer_options = {"socket_connect_timeout": 3, "socket_timeout": 3}
            if parsed.scheme == "rediss":
                observer_options.update(ssl_cert_reqs="required", ssl_check_hostname=True)

            try:
                diagnostics.stage = "관찰용 Redis 클라이언트 생성"
                observer = redis.Redis.from_url(storage_uri, **observer_options)
                diagnostics.stage = "관찰용 Redis PING 연결·인증·TLS 확인"
                require(observer.ping() is True, "관찰용 Redis PING 결과 검사")

                diagnostics.stage = "server.py 모듈 로더 준비"
                spec = importlib.util.spec_from_file_location("server", BASE_DIR / "server.py")
                require(spec is not None and spec.loader is not None, "server.py 모듈 로더 검사")
                server = importlib.util.module_from_spec(spec)
                stack.enter_context(patch.dict(sys.modules, {"server": server}))
                diagnostics.stage = "server.py 로딩 및 운영 설정·Redis 초기화"
                spec.loader.exec_module(server)

                diagnostics.stage = "서버 Mock·production·오류 처리 설정 검사"
                require(server.OpenAI is mock_openai, "서버 OpenAI Mock 연결 검사")
                require(server.app.config.get("APP_ENV") == "production", "서버 APP_ENV production 조건")
                require(server.app.config.get("RATELIMIT_STORAGE_URI") == storage_uri, "서버 Redis URI 일치 조건")
                require(server.app.config.get("RATELIMIT_SWALLOW_ERRORS") is False, "Redis 오류 무시 비활성 검사")
                require(server.app.config.get("RATELIMIT_IN_MEMORY_FALLBACK_ENABLED") is False, "메모리 대체 비활성 검사")
                diagnostics.stage = "서버 Redis 저장소 및 연결 검사"
                storage = server.limiter.storage
                require(isinstance(storage, RedisStorage) and not isinstance(storage, MemoryStorage), "실제 RedisStorage 유형 검사")
                require(storage.key_prefix == prefix and storage.check(), "Redis 접두사·연결 검사 결과")
                server.app.config.update(TESTING=True)

                diagnostics.stage = "Flask 테스트 클라이언트·인증 세션·CSRF 준비"
                client = server.app.test_client()
                csrf_token = secrets.token_urlsafe(32)
                with client.session_transaction() as state:
                    state.clear()
                    state["authenticated"] = True
                    state["csrf_token"] = csrf_token

                started = time.monotonic()
                counts = []
                constructor_counts = []
                for number in range(1, 7):
                    diagnostics.stage = f"채팅 요청 {number}: 요청 전 시간 검사"
                    require(time.monotonic() - started < 60, f"요청 {number}: 전송 전 60초 이내 조건")
                    diagnostics.stage = f"채팅 요청 {number}: Flask 처리"
                    response = client.post(
                        "/api/chat",
                        json={"messages": [{"role": "user", "content": "Hello! I want to practice English."}]},
                        headers={"X-CSRF-Token": csrf_token},
                    )
                    self.statuses.append(response.status_code)
                    diagnostics.stage = f"채팅 요청 {number}: 응답 검증"
                    require(time.monotonic() - started < 60, f"요청 {number}: 응답 후 60초 이내 조건")
                    counts.append(mock_client.responses.create.call_count)
                    constructor_counts.append(mock_openai.call_count)
                    if number <= 5:
                        require(response.status_code == 200, f"요청 {number}: HTTP 200 조건")
                        require(response.get_json(silent=True) == {"reply": MOCK_REPLY, "correction": None}, f"요청 {number}: Mock 답변 JSON 조건")
                    else:
                        require(response.status_code == 429, "요청 6: HTTP 429 조건")
                        error = response.get_json(silent=True)
                        require(isinstance(error, dict) and isinstance(error.get("error"), str), "요청 6: 안전한 오류 JSON 조건")

                    if number == 5:
                        # 분·시간 제한이 실제 Redis에 5건씩 저장됐는지 확인합니다.
                        diagnostics.stage = "5회 요청 후 실제 Redis 저장 기록 검사"
                        require(set(limit_keys) == {(5, 60), (30, 3600)}, "분당·시간당 제한 키 구성 검사")
                        require(len(touched_keys) == 2, "테스트 Redis 키 개수 검사")
                        for full_key in touched_keys:
                            diagnostics.stage = "Redis 저장 키 자료형 조회"
                            require(observer.type(full_key) in (b"list", "list"), "Redis LIST 자료형 검사")
                            diagnostics.stage = "Redis 저장 요청 횟수 조회"
                            require(observer.llen(full_key) == 5, "Redis 저장 요청 횟수 5회 조건")
                            diagnostics.stage = "Redis 저장 키 만료 시간 조회"
                            require(observer.ttl(full_key) > 0, "Redis 키 TTL 양수 조건")

                diagnostics.stage = "전체 응답 및 Mock 호출 횟수 최종 검사"
                require(self.statuses == [200, 200, 200, 200, 200, 429], "6회 HTTP 상태 코드 순서 검사")
                require(counts == [1, 2, 3, 4, 5, 5], "OpenAI 응답 Mock 호출 횟수 검사")
                require(constructor_counts == [1, 2, 3, 4, 5, 5], "OpenAI 생성자 Mock 호출 횟수 검사")
                diagnostics.stage = "차단 후 Redis 요청 횟수 재확인"
                require(observer.llen(limit_keys[(5, 60)]) == 5, "6번째 요청 후 Redis 분당 기록 5회 조건")
                require(not blocked_calls, "실제 HTTP 통신·서버 실행 차단 발생 여부 검사")
            except BaseException as error:
                # finally 정리 오류가 최초 실패를 덮기 전에 안전한 메타데이터만 저장합니다.
                diagnostics.record(error)
                raise
            finally:
                # 전체 초기화·키 검색 없이 이번 실행이 기록한 키만 삭제합니다.
                try:
                    diagnostics.stage = "테스트 Redis 키 정리 범위 검사"
                    require(all(key.startswith(owned_prefix) for key in touched_keys), "정리할 키의 테스트 접두사 검사")
                    if touched_keys:
                        require(observer is not None, "키 정리용 Redis 클라이언트 존재 검사")
                        exact_keys = sorted(touched_keys)
                        diagnostics.stage = "테스트 Redis 키 삭제"
                        observer.delete(*exact_keys)
                        diagnostics.stage = "테스트 Redis 키 삭제 결과 확인"
                        require(observer.exists(*exact_keys) == 0, "테스트 Redis 키 삭제 완료 조건")
                except BaseException as error:
                    self.cleanup_failed = True
                    diagnostics.record(error)
                finally:
                    diagnostics.stage = "관찰용·서버 Redis 연결 종료"
                    with diagnostics.step("관찰용·서버 Redis 연결 종료"):
                        for connection in (observer, storage.get_connection() if storage is not None else None):
                            if connection is not None:
                                try:
                                    connection.close()
                                except Exception as error:
                                    self.cleanup_failed = True
                                    diagnostics.record(error)
                require(not self.cleanup_failed, "테스트 정리 완료 조건")


def main():
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RedisRateLimitTest)
        result = unittest.TextTestRunner(
            stream=sys.stdout,
            verbosity=2,
            resultclass=SafeTestResult,
        ).run(suite)
        print("PASS" if result.wasSuccessful() else "FAIL", flush=True)
        return 0 if result.wasSuccessful() else 1
    except BaseException as error:
        diagnostics = SafeDiagnostics()
        diagnostics.record(error, "테스트 실행기 준비 또는 종료")
        print(diagnostics.render(), flush=True)
        print("FAIL", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
