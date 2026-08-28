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
const searchForm = document.querySelector("#search-form");
const searchInput = document.querySelector("#search-input");
const searchButton = document.querySelector("#search-button");
const clearSearchButton = document.querySelector("#clear-search-button");
const searchStatus = document.querySelector("#search-status");
const searchExamples = document.querySelectorAll("[data-search-example]");
const listKicker = document.querySelector("#list-kicker");
const listTitle = document.querySelector("#list-title");

let currentVideo = null;
let analysisRunning = false;
let searchRunning = false;

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
  document.querySelectorAll(".frame-card.active, .search-result.active").forEach((item) => item.classList.remove("active"));
  card.classList.add("active");
  player.currentTime = timestampSeconds;
  player.play().catch(() => {});
}

function renderPlayer(video) {
  if (!video) return;
  currentVideo = video;
  videoTitle.textContent = video.original_name;
  durationLabel.textContent = formatTime(video.duration_seconds);
  if (player.getAttribute("src") !== video.video_url) {
    player.src = video.video_url;
  }
  player.hidden = false;
  emptyState.hidden = true;
  renderVisionControls(video);
}

function playAt(timestampSeconds) {
  const seekAndPlay = () => {
    player.currentTime = Number(timestampSeconds) || 0;
    player.play().catch(() => {});
  };
  if (player.readyState >= 1) {
    seekAndPlay();
  } else {
    player.addEventListener("loadedmetadata", seekAndPlay, { once: true });
  }
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
  listKicker.textContent = "时间点索引";
  listTitle.textContent = "关键帧";
  frameCount.textContent = `${video.frames.length} 张`;
  renderPlayer(video);
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

const searchFieldLabels = {
  subjects: "主体",
  actions: "动作",
  scene: "场景",
  shot_type: "镜头",
  ocr_text: "OCR",
  summary: "摘要",
  video_name: "文件名",
  time_text: "时间点",
};

function appendSearchField(container, label, values) {
  if (!Array.isArray(values) || values.length === 0) return;
  const row = document.createElement("p");
  row.className = "search-result-field";
  const name = document.createElement("span");
  name.textContent = `${label}：`;
  row.append(name, document.createTextNode(values.join("、")));
  container.append(row);
}

async function openSearchResult(card, result) {
  document.querySelectorAll(".search-result.active").forEach((item) => item.classList.remove("active"));
  card.classList.add("active");
  setStatus(searchStatus, `正在载入 ${result.video_name} 的 ${formatTime(result.timestamp, true)}…`, "working");
  try {
    const video = await requestJson(`/api/videos/${encodeURIComponent(result.video_id)}`);
    renderPlayer(video);
    playAt(result.timestamp);
    setStatus(searchStatus, `已定位到 ${result.video_name} · ${formatTime(result.timestamp, true)}`, "success");
  } catch (error) {
    setStatus(searchStatus, error.message || "视频载入失败，请重试。", "error");
  }
}

function renderSearchResults(response) {
  listKicker.textContent = `检索“${response.query}”`;
  listTitle.textContent = "搜索结果";
  frameCount.textContent = `${response.count} 条`;
  framesList.replaceChildren();

  if (response.results.length === 0) {
    const empty = document.createElement("div");
    empty.className = "search-empty";
    const title = document.createElement("strong");
    title.textContent = "没有找到相关画面";
    const hint = document.createElement("span");
    hint.textContent = "可尝试更短的关键词，或先对视频完成 AI 画面识别。";
    empty.append(title, hint);
    framesList.append(empty);
    return;
  }

  response.results.forEach((result) => {
    const card = document.createElement("article");
    card.className = "search-result";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "search-result-button";
    button.setAttribute("aria-label", `播放 ${result.video_name} ${formatTime(result.timestamp, true)} 的画面`);

    const image = document.createElement("img");
    image.src = result.thumbnail_url;
    image.alt = `${result.video_name} ${formatTime(result.timestamp, true)} 的关键帧`;
    image.loading = "lazy";

    const body = document.createElement("div");
    body.className = "search-result-body";
    const meta = document.createElement("div");
    meta.className = "search-result-meta";
    const source = document.createElement("span");
    source.textContent = `${result.video_name} · ${formatTime(result.timestamp, true)}`;
    const score = document.createElement("strong");
    score.textContent = `相关度 ${result.score}`;
    meta.append(source, score);

    const summary = document.createElement("p");
    summary.className = "search-result-summary";
    summary.textContent = result.summary || "该画面暂无摘要";

    const tags = document.createElement("div");
    tags.className = "search-match-tags";
    result.matched_fields.forEach((field) => {
      const tag = document.createElement("span");
      tag.textContent = searchFieldLabels[field] || field;
      tags.append(tag);
    });

    const details = document.createElement("div");
    details.className = "search-result-details";
    appendSearchField(details, "主体", result.subjects);
    appendSearchField(details, "动作", result.actions);
    appendSearchField(details, "场景", result.scene);
    appendSearchField(details, "镜头", result.shot_type);
    appendSearchField(details, "OCR", result.ocr_text);

    const reason = document.createElement("p");
    reason.className = "search-match-reason";
    reason.textContent = result.match_reason;
    body.append(meta, summary, tags, details, reason);
    button.append(image, body);
    button.addEventListener("click", () => openSearchResult(card, result));
    card.append(button);
    framesList.append(card);
  });
}

async function runSearch() {
  if (searchRunning) return;
  const query = searchInput.value.trim();
  if (query.length < 2) {
    setStatus(searchStatus, "请输入至少 2 个字符。", "error");
    searchInput.focus();
    return;
  }

  searchRunning = true;
  searchButton.disabled = true;
  searchButton.textContent = "搜索中…";
  setStatus(searchStatus, "正在检索已保存的 AI 画面索引…", "working");
  try {
    const response = await requestJson(`/api/search?q=${encodeURIComponent(query)}&limit=20`);
    renderSearchResults(response);
    clearSearchButton.hidden = false;
    const backend = response.backend === "fts5" ? "全文索引" : "兼容检索";
    setStatus(searchStatus, `找到 ${response.count} 条结果 · ${response.elapsed_ms}ms · ${backend}`, "success");
  } catch (error) {
    setStatus(searchStatus, error.message || "搜索失败，请重试。", "error");
  } finally {
    searchRunning = false;
    searchButton.disabled = false;
    searchButton.textContent = "搜索画面";
  }
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
    searchInput.value = "";
    clearSearchButton.hidden = true;
    setStatus(searchStatus, "");
    renderVideo(result);
    setStatus(uploadStatus, `索引完成：已提取 ${result.frames.length} 张关键帧。`, "success");
  } catch (error) {
    setStatus(uploadStatus, error.message || "上传失败，请重试。", "error");
  } finally {
    uploadButton.disabled = false;
    fileInput.disabled = false;
  }
});

searchForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runSearch();
});
searchExamples.forEach((button) => {
  button.addEventListener("click", () => {
    searchInput.value = button.dataset.searchExample;
    runSearch();
  });
});
clearSearchButton.addEventListener("click", () => {
  searchInput.value = "";
  clearSearchButton.hidden = true;
  setStatus(searchStatus, "");
  if (currentVideo) renderVideo(currentVideo);
  searchInput.focus();
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
