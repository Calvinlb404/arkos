/* =========================================================
   data layer — config, auth, and the live backend api.
   (formerly mock seed; now the new design talks to the real
    ark backend: /auth, /tasks, /computer, /services, chat.)
   ========================================================= */

/* ---- backend resolution ----
   The frontend is served by the backend at /app/, so the backend URL is
   just the origin we were loaded from. An explicit override from the login
   card wins; file:// dev falls back to localhost:1113. */
function resolveBackend() {
  const override = localStorage.getItem("ark_backend");
  if (override) return override;
  if (location.protocol.startsWith("http") && location.host) return location.origin;
  return "http://localhost:1113";
}

const CONFIG = {
  backend: resolveBackend(),
  token: localStorage.getItem("ark_token") || "",
  userId: localStorage.getItem("ark_user_id") || "",
  username: localStorage.getItem("ark_username") || "",
  model: "ark-agent",
};

function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (CONFIG.token) h["Authorization"] = "Bearer " + CONFIG.token;
  return h;
}

function backendHost() {
  return CONFIG.backend.replace(/^https?:\/\//, "");
}

/* ---- relative time ---- */
function relTime(iso) {
  if (!iso) return "";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "";
  const s = Math.max(1, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m";
  if (s < 86400) return Math.floor(s / 3600) + "h";
  return Math.floor(s / 86400) + "d";
}

/* pull an ```ark-plan {json}``` block out of a buddy reply.
   returns { text: reply with the fenced block removed, plan: obj|null } */
function parsePlan(s) {
  const str = String(s || "");
  const m = str.match(/```ark-plan\s*\n([\s\S]*?)\n```/);
  if (!m) return { text: str, plan: null };
  let plan = null;
  try { plan = JSON.parse(m[1]); } catch { return { text: str, plan: null }; }
  // drop malformed blocks (matches the original app.js guard); the prose above still shows
  if (!plan || !Array.isArray(plan.plan_steps)) return { text: str.replace(m[0], "").trim(), plan: null };
  return { text: str.replace(m[0], "").trim(), plan };
}

/* kind -> short glyph for the activity / event stream */
const KIND_ICON = {
  shell: "$", file: "▤", search: "◉", plan: "≡",
  ask: "?", mcp: "⊕", completed: "✓", failed: "✗", start: "▶",
};

/* watching has no backend table yet — keep it local so the zone isn't empty.
   (mirrors the placeholder the previous frontend used.) */
const WATCHING = [
  { id: "w1", src: "linear.app / team ark", note: "new issues + status changes", cadence: "every 5m", live: true },
  { id: "w2", src: "mail.google.com / inbox", note: "anything that needs a reply", cadence: "live", live: true },
  { id: "w3", src: "calendar.google.com", note: "conflicts + prep needed", cadence: "every 15m", live: true },
  { id: "w4", src: "github.com / arkos", note: "review requests on your PRs", cadence: "every 10m", live: false },
];

/* empty shell the UI starts from before the first poll lands */
function emptyData() {
  return {
    user: CONFIG.username || "",
    backend: backendHost(),
    online: null,            // null unknown, true/false from /health
    approvals: [],
    tasks: [],
    watching: WATCHING,
    computerTasks: [],
    chat: [],
  };
}

/* =========================================================
   api — thin wrappers over the ark backend. Each returns
   parsed JSON (or a safe default); callers handle shaping.
   Endpoints + payloads mirror the contract the backend
   exposes (/auth, /tasks, /computer, /services, chat).
   ========================================================= */
const api = {
  async health() {
    try {
      const r = await fetch(CONFIG.backend + "/health", { method: "GET" });
      if (r.status !== 200 && r.status !== 503) return false;
      const j = await r.json();
      return j.status === "ok";
    } catch { return false; }
  },

  async me() {
    if (!CONFIG.token) return null;
    try {
      const r = await fetch(CONFIG.backend + "/auth/me", { headers: authHeaders() });
      if (!r.ok) return null;
      return await r.json();   // { user_id, username }
    } catch { return null; }
  },

  async login(username, backendOverride) {
    if (backendOverride) {
      const b = backendOverride.trim().replace(/\/$/, "");
      const url = /^https?:\/\//.test(b) ? b : "https://" + b;
      CONFIG.backend = url;
      localStorage.setItem("ark_backend", url);
    }
    const r = await fetch(CONFIG.backend + "/auth/demo-login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username }),
    });
    if (!r.ok) throw new Error(r.status + ": " + (await r.text()));
    const j = await r.json();   // { token, user_id, username }
    CONFIG.token = j.token;
    CONFIG.userId = j.user_id;
    CONFIG.username = j.username;
    localStorage.setItem("ark_token", j.token);
    localStorage.setItem("ark_user_id", j.user_id);
    localStorage.setItem("ark_username", j.username);
    return j;
  },

  signOut() {
    CONFIG.token = ""; CONFIG.userId = ""; CONFIG.username = "";
    localStorage.removeItem("ark_token");
    localStorage.removeItem("ark_user_id");
    localStorage.removeItem("ark_username");
  },

  // returns { tasks: [...] } for a status filter, or { unauthorized: true }
  async tasks(status) {
    try {
      const r = await fetch(`${CONFIG.backend}/tasks?status=${encodeURIComponent(status)}`, { headers: authHeaders() });
      if (r.status === 401) return { unauthorized: true, tasks: [] };
      if (!r.ok) return { tasks: [] };
      return await r.json();
    } catch { return { tasks: [] }; }
  },

  async taskEvents(id) {
    try {
      const r = await fetch(`${CONFIG.backend}/tasks/${encodeURIComponent(id)}/events`, { headers: authHeaders() });
      if (!r.ok) return [];
      const j = await r.json();
      return j.events || [];
    } catch { return []; }
  },

  async pendingApprovals() {
    try {
      const r = await fetch(`${CONFIG.backend}/tasks/approvals/pending`, { headers: authHeaders() });
      if (!r.ok) return [];
      const j = await r.json();
      return j.approvals || [];
    } catch { return []; }
  },

  async respondApproval(approvalId, body) {
    try {
      await fetch(`${CONFIG.backend}/tasks/approvals/${encodeURIComponent(approvalId)}/respond`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      });
    } catch (e) { console.warn("respondApproval", e); }
  },

  async cancelTask(id) {
    try {
      await fetch(`${CONFIG.backend}/tasks/${encodeURIComponent(id)}/cancel`, {
        method: "POST", headers: authHeaders(),
      });
    } catch (e) { console.warn("cancelTask", e); }
  },

  /* dispatch an approved plan. target=computer -> the sandbox agent;
     otherwise -> the executor subagent via /tasks. nothing runs until here. */
  async dispatchComputer(prompt) {
    const r = await fetch(`${CONFIG.backend}/computer/tasks`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json", "X-User-ID": CONFIG.userId }),
      body: JSON.stringify({ prompt }),
    });
    if (!r.ok) throw new Error("dispatch failed: " + r.status);
    return r.json();
  },
  async createTask(plan) {
    const r = await fetch(`${CONFIG.backend}/tasks`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json", "X-User-ID": CONFIG.userId }),
      body: JSON.stringify({
        title: plan.title,
        plan_steps: plan.plan_steps || [],
        required_tools: plan.required_tools || [],
        context_payload: { source: "chat", title: plan.title },
      }),
    });
    if (!r.ok) throw new Error("create task failed: " + r.status);
    return r.json();
  },
  async computerTasks() {
    if (!CONFIG.token) return [];
    try {
      const r = await fetch(`${CONFIG.backend}/computer/tasks`, { headers: authHeaders() });
      if (!r.ok) return [];
      const j = await r.json();
      return j.tasks || [];
    } catch { return []; }
  },

  async computerFiles(path) {
    const r = await fetch(`${CONFIG.backend}/computer/files?path=${encodeURIComponent(path)}`, { headers: authHeaders() });
    if (!r.ok) throw new Error("files " + r.status);
    const j = await r.json();
    return j.entries || [];
  },

  async computerFile(path) {
    const r = await fetch(`${CONFIG.backend}/computer/file?path=${encodeURIComponent(path)}`, { headers: authHeaders() });
    if (!r.ok) throw new Error("file " + r.status);
    const j = await r.json();   // { content, truncated, size }
    return j;
  },

  async services() {
    try {
      const r = await fetch(CONFIG.backend + "/services", {
        headers: authHeaders({ "X-User-ID": CONFIG.userId || "" }),
      });
      if (!r.ok) throw new Error("bad status " + r.status);
      return await r.json();   // { shared: [], per_user: [] }
    } catch (e) { return { error: String(e), shared: [], per_user: [] }; }
  },

  async connectService(service) {
    const r = await fetch(`${CONFIG.backend}/services/${encodeURIComponent(service)}/connect`, {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json", "X-User-ID": CONFIG.userId || "" }),
    });
    return await r.json();   // { status } | { setup_url } | { error }
  },

  async disconnectService(service) {
    try {
      await fetch(`${CONFIG.backend}/services/${encodeURIComponent(service)}/disconnect`, {
        method: "POST",
        headers: authHeaders({ "Content-Type": "application/json", "X-User-ID": CONFIG.userId || "" }),
      });
    } catch (e) { console.warn("disconnectService", e); }
  },

  /* streaming chat. `messages` is the OpenAI-style history; onDelta(textChunk)
     fires for each token. Resolves with the full reply text. */
  async chatStream(messages, onDelta) {
    const res = await fetch(CONFIG.backend + "/v1/chat/completions", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json", "X-User-ID": CONFIG.userId }),
      body: JSON.stringify({ model: CONFIG.model, stream: true, user: CONFIG.userId, messages }),
    });
    if (!res.ok || !res.body) throw new Error("stream failed: " + res.status);
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "", reply = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const raw of lines) {
        const line = raw.trim();
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") continue;
        try {
          const j = JSON.parse(payload);
          const delta = j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content;
          if (delta) { reply += delta; onDelta && onDelta(reply); }
        } catch {}
      }
    }
    return reply;
  },
};

Object.assign(window, { CONFIG, authHeaders, backendHost, relTime, parsePlan, KIND_ICON, WATCHING, emptyData, api });
