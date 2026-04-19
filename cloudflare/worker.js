const ALLOWED_ORIGINS = ["https://trader.akshaydhenge.uk"];
const ALLOWED_TYPES = ["watchlist_add", "watchlist_remove", "watchlist_edit", "aiscan", "deep_scan"];

function corsHeaders(origin) {
  const allowed = ALLOWED_ORIGINS.includes(origin) ? origin : ALLOWED_ORIGINS[0];
  return {
    "Access-Control-Allow-Origin": allowed,
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
    "Access-Control-Max-Age": "86400",
  };
}

function json(body, status = 200, origin = "") {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders(origin) },
  });
}

function validateCommand(cmd) {
  if (!ALLOWED_TYPES.includes(cmd.type)) {
    return `Unknown command type: ${cmd.type}`;
  }
  if (cmd.type !== "deep_scan") {
    if (!cmd.symbol || typeof cmd.symbol !== "string") return "Missing or invalid symbol";
    cmd.symbol = cmd.symbol.toUpperCase().trim();
  }
  if (cmd.type === "watchlist_add" || cmd.type === "watchlist_edit") {
    const low = parseFloat(cmd.low);
    const high = parseFloat(cmd.high);
    if (isNaN(low) || low <= 0) return "Missing or invalid low target price";
    if (isNaN(high) || high <= 0) return "Missing or invalid high target price";
    cmd.low = low;
    cmd.high = high;
    cmd.notes = typeof cmd.notes === "string" ? cmd.notes : "";
  }
  return null;
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const url = new URL(request.url);

    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(origin) });
    }

    // POST /api/cmd — submit a command
    if (request.method === "POST" && url.pathname === "/api/cmd") {
      const apiKey = request.headers.get("X-API-Key") || "";
      if (apiKey !== env.API_KEY) {
        return json({ error: "Unauthorized" }, 401, origin);
      }

      let cmd;
      try {
        cmd = await request.json();
      } catch {
        return json({ error: "Invalid JSON body" }, 400, origin);
      }

      const validationError = validateCommand(cmd);
      if (validationError) {
        return json({ error: validationError }, 400, origin);
      }

      const cmdId = crypto.randomUUID();
      const payload = JSON.stringify({ ...cmd, queued_at: new Date().toISOString() });

      try {
        await env.TRADER_DATA.put(`cmds/pending/${cmdId}.json`, payload, {
          httpMetadata: { contentType: "application/json" },
        });
      } catch (err) {
        return json({ error: `Failed to queue command: ${err.message}` }, 500, origin);
      }

      return json({ cmd_id: cmdId }, 202, origin);
    }

    // GET /api/cmd/:uuid — poll for result
    if (request.method === "GET" && url.pathname.startsWith("/api/cmd/")) {
      const apiKey = request.headers.get("X-API-Key") || "";
      if (apiKey !== env.API_KEY) {
        return json({ error: "Unauthorized" }, 401, origin);
      }

      const cmdId = url.pathname.replace("/api/cmd/", "").replace(/\.json$/, "");
      if (!cmdId || cmdId.includes("/")) {
        return json({ error: "Invalid cmd_id" }, 400, origin);
      }

      let obj;
      try {
        obj = await env.TRADER_DATA.get(`cmds/done/${cmdId}.json`);
      } catch (err) {
        return json({ error: `R2 error: ${err.message}` }, 500, origin);
      }

      if (!obj) {
        return json({ status: "pending" }, 202, origin);
      }

      const result = JSON.parse(await obj.text());
      return json(result, 200, origin);
    }

    return json({ error: "Not found" }, 404, origin);
  },
};
