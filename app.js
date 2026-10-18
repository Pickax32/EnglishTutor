const startButton = document.querySelector("#start-conversation");
const chatForm = document.querySelector("#chat-form");
const messageInput = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-message");
const chatMessages = document.querySelector("#chat-messages");
const connectionNotice = document.querySelector("#connection-notice");
const conversationNote = document.querySelector("#conversation-note");
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content ?? "";

// 뒤로 가기로 복원된 화면도 서버에서 로그인 상태를 다시 확인합니다.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

let conversationStarted = false;
let inFlight = false;
const MAX_HISTORY_TURNS = 5;
const MAX_USER_MESSAGE_LENGTH = 500;
const MAX_ASSISTANT_MESSAGE_LENGTH = 2000;

// 새로고침하면 초기화됩니다. 화면의 고정 인사말은 이력에 넣지 않습니다.
// 성공한 user/assistant 대화 쌍만 순서대로 저장합니다.
let conversationHistory = [];

function showNotice(message, isError = false) {
  connectionNotice.textContent = message;
  connectionNotice.classList.toggle("is-error", isError);
}

function updateSendButton() {
  const message = messageInput.value.trim();
  sendButton.disabled = !conversationStarted || inFlight || message.length === 0;
}

function setLoading(isLoading) {
  messageInput.disabled = isLoading;
  chatForm.setAttribute("aria-busy", String(isLoading));
  sendButton.textContent = isLoading ? "Loading..." : "Send";
  updateSendButton();
}

// 문장과 AI 답변은 HTML로 해석하지 않고 텍스트로만 표시합니다.
function appendMessage(role, text) {
  const message = document.createElement("div");
  message.className = role === "user" ? "user-message" : "ai-message";

  if (role === "assistant") {
    const avatar = document.createElement("span");
    avatar.className = "ai-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "AI";
    message.append(avatar);
  }

  const content = document.createElement("div");
  content.className = "message-content";
  const author = document.createElement("p");
  author.className = "message-author";
  author.lang = "en";
  author.textContent = role === "user" ? "You" : "English Tutor";
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.lang = "en";
  const messageText = document.createElement("p");
  messageText.className = "chat-text";
  messageText.textContent = text;
  bubble.append(messageText);

  content.append(author, bubble);
  message.append(content);
  chatMessages.append(message);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return message;
}

startButton.addEventListener("click", () => {
  if (conversationStarted) return;
  conversationStarted = true;
  startButton.hidden = true;
  chatForm.hidden = false;
  conversationNote.textContent = "문법 교정이 필요하면 영어로 요청해 보세요. 한 문장씩 편하게 연습해요.";
  showNotice("대화를 시작했어요. 첫 영어 문장을 보내 보세요.");
  updateSendButton();
  messageInput.focus();
});

messageInput.addEventListener("input", () => {
  updateSendButton();
  showNotice("");
});

messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!inFlight) chatForm.requestSubmit();
  }
});

// STEP 3-1A: 마이크 접근만 확인합니다. 음성을 녹음하거나 서버에 보내지 않습니다.
const microphoneTestButton = document.querySelector("#test-microphone");
const microphoneStatus = document.querySelector("#microphone-status");
let microphoneTestInFlight = false;

function showMicrophoneStatus(message, isError = false) {
  microphoneStatus.textContent = message;
  microphoneStatus.classList.toggle("is-error", isError);
}

function microphoneErrorMessage(error) {
  switch (error?.name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
      return "마이크 권한이 필요합니다";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "마이크를 찾을 수 없습니다. 기기에 마이크가 연결되어 있는지 확인해 주세요.";
    case "NotReadableError":
    case "TrackStartError":
      return "마이크를 사용할 수 없습니다. 다른 앱에서 사용 중인지 확인한 뒤 다시 시도해 주세요.";
    case "AbortError":
      return "마이크 연결이 중단되었습니다. 잠시 후 다시 시도해 주세요.";
    case "SecurityError":
      return "브라우저의 보안 설정으로 마이크가 차단되었습니다. 사이트 권한을 확인해 주세요.";
    case "InvalidStateError":
      return "이 화면을 활성화한 뒤 마이크 테스트를 다시 눌러 주세요.";
    default:
      return "마이크에 연결하지 못했습니다. 브라우저의 마이크 설정을 확인한 뒤 다시 시도해 주세요.";
  }
}

async function testMicrophone() {
  if (microphoneTestInFlight || activeRecording) return;
  if (window.isSecureContext === false) {
    showMicrophoneStatus("마이크 테스트에는 HTTPS 연결이 필요합니다. 태블릿에서는 HTTPS 주소로 접속해 주세요.", true);
    return;
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
    showMicrophoneStatus("이 브라우저는 마이크 테스트를 지원하지 않습니다. 브라우저를 업데이트하거나 다른 브라우저에서 다시 시도해 주세요.", true);
    return;
  }

  // 권한 요청 중에는 중복 클릭을 막습니다. 텍스트 채팅 상태와는 별개입니다.
  microphoneTestInFlight = true;
  microphoneTestButton.disabled = true;
  microphoneTestButton.textContent = "확인 중…";
  microphoneTestButton.setAttribute("aria-busy", "true");
  updateRecordingControls();
  showMicrophoneStatus("마이크 권한을 확인하고 있어요. 브라우저의 권한 요청에 응답해 주세요.");

  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    showMicrophoneStatus("마이크 연결 성공");
  } catch (error) {
    showMicrophoneStatus(microphoneErrorMessage(error), true);
  } finally {
    // 스트림의 모든 트랙을 즉시 종료하여 마이크를 계속 사용하지 않습니다.
    stream?.getTracks().forEach((track) => track.stop());
    microphoneTestInFlight = false;
    microphoneTestButton.disabled = false;
    microphoneTestButton.textContent = "마이크 테스트";
    microphoneTestButton.setAttribute("aria-busy", "false");
    updateRecordingControls();
  }
}

microphoneTestButton.addEventListener("click", testMicrophone);

// STEP 3-2A/3-3C: 녹음·재생은 로컬에서 처리하고 변환 버튼을 누를 때만 음성을 보냅니다.
const recordingControls = document.querySelector("#recording-controls");
const recordStartButton = document.querySelector("#start-recording");
const recordStopButton = document.querySelector("#stop-recording");
const recordPlayButton = document.querySelector("#play-recording");
const recordClearButton = document.querySelector("#clear-recording");
const recordTranscribeButton = document.querySelector("#transcribe-recording");
const recordingStatus = document.querySelector("#recording-status");
const recordingTime = document.querySelector("#recording-time");
const recordingAudio = document.querySelector("#recording-playback");
const transcriptionStatus = document.querySelector("#transcription-status");
const transcriptionResult = document.querySelector("#transcription-result");
const transcriptionText = document.querySelector("#transcription-text");
const transcriptionLogin = document.querySelector("#transcription-login");
const MAX_RECORDING_MS = 15000;
const MAX_TRANSCRIPTION_BYTES = 5 * 1024 * 1024;
const TRANSCRIPTION_TIMEOUT_MS = 45000;
let activeRecording = null;
let recordingObjectUrl = null;
let recordedBlob = null;
let transcriptionRequest = null;
let playbackRequest = 0;

function showRecordingStatus(message, isError = false) {
  recordingStatus.textContent = message;
  recordingStatus.classList.toggle("is-error", isError);
}

function updateRecordingControls() {
  const busy = activeRecording !== null;
  recordStartButton.disabled = busy || microphoneTestInFlight;
  recordStopButton.disabled = !busy || activeRecording.phase === "stopping";
  recordPlayButton.disabled = busy || !recordingObjectUrl;
  recordClearButton.disabled = busy || !recordingObjectUrl;
  recordTranscribeButton.disabled = busy || microphoneTestInFlight || !recordedBlob || transcriptionRequest !== null;
  recordTranscribeButton.textContent = transcriptionRequest ? "변환 중..." : "음성을 텍스트로 변환";
  recordTranscribeButton.setAttribute("aria-busy", String(transcriptionRequest !== null));
  microphoneTestButton.disabled = microphoneTestInFlight || busy;
  recordingControls.classList.toggle("is-recording", activeRecording?.phase === "recording");
  recordingControls.setAttribute("aria-busy", String(busy && activeRecording.phase !== "recording"));
}

function releaseRecordingTracks(session) {
  const stream = session.stream;
  session.stream = null;
  stream?.getTracks().forEach((track) => track.stop());
}

function clearRecordingTimers(session) {
  clearTimeout(session.timeout);
  clearInterval(session.clock);
  session.timeout = null;
  session.clock = null;
}

function updateRecordingClock(session) {
  const seconds = Math.min(15, Math.floor((performance.now() - session.startedAt) / 1000));
  recordingTime.textContent = `${seconds} / 15초`;
}

function releaseRecordingPlayback() {
  // 새 녹음·삭제·화면 이탈 때 Blob과 결과를 지우고 이전 응답을 무효화합니다.
  clearTranscription();
  recordedBlob = null;
  playbackRequest += 1;
  const previousUrl = recordingObjectUrl;
  recordingObjectUrl = null;
  recordingAudio.pause();
  recordingAudio.removeAttribute("src");
  recordingAudio.load();
  recordingAudio.hidden = true;
  if (previousUrl) URL.revokeObjectURL(previousUrl);
}

function detachRecorder(session) {
  if (!session.recorder) return;
  session.recorder.ondataavailable = null;
  session.recorder.onstop = null;
  session.recorder.onerror = null;
}

function discardRecordingSession(session) {
  session.discarded = true;
  clearRecordingTimers(session);
  detachRecorder(session);
  try {
    if (session.recorder && session.recorder.state !== "inactive") session.recorder.stop();
  } catch {
    // 종료 중 오류가 발생해도 마이크와 임시 녹음 데이터는 정리합니다.
  } finally {
    releaseRecordingTracks(session);
    session.chunks = [];
    session.recorder = null;
    if (activeRecording === session) activeRecording = null;
    updateRecordingControls();
  }
}

function recordingErrorMessage(error) {
  if (error?.name === "NotSupportedError") {
    return "이 브라우저에서는 음성 녹음을 사용할 수 없습니다. 브라우저를 업데이트하거나 다른 브라우저에서 시도해 주세요.";
  }
  if (["NotAllowedError", "PermissionDeniedError", "NotFoundError", "NotReadableError", "SecurityError"].includes(error?.name)) {
    return microphoneErrorMessage(error);
  }
  return "녹음에 실패했습니다. 마이크와 브라우저 설정을 확인한 뒤 다시 시도해 주세요.";
}

function finishRecording(session) {
  clearRecordingTimers(session);
  releaseRecordingTracks(session);
  if (activeRecording !== session || session.discarded) return;
  const mimeType = session.recorder.mimeType || session.chunks.find((chunk) => chunk.type)?.type || "";
  const chunks = session.chunks;
  session.chunks = [];
  detachRecorder(session);
  session.recorder = null;
  activeRecording = null;
  try {
    // 브라우저가 실제 선택한 형식을 사용하며 특정 MIME 타입을 강제하지 않습니다.
    const blob = mimeType ? new Blob(chunks, { type: mimeType }) : new Blob(chunks);
    if (!blob.size) throw new Error("Empty recording");
    recordingObjectUrl = URL.createObjectURL(blob);
    recordingAudio.src = recordingObjectUrl;
    recordingAudio.hidden = false;
    recordingAudio.load();
    recordedBlob = blob;
    showRecordingStatus(session.stopReason === "limit"
      ? "15초가 되어 자동으로 녹음을 종료했습니다. 녹음을 재생할 수 있어요."
      : "녹음이 종료되었습니다. 녹음 재생을 눌러 들어보세요.");
  } catch {
    releaseRecordingPlayback();
    showRecordingStatus("재생할 녹음이 만들어지지 않았습니다. 다시 녹음해 주세요.", true);
  } finally {
    updateRecordingControls();
  }
}

function stopLocalRecording(reason = "manual") {
  const session = activeRecording;
  if (!session || session.phase === "stopping") return;
  if (session.phase === "requesting") {
    discardRecordingSession(session);
    showRecordingStatus("녹음 시작을 취소했습니다. 다시 시작할 수 있어요.");
    return;
  }
  updateRecordingClock(session);
  session.phase = "stopping";
  session.stopReason = reason;
  clearRecordingTimers(session);
  updateRecordingControls();
  showRecordingStatus("녹음을 종료하고 재생을 준비하고 있어요.");
  try {
    if (session.recorder.state !== "inactive") session.recorder.stop();
  } catch (error) {
    discardRecordingSession(session);
    showRecordingStatus(recordingErrorMessage(error), true);
  } finally {
    // 최종 데이터는 stop 이벤트에서 받고 마이크는 종료 버튼을 누르는 즉시 해제합니다.
    releaseRecordingTracks(session);
  }
}

async function startLocalRecording() {
  if (activeRecording || microphoneTestInFlight) return;
  if (window.isSecureContext === false) {
    showRecordingStatus("녹음에는 HTTPS 연결이 필요합니다. HTTPS 주소로 접속해 주세요.", true);
    return;
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function" || typeof MediaRecorder !== "function") {
    showRecordingStatus("이 브라우저는 음성 녹음을 지원하지 않습니다. 브라우저를 업데이트하거나 다른 브라우저에서 시도해 주세요.", true);
    return;
  }

  releaseRecordingPlayback();
  recordingTime.textContent = "0 / 15초";
  const session = {
    phase: "requesting", stream: null, recorder: null, chunks: [],
    timeout: null, clock: null, startedAt: 0, stopReason: "manual", discarded: false,
  };
  activeRecording = session;
  updateRecordingControls();
  showRecordingStatus("마이크 권한을 확인하고 있어요. 권한 요청에 응답해 주세요.");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // 취소한 뒤 늦게 허용된 이전 요청도 받은 모든 트랙을 즉시 종료합니다.
    if (activeRecording !== session || session.discarded || document.visibilityState === "hidden") {
      stream.getTracks().forEach((track) => track.stop());
      if (activeRecording === session) discardRecordingSession(session);
      return;
    }
    session.stream = stream;
    const recorder = new MediaRecorder(stream);
    session.recorder = recorder;
    recorder.ondataavailable = (event) => {
      if (activeRecording !== session || session.discarded) return;
      if (event.data?.size > 0) session.chunks.push(event.data);
      if (session.phase === "recording" && performance.now() - session.startedAt >= MAX_RECORDING_MS) stopLocalRecording("limit");
    };
    recorder.onstop = () => finishRecording(session);
    recorder.onerror = (event) => {
      if (activeRecording !== session) return;
      discardRecordingSession(session);
      showRecordingStatus(recordingErrorMessage(event.error), true);
    };
    recorder.start(250);
    session.phase = "recording";
    session.startedAt = performance.now();
    session.timeout = setTimeout(() => {
      if (activeRecording === session) stopLocalRecording("limit");
    }, MAX_RECORDING_MS);
    session.clock = setInterval(() => {
      if (activeRecording !== session) return;
      updateRecordingClock(session);
      if (performance.now() - session.startedAt >= MAX_RECORDING_MS) stopLocalRecording("limit");
    }, 250);
    updateRecordingControls();
    showRecordingStatus("녹음 중입니다. 최대 15초까지 녹음해요. 끝나면 녹음 종료를 눌러 주세요.");
  } catch (error) {
    if (activeRecording !== session) return;
    discardRecordingSession(session);
    showRecordingStatus(recordingErrorMessage(error), true);
  }
}

function clearLocalRecording(message = "녹음을 지웠습니다. 다시 녹음할 수 있어요.") {
  if (activeRecording) discardRecordingSession(activeRecording);
  releaseRecordingPlayback();
  recordingTime.textContent = "0 / 15초";
  showRecordingStatus(message);
  updateRecordingControls();
}

async function playLocalRecording() {
  if (activeRecording || !recordingObjectUrl) return;
  const request = ++playbackRequest;
  try {
    recordingAudio.currentTime = 0;
    await recordingAudio.play();
  } catch {
    if (request === playbackRequest && recordingObjectUrl) {
      showRecordingStatus("녹음을 재생하지 못했습니다. 녹음 재생을 다시 누르거나 새로 녹음해 주세요.", true);
    }
  }
}

function showTranscriptionStatus(message, isError = false) {
  transcriptionStatus.textContent = message;
  transcriptionStatus.classList.toggle("is-error", isError);
}

function clearTranscription() {
  const previous = transcriptionRequest;
  // 먼저 무효화해 이전 요청의 catch/finally가 새 요청의 화면을 바꾸지 못하게 합니다.
  transcriptionRequest = null;
  if (previous) {
    clearTimeout(previous.timeout);
    previous.controller.abort();
  }
  transcriptionText.textContent = "";
  transcriptionResult.hidden = true;
  transcriptionLogin.hidden = true;
  showTranscriptionStatus("");
}

function recordingUploadFilename(blob) {
  // MIME의 codecs 파라미터는 파일 확장자 선택에 사용하지 않습니다.
  const mimeType = blob.type.split(";")[0].trim().toLowerCase();
  const extensions = {
    "audio/webm": "webm", "video/webm": "webm",
    "audio/ogg": "ogg", "application/ogg": "ogg",
    "audio/mp4": "mp4", "video/mp4": "mp4", "application/mp4": "mp4",
    "audio/m4a": "m4a", "audio/x-m4a": "m4a",
    "audio/wav": "wav", "audio/wave": "wav", "audio/x-wav": "wav", "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/x-mp3": "mp3",
    "video/mpeg": "mpeg", "audio/mpga": "mpga",
    "audio/flac": "flac", "audio/x-flac": "flac",
  };
  const extension = Object.prototype.hasOwnProperty.call(extensions, mimeType) ? extensions[mimeType] : null;
  // 형식을 알 수 없으면 WebM으로 추측해서 전송하지 않습니다.
  return extension ? `recording.${extension}` : null;
}

function transcriptionErrorMessage(status) {
  switch (status) {
    case 401:
      return "로그인이 만료되었어요. 다시 로그인한 뒤 녹음해 주세요.";
    case 403:
      return "요청 인증이 만료되었어요. 페이지를 새로고침한 뒤 다시 녹음해 주세요.";
    case 429:
      return "음성 변환 요청 횟수 제한에 도달했어요. 잠시 후 다시 시도해 주세요.";
    case 413:
      return "녹음 파일이 너무 커요. 5MiB 이하가 되도록 더 짧게 녹음해 주세요.";
    case 415:
      return "서버에서 지원하지 않는 녹음 형식이에요. 다른 브라우저에서 다시 녹음해 주세요.";
    case 503:
      return "서버를 일시적으로 사용할 수 없어 변환이 중단되었어요. 잠시 후 다시 시도해 주세요.";
    case 400:
      return "녹음 파일을 확인할 수 없어요. 새로 녹음한 뒤 다시 변환해 주세요.";
    case 502:
      return "음성 변환 서비스에서 답을 받지 못했어요. 잠시 후 다시 시도해 주세요.";
    default:
      return "서버에서 음성을 변환하지 못했어요. 잠시 후 다시 시도해 주세요.";
  }
}

async function transcribeLocalRecording() {
  if (activeRecording || microphoneTestInFlight || transcriptionRequest) return;
  if (!recordedBlob || !recordedBlob.size) {
    showTranscriptionStatus("먼저 목소리를 녹음하고 녹음을 종료해 주세요.", true);
    return;
  }
  if (recordedBlob.size > MAX_TRANSCRIPTION_BYTES) {
    showTranscriptionStatus(transcriptionErrorMessage(413), true);
    return;
  }
  const filename = recordingUploadFilename(recordedBlob);
  if (!filename) {
    showTranscriptionStatus("이 녹음의 형식을 확인할 수 없거나 지원하지 않아요. 다른 브라우저에서 다시 녹음해 주세요.", true);
    return;
  }
  if (typeof FormData !== "function" || typeof fetch !== "function" || typeof AbortController !== "function") {
    showTranscriptionStatus("이 브라우저는 음성 변환 요청을 지원하지 않아요. 브라우저를 업데이트해 주세요.", true);
    return;
  }

  clearTranscription();
  const request = { blob: recordedBlob, controller: new AbortController(), timeout: null, timedOut: false };
  // 첫 await 전에 요청 상태를 저장해 빠른 연속 클릭도 한 번만 전송합니다.
  transcriptionRequest = request;
  const isCurrent = () => transcriptionRequest === request && recordedBlob === request.blob;
  let receivedResponse = false;
  showTranscriptionStatus("변환 중...");
  updateRecordingControls();
  try {
    const formData = new FormData();
    // 파일명을 명시하지 않으면 확장자가 없는 'blob'이 되어 서버에서 거부됩니다.
    formData.append("audio", request.blob, filename);
    request.timeout = setTimeout(() => {
      if (!isCurrent()) return;
      request.timedOut = true;
      request.controller.abort();
    }, TRANSCRIPTION_TIMEOUT_MS);
    const response = await fetch("/api/transcribe", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Accept": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      // multipart의 Content-Type과 boundary는 브라우저가 자동으로 붙입니다.
      body: formData,
      signal: request.controller.signal,
    });
    if (!isCurrent()) return;
    receivedResponse = true;
    if (request.timedOut) {
      showTranscriptionStatus("변환 응답이 늦어 요청을 중단했어요. 잠시 후 다시 시도해 주세요.", true);
      return;
    }
    if (!response.ok) {
      showTranscriptionStatus(transcriptionErrorMessage(response.status), true);
      transcriptionLogin.hidden = response.status !== 401;
      return;
    }
    const data = await response.json();
    if (!isCurrent()) return;
    if (request.timedOut) {
      showTranscriptionStatus("변환 응답이 늦어 요청을 중단했어요. 잠시 후 다시 시도해 주세요.", true);
      return;
    }
    if (!data || typeof data.text !== "string" || !data.text.trim()) {
      showTranscriptionStatus("변환된 영어 문장을 받지 못했어요. 다시 녹음하거나 잠시 후 다시 시도해 주세요.", true);
      return;
    }
    // 서버 응답을 HTML로 해석하지 않으며, 텍스트 채팅 입력·이력도 변경하지 않습니다.
    transcriptionText.textContent = data.text.trim();
    transcriptionResult.hidden = false;
    showTranscriptionStatus("음성을 텍스트로 변환했어요. 아래 영어 문장을 확인해 주세요.");
  } catch (error) {
    if (!isCurrent()) return;
    if (request.timedOut || error?.name === "AbortError") {
      showTranscriptionStatus("변환 응답이 늦어 요청을 중단했어요. 잠시 후 다시 시도해 주세요.", true);
    } else if (!receivedResponse) {
      showTranscriptionStatus("네트워크 연결 오류로 변환 결과를 받지 못했어요. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.", true);
    } else {
      showTranscriptionStatus("서버의 변환 응답을 읽을 수 없어요. 잠시 후 다시 시도해 주세요.", true);
    }
  } finally {
    clearTimeout(request.timeout);
    if (transcriptionRequest === request) {
      transcriptionRequest = null;
      updateRecordingControls();
    }
  }
}

recordStartButton.addEventListener("click", startLocalRecording);
recordStopButton.addEventListener("click", () => stopLocalRecording());
recordPlayButton.addEventListener("click", playLocalRecording);
recordClearButton.addEventListener("click", () => clearLocalRecording());
recordTranscribeButton.addEventListener("click", transcribeLocalRecording);
recordingAudio.addEventListener("play", () => {
  if (recordingObjectUrl && !activeRecording) showRecordingStatus("녹음 재생 중입니다.");
});
recordingAudio.addEventListener("pause", () => {
  if (recordingObjectUrl && !activeRecording && !recordingAudio.ended) showRecordingStatus("재생을 멈췄습니다. 다시 들을 수 있어요.");
});
recordingAudio.addEventListener("ended", () => {
  if (recordingObjectUrl && !activeRecording) showRecordingStatus("재생이 끝났습니다. 다시 들을 수 있어요.");
});
recordingAudio.addEventListener("error", () => {
  if (!recordingObjectUrl) return;
  clearLocalRecording("녹음을 재생할 수 없습니다. 다시 녹음해 주세요.");
  showRecordingStatus("녹음을 재생할 수 없습니다. 다시 녹음해 주세요.", true);
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && (activeRecording || recordingObjectUrl)) {
    clearLocalRecording("화면이 숨겨져 녹음과 재생을 정리했습니다. 다시 녹음해 주세요.");
  }
});
window.addEventListener("pagehide", () => clearLocalRecording());
document.querySelector(".logout-form")?.addEventListener("submit", () => clearLocalRecording());
updateRecordingControls();

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!conversationStarted || inFlight) return;

  const message = messageInput.value.trim();
  if (!message) {
    showNotice("영어 문장을 입력해 주세요.", true);
    messageInput.focus();
    return;
  }
  if (Array.from(message).length > MAX_USER_MESSAGE_LENGTH) {
    showNotice("문장은 500자 이내로 입력해 주세요.", true);
    messageInput.focus();
    return;
  }

  // 첫 await 전에 잠가서 Enter와 클릭이 겹쳐도 한 번만 요청합니다.
  inFlight = true;
  setLoading(true);
  showNotice("Loading... AI가 답변을 준비하고 있어요.");
  const userMessage = appendMessage("user", message);
  // 새 문장은 임시 요청에만 추가하고, 답변에 성공한 뒤 이력에 저장합니다.
  const candidateMessages = [
    ...conversationHistory,
    { role: "user", content: message },
  ];

  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ messages: candidateMessages }),
    });

    if (response.status === 401 || response.status === 403) {
      conversationHistory = [];
      messageInput.value = "";
      chatMessages.replaceChildren();
      window.location.replace(response.status === 401 ? "/login" : "/");
      return;
    }

    if (response.status === 429) {
      userMessage.remove();
      showNotice("요청 횟수 제한에 도달했어요. 잠시 후 다시 시도해 주세요.", true);
      return;
    }

    if (!response.ok) throw new Error("Chat request failed");
    const data = await response.json();
    if (!data || typeof data.reply !== "string") {
      throw new Error("Invalid chat response");
    }
    const reply = data.reply.trim();
    if (!reply || Array.from(reply).length > MAX_ASSISTANT_MESSAGE_LENGTH) {
      throw new Error("Invalid chat response");
    }

    const completedHistory = [
      ...candidateMessages,
      { role: "assistant", content: reply },
    ];
    // 오래된 user/assistant 두 메시지를 함께 빼서 최근 5회 왕복만 보관합니다.
    while (completedHistory.length > MAX_HISTORY_TURNS * 2) {
      completedHistory.splice(0, 2);
    }

    appendMessage("assistant", reply);
    conversationHistory = completedHistory;
    messageInput.value = "";
    showNotice("답변이 도착했어요. 다음 문장도 보내 보세요.");
  } catch {
    // 이력은 바꾸지 않습니다. 실패한 문장은 입력창에 남겨 다시 보낼 수 있습니다.
    userMessage.remove();
    showNotice("답변을 받지 못했어요. 잠시 후 Send를 눌러 다시 시도해 주세요.", true);
  } finally {
    inFlight = false;
    setLoading(false);
    messageInput.focus();
  }
});
