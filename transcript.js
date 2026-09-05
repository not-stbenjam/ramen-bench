(function () {
  "use strict";

  const BATCH_SIZE = 25;
  let events = [];
  let renderedEvents = 0;
  let toolNames = new Map();

  const elements = {
    title: document.querySelector("#transcript-title"),
    subtitle: document.querySelector("#transcript-subtitle"),
    status: document.querySelector("#transcript-status"),
    back: document.querySelector("#back-link"),
    links: document.querySelector("#artifact-links"),
    summary: document.querySelector("#run-summary"),
    transcript: document.querySelector("#transcript-events"),
    eventList: document.querySelector("#event-list"),
    eventCount: document.querySelector("#event-count"),
    loadMore: document.querySelector("#load-more")
  };

  async function fetchJson(url, label) {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) throw new Error(`${label} request failed with ${response.status}`);
    return { data: await response.json(), url: response.url };
  }

  function runIdFromHash() {
    try {
      const id = decodeURIComponent(window.location.hash.slice(1));
      return /^[a-z0-9][a-z0-9._-]*\/[a-z0-9][a-z0-9._-]*\/[a-z0-9][a-z0-9._-]*$/.test(id) ? id : "";
    } catch (_error) {
      return "";
    }
  }

  async function load() {
    const runId = runIdFromHash();
    if (!runId) throw new Error("Choose a transcript from a benchmark result.");

    elements.back.href = `./#${encodeURIComponent(runId)}`;
    const { data: registry, url: registryUrl } = await fetchJson("registry.json", "Registry");
    const manifestPath = Array.isArray(registry.runs)
      ? registry.runs.find((path) => String(path).replace(/^\.\//, "") === `${runId}/run.json`)
      : null;
    if (!manifestPath) throw new Error("That run is not listed in the benchmark registry.");

    const manifestUrl = new URL(manifestPath, registryUrl);
    const { data: run } = await fetchJson(manifestUrl, "Run metadata");
    if (run.id !== runId || !run.artifacts?.transcript) throw new Error("The run metadata is incomplete.");

    const harness = formatHarness(run.harness);
    elements.title.textContent = `${run.model.displayName} · ${run.variation.displayName}`;
    elements.subtitle.textContent = `${run.vendor.displayName} · ${harness}`;
    document.title = `${run.model.displayName} ${run.variation.displayName} transcript · Ramen Bench`;
    renderRunSummary(run, harness);

    const transcriptUrl = new URL(run.artifacts.transcript, manifestUrl);
    addArtifactLink(transcriptUrl, "View transcript JSON");
    if (run.artifacts.rawTranscript) {
      addArtifactLink(new URL(run.artifacts.rawTranscript, manifestUrl), "View raw harness export");
    }
    elements.links.hidden = false;

    const { data: transcript } = await fetchJson(transcriptUrl, "Transcript");
    if (transcript.runId !== runId || !Array.isArray(transcript.events)) {
      throw new Error("The transcript format is invalid.");
    }

    events = transcript.events;
    toolNames = new Map(
      events
        .filter((event) => event.type === "tool_call")
        .map((event) => [event.id, event.tool])
    );
    elements.status.hidden = true;
    elements.transcript.hidden = false;
    renderNextBatch();
  }

  function formatHarness(harness) {
    if (!harness.version) return harness.name;
    return String(harness.version).toLocaleLowerCase().startsWith(`${harness.name.toLocaleLowerCase()} `)
      ? harness.version
      : `${harness.name} ${harness.version}`;
  }

  function renderRunSummary(run, harness) {
    const wrapper = document.createElement("section");
    wrapper.className = "run-summary";
    const cards = document.createElement("div");
    cards.className = "summary-cards";
    const tokens = run.usage?.tokens || {};
    addSummaryCard(cards, "Total cost", formatCost(run.usage?.cost) || "Unknown");
    addSummaryCard(cards, "Total tokens", formatInteger(tokens.total) || "Unknown");
    addSummaryCard(cards, "Output tokens", formatInteger(tokens.output) || "Unknown");
    addSummaryCard(cards, "Wall time", formatDuration(run.timing?.wallDurationMs) || "Unknown");

    const details = document.createElement("details");
    details.className = "usage-details";
    const summary = document.createElement("summary");
    summary.textContent = "Full run and usage details";
    const list = document.createElement("dl");
    addDetail(list, "Run ID", run.id);
    addDetail(list, "Provider model", run.model.providerModelId);
    addDetail(list, "Variation", run.variation.displayName);
    addDetail(list, "Reasoning effort", run.variation.reasoningEffort);
    addDetail(list, "Harness", harness);
    addDetail(list, "Started", formatTimestamp(run.timing?.startedAt));
    addDetail(list, "API time", formatDuration(run.timing?.apiDurationMs));
    addDetail(list, "Input tokens", formatInteger(tokens.input));
    addDetail(list, "Cached input", formatInteger(tokens.cachedInput));
    addDetail(list, "Cache creation", formatInteger(tokens.cacheCreationInput));
    addDetail(list, "Reasoning tokens", formatInteger(tokens.reasoning));
    addDetail(list, "Output tokens", formatInteger(tokens.output));
    addDetail(list, "Total tokens", formatInteger(tokens.total));
    addCostDetails(list, run.usage?.cost);
    addDetail(list, "Requests", formatInteger(run.usage?.requests));
    addDetail(list, "Turns", formatInteger(run.usage?.turns));
    addDetail(list, "Tool calls", formatInteger(run.usage?.toolCalls));
    if (typeof run.usage?.providerReported === "boolean") {
      addDetail(list, "Usage source", run.usage.providerReported ? "Provider reported" : "Reconstructed / estimated");
    }
    details.append(summary, list);
    wrapper.append(cards, details);
    elements.summary.replaceChildren(wrapper);
    elements.summary.hidden = false;
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
    const currency = cost.currency || "USD";
    for (const [key, label] of [
      ["input", "Input cost"],
      ["cachedInput", "Cached input cost"],
      ["cacheCreationInput", "Cache creation cost"],
      ["output", "Output cost"],
      ["other", "Other cost"],
      ["total", "Total cost"]
    ]) {
      if (!Number.isFinite(cost[key])) continue;
      addDetail(list, label, `${cost[key].toFixed(6)} ${currency}${key === "total" && cost.estimated ? " (estimated)" : ""}`);
    }
  }

  function addArtifactLink(url, label) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = label;
    elements.links.append(link);
  }

  function renderNextBatch() {
    const end = Math.min(renderedEvents + BATCH_SIZE, events.length);
    const fragment = document.createDocumentFragment();
    for (; renderedEvents < end; renderedEvents += 1) {
      fragment.append(createTranscriptEvent(events[renderedEvents]));
    }
    elements.eventList.append(fragment);
    elements.eventCount.textContent = events.length
      ? `${renderedEvents.toLocaleString()} of ${events.length.toLocaleString()}`
      : "No public events";
    elements.loadMore.hidden = renderedEvents >= events.length;
    if (!elements.loadMore.hidden) {
      const remaining = Math.min(BATCH_SIZE, events.length - renderedEvents);
      elements.loadMore.textContent = `Load ${remaining} more event${remaining === 1 ? "" : "s"}`;
    }
  }

  function createTranscriptEvent(event) {
    const isTool = event.type === "tool_call" || event.type === "tool_result";
    const container = document.createElement(isTool ? "details" : "article");
    container.className = `transcript-event event-${event.type || "unknown"}`;
    const header = document.createElement(isTool ? "summary" : "header");
    const label = document.createElement("strong");
    label.textContent = eventLabel(event);
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

  function eventLabel(event) {
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

  function formatInteger(value) {
    return Number.isFinite(value) ? new Intl.NumberFormat("en-US").format(value) : "";
  }

  function formatTimestamp(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
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

  elements.loadMore.addEventListener("click", renderNextBatch);
  load().catch((error) => {
    elements.status.textContent = `Transcript unavailable: ${error.message}`;
    elements.status.classList.add("error");
  });
})();
