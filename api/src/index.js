const RUN_ID_PATTERN = /^[a-z0-9][a-z0-9._-]*\/[a-z0-9][a-z0-9._-]*\/[a-z0-9][a-z0-9._-]*$/;

export default {
  async fetch(request, env) {
    const cors = corsHeaders(request, env);
    if (!cors) return json({ error: "Origin not allowed" }, 403);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    const url = new URL(request.url);
    try {
      if (url.pathname === "/health" && request.method === "GET") {
        return json({ ok: true }, 200, cors);
      }
      if (url.pathname === "/votes" && request.method === "GET") {
        return getVotes(env, cors);
      }
      if (url.pathname === "/vote" && request.method === "POST") {
        return postVote(request, env, cors);
      }
      return json({ error: "Not found" }, 404, cors);
    } catch (error) {
      console.error(error);
      return json({ error: "Internal server error" }, 500, cors);
    }
  }
};

async function getVotes(env, cors) {
  const result = await env.DB.prepare(
    `SELECT
       run_id AS runId,
       COUNT(*) AS count,
       ROUND(AVG(rating), 2) AS average
     FROM votes
     GROUP BY run_id
     ORDER BY average DESC, count DESC, run_id ASC`
  ).all();

  return json(result.results || [], 200, {
    ...cors,
    "Cache-Control": "public, max-age=15, stale-while-revalidate=45"
  });
}

async function postVote(request, env, cors) {
  const contentLength = Number(request.headers.get("Content-Length") || 0);
  if (contentLength > 4096) return json({ error: "Request too large" }, 413, cors);

  let body;
  try {
    body = await request.json();
  } catch (_error) {
    return json({ error: "Invalid JSON" }, 400, cors);
  }

  const { runId, rating } = body || {};
  if (typeof runId !== "string" || !RUN_ID_PATTERN.test(runId) || runId.length > 200) {
    return json({ error: "Invalid runId" }, 400, cors);
  }
  if (!Number.isInteger(rating) || rating < 1 || rating > 5) {
    return json({ error: "Rating must be an integer from 1 to 5" }, 400, cors);
  }
  if (!env.VOTER_HASH_SECRET) {
    throw new Error("VOTER_HASH_SECRET is not configured");
  }

  const voterHash = await fingerprint(request, env.VOTER_HASH_SECRET);
  await env.DB.prepare(
    `INSERT INTO votes (run_id, voter_hash, rating)
     VALUES (?, ?, ?)
     ON CONFLICT (run_id, voter_hash)
     DO UPDATE SET rating = excluded.rating, updated_at = datetime('now')`
  ).bind(runId, voterHash, rating).run();

  return json({ ok: true }, 200, {
    ...cors,
    "Cache-Control": "no-store"
  });
}

function corsHeaders(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return baseCorsHeaders();

  const allowed = new Set(
    String(env.ALLOWED_ORIGINS || "")
      .split(",")
      .map((value) => value.trim())
      .filter(Boolean)
  );
  if (!allowed.has("*") && !allowed.has(origin)) return null;

  return {
    ...baseCorsHeaders(),
    "Access-Control-Allow-Origin": allowed.has("*") ? "*" : origin,
    Vary: "Origin"
  };
}

function baseCorsHeaders() {
  return {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400"
  };
}

async function fingerprint(request, secret) {
  const ip = request.headers.get("CF-Connecting-IP")
    || request.headers.get("X-Forwarded-For")?.split(",")[0]?.trim()
    || "local";
  const userAgent = (request.headers.get("User-Agent") || "unknown").slice(0, 512);
  const bytes = new TextEncoder().encode(`${secret}\n${ip}\n${userAgent}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function json(value, status = 200, headers = {}) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      ...headers,
      "Content-Type": "application/json; charset=utf-8",
      "X-Content-Type-Options": "nosniff"
    }
  });
}
