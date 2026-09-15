const message = new URLSearchParams(location.search).get("message");
if (message) {
  document.getElementById("startup").classList.add("error");
  document.getElementById("title").textContent = "엔진을 시작하지 못했습니다";
  document.getElementById("message").textContent =
    `${message}\n\n터미널에서 npm run doctor 로 환경을 확인하세요.`;
  const retry = document.getElementById("retry");
  retry.hidden = false;
  retry.addEventListener("click", () => {
    retry.disabled = true;
    window.assetsStudio?.retryEngine();
  });
}
