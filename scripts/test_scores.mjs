import assert from "node:assert/strict";
import { test } from "node:test";
import { contrastingModelColors, descendingScale, rewardExtent, axisValue, byEffort, generationTimestamp, newestGeneratedFirst, modelLabelAnchor, validScore, validWeights, reweightScore, withHumanScore, criteria } from "../scores-data.mjs";

test("generation order uses recorded completion, with unknown dates last", () => {
  const row = (id, timing) => ({ run: { id, timing } });
  const rows = [row("unknown", {}), row("older", { completedAt: "2026-09-20T12:00:00Z" }), row("new", { completedAt: "2026-09-30T12:00:00Z" }), row("start-only", { startedAt: "2026-09-25T12:00:00Z" })];
  assert.deepEqual(rows.sort(newestGeneratedFirst).map(row => row.run.id), ["new", "start-only", "older", "unknown"]);
  assert.equal(generationTimestamp({ timing: { completedAt: "invalid" } }), -Infinity);
});

test("model labels anchor at the highest part of each line", () => {
  assert.deepEqual(modelLabelAnchor([{ x: 10, y: 200 }, { x: 500, y: 80 }, { x: 100, y: 160 }]), { x: 500, y: 80 });
  assert.deepEqual(modelLabelAnchor([{ x: 10, y: 100 }]), { x: 10, y: 100 });
});

test("model colors stay distinct, saturated, and readable on the chart background", () => {
  const keys = Array.from({ length: 24 }, (_, i) => `vendor${i % 4}/model${i}`);
  const colors = contrastingModelColors(keys);
  assert.equal(new Set(colors.values()).size, keys.length);
  const luminance = hex => {
    const linear = hex.match(/[0-9a-f]{2}/g).map(v => parseInt(v, 16) / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4);
    return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2];
  };
  for (const color of colors.values()) {
    assert.ok((luminance(color) + .05) / (luminance("#1b1511") + .05) >= 4.5);
    const rgb = color.match(/[0-9a-f]{2}/g).map(v => parseInt(v, 16));
    assert.ok(Math.max(...rgb) - Math.min(...rgb) > 150);
  }
  assert.deepEqual([...contrastingModelColors(keys.slice(0, 18)).values()], [...colors.values()].slice(0, 18));
});

test("lower costs lie to the right and tick values descend in both scales", () => {
  for (const log of [false, true]) {
    const scale = descendingScale([.01, 1, 100], log);
    assert.equal(scale.position(100), 0);
    assert.ok(scale.position(.01) > scale.position(1));
    assert.ok(scale.position(1) > scale.position(100));
    assert.ok(scale.valueAt(0) > scale.valueAt(.5));
    assert.ok(scale.valueAt(.5) > scale.valueAt(1));
    for (const fraction of [0, .2, .5, 1]) {
      assert.ok(Math.abs(scale.position(scale.valueAt(fraction)) - fraction) < 1e-10);
    }
  }
  assert.equal(descendingScale([0, 10]).position(0), 1);
  assert.ok(Number.isFinite(descendingScale([0]).position(0)));
  assert.ok(Number.isFinite(descendingScale([2], true).position(2)));
});

test("unknown axes stay unknown; only USD costs are plotted", () => {
  assert.equal(axisValue({ usage: { cost: null } }, "cost"), null);
  assert.equal(axisValue({ usage: { cost: { currency: "EUR", total: 1 } } }, "cost"), null);
  assert.equal(axisValue({ usage: { cost: { currency: "USD", total: 0 } } }, "cost"), 0);
  assert.equal(axisValue({ timing: { wallDurationMs: 120000 } }, "time"), 2);
});

test("lines follow effort order even when cost order differs", () => {
  const row = effort => ({ run: { variation: { reasoningEffort: effort } } });
  const rows = [row("ultra"), row("high"), row("low"), row("max"), row("medium"), row("xhigh")];
  assert.deepEqual(rows.sort(byEffort).map(r => r.run.variation.reasoningEffort), ["low", "medium", "high", "xhigh", "max", "ultra"]);
});

test("zero gate is distinct from missing or invalid visual scores", () => {
  assert.equal(validScore({ reward: 0, scores: { standalone: 0 } }), true);
  assert.equal(validScore({ reward: 0, scores: { standalone: 1 } }), false);
  const scores = Object.fromEntries(criteria.map(key => [key, .7]));
  assert.equal(validScore({ reward: .7, scores: { standalone: 1, ...scores } }), true);
  assert.equal(validScore({ reward: .9, scores: { standalone: 1, ...scores } }), false);
});

test("reward axis starts at the lowest plotted reward, including real zeroes", () => {
  assert.deepEqual(rewardExtent([.63, .85]), { min: .63, max: 1 });
  assert.deepEqual(rewardExtent([0, .85]), { min: 0, max: 1 });
  assert.ok(rewardExtent([1]).min < rewardExtent([1]).max);
});

test("display weighting preserves original judgments and recalculates sample disagreement", () => {
  const newWeights = { noodles: .1, toppings: .1, bowl: .2, broth: .1, steam: .25, composition: .25 };
  assert.equal(validWeights(newWeights), true);
  assert.equal(validWeights({ ...newWeights, steam: .5 }), false);
  const sample = score => Object.fromEntries(criteria.map(key => [key, { score: ["steam", "composition"].includes(key) ? score : 0, reason: "Recorded fixture evidence" }]));
  const scores = score => ({ standalone: 1, procedural: 1, ...Object.fromEntries(criteria.map(key => [key, ["steam", "composition"].includes(key) ? score : 0])) });
  const row = { reward: .1625, scores: scores(.65), judgeResults: [
    { reward: .125, scores: scores(.5), judgments: [sample(1), sample(0)] },
    { reward: .2, scores: scores(.8), judgments: [sample(.8), sample(.8)] },
  ] };
  const original = JSON.stringify(row);
  const adjusted = reweightScore(row, newWeights);
  assert.ok(Math.abs(adjusted.reward - .325) < 1e-10);
  assert.equal(adjusted.sourceReward, row.reward);
  assert.ok(Math.abs(adjusted.judgeResults[0].sampleStdDev - Math.sqrt(.125)) < 1e-10);
  assert.ok(Math.abs(adjusted.rewardStdDev - Math.sqrt(.01125)) < 1e-10);
  assert.equal(JSON.stringify(row), original);
  assert.equal(validScore(adjusted, criteria, newWeights), true);
});

test("missing human ratings use the LLM score; zero is a real rating and gates remain binding", () => {
  const row = { reward: .75, scores: { standalone: 1, procedural: 1 } };
  assert.equal(withHumanScore(row, null, .8).reward, .75);
  assert.ok(Math.abs(withHumanScore(row, 0, .8).reward - .15) < 1e-10);
  assert.ok(Math.abs(withHumanScore(row, 1, .8).reward - .95) < 1e-10);
  assert.equal(withHumanScore({ ...row, scores: { standalone: 1, procedural: 0 } }, 1, .8).reward, 0);
  assert.equal(withHumanScore({ ...row, diagnostics: { veganFailed: true } }, 1, .8).reward, 0);
  assert.throws(() => withHumanScore(row, 85, .8));
  assert.throws(() => withHumanScore(row, true, .8));
});
