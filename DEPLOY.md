# EnglishTutor의 Render 실행 설정

Render의 Linux 환경에서 Gunicorn으로 Flask 앱을 실행하기 위한 안내입니다.
이 문서를 추가하는 작업으로 GitHub 업로드나 Render 배포가 실행되지는 않습니다.

## Render Web Service 설정

| 항목 | 설정 |
| --- | --- |
| Language / Runtime | Python 3 |
| Root Directory | `server.py`와 `requirements.txt`가 있는 폴더. 저장소 최상위에 있다면 비워 둡니다. |
| Health Check Path | `/api/health` |

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
gunicorn --workers 1 --bind 0.0.0.0:$PORT server:app
```

Build Command는 필요한 라이브러리를 설치합니다. Render의 Flask 앱 설정 방식은
[Render 공식 가이드](https://render.com/docs/deploy-flask)를 참고하세요.

Gunicorn은 브라우저의 HTTP 요청을 받아 Flask 앱에 전달하는 운영용 WSGI 서버입니다.
`server:app`은 `server.py` 모듈의 `app` 객체를 불러오라는 뜻입니다.
현재 `app`은 모듈의 전역 변수이며, `app.run()`은
`if __name__ == "__main__":` 안에 있어 import할 때 실행되지 않습니다.
[Flask의 Gunicorn 설명](https://flask.palletsprojects.com/en/stable/deploying/gunicorn/)

`--workers 1`은 요청을 처리하는 작업 프로세스를 하나 사용한다는 뜻입니다.
`--bind 0.0.0.0:$PORT`는 Render가 전달하는 요청을 지정된 포트에서 받도록 합니다.
`$PORT`는 Render의 Linux 셸이 환경변수 값으로 바꾸므로 그대로 입력합니다.
[Render 포트 설정](https://render.com/docs/web-services#port-binding)

## 환경변수

Render 서비스의 Environment 설정에 다음 이름으로 값을 등록합니다.
실제 값은 코드, 이 문서, HTML, JavaScript, 로그에 작성하지 않습니다.

| 이름 | 설정 주체 | 용도 |
| --- | --- | --- |
| `APP_PASSWORD_HASH` | 사용자가 등록 | Werkzeug의 `generate_password_hash`로 만든 로그인 비밀번호 해시. 비밀번호 원문을 넣지 않습니다. |
| `FLASK_SECRET_KEY` | 사용자가 등록 | 충분히 길고 예측하기 어려운 세션 서명키. 재시작 후에도 같은 값을 사용하며, 변경하면 기존 로그인 세션이 무효화됩니다. |
| `OPENAI_API_KEY` | 사용자가 등록 | 서버에서 OpenAI API를 호출할 때 사용하는 키. 브라우저에 전달하지 않습니다. |
| `APP_ENV` | 사용자가 등록 | Render에서는 반드시 `production`. 로컬에서는 미설정 시 `development`이며, 두 값 이외에는 시작 오류가 발생합니다. |
| `RATELIMIT_STORAGE_URI` | 사용자가 등록 | `production`에서 필수인 외부 Redis 연결 주소. `redis://` 또는 TLS를 사용하는 `rediss://` 형식을 사용하며, 주소와 인증정보를 비밀값으로 관리합니다. |
| `PORT` | Render가 제공 | Start Command가 바인딩할 포트 번호. Windows 로컬 실행의 5000번 포트와 별개입니다. |
| `RENDER` | Render가 제공 | `true`이면 세션 쿠키에 `Secure=True`를 적용합니다. 이때 `APP_ENV=production`이 아니면 시작을 중단합니다. |

`APP_PASSWORD_HASH` 또는 `FLASK_SECRET_KEY`가 없거나 공백이면 서버를 import하는
단계에서 설정 오류로 시작을 중단합니다. `APP_PASSWORD_HASH`에는 지원되는
Werkzeug 해시 형식이 필요합니다. `OPENAI_API_KEY`는 실제 AI 대화에 필요합니다.

Render가 제공하는 변수는 [기본 환경변수 문서](https://render.com/docs/environment-variables)에
설명되어 있습니다. `.env` 파일을 만드는 것만으로 Gunicorn의 환경변수가 자동 설정되지는
않습니다. `.env`, `.env.*`, `.venv/`는 기존 `.gitignore`의 제외 설정을 유지합니다.

## 로컬과 운영 환경의 Rate Limiting

Windows 로컬에서는 `APP_ENV`를 설정하지 않거나 `development`로 설정하면 기존
`memory://` 저장소를 사용합니다. 이 모드에서는 `RATELIMIT_STORAGE_URI`를 사용하지
않습니다. 로컬 저장 기록은 프로세스를 종료하면 사라지며, 여러 프로세스가 공유하지
않으므로 운영 보호용으로 사용할 수 없습니다. 기존 `test_rate_limit.py`는 변경하지
않았으며, 로컬 개발 모드에서 기존 방식으로 사용할 수 있습니다.

Render에서는 다음 두 환경변수를 서비스의 Environment 설정에 등록합니다.

- `APP_ENV=production`
- `RATELIMIT_STORAGE_URI`: 사용할 외부 Redis의 실제 연결 주소. 코드나 문서에 기록하지 않습니다.

먼저 외부 Redis를 준비하고 연결 주소를 등록해야 운영 서버를 시작할 수 있습니다.
이 코드 변경으로 Redis 서비스가 자동 생성되거나 실제 연결 정보가 설정되지는 않습니다.

운영 모드는 Redis 설정이 없거나 잘못되면 `memory://`로 대체하지 않습니다.
서버를 import할 때 주소 형식을 검사하고 Redis에 연결해 `PING`으로 응답을 확인합니다.
연결·인증·TLS 검증이 실패하면 주소, 비밀번호, 내부 오류를 노출하지 않는 설정 오류로
시작을 중단합니다. `RENDER=true`에서 `APP_ENV`를 빠뜨린 경우에도 시작되지 않습니다.
`PING` 검사는 연결 확인용입니다. Redis 계정에는 moving-window 제한에 필요한 Lua 스크립트와
키 명령 권한도 있어야 하며, 해당 권한이 부족하면 요청 검사에서 HTTP 503으로 차단됩니다.

Redis 주소에는 `redis://` 또는 `rediss://` 스킴, 호스트, 선택적인 포트와 숫자 DB 경로만
허용합니다. 쿼리 문자열과 fragment는 허용하지 않습니다. URL 옵션으로 TLS 검증이나
연결 제한 시간을 덮어쓰는 것을 막기 위한 검사입니다. 인증이 필요하면 Redis 제공업체의
안내에 따라 인증정보가 포함된 주소를 환경변수로만 등록합니다.

공용 네트워크나 외부 Redis 연결에는 **`rediss://`를 권장합니다.** 서버는 TLS 인증서와
호스트 이름을 검증하므로 검증을 끄는 설정을 추가하지 않습니다. `requirements.txt`의
`redis` 패키지가 연결을 담당하며, TLS에는 Python 표준 라이브러리를 사용하므로 별도
TLS 추가 패키지는 필요하지 않습니다.

실행 중 Redis 요청 제한 검사에 실패하면 로그인·채팅 처리 전에 안전한 JSON 오류와
HTTP 503을 반환합니다. 오류를 무시하고 OpenAI API 호출로 진행하지 않습니다. 정상적으로
제한 횟수를 초과한 경우의 HTTP 429 응답은 그대로 유지합니다. 운영에서는 응답 헤더를
만들기 위한 추가 Redis 조회를 피하도록 자동 Rate Limit 헤더를 끕니다. 로컬 헤더와
요청 제한 수치에는 변화가 없습니다.

요청 제한은 로그인 계정 전체에 채팅 **분당 5회·시간당 30회**, 로그인 시도에는
IP별 **15분당 5회**입니다. 재로그인해도 공용 계정의 채팅 한도가 초기화되지 않습니다.
미인증 채팅 요청의 HTTP 401과 `/api/health`의 제한 제외도 유지합니다.

모든 Gunicorn worker와 Render 인스턴스는 같은 Redis 저장소를 사용해야 같은 요청 횟수를
공유합니다. 앱만 재시작하면 Redis에 남은 기록은 유지되지만, Redis의 재시작·데이터 유실·
메모리 부족에 따른 데이터 제거 정책은 기록을 초기화할 수 있습니다. Redis의 영속성,
가용성, 용량과 장애 감시를 함께 준비해야 합니다. 다른 앱은 별도 Redis DB를 사용해
제한 기록이 섞이지 않게 합니다. 키에는 `EnglishTutor` 접두사를 사용하므로 같은 앱의 운영·
테스트 서버도 저장소를 분리해야 합니다. **worker 1개나 Redis 연결만으로 운영 보안이 완성되지는
않습니다.**

저장소 설정은 [Flask-Limiter 공식 문서](https://flask-limiter.readthedocs.io/en/stable/configuration.html#ratelimit-storage-uri),
TLS 연결은 [limits Redis SSL 문서](https://limits.readthedocs.io/en/stable/storage.html#redis-ssl-storage)를 참고하세요.

현재 로그인 시도 제한은 `request.remote_addr`를 기준으로 하며,
`X-Forwarded-For` 등의 프록시 헤더를 무조건 신뢰하지 않습니다.
Render 배포 전 실제 프록시 경로와 신뢰할 프록시의 범위를 검토해야 합니다.
검토 없이 헤더를 신뢰하면 IP 제한을 우회할 수 있고, 현재 설정을 그대로 사용하면
여러 사용자가 프록시 IP 하나의 한도를 공유할 수 있습니다.

## Windows 로컬 실행

기존처럼 필요한 환경변수를 먼저 설정한 뒤 프로젝트 폴더에서 실행합니다.
로컬 메모리 제한을 사용하려면 `APP_ENV`를 미설정 상태나 `development`로 유지하고,
Render의 환경변수를 그대로 복사하지 않습니다.

```powershell
.\.venv\Scripts\python.exe .\server.py
```

로컬 주소는 `127.0.0.1:5000`이고 `debug=False`를 유지합니다.
Gunicorn은 Windows에서 실행하지 않습니다. 위의 Gunicorn Start Command는
Render의 Linux 환경을 위한 명령입니다.

## 배포 후 확인할 동작

- `GET /api/health`: 로그인 없이 정상 JSON 응답. 앱 프로세스의 생존 여부를 확인하며,
  Redis의 연결 상태를 보장하는 준비 상태 검사는 아닙니다.
- 로그인하지 않은 `/`, `/index.html`: 로그인 화면으로 이동.
- 로그인하지 않은 `POST /api/chat`: HTTP 401.
- 로그인 후 대화: 기존 모델, 최근 5쌍의 대화 이력과 요청 횟수 제한 적용.
- 제한 횟수 초과: HTTP 429. Redis 제한 검사 장애: 안전한 JSON 오류와 HTTP 503이며,
  OpenAI API를 호출하지 않습니다.

이번 준비 작업에서는 실제 비밀값을 설정하지 않고, Gunicorn 실행이나 배포를 수행하지 않습니다.
