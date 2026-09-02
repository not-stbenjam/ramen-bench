import assert from "node:assert/strict";
import test from "node:test";

import worker from "../src/index.js";

const allowedOrigin = "https://not-stbenjam.github.io";

function environment() {
  const votes = new Map();
  const DB = {
    prepare(sql) {
      return {
        bind(runId, voterHash, rating) {
          return {
            async run() {
              votes.set(`${runId}:${voterHash}`, { runId, rating });
              return { success: true };
            }
          };
        },
        async all() {
          const grouped = new Map();
          for (const vote of votes.values()) {
            if (!grouped.has(vote.runId)) grouped.set(vote.runId, []);
            grouped.get(vote.runId).push(vote.rating);
          }
          return {
            results: [...grouped].map(([runId, ratings]) => ({
              runId,
              count: ratings.length,
              average: ratings.reduce((sum, rating) => sum + rating, 0) / ratings.length
            }))
          };
        }
      };
    }
  };
  return {
    DB,
    ALLOWED_ORIGINS: allowedOrigin,
    VOTER_HASH_SECRET: "test-secret"
  };
}

function request(path, options = {}) {
  return new Request(`https://ratings.example${path}`, {
    ...options,
    headers: {
      Origin: allowedOrigin,
      "CF-Connecting-IP": "192.0.2.1",
      "User-Agent": "test-browser",
      ...options.headers
    }
  });
}

test("stores and aggregates a vote", async () => {
  const env = environment();
  const voteResponse = await worker.fetch(request("/vote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId: "openai/gpt-5.6-luna/max", rating: 5 })
  }), env);
  assert.equal(voteResponse.status, 200);

  const response = await worker.fetch(request("/votes"), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), [
    { runId: "openai/gpt-5.6-luna/max", count: 1, average: 5 }
  ]);
});

test("updates the same visitor's vote", async () => {
  const env = environment();
  for (const rating of [2, 4]) {
    await worker.fetch(request("/vote", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runId: "openai/gpt-5.6-luna/max", rating })
    }), env);
  }
  const response = await worker.fetch(request("/votes"), env);
  assert.deepEqual(await response.json(), [
    { runId: "openai/gpt-5.6-luna/max", count: 1, average: 4 }
  ]);
});

test("rejects invalid votes and unknown origins", async () => {
  const env = environment();
  const invalid = await worker.fetch(request("/vote", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId: "not-a-run", rating: 7 })
  }), env);
  assert.equal(invalid.status, 400);

  const forbidden = await worker.fetch(new Request("https://ratings.example/votes", {
    headers: { Origin: "https://example.com" }
  }), env);
  assert.equal(forbidden.status, 403);
});
