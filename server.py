import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from limits.errors import StorageError
from openai import OpenAI
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.security import check_password_hash

BASE_DIR = Path(__file__).resolve().parent


def load_auth_config():
    # 비밀값은 환경변수에서만 읽습니다. 오류에는 이름만 표시합니다.
    values = {name: os.environ.get(name) for name in ("APP_PASSWORD_HASH", "FLASK_SECRET_KEY")}
    missing = [name for name, value in values.items() if not value or not value.strip()]
    if missing:
        raise RuntimeError("로그인 설정 오류: 환경변수가 필요합니다: " + ", ".join(missing))

    password_hash = values["APP_PASSWORD_HASH"]
    if len(password_hash.split("$")) != 3 or not all(password_hash.split("$")):
        raise RuntimeError("APP_PASSWORD_HASH 설정 오류: Werkzeug로 생성한 비밀번호 해시가 필요합니다.")
    try:
        check_password_hash(password_hash, "")
    except (ValueError, TypeError):
        raise RuntimeError("APP_PASSWORD_HASH 설정 오류: 지원하는 비밀번호 해시 형식이 아닙니다.") from None

    return {
        "APP_PASSWORD_HASH": password_hash,
        "SECRET_KEY": values["FLASK_SECRET_KEY"],
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        # Render는 RENDER=true를 제공합니다. 로컬 HTTP에서는 Secure를 끕니다.
        "SESSION_COOKIE_SECURE": os.environ.get("RENDER", "").lower() == "true",
    }


def load_rate_limit_config():
    app_env = os.environ.get("APP_ENV", "development").strip().lower()
    if app_env not in ("development", "production"):
        raise RuntimeError("APP_ENV 설정 오류: development 또는 production이 필요합니다.")
    if os.environ.get("RENDER", "").lower() == "true" and app_env != "production":
        raise RuntimeError("Render에서는 APP_ENV=production 설정이 필요합니다.")

    config = {
        "APP_ENV": app_env,
        "RATELIMIT_ENABLED": True,
        "RATELIMIT_SWALLOW_ERRORS": False,
        "RATELIMIT_IN_MEMORY_FALLBACK_ENABLED": False,
        "RATELIMIT_IN_MEMORY_FALLBACK": [],
        "RATELIMIT_STORAGE_URI": "memory://",
        "RATELIMIT_STORAGE_OPTIONS": {"wrap_exceptions": True},
    }
    if app_env != "production":
        # 로컬 전용: 프로세스 재시작 시 초기화되며 다른 프로세스와 공유되지 않습니다.
        return config

    storage_uri = os.environ.get("RATELIMIT_STORAGE_URI", "").strip()
    try:
        parsed = urlsplit(storage_uri)
        if (
            parsed.scheme not in ("redis", "rediss")
            or not parsed.hostname
            or any(char.isspace() for char in storage_uri)
            or parsed.query
            or parsed.fragment
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or (
                parsed.path not in ("", "/")
                and not (
                    parsed.path.startswith("/")
                    and parsed.path[1:].isascii()
                    and parsed.path[1:].isdigit()
                )
            )
        ):
            raise ValueError
    except ValueError:
        # 주소·비밀번호·원본 예외를 출력하지 않습니다. memory://로 대체하지 않습니다.
        raise RuntimeError(
            "RATELIMIT_STORAGE_URI 설정 오류: query/fragment 없는 Redis 연결 URI가 필요합니다."
        ) from None

    options = {
        "wrap_exceptions": True,
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
    }
    if parsed.scheme == "rediss":
        # URI query를 허용하지 않아 TLS 검증과 연결 제한 시간이 덮어써지지 않습니다.
        options.update(ssl_cert_reqs="required", ssl_check_hostname=True)
    config["RATELIMIT_STORAGE_URI"] = storage_uri
    config["RATELIMIT_STORAGE_OPTIONS"] = options
    return config


# template_folder는 파일을 자동 공개하지 않습니다. 아래의 고정된 화면만 렌더링합니다.
app = Flask(__name__, static_folder=None, template_folder=str(BASE_DIR))
app.config.update(load_auth_config())
app.config.update(load_rate_limit_config())

# request.remote_addr만 사용하며 프록시 헤더를 무조건 신뢰하지 않습니다.
try:
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri=app.config["RATELIMIT_STORAGE_URI"],
        storage_options=app.config["RATELIMIT_STORAGE_OPTIONS"],
        strategy="moving-window",
        key_prefix="EnglishTutor",
        swallow_errors=False,
        in_memory_fallback=[],
        in_memory_fallback_enabled=False,
        # 운영에서는 응답 이후 헤더 생성을 위한 Redis 재조회도 하지 않습니다.
        headers_enabled=app.config["APP_ENV"] != "production",
    )
    if app.config["APP_ENV"] == "production" and not limiter.storage.check():
        raise RuntimeError
except Exception:
    raise RuntimeError(
        "요청 제한 저장소 초기화 실패: RATELIMIT_STORAGE_URI와 Redis 연결 설정을 확인하세요."
    ) from None


def chat_account_key():
    # 공용 비밀번호의 단일 계정입니다. 모든 로그인 세션이 같은 한도를 공유합니다.
    # 세션 토큰이나 IP를 키로 쓰지 않아 재로그인으로 한도를 초기화할 수 없습니다.
    return "account:shared"


CHAT_INSTRUCTIONS = (
    "You are a friendly English conversation partner. "
    "Reply naturally in English in 1-2 short sentences, including exactly one follow-up question. "
    "Keep the English reply focused on the conversation; put any correction only in the separate correction field. "
    "Offer at most one meaningful grammar or expression correction from the latest user message only. "
    "Use a short exact substring of that message as original, an improved English expression, "
    "and a brief Korean explanation of at most 400 characters. "
    "Use correction=null if there is no clear meaningful error, if the wording is already natural, "
    "or if the transcription is uncertain. Do not invent errors or force stylistic rewrites. "
    "Do not assess pronunciation from text. Never correct an earlier user or assistant message."
)
# 브라우저는 선택값만 보냅니다. AI 지시는 검증된 선택에 대응하는 서버 문구로만 만듭니다.
DIFFICULTY_INSTRUCTIONS = {
    "beginner": (
        "Use simple, common English words and very short, easy sentences. "
        "Keep the single follow-up question easy to answer."
    ),
    "intermediate": "",
    "advanced": (
        "Use richer, precise English vocabulary and a thoughtful follow-up question "
        "that invites deeper discussion, while keeping the reply brief and natural."
    ),
}
TOPIC_INSTRUCTIONS = {
    "free": "",
    "daily": "Focus on realistic everyday situations such as routines, hobbies, meals, and plans.",
    "travel": "Focus on realistic travel situations such as transport, accommodation, sightseeing, and asking for help.",
    "business": "Focus on realistic workplace situations such as meetings, projects, schedules, and professional communication.",
}
TOPIC_PRIORITY_INSTRUCTIONS = (
    "Use the currently selected topic as the main direction even if earlier conversation was about something else. "
    "Still respond to the user's latest message and preserve relevant context; do not disregard or rewrite the conversation history."
)
TOPIC_CHANGE_INSTRUCTIONS = (
    "The user has selected a different conversation topic for this turn. "
    "Briefly and naturally transition toward the current topic instead of continuing questions tied to the previous topic, "
    "while responding to the latest user message."
)
FREE_TOPIC_RETURN_INSTRUCTIONS = (
    "The user has returned to free conversation. Let the latest user message guide the subject; "
    "do not keep steering questions toward a topic selected earlier."
)
CHAT_ERROR = "AI 답변을 받지 못했습니다. 잠시 후 다시 시도해 주세요."
MAX_HISTORY_TURNS = 5
# 최근 5쌍(10개)에 새 사용자 문장 하나를 더한 요청까지 받습니다.
MAX_REQUEST_MESSAGES = MAX_HISTORY_TURNS * 2 + 1
MAX_USER_MESSAGE_CHARS = 6000
MAX_ASSISTANT_MESSAGE_CHARS = 2000
MAX_CORRECTION_EXPLANATION_CHARS = 400
# 최대 필드 길이와 JSON의 유니코드 이스케이프를 수용하되 무제한 출력 파싱은 막습니다.
MAX_CHAT_OUTPUT_CHARS = 192 * 1024
CHAT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "name": "english_conversation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "correction": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "original": {"type": "string"},
                            "improved": {"type": "string"},
                            "explanation": {"type": "string"},
                        },
                        "required": ["original", "improved", "explanation"],
                        "additionalProperties": False,
                    },
                    {"type": "null"},
                ]
            },
        },
        "required": ["reply", "correction"],
        "additionalProperties": False,
    },
}


def chat_instructions_for_request(data):
    settings = data.get("settings", {})
    if not isinstance(settings, dict) or set(settings) - {"difficulty", "topic"}:
        raise ValueError
    difficulty = settings.get("difficulty", "intermediate")
    topic = settings.get("topic", "free")
    previous_topic = data.get("previous_topic")
    if (
        not isinstance(difficulty, str)
        or difficulty not in DIFFICULTY_INSTRUCTIONS
        or not isinstance(topic, str)
        or topic not in TOPIC_INSTRUCTIONS
        or (
            previous_topic is not None
            and (not isinstance(previous_topic, str) or previous_topic not in TOPIC_INSTRUCTIONS)
        )
    ):
        raise ValueError

    fragments = [CHAT_INSTRUCTIONS]
    fragments.extend(filter(None, (DIFFICULTY_INSTRUCTIONS[difficulty], TOPIC_INSTRUCTIONS[topic])))
    if topic != "free":
        fragments.append(TOPIC_PRIORITY_INSTRUCTIONS)
    if previous_topic is not None and previous_topic != topic:
        fragments.append(TOPIC_CHANGE_INSTRUCTIONS)
        if topic == "free":
            fragments.append(FREE_TOPIC_RETURN_INSTRUCTIONS)
    return " ".join(fragments)


def validated_correction(value, latest_user_message):
    fields = {"original", "improved", "explanation"}
    if not isinstance(value, dict) or not fields.issubset(value):
        return None
    if any(not isinstance(value[field], str) or not value[field].strip() for field in fields):
        return None
    original = value["original"].strip()
    improved = value["improved"].strip()
    explanation = value["explanation"].strip()
    if (
        len(original) > MAX_USER_MESSAGE_CHARS
        or len(improved) > MAX_USER_MESSAGE_CHARS
        or len(explanation) > MAX_CORRECTION_EXPLANATION_CHARS
        or original not in latest_user_message
        or original == improved
        or not any("\uac00" <= char <= "\ud7a3" for char in explanation)
    ):
        return None
    return {"original": original, "improved": improved, "explanation": explanation}


def reject_non_json_constant(_value):
    # 표준 JSON이 아닌 NaN/Infinity도 일반 JSON 파싱 오류와 동일하게 거부합니다.
    raise ValueError


def parsed_chat_response(response, latest_user_message):
    if (
        getattr(response, "status", None) != "completed"
        or getattr(response, "incomplete_details", None) is not None
        or getattr(response, "error", None) is not None
    ):
        raise ValueError
    for item in getattr(response, "output", []) or []:
        content = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
        for part in content or []:
            kind = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            if kind == "refusal":
                raise ValueError
    raw = response.output_text
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_CHAT_OUTPUT_CHARS:
        raise ValueError
    payload = json.loads(raw, parse_constant=reject_non_json_constant)
    if not isinstance(payload, dict) or not isinstance(payload.get("reply"), str):
        raise ValueError
    reply = payload["reply"].strip()
    if not reply or len(reply) > MAX_ASSISTANT_MESSAGE_CHARS:
        raise ValueError
    # 교정이 없거나 부정확해도 검증된 대화 답변은 유지합니다. 원본 JSON은 반환하지 않습니다.
    return {
        "reply": reply,
        "correction": validated_correction(payload.get("correction"), latest_user_message),
    }


# STT는 텍스트 대화와 별도의 업로드·요청 한도를 사용합니다.
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"
TRANSCRIBE_ERROR = "음성을 텍스트로 변환하지 못했습니다. 잠시 후 다시 시도해 주세요."
MAX_AUDIO_BYTES = 5 * 1024 * 1024  # 5MiB: 음성 파일 자체의 최대 크기입니다.
# multipart 경계와 파일 헤더를 위한 여유이며, 파일 한도는 별도로 검사합니다.
MAX_TRANSCRIBE_REQUEST_BYTES = MAX_AUDIO_BYTES + 64 * 1024
# 첫 MIME은 OpenAI에 보낼 표준 값이고, 나머지는 브라우저에서 사용하는 별칭입니다.
AUDIO_MIME_TYPES = {
    ".flac": ("audio/flac", "audio/x-flac"),
    ".mp3": ("audio/mpeg", "audio/mp3", "audio/x-mp3"),
    ".mp4": ("audio/mp4", "video/mp4", "application/mp4"),
    ".mpeg": ("audio/mpeg", "video/mpeg"),
    ".mpga": ("audio/mpeg", "audio/mpga"),
    ".m4a": ("audio/mp4", "audio/m4a", "audio/x-m4a"),
    ".ogg": ("audio/ogg", "application/ogg"),
    ".wav": ("audio/wav", "audio/wave", "audio/x-wav", "audio/vnd.wave"),
    ".webm": ("audio/webm", "video/webm"),
}


def get_csrf_token():
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def valid_csrf_token(token):
    expected = session.get("csrf_token")
    return (
        isinstance(expected, str)
        and isinstance(token, str)
        and expected.isascii()
        and token.isascii()
        and secrets.compare_digest(expected, token)
    )


@app.errorhandler(StorageError)
def rate_limit_storage_unavailable(_error):
    # 제한 검사에서 실패하면 로그인/채팅/STT 함수에 진입하지 않아 OpenAI도 호출하지 않습니다.
    # 원본 Redis 예외에는 접속 정보가 있을 수 있어 로그나 응답에 포함하지 않습니다.
    app.logger.warning("Rate limit storage unavailable.")
    return jsonify({"error": "요청 제한 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."}), 503


@app.errorhandler(429)
def rate_limit_exceeded(_error):
    # 제한 키, 예외 상세, 비밀값을 응답에 포함하지 않습니다.
    return jsonify({"error": "요청 횟수 제한에 도달했습니다. 잠시 후 다시 시도해 주세요."}), 429


@app.after_request
def protect_auth_responses(response):
    # 로그인 화면과 대화 응답을 브라우저 캐시에 남기지 않습니다.
    if request.path in ("/", "/index.html", "/login", "/logout", "/api/chat", "/api/transcribe"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/index.html")
@app.get("/")
def index():
    if session.get("authenticated") is not True:
        return redirect(url_for("login"))
    return render_template("index.html", csrf_token=get_csrf_token())


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per 15 minutes", methods=["POST"])
def login():
    if session.get("authenticated") is True:
        return redirect(url_for("index"))

    error = None
    status = 200
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token")):
            error = "요청을 확인할 수 없습니다. 페이지를 새로고침하고 다시 시도해 주세요."
            status = 403
        else:
            # 폼 본문으로만 받으며, 입력 비밀번호를 응답이나 로그에 넣지 않습니다.
            password = request.form.get("password", "")
            if (
                not password
                or len(password) > 1024
                or not check_password_hash(app.config["APP_PASSWORD_HASH"], password)
            ):
                error = "비밀번호가 올바르지 않습니다."
                status = 401
            else:
                # 기존 세션을 비우고 새 로그인 상태와 위조 요청 방지 토큰을 만듭니다.
                session.clear()
                session["authenticated"] = True
                session["csrf_token"] = secrets.token_urlsafe(32)
                return redirect(url_for("index"), code=303)

    return render_template("login.html", error=error, csrf_token=get_csrf_token()), status


@app.post("/logout")
def logout():
    if session.get("authenticated") is not True:
        return redirect(url_for("login"), code=303)
    if not valid_csrf_token(request.form.get("csrf_token")):
        return jsonify({"error": "요청을 확인할 수 없습니다. 페이지를 새로고침해 주세요."}), 403
    session.clear()
    return redirect(url_for("login"), code=303)


@app.get("/style.css")
def stylesheet():
    return send_from_directory(BASE_DIR, "style.css")


@app.get("/app.js")
def javascript():
    return send_from_directory(BASE_DIR, "app.js")


# GET /api/health 요청에 서버 상태를 JSON으로 응답합니다.
@app.get("/api/health")
@limiter.exempt
def health():
    return jsonify({"status": "ok", "service": "EnglishTutor"})


@app.post("/api/chat")
@limiter.limit(
    "5 per minute;30 per hour",
    key_func=chat_account_key,
    methods=["POST"],
    # 미로그인 요청은 제한 처리 대신 기존 인증 검사에서 항상 401을 반환합니다.
    exempt_when=lambda: session.get("authenticated") is not True,
)
def chat():
    # 인증을 먼저 확인해 로그인하지 않은 요청에서는 API를 호출하지 않습니다.
    if session.get("authenticated") is not True:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    if not valid_csrf_token(request.headers.get("X-CSRF-Token")):
        return jsonify({"error": "요청을 확인할 수 없습니다. 페이지를 새로고침해 주세요."}), 403

    # 클라이언트가 보낸 이력은 API를 호출하기 전에 모두 검증합니다.
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return jsonify({"error": "messages에 대화 목록을 보내 주세요."}), 400
    try:
        instructions = chat_instructions_for_request(data)
    except ValueError:
        # 클라이언트가 보낸 설정이나 원본 오류는 응답·로그에 반영하지 않습니다.
        return jsonify({"error": "대화 설정이 올바르지 않습니다. 난이도와 주제를 다시 선택해 주세요."}), 400

    messages = data["messages"]
    if not 1 <= len(messages) <= MAX_REQUEST_MESSAGES:
        return jsonify({"error": "대화 목록은 1개 이상 11개 이하여야 합니다."}), 400

    validated_messages = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            return jsonify({"error": "각 메시지는 role과 content가 있는 객체여야 합니다."}), 400

        role = message.get("role")
        content = message.get("content")
        if role not in ("user", "assistant"):
            return jsonify({"error": "role은 user와 assistant만 사용할 수 있습니다."}), 400

        # user로 시작해 질문과 답변이 번갈아 나와야 완전한 쌍을 유지합니다.
        expected_role = "user" if index % 2 == 0 else "assistant"
        if role != expected_role:
            return jsonify({"error": "user와 assistant 메시지를 순서대로 보내 주세요."}), 400

        if not isinstance(content, str) or not content.strip():
            return jsonify({"error": "각 메시지에는 비어 있지 않은 문장이 필요합니다."}), 400

        max_length = MAX_USER_MESSAGE_CHARS if role == "user" else MAX_ASSISTANT_MESSAGE_CHARS
        if len(content) > max_length:
            return jsonify({"error": f"{role} 메시지는 {max_length}자 이내여야 합니다."}), 400

        # 추가 필드는 전달하지 않고, 검증된 역할과 문장만 API에 보냅니다.
        validated_messages.append({"role": role, "content": content.strip()})

    if validated_messages[-1]["role"] != "user":
        return jsonify({"error": "마지막 메시지는 user여야 합니다."}), 400

    try:
        # SDK가 서버의 OPENAI_API_KEY 환경변수를 자동으로 읽습니다.
        # 받은 대화 이력을 input으로 보내며, 서버에는 이력을 저장하지 않습니다.
        with OpenAI(timeout=30.0, max_retries=0) as client:
            response = client.responses.create(
                model="gpt-5.4-mini",
                instructions=instructions,
                input=validated_messages,
                text={"format": CHAT_RESPONSE_FORMAT},
                store=False,
            )
        result = parsed_chat_response(response, validated_messages[-1]["content"])
    except Exception:
        # 예외 내용에는 키나 내부 정보가 포함될 수 있어 응답에 넣지 않습니다.
        app.logger.warning("Chat request failed.")
        return jsonify({"error": CHAT_ERROR}), 502

    return jsonify(result)


@app.post("/api/transcribe")
@limiter.limit(
    "5 per minute;30 per hour",
    key_func=chat_account_key,
    methods=["POST"],
    # chat과 같은 공용 계정 키를 사용하되, 한도는 STT 엔드포인트에 따로 적용합니다.
    # 미로그인 요청은 Redis 검사 대신 아래 인증 검사에서 401을 반환합니다.
    exempt_when=lambda: session.get("authenticated") is not True,
)
def transcribe():
    if session.get("authenticated") is not True:
        return jsonify({"error": "로그인이 필요합니다."}), 401
    if not valid_csrf_token(request.headers.get("X-CSRF-Token")):
        return jsonify({"error": "요청을 확인할 수 없습니다. 페이지를 새로고침해 주세요."}), 403
    if request.mimetype != "multipart/form-data":
        return jsonify({"error": "multipart/form-data의 audio 파일로 보내 주세요."}), 415

    # Flask 3.1의 요청별 제한입니다. 기존 로그인·채팅 요청의 설정은 바꾸지 않습니다.
    # 반드시 request.files를 읽기 전에 설정해 multipart 파싱 단계도 제한합니다.
    request.max_content_length = MAX_TRANSCRIBE_REQUEST_BYTES
    request.max_form_memory_size = 128 * 1024
    request.max_form_parts = 4
    try:
        if request.content_length is not None and request.content_length > MAX_TRANSCRIBE_REQUEST_BYTES:
            raise RequestEntityTooLarge()
        files = request.files
        if set(files) != {"audio"} or len(files.getlist("audio")) != 1:
            return jsonify({"error": "audio 항목에 음성 파일 하나를 보내 주세요."}), 400
        audio = files["audio"]
        if not audio.filename:
            return jsonify({"error": "음성 파일이 없습니다. 녹음 파일을 보내 주세요."}), 400

        extension = Path(audio.filename).suffix.lower()
        mime_types = AUDIO_MIME_TYPES.get(extension)
        # codecs 파라미터가 제거된 MIME을 검사합니다. 일반 바이너리 MIME은
        # 확장자가 허용된 경우에만 받으며, 실제 디코딩 검사는 전사 API가 수행합니다.
        if mime_types is None or audio.mimetype not in (*mime_types, "", "application/octet-stream"):
            return jsonify({"error": "지원하는 음성 형식은 flac, mp3, mp4, mpeg, mpga, m4a, ogg, wav, webm입니다."}), 415

        try:
            audio_bytes = audio.read(MAX_AUDIO_BYTES + 1)
        finally:
            # Flask의 요청용 임시 스트림만 사용하며 업로드 파일을 영구 저장하지 않습니다.
            audio.close()
        if not audio_bytes:
            return jsonify({"error": "음성 파일이 비어 있습니다. 다시 녹음해 주세요."}), 400
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise RequestEntityTooLarge()
    except RequestEntityTooLarge:
        return jsonify({"error": "음성 파일은 최대 5MiB까지 보낼 수 있습니다."}), 413
    except (BadRequest, OSError):
        return jsonify({"error": "음성 파일 요청을 읽을 수 없습니다. 다시 보내 주세요."}), 400

    try:
        # SDK가 서버의 OPENAI_API_KEY 환경변수를 자동으로 읽습니다.
        # 사용자 파일명은 전달하지 않고, 검증된 확장자의 고정 이름을 사용합니다.
        with OpenAI(timeout=30.0, max_retries=0) as client:
            response = client.audio.transcriptions.create(
                model=TRANSCRIBE_MODEL,
                file=("recording" + extension, audio_bytes, mime_types[0]),
                language="en",
                response_format="json",
            )
        text = response.text
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": TRANSCRIBE_ERROR}), 502
    except Exception:
        # 예외 상세·음성 데이터·전사 텍스트·비밀값은 로그나 응답에 넣지 않습니다.
        app.logger.warning("Transcription request failed.")
        return jsonify({"error": TRANSCRIBE_ERROR}), 502

    return jsonify({"text": text.strip()})


# 이 파일을 직접 실행할 때만 로컬 서버를 시작합니다.
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
