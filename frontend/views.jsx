/* =========================================================
   views
   ========================================================= */

function PageHead({ title, accent, lede }) {
  return (
    <div className="head">
      <h1>{title}{accent && <span className="accent">{accent}</span>}<span className="caret" /></h1>
      {lede && <div className="lede">{lede}</div>}
    </div>
  );
}

/* ---------- DESK ---------- */
function DeskView({ data, onResolve }) {
  return (
    <div className="view">
      <PageHead
        title="buddy's desk"
        lede="your digital life, handled in the background. buddy watches, triages, and workshops plans for your approval."
      />
      <div className="zones">
        <section className="zone">
          <header>
            <span className="kicker">pending approvals</span>
            <span className="n">{data.approvals.length}</span>
          </header>
          <div className="stack">
            {data.approvals.length === 0
              ? <Empty glyph="✓">nothing waiting on you</Empty>
              : data.approvals.map((a) => <ApprovalCard key={a.id} item={a} onResolve={onResolve} />)}
          </div>
        </section>

        <section className="zone">
          <header>
            <span className="kicker">active tasks</span>
            <span className="n">{data.tasks.filter((t) => t.state === "run").length}</span>
          </header>
          <div className="stack">
            {data.tasks.length === 0
              ? <Empty glyph="○">buddy is idle</Empty>
              : data.tasks.map((t) => <TaskRow key={t.id} item={t} />)}
          </div>
        </section>

        <section className="zone">
          <header>
            <span className="kicker">watching</span>
            <span className="n">{data.watching.length}</span>
          </header>
          <div className="stack">
            {data.watching.map((w) => <WatchRow key={w.id} item={w} />)}
          </div>
        </section>
      </div>
    </div>
  );
}

/* ---------- TASKS ---------- */
function TasksView({ data }) {
  return (
    <div className="view">
      <PageHead title="tasks" lede="everything buddy is doing or has done. expand a task to follow its thinking." />
      <div className="stack" style={{ maxWidth: 820 }}>
        {data.tasks.length === 0
          ? <Empty glyph="○">buddy is idle</Empty>
          : data.tasks.map((t) => <TaskRow key={t.id} item={t} />)}
      </div>
    </div>
  );
}

/* ---------- WATCHING ---------- */
function WatchingView({ data }) {
  return (
    <div className="view">
      <PageHead title="watching" lede="the sources buddy keeps an eye on, and how often it checks." />
      <div className="stack" style={{ maxWidth: 820 }}>
        {data.watching.map((w) => <WatchRow key={w.id} item={w} />)}
      </div>
    </div>
  );
}

/* ---------- APPROVALS ---------- */
function ApprovalsView({ data, onResolve }) {
  return (
    <div className="view">
      <PageHead title="approvals" lede="plans buddy has workshopped and is holding for your ok. nothing acts without it." />
      <div className="stack" style={{ maxWidth: 620 }}>
        {data.approvals.length === 0
          ? <Empty glyph="✓">all caught up — nothing waiting on you</Empty>
          : data.approvals.map((a) => <ApprovalCard key={a.id} item={a} onResolve={onResolve} />)}
      </div>
    </div>
  );
}

/* ---------- CHAT ---------- */
function ChatView({ data }) {
  return (
    <div className="view">
      <PageHead title="chat" lede="think out loud with buddy. it remembers, and turns the useful bits into standing rules." />
      <div className="chat-wrap">
        {data.chat.length === 0
          ? <Empty glyph="·">say hi — buddy is listening</Empty>
          : data.chat.map((m, i) => (
            <div className={"msg " + (m.who === "you" ? "user" : "buddy")} key={i}>
              <span className="who">{m.who === "you" ? (data.user || "you") : "buddy"}</span>
              <span className="bubble">{m.text}</span>
            </div>
          ))}
      </div>
    </div>
  );
}

/* ---------- COMPUTER ---------- */
function ComputerView({ data }) {
  const [path, setPath] = useState("/home/user");
  const [entries, setEntries] = useState([]);
  const [filePath, setFilePath] = useState(null);
  const [body, setBody] = useState(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);

  async function browse(p) {
    setLoading(true); setErr(null); setFilePath(null); setBody(null);
    try {
      const es = await api.computerFiles(p);
      setEntries(es);
      setPath(p);
    } catch (e) {
      setErr("could not read directory");
      setEntries([]);
    } finally { setLoading(false); }
  }

  async function openFile(p) {
    setFilePath(p); setBody("loading…");
    try {
      const j = await api.computerFile(p);
      setBody((j.content || "") + (j.truncated ? "\n\n… (truncated, file is " + j.size + " bytes)" : ""));
    } catch (e) {
      setBody("(could not read file)");
    }
  }

  useEffect(() => { browse("/home/user"); }, []);

  function goUp() {
    const parts = path.split("/").filter(Boolean);
    parts.pop();
    browse("/" + parts.join("/") || "/");
  }

  // dirs first, then files, alphabetical
  const sorted = [...entries].sort((a, b) => ((b.is_dir ? 1 : 0) - (a.is_dir ? 1 : 0)) || a.name.localeCompare(b.name));
  const running = data.computerTasks.filter((t) => ["running", "pending"].includes(t.status)).length;

  return (
    <div className="view view-wide" style={{ padding: 0, height: "100%" }}>
      <div className="computer">
        <div className="cv-files">
          <div className="cv-head">
            <span className="path">{path}</span>
            <button className="icon-btn" onClick={goUp} disabled={path === "/" || loading}>up ↑</button>
          </div>
          <div className="cv-entries">
            {loading ? (
              <div className="cv-entry" style={{ color: "var(--ink-mute)" }}><span className="nm"><span className="g">·</span>loading…</span></div>
            ) : err ? (
              <div className="cv-entry" style={{ color: "var(--ink-mute)" }}><span className="nm"><span className="g">·</span>{err}</span></div>
            ) : sorted.length === 0 ? (
              <div className="cv-entry" style={{ color: "var(--ink-mute)" }}><span className="nm"><span className="g">·</span>(empty)</span></div>
            ) : sorted.map((e, i) => (
              <div className={"cv-entry" + (e.is_dir ? " dir" : "") + (filePath === e.path ? " sel" : "")} key={i}
                onClick={() => e.is_dir ? browse(e.path) : openFile(e.path)}>
                <span className="nm"><span className="g">{e.is_dir ? "▸" : "·"}</span>{e.name}</span>
                {!e.is_dir && e.size != null && <span className="sz">{fmtSize(e.size)}</span>}
              </div>
            ))}
          </div>
        </div>
        <div className="cv-read">
          <div className="cv-read-head">
            <span>{filePath ? filePath : "select a file to read"}</span>
            {running > 0 && <span style={{ display: "flex", alignItems: "center", gap: 8 }}><span className="spin" /> {running} running</span>}
          </div>
          {body !== null ? (
            <pre className="cv-read-body">{body.split("\n").map((ln, i) => (
              <div key={i}><span className="ln">{String(i + 1).padStart(2, " ")}</span>{ln || " "}</div>
            ))}</pre>
          ) : (
            <div className="cv-read-body" style={{ color: "var(--ink-mute)", fontStyle: "italic" }}>
              buddy keeps a workspace here. click a file on the left to read it.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " b";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " kb";
  return (n / 1024 / 1024).toFixed(1) + " mb";
}

/* ---------- SETTINGS MODAL ---------- */
function SettingsModal({ data, onClose, onSignOut }) {
  const [services, setServices] = useState(null);   // { shared, per_user, error }
  const [busy, setBusy] = useState({});
  const pollRef = useRef(null);

  async function refresh() {
    setServices(await api.services());
  }
  useEffect(() => {
    refresh();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, []);

  function startPoll(service, popup) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      const s = await api.services();
      setServices(s);
      const svc = (s.per_user || []).find((x) => x.service === service);
      if ((svc && svc.connected) || (popup && popup.closed)) {
        clearInterval(pollRef.current); pollRef.current = null;
        setTimeout(refresh, 400);
      }
    }, 2000);
  }

  async function connect(service) {
    setBusy((b) => ({ ...b, [service]: true }));
    try {
      const j = await api.connectService(service);
      if (j.status === "connected") { await refresh(); return; }
      if (j.setup_url) {
        const popup = window.open(j.setup_url, "ark_oauth", "width=560,height=720");
        startPoll(service, popup);
      } else {
        await refresh();
      }
    } finally {
      setBusy((b) => ({ ...b, [service]: false }));
    }
  }

  async function disconnect(service) {
    setBusy((b) => ({ ...b, [service]: true }));
    await api.disconnectService(service);
    await refresh();
    setBusy((b) => ({ ...b, [service]: false }));
  }

  const perUser = (services && services.per_user) || [];
  const shared = (services && services.shared) || [];
  const all = [...perUser, ...shared];
  const isPerUser = (svc) => perUser.some((s) => s.service === svc.service);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>settings</h2>
        <p className="sub">connections, account, and backend.</p>

        <section>
          <span className="kicker">tools &amp; connections</span>
          {services === null ? (
            <div className="soft" style={{ fontSize: 12, padding: "8px 2px" }}>loading…</div>
          ) : services.error ? (
            <div className="soft" style={{ fontSize: 12, padding: "8px 2px", color: "var(--bad, #c0392b)" }}>{services.error}</div>
          ) : all.length === 0 ? (
            <div className="soft" style={{ fontSize: 12, padding: "8px 2px" }}>no services configured. add entries to <code>mcp_servers</code> in config.yaml.</div>
          ) : all.map((c) => {
            const connected = !!c.connected;
            const perU = isPerUser(c);
            return (
              <div className="conn" key={c.service}>
                <span className="meta"><Dot kind={connected ? "live" : ""} /> <span className="nm">{c.name || c.service}</span></span>
                <span style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <span className={"st" + (connected ? " on" : "")}>{connected ? "connected" : (perU ? "not connected" : "shared")}</span>
                  {perU ? (
                    connected
                      ? <button className="btn" disabled={busy[c.service]} onClick={() => disconnect(c.service)}>disconnect</button>
                      : <button className="btn primary" disabled={busy[c.service]} onClick={() => connect(c.service)}>{busy[c.service] ? "…" : "connect"}</button>
                  ) : (
                    <span className="soft" style={{ fontSize: 10 }}>always on</span>
                  )}
                </span>
              </div>
            );
          })}
        </section>

        <section>
          <span className="kicker">account</span>
          <div className="soft" style={{ fontSize: 12, lineHeight: 1.9 }}>
            signed in as <b style={{ color: "var(--ink)" }}>{data.user || "—"}</b><br />
            backend · <b style={{ color: "var(--ink)" }}>{data.backend}</b>
          </div>
        </section>

        <div className="foot">
          <span className="mute" style={{ fontSize: 10.5 }}>changes save automatically</span>
          <div style={{ display: "flex", gap: 8 }}>
            <button className="btn danger" onClick={onSignOut}>sign out</button>
            <button className="btn" onClick={onClose}>close</button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---------- LOGIN ---------- */
function Login({ gone, onEnter }) {
  const [name, setName] = useState("");
  const [backend, setBackend] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef(null);
  useEffect(() => { if (!gone && inputRef.current) inputRef.current.focus(); }, [gone]);

  function submit() {
    const n = name.trim();
    if (n.length < 2) { setErr("username must be at least 2 characters"); return; }
    setErr(""); setBusy(true);
    onEnter(n, backend.trim(), (msg) => { setErr(msg); setBusy(false); });
  }

  return (
    <div className={"login" + (gone ? " gone" : "")}>
      <div className="login-card">
        <div className="mark-lg">ark<span className="pip" /></div>
        <p>buddy handles your digital life in the background. pick a name to begin — no passwords, no email. just you and your buddy.</p>
        <div className="field">
          <label>username</label>
          <input ref={inputRef} value={name} placeholder="e.g. nate" spellCheck={false}
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
        </div>
        <div className="field opt">
          <label>backend url — optional override</label>
          <input value={backend} placeholder="ark.mit.edu" spellCheck={false}
            onChange={(e) => setBackend(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
        </div>
        {err && <div className="login-err" style={{ color: "var(--bad, #c0392b)", fontSize: 12, marginTop: 4 }}>{err}</div>}
        <div className="go">
          <span className="hint">press enter to continue</span>
          <button className="btn primary lg" onClick={submit} disabled={!name.trim() || busy}>{busy ? "…" : "enter →"}</button>
        </div>
      </div>
    </div>
  );
}

Object.assign(window, { PageHead, DeskView, TasksView, WatchingView, ApprovalsView, ChatView, ComputerView, SettingsModal, Login });
