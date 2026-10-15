// HTML의 버튼과 안내 메시지 영역을 찾습니다.
const startButton = document.querySelector("#start-conversation");
const connectionNotice = document.querySelector("#connection-notice");

// 현재 단계에서는 안내만 표시합니다. API 요청이나 마이크 접근은 하지 않습니다.
startButton.addEventListener("click", () => {
  connectionNotice.textContent = "AI 음성 대화는 다음 단계에서 연결됩니다.";
});
