(function () {
  "use strict";

  let entries = [];
  let toastTimer;
  const voteStoragePrefix = "rb-vote-";
  const ratingsApiUrl = String(window.RAMEN_BENCH_CONFIG?.ratingsApiUrl || "").replace(/\/+$/, "");
  const ratingsByRun = new Map();
  const sessionVotes = new Map();
  const framesById = new Map();
  const frameLoads = new WeakMap();
  const expandedModels = new Set();
  const swipeGesture = {
    pointerId: null,
    startX: 0,
    startY: 0,
    startedAt: 0
  };
  const swipeSurfaces = new WeakSet();

  const swipeMinimumDistance = 56;
  const swipeMaximumDuration = 650;
  const swipeMinimumVelocity = 0.25;
  const swipeAxisRatio = 1.35;
  const swipeDirectionLockDistance = 12;
  const swipeBridgeUrl = new URL("swipe-bridge.js", document.baseURI).href;
  const elements = {
    sidebar: document.querySelector("#sidebar"),
    brand: document.querySelector("#brand-home"),
    scrim: document.querySelector("#sidebar-scrim"),
    mobileMenu: document.querySelector("#mobile-menu"),
    singleMode: document.querySelector("#single-mode"),
    gridMode: document.querySelector("#grid-mode"),
    search: document.querySelector("#model-search"),
    creatorFilter: document.querySelector("#creator-filter"),
    harnessFilter: document.querySelector("#harness-filter"),
    filterCount: document.querySelector("#filter-count"),
    clearFilters: document.querySelector("#clear-filters"),
    list: document.querySelector("#model-list"),
    gridHint: document.querySelector("#grid-hint"),
    viewer: document.querySelector("#viewer"),
    welcome: document.querySelector("#welcome"),
    frames: document.querySelector("#frames"),
    previous: document.querySelector("#previous-model"),
    next: document.querySelector("#next-model"),
    leaderboard: document.querySelector("#leaderboard"),
    copyPrompt: document.querySelector("#copy-prompt"),
    promptText: document.querySelector("#prompt-text"),
    toast: document.querySelector("#toast")
  };

  const state = {
    mode: "single",
    activeId: null,
    selectedIds: new Set(),
    filteredEntries: []
  };

  function isRun(run) {
    return Boolean(
      run
      && typeof run.id === "string"
      && run.vendor?.displayName
      && run.model?.displayName
      && run.variation?.displayName
      && run.harness?.name
      && run.artifacts?.result
    );
  }

  function normalizeRun(run, manifestUrl) {
    const resolveArtifact = (path) => path ? new URL(path, manifestUrl).href : null;
    return {
      id: run.id,
      name: run.model.displayName,
      creator: run.vendor.displayName,
      badge: run.variation.displayName,
      harness: formatHarness(run.harness),
      resultUrl: resolveArtifact(run.artifacts.result),
      transcriptUrl: resolveArtifact(run.artifacts.transcript),
      rawTranscriptUrl: resolveArtifact(run.artifacts.rawTranscript),
      manifestUrl,
      run
    };
  }

  function formatHarness(harness) {
    if (!harness.version) return harness.name;
    return String(harness.version).toLocaleLowerCase().startsWith(`${harness.name.toLocaleLowerCase()} `)
      ? harness.version
      : `${harness.name} ${harness.version}`;
  }

  async function loadEntries() {
    const registryResponse = await fetch("registry.json", { cache: "no-store" });
    if (!registryResponse.ok) {
      throw new Error(`Registry request failed with ${registryResponse.status}`);
    }

    const registry = await registryResponse.json();
    if (registry.schemaVersion !== 1 || !Array.isArray(registry.runs)) {
      throw new Error("Unsupported registry format");
    }

    const results = await Promise.allSettled(registry.runs.map(async (path) => {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) throw new Error(`${path}: ${response.status}`);
      const run = await response.json();
      if (!isRun(run)) throw new Error(`${path}: invalid run manifest`);
      return normalizeRun(run, response.url);
    }));

    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length) {
      console.warn("Some Ramen Bench runs could not be loaded:", failures.map((failure) => failure.reason));
      showToast(`${failures.length} run manifest${failures.length === 1 ? "" : "s"} could not be loaded.`);
    }

    return results
      .filter((result) => result.status === "fulfilled")
      .map((result) => result.value);
  }

  function entryFromHash() {
    const id = decodeURIComponent(window.location.hash.slice(1));
    return entries.find((entry) => entry.id === id);
  }

  function expandEntryModel(entry) {
    if (entry) expandedModels.add(`${entry.creator}\u0000${entry.name}`);
  }

  async function fetchVotes({ quiet = true } = {}) {
    if (!ratingsApiUrl) return;
    try {
      const response = await fetch(`${ratingsApiUrl}/votes`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Ratings request failed with ${response.status}`);
      const votes = await response.json();
      if (!Array.isArray(votes)) throw new Error("Ratings response was not an array");
      ratingsByRun.clear();
      for (const vote of votes) {
        if (typeof vote.runId !== "string") continue;
        ratingsByRun.set(vote.runId, {
          average: Number(vote.average),
          count: Number(vote.count)
        });
      }
    } catch (error) {
      console.warn(error);
      if (!quiet) showToast("The shared leaderboard is temporarily unavailable.");
    }
  }

  async function submitVote(entry, rating, control) {
    if (getVote(entry.id)) return;
    if (!ratingsApiUrl) {
      showToast("The shared ratings API has not been configured yet.");
      return;
    }

    saveVote(entry.id, rating);
    refreshRatingControls();
    control.classList.add("submitting");
    try {
      const response = await fetch(`${ratingsApiUrl}/vote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ runId: entry.id, rating })
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.error || `Vote request failed with ${response.status}`);
      }
      await fetchVotes();
      renderList();
      refreshRatingControls();
      showToast(`Rated ${entry.name} ${rating} out of 5.`);
    } catch (error) {
      console.warn(error);
      showToast("Your rating could not be saved. Please try again.");
    } finally {
      control.classList.remove("submitting");
    }
  }

  function getVote(runId) {
    try {
      const rating = Number.parseInt(window.localStorage.getItem(`${voteStoragePrefix}${runId}`), 10);
      if (rating >= 1 && rating <= 5) return rating;
    } catch (_error) {
      // Fall through to the in-memory lock when storage is unavailable.
    }
    return sessionVotes.get(runId) || 0;
  }

  function saveVote(runId, rating) {
    sessionVotes.set(runId, rating);
    try {
      window.localStorage.setItem(`${voteStoragePrefix}${runId}`, String(rating));
    } catch (_error) {
      // Voting still proceeds when storage is unavailable; the Worker also de-duplicates voters.
    }
  }

  function hoverStars(control, rating) {
    if (getVote(control.dataset.runId)) return;
    control.querySelectorAll(".star").forEach((star, index) => {
      star.classList.toggle("hover", index < rating);
    });
  }

  function unhoverStars(control) {
    control.querySelectorAll(".star").forEach((star) => star.classList.remove("hover"));
  }

  function uniqueValues(key) {
    return [...new Set(entries.map((entry) => entry[key]).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
  }

  function populateSelect(select, values) {
    while (select.options.length > 1) select.remove(1);
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    }
  }

  function applyFilters() {
    const term = elements.search.value.trim().toLocaleLowerCase();
    const creator = elements.creatorFilter.value;
    const harness = elements.harnessFilter.value;

    state.filteredEntries = entries.filter((entry) => {
      const searchable = [entry.name, entry.creator, entry.harness, entry.badge]
        .filter(Boolean)
        .join(" ")
        .toLocaleLowerCase();
      return (!term || searchable.includes(term))
        && (!creator || entry.creator === creator)
        && (!harness || entry.harness === harness);
    });

    const hasFilters = Boolean(term || creator || harness);
    elements.creatorFilter.classList.toggle("active", Boolean(creator));
    elements.harnessFilter.classList.toggle("active", Boolean(harness));
    elements.clearFilters.hidden = !hasFilters;
    elements.filterCount.textContent = entries.length
      ? `${state.filteredEntries.length} of ${entries.length}`
      : "No entries yet";
    renderList();
  }

  function renderList() {
    elements.list.replaceChildren();

    if (!state.filteredEntries.length) {
      const empty = document.createElement("p");
      empty.className = "model-list-empty";
      empty.textContent = entries.length
        ? "No models match those filters."
        : "No bowls on the pass yet. Add the first run when it is ready to taste.";
      elements.list.append(empty);
      return;
    }

    for (const [creator, models] of groupEntries(state.filteredEntries)) {
      const section = document.createElement("section");
      section.className = "model-group";
      const heading = document.createElement("h2");
      heading.className = "group-label";
      const vendorButton = document.createElement("button");
      vendorButton.type = "button";
      vendorButton.className = "vendor-button";
      vendorButton.textContent = creator;
      vendorButton.title = `Open the first ${creator} result`;
      vendorButton.addEventListener("click", () => {
        const firstEntry = entriesInModelOrder(models)[0];
        if (firstEntry) chooseSingleEntry(firstEntry);
      });
      heading.append(vendorButton);
      section.append(heading);

      let modelIndex = 0;
      for (const [model, modelRuns] of [...models].sort(compareModels)) {
        const modelSection = document.createElement("section");
        modelSection.className = "model-family";
        const modelHeading = document.createElement("h3");
        modelHeading.className = "model-family-label";
        const modelKey = `${creator}\u0000${model}`;
        const effortsId = `efforts-${slugify(creator)}-${slugify(model)}-${modelIndex++}`;
        const filtersActive = Boolean(
          elements.search.value.trim()
          || elements.creatorFilter.value
          || elements.harnessFilter.value
        );
        const expanded = expandedModels.has(modelKey) || filtersActive;
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "model-family-toggle";
        toggle.setAttribute("aria-controls", effortsId);
        toggle.setAttribute("aria-expanded", String(expanded));
        const chevron = document.createElement("span");
        chevron.className = "model-family-chevron";
        chevron.setAttribute("aria-hidden", "true");
        chevron.textContent = "›";
        const label = document.createElement("span");
        label.textContent = model;
        toggle.append(chevron, label);
        modelHeading.append(toggle);

        const efforts = document.createElement("div");
        efforts.className = "effort-list";
        efforts.id = effortsId;
        efforts.hidden = !expanded;

        for (const entry of [...modelRuns].sort(compareEffort)) {
          efforts.append(createModelButton(entry));
        }
        toggle.addEventListener("click", () => {
          const nextExpanded = !expandedModels.has(modelKey);
          if (nextExpanded) expandedModels.add(modelKey);
          else expandedModels.delete(modelKey);
          toggle.setAttribute("aria-expanded", String(nextExpanded));
          efforts.hidden = !nextExpanded;
        });
        modelSection.append(modelHeading, efforts);
        section.append(modelSection);
      }
      elements.list.append(section);
    }
  }

  function groupEntries(list) {
    const groups = new Map();
    for (const entry of list) {
      const creator = entry.creator || "Other";
      const model = entry.name || "Unknown model";
      if (!groups.has(creator)) groups.set(creator, new Map());
      const models = groups.get(creator);
      if (!models.has(model)) models.set(model, []);
      models.get(model).push(entry);
    }
    return groups;
  }

  function entriesInSidebarOrder(list = state.filteredEntries) {
    const ordered = [];
    for (const [, models] of groupEntries(list)) {
      ordered.push(...entriesInModelOrder(models));
    }
    return ordered;
  }

  function entriesInModelOrder(models) {
    const ordered = [];
    for (const [, modelRuns] of [...models].sort(compareModels)) {
      ordered.push(...[...modelRuns].sort(compareEffort));
    }
    return ordered;
  }

  function compareEffort(a, b) {
    const effortOrder = ["ultra", "max", "xhigh", "high", "medium", "low"];
    const rank = (entry) => {
      const index = effortOrder.indexOf(String(entry.badge || "").toLocaleLowerCase());
      return index === -1 ? effortOrder.length : index;
    };
    return rank(a) - rank(b) || String(a.badge || "").localeCompare(String(b.badge || ""));
  }

  function compareModels([nameA], [nameB]) {
    const versionA = modelVersion(nameA);
    const versionB = modelVersion(nameB);
    const width = Math.max(versionA.length, versionB.length);
    for (let index = 0; index < width; index += 1) {
      const difference = (versionB[index] || 0) - (versionA[index] || 0);
      if (difference) return difference;
    }
    return 0;
  }

  function modelVersion(name) {
    const match = String(name).match(/\d+(?:\.\d+)*/);
    return match ? match[0].split(".").map(Number) : [];
  }

  function slugify(value) {
    return String(value).toLocaleLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
  }

  function createModelButton(entry) {
    const selected = state.mode === "single"
      ? state.activeId === entry.id
      : state.selectedIds.has(entry.id);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `model-button${selected ? " active" : ""}`;
    button.dataset.entryId = entry.id;
    button.setAttribute("aria-pressed", String(selected));
    button.setAttribute("aria-label", `${entry.name}, ${entry.badge || "default effort"}`);

    const mark = document.createElement("span");
    mark.className = "selection-mark";
    mark.setAttribute("aria-hidden", "true");
    mark.textContent = state.mode === "single" ? "•" : "✓";

    const copy = document.createElement("span");
    copy.className = "model-copy";
    const nameRow = document.createElement("span");
    nameRow.className = "model-name-row";
    const name = document.createElement("span");
    name.className = "model-name";
    name.textContent = entry.badge || "Default";
    nameRow.append(name);

    const harness = document.createElement("span");
    harness.className = "model-harness";
    harness.textContent = entry.harness;
    copy.append(nameRow, harness);

    const score = document.createElement("span");
    score.className = "rating-badge";
    const rating = ratingsByRun.get(entry.id);
    score.textContent = rating?.count ? `${formatAverage(rating.average)} ★` : "";
    if (rating?.count) score.title = `${rating.count} community vote${rating.count === 1 ? "" : "s"}`;

    button.append(mark, copy, score);
    button.addEventListener("click", () => chooseEntry(entry));
    return button;
  }

  function chooseEntry(entry) {
    if (state.mode === "single") {
      chooseSingleEntry(entry);
      return;
    }

    if (state.selectedIds.has(entry.id)) {
      state.selectedIds.delete(entry.id);
    } else if (state.selectedIds.size < 4) {
      state.selectedIds.add(entry.id);
    } else {
      showToast("Grid view holds up to four bowls.");
    }
    render();
  }

  function chooseSingleEntry(entry) {
    state.activeId = entry.id;
    expandEntryModel(entry);
    window.history.replaceState(null, "", `#${encodeURIComponent(entry.id)}`);
    if (state.mode !== "single") setMode("single");
    else render();
    revealEntryInSidebar(entry.id);
    closeSidebar();
  }

  function revealEntryInSidebar(entryId) {
    window.requestAnimationFrame(() => {
      const target = [...elements.list.querySelectorAll(".model-button")]
        .find((button) => button.dataset.entryId === entryId);
      if (!target) return;
      const containerBounds = elements.list.getBoundingClientRect();
      const targetBounds = target.getBoundingClientRect();
      const modelToggle = target.closest(".model-family")?.querySelector(".model-family-toggle");
      const toggleBounds = modelToggle?.getBoundingClientRect();

      if (targetBounds.bottom > containerBounds.bottom) {
        elements.list.scrollTop += targetBounds.bottom - containerBounds.bottom + 8;
      } else if (toggleBounds && toggleBounds.top < containerBounds.top) {
        elements.list.scrollTop += toggleBounds.top - containerBounds.top - 8;
      } else if (targetBounds.top < containerBounds.top) {
        elements.list.scrollTop += targetBounds.top - containerBounds.top - 8;
      }
    });
  }

  function setMode(mode) {
    if (mode === state.mode) return;
    resetSwipeGesture();
    state.mode = mode;
    elements.viewer.classList.toggle("mode-single", mode === "single");
    elements.viewer.classList.toggle("mode-grid", mode === "grid");
    elements.singleMode.classList.toggle("active", mode === "single");
    elements.gridMode.classList.toggle("active", mode === "grid");
    elements.singleMode.setAttribute("aria-pressed", String(mode === "single"));
    elements.gridMode.setAttribute("aria-pressed", String(mode === "grid"));

    if (mode === "grid" && !state.selectedIds.size) {
      entries.slice(0, 4).forEach((entry) => state.selectedIds.add(entry.id));
    }
    render();
  }

  function render() {
    renderList();
    renderViewer();
    elements.gridHint.classList.toggle("visible", state.mode === "grid");
    if (state.mode === "grid") {
      elements.gridHint.textContent = `${state.selectedIds.size} of 4 bowls selected`;
    }
  }

  function setWelcome(kicker, title, body, showPrompt = true) {
    elements.welcome.querySelector(".eyebrow").textContent = kicker;
    elements.welcome.querySelector("h1").textContent = title;
    elements.welcome.querySelector("p:not(.eyebrow)").textContent = body;
    elements.welcome.querySelector("button").hidden = !showPrompt;
  }

  function renderViewer() {
    const hasEntries = entries.length > 0;
    const navigationEntries = entriesInSidebarOrder();
    elements.welcome.hidden = hasEntries;
    elements.previous.hidden = !hasEntries || state.mode !== "single" || navigationEntries.length < 2;
    elements.next.hidden = !hasEntries || state.mode !== "single" || navigationEntries.length < 2;

    if (!hasEntries) {
      setWelcome(
        "The kitchen is warming up",
        "Ready for the first bowl.",
        "Add run manifests to registry.json and their single-file results will appear here."
      );
      return;
    }

    const visibleEntries = state.mode === "single"
      ? [entries.find((entry) => entry.id === state.activeId) || entries[0]]
      : entries.filter((entry) => state.selectedIds.has(entry.id)).slice(0, 4);

    if (!visibleEntries.length) {
      elements.welcome.hidden = false;
      setWelcome("Grid view", "Choose a bowl to compare.", "Select up to four models from the sidebar.", false);
    }

    const visibleIds = new Set(visibleEntries.map((entry) => entry.id));
    for (const entry of entries) {
      const frame = framesById.get(entry.id);
      if (!frame) continue;
      const iframe = frame.querySelector("iframe");
      const visible = visibleIds.has(entry.id);
      frame.classList.toggle("visible", visible);

      if (visible) {
        loadFrameContent(iframe);
      } else {
        unloadFrameContent(iframe);
      }
    }
  }

  function escapeAttribute(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function injectSwipeBridge(html, sourceUrl) {
    const baseUrl = new URL(".", sourceUrl).href;
    const support = `<base href="${escapeAttribute(baseUrl)}">`
      + `<script src="${escapeAttribute(swipeBridgeUrl)}"></script>`;
    const head = /<head(?:\s[^>]*)?>/i.exec(html);
    if (!head) return `${support}${html}`;
    const position = head.index + head[0].length;
    return `${html.slice(0, position)}${support}${html.slice(position)}`;
  }

  async function loadFrameContent(iframe) {
    if (
      iframe.hasAttribute("srcdoc")
      || iframe.hasAttribute("src")
      || frameLoads.has(iframe)
    ) {
      return;
    }

    const controller = new AbortController();
    frameLoads.set(iframe, controller);
    try {
      const response = await fetch(iframe.dataset.src, { signal: controller.signal });
      if (!response.ok) throw new Error(`Result request failed with ${response.status}`);
      const html = await response.text();
      if (controller.signal.aborted || !iframe.closest(".frame")?.classList.contains("visible")) return;
      iframe.srcdoc = injectSwipeBridge(html, response.url);
    } catch (error) {
      if (error.name === "AbortError") return;
      console.warn("Full-surface swipe support could not be loaded; opening the result directly:", error);
      if (iframe.closest(".frame")?.classList.contains("visible")) iframe.src = iframe.dataset.src;
    } finally {
      if (frameLoads.get(iframe) === controller) frameLoads.delete(iframe);
    }
  }

  function unloadFrameContent(iframe) {
    frameLoads.get(iframe)?.abort();
    frameLoads.delete(iframe);
    if (iframe.hasAttribute("srcdoc")) {
      iframe.srcdoc = "";
      iframe.removeAttribute("srcdoc");
    }
    if (iframe.hasAttribute("src")) {
      iframe.src = "";
      iframe.removeAttribute("src");
    }
  }

  function buildFrames() {
    elements.frames.replaceChildren();
    framesById.clear();
    for (const entry of entries) {
      const frame = createFrame(entry);
      framesById.set(entry.id, frame);
      elements.frames.append(frame);
    }
  }

  function createFrame(entry) {
    const frame = document.createElement("article");
    frame.className = "frame";
    frame.dataset.entryId = entry.id;

    const iframe = document.createElement("iframe");
    iframe.dataset.src = entry.resultUrl;
    iframe.title = `${entry.name} ramen benchmark result`;
    iframe.loading = "lazy";
    iframe.sandbox = "allow-scripts allow-pointer-lock";

    const label = document.createElement("div");
    label.className = "frame-label";
    const heading = document.createElement("div");
    heading.className = "frame-heading";
    const title = document.createElement("span");
    title.className = "frame-title";
    title.textContent = entry.badge ? `${entry.name} · ${entry.badge}` : entry.name;
    const harness = document.createElement("span");
    harness.className = "frame-harness";
    harness.textContent = entry.harness;
    heading.append(title, harness);

    const stats = document.createElement("div");
    stats.className = "run-stats";
    addStat(stats, formatCost(entry.run.usage?.cost), "Cost");
    addStat(stats, formatTokens(entry.run.usage?.tokens?.total), "Total tokens");
    addStat(stats, formatDuration(entry.run.timing?.wallDurationMs), "Wall time");

    const transcript = document.createElement("a");
    transcript.className = "transcript-button";
    transcript.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M6 3h9l3 3v15H6V3Z"></path><path d="M9 11h6M9 15h6M9 7h3"></path></svg><span>Transcript</span>';
    transcript.href = `transcript.html#${encodeURIComponent(entry.id)}`;
    transcript.target = "_blank";
    transcript.rel = "noopener noreferrer";
    transcript.setAttribute("aria-label", `Open ${entry.name} ${entry.badge} transcript`);

    const ratingControl = createRatingControl(entry);
    label.append(heading, stats, transcript, ratingControl);
    frame.append(iframe, label);
    return frame;
  }

  function addStat(container, value, label) {
    if (!value) return;
    const stat = document.createElement("span");
    stat.textContent = value;
    stat.title = label;
    container.append(stat);
  }

  function formatTokens(value, compact = true) {
    if (!Number.isFinite(value)) return "";
    return compact
      ? `${new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 }).format(value)} tokens`
      : new Intl.NumberFormat("en-US").format(value);
  }

  function formatCost(cost) {
    if (!cost || !Number.isFinite(cost.total)) return "";
    const digits = cost.total > 0 && cost.total < 0.01 ? 4 : 2;
    try {
      const amount = new Intl.NumberFormat("en-US", {
        style: "currency",
        currency: cost.currency || "USD",
        minimumFractionDigits: digits,
        maximumFractionDigits: Math.max(digits, 4)
      }).format(cost.total);
      return `${cost.estimated ? "~" : ""}${amount}`;
    } catch (_error) {
      return `${cost.estimated ? "~" : ""}${cost.total} ${cost.currency || "USD"}`;
    }
  }

  function formatDuration(milliseconds) {
    if (!Number.isFinite(milliseconds)) return "";
    const totalSeconds = Math.round(milliseconds / 1000);
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    return [hours ? `${hours}h` : "", minutes || hours ? `${minutes}m` : "", `${seconds}s`]
      .filter(Boolean)
      .join(" ");
  }

  function createRatingControl(entry) {
    const control = document.createElement("span");
    control.className = "rating-control";
    control.dataset.runId = entry.id;
    const wrapper = document.createElement("span");
    wrapper.className = "stars";
    wrapper.setAttribute("aria-label", `Rate ${entry.name}`);
    const current = getVote(entry.id);

    for (let score = 1; score <= 5; score += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `star${score <= current ? " voted" : ""}`;
      button.textContent = "★";
      button.title = `${score} star${score === 1 ? "" : "s"}`;
      button.setAttribute("aria-label", `${score} star${score === 1 ? "" : "s"}`);
      button.setAttribute("aria-pressed", String(score <= current));
      button.disabled = !ratingsApiUrl || Boolean(current);
      button.addEventListener("click", () => submitVote(entry, score, control));
      button.addEventListener("mouseenter", () => hoverStars(control, score));
      button.addEventListener("mouseleave", () => unhoverStars(control));
      wrapper.append(button);
    }

    const summary = document.createElement("span");
    summary.className = "vote-summary";
    control.append(wrapper, summary);
    updateRatingControl(control);
    return control;
  }

  function refreshRatingControls() {
    document.querySelectorAll(".rating-control").forEach(updateRatingControl);
  }

  function updateRatingControl(control) {
    const runId = control.dataset.runId;
    const current = getVote(runId);
    const community = ratingsByRun.get(runId);
    control.querySelectorAll(".star").forEach((star, index) => {
      const selected = index < current;
      star.classList.toggle("voted", selected);
      star.classList.remove("hover");
      star.setAttribute("aria-pressed", String(selected));
      star.disabled = !ratingsApiUrl || Boolean(current);
    });
    const summary = control.querySelector(".vote-summary");
    if (!ratingsApiUrl) {
      summary.textContent = "Offline";
      control.title = "Shared ratings API not configured";
    } else if (community?.count) {
      summary.textContent = `${current ? "Voted · " : ""}${formatAverage(community.average)} · ${community.count}`;
      control.title = `${formatAverage(community.average)} from ${community.count} vote${community.count === 1 ? "" : "s"}`;
    } else if (current) {
      summary.textContent = "Voted";
      control.title = "Your vote has been recorded";
    } else {
      summary.textContent = "Rate";
      control.title = "No community votes yet";
    }
  }

  function formatAverage(value) {
    return Number.isFinite(value) ? value.toFixed(1) : "—";
  }

  function move(direction) {
    const navigationEntries = entriesInSidebarOrder();
    if (navigationEntries.length < 2) return;
    const currentIndex = navigationEntries.findIndex((entry) => entry.id === state.activeId);
    const nextIndex = currentIndex === -1
      ? (direction > 0 ? 0 : navigationEntries.length - 1)
      : (currentIndex + direction + navigationEntries.length) % navigationEntries.length;
    state.activeId = navigationEntries[nextIndex].id;
    expandEntryModel(navigationEntries[nextIndex]);
    window.history.replaceState(null, "", `#${encodeURIComponent(state.activeId)}`);
    render();
    revealEntryInSidebar(state.activeId);
  }

  function resetSwipeGesture() {
    swipeGesture.pointerId = null;
    swipeGesture.startX = 0;
    swipeGesture.startY = 0;
    swipeGesture.startedAt = 0;
  }

  function beginSwipeGesture(pointerId, clientX, clientY) {
    if (
      state.mode !== "single"
      || entriesInSidebarOrder().length < 2
    ) {
      return false;
    }

    swipeGesture.pointerId = pointerId;
    swipeGesture.startX = clientX;
    swipeGesture.startY = clientY;
    swipeGesture.startedAt = performance.now();
    return true;
  }

  function updateSwipeGesture(pointerId, clientX, clientY) {
    if (swipeGesture.pointerId !== pointerId) return false;

    const horizontalDistance = Math.abs(clientX - swipeGesture.startX);
    const verticalDistance = Math.abs(clientY - swipeGesture.startY);
    if (
      verticalDistance >= swipeDirectionLockDistance
      && verticalDistance > horizontalDistance * swipeAxisRatio
    ) {
      resetSwipeGesture();
      return false;
    }

    return horizontalDistance >= swipeDirectionLockDistance
      && horizontalDistance > verticalDistance * swipeAxisRatio;
  }

  function finishSwipeGesture(pointerId, clientX, clientY) {
    if (swipeGesture.pointerId !== pointerId) return;

    const horizontalDistance = clientX - swipeGesture.startX;
    const verticalDistance = clientY - swipeGesture.startY;
    const elapsed = performance.now() - swipeGesture.startedAt;
    const horizontalSpeed = Math.abs(horizontalDistance) / Math.max(elapsed, 1);
    resetSwipeGesture();

    if (
      state.mode !== "single"
      || Math.abs(horizontalDistance) < swipeMinimumDistance
      || elapsed > swipeMaximumDuration
      || horizontalSpeed < swipeMinimumVelocity
      || Math.abs(horizontalDistance) < Math.abs(verticalDistance) * swipeAxisRatio
    ) {
      return;
    }

    move(horizontalDistance < 0 ? 1 : -1);
  }

  function bindSwipeSurface(surface) {
    if (!surface || swipeSurfaces.has(surface)) return;
    swipeSurfaces.add(surface);

    surface.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 1) {
        resetSwipeGesture();
        return;
      }
      const touch = event.touches[0];
      beginSwipeGesture(touch.identifier, touch.clientX, touch.clientY);
    }, { capture: true, passive: true });
    surface.addEventListener("touchmove", (event) => {
      if (event.touches.length !== 1) {
        resetSwipeGesture();
        return;
      }
      const touch = event.touches[0];
      updateSwipeGesture(touch.identifier, touch.clientX, touch.clientY);
    }, { capture: true, passive: true });
    surface.addEventListener("touchend", (event) => {
      if (event.touches.length || !event.changedTouches.length) {
        resetSwipeGesture();
        return;
      }
      const touch = Array.from(event.changedTouches)
        .find((candidate) => candidate.identifier === swipeGesture.pointerId);
      if (touch) finishSwipeGesture(touch.identifier, touch.clientX, touch.clientY);
      else resetSwipeGesture();
    }, { capture: true, passive: true });
    surface.addEventListener("touchcancel", resetSwipeGesture, { capture: true, passive: true });
  }

  function bindSwipeNavigation() {
    bindSwipeSurface(elements.viewer);

    window.addEventListener("message", (event) => {
      const direction = event.data?.direction;
      if (
        state.mode !== "single"
        || event.data?.type !== "ramen-bench:swipe"
        || (direction !== -1 && direction !== 1)
      ) {
        return;
      }
      const activeIframe = framesById.get(state.activeId)?.querySelector("iframe");
      if (!activeIframe || event.source !== activeIframe.contentWindow) return;
      move(direction);
    });

    window.addEventListener("blur", resetSwipeGesture);
  }

  function renderLeaderboard() {
    elements.leaderboard.replaceChildren();
    const rated = entries
      .map((entry) => ({ entry, rating: ratingsByRun.get(entry.id) }))
      .filter((item) => item.rating?.count)
      .sort((a, b) => b.rating.average - a.rating.average
        || b.rating.count - a.rating.count
        || a.entry.name.localeCompare(b.entry.name));

    if (!rated.length) {
      const empty = document.createElement("p");
      empty.className = "leaderboard-empty";
      empty.textContent = !ratingsApiUrl
        ? "The shared leaderboard will appear when the ratings API is configured."
        : entries.length
          ? "No ratings yet—taste a few bowls first."
          : "The leaderboard will appear once entries are added and rated.";
      elements.leaderboard.append(empty);
      return;
    }

    const list = document.createElement("div");
    list.className = "leaderboard-list";
    rated.forEach(({ entry, rating }, index) => {
      const row = document.createElement("div");
      row.className = "leaderboard-row";
      const rank = document.createElement("span");
      rank.className = "leaderboard-rank";
      rank.textContent = String(index + 1).padStart(2, "0");
      const name = document.createElement("span");
      name.className = "leaderboard-name";
      name.textContent = entry.badge ? `${entry.name} · ${entry.badge}` : entry.name;
      const score = document.createElement("span");
      score.className = "leaderboard-score";
      score.textContent = `${formatAverage(rating.average)} ★ · ${rating.count}`;
      row.append(rank, name, score);
      list.append(row);
    });
    elements.leaderboard.append(list);
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (dialog.id === "leaderboard-dialog") {
      renderLeaderboard();
      fetchVotes({ quiet: false }).then(() => {
        renderList();
        refreshRatingControls();
        renderLeaderboard();
      });
    }
    if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
    closeSidebar();
  }

  function openSidebar() {
    elements.sidebar.classList.add("open");
    elements.scrim.classList.add("open");
    elements.mobileMenu.setAttribute("aria-expanded", "true");
  }

  function closeSidebar() {
    elements.sidebar.classList.remove("open");
    elements.scrim.classList.remove("open");
    elements.mobileMenu.setAttribute("aria-expanded", "false");
  }

  function showToast(message) {
    window.clearTimeout(toastTimer);
    elements.toast.textContent = message;
    elements.toast.classList.add("visible");
    toastTimer = window.setTimeout(() => elements.toast.classList.remove("visible"), 2200);
  }

  async function copyPrompt() {
    const prompt = elements.promptText.textContent.trim();
    try {
      await navigator.clipboard.writeText(prompt);
      elements.copyPrompt.querySelector("span").textContent = "Copied";
      showToast("Prompt copied to clipboard.");
      window.setTimeout(() => {
        elements.copyPrompt.querySelector("span").textContent = "Copy prompt";
      }, 1800);
    } catch (_error) {
      showToast("Copy was blocked. Select the prompt text instead.");
    }
  }

  function bindEvents() {
    bindSwipeNavigation();
    elements.search.addEventListener("input", applyFilters);
    elements.creatorFilter.addEventListener("change", applyFilters);
    elements.harnessFilter.addEventListener("change", applyFilters);
    elements.clearFilters.addEventListener("click", () => {
      elements.search.value = "";
      elements.creatorFilter.value = "";
      elements.harnessFilter.value = "";
      applyFilters();
      elements.search.focus();
    });
    elements.singleMode.addEventListener("click", () => setMode("single"));
    elements.gridMode.addEventListener("click", () => setMode("grid"));
    elements.previous.addEventListener("click", () => move(-1));
    elements.next.addEventListener("click", () => move(1));
    elements.brand.addEventListener("click", (event) => {
      event.preventDefault();
      const firstEntry = entriesInSidebarOrder()[0] || entriesInSidebarOrder(entries)[0];
      if (!firstEntry) return;
      chooseSingleEntry(firstEntry);
      elements.list.scrollTop = 0;
    });
    elements.mobileMenu.addEventListener("click", () => {
      elements.sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
    });
    elements.scrim.addEventListener("click", closeSidebar);
    elements.copyPrompt.addEventListener("click", copyPrompt);

    document.querySelectorAll("[data-dialog]").forEach((trigger) => {
      trigger.addEventListener("click", (event) => {
        event.preventDefault();
        openDialog(document.getElementById(trigger.dataset.dialog));
      });
    });

    document.querySelectorAll("dialog").forEach((dialog) => {
      dialog.querySelector(".modal-close")?.addEventListener("click", () => dialog.close());
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });

    document.addEventListener("keydown", (event) => {
      const typing = /^(BUTTON|INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName);
      if (typing || state.mode !== "single" || entriesInSidebarOrder().length < 2) return;
      if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
        event.preventDefault();
        move(-1);
      }
      if (event.key === "ArrowRight" || event.key === "ArrowDown") {
        event.preventDefault();
        move(1);
      }
    });

    window.addEventListener("hashchange", () => {
      const entry = entryFromHash();
      if (!entry) return;
      state.activeId = entry.id;
      expandEntryModel(entry);
      if (state.mode !== "single") setMode("single");
      else render();
      revealEntryInSidebar(entry.id);
    });
  }

  async function init() {
    bindEvents();
    setWelcome("Setting the table", "Loading the kitchen…", "Reading the benchmark registry.", false);
    elements.previous.hidden = true;
    elements.next.hidden = true;

    try {
      [entries] = await Promise.all([loadEntries(), fetchVotes()]);
      const hashEntry = entryFromHash();
      const initialEntry = hashEntry || entriesInSidebarOrder(entries)[0] || null;
      state.activeId = initialEntry?.id || null;
      expandEntryModel(initialEntry);
      state.selectedIds = new Set(entries.slice(0, 4).map((entry) => entry.id));
      populateSelect(elements.creatorFilter, uniqueValues("creator"));
      populateSelect(elements.harnessFilter, uniqueValues("harness"));
      buildFrames();
      applyFilters();
      renderViewer();
      revealEntryInSidebar(state.activeId);
    } catch (error) {
      console.error(error);
      elements.filterCount.textContent = "Registry unavailable";
      renderList();
      setWelcome(
        "The kitchen is closed",
        "The registry could not be loaded.",
        "Serve the repository through a local web server and check registry.json.",
        false
      );
    }
  }

  init();
})();
