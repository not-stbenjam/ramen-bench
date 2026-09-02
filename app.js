(function () {
  "use strict";

  let entries = [];
  let toastTimer;
  const ratingsApiUrl = String(window.RAMEN_BENCH_CONFIG?.ratingsApiUrl || "").replace(/\/+$/, "");
  const ratingsByRun = new Map();
  const submittedRatings = new Map();

  const elements = {
    sidebar: document.querySelector("#sidebar"),
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
    transcriptDialog: document.querySelector("#transcript-dialog"),
    transcriptTitle: document.querySelector("#transcript-title"),
    transcriptBody: document.querySelector("#transcript-body"),
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
      harness: run.harness.version
        ? `${run.harness.name} ${run.harness.version}`
        : run.harness.name,
      resultUrl: resolveArtifact(run.artifacts.result),
      transcriptUrl: resolveArtifact(run.artifacts.transcript),
      rawTranscriptUrl: resolveArtifact(run.artifacts.rawTranscript),
      manifestUrl,
      run
    };
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
    if (!ratingsApiUrl) {
      showToast("The shared ratings API has not been configured yet.");
      return;
    }

    control.classList.add("submitting");
    control.querySelectorAll("button").forEach((button) => { button.disabled = true; });
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
      submittedRatings.set(entry.id, rating);
      await fetchVotes();
      renderList();
      refreshRatingControls();
      showToast(`Rated ${entry.name} ${rating} out of 5.`);
    } catch (error) {
      console.warn(error);
      showToast("Your rating could not be saved. Please try again.");
    } finally {
      control.classList.remove("submitting");
      control.querySelectorAll("button").forEach((button) => { button.disabled = false; });
    }
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

    for (const [creator, groupRuns] of groupEntries(state.filteredEntries)) {
      const section = document.createElement("section");
      section.className = "model-group";
      const heading = document.createElement("h2");
      heading.className = "group-label";
      heading.textContent = creator;
      section.append(heading);
      for (const entry of groupRuns) section.append(createModelButton(entry));
      elements.list.append(section);
    }
  }

  function groupEntries(list) {
    const groups = new Map();
    for (const entry of list) {
      const key = entry.creator || "Other";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(entry);
    }
    return groups;
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
    name.textContent = entry.name;
    nameRow.append(name);

    if (entry.badge) {
      const badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = entry.badge;
      nameRow.append(badge);
    }

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
      state.activeId = entry.id;
      window.history.replaceState(null, "", `#${encodeURIComponent(entry.id)}`);
      render();
      closeSidebar();
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

  function setMode(mode) {
    if (mode === state.mode) return;
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
    elements.frames.replaceChildren();
    const hasEntries = entries.length > 0;
    elements.welcome.hidden = hasEntries;
    elements.previous.hidden = !hasEntries || state.mode !== "single" || entries.length < 2;
    elements.next.hidden = !hasEntries || state.mode !== "single" || entries.length < 2;

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
      return;
    }

    for (const entry of visibleEntries) elements.frames.append(createFrame(entry));
  }

  function createFrame(entry) {
    const frame = document.createElement("article");
    frame.className = "frame";

    const iframe = document.createElement("iframe");
    iframe.src = entry.resultUrl;
    iframe.title = `${entry.name} ramen benchmark result`;
    iframe.loading = "eager";
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

    const transcript = document.createElement("button");
    transcript.type = "button";
    transcript.className = "transcript-button";
    transcript.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="M6 3h9l3 3v15H6V3Z"></path><path d="M9 11h6M9 15h6M9 7h3"></path></svg><span>Transcript</span>';
    transcript.disabled = !entry.transcriptUrl;
    transcript.addEventListener("click", () => viewTranscript(entry));

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
    const current = submittedRatings.get(entry.id) || 0;

    for (let score = 1; score <= 5; score += 1) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `star${score <= current ? " filled" : ""}`;
      button.textContent = "★";
      button.title = `${score} star${score === 1 ? "" : "s"}`;
      button.setAttribute("aria-label", `${score} star${score === 1 ? "" : "s"}`);
      button.setAttribute("aria-pressed", String(score <= current));
      button.disabled = !ratingsApiUrl;
      button.addEventListener("click", () => submitVote(entry, score, control));
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
    const current = submittedRatings.get(runId) || 0;
    const community = ratingsByRun.get(runId);
    control.querySelectorAll(".star").forEach((star, index) => {
      const selected = index < current;
      star.classList.toggle("filled", selected);
      star.setAttribute("aria-pressed", String(selected));
    });
    const summary = control.querySelector(".vote-summary");
    if (!ratingsApiUrl) {
      summary.textContent = "Offline";
      control.title = "Shared ratings API not configured";
    } else if (community?.count) {
      summary.textContent = `${formatAverage(community.average)} · ${community.count}`;
      control.title = `${formatAverage(community.average)} from ${community.count} vote${community.count === 1 ? "" : "s"}`;
    } else {
      summary.textContent = "Rate";
      control.title = "No community votes yet";
    }
  }

  function formatAverage(value) {
    return Number.isFinite(value) ? value.toFixed(1) : "—";
  }

  function move(direction) {
    const currentIndex = Math.max(0, entries.findIndex((entry) => entry.id === state.activeId));
    const nextIndex = (currentIndex + direction + entries.length) % entries.length;
    state.activeId = entries[nextIndex].id;
    window.history.replaceState(null, "", `#${encodeURIComponent(state.activeId)}`);
    render();
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

  function buildRunSummary(entry) {
    const wrapper = document.createElement("section");
    wrapper.className = "run-summary";
    const cards = document.createElement("div");
    cards.className = "summary-cards";
    const tokens = entry.run.usage?.tokens || {};
    addSummaryCard(cards, "Total cost", formatCost(entry.run.usage?.cost) || "Unknown");
    addSummaryCard(cards, "Total tokens", formatTokens(tokens.total, false) || "Unknown");
    addSummaryCard(cards, "Output tokens", formatTokens(tokens.output, false) || "Unknown");
    addSummaryCard(cards, "Wall time", formatDuration(entry.run.timing?.wallDurationMs) || "Unknown");

    const details = document.createElement("details");
    details.className = "usage-details";
    const summary = document.createElement("summary");
    summary.textContent = "Full run and usage details";
    const table = document.createElement("dl");
    addDetail(table, "Run ID", entry.id);
    addDetail(table, "Provider model", entry.run.model.providerModelId);
    addDetail(table, "Variation", entry.run.variation.displayName);
    addDetail(table, "Reasoning effort", entry.run.variation.reasoningEffort);
    addDetail(table, "Harness", entry.harness);
    addDetail(table, "Started", formatTimestamp(entry.run.timing?.startedAt));
    addDetail(table, "API time", formatDuration(entry.run.timing?.apiDurationMs));
    addDetail(table, "Input tokens", formatInteger(tokens.input));
    addDetail(table, "Cached input", formatInteger(tokens.cachedInput));
    addDetail(table, "Cache creation", formatInteger(tokens.cacheCreationInput));
    addDetail(table, "Reasoning tokens", formatInteger(tokens.reasoning));
    addDetail(table, "Output tokens", formatInteger(tokens.output));
    addDetail(table, "Total tokens", formatInteger(tokens.total));
    addCostDetails(table, entry.run.usage?.cost);
    addDetail(table, "Requests", formatInteger(entry.run.usage?.requests));
    addDetail(table, "Turns", formatInteger(entry.run.usage?.turns));
    addDetail(table, "Tool calls", formatInteger(entry.run.usage?.toolCalls));
    if (typeof entry.run.usage?.providerReported === "boolean") {
      addDetail(table, "Usage source", entry.run.usage.providerReported ? "Provider reported" : "Reconstructed / estimated");
    }
    details.append(summary, table);
    wrapper.append(cards, details);
    return wrapper;
  }

  function addSummaryCard(container, label, value) {
    const card = document.createElement("div");
    const term = document.createElement("span");
    term.textContent = label;
    const data = document.createElement("strong");
    data.textContent = value;
    card.append(term, data);
    container.append(card);
  }

  function addDetail(list, label, value) {
    if (value === undefined || value === null || value === "") return;
    const term = document.createElement("dt");
    term.textContent = label;
    const description = document.createElement("dd");
    description.textContent = value;
    list.append(term, description);
  }

  function addCostDetails(list, cost) {
    if (!cost) return;
    const suffix = cost.currency || "USD";
    for (const [key, label] of [
      ["input", "Input cost"],
      ["cachedInput", "Cached input cost"],
      ["cacheCreationInput", "Cache creation cost"],
      ["output", "Output cost"],
      ["other", "Other cost"],
      ["total", "Total cost"]
    ]) {
      if (!Number.isFinite(cost[key])) continue;
      addDetail(list, label, `${cost[key].toFixed(6)} ${suffix}${key === "total" && cost.estimated ? " (estimated)" : ""}`);
    }
  }

  function formatInteger(value) {
    return Number.isFinite(value) ? new Intl.NumberFormat("en-US").format(value) : "";
  }

  function formatTimestamp(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
  }

  async function viewTranscript(entry) {
    elements.transcriptTitle.textContent = `${entry.name} · ${entry.badge}`;
    elements.transcriptBody.replaceChildren(buildRunSummary(entry));
    const loading = document.createElement("p");
    loading.className = "transcript-status";
    loading.textContent = "Loading session transcript…";
    elements.transcriptBody.append(loading);
    openDialog(elements.transcriptDialog);

    try {
      const response = await fetch(entry.transcriptUrl, { cache: "no-store" });
      if (!response.ok) throw new Error(`Transcript request failed with ${response.status}`);
      const text = await response.text();
      loading.remove();
      let transcript;
      try {
        transcript = JSON.parse(text);
      } catch (_error) {
        renderRawTranscript(text, entry);
        return;
      }
      renderTranscript(transcript, entry);
    } catch (error) {
      loading.textContent = `Transcript unavailable: ${error.message}`;
      loading.classList.add("error");
    }
  }

  function renderTranscript(transcript, entry) {
    const section = document.createElement("section");
    section.className = "transcript";
    const header = document.createElement("div");
    header.className = "transcript-header";
    const heading = document.createElement("h3");
    heading.textContent = "Session events";
    header.append(heading, artifactLink(entry.transcriptUrl, "View raw JSON"));
    section.append(header);

    const events = Array.isArray(transcript.events) ? transcript.events : [];
    const toolNames = new Map(
      events
        .filter((event) => event.type === "tool_call")
        .map((event) => [event.id, event.tool])
    );

    if (!events.length) {
      const empty = document.createElement("p");
      empty.className = "transcript-status";
      empty.textContent = "This transcript contains no public events.";
      section.append(empty);
    } else {
      events.forEach((event) => section.append(createTranscriptEvent(event, toolNames)));
    }

    if (entry.rawTranscriptUrl) {
      const raw = document.createElement("p");
      raw.className = "raw-transcript-link";
      raw.append("An untouched harness export is also available: ", artifactLink(entry.rawTranscriptUrl, "raw transcript"), ".");
      section.append(raw);
    }
    elements.transcriptBody.append(section);
  }

  function renderRawTranscript(text, entry) {
    const section = document.createElement("section");
    section.className = "transcript";
    const header = document.createElement("div");
    header.className = "transcript-header";
    const heading = document.createElement("h3");
    heading.textContent = "Raw session transcript";
    header.append(heading, artifactLink(entry.transcriptUrl, "Open file"));
    const pre = document.createElement("pre");
    pre.className = "raw-transcript";
    pre.textContent = text;
    section.append(header, pre);
    elements.transcriptBody.append(section);
  }

  function artifactLink(url, label) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = label;
    return link;
  }

  function createTranscriptEvent(event, toolNames) {
    const isTool = event.type === "tool_call" || event.type === "tool_result";
    const container = document.createElement(isTool ? "details" : "article");
    container.className = `transcript-event event-${event.type || "unknown"}`;
    const header = document.createElement(isTool ? "summary" : "header");
    const label = document.createElement("strong");
    label.textContent = eventLabel(event, toolNames);
    header.append(label);
    if (event.timestamp) {
      const time = document.createElement("time");
      time.dateTime = event.timestamp;
      time.textContent = formatTimestamp(event.timestamp);
      header.append(time);
    }

    const content = document.createElement("pre");
    content.textContent = eventContent(event);
    container.append(header, content);
    return container;
  }

  function eventLabel(event, toolNames) {
    if (event.type === "message") return event.role || "Message";
    if (event.type === "tool_call") return `Tool call · ${event.tool}`;
    if (event.type === "tool_result") return `Tool result · ${toolNames.get(event.callId) || event.callId}`;
    if (event.type === "error") return "Error";
    if (event.type === "note") return "Session note";
    return event.type || "Event";
  }

  function eventContent(event) {
    if (event.type === "message" || event.type === "note") return event.content || "";
    if (event.type === "error") return event.message || "";
    if (event.type === "tool_call") return stringify(event.input);
    if (event.type === "tool_result") return stringify(event.output);
    return stringify(event);
  }

  function stringify(value) {
    return typeof value === "string" ? value : JSON.stringify(value, null, 2);
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
      const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName);
      if (typing || state.mode !== "single" || entries.length < 2) return;
      if (event.key === "ArrowLeft") move(-1);
      if (event.key === "ArrowRight") move(1);
    });

    window.addEventListener("hashchange", () => {
      const entry = entryFromHash();
      if (!entry) return;
      state.activeId = entry.id;
      if (state.mode !== "single") setMode("single");
      else render();
    });
  }

  async function init() {
    bindEvents();
    setWelcome("Setting the table", "Loading the kitchen…", "Reading the benchmark registry.", false);
    elements.previous.hidden = true;
    elements.next.hidden = true;

    try {
      [entries] = await Promise.all([loadEntries(), fetchVotes()]);
      state.activeId = entryFromHash()?.id || entries[0]?.id || null;
      state.selectedIds = new Set(entries.slice(0, 4).map((entry) => entry.id));
      populateSelect(elements.creatorFilter, uniqueValues("creator"));
      populateSelect(elements.harnessFilter, uniqueValues("harness"));
      applyFilters();
      renderViewer();
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
