/* =========================================================
   app — root: live backend state, routing, theme, rail,
   command bar. Polls the ark backend for tasks/approvals/
   computer activity; chat streams over /v1/chat/completions.
   ========================================================= */

const NAV = ["desk", "tasks", "watching", "approvals", "computer", "chat"];

/* ---- shapers: backend payloads -> the shapes the views expect ---- */
/* One shaper for every task (executor or computer). agent_kind='computer'
   tasks are just rows in `tasks` now (migration 0007), so there is no separate
   computer shaper. Header prefers the agent's summary, then the title. */
function shapeTask(t, events) {
  const waiting = t.status === "awaiting_approval";
  const ctx = t.context_payload || {};
  return {
    id: t.task_id,
    kind: t.agent_kind || "executor",
    state: t.status === "completed" ? "done" : (t.status === "failed" ? "stop" : "run"),
    when: waiting ? "waiting on you" : relTime(t.updated_at),
    text: ctx.summary || t.title || "task",
    src: waiting ? "awaiting approval" : t.status,
    events: (events || []).map((e) => ({ k: e.kind, t: (e.content || "").slice(0, 240) })),
  };
}

function shapeApproval(a) {
  return {
    id: a.approval_id,
    src: a.task_title || "task",
    when: "",
    tag: a.kind === "binary" ? "approve / decline" : "answer",
    kind: a.kind,
    title: a.task_title || "approval",
    body: a.prompt || "",
    plan: [],
    tools: [],
  };
}

function App() {
  const [theme, setTheme] = useState(() => localStorage.getItem("ark-theme") || "light");
  const [authed, setAuthed] = useState(false);
  const [loginGone, setLoginGone] = useState(false);
  const [booting, setBooting] = useState(true);
  const [view, setView] = useState("desk");
  const [settings, setSettings] = useState(false);
  const [data, setData] = useState(emptyData);
  const [floaters, setFloaters] = useState([]);
  // Completed/failed task IDs dismissed by the user this session.
  // Dismissed tasks are hidden from the desk's pending-approvals zone.
  const [dismissed, setDismissed] = useState(() => new Set());
  const inputRef = useRef(null);
  const pollRef = useRef(null);

  /* theme */
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("ark-theme", theme);
  }, [theme]);

  /* keyboard: / focus, esc clear/close */
  useEffect(() => {
    function key(e) {
      if (e.key === "/" && document.activeElement !== inputRef.current) {
        e.preventDefault(); inputRef.current && inputRef.current.focus();
      }
      if (e.key === "Escape") {
        if (settings) setSettings(false);
        else if (inputRef.current) { inputRef.current.value = ""; inputRef.current.blur(); }
      }
    }
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [settings]);

  /* ---- auth bootstrap: validate an existing token, else show login ---- */
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const online = await api.health();
      if (!cancelled) setData((d) => ({ ...d, online }));
      const me = await api.me();
      if (cancelled) return;
      if (me) {
        CONFIG.userId = me.user_id; CONFIG.username = me.username;
        localStorage.setItem("ark_user_id", me.user_id);
        localStorage.setItem("ark_username", me.username);
        setData((d) => ({ ...d, user: me.username }));
        setAuthed(true);
        setLoginGone(true);
        startPolling();
      } else {
        api.signOut();
      }
      setBooting(false);
    })();
    return () => { cancelled = true; stopPolling(); };
  }, []);

  /* ---- live polling ---- */
  function startPolling() {
    refreshAll();
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(refreshAll, 6000);
  }
  function stopPolling() {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  }

  async function refreshAll() {
    const online = await api.health();

    const [running, waiting, done, failed, approvalsRaw] = await Promise.all([
      api.tasks("running"),
      api.tasks("awaiting_approval"),
      api.tasks("completed"),   // server returns only last 15 min (ISSUES.md Task 1)
      api.tasks("failed"),      // server returns only last 15 min
      api.pendingApprovals(),
    ]);

    if (running.unauthorized || waiting.unauthorized) {
      // token went stale — drop back to login
      api.signOut();
      stopPolling();
      setAuthed(false);
      setLoginGone(false);
      return;
    }

    // One task source: computer tasks are agent_kind='computer' rows in `tasks`
    // (migration 0007), so they arrive here alongside executor tasks.
    // Terminal tasks (completed/failed) come from the 15-min server window so
    // they stay visible after finishing instead of vanishing on the next poll.
    const rawTasks = [
      ...(running.tasks || []),
      ...(waiting.tasks || []),
      ...(done.tasks || []),
      ...(failed.tasks || []),
    ];
    // pull events for each task so the expandable log is real
    const eventsList = await Promise.all(rawTasks.map((t) => api.taskEvents(t.task_id)));

    const tasks = rawTasks.map((t, i) => shapeTask(t, eventsList[i]));
    const approvals = approvalsRaw.map(shapeApproval);
    // Computer tab badge derives from the same list (raw rows keep .status/.agent_kind).
    const computerTasks = rawTasks.filter((t) => t.agent_kind === "computer");

    setData((d) => ({ ...d, online, tasks, approvals, computerTasks }));
  }

  /* ---- login / sign out ---- */
  async function enter(name, backendOverride, onError) {
    try {
      await api.login(name, backendOverride);
      const online = await api.health();
      setData((d) => ({ ...d, user: CONFIG.username, backend: backendHost(), online }));
      setAuthed(true);
      setTimeout(() => setLoginGone(true), 30);
      startPolling();
    } catch (e) {
      onError && onError("login failed. " + (e.message || e));
    }
  }

  function signOut() {
    stopPolling();
    api.signOut();
    setSettings(false);
    setLoginGone(false);
    setData(emptyData());
    setTimeout(() => setAuthed(false), 450);
  }

  /* ---- approvals ---- */
  async function resolveApproval(id, verb, note) {
    const item = data.approvals.find((a) => a.id === id);
    let body;
    if (item && item.kind === "binary") body = { approved: verb === "approved" };
    else body = { answer: note || (verb === "approved" ? "yes" : "no") };
    try {
      // Remove only AFTER server confirms — respondApproval throws on non-2xx so a
      // failed call leaves the approval visible and resolvable (no zombie approvals).
      await api.respondApproval(id, body);
      setData((d) => ({ ...d, approvals: d.approvals.filter((a) => a.id !== id) }));
      refreshAll();
    } catch (e) {
      console.error("resolveApproval failed:", e);
      // Approval stays in the list; user can try again.
    }
  }

  /* ---- chat (streaming) ---- */
  async function send(text) {
    const id = "f" + Date.now();
    setFloaters((f) => [...f, { id, who: data.user || "you", text }]);
    setData((d) => ({ ...d, chat: [...d.chat, { who: "you", text }] }));

    // assistant floater that fills in live
    const rId = id + "r";
    setFloaters((f) => [...f, { id: rId, who: "buddy", text: "…" }]);

    const history = [...data.chat, { who: "you", text }].map((m) => ({
      role: m.who === "you" ? "user" : "assistant",
      content: m.text,
    }));

    let reply = "";
    try {
      reply = await api.chatStream(
        history,
        (full) => {
          // strip the ark-plan fence live so the floater never shows raw json
          setFloaters((f) => f.map((x) => x.id === rId ? { ...x, text: parsePlan(full).text } : x));
        },
        (status) => {
          // buddy's live activity (thinking / drafting a plan) shown until text arrives
          setFloaters((f) => f.map((x) => x.id === rId ? { ...x, status } : x));
        },
      );
    } catch (err) {
      reply = "[buddy offline: " + (err.message || err) + "]";
      setFloaters((f) => f.map((x) => x.id === rId ? { ...x, text: reply } : x));
    }

    // split any workshopped plan out of the reply into an approve card
    const { text: clean, plan, parseError } = parsePlan(reply);
    setData((d) => ({
      ...d,
      chat: [...d.chat, { who: "buddy", text: clean || reply, plan, parseError }],
    }));
    // fold both away, then refresh state (a reply may have spawned tasks)
    setTimeout(() => {
      setFloaters((f) => f.map((x) => (x.id === id || x.id === rId) ? { ...x, fold: true } : x));
      setTimeout(() => setFloaters((f) => f.filter((x) => x.id !== id && x.id !== rId)), 460);
      refreshAll();
    }, 1600);
  }

  function onKey(e) {
    if (e.key === "Enter" && e.target.value.trim()) {
      send(e.target.value.trim());
      e.target.value = "";
    }
  }

  /* ---- plan approval: nothing runs on the computer until this fires ---- */
  async function approvePlan(plan, msgIndex) {
    try {
      if (plan.target === "computer") await api.dispatchComputer(plan.prompt || plan.title);
      else await api.createTask(plan);
      // mark the card resolved so it can't be double-submitted
      setData((d) => ({
        ...d,
        chat: d.chat.map((m, i) => i === msgIndex ? { ...m, planResolved: "approved" } : m),
      }));
      refreshAll();
    } catch (e) {
      setData((d) => ({
        ...d,
        chat: d.chat.map((m, i) => i === msgIndex ? { ...m, planError: e.message || String(e) } : m),
      }));
    }
  }
  function declinePlan(msgIndex) {
    setData((d) => ({
      ...d,
      chat: d.chat.map((m, i) => i === msgIndex ? { ...m, planResolved: "declined" } : m),
    }));
  }

  function dismissTask(id) {
    setDismissed((s) => new Set([...s, id]));
  }

  const views = {
    desk: <DeskView data={data} onResolve={resolveApproval} dismissed={dismissed} onDismiss={dismissTask} />,
    tasks: <TasksView data={data} />,
    watching: <WatchingView data={data} />,
    approvals: <ApprovalsView data={data} onResolve={resolveApproval} />,
    computer: <ComputerView data={data} />,
    chat: <ChatView data={data} onApprovePlan={approvePlan} onDeclinePlan={declinePlan} />,
  };

  const completedUndismissed = data.tasks.filter((t) => (t.state === "done" || t.state === "stop") && !dismissed.has(t.id));
  const pending = data.approvals.length + completedUndismissed.length;

  return (
    <React.Fragment>
      <div className="app">
        {/* rail */}
        <div className="rail">
          <div className="mark">
            <span className="glyph">a</span>
            <span className={"pip" + (data.online === false ? " off" : data.online ? " live" : "")} title={"backend " + (data.online === false ? "unreachable" : data.online ? "ok" : "…")} />
          </div>
          <nav>
            {NAV.map((v) => (
              <a key={v} className={(view === v ? "active" : "") + (v === "approvals" && pending > 0 ? " alert" : "")} onClick={() => setView(v)}>
                {v}
              </a>
            ))}
          </nav>
          <div className="foot">
            <button className="theme-btn" onClick={() => setTheme((t) => t === "light" ? "dark" : "light")}>
              {theme === "light" ? "dark" : "light"}
            </button>
          </div>
        </div>

        {/* topbar */}
        <div className="topbar">
          <div className="crumbs">
            <span>buddy <b>v1</b></span>
            <span className="sep">/</span>
            <span>user <b>{data.user || "—"}</b></span>
            <span className="sep">/</span>
            <span>{data.backend}</span>
          </div>
          <div className="right">
            <span className={"pill" + (pending > 0 ? " attn" : "")} onClick={() => setView("approvals")}>
              {pending > 0 && <Dot kind="work" />}{pending} pending
            </span>
            <button className="icon-btn" onClick={() => setSettings(true)}>settings</button>
          </div>
        </div>

        {/* main */}
        <main key={view}>{views[view]}</main>

        {/* ambient command bar */}
        <div className="ambient">
          <span className="prompt">ark&gt;</span>
          <input ref={inputRef} spellCheck={false} autoComplete="off"
            placeholder="tell buddy what to do. or just think out loud." onKeyDown={onKey} />
          <div className="hints">
            <span className="hint"><kbd>/</kbd> focus</span>
            <span className="hint"><kbd>enter</kbd> send</span>
            <span className="hint"><kbd>esc</kbd> clear</span>
          </div>
          <div className="floaters">
            {floaters.map((f) => (
              <div className={"floater" + (f.fold ? " fold" : "")} key={f.id}>
                <span className="who">{f.who}</span>
                {(f.text && f.text !== "…")
                  ? f.text
                  : (f.status
                      ? <span className="mute"><span className="spin" style={{ marginRight: 8, verticalAlign: "-1px" }} />{f.status}…</span>
                      : f.text)}
              </div>
            ))}
          </div>
        </div>
      </div>

      {settings && <SettingsModal data={data} onClose={() => setSettings(false)} onSignOut={signOut} />}
      {!loginGone && !booting && <Login gone={authed} onEnter={enter} />}
    </React.Fragment>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
