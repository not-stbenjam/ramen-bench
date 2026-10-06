import { contrastingModelColors, descendingScale, rewardExtent, axisValue, byEffort, generationTimestamp, newestGeneratedFirst, modelLabelAnchor, validScore, validWeights, reweightScore, withHumanScore, criteria as defaultCriteria, weights as defaultWeights } from "./scores-data.mjs";

const $ = id => document.getElementById(id);
let rows = [];
let models = [];
const selectedModels = new Set();
let criteria = defaultCriteria, weights = defaultWeights, procedural = true;
const labels = { noodles: "Noodles", toppings: "Toppings", bowl: "Bowl", broth: "Broth", composition: "Composition", vegan: "Vegan", appetizing: "Appetizing", steam: "Steam", aesthetics: "Aesthetics", interaction: "Interaction", reduced_motion: "Reduced motion" };
const judgeName = judge => `${judge.model.replace("openrouter/", "")}${judge.effort ? ` · ${judge.effort}` : ""}`;
const number = value => Number.isFinite(value) ? value.toFixed(3) : "—";
const modelKey = run => `${run.vendor.slug}/${run.model.slug}`;
const modelName = run => `${run.vendor.displayName} · ${run.model.displayName}`;
let modelColors = new Map();
const color = key => modelColors.get(key);

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function svgNode(tag, attrs = {}, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
  if (text !== undefined) node.textContent = text;
  return node;
}

function updateModelFilter() {
  const vendor = $("vendor").value;
  const visible = models.filter(model => !vendor || model.vendor === vendor);
  for (const option of $("model-options").querySelectorAll(".model-option")) {
    option.hidden = Boolean(vendor && option.dataset.vendor !== vendor);
    const input = option.querySelector("input");
    input.checked = selectedModels.has(input.value);
  }
  const count = visible.filter(model => selectedModels.has(model.key)).length;
  $("model-summary").textContent = count === visible.length ? "All models" : `${count} of ${visible.length} models`;
}

function positionTooltip(x, y) {
  const tooltip = $("plot-tooltip");
  const box = tooltip.getBoundingClientRect();
  const left = x + box.width + 16 > innerWidth ? x - box.width - 12 : x + 12;
  const top = y + box.height + 16 > innerHeight ? y - box.height - 12 : y + 12;
  tooltip.style.left = `${Math.max(12, Math.min(innerWidth - box.width - 12, left))}px`;
  tooltip.style.top = `${Math.max(12, Math.min(innerHeight - box.height - 12, top))}px`;
}

function showTooltip(row, x, y) {
  const tooltip = $("plot-tooltip");
  const cost = axisValue(row.run, "cost");
  const heading = element("strong", `${modelName(row.run)} · ${row.run.variation.displayName}`);
  tooltip.replaceChildren(heading,
    element("p", `Reward ${number(row.reward)} · LLM ${number(row.llmReward)}`),
    element("p", `Human ${number(row.humanScore)}`),
    element("p", `Cost ${cost === null ? "unknown" : `$${cost.toFixed(3)}${row.run.usage.cost.estimated ? " (estimated)" : ""}`}`),
    element("p", `Time ${number(axisValue(row.run, "time"))} minutes`));
  tooltip.hidden = false;
  positionTooltip(x, y);
}

function showNotes(row, toggle = false) {
  const selected = document.getElementById(`row-${row.runId}`);
  const panel = document.getElementById(`notes-${row.runId}`);
  const opening = !toggle || panel.hidden;
  document.querySelectorAll(".result-row").forEach(node => {
    node.classList.remove("selected");
    node.querySelector("button").setAttribute("aria-expanded", "false");
  });
  document.querySelectorAll(".notes-row").forEach(node => { node.hidden = true; });
  if (!opening) return;
  selected.classList.add("selected");
  selected.querySelector("button").setAttribute("aria-expanded", "true");
  panel.hidden = false;
  const heading = element("h3", `${row.run.model.displayName} · ${row.run.variation.displayName} · ${number(row.reward)}`);
  const bowl = element("a", "View original bowl ↗");
  bowl.href = `${row.runId}/${row.run.artifacts.result}`;
  bowl.target = "_blank";
  bowl.rel = "noopener";
  const spread = row.rewardStdDev === null ? "No between-judge spread estimate." : `Standard deviation between judge means: ${number(row.rewardStdDev)}. This measures judge disagreement, not variation across model runs.`;
  const content = element("div", undefined, "judge-review");
  content.append(element("p", row.humanScore === null
    ? `No human rating yet. Current LLM score: ${number(row.llmReward)}. Original LLM weighting: ${number(row.sourceReward)}.`
    : `Human score: ${number(row.humanScore)} (${Math.round(row.humanWeight * 100)}%). LLM score: ${number(row.llmReward)} (${Math.round((1 - row.humanWeight) * 100)}%). Combined: ${number(row.reward)}. Original LLM weighting: ${number(row.sourceReward)}.`));
  if (row.scores.standalone === 0 || row.scores.procedural === 0) {
    content.append(element("p", `Compliance failed: ${[...row.standaloneViolations, ...(row.complianceViolations || [])].join("; ")}`));
  } else {
    const scroll = element("div", undefined, "table-scroll");
    const table = element("table");
    const caption = element("caption", "Per-judge rewards and visual criteria", "sr-only");
    const head = element("thead"); const header = element("tr");
    for (const label of ["Judge", "Reward", ...criteria.map(key => labels[key])]) {
      const th = element("th", label); th.scope = "col"; header.append(th);
    }
    head.append(header);
    const body = element("tbody");
    for (const judge of row.judgeResults) {
      const line = element("tr");
      line.append(element("td", judgeName(judge)), element("td", number(judge.reward)));
      for (const key of criteria) line.append(element("td", number(judge.scores[key])));
      body.append(line);
    }
    table.append(caption, head, body); scroll.append(table); content.append(scroll);
    for (const judge of row.judgeResults) {
      const details = element("details");
      details.append(element("summary", `${judgeName(judge)} · ${number(judge.reward)} · sample spread ${number(judge.sampleStdDev)}`));
      const dl = element("dl");
      for (const key of criteria) {
        const explanation = element("dd");
        judge.judgments.forEach((sample, index) => explanation.append(element("p", `Sample ${index + 1}: ${sample[key].reason}`)));
        dl.append(element("dt", `${labels[key]} · ${number(judge.scores[key])}`), explanation);
      }
      if (judge.diagnostics) {
        const explanation = element("dd");
        judge.judgments.forEach((sample, index) => explanation.append(element("p", `Sample ${index + 1}: ${sample.vegan.reason}`)));
        dl.append(element("dt", `Vegan compliance · ${number(judge.diagnostics.vegan)}`), explanation);
      }
      details.append(dl); content.append(details);
    }
  }
  if (row.diagnostics?.veganFailed) content.prepend(element("p", "Vegan compliance failed: a majority of judges identify clear animal-derived ingredients. Composite reward is zero."));
  panel.querySelector(".notes").replaceChildren(heading, bowl, element("p", spread), content);
}

function renderTable(filtered) {
  const header = element("tr");
  for (const label of ["Model / effort", "Reward", "Human judge", "LLM score", "Cost", "Minutes", "Standalone", ...(procedural ? ["Procedural", "Vegan compliance"] : []), ...criteria.map(key => `${labels[key]} ${Math.round(weights[key] * 100)}%`)]) {
    const th = element("th", label); th.scope = "col"; header.append(th);
  }
  $("criteria-heading").replaceChildren(header);
  $("results").replaceChildren();
  for (const row of [...filtered].sort(newestGeneratedFirst)) {
    const tr = element("tr");
    tr.id = `row-${row.runId}`;
    tr.className = "result-row";
    const identity = element("td");
    const notes = element("button", undefined, "expand-row");
    const indicator = element("span", "▸", "expand-indicator");
    indicator.setAttribute("aria-hidden", "true");
    notes.append(indicator, document.createTextNode(row.run.model.displayName));
    notes.setAttribute("aria-label", `Tasting notes for ${row.run.model.displayName} ${row.run.variation.displayName}`);
    notes.setAttribute("aria-expanded", "false");
    notes.setAttribute("aria-controls", `notes-${row.runId}`);
    identity.append(notes, element("small", `${row.run.vendor.displayName} / ${row.run.variation.displayName}`));
    tr.addEventListener("click", () => showNotes(row, true));
    const reward = element("td", number(row.reward));
    const reference = Number.isFinite(row.humanScore) ? number(row.humanScore) : "—";
    const cost = axisValue(row.run, "cost");
    tr.append(identity, reward, element("td", reference), element("td", number(row.llmReward)), element("td", cost === null ? "—" : `$${cost.toFixed(3)}${row.run.usage.cost.estimated ? " ≈" : ""}`), element("td", number(axisValue(row.run, "time"))));
    tr.append(element("td", number(row.scores.standalone)));
    if (procedural) tr.append(element("td", number(row.scores.procedural)), element("td", number(row.diagnostics?.vegan)));
    for (const key of criteria) tr.append(element("td", number(row.scores[key])));
    const detail = element("tr", undefined, "notes-row");
    detail.id = `notes-${row.runId}`;
    detail.hidden = true;
    const cell = element("td");
    cell.colSpan = 7 + criteria.length + (procedural ? 2 : 0);
    cell.append(element("div", undefined, "notes"));
    detail.append(cell);
    $("results").append(tr, detail);
  }
}

function render() {
  const vendor = $("vendor").value;
  const filtered = rows.filter(row => (!vendor || row.run.vendor.slug === vendor) && selectedModels.has(modelKey(row.run)));
  const axis = $("axis").value;
  const log = $("log").checked;
  $("recorded").disabled = axis !== "cost";
  $("plot").replaceChildren();
  $("plot-tooltip").hidden = true;
  $("legend").replaceChildren();
  renderTable(filtered);
  const points = filtered.map(row => ({ ...row, x: axisValue(row.run, axis) })).filter(row =>
    row.reward > 0 && row.x !== null && (!log || row.x > 0) &&
    (axis !== "cost" || !$("recorded").checked || !row.run.usage.cost.estimated)
  );
  $("empty").hidden = points.length > 0;
  $("empty").querySelector("h3").textContent = rows.length ? "No points match this view." : "The tasting hasn’t started yet.";
  $("empty").querySelector("p").textContent = rows.length ? "Zero-score bowls remain in the table. Try another model, include estimated costs, or switch off the log scale to include zero cost or time values." : "Existing bowls are waiting for their first visual evaluation.";
  const zeroCount = filtered.filter(row => row.reward === 0).length;
  $("plot-note").textContent = `${points.length} plotted; ${zeroCount} zero-score bowls omitted from the chart; ${filtered.length - points.length - zeroCount} omitted for missing values or active axis filters. ≈ marks estimated generation costs. Unscored bowls are never assigned a zero.`;
  if (!points.length) return;
  const svg = svgNode("svg", { viewBox: "0 0 1280 650", role: "img", "aria-labelledby": "plot-title plot-description" });
  svg.append(svgNode("title", { id: "plot-title" }, "Composite reward versus generation " + axis), svgNode("desc", { id: "plot-description" }, "Up and to the right is better: higher reward and lower cost or time. Model names label their lines; each solid dot is labeled with its effort. The same values are in the table below."));
  const left = 85, top = 50, width = 1110, height = 515;
  const scale = descendingScale(points.map(point => point.x), log);
  const xPos = value => left + scale.position(value) * width;
  const extent = rewardExtent(points.map(point => point.reward));
  const yPos = value => top + (extent.max - value) / (extent.max - extent.min) * height;
  for (let i = 0; i <= 5; i++) {
    const reward = extent.min + (extent.max - extent.min) * i / 5;
    const y = yPos(reward);
    svg.append(svgNode("line", { x1: left, x2: left + width, y1: y, y2: y, stroke: "#3e2b20" }), svgNode("text", { x: left - 16, y: y + 5, "text-anchor": "end", class: "reward-tick" }, number(reward)));
    const original = scale.valueAt(i / 5);
    const x = left + width * i / 5;
    svg.append(svgNode("text", { x, y: top + height + 28, "text-anchor": "middle" }, axis === "cost" ? `$${original.toPrecision(2)}` : original.toPrecision(2)));
  }
  svg.append(svgNode("text", { x: left + width / 2, y: 625, "text-anchor": "middle" }, `${axis === "cost" ? "Generation cost (USD)" : "Generation time (minutes)"}${log ? " · log scale" : ""} · lower →`), svgNode("text", { x: 20, y: 305, transform: "rotate(-90 20 305)", "text-anchor": "middle" }, "Composite reward →"));
  const groups = new Map();
  for (const point of points) {
    const key = modelKey(point.run);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(point);
  }
  const occupied = points.map(point => ({ x: xPos(point.x) - 10, y: yPos(point.reward) - 10, width: 20, height: 20 }));
  const labelLayer = svgNode("g", { class: "plot-labels" });
  function addLabel(text, x, y, className, hue) {
    const labelWidth = text.length * (className === "model-label" ? 7.5 : 5.5) + 8;
    const labelHeight = className === "model-label" ? 19 : 16;
    const offsets = className === "model-label" ? [[0, -24], [0, -48], [70, -24], [-70, -24], [0, -76], [70, -48], [-70, -48], [0, 32], [0, 58], [0, 84]] : [[0, -17], [0, 27], [30, -17], [-30, -17], [30, 27], [-30, 27], [0, -40], [0, 50]];
    const candidates = offsets.map(([dx, dy], index) => {
      const cx = Math.max(left + labelWidth / 2, Math.min(left + width - labelWidth / 2, x + dx));
      const cy = Math.max(18, Math.min(top + height - 3, y + dy));
      const box = { x: cx - labelWidth / 2, y: cy - 15, width: labelWidth, height: labelHeight };
      const overlap = occupied.reduce((sum, other) => sum + Math.max(0, Math.min(box.x + box.width, other.x + other.width) - Math.max(box.x, other.x)) * Math.max(0, Math.min(box.y + box.height, other.y + other.height) - Math.max(box.y, other.y)), 0);
      return { cx, cy, box, score: overlap * 1000 + index };
    });
    candidates.sort((a, b) => a.score - b.score);
    const label = candidates[0];
    occupied.push(label.box);
    if (Math.hypot(label.cx - x, label.cy - y) > 32) {
      labelLayer.append(svgNode("line", { x1: x, y1: y, x2: label.cx, y2: label.cy - 6, stroke: hue, "stroke-opacity": ".4" }));
    }
    labelLayer.append(svgNode("text", { x: label.cx, y: label.cy, "text-anchor": "middle", class: className, ...(className === "model-label" ? { style: `fill: ${hue}` } : {}) }, text));
  }
  // Reserve space for all model names before placing the smaller effort labels.
  for (const [key, group] of groups) {
    const anchor = modelLabelAnchor(group.map(point => ({ x: xPos(point.x), y: yPos(point.reward) })));
    addLabel(group[0].run.model.displayName, anchor.x, anchor.y, "model-label", color(key));
  }
  for (const [key, group] of groups) {
    group.sort(byEffort);
    const hue = color(key);
    if (group.length > 1) svg.append(svgNode("polyline", { points: group.map(p => `${xPos(p.x)},${yPos(p.reward)}`).join(" "), fill: "none", stroke: hue, "stroke-opacity": ".65", "stroke-width": "2" }));
    for (const point of group) {
      const description = `${modelName(point.run)}, ${point.run.variation.displayName}: reward ${number(point.reward)}, ${axis === "cost" ? "$" : ""}${number(point.x)}${axis === "cost" && point.run.usage.cost.estimated ? " (estimated)" : ""}`;
      const circle = svgNode("circle", { cx: xPos(point.x), cy: yPos(point.reward), r: 6, fill: hue, stroke: hue, "stroke-width": "2", tabindex: "0", role: "button", "aria-label": description, "aria-describedby": "plot-tooltip" });
      circle.addEventListener("click", () => showNotes(point));
      circle.addEventListener("pointerenter", event => showTooltip(point, event.clientX, event.clientY));
      circle.addEventListener("pointermove", event => positionTooltip(event.clientX, event.clientY));
      circle.addEventListener("pointerleave", () => { $("plot-tooltip").hidden = true; });
      circle.addEventListener("focus", () => { const box = circle.getBoundingClientRect(); showTooltip(point, box.x + box.width / 2, box.y + box.height / 2); });
      circle.addEventListener("blur", () => { $("plot-tooltip").hidden = true; });
      circle.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showNotes(point); } });
      svg.append(circle);
      addLabel(point.run.variation.displayName, xPos(point.x), yPos(point.reward), "effort-label", hue);
    }
    const item = element("span");
    const swatch = element("i"); swatch.style.background = hue;
    item.append(swatch, document.createTextNode(modelName(group[0].run)));
    $("legend").append(item);
  }
  svg.append(labelLayer);
  $("plot").append(svg);
}

async function readJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  return response.json();
}

async function start() {
  const [registry, scores, display, panel] = await Promise.all([readJSON("registry.json"), readJSON("scores.json"), readJSON("harbor/weights.json"), readJSON("harbor/human-scores.json")]);
  if (scores.schemaVersion !== 1 || !Array.isArray(scores.results)) throw new Error("Unsupported score format");
  const runs = await Promise.all(registry.runs.map(readJSON));
  const manifests = new Map(runs.map(run => [run.id, run]));
  const humanRatings = new Map();
  if (panel.schemaVersion !== 1 || !Number.isFinite(panel.weight) || panel.weight < 0 || panel.weight > 1 || typeof panel.panelName !== "string" || !panel.panelName.trim() || !Array.isArray(panel.ratings)) throw new Error("Invalid human panel configuration");
  for (const rating of panel.ratings) {
    if (!manifests.has(rating.runId) || humanRatings.has(rating.runId) || (rating.humanScore !== null && (!Number.isFinite(rating.humanScore) || rating.humanScore < 0 || rating.humanScore > 1))) throw new Error("Invalid or duplicate human rating");
    humanRatings.set(rating.runId, rating.humanScore);
  }
  const seen = new Set();
  criteria = scores.evaluation?.criteria || defaultCriteria;
  const sourceWeights = scores.evaluation?.weights || Object.fromEntries(criteria.map(key => [key, 1 / criteria.length]));
  procedural = scores.evaluation?.rubricVersion === "ramen-v3";
  weights = procedural ? display.weights : sourceWeights;
  if (display.schemaVersion !== 1 || !validWeights(weights, criteria)) throw new Error("Invalid display weights");
  rows = scores.results.map(row => {
    if (!manifests.has(row.runId) || !validScore(row, criteria, sourceWeights) || seen.has(row.runId)) throw new Error("Invalid or duplicate evaluation");
    seen.add(row.runId);
    return { ...withHumanScore(reweightScore(row, weights, criteria), humanRatings.get(row.runId) ?? null, panel.weight), run: manifests.get(row.runId) };
  });
  $("coverage").textContent = `${rows.length} of ${runs.length} bowls evaluated · higher quality, lower cost`;
  if (scores.evaluation) $("judge-note").textContent = `${scores.evaluation.rubricVersion} · ${scores.evaluation.judge.models.map(model => `${model}${scores.evaluation.judge.efforts ? ` @ ${scores.evaluation.judge.efforts[model]}` : ""}`).join(" + ")} · ${scores.evaluation.judge.samples} samples per judge. Judges have equal weight. Click a row to expand its notes.`;
  const weightDescription = criteria.map(key => `${labels[key].toLowerCase()} ${Math.round(weights[key] * 100)}%`).join(", ");
  $("scoring-method").textContent = `${panel.panelName}: ${Math.round(panel.weight * 100)}% human, ${Math.round((1 - panel.weight) * 100)}% LLM when rated; unrated bowls use their LLM score. ${panel.panelNote} Within the LLM score: ${weightDescription}. Compliance failures remain zero. Each LLM judge has equal weight. These weights can change without rejudging; raw downloads retain the original LLM weighting.`;
  const vendors = new Map(rows.map(row => [row.run.vendor.slug, row.run.vendor.displayName]));
  for (const [key, name] of [...vendors].sort((a, b) => a[1].localeCompare(b[1]))) {
    const option = element("option", name); option.value = key; $("vendor").append(option);
  }
  const modelMap = new Map();
  for (const row of rows) {
    const key = modelKey(row.run), generatedAt = generationTimestamp(row.run);
    const model = modelMap.get(key);
    if (model) model.generatedAt = Math.max(model.generatedAt, generatedAt);
    else modelMap.set(key, { key, name: modelName(row.run), vendor: row.run.vendor.slug, generatedAt });
  }
  models = [...modelMap.values()];
  const paletteOrder = [...models].sort((a, b) => (Date.parse(registry.modelAddedAt?.[b.key]) || 0) - (Date.parse(registry.modelAddedAt?.[a.key]) || 0));
  modelColors = contrastingModelColors(paletteOrder.map(model => model.key));
  models.sort((a, b) => a.generatedAt === b.generatedAt ? a.name.localeCompare(b.name) : a.generatedAt > b.generatedAt ? -1 : 1);
  for (const model of models) {
    selectedModels.add(model.key);
    const label = element("label", undefined, "model-option"); label.dataset.vendor = model.vendor;
    const input = element("input"); input.type = "checkbox"; input.value = model.key; input.checked = true;
    input.addEventListener("change", () => {
      if (input.checked) selectedModels.add(model.key); else selectedModels.delete(model.key);
      updateModelFilter(); render();
    });
    label.append(input, document.createTextNode(model.name));
    $("model-options").append(label);
  }
  $("models-all").addEventListener("click", () => { for (const model of models) selectedModels.add(model.key); updateModelFilter(); render(); });
  $("models-none").addEventListener("click", () => { selectedModels.clear(); updateModelFilter(); render(); });
  $("vendor").addEventListener("change", () => { updateModelFilter(); render(); });
  $("model-menu").addEventListener("keydown", event => { if (event.key === "Escape") { $("model-menu").open = false; $("model-summary").focus(); } });
  document.addEventListener("click", event => { if (!$("model-menu").contains(event.target)) $("model-menu").open = false; });
  addEventListener("scroll", () => { $("plot-tooltip").hidden = true; }, true);
  ["axis", "log", "recorded"].forEach(id => $(id).addEventListener("change", render));
  updateModelFilter();
  render();
}

start().catch(error => {
  $("coverage").textContent = "Evaluations could not be loaded. Please try again.";
  console.error(error);
});
