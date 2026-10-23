const chatForm = document.querySelector("#chat-form");
const messageInput = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-message");
const chatMessages = document.querySelector("#chat-messages");
const connectionNotice = document.querySelector("#connection-notice");
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content ?? "";

// 뒤로 가기로 복원된 화면도 서버에서 로그인 상태를 다시 확인합니다.
window.addEventListener("pageshow", (event) => {
  if (event.persisted) window.location.reload();
});

let inFlight = false;
let activeChatRequest = null;
const MAX_HISTORY_TURNS = 5;
// 3분 × 분당 200단어 × 단어/공백 평균 7자(약 4,200자)에 여유를 둡니다.
const MAX_USER_MESSAGE_LENGTH = 6000;
const MAX_ASSISTANT_MESSAGE_LENGTH = 2000;

// 새로고침하면 초기화됩니다. 화면의 고정 인사말은 이력에 넣지 않습니다.
// 성공한 user/assistant 대화 쌍만 순서대로 저장합니다.
let conversationHistory = [];

const englishLevelSelect = document.querySelector("#english-level");
const conversationTopicSelect = document.querySelector("#conversation-topic");
const learningSettingsStatus = document.querySelector("#learning-settings-status");
const LEARNING_SETTINGS_KEY = "englishtutor.learningSettings";
const DEFAULT_LEARNING_SETTINGS = Object.freeze({ difficulty: "intermediate", topic: "free" });
const ENGLISH_LEVELS = new Set(["beginner", "intermediate", "advanced"]);
const CONVERSATION_TOPICS = new Set(["free", "daily", "travel", "business"]);

function normalizeLearningSettings(value) {
  const settings = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  return {
    difficulty: ENGLISH_LEVELS.has(settings.difficulty) ? settings.difficulty : DEFAULT_LEARNING_SETTINGS.difficulty,
    topic: CONVERSATION_TOPICS.has(settings.topic) ? settings.topic : DEFAULT_LEARNING_SETTINGS.topic,
  };
}

function loadLearningSettings() {
  try {
    return normalizeLearningSettings(JSON.parse(window.localStorage.getItem(LEARNING_SETTINGS_KEY)));
  } catch {
    // 저장소 접근 제한이나 잘못된 JSON도 대화 시작을 막지 않습니다.
    return { ...DEFAULT_LEARNING_SETTINGS };
  }
}

let learningSettings = loadLearningSettings();
// 대화와 함께 메모리에만 보관합니다. 실패·취소한 요청은 주제 전환 기준이 되지 않습니다.
let lastSuccessfulTopic = null;

function reflectLearningSettings() {
  englishLevelSelect.value = learningSettings.difficulty;
  conversationTopicSelect.value = learningSettings.topic;
}

function updateLearningSettings() {
  learningSettings = normalizeLearningSettings({
    difficulty: englishLevelSelect.value,
    topic: conversationTopicSelect.value,
  });
  reflectLearningSettings();
  try {
    window.localStorage.setItem(LEARNING_SETTINGS_KEY, JSON.stringify(learningSettings));
    learningSettingsStatus.textContent = "다음 채팅부터 적용돼요. 대화 내용은 유지돼요.";
  } catch {
    learningSettingsStatus.textContent = "현재 화면에는 적용돼요. 이 브라우저에서는 설정을 저장할 수 없어요.";
  }
}

reflectLearningSettings();
englishLevelSelect.addEventListener("change", updateLearningSettings);
conversationTopicSelect.addEventListener("change", updateLearningSettings);

function showNotice(message, isError = false) {
  connectionNotice.textContent = message;
  connectionNotice.classList.toggle("is-error", isError);
}

function updateSendButton() {
  const message = messageInput.value.trim();
  sendButton.disabled = inFlight || activeRecording !== null || autoSttJob !== null || message.length === 0;
}

function setLoading(isLoading) {
  messageInput.disabled = isLoading || activeRecording !== null || autoSttJob !== null;
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
  if (role === "assistant") {
    addAnswerSpeechControls(content, text);
    // API 전송 함수는 유지하고, 그 요청이 방금 표시한 답변의 읽기 항목만 연결합니다.
    const ownerJob = activeChatRequest?.ownerJob;
    if (ownerJob && autoSttJob === ownerJob) ownerJob.answerEntry = speechEntries.get(content);
  }
  message.append(content);
  chatMessages.append(message);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return message;
}

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
      return "이 화면을 활성화한 뒤 마이크를 다시 눌러 주세요.";
    default:
      return "마이크에 연결하지 못했습니다. 브라우저의 마이크 설정을 확인한 뒤 다시 시도해 주세요.";
  }
}

// 한 마이크 버튼이 녹음부터 답변 읽기까지 같은 작업을 이어갑니다.
const recordToggleButton = document.querySelector("#toggle-recording");
const recordToggleLabel = document.querySelector("#recording-toggle-label");
const autoSttStatus = document.querySelector("#auto-stt-status");
const cancelAutoSttButton = document.querySelector("#cancel-auto-stt");
const recordingStatus = document.querySelector("#recording-status");
const recordingTime = document.querySelector("#recording-time");
const transcriptionLogin = document.querySelector("#transcription-login");
const MAX_RECORDING_MS = 180000;
// MIME 형식은 브라우저가 선택하며 실제 Blob의 크기도 검사합니다.
const RECORDING_AUDIO_BITS_PER_SECOND = 128000;
const MAX_TRANSCRIPTION_BYTES = 5 * 1024 * 1024;
const TRANSCRIPTION_TIMEOUT_MS = 45000;
let activeRecording = null;
let recordedBlob = null;
let transcriptionRequest = null;
let autoSttJob = null;

function showRecordingStatus(message, isError = false) {
  recordingStatus.textContent = message;
  recordingStatus.classList.toggle("is-error", isError);
}

function updateRecordingControls() {
  const phase = activeRecording?.phase ?? (autoSttJob
    ? (["transcribing", "chatting", "speaking"].includes(autoSttJob.phase) ? autoSttJob.phase : "preparing")
    : (inFlight ? "chatting" : (activeSpeech ? "speaking" : "idle")));
  const labels = {
    idle: "눌러서 대화 시작", requesting: "마이크 준비 중...",
    recording: "녹음 중 · 눌러서 종료", stopping: "녹음 마무리 중...",
    preparing: "음성 처리 준비 중...", transcribing: "음성을 변환하는 중...",
    chatting: "AI 답변 기다리는 중...", speaking: "답변 읽는 중 · 눌러서 새 녹음",
  };
  recordToggleLabel.textContent = labels[phase];
  recordToggleButton.setAttribute("aria-label", labels[phase]);
  recordToggleButton.disabled = ["requesting", "stopping", "preparing", "transcribing", "chatting"].includes(phase);
  recordToggleButton.setAttribute("data-state", phase);
  recordToggleButton.setAttribute("aria-pressed", String(phase === "recording"));
  recordToggleButton.setAttribute("aria-busy", String(!["idle", "recording", "speaking"].includes(phase)));
  const canCancel = Boolean(activeRecording || autoSttJob || transcriptionRequest || activeSpeech || inFlight);
  cancelAutoSttButton.hidden = !canCancel;
  cancelAutoSttButton.disabled = !canCancel;
  setLoading(inFlight);
  updateSpeechButtons();
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
  const seconds = Math.max(0, Math.min(MAX_RECORDING_MS / 1000, Math.floor((performance.now() - session.startedAt) / 1000)));
  const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
  const remainingSeconds = String(seconds % 60).padStart(2, "0");
  recordingTime.textContent = `${minutes}:${remainingSeconds} / 03:00`;
}

function releaseRecordingData() {
  // 재생 UI가 없어 Object URL은 만들지 않습니다. 임시 Blob과 요청만 정리합니다.
  clearTranscription();
  recordedBlob = null;
}

function detachRecorder(session) {
  if (!session.recorder) return;
  session.recorder.ondataavailable = null;
  session.recorder.onstop = null;
  session.recorder.onerror = null;
}

function settleRecordingCompletion(session, result) {
  // 완료는 한 번만 알립니다. 실패도 resolve하여 처리되지 않은 Promise 거부를 막습니다.
  const resolve = session.resolveCompletion;
  if (!resolve) return;
  session.resolveCompletion = null;
  resolve(result);
}

function discardRecordingSession(session, error = "cancelled") {
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
    settleRecordingCompletion(session, { ok: false, blob: null, error });
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
  if (activeRecording !== session || session.discarded) {
    settleRecordingCompletion(session, { ok: false, blob: null, error: "cancelled" });
    return;
  }
  let result = { ok: false, blob: null, error: "recording_failed" };
  try {
    const mimeType = session.recorder.mimeType || session.chunks.find((chunk) => chunk.type)?.type || "";
    const chunks = session.chunks;
    // 브라우저가 실제 선택한 형식을 사용하며 특정 MIME 타입을 강제하지 않습니다.
    const blob = mimeType ? new Blob(chunks, { type: mimeType }) : new Blob(chunks);
    if (!blob.size) {
      result.error = "empty_recording";
      throw new Error("Empty recording");
    }
    recordedBlob = blob;
    showRecordingStatus(session.stopReason === "limit"
      ? "03:00 · 녹음을 마쳤어요."
      : "녹음을 마쳤어요.");
    result = { ok: true, blob, error: null };
  } catch {
    releaseRecordingData();
    showRecordingStatus("녹음이 비어 있거나 사용할 수 없어요. 다시 녹음해 주세요.", true);
  } finally {
    session.chunks = [];
    detachRecorder(session);
    session.recorder = null;
    if (activeRecording === session) activeRecording = null;
    updateRecordingControls();
    // 마지막 dataavailable 이후 Blob 생성이 끝나야 다음 단계에 알립니다.
    settleRecordingCompletion(session, result);
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
  showRecordingStatus("녹음을 마무리하고 있어요.");
  try {
    if (session.recorder.state !== "inactive") session.recorder.stop();
  } catch (error) {
    discardRecordingSession(session, "recording_failed");
    showRecordingStatus(recordingErrorMessage(error), true);
  } finally {
    // 최종 데이터는 stop 이벤트에서 받고 마이크는 종료 버튼을 누르는 즉시 해제합니다.
    releaseRecordingTracks(session);
  }
}

// 시작 준비를 기다린 뒤, 반환된 completion으로 별도로 녹음 완료를 기다릴 수 있습니다.
async function startLocalRecording() {
  if (activeRecording || autoSttJob || inFlight) return { ok: false, completion: null, error: "busy" };
  if (window.isSecureContext === false) {
    showRecordingStatus("녹음에는 HTTPS 연결이 필요합니다. HTTPS 주소로 접속해 주세요.", true);
    return { ok: false, completion: null, error: "insecure_context" };
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function" || typeof MediaRecorder !== "function") {
    showRecordingStatus("이 브라우저는 음성 녹음을 지원하지 않습니다. 브라우저를 업데이트하거나 다른 브라우저에서 시도해 주세요.", true);
    return { ok: false, completion: null, error: "unsupported_browser" };
  }

  if (!stopAnswerSpeech("녹음을 시작하기 위해 답변 읽기를 멈췄어요.")) {
    return { ok: false, completion: null, error: "speech_stop_failed" };
  }
  releaseRecordingData();
  showNotice("");
  showSpeechStatus("");
  recordingTime.textContent = "00:00 / 03:00";
  const session = {
    phase: "requesting", stream: null, recorder: null, chunks: [],
    timeout: null, clock: null, startedAt: 0, stopReason: "manual", discarded: false,
    completion: null, resolveCompletion: null,
  };
  session.completion = new Promise((resolve) => { session.resolveCompletion = resolve; });
  activeRecording = session;
  updateRecordingControls();
  showRecordingStatus("마이크 권한을 확인하고 있어요.");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    // 취소한 뒤 늦게 허용된 이전 요청도 받은 모든 트랙을 즉시 종료합니다.
    if (activeRecording !== session || session.discarded || document.visibilityState === "hidden") {
      stream.getTracks().forEach((track) => track.stop());
      if (activeRecording === session) discardRecordingSession(session);
      return { ok: false, completion: session.completion, error: "cancelled" };
    }
    session.stream = stream;
    const recorder = new MediaRecorder(stream, { audioBitsPerSecond: RECORDING_AUDIO_BITS_PER_SECOND });
    session.recorder = recorder;
    recorder.ondataavailable = (event) => {
      if (activeRecording !== session || session.discarded) return;
      if (event.data?.size > 0) session.chunks.push(event.data);
      if (session.phase === "recording" && performance.now() - session.startedAt >= MAX_RECORDING_MS) stopLocalRecording("limit");
    };
    recorder.onstop = () => finishRecording(session);
    recorder.onerror = (event) => {
      if (activeRecording !== session) return;
      discardRecordingSession(session, "recording_failed");
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
    }, 1000);
    updateRecordingControls();
    showRecordingStatus("녹음 중입니다. 최대 03:00까지 녹음해요. 마이크 버튼을 다시 누르면 종료돼요.");
    return { ok: true, completion: session.completion, error: null };
  } catch (error) {
    if (activeRecording !== session) return { ok: false, completion: session.completion, error: "cancelled" };
    discardRecordingSession(session, "recording_failed");
    showRecordingStatus(recordingErrorMessage(error), true);
    return { ok: false, completion: session.completion, error: "recording_failed" };
  }
}

function toggleLocalRecording() {
  if (activeRecording) {
    if (activeRecording.phase === "recording") stopLocalRecording();
    return;
  }
  if (autoSttJob && autoSttJob.phase !== "speaking") return;
  if (messageInput.value.length > 0) {
    showAutoSttStatus("작성 중인 문장을 보내거나 비운 뒤 마이크를 눌러 주세요.", true);
    return;
  }
  if (inFlight) return;
  // 이전 TTS의 소유 작업부터 무효화해 지연된 완료 이벤트를 분리합니다.
  if (autoSttJob && !invalidateAutomaticStt()) return;
  const started = startLocalRecording();
  if (activeRecording) {
    const job = { session: activeRecording, phase: "recording", blob: null, request: null,
      started: false, attempted: false, chatAttempted: false, answerEntry: null, speechAttempted: false };
    autoSttJob = job;
    showAutoSttStatus("");
    updateRecordingControls();
    job.finished = runAutomaticStt(job);
  }
  return started;
}

// 한 녹음의 완료 신호를 기다린 뒤 STT·채팅·읽기를 한 번씩 이어갑니다.
function showAutoSttStatus(message, isError = false) {
  autoSttStatus.textContent = message;
  autoSttStatus.classList.toggle("is-error", isError);
}

function isCurrentAutomaticStt(job) {
  return Boolean(job) && autoSttJob === job && document.visibilityState !== "hidden";
}

function invalidateAutomaticStt() {
  if (!autoSttJob) return true;
  const job = autoSttJob;
  autoSttJob = null;
  cancelAutomaticChat(job);
  return activeSpeech?.ownerJob === job ? stopAnswerSpeech() : true;
}

function cancelAutomaticChat(job = null) {
  const request = activeChatRequest;
  if (!request || request.ownerJob !== job) return;
  // 먼저 요청을 무효화해 abort를 무시한 응답이나 이전 finally도 새 요청을 건드리지 못합니다.
  activeChatRequest = null;
  inFlight = false;
  request.userMessage?.remove();
  try { request.controller?.abort(); } catch { /* 취소 실패 시에도 응답은 무효입니다. */ }
  updateRecordingControls();
  showNotice("");
}

async function runAutomaticStt(job) {
  // 같은 완료 신호에 처리 함수를 다시 연결해도 진행 중 작업을 풀거나 재전송하지 않습니다.
  if (job.started) return { ok: false, text: null, error: "busy" };
  job.started = true;
  try {
    const completed = await job.session.completion;
    if (!isCurrentAutomaticStt(job)) return { ok: false, text: null, error: "cancelled" };
    if (!completed.ok || !(completed.blob instanceof Blob) || !completed.blob.size || recordedBlob !== completed.blob) {
      // 녹음 함수가 표시한 권한·빈 녹음 등의 안내를 유지합니다.
      return { ok: false, text: null, error: completed.error || "no_recording" };
    }
    job.blob = completed.blob;
    if (job.attempted) return { ok: false, text: null, error: "busy" };
    job.attempted = true;
    job.phase = "transcribing";
    showAutoSttStatus("음성을 변환하는 중...");
    updateRecordingControls();
    const result = await transcribeLocalRecording(job);
    if (!isCurrentAutomaticStt(job) || recordedBlob !== job.blob) return { ok: false, text: null, error: "cancelled" };
    if (!result.ok || typeof result.text !== "string" || !result.text.trim()) {
      // STT가 표시한 구체적인 안전한 오류 안내를 유지합니다.
      return result.ok ? { ok: false, text: null, error: "empty_transcription" } : result;
    }
    const text = result.text.trim();
    if (Array.from(text).length > MAX_USER_MESSAGE_LENGTH) {
      showAutoSttStatus("인식된 문장이 6,000자를 넘어요. 더 짧게 녹음해 주세요.", true);
      return { ok: false, text, error: "message_too_long" };
    }
    if (messageInput.value.length > 0) {
      showAutoSttStatus("작성 중인 채팅 문장을 보존하기 위해 자동 전송을 중단했어요.", true);
      return { ok: false, text, error: "draft_present" };
    }
    if (job.chatAttempted) return { ok: false, text, error: "busy" };
    job.chatAttempted = true;
    job.phase = "chatting";
    showAutoSttStatus("AI 답변 기다리는 중...");
    updateRecordingControls();
    const chat = await sendChatMessage(text, job);
    if (!isCurrentAutomaticStt(job)) return { ok: false, text: null, error: "cancelled" };
    if (!chat.ok || typeof chat.reply !== "string" || !chat.reply.trim()) {
      showAutoSttStatus(connectionNotice.textContent || "AI 답변을 받지 못했어요. 다시 시도해 주세요.", true);
      return { ok: false, text, error: chat.error || "invalid_response" };
    }
    job.phase = "speaking";
    showAutoSttStatus("AI 답변을 읽고 있어요.");
    updateRecordingControls();
    const speech = await playAutomaticAnswer(job, chat.reply);
    if (!isCurrentAutomaticStt(job)) return { ok: false, text: null, error: "cancelled" };
    if (speech.ok) showAutoSttStatus("다음 대화는 마이크를 눌러 주세요.");
    else if (speech.error === "cancelled") showAutoSttStatus("읽기를 멈췄어요.");
    // 음성만 실패해도 이미 성공한 채팅과 이력은 그대로 둡니다.
    return { ...result, reply: chat.reply, speech };
  } catch {
    if (isCurrentAutomaticStt(job)) showAutoSttStatus("대화를 이어가지 못했어요. 다시 녹음해 주세요.", true);
    return { ok: false, text: null, error: "request_failed" };
  } finally {
    if (autoSttJob === job) {
      autoSttJob = null;
      recordedBlob = null;
      job.blob = null;
      updateRecordingControls();
    }
  }
}

function clearLocalRecording(message = "취소했어요. 마이크를 눌러 다시 시작하세요.") {
  if (activeRecording) discardRecordingSession(activeRecording);
  releaseRecordingData();
  cancelAutomaticChat();
  stopAnswerSpeech();
  recordingTime.textContent = "00:00 / 03:00";
  showAutoSttStatus("");
  showRecordingStatus(message);
  updateRecordingControls();
}

// AI 답변만 읽으며 자동 읽기는 방금 완료된 음성 대화의 답변에만 적용합니다.
const speechStatus = document.querySelector("#speech-status");
const speechRateSelect = document.querySelector("#speech-rate");
// 브라우저가 이전 폼 값을 복원해도 새 화면의 기본 속도는 1.0배입니다.
speechRateSelect.value = "1.0";
const speechSynth = window.speechSynthesis;
const speechSupported = Boolean(speechSynth
  && typeof speechSynth.speak === "function"
  && typeof speechSynth.cancel === "function"
  && typeof SpeechSynthesisUtterance === "function");
const speechEntries = new WeakMap();
let speechVoices = [];
let activeSpeech = null;
let speechCancelPending = false;
const SPEECH_START_TIMEOUT_MS = 5000;
const AUTOMATIC_SPEECH_FALLBACK = "자동 읽기를 완료하지 못했어요. 답변 듣기를 눌러 주세요.";

async function playAutomaticAnswer(job, reply) {
  if (job.speechAttempted) return { ok: false, error: "busy" };
  job.speechAttempted = true;
  if (!isCurrentAutomaticStt(job)) return { ok: false, error: "cancelled" };
  const entry = job.answerEntry;
  if (!entry || entry.text !== reply.trim() || entry.controls.isConnected === false) {
    showAutoSttStatus(AUTOMATIC_SPEECH_FALLBACK, true);
    return { ok: false, error: "missing_answer" };
  }
  const started = speakAnswer(entry, job);
  const result = started.ok ? await started.completion : { ok: false, error: started.error };
  if (!isCurrentAutomaticStt(job)) return { ok: false, error: "cancelled" };
  if (!result.ok && result.error !== "cancelled") showAutoSttStatus(AUTOMATIC_SPEECH_FALLBACK, true);
  return result;
}

function showSpeechStatus(message, isError = false) {
  speechStatus.textContent = message;
  speechStatus.classList.toggle("is-error", isError);
}

function selectedSpeechRate() {
  switch (speechRateSelect.value) {
    case "0.8": return 0.8;
    case "1.2": return 1.2;
    default: return 1.0;
  }
}

function refreshSpeechVoices() {
  try {
    speechVoices = speechSupported && typeof speechSynth.getVoices === "function"
      ? Array.from(speechSynth.getVoices()) : [];
  } catch {
    // 목록을 얻지 못해도 en-US의 브라우저 기본 음성으로 시도합니다.
    speechVoices = [];
  }
}

function chooseEnglishVoice() {
  refreshSpeechVoices();
  const language = (voice) => String(voice.lang ?? "").replace(/_/g, "-").toLowerCase();
  const english = speechVoices.filter((voice) => /^en(?:-|$)/.test(language(voice)));
  const american = english.filter((voice) => language(voice) === "en-us");
  const preferred = (voices) => voices.find((voice) => voice.default)
    || voices.find((voice) => voice.localService) || voices[0];
  return preferred(american) || preferred(english) || null;
}

function updateSpeechButtons() {
  const microphoneBusy = activeRecording !== null;
  chatMessages.querySelectorAll(".ai-message .message-content").forEach((content) => {
    const entry = speechEntries.get(content);
    if (!entry) return;
    const reading = activeSpeech?.entry === entry;
    entry.playButton.disabled = !speechSupported || microphoneBusy || reading;
    entry.playButton.textContent = reading ? "읽는 중..." : "답변 듣기";
    entry.playButton.setAttribute("aria-pressed", String(reading));
    entry.stopButton.disabled = !reading;
    entry.controls.classList.toggle("is-speaking", reading);
  });
}

function detachSpeechEvents(session) {
  clearTimeout(session.startTimeout);
  session.startTimeout = null;
  session.utterance.onstart = null;
  session.utterance.onend = null;
  session.utterance.onerror = null;
}

function settleSpeechCompletion(session, result) {
  const resolve = session.resolveCompletion;
  if (!resolve) return;
  session.resolveCompletion = null;
  resolve(result);
}

function finishAnswerSpeech(session, message, isError = false, error = null) {
  if (activeSpeech !== session) return;
  const validOwner = !session.ownerJob || isCurrentAutomaticStt(session.ownerJob);
  activeSpeech = null;
  detachSpeechEvents(session);
  updateRecordingControls();
  if (validOwner) showSpeechStatus(session.ownerJob && isError ? AUTOMATIC_SPEECH_FALLBACK : message, isError);
  settleSpeechCompletion(session, { ok: validOwner && !isError, error: validOwner ? error : "cancelled" });
}

function stopAnswerSpeech(message = "", cancelQueued = false, completionError = "cancelled") {
  const previous = activeSpeech;
  // cancel()이 즉시 종료 이벤트를 보내도 이전 발화가 새 상태를 건드리지 못합니다.
  activeSpeech = null;
  if (previous) detachSpeechEvents(previous);
  updateRecordingControls();
  if (!speechSupported || (!previous && !cancelQueued && !speechCancelPending)) {
    if (previous) settleSpeechCompletion(previous, { ok: false, error: completionError });
    return true;
  }
  try {
    speechSynth.cancel();
    speechCancelPending = false;
  } catch {
    speechCancelPending = true;
    showSpeechStatus("음성 읽기를 멈추지 못했어요. 페이지를 새로고침한 뒤 다시 시도해 주세요.", true);
    if (previous) settleSpeechCompletion(previous, { ok: false, error: "speech_cancel_failed" });
    return false;
  }
  if (previous) settleSpeechCompletion(previous, { ok: false, error: completionError });
  if (previous && message) showSpeechStatus(message);
  return true;
}

function speakAnswer(entry, ownerJob = null) {
  if (ownerJob && !isCurrentAutomaticStt(ownerJob)) return { ok: false, completion: null, error: "cancelled" };
  if (!speechSupported) {
    showSpeechStatus("이 브라우저는 답변 읽기를 지원하지 않아요. 다른 브라우저에서 시도해 주세요.", true);
    return { ok: false, completion: null, error: "unsupported_browser" };
  }
  if (activeRecording) {
    showSpeechStatus("녹음을 마친 뒤 답변 듣기를 눌러 주세요.", true);
    return { ok: false, completion: null, error: "microphone_busy" };
  }
  if (document.visibilityState === "hidden" || entry.controls.isConnected === false) return { ok: false, completion: null, error: "cancelled" };
  if (!stopAnswerSpeech("", true)) return { ok: false, completion: null, error: "speech_cancel_failed" };
  let session = null;
  try {
    const utterance = new SpeechSynthesisUtterance(entry.text);
    utterance.lang = "en-US";
    // 새 발화를 만들 때만 읽으므로 선택을 바꿔도 현재 재생은 변하지 않습니다.
    utterance.rate = selectedSpeechRate();
    const voice = chooseEnglishVoice();
    if (voice) utterance.voice = voice;
    session = { entry, utterance, ownerJob, started: false, startTimeout: null, completion: null, resolveCompletion: null };
    session.completion = new Promise((resolve) => { session.resolveCompletion = resolve; });
    activeSpeech = session; // 발화 객체를 끝날 때까지 보관합니다.
    utterance.onstart = () => {
      if (activeSpeech !== session) return;
      if (ownerJob && !isCurrentAutomaticStt(ownerJob)) { stopAnswerSpeech(); return; }
      session.started = true;
      clearTimeout(session.startTimeout);
      session.startTimeout = null;
      showSpeechStatus("AI 답변을 영어로 읽고 있어요.");
    };
    utterance.onend = () => finishAnswerSpeech(session, "답변 읽기가 끝났어요. 다시 들을 수 있어요.");
    utterance.onerror = (event) => finishAnswerSpeech(session,
      "답변을 읽지 못했어요. 기기의 영어 음성 설정을 확인하거나 다시 시도해 주세요.", true,
      event?.error === "not-allowed" ? "not_allowed" : "speech_failed");
    if (ownerJob) {
      session.startTimeout = setTimeout(() => {
        if (activeSpeech !== session || session.started) return;
        const validOwner = isCurrentAutomaticStt(ownerJob);
        stopAnswerSpeech(validOwner ? AUTOMATIC_SPEECH_FALLBACK : "", true, validOwner ? "start_timeout" : "cancelled");
      }, SPEECH_START_TIMEOUT_MS);
    }
    updateRecordingControls();
    showSpeechStatus("영어 답변 읽기를 준비하고 있어요.");
    speechSynth.speak(utterance);
    return { ok: true, completion: session.completion, error: null };
  } catch {
    const message = "답변을 읽지 못했어요. 기기의 영어 음성 설정을 확인하거나 다시 시도해 주세요.";
    if (session && ownerJob && activeSpeech === session) stopAnswerSpeech(AUTOMATIC_SPEECH_FALLBACK, true, "speech_failed");
    else if (session) finishAnswerSpeech(session, message, true, "speech_failed");
    else showSpeechStatus(message, true);
    return { ok: false, completion: session?.completion ?? null, error: "speech_failed" };
  }
}

function addAnswerSpeechControls(content, text) {
  if (speechEntries.has(content) || typeof text !== "string" || !text.trim()) return;
  const controls = document.createElement("div");
  controls.className = "speech-controls";
  controls.setAttribute("role", "group");
  controls.setAttribute("aria-label", "AI 답변 음성 재생");
  const playButton = document.createElement("button");
  playButton.type = "button";
  playButton.className = "speech-button speech-play-button";
  playButton.textContent = "답변 듣기";
  playButton.disabled = !speechSupported || activeRecording !== null;
  playButton.setAttribute("aria-pressed", "false");
  playButton.setAttribute("aria-describedby", "speech-note speech-status");
  const stopButton = document.createElement("button");
  stopButton.type = "button";
  stopButton.className = "speech-button speech-stop-button";
  stopButton.textContent = "읽기 중지";
  stopButton.disabled = true;
  const entry = { controls, playButton, stopButton, text: text.trim() };
  speechEntries.set(content, entry);
  controls.append(playButton, stopButton);
  content.append(controls);
  playButton.addEventListener("click", () => speakAnswer(entry));
  stopButton.addEventListener("click", () => {
    if (activeSpeech?.entry === entry) stopAnswerSpeech("답변 읽기를 멈췄어요.");
  });
}

function initializeAnswerSpeech() {
  chatMessages.querySelectorAll(".ai-message .message-content").forEach((content) => {
    const text = Array.from(content.querySelectorAll(".message-bubble p"))
      .map((paragraph) => paragraph.textContent.trim()).filter(Boolean).join("\n");
    addAnswerSpeechControls(content, text);
  });
  refreshSpeechVoices();
  if (speechSupported && typeof speechSynth.addEventListener === "function") {
    try {
      speechSynth.addEventListener("voiceschanged", refreshSpeechVoices);
    } catch {
      // 이벤트 등록이 불가능해도 듣기 버튼을 누를 때 목록을 다시 확인합니다.
    }
  }
  if (!speechSupported) {
    showSpeechStatus("이 브라우저는 답변 읽기를 지원하지 않아요. 다른 브라우저에서 시도해 주세요.", true);
  }
  // 인증 만료 등으로 답변이 화면에서 제거되면 읽기도 중지합니다.
  if (typeof MutationObserver === "function") {
    const observer = new MutationObserver(() => {
      if (activeSpeech?.entry.controls.isConnected === false) stopAnswerSpeech();
    });
    observer.observe(chatMessages, { childList: true });
  }
}

function showTranscriptionStatus(message, isError = false) {
  showAutoSttStatus(message, isError);
}

function clearTranscription(keepAutomaticJob = null) {
  if (autoSttJob && autoSttJob !== keepAutomaticJob) invalidateAutomaticStt();
  const previous = transcriptionRequest;
  transcriptionRequest = null;
  if (previous) {
    clearTimeout(previous.timeout);
    try { previous.controller.abort(); } catch { /* 응답은 이미 무효화했습니다. */ }
  }
  transcriptionLogin.hidden = true;
  showTranscriptionStatus("");
  updateRecordingControls();
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

// 녹음 결과를 한 번 변환하고 자동 대화 호출자에게 안전한 결과만 반환합니다.
/**
 * @returns {Promise<{ok: boolean, text: string|null, error: string|null}>}
 */
async function transcribeLocalRecording(ownerJob = null) {
  if (ownerJob && (!isCurrentAutomaticStt(ownerJob) || recordedBlob !== ownerJob.blob)) {
    return { ok: false, text: null, error: "cancelled" };
  }
  if (activeRecording || transcriptionRequest || (autoSttJob && autoSttJob !== ownerJob)) {
    return { ok: false, text: null, error: "busy" };
  }
  if (!recordedBlob || !recordedBlob.size) {
    showTranscriptionStatus("먼저 목소리를 녹음하고 녹음을 종료해 주세요.", true);
    return { ok: false, text: null, error: "no_recording" };
  }
  if (recordedBlob.size > MAX_TRANSCRIPTION_BYTES) {
    showTranscriptionStatus(transcriptionErrorMessage(413), true);
    return { ok: false, text: null, error: "file_too_large" };
  }
  const filename = recordingUploadFilename(recordedBlob);
  if (!filename) {
    showTranscriptionStatus("이 녹음의 형식을 확인할 수 없거나 지원하지 않아요. 다른 브라우저에서 다시 녹음해 주세요.", true);
    return { ok: false, text: null, error: "unsupported_format" };
  }
  if (typeof FormData !== "function" || typeof fetch !== "function" || typeof AbortController !== "function") {
    showTranscriptionStatus("이 브라우저는 음성 변환 요청을 지원하지 않아요. 브라우저를 업데이트해 주세요.", true);
    return { ok: false, text: null, error: "unsupported_browser" };
  }

  clearTranscription(ownerJob);
  const request = { blob: recordedBlob, controller: new AbortController(), timeout: null, timedOut: false };
  if (ownerJob) ownerJob.request = request;
  // 첫 await 전에 요청 상태를 저장해 빠른 연속 클릭도 한 번만 전송합니다.
  transcriptionRequest = request;
  const isCurrent = () => transcriptionRequest === request && recordedBlob === request.blob
    && (!ownerJob || isCurrentAutomaticStt(ownerJob));
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
    if (!isCurrent()) return { ok: false, text: null, error: "cancelled" };
    receivedResponse = true;
    if (request.timedOut) {
      showTranscriptionStatus("변환 응답이 늦어 요청을 중단했어요. 잠시 후 다시 시도해 주세요.", true);
      return { ok: false, text: null, error: "timeout" };
    }
    if (!response.ok) {
      showTranscriptionStatus(transcriptionErrorMessage(response.status), true);
      transcriptionLogin.hidden = response.status !== 401;
      // 서버 오류 본문이나 내부 예외 대신 정해진 코드만 반환합니다.
      const errorCodes = {
        400: "invalid_audio", 401: "unauthorized", 403: "forbidden",
        413: "file_too_large", 415: "unsupported_format", 429: "rate_limited",
        502: "transcription_failed", 503: "service_unavailable",
      };
      return { ok: false, text: null, error: errorCodes[response.status] || "request_failed" };
    }
    const data = await response.json();
    if (!isCurrent()) return { ok: false, text: null, error: "cancelled" };
    if (request.timedOut) {
      showTranscriptionStatus("변환 응답이 늦어 요청을 중단했어요. 잠시 후 다시 시도해 주세요.", true);
      return { ok: false, text: null, error: "timeout" };
    }
    if (!data || typeof data.text !== "string" || !data.text.trim()) {
      showTranscriptionStatus("변환된 영어 문장을 받지 못했어요. 다시 녹음하거나 잠시 후 다시 시도해 주세요.", true);
      return { ok: false, text: null, error: typeof data?.text === "string" ? "empty_transcription" : "invalid_response" };
    }
    const text = data.text.trim();
    // 문장 표시와 이력 저장은 공통 채팅 함수가 담당합니다.
    showTranscriptionStatus("음성을 인식했어요.");
    return { ok: true, text, error: null };
  } catch (error) {
    if (!isCurrent()) return { ok: false, text: null, error: "cancelled" };
    if (request.timedOut || error?.name === "AbortError") {
      showTranscriptionStatus("변환 응답이 늦어 요청을 중단했어요. 잠시 후 다시 시도해 주세요.", true);
      return { ok: false, text: null, error: request.timedOut ? "timeout" : "cancelled" };
    } else if (!receivedResponse) {
      showTranscriptionStatus("네트워크 연결 오류로 변환 결과를 받지 못했어요. 인터넷 연결을 확인한 뒤 다시 시도해 주세요.", true);
      return { ok: false, text: null, error: "network_error" };
    } else {
      showTranscriptionStatus("서버의 변환 응답을 읽을 수 없어요. 잠시 후 다시 시도해 주세요.", true);
      return { ok: false, text: null, error: "invalid_response" };
    }
  } finally {
    clearTimeout(request.timeout);
    if (ownerJob?.request === request) ownerJob.request = null;
    if (transcriptionRequest === request) {
      transcriptionRequest = null;
      updateRecordingControls();
    }
  }
}

recordToggleButton.addEventListener("click", toggleLocalRecording);
cancelAutoSttButton.addEventListener("click", () => clearLocalRecording());
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden" && (activeRecording || recordedBlob || autoSttJob || activeSpeech || inFlight)) {
    clearLocalRecording("화면이 숨겨져 대화를 중단했어요.");
  }
});
window.addEventListener("pagehide", () => clearLocalRecording());
document.querySelector(".logout-form")?.addEventListener("submit", () => clearLocalRecording());
initializeAnswerSpeech();
updateRecordingControls();

// STEP 4-1B: 수동 전송과 향후 음성 대화가 같은 검증·요청·이력 처리를 사용합니다.
// 실패 사유에는 내부 예외나 서버 응답 본문을 넣지 않습니다.
/**
 * @param {string} text 전송할 사용자 문장
 * @param {object|null} ownerJob 자동 전송을 시작한 내부 작업. 수동 호출에서는 생략합니다.
 * @returns {Promise<{ok: boolean, reply: string|null, error: string|null}>}
 */
async function sendChatMessage(text, ownerJob = null) {
  if (ownerJob && !isCurrentAutomaticStt(ownerJob)) return { ok: false, reply: null, error: "cancelled" };
  if (autoSttJob && autoSttJob !== ownerJob) return { ok: false, reply: null, error: "busy" };
  if (inFlight) return { ok: false, reply: null, error: "busy" };
  if (ownerJob && messageInput.value.length > 0) return { ok: false, reply: null, error: "draft_present" };

  if (typeof text !== "string") {
    showNotice("영어 문장을 입력해 주세요.", true);
    messageInput.focus();
    return { ok: false, reply: null, error: "invalid_input" };
  }
  const message = text.trim();
  if (!message) {
    showNotice("영어 문장을 입력해 주세요.", true);
    messageInput.focus();
    return { ok: false, reply: null, error: "empty_message" };
  }
  if (Array.from(message).length > MAX_USER_MESSAGE_LENGTH) {
    showNotice(`문장은 ${MAX_USER_MESSAGE_LENGTH.toLocaleString()}자 이내로 입력해 주세요.`, true);
    messageInput.focus();
    return { ok: false, reply: null, error: "message_too_long" };
  }

  // 첫 await 전에 잠가서 Enter와 클릭이 겹쳐도 한 번만 요청합니다.
  // 진행 중에 선택을 바꿔도 이 요청은 시작 시점의 설정을 끝까지 사용합니다.
  const requestSettings = { ...learningSettings };
  const previousTopic = lastSuccessfulTopic;
  const request = { ownerJob, controller: null, userMessage: null };
  activeChatRequest = request;
  inFlight = true;
  updateRecordingControls();
  showNotice("Loading... AI가 답변을 준비하고 있어요.");
  const userMessage = appendMessage("user", message);
  request.userMessage = userMessage;
  const isCurrent = () => activeChatRequest === request && (!ownerJob || isCurrentAutomaticStt(ownerJob));
  // 새 문장은 임시 요청에만 추가하고, 답변에 성공한 뒤 이력에 저장합니다.
  const candidateMessages = [
    ...conversationHistory,
    { role: "user", content: message },
  ];

  try {
    if (typeof AbortController === "function") request.controller = new AbortController();
    const response = await fetch("/api/chat", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CSRF-Token": csrfToken,
      },
      body: JSON.stringify({ messages: candidateMessages, settings: requestSettings, previous_topic: previousTopic }),
      ...(request.controller ? { signal: request.controller.signal } : {}),
    });
    if (!isCurrent()) {
      userMessage.remove();
      return { ok: false, reply: null, error: "cancelled" };
    }

    if (response.status === 401 || response.status === 403) {
      conversationHistory = [];
      lastSuccessfulTopic = null;
      messageInput.value = "";
      chatMessages.replaceChildren();
      window.location.replace(response.status === 401 ? "/login" : "/");
      return { ok: false, reply: null, error: response.status === 401 ? "unauthorized" : "forbidden" };
    }

    if (response.status === 429) {
      userMessage.remove();
      showNotice("요청 횟수 제한에 도달했어요. 잠시 후 다시 시도해 주세요.", true);
      return { ok: false, reply: null, error: "rate_limited" };
    }

    if (!response.ok) throw new Error("Chat request failed");
    const data = await response.json();
    if (!isCurrent()) {
      userMessage.remove();
      return { ok: false, reply: null, error: "cancelled" };
    }
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
    lastSuccessfulTopic = requestSettings.topic;
    if (!ownerJob) messageInput.value = "";
    showNotice(ownerJob ? "" : "답변이 도착했어요.");
    return { ok: true, reply, error: null };
  } catch {
    if (!isCurrent()) {
      userMessage.remove();
      return { ok: false, reply: null, error: "cancelled" };
    }
    // 이력은 바꾸지 않습니다. 실패한 문장은 입력창에 남겨 다시 보낼 수 있습니다.
    userMessage.remove();
    showNotice("답변을 받지 못했어요. 잠시 후 Send를 눌러 다시 시도해 주세요.", true);
    return { ok: false, reply: null, error: "request_failed" };
  } finally {
    if (activeChatRequest === request) {
      activeChatRequest = null;
      inFlight = false;
      updateRecordingControls();
      if (!ownerJob) messageInput.focus();
    }
  }
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await sendChatMessage(messageInput.value);
});
