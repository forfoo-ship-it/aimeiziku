const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#video-file");
const fileLabel = document.querySelector("#file-label");
const uploadButton = document.querySelector("#upload-button");
const uploadStatus = document.querySelector("#upload-status");
const framesList = document.querySelector("#frames-list");
const frameCount = document.querySelector("#frame-count");
const player = document.querySelector("#video-player");
const playerShell = document.querySelector(".player-shell");
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
const videoIndexStatus = document.querySelector("#video-index-status");
const folderForm = document.querySelector("#folder-form");
const folderPathInput = document.querySelector("#folder-path");
const folderInterval = document.querySelector("#folder-interval");
const folderAutoAnalyze = document.querySelector("#folder-auto-analyze");
const folderAddButton = document.querySelector("#folder-add-button");
const folderStatus = document.querySelector("#folder-status");
const watchFoldersList = document.querySelector("#watch-folders-list");
const videoLibraryList = document.querySelector("#video-library-list");
const videoLibraryCount = document.querySelector("#video-library-count");
const selectAllVideosButton = document.querySelector("#select-all-videos");
const selectPendingVideosButton = document.querySelector("#select-pending-videos");
const clearVideoSelectionButton = document.querySelector("#clear-video-selection");
const selectedVideoCount = document.querySelector("#selected-video-count");
const batchAnalyzeButton = document.querySelector("#batch-analyze-button");
const batchReanalyzeButton = document.querySelector("#batch-reanalyze-button");
const batchVisionStatus = document.querySelector("#batch-vision-status");
const batchVisionProgress = document.querySelector("#batch-vision-progress");
const batchVisionProgressBar = document.querySelector("#batch-vision-progress-bar");
const adminConsole = document.querySelector("#admin-console");
const adminEntryButton = document.querySelector("#admin-entry-button");
const adminCloseButton = document.querySelector("#admin-close-button");

let currentVideo = null;
let analysisRunning = false;
let searchRunning = false;
let batchAnalysisRunning = false;
let libraryVideos = [];
let previousActiveScanCount = null;
const selectedVideoIds = new Set();

const indexStatusLabels = {
  extracting: "正在抽帧",
  pending_analysis: "待AI识别",
  analyzing: "AI识别中",
  indexed: "已建立索引",
  partial: "部分完成",
  failed: "处理失败",
};

function setIndexStatusBadge(element, status) {
  const safeStatus = indexStatusLabels[status] ? status : "pending_analysis";
  element.className = `index-status-badge ${safeStatus}`;
  element.textContent = indexStatusLabels[safeStatus];
}

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

function setAdminConsoleOpen(open) {
  adminConsole.hidden = !open;
  adminEntryButton.setAttribute("aria-expanded", String(open));
  adminEntryButton.textContent = open ? "后台已打开" : "进入素材管理后台";
  if (open) {
    loadFolderDashboard(true);
    adminConsole.scrollIntoView({ behavior: "smooth", block: "start" });
    adminCloseButton.focus({ preventScroll: true });
  } else {
    document.querySelector(".search-panel")?.scrollIntoView({ behavior: "smooth", block: "start" });
    adminEntryButton.focus({ preventScroll: true });
  }
}

function applyImageOrientation(image) {
  const update = () => {
    const portrait = image.naturalHeight > image.naturalWidth;
    image.classList.toggle("portrait-frame", portrait);
    image.classList.toggle("landscape-frame", !portrait);
  };
  if (image.complete && image.naturalWidth > 0) {
    update();
  } else {
    image.addEventListener("load", update, { once: true });
  }
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
  setIndexStatusBadge(videoIndexStatus, video.index_status);
  if (player.getAttribute("src") !== video.video_url) {
    playerShell.classList.remove("portrait-video");
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
  visionButton.disabled = analysisRunning || batchAnalysisRunning || allSuccessful;
  reanalyzeButton.hidden = progress.success === 0;
  reanalyzeButton.disabled = analysisRunning || batchAnalysisRunning;
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
    applyImageOrientation(seekButton.querySelector("img"));
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

  const grouped = new Map();
  response.results.forEach((result) => {
    const key = result.media_month || "unknown";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(result);
  });
  const monthKeys = [...grouped.keys()].sort((left, right) => {
    if (left === "unknown") return 1;
    if (right === "unknown") return -1;
    return right.localeCompare(left);
  });

  monthKeys.forEach((monthKey) => {
    const monthResults = grouped.get(monthKey);
    const group = document.createElement("section");
    group.className = "search-month-group";
    const heading = document.createElement("div");
    heading.className = "search-month-heading";
    const monthTitle = document.createElement("h3");
    monthTitle.textContent = monthResults[0].media_month_label || "日期待确认";
    const monthCount = document.createElement("span");
    monthCount.textContent = `${monthResults.length} 条`;
    heading.append(monthTitle, monthCount);
    const monthList = document.createElement("div");
    monthList.className = "search-month-list";

    monthResults.forEach((result) => {
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
    applyImageOrientation(image);

    const caption = document.createElement("div");
    caption.className = "search-result-caption";
    const captionSource = document.createElement("span");
    captionSource.textContent = `${result.video_name} · ${formatTime(result.timestamp, true)}`;
    const indexBadge = document.createElement("span");
    setIndexStatusBadge(indexBadge, result.index_status);
    caption.append(captionSource, indexBadge);

    const body = document.createElement("div");
    body.className = "search-result-overlay";
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
    button.append(image, caption, body);
    button.addEventListener("click", () => openSearchResult(card, result));
    card.append(button);
    monthList.append(card);
    });
    group.append(heading, monthList);
    framesList.append(group);
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
    const response = await requestJson(`/api/search?q=${encodeURIComponent(query)}&limit=50`);
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
  if (response.status === 204) return null;
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || "请求失败，请重试。");
  return result;
}

function renderWatchFolders(folders) {
  watchFoldersList.replaceChildren();
  if (folders.length === 0) {
    const empty = document.createElement("p");
    empty.className = "folder-empty";
    empty.textContent = "尚未添加监测目录。";
    watchFoldersList.append(empty);
    return;
  }

  folders.forEach((folder) => {
    const card = document.createElement("article");
    card.className = "watch-folder-card";
    const top = document.createElement("div");
    top.className = "watch-folder-top";
    const path = document.createElement("strong");
    path.textContent = folder.path;
    path.title = folder.path;
    const scanButton = document.createElement("button");
    scanButton.type = "button";
    scanButton.className = "folder-scan-button";
    scanButton.textContent = "立即扫描";
    const job = folder.latest_job;
    const active = job && ["queued", "running"].includes(job.status);
    scanButton.disabled = active;
    scanButton.addEventListener("click", async () => {
      scanButton.disabled = true;
      try {
        await requestJson(`/api/watch-folders/${folder.id}/scan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ auto_analyze: null }),
        });
        setStatus(folderStatus, "扫描任务已开始。", "working");
        await loadFolderDashboard();
      } catch (error) {
        setStatus(folderStatus, error.message || "启动扫描失败。", "error");
      } finally {
        scanButton.disabled = false;
      }
    });
    const actions = document.createElement("div");
    actions.className = "watch-folder-actions";
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "folder-remove-button";
    removeButton.textContent = "停止监测";
    removeButton.disabled = active;
    removeButton.addEventListener("click", async () => {
      if (!window.confirm("停止监测不会删除源视频、已导入视频或现有索引。确认继续吗？")) return;
      try {
        await requestJson(`/api/watch-folders/${folder.id}`, { method: "DELETE" });
        setStatus(folderStatus, "已停止监测该目录，已有素材和索引保持不变。", "success");
        await loadFolderDashboard();
      } catch (error) {
        setStatus(folderStatus, error.message || "停止监测失败。", "error");
      }
    });
    actions.append(scanButton, removeButton);
    top.append(path, actions);

    const options = document.createElement("p");
    options.className = "watch-folder-options";
    options.textContent = `每 ${folder.scan_interval_seconds} 秒自动扫描 · ${folder.auto_analyze ? "自动AI识别已开启" : "自动AI识别未开启"}`;
    card.append(top, options);

    if (job) {
      const progress = document.createElement("div");
      progress.className = "scan-progress";
      const bar = document.createElement("span");
      bar.style.width = `${job.progress_percent}%`;
      progress.append(bar);
      const detail = document.createElement("p");
      detail.className = "scan-detail";
      const stage = job.current_stage || (job.status === "completed" ? "扫描完成" : "等待扫描");
      detail.textContent = `${stage} · ${job.processed}/${job.discovered} · 新增 ${job.imported} · 跳过 ${job.skipped} · 失败 ${job.failed}${job.current_file ? ` · ${job.current_file}` : ""}`;
      card.append(progress, detail);
      if (job.error) {
        const error = document.createElement("p");
        error.className = "scan-error";
        error.textContent = job.error;
        card.append(error);
      }
    }
    watchFoldersList.append(card);
  });
}

function mediaMonthLabel(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})/);
  return match ? `${match[1]}年${match[2]}月` : "日期未知";
}

function updateLibrarySelectionControls() {
  const selectedCount = selectedVideoIds.size;
  selectedVideoCount.textContent = `已选 ${selectedCount} 个`;
  const controlsDisabled = batchAnalysisRunning || analysisRunning || libraryVideos.length === 0;
  selectAllVideosButton.disabled = controlsDisabled;
  selectPendingVideosButton.disabled = controlsDisabled;
  clearVideoSelectionButton.disabled = controlsDisabled || selectedCount === 0;
  batchAnalyzeButton.disabled = controlsDisabled || selectedCount === 0;
  batchReanalyzeButton.disabled = controlsDisabled || selectedCount === 0;
  videoLibraryList.querySelectorAll(".video-library-card").forEach((card) => {
    const selected = selectedVideoIds.has(card.dataset.videoId);
    card.classList.toggle("selected", selected);
    const checkbox = card.querySelector("input[type='checkbox']");
    checkbox.checked = selected;
    checkbox.disabled = batchAnalysisRunning;
  });
}

async function openLibraryVideo(video) {
  try {
    const detail = await requestJson(`/api/videos/${encodeURIComponent(video.id)}`);
    renderVideo(detail);
    setStatus(visionStatus, `已载入 ${video.original_name}，可开始AI识别或重新分析。`);
    visionPanel.scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    setStatus(folderStatus, error.message || "视频载入失败。", "error");
  }
}

function renderVideoLibrary(videos, total = videos.length) {
  libraryVideos = videos;
  const availableIds = new Set(videos.map((video) => video.id));
  selectedVideoIds.forEach((id) => {
    if (!availableIds.has(id)) selectedVideoIds.delete(id);
  });
  videoLibraryCount.textContent = `共 ${total} 个视频，当前已显示全部`;
  videoLibraryList.replaceChildren();
  if (videos.length === 0) {
    const empty = document.createElement("p");
    empty.className = "folder-empty";
    empty.textContent = "尚无已导入视频。";
    videoLibraryList.append(empty);
    updateLibrarySelectionControls();
    return;
  }

  videos.forEach((video) => {
    const card = document.createElement("article");
    card.className = "video-library-card";
    card.dataset.videoId = video.id;

    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "video-library-thumbnail";
    preview.setAttribute("aria-label", `打开视频 ${video.original_name}`);
    if (video.thumbnail_url) {
      const image = document.createElement("img");
      image.src = video.thumbnail_url;
      image.alt = `${video.original_name} 的首帧`;
      image.loading = "lazy";
      preview.append(image);
    } else {
      const placeholder = document.createElement("span");
      placeholder.textContent = "暂无画面";
      preview.append(placeholder);
    }
    preview.addEventListener("click", () => openLibraryVideo(video));

    const selection = document.createElement("label");
    selection.className = "video-library-select";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selectedVideoIds.has(video.id);
    checkbox.setAttribute("aria-label", `选择 ${video.original_name}`);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedVideoIds.add(video.id);
      else selectedVideoIds.delete(video.id);
      updateLibrarySelectionControls();
    });
    selection.append(checkbox, document.createTextNode("选择"));

    const details = document.createElement("div");
    details.className = "video-library-details";
    const titleRow = document.createElement("div");
    titleRow.className = "video-library-title-row";
    const name = document.createElement("strong");
    name.textContent = video.original_name;
    name.title = video.original_name;
    const badge = document.createElement("span");
    setIndexStatusBadge(badge, video.index_status);
    titleRow.append(name, badge);
    const meta = document.createElement("p");
    meta.textContent = `${mediaMonthLabel(video.media_created_at)} · ${video.frame_count} 帧 · 已识别 ${video.success_count}/${video.frame_count}`;
    details.append(titleRow, meta);
    card.append(preview, selection, details);
    videoLibraryList.append(card);
  });
  updateLibrarySelectionControls();
}

async function loadVideoLibrary(showErrors = false) {
  try {
    const videos = [];
    let offset = 0;
    let total = 0;
    do {
      const page = await requestJson(`/api/videos?limit=500&offset=${offset}`);
      videos.push(...page.videos);
      total = page.total;
      offset += page.count;
      if (!page.has_more || page.count === 0) break;
    } while (offset < total);
    renderVideoLibrary(videos, total);
  } catch (error) {
    if (showErrors) setStatus(folderStatus, error.message || "无法读取视频素材。", "error");
  }
}

async function loadWatchFolderStatus(showErrors = false) {
  try {
    const response = await requestJson("/api/watch-folders");
    renderWatchFolders(response.folders);
    const activeCount = response.folders.filter((folder) =>
      folder.latest_job && ["queued", "running"].includes(folder.latest_job.status)
    ).length;
    if (previousActiveScanCount > 0 && activeCount === 0) loadVideoLibrary();
    previousActiveScanCount = activeCount;
  } catch (error) {
    if (showErrors) setStatus(folderStatus, error.message || "无法读取扫描状态。", "error");
  }
}

async function loadFolderDashboard(showErrors = false) {
  await Promise.all([loadWatchFolderStatus(showErrors), loadVideoLibrary(showErrors)]);
}

function setBatchProgress(completedVideos, totalVideos, frameProgress = 0) {
  const ratio = totalVideos > 0
    ? Math.min(1, (completedVideos + Math.max(0, Math.min(1, frameProgress))) / totalVideos)
    : 0;
  batchVisionProgress.hidden = false;
  batchVisionProgressBar.style.width = `${Math.round(ratio * 100)}%`;
}

async function processSelectedVideo(video, force, batchPosition, batchTotal) {
  let analyzedVideo = await requestJson(`/api/videos/${video.id}/vision/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force }),
  });
  if (currentVideo?.id === video.id) renderVideo(analyzedVideo);

  while (!analyzedVideo.vision_progress.done) {
    const response = await requestJson(`/api/videos/${video.id}/vision/next`, { method: "POST" });
    analyzedVideo = response.video;
    if (currentVideo?.id === video.id) renderVideo(analyzedVideo);
    const progress = analyzedVideo.vision_progress;
    const frameRatio = progress.total > 0 ? progress.completed / progress.total : 0;
    setBatchProgress(batchPosition, batchTotal, frameRatio);
    setStatus(
      batchVisionStatus,
      `正在处理第 ${batchPosition + 1}/${batchTotal} 个视频：${video.original_name} · ${progress.completed}/${progress.total} 帧`,
      "working",
    );
    if (!response.processed) break;
  }
  return analyzedVideo;
}

async function runBatchVisionAnalysis(force = false) {
  if (batchAnalysisRunning || analysisRunning || selectedVideoIds.size === 0) return;
  const selected = libraryVideos.filter((video) => selectedVideoIds.has(video.id));
  const ready = selected.filter((video) => video.frame_count > 0 && video.index_status !== "extracting");
  const targets = force ? ready : ready.filter((video) => video.index_status !== "indexed");
  const skipped = selected.length - targets.length;
  if (targets.length === 0) {
    const message = force
      ? "所选视频暂时没有可识别的关键帧。"
      : "所选视频均已建立索引，普通识别已自动跳过，不会重复调用 API。";
    setStatus(batchVisionStatus, message, "success");
    return;
  }
  if (force && !window.confirm(`将重新分析 ${targets.length} 个视频并再次调用视觉 API，可能产生费用。确认继续吗？`)) return;

  batchAnalysisRunning = true;
  updateLibrarySelectionControls();
  if (currentVideo) renderVisionControls(currentVideo);
  setBatchProgress(0, targets.length);
  let succeeded = 0;
  let partiallyFailed = 0;
  let failedVideos = 0;

  try {
    for (let index = 0; index < targets.length; index += 1) {
      const video = targets[index];
      setStatus(batchVisionStatus, `正在准备第 ${index + 1}/${targets.length} 个视频：${video.original_name}`, "working");
      try {
        const result = await processSelectedVideo(video, force, index, targets.length);
        if (result.vision_progress.failed > 0) partiallyFailed += 1;
        else succeeded += 1;
      } catch (error) {
        failedVideos += 1;
      }
      setBatchProgress(index + 1, targets.length);
    }
    const type = partiallyFailed > 0 || failedVideos > 0 ? "error" : "success";
    setStatus(
      batchVisionStatus,
      `批量处理完成：成功 ${succeeded} 个，部分帧失败 ${partiallyFailed} 个，处理失败 ${failedVideos} 个，自动跳过 ${skipped} 个。`,
      type,
    );
  } finally {
    batchAnalysisRunning = false;
    await loadVideoLibrary();
    updateLibrarySelectionControls();
    if (currentVideo) renderVisionControls(currentVideo);
  }
}

async function runVisionAnalysis(force = false) {
  if (!currentVideo || analysisRunning || batchAnalysisRunning) return;
  analysisRunning = true;
  renderVisionControls(currentVideo);
  updateLibrarySelectionControls();
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
    updateLibrarySelectionControls();
    loadVideoLibrary();
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
    loadFolderDashboard();
    setStatus(uploadStatus, `索引完成：已提取 ${result.frames.length} 张关键帧。`, "success");
  } catch (error) {
    setStatus(uploadStatus, error.message || "上传失败，请重试。", "error");
  } finally {
    uploadButton.disabled = false;
    fileInput.disabled = false;
  }
});

folderForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  folderAddButton.disabled = true;
  setStatus(folderStatus, "正在添加目录并启动首次扫描……", "working");
  try {
    const result = await requestJson("/api/watch-folders", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: folderPathInput.value.trim(),
        auto_analyze: folderAutoAnalyze.checked,
        scan_interval_seconds: Number(folderInterval.value),
      }),
    });
    setStatus(
      folderStatus,
      result.job.auto_analyze
        ? "目录已开始扫描；新视频将自动进行AI画面识别。"
        : "目录已开始扫描；新视频只抽帧，需手动启动AI识别。",
      "success",
    );
    await loadFolderDashboard();
  } catch (error) {
    setStatus(folderStatus, error.message || "添加监测目录失败。", "error");
  } finally {
    folderAddButton.disabled = false;
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

adminEntryButton.addEventListener("click", () => setAdminConsoleOpen(true));
adminCloseButton.addEventListener("click", () => setAdminConsoleOpen(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !adminConsole.hidden && !batchAnalysisRunning && !analysisRunning) {
    setAdminConsoleOpen(false);
  }
});

selectAllVideosButton.addEventListener("click", () => {
  libraryVideos.forEach((video) => selectedVideoIds.add(video.id));
  updateLibrarySelectionControls();
});
selectPendingVideosButton.addEventListener("click", () => {
  selectedVideoIds.clear();
  libraryVideos
    .filter((video) => video.frame_count > 0 && !["indexed", "extracting"].includes(video.index_status))
    .forEach((video) => selectedVideoIds.add(video.id));
  updateLibrarySelectionControls();
  if (selectedVideoIds.size === 0) {
    setStatus(batchVisionStatus, "目前没有待识别或需要重试的视频。", "success");
  } else {
    setStatus(batchVisionStatus, `已选择 ${selectedVideoIds.size} 个未完成识别的视频。`);
  }
});
clearVideoSelectionButton.addEventListener("click", () => {
  selectedVideoIds.clear();
  updateLibrarySelectionControls();
  setStatus(batchVisionStatus, "");
});
batchAnalyzeButton.addEventListener("click", () => runBatchVisionAnalysis(false));
batchReanalyzeButton.addEventListener("click", () => runBatchVisionAnalysis(true));

visionButton.addEventListener("click", () => runVisionAnalysis(false));
reanalyzeButton.addEventListener("click", () => {
  if (window.confirm("重新分析会再次调用视觉 API，确认继续吗？")) {
    runVisionAnalysis(true);
  }
});
player.addEventListener("timeupdate", () => {
  currentPosition.textContent = formatTime(player.currentTime, true);
});
player.addEventListener("loadedmetadata", () => {
  playerShell.classList.toggle("portrait-video", player.videoHeight > player.videoWidth);
});

renderVideo(window.__INITIAL_VIDEO__);
loadFolderDashboard(true);
window.setInterval(() => loadWatchFolderStatus(false), 3000);
