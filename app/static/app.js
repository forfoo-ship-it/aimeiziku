const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#video-file");
const fileLabel = document.querySelector("#file-label");
const uploadButton = document.querySelector("#upload-button");
const uploadStatus = document.querySelector("#upload-status");
const framesList = document.querySelector("#frames-list");
const frameCount = document.querySelector("#frame-count");
const player = document.querySelector("#video-player");
const emptyState = document.querySelector("#empty-state");
const videoTitle = document.querySelector("#video-title");
const durationLabel = document.querySelector("#duration");
const currentPosition = document.querySelector("#current-position");

function formatTime(seconds, milliseconds = false) {
  const safeSeconds = Math.max(0, Number(seconds) || 0);
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const wholeSeconds = Math.floor(safeSeconds % 60);
  const ms = Math.floor((safeSeconds - Math.floor(safeSeconds)) * 1000);
  const base = hours > 0
    ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}`
    : `${String(minutes).padStart(2, "0")}:${String(wholeSeconds).padStart(2, "0")}`;
  return milliseconds ? `${base}.${String(ms).padStart(3, "0")}` : base;
}

function setStatus(message, type = "") {
  uploadStatus.textContent = message;
  uploadStatus.className = `status ${type}`.trim();
}

function selectFrame(button, timestampSeconds) {
  document.querySelectorAll(".frame-card.active").forEach((card) => card.classList.remove("active"));
  button.classList.add("active");
  player.currentTime = timestampSeconds;
  player.play().catch(() => {});
}

function renderVideo(video) {
  if (!video) return;

  videoTitle.textContent = video.original_name;
  durationLabel.textContent = formatTime(video.duration_seconds);
  frameCount.textContent = `${video.frames.length} 张`;
  player.src = video.video_url;
  player.hidden = false;
  emptyState.hidden = true;
  framesList.replaceChildren();

  video.frames.forEach((frame, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "frame-card";
    button.setAttribute("aria-label", `跳转到 ${formatTime(frame.timestamp_seconds)}`);
    button.innerHTML = `
      <img src="${frame.image_url}" alt="${formatTime(frame.timestamp_seconds)} 的视频画面" loading="lazy">
      <span class="frame-meta">
        <span class="frame-number">画面 ${String(index + 1).padStart(2, "0")}</span>
        <strong>${formatTime(frame.timestamp_seconds, true)}</strong>
      </span>
    `;
    button.addEventListener("click", () => selectFrame(button, frame.timestamp_seconds));
    framesList.append(button);
  });
}

fileInput.addEventListener("change", () => {
  fileLabel.textContent = fileInput.files[0]?.name || "选择 MP4 视频";
  setStatus("");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;

  uploadButton.disabled = true;
  fileInput.disabled = true;
  setStatus("正在上传并提取关键帧，请稍候……", "working");

  try {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch("/api/videos", { method: "POST", body });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "视频处理失败。");
    renderVideo(result);
    setStatus(`索引完成：已提取 ${result.frames.length} 张关键帧。`, "success");
  } catch (error) {
    setStatus(error.message || "上传失败，请重试。", "error");
  } finally {
    uploadButton.disabled = false;
    fileInput.disabled = false;
  }
});

player.addEventListener("timeupdate", () => {
  currentPosition.textContent = formatTime(player.currentTime, true);
});

renderVideo(window.__INITIAL_VIDEO__);

