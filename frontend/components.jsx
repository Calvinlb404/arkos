/* =========================================================
   components — shared atoms + cards
   ========================================================= */
const { useState, useRef, useEffect } = React;

/* ---- atoms ---- */
function Dot({ kind }) { return <span className={"dot" + (kind ? " " + kind : "")} />; }

function Empty({ glyph, children }) {
  return (
    <div className="empty">
      <span className="glyph">{glyph}</span>
      {children}
    </div>
  );
}

/* ---- approval card (workshopped plan awaiting your ok) ---- */
function ApprovalCard({ item, onResolve }) {
  const [noteOpen, setNoteOpen] = useState(false);
  const [gone, setGone] = useState(false);
  const ref = useRef(null);
  const noteRef = useRef(null);

  function resolve(verb) {
    const note = noteRef.current ? noteRef.current.value.trim() : "";
    if (ref.current) {
      ref.current.style.transition = "opacity .35s, transform .35s, margin .35s, max-height .35s, padding .35s, border-color .35s";
      ref.current.style.maxHeight = ref.current.offsetHeight + "px";
      requestAnimationFrame(() => {
        ref.current.style.maxHeight = "0px";
        ref.current.style.opacity = "0";
        ref.current.style.paddingTop = "0px";
        ref.current.style.paddingBottom = "0px";
        ref.current.style.marginBottom = "-12px";
        ref.current.style.borderColor = "transparent";
        ref.current.style.transform = "translateY(-4px)";
      });
    }
    setGone(true);
    setTimeout(() => onResolve(item.id, verb, note), 360);
  }

  const plan = item.plan || [];
  const tools = item.tools || [];

  return (
    <div className="card approval" ref={ref} style={{ overflow: "hidden" }}>
      <div className="top">
        <span className="src"><Dot kind="work" /> {item.src}</span>
        <span className="tag accent">{item.tag}</span>
      </div>
      <div className="title">{item.title}</div>
      {item.body && <div className="body">{item.body}</div>}
      {(plan.length > 0 || tools.length > 0) && (
        <div className="plan">
          {plan.length > 0 && <ol>{plan.map((p, i) => <li key={i}>{p}</li>)}</ol>}
          {tools.length > 0 && (
            <div className="toolrow">
              {tools.map((t) => <span className="chip" key={t}>{t}</span>)}
            </div>
          )}
        </div>
      )}
      <textarea
        ref={noteRef}
        className={"note" + (noteOpen ? " open" : "")}
        placeholder="add a note or tweak before approving…"
      />
      <div className="actions">
        <button className="btn ghost grow" style={{ textAlign: "left", flex: "0 0 auto" }} onClick={() => setNoteOpen((o) => !o)}>
          {noteOpen ? "− note" : "+ note"}
        </button>
        <span className="grow" />
        <button className="btn" disabled={gone} onClick={() => resolve("declined")}>decline</button>
        <button className="btn primary" disabled={gone} onClick={() => resolve("approved")}>approve →</button>
      </div>
    </div>
  );
}

/* ---- inline plan card in chat (workshopped plan awaiting approval).
   nothing runs until approve; mirrors the original app.js plan-card flow. ---- */
function PlanCard({ plan, resolved, error, onApprove, onDecline }) {
  const steps = plan.plan_steps || [];
  const tools = (plan.required_tools || []);
  const isComputer = plan.target === "computer";
  return (
    <div className="card approval" style={{ marginTop: 10, maxWidth: 560 }}>
      <div className="top">
        <span className="src"><Dot kind="work" /> {plan.title || "plan"}</span>
        <span className="tag accent">{isComputer ? "computer" : "task"}</span>
      </div>
      <div className="plan">
        <ol>{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
        <div className="toolrow">
          {tools.map((t) => <span className="chip" key={t}>{t}</span>)}
          {isComputer && <span className="chip">sandbox</span>}
        </div>
      </div>
      {error && <div className="body" style={{ color: "var(--stop)" }}>dispatch failed: {error}</div>}
      <div className="actions">
        <span className="grow" />
        {resolved
          ? <span className="mute" style={{ fontSize: 11 }}>{resolved === "approved" ? "✓ approved — running" : "declined"}</span>
          : <React.Fragment>
              <button className="btn" onClick={onDecline}>decline</button>
              <button className="btn primary" onClick={onApprove}>{isComputer ? "approve & run →" : "approve →"}</button>
            </React.Fragment>}
      </div>
    </div>
  );
}

/* ---- task row (expandable event log) ---- */
function TaskRow({ item, onCancel }) {
  const [open, setOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const running = item.state === "run";

  async function cancel(e) {
    e.stopPropagation();
    setCancelling(true);
    try { await api.cancelTask(item.id); } catch { /* ignored — poll will refresh */ }
  }

  return (
    <div className={"row col" + (open ? " expanded" : "")}>
      <div className="row-top" style={{ cursor: item.events ? "pointer" : "default" }} onClick={() => item.events && setOpen((o) => !o)}>
        <span className="label">
          {running ? <span className="spin" /> : <Dot kind={item.state === "done" ? "live" : "stop"} />}
          <span className="text">{item.text} <span className="src">· {item.src}</span></span>
        </span>
        <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className="when">{item.when}</span>
          {running && (
            <button
              className="btn ghost"
              style={{ fontSize: "0.75em", padding: "2px 8px", opacity: cancelling ? 0.4 : 1 }}
              disabled={cancelling}
              onClick={cancel}
            >
              cancel
            </button>
          )}
        </span>
      </div>
      {item.events && (
        <div className={"events" + (open ? " open" : "")}>
          {item.events.map((e, i) => (
            <div className="ev" key={i}>
              <span className="k">{e.k}</span>
              <span className={e.ok ? "ok" : ""}>{e.t}{e.ok ? " ✓" : ""}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---- completed task card — shown in the pending approvals zone on the desk.
   Dismissed client-side (per session); clicking dismiss calls onDismiss(id). ---- */
function CompletedTaskCard({ item, onDismiss }) {
  const [gone, setGone] = useState(false);
  const ref = useRef(null);
  const failed = item.state === "stop";

  function dismiss() {
    if (ref.current) {
      ref.current.style.transition = "opacity .3s, max-height .35s, padding .35s, margin .35s, border-color .35s";
      ref.current.style.maxHeight = ref.current.offsetHeight + "px";
      requestAnimationFrame(() => {
        ref.current.style.maxHeight = "0px";
        ref.current.style.opacity = "0";
        ref.current.style.paddingTop = "0px";
        ref.current.style.paddingBottom = "0px";
        ref.current.style.marginBottom = "-12px";
        ref.current.style.borderColor = "transparent";
      });
    }
    setGone(true);
    setTimeout(() => onDismiss(item.id), 360);
  }

  return (
    <div className="card approval" ref={ref} style={{ overflow: "hidden" }}>
      <div className="top">
        <span className="src">
          <Dot kind={failed ? "stop" : "live"} /> {failed ? "failed" : "done"}
        </span>
        <span className="tag" style={{ color: failed ? "var(--err,#c0392b)" : "var(--acc,#4ade80)" }}>
          {failed ? "✗ failed" : "✓ completed"}
        </span>
      </div>
      <div className="title">{item.text}</div>
      <div className="actions">
        <span className="grow" />
        <button className="btn primary" disabled={gone} onClick={dismiss}>dismiss →</button>
      </div>
    </div>
  );
}

/* ---- watch row ---- */
function WatchRow({ item }) {
  return (
    <div className="row">
      <span className="label">
        <Dot kind={item.live ? "live" : ""} />
        <span className="text">{item.src}{item.note ? <span className="src"> · {item.note}</span> : null}</span>
      </span>
      <span className="when">{item.cadence}</span>
    </div>
  );
}

Object.assign(window, { Dot, Empty, ApprovalCard, PlanCard, TaskRow, WatchRow, CompletedTaskCard });
