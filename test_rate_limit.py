"""서버를 띄우지 않고 5회/분 제한을 검사합니다.

실행 방법: .venv\\Scripts\\python.exe test_rate_limit.py
스크립트는 테스트를 새 Python 프로세스로 분리합니다.
"""

import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


BASE_DIR = Path(__file__).resolve().parent
WORKER_FLAG = "--isolated-worker"
WORKER_MARKER = "ENGLISHTUTOR_RATE_TEST_WORKER"
MOCK_REPLY = "Hello! What would you like to talk about?"


class SafetyStop(BaseException):
    """server.py의 except Exception에도 삼켜지지 않는 안전 중단입니다."""


def is_worker():
    return sys.argv[1:] == [WORKER_FLAG] and os.environ.get(WORKER_MARKER) == "1"


def launch_fresh_process():
    # 실제 비밀번호, API Key, 프록시 설정 등을 자식 프로세스에 전달하지 않습니다.
    system_names = (
        "SystemRoot", "WINDIR", "TEMP", "TMP", "PATH", "PATHEXT",
        "COMSPEC", "SYSTEMDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
    )
    child_env = {name: os.environ[name] for name in system_names if name in os.environ}
    child_env.update({
        WORKER_MARKER: "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    try:
        completed = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), WORKER_FLAG],
            cwd=str(BASE_DIR),
            env=child_env,
            check=False,
            timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("FAIL: 새 테스트 프로세스를 시작하거나 완료할 수 없습니다.", flush=True)
        return 1
    return completed.returncode


class ChatRateLimitTest(unittest.TestCase):
    def test_six_requests(self):
        # unittest 모듈로 불러온 경우에도 실제 검사는 새 프로세스에서 합니다.
        if not is_worker():
            self.assertEqual(launch_fresh_process(), 0, "분리된 테스트가 실패했습니다.")
            return

        network_attempts = []

        def block_network(*_args, **_kwargs):
            network_attempts.append(True)
            raise SafetyStop("외부 통신 또는 서버 실행 시도가 감지되어 테스트를 중단합니다.")

        with ExitStack() as stack:
            # 서버를 import하기 전에 TCP 연결, DNS, 데이터 전송, 포트 개방을 막습니다.
            # 차단 설치에 실패하면 예외로 종료하며, 차단 없이 계속하지 않습니다.
            for name in ("connect", "connect_ex", "bind", "send", "sendall", "sendto"):
                stack.enter_context(patch.object(socket.socket, name, side_effect=block_network))
            stack.enter_context(patch("socket.create_connection", side_effect=block_network))
            stack.enter_context(patch("socket.getaddrinfo", side_effect=block_network))

            # 의존 라이브러리도 통신 차단이 설치된 뒤에 불러옵니다.
            import flask
            import httpx
            import openai
            from limits.storage import MemoryStorage
            from werkzeug.security import generate_password_hash

            stack.enter_context(patch.object(flask.Flask, "run", side_effect=block_network))
            stack.enter_context(patch.object(httpx.Client, "send", side_effect=block_network))
            stack.enter_context(patch.object(httpx.AsyncClient, "send", side_effect=block_network))

            # 실제 설정 대신 실행할 때마다 새 테스트 전용 비밀값을 만듭니다.
            test_env = {
                "APP_PASSWORD_HASH": generate_password_hash(secrets.token_urlsafe(24)),
                "FLASK_SECRET_KEY": secrets.token_hex(32),
                "OPENAI_API_KEY": "test-only-not-a-real-openai-key",
                "RENDER": "false",
            }
            stack.enter_context(patch.dict(os.environ, test_env, clear=True))

            # 실제 SDK처럼 with 구문을 지원하고 output_text 문자열을 반환합니다.
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

            # 새 프로세스라도 이미 import된 서버가 있다면 재사용하지 않고 중단합니다.
            if "server" in sys.modules:
                raise SafetyStop("기존 서버 모듈이 감지되어 테스트를 중단합니다.")
            spec = importlib.util.spec_from_file_location("server", BASE_DIR / "server.py")
            if spec is None or spec.loader is None:
                raise SafetyStop("서버를 안전하게 불러올 수 없어 테스트를 중단합니다.")
            server = importlib.util.module_from_spec(spec)
            stack.enter_context(patch.dict(sys.modules, {"server": server}))
            spec.loader.exec_module(server)

            # 서버가 다른 SDK 기호를 사용하도록 바뀌면 Mock 확인 단계에서 실패합니다.
            self.assertIs(server.OpenAI, mock_openai, "OpenAI Mock이 연결되지 않았습니다.")
            self.assertIsInstance(server.limiter.storage, MemoryStorage)
            server.app.config.update(TESTING=True)
            client = server.app.test_client()

            # 현재 로그인 구조에 맞춰 같은 클라이언트에 인증과 CSRF를 설정합니다.
            csrf_token = secrets.token_urlsafe(32)
            with client.session_transaction() as state:
                state.clear()
                state["authenticated"] = True
                state["csrf_token"] = csrf_token

            statuses = []
            mock_counts = []
            constructor_counts = []
            started = time.monotonic()
            for number in range(1, 7):
                # 중단점 등으로 한 분을 넘겼다면 잘못된 판정을 하지 않고 중단합니다.
                if time.monotonic() - started >= 60:
                    raise SafetyStop("검사 시간이 1분을 넘어 테스트를 중단합니다.")
                calls_before = mock_client.responses.create.call_count
                response = client.post(
                    "/api/chat",
                    json={"messages": [{"role": "user", "content": "Hello! I want to practice English."}]},
                    headers={"X-CSRF-Token": csrf_token},
                )
                print(f"요청 {number}: HTTP {response.status_code}", flush=True)
                statuses.append(response.status_code)
                mock_counts.append(mock_client.responses.create.call_count)
                constructor_counts.append(mock_openai.call_count)

                with self.subTest(request=number):
                    if number <= 5:
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.get_json(), {"reply": MOCK_REPLY, "correction": None})
                    else:
                        self.assertEqual(response.status_code, 429)
                        self.assertTrue(response.is_json)
                        self.assertIsInstance(response.get_json().get("error"), str)
                        self.assertEqual(mock_client.responses.create.call_count, calls_before)

            self.assertEqual(statuses, [200, 200, 200, 200, 200, 429])
            self.assertEqual(mock_counts, [1, 2, 3, 4, 5, 5])
            self.assertEqual(constructor_counts, [1, 2, 3, 4, 5, 5])
            self.assertEqual(network_attempts, [], "차단된 외부 통신 시도가 있었습니다.")
            print("OpenAI Mock 호출: 5회 (6번째 요청에서 증가 없음)", flush=True)


def main():
    if sys.argv[1:] == []:
        return launch_fresh_process()
    if not is_worker():
        print("FAIL: 테스트 시작 인자가 올바르지 않습니다.", flush=True)
        return 1

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChatRateLimitTest)
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite)
    print("PASS" if result.wasSuccessful() else "FAIL", flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
