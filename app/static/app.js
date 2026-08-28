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
const visionPanel = document.querySelector("#vision-panel");
const visionButton = document.querySelector("#vision-button");
const reanalyzeButton = document.querySelector("#reanalyze-button");
const visionProgress = document.querySelector("#vision-progress");
const visionStatus = document.querySelector("#vision-status");

let currentVideo = null;
let analysisRunning = false;

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

function setStatus(element, message, type = "") {
  element.textContent = message;
  element.className = `status ${type}`.trim();
}

function selectFrame(card, timestampSeconds) {
  document.querySelectorAll(".frame-card.active").forEach((item) => item.classList.remove("active"));
  card.classList.add("active");
  player.currentTime = timestampSeconds;
  player.play().catch(() => {});
}

function appendVisionField(container, label, values) {
  if (!Array.isArray(values) || values.length === 0) return;
  const row = document.createElement("div");
  row.className = "vision-field";
  const title = document.createElement("span");
  title.textContent = label;
  const value = document.createElement("p");
  value.textContent = values.join("、");
  row.append(title, value);
  container.append(row);
}

function renderVisionResult(frame) {
  const container = document.createElement("div");
  container.className = `vision-result ${frame.vision_status}`;
  const status = document.createElement("span");
  status.className = "vision-status-badge";
  const statusLabels = {
    pending: "待识别",
    processing: "识别中",
    success: "已识别",
    failed: "识别失败",
  };
  status.textContent = statusLabels[frame.vision_status] || "待识别";
  container.append(status);

  if (frame.vision_status === "success" && frame.vision_result) {
    const summary = document.createElement("p");
    summary.className = "vision-summary";
    summary.textContent = frame.vision_result.summary;
    container.append(summary);
    appendVisionField(container, "主体", frame.vision_result.subjects);
    appendVisionField(container, "动作", frame.vision_result.actions);
    appendVisionField(container, "场景", frame.vision_result.scene);
    appendVisionField(container, "镜头", frame.vision_result.shot_type);
    appendVisionField(container, "OCR", frame.vision_result.ocr_text);

    const meta = document.createElement("p");
    meta.className = "vision-meta";
    const confidence = Math.round((frame.vision_result.confidence || 0) * 100);
    const duration = frame.vision_duration_ms ? ` · ${frame.vision_duration_ms}ms` : "";
    meta.textContent = `置信度 ${confidence}%${duration}`;
    container.append(meta);
  } else if (frame.vision_status === "failed") {
    const error = document.createElement("p");
    error.className = "vision-error";
    error.textContent = frame.vision_error || "该帧识别失败，可再次尝试。";
    container.append(error);
  }
  return container;
}

function renderVisionControls(video) {
  const progress = video.vision_progress;
  visionPanel.hidden = false;
  visionProgress.textContent = `已完成 ${progress.completed}/${progress.total} 帧 · 成功 ${progress.success} · 失败 ${progress.failed}`;
  const allSuccessful = progress.total > 0 && progress.success === progress.total;
  visionButton.textContent = progress.failed > 0 ? "重试失败画面" : (allSuccessful ? "识别已完成" : "AI识别画面");
  visionButton.disabled = analysisRunning || allSuccessful;
  reanalyzeButton.hidden = progress.success === 0;
  reanalyzeButton.disabled = analysisRunning;
}

function renderVideo(video) {
  if (!video) return;
  currentVideo = video;
  videoTitle.textContent = video.original_name;
  durationLabel.textContent = formatTime(video.duration_seconds);
  frameCount.textContent = `${video.frames.length} 张`;
  if (player.getAttribute("src") !== video.video_url) {
    player.src = video.video_url;
  }
  player.hidden = false;
  emptyState.hidden = true;
  framesList.replaceChildren();

  video.frames.forEach((frame, index) => {
    const card = document.createElement("article");
    card.className = "frame-card";
    const seekButton = document.createElement("button");
    seekButton.type = "button";
    seekButton.className = "frame-seek";
    seekButton.setAttribute("aria-label", `跳转到 ${formatTime(frame.timestamp_seconds)}`);
    seekButton.innerHTML = `
      <img src="${frame.image_url}" alt="${formatTime(frame.timestamp_seconds)} 的视频画面" loading="lazy">
      <span class="frame-meta">
        <span class="frame-number">画面 ${String(index + 1).padStart(2, "0")}</span>
        <strong>${formatTime(frame.timestamp_seconds, true)}</strong>
      </span>
    `;
    seekButton.addEventListener("click", () => selectFrame(card, frame.timestamp_seconds));
    card.append(seekButton, renderVisionResult(frame));
    framesList.append(card);
  });
  renderVisionControls(video);
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "请求失败，请重试。");
  return result;
}

async function runVisionAnalysis(force = false) {
  if (!currentVideo || analysisRunning) return;
  analysisRunning = true;
  renderVisionControls(currentVideo);
  setStatus(visionStatus, "正在准备画面识别……", "working");

  try {
    const startedVideo = await requestJson(`/api/videos/${currentVideo.id}/vision/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    });
    renderVideo(startedVideo);

    if (startedVideo.vision_progress.done && startedVideo.vision_progress.failed === 0) {
      setStatus(visionStatus, "所有关键帧已有识别结果，未重复调用 API。", "success");
      return;
    }

    while (true) {
      const response = await requestJson(`/api/videos/${currentVideo.id}/vision/next`, { method: "POST" });
      renderVideo(response.video);
      const progress = response.video.vision_progress;
      setStatus(visionStatus, `正在识别：${progress.completed}/${progress.total} 帧已完成`, "working");
      if (!response.processed || response.done) break;
    }

    const progress = currentVideo.vision_progress;
    const type = progress.failed > 0 ? "error" : "success";
    setStatus(visionStatus, `识别完成：成功 ${progress.success} 帧，失败 ${progress.failed} 帧。`, type);
  } catch (error) {
    setStatus(visionStatus, error.message || "画面识别失败，请重试。", "error");
  } finally {
    analysisRunning = false;
    if (currentVideo) renderVisionControls(currentVideo);
  }
}

fileInput.addEventListener("change", () => {
  fileLabel.textContent = fileInput.files[0]?.name || "选择 MP4 视频";
  setStatus(uploadStatus, "");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;
  uploadButton.disabled = true;
  fileInput.disabled = true;
  setStatus(uploadStatus, "正在上传并提取关键帧，请稍候……", "working");

  try {
    const body = new FormData();
    body.append("file", file);
    const result = await requestJson("/api/videos", { method: "POST", body });
    renderVideo(result);
    setStatus(uploadStatus, `索引完成：已提取 ${result.frames.length} 张关键帧。`, "success");
  } catch (error) {
    setStatus(uploadStatus, error.message || "上传失败，请重试。", "error");
  } finally {
    uploadButton.disabled = false;
    fileInput.disabled = false;
  }
});

visionButton.addEventListener("click", () => runVisionAnalysis(false));
reanalyzeButton.addEventListener("click", () => {
  if (window.confirm("重新分析会再次调用视觉 API，确认继续吗？")) {
    runVisionAnalysis(true);
  }
});
player.addEventListener("timeupdate", () => {
  currentPosition.textContent = formatTime(player.currentTime, true);
});

renderVideo(window.__INITIAL_VIDEO__);

