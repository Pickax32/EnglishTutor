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
