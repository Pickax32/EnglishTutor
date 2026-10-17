"""Redis 장애 시 인증된 채팅 요청이 OpenAI에 도달하지 않는지 검증합니다.

실제 Upstash 서버나 외부 환경변수 값은 사용하지 않습니다.
APP_ENV=production과 테스트 전용 인증 설정을 내부에서 적용합니다.
Windows 시스템 환경변수는 clear=False로 보존합니다.

실제 Flask-Limiter와 RedisStorage는 유지하고 Redis 클라이언트만 Mock으로 만듭니다.
시작 시 PING은 성공시키고, 요청 처리 중 Lua 호출에서 ConnectionError를 주입합니다.
실제 Redis·OpenAI 통신과 서버 포트 개방은 별도의 차단 장치로 막습니다.
이 파일을 작성하거나 import하는 것만으로 테스트가 실행되지는 않습니다.
"""

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import importlib.util
import io
import logging
import os
from pathlib import Path
import secrets
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import warnings


BASE_DIR = Path(__file__).resolve().parent
# .invalid는 테스트용 가짜 주소입니다. 실제 URI·호스트·비밀번호가 아닙니다.
TEST_REDIS_URI = "rediss://redis-failure-test.invalid:6379/0"
MOCK_REPLY = "Hello! What would you like to talk about?"


class SafetyStop(BaseException):
    """서버가 일반 예외를 처리해도 우회할 수 없는 통신 차단입니다."""


def check(condition, label):
    if not condition:
        error = AssertionError()
        error.diagnostic_check = label
        raise error


def safe_failure(error, stage):
    """예외 메시지·소스·변수값 없이 종류와 위치만 표시합니다."""
    stage = getattr(error, "diagnostic_check", stage)
    location = "위치 정보 없음"
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code is not check.__code__:
            location = f"{Path(frame.tb_frame.f_code.co_filename).name}:{frame.tb_lineno}"
        frame = frame.tb_next
    kind = type(error)
    return f"실패 단계: {stage}\n예외 종류: {kind.__module__}.{kind.__qualname__}\n발생 위치: {location}"


class SafeTestResult(unittest.TextTestResult):
    def _exc_info_to_string(self, err, test):
        return getattr(test, "failure_report", None) or safe_failure(err[1], "unittest 준비 또는 실행")


class RedisFailureTest(unittest.TestCase):
    def test_redis_outage_blocks_openai(self):
        self.stage = "테스트 준비"
        self.failure_report = None
        self.http_status = None
        self.outage_calls = 0
        self.acquire_calls = 0
        self.openai_constructor_calls = 0
        self.openai_response_calls = 0
        self.exception_types = []
        previous_logging = logging.root.manager.disable
        try:
            logging.disable(logging.CRITICAL)
            with warnings.catch_warnings(), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                warnings.simplefilter("ignore")
                self.exercise_failure_path()
        except BaseException as error:
            self.failure_report = safe_failure(error, self.stage)
        finally:
            logging.disable(previous_logging)

        status = self.http_status if self.http_status is not None else "응답 없음"
        print(f"장애 시 HTTP 응답: {status}", flush=True)
        print(f"실제 저장소 메서드 호출: {self.acquire_calls}회", flush=True)
        print(f"Redis 장애 Mock 호출: {self.outage_calls}회", flush=True)
        for handled_kind, cause_kind in self.exception_types:
            print(f"처리된 예외 종류: {handled_kind}", flush=True)
            print(f"Redis 원인 예외 종류: {cause_kind}", flush=True)
        print(f"OpenAI 생성자 Mock 호출: {self.openai_constructor_calls}회", flush=True)
        print(f"OpenAI 응답 Mock 호출: {self.openai_response_calls}회", flush=True)
        if self.failure_report is not None:
            raise AssertionError(self.failure_report) from None

    def exercise_failure_path(self):
        self.stage = "기존 서버 모듈 확인"
        check("server" not in sys.modules, "기존 server 모듈이 없는 독립 테스트 환경")
        blocked_network = []

        def block_network(*_args, **_kwargs):
            blocked_network.append(True)
            raise SafetyStop

        with ExitStack() as stack:
            self.stage = "실제 통신·서버 실행 차단 설치"
            for name in ("connect", "connect_ex", "bind", "send", "sendall", "sendto"):
                stack.enter_context(patch.object(socket.socket, name, side_effect=block_network))
            stack.enter_context(patch("socket.create_connection", side_effect=block_network))
            stack.enter_context(patch("socket.getaddrinfo", side_effect=block_network))

            self.stage = "의존 라이브러리 로딩"
            import flask
            import httpx
            import openai
            import redis
            from limits.errors import StorageError
            from limits.storage import MemoryStorage, RedisStorage
            from limits.strategies import MovingWindowRateLimiter
            from werkzeug.security import generate_password_hash

            stack.enter_context(patch.object(flask.Flask, "run", side_effect=block_network))
            stack.enter_context(patch.object(httpx.Client, "send", side_effect=block_network))
            stack.enter_context(patch.object(httpx.AsyncClient, "send", side_effect=block_network))
            stack.enter_context(patch.object(openai, "AsyncOpenAI", side_effect=block_network))

            self.stage = "운영 모드·테스트 전용 환경변수 적용"
            test_env = {
                "APP_ENV": "production",
                "RATELIMIT_STORAGE_URI": TEST_REDIS_URI,
                "APP_PASSWORD_HASH": generate_password_hash(secrets.token_urlsafe(24)),
                "FLASK_SECRET_KEY": secrets.token_hex(32),
                "OPENAI_API_KEY": "test-only-not-a-real-openai-key",
                "RENDER": "false",
            }
            # 시스템 환경변수는 유지하며 앱 설정만 테스트 값으로 덮어씁니다.
            stack.enter_context(patch.dict(os.environ, test_env, clear=False))

            self.stage = "OpenAI Mock 및 Redis 클라이언트 Mock 설치"
            mock_client = MagicMock(spec_set=["responses", "__enter__", "__exit__"])
            mock_client.responses = MagicMock(spec_set=["create"])
            mock_client.__enter__.return_value = mock_client
            mock_client.__exit__.return_value = False
            mock_client.responses.create.return_value = SimpleNamespace(output_text=MOCK_REPLY)
            mock_openai = MagicMock(name="OpenAI", return_value=mock_client)
            stack.enter_context(patch.object(openai, "OpenAI", mock_openai))

            redis_client = MagicMock(spec_set=["ping", "register_script", "close"])
            redis_client.ping.return_value = True
            # 스크립트마다 별도 Mock을 만들어 한 경로에만 장애를 주입합니다.
            redis_client.register_script.side_effect = lambda _script: MagicMock(name="RedisLuaScript")
            redis_factory = stack.enter_context(patch.object(redis, "from_url", return_value=redis_client))

            self.stage = "실제 Flask 서버·Limiter·RedisStorage 로딩"
            spec = importlib.util.spec_from_file_location("server", BASE_DIR / "server.py")
            check(spec is not None and spec.loader is not None, "server.py 모듈 로더 존재")
            server = importlib.util.module_from_spec(spec)
            stack.enter_context(patch.dict(sys.modules, {"server": server}))
            spec.loader.exec_module(server)

            storage = server.limiter.storage
            check(server.app.config.get("APP_ENV") == "production", "APP_ENV production 조건")
            check(server.OpenAI is mock_openai, "서버 OpenAI Mock 연결")
            check(isinstance(storage, RedisStorage) and not isinstance(storage, MemoryStorage), "실제 RedisStorage 사용")
            check(isinstance(server.limiter.limiter, MovingWindowRateLimiter), "실제 moving-window 전략 사용")
            check(server.limiter.limiter.storage is storage, "Limiter와 장애 대상 저장소 일치")
            check(storage.wrap_exceptions is True, "Redis 예외의 StorageError 변환 활성")
            check(storage.get_connection() is redis_client, "Redis 클라이언트 Mock 연결")
            check(redis_factory.call_count == 1 and redis_client.ping.call_count >= 1, "시작 시 모의 Redis PING 성공")
            check(server.app.config.get("RATELIMIT_SWALLOW_ERRORS") is False, "Redis 오류 무시 비활성")
            check(server.app.config.get("RATELIMIT_IN_MEMORY_FALLBACK_ENABLED") is False, "메모리 대체 비활성")
            server.app.config.update(TESTING=True)

            self.stage = "Redis 요청 제한 경로에 ConnectionError 주입"
            fault = redis.exceptions.ConnectionError("Injected Redis connection failure for test")
            failing_script = stack.enter_context(patch.object(storage, "lua_acquire_moving_window", side_effect=fault))
            # 실제 저장소 메서드를 호출하므로 라이브러리의 예외 변환도 그대로 거칩니다.
            acquire_spy = stack.enter_context(patch.object(storage, "acquire_entry", wraps=storage.acquire_entry))
            original_handler = server.app.error_handler_spec[None][None][StorageError]
            handled_expected_fault = []

            def observe_storage_error(error):
                cause = error.storage_error
                self.exception_types.append((
                    f"{type(error).__module__}.{type(error).__qualname__}",
                    f"{type(cause).__module__}.{type(cause).__qualname__}",
                ))
                handled_expected_fault.append(isinstance(error, StorageError) and cause is fault)
                # 응답은 기존에 등록된 서버 핸들러가 직접 만듭니다.
                return original_handler(error)

            server.app.register_error_handler(StorageError, observe_storage_error)

            self.stage = "인증된 테스트 클라이언트·CSRF 준비"
            client = server.app.test_client()
            csrf_token = secrets.token_urlsafe(32)
            with client.session_transaction() as state:
                state.clear()
                state["authenticated"] = True
                state["csrf_token"] = csrf_token

            self.stage = "Redis 장애가 있는 인증된 POST /api/chat 요청"
            try:
                response = client.post(
                    "/api/chat",
                    json={"messages": [{"role": "user", "content": "Hello! I want to practice English."}]},
                    headers={"X-CSRF-Token": csrf_token},
                )
                self.http_status = response.status_code
            finally:
                self.outage_calls = failing_script.call_count
                self.acquire_calls = acquire_spy.call_count
                self.openai_constructor_calls = mock_openai.call_count
                self.openai_response_calls = mock_client.responses.create.call_count

            self.stage = "장애 발생·HTTP 차단·OpenAI 미호출 검증"
            check(self.acquire_calls == 1, "실제 RedisStorage.acquire_entry 호출 1회")
            check(self.outage_calls == 1, "Redis Lua 경로에서 연결 장애 실제 발생 1회")
            check(handled_expected_fault == [True], "주입한 ConnectionError가 StorageError로 변환되어 처리됨")
            check(response.status_code == 503, "Redis 장애 시 HTTP 503 반환 (HTTP 200은 실패)")
            payload = response.get_json(silent=True)
            check(isinstance(payload, dict) and isinstance(payload.get("error"), str), "안전한 JSON 오류 응답")
            check(self.openai_constructor_calls == 0, "OpenAI 생성자 Mock 호출 0회")
            check(self.openai_response_calls == 0, "OpenAI 응답 Mock 호출 0회")
            check(not blocked_network, "실제 외부 통신·서버 실행 시도 없음")
            redis_client.close()


def main():
    try:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(RedisFailureTest)
        result = unittest.TextTestRunner(
            stream=sys.stdout,
            verbosity=2,
            resultclass=SafeTestResult,
        ).run(suite)
        print("PASS" if result.wasSuccessful() else "FAIL", flush=True)
        return 0 if result.wasSuccessful() else 1
    except BaseException as error:
        print(safe_failure(error, "테스트 실행기 준비 또는 종료"), flush=True)
        print("FAIL", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
