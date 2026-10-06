export const effortOrder = ["low", "medium", "high", "xhigh", "max", "ultra"];
export const criteria = ["noodles", "toppings", "bowl", "broth", "steam", "composition"];
export const weights = { noodles: .20, toppings: .25, bowl: .20, broth: .10, steam: .15, composition: .10 };

const linearRGB = value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4;
const luminance = rgb => .2126 * rgb[0] + .7152 * rgb[1] + .0722 * rgb[2];

function oklab([r, g, b]) {
  // Björn Ottosson's public-domain linear-sRGB transform:
  // https://bottosson.github.io/posts/oklab/
  const l = Math.cbrt(.4122214708 * r + .5363325363 * g + .0514459929 * b);
  const m = Math.cbrt(.2119034982 * r + .6806995451 * g + .1073969566 * b);
  const s = Math.cbrt(.0883024619 * r + .2817188376 * g + .6299787005 * b);
  return [.2104542553 * l + .793617785 * m - .0040720468 * s,
    1.9779984951 * l - 2.428592205 * m + .4505937099 * s,
    .0259040371 * l + .7827717662 * m - .808675766 * s];
}

export function contrastingModelColors(keys) {
  const background = luminance([27, 21, 17].map(v => linearRGB(v / 255)));
  const candidates = [];
  for (let hue = 0; hue < 360; hue += 10) {
    for (const saturation of [.85, 1]) for (const lightness of [.4, .5, .6]) {
      const a = saturation * Math.min(lightness, 1 - lightness);
      const channel = n => {
        const k = (n + hue / 30) % 12;
        return Math.round(255 * (lightness - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
      };
      const rgb = [channel(0), channel(8), channel(4)];
      const linear = rgb.map(v => linearRGB(v / 255));
      if ((luminance(linear) + .05) / (background + .05) < 4.5) continue;
      candidates.push({ hex: `#${rgb.map(v => v.toString(16).padStart(2, "0")).join("")}`, lab: oklab(linear) });
    }
  }
  const selected = [], colors = new Map();
  const distance = (a, b) => Math.hypot(.55 * (a[0] - b[0]), a[1] - b[1], a[2] - b[2]);
  for (const key of keys) {
    const vendor = key.split("/")[0];
    let best, bestScore = -Infinity;
    for (const candidate of candidates) {
      if (selected.some(color => color.hex === candidate.hex)) continue;
      const score = selected.length
        ? Math.min(...selected.map(color => distance(candidate.lab, color.lab) * (color.vendor === vendor ? .65 : 1)))
        : -distance(candidate.lab, oklab([255, 128, 0].map(v => linearRGB(v / 255))));
      if (score > bestScore) { best = candidate; bestScore = score; }
    }
    if (!best) throw new Error("Model color candidate pool exhausted");
    colors.set(key, best.hex);
    selected.push({ ...best, vendor });
  }
  return colors;
}

export function descendingScale(values, logarithmic = false) {
  const transform = value => logarithmic ? Math.log10(value) : value;
  let min = logarithmic ? Math.min(...values.map(transform)) : 0;
  let max = Math.max(...values.map(transform));
  if (max === min) {
    min -= logarithmic ? .3 : 0;
    max += logarithmic ? .3 : Math.max(1, max * .1);
  }
  return {
    position: value => (max - transform(value)) / (max - min),
    valueAt: fraction => {
      const value = max - fraction * (max - min);
      return logarithmic ? 10 ** value : value;
    },
  };
}

export function rewardExtent(values) {
  const lowest = Math.min(...values);
  return { min: lowest === 1 ? .95 : lowest, max: 1 };
}

export function axisValue(run, axis) {
  if (axis === "time") {
    const duration = run.timing?.wallDurationMs;
    return Number.isFinite(duration) && duration >= 0 ? duration / 60000 : null;
  }
  const cost = run.usage?.cost;
  return cost?.currency === "USD" && Number.isFinite(cost.total) && cost.total >= 0 ? cost.total : null;
}

export function byEffort(a, b) {
  return effortOrder.indexOf(a.run.variation.reasoningEffort || a.run.variation.slug)
    - effortOrder.indexOf(b.run.variation.reasoningEffort || b.run.variation.slug);
}

export function generationTimestamp(run) {
  const timestamp = Date.parse(run.timing?.completedAt || run.timing?.startedAt);
  return Number.isFinite(timestamp) ? timestamp : -Infinity;
}

export function newestGeneratedFirst(a, b) {
  const aTime = generationTimestamp(a.run), bTime = generationTimestamp(b.run);
  return aTime === bTime ? a.run.id.localeCompare(b.run.id) : aTime > bTime ? -1 : 1;
}

export function modelLabelAnchor(points) {
  // Anchor at the top of the line; nearby placement handles crowded peaks.
  return points.reduce((best, point) => point.y < best.y ? point : best);
}

export function validScore(row, keys = criteria, criterionWeights = weights) {
  if (!row || !Number.isFinite(row.reward) || row.reward < 0 || row.reward > 1) return false;
  if (row.scores?.standalone === 0 || row.scores?.procedural === 0) return row.reward === 0;
  if (row.scores?.standalone !== 1 || !keys.every(key =>
    Number.isFinite(row.scores[key]) && row.scores[key] >= 0 && row.scores[key] <= 1
  )) return false;
  const expected = row.diagnostics?.veganFailed ? 0 : keys.reduce((sum, key) => sum + row.scores[key] * criterionWeights[key], 0);
  return Math.abs(expected - row.reward) < 1e-10;
}

export function validWeights(value, keys = criteria) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).length === keys.length
    && keys.every(key => Number.isFinite(value[key]) && value[key] >= 0 && value[key] <= 1)
    && Math.abs(keys.reduce((sum, key) => sum + value[key], 0) - 1) < 1e-10;
}

function sampleSpread(values) {
  if (values.length < 2) return null;
  const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
  return Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (values.length - 1));
}

export function reweightScore(row, criterionWeights, keys = criteria) {
  if (!validWeights(criterionWeights, keys)) throw new Error("Invalid display weights");
  const weighted = scores => keys.reduce((sum, key) => sum + criterionWeights[key] * scores[key], 0);
  const judgeResults = (row.judgeResults || []).map(judge => ({
    ...judge,
    sourceReward: judge.reward,
    reward: weighted(judge.scores),
    sampleStdDev: sampleSpread(judge.judgments.map(sample => weighted(Object.fromEntries(keys.map(key => [key, sample[key].score]))))),
  }));
  return {
    ...row,
    sourceReward: row.reward,
    reward: row.scores.standalone === 0 || row.scores.procedural === 0 || row.diagnostics?.veganFailed ? 0 : weighted(row.scores),
    rewardStdDev: sampleSpread(judgeResults.map(judge => judge.reward)),
    judgeResults,
  };
}

export function withHumanScore(row, humanScore, humanWeight) {
  if (!Number.isFinite(humanWeight) || humanWeight < 0 || humanWeight > 1
      || (humanScore !== null && (!Number.isFinite(humanScore) || humanScore < 0 || humanScore > 1))) {
    throw new Error("Invalid human rating or weight");
  }
  const weight = humanScore === null ? 0 : humanWeight;
  return {
    ...row,
    llmReward: row.reward,
    humanScore,
    humanWeight: weight,
    reward: row.scores.standalone === 0 || row.scores.procedural === 0 || row.diagnostics?.veganFailed
      ? 0 : (weight ? weight * humanScore + (1 - weight) * row.reward : row.reward),
  };
}
