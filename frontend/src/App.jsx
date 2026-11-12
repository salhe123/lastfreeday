import { useCallback, useEffect, useState } from "react";

const TOKEN_KEY = "lfd_token";

function authHeaders() {
  const token = sessionStorage.getItem(TOKEN_KEY);
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}), ...authHeaders() };
  if (options.body && !(options.body instanceof FormData) && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(path, { ...options, headers });
  const text = await res.text();
  let data;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = { detail: text };
  }
  if (res.status === 401) {
    sessionStorage.removeItem(TOKEN_KEY);
  }
  if (!res.ok) {
    throw new Error(data.detail || data.message || res.statusText);
  }
  return data;
}

function money(n, ccy) {
  if (n == null) return "—";
  return `${Number(n).toFixed(0)} ${ccy || ""}`.trim();
}

export default function App() {
  const [user, setUser] = useState(null);
  const [tab, setTab] = useState("board");
  const [health, setHealth] = useState(null);
  const [board, setBoard] = useState(null);
  const [selected, setSelected] = useState(null);
  const [review, setReview] = useState([]);
  const [tariffs, setTariffs] = useState([]);
  const [audit, setAudit] = useState([]);
  const [notice, setNotice] = useState("");
  const [parsed, setParsed] = useState(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [flash, setFlash] = useState("");
  const [loginForm, setLoginForm] = useState({ username: "ops", password: "ops" });

  const refreshBoard = useCallback(async () => {
    const data = await api("/api/board");
    setBoard(data);
    return data;
  }, []);

  useEffect(() => {
    const token = sessionStorage.getItem(TOKEN_KEY);
    if (!token) return;
    api("/api/health")
      .then(setHealth)
      .catch(() => setHealth({ ok: false }));
    api("/api/board")
      .then((data) => {
        setBoard(data);
        setUser(JSON.parse(sessionStorage.getItem("lfd_user") || "null"));
      })
      .catch(() => sessionStorage.removeItem(TOKEN_KEY));
  }, []);

  async function run(label, fn) {
    setBusy(label);
    setError("");
    setFlash("");
    try {
      return await fn();
    } catch (err) {
      setError(err.message);
      throw err;
    } finally {
      setBusy("");
    }
  }

  async function doLogin(e) {
    e.preventDefault();
    await run("login", async () => {
      const data = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify(loginForm),
      });
      sessionStorage.setItem(TOKEN_KEY, data.token);
      sessionStorage.setItem("lfd_user", JSON.stringify(data));
      setUser(data);
      setHealth(await api("/api/health"));
      await refreshBoard();
    });
  }

  function logout() {
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.removeItem("lfd_user");
    setUser(null);
    setBoard(null);
    setSelected(null);
    setTab("board");
    setReview([]);
  }

  async function loadSample() {
    await run("sample", async () => {
      const data = await api("/api/sample");
      setNotice(data.text);
    });
  }

  async function parseNotice() {
    await run("parse", async () => {
      const data = await api("/api/ingest/parse", {
        method: "POST",
        body: JSON.stringify({ text: notice }),
      });
      setParsed(data);
    });
  }

  async function uploadPdf(file) {
    if (!file) return;
    await run("pdf", async () => {
      const body = new FormData();
      body.append("file", file);
      const data = await api("/api/ingest/pdf", { method: "POST", body });
      setParsed(data);
      setNotice("(from PDF text layer)");
    });
  }

  async function saveBl() {
    await run("save", async () => {
      const saved = await api("/api/bls", {
        method: "POST",
        body: JSON.stringify({
          raw_source: notice,
          source_type: "paste",
          extract: parsed.extract,
          reconciled: parsed.reconciled,
        }),
      });
      setFlash(
        saved.status === "review"
          ? `Saved ${saved.containers.length} containers to REVIEW — no auto-mail.`
          : `Saved ${saved.containers.length} containers.`
      );
      setParsed(null);
      await refreshBoard();
      setTab("board");
    });
  }

  async function openDetail(id) {
    await run("detail", async () => {
      setSelected(await api(`/api/containers/${id}`));
    });
  }

  async function scanAndDrain() {
    await run("scan", async () => {
      const scan = await api("/api/jobs/scan-deadlines", {
        method: "POST",
        headers: { "X-Scan-Secret": "lastday-dev" },
      });
      const drain = await api("/api/jobs/drain-outbox", {
        method: "POST",
        headers: { "X-Scan-Secret": "lastday-dev" },
      });
      setFlash(
        `Scan queued ${scan.queued.length}, blocked ${scan.blocked.length} (no auto-mail), missed ${scan.missed.length}. Outbox drained ${drain.drained.length}.`
      );
      await refreshBoard();
      if (selected) await openDetail(selected.id);
    });
  }

  async function approve(id) {
    await run("approve", async () => {
      await api(`/api/containers/${id}/approve-nudge`, { method: "POST" });
      setFlash("Owner approved nudge. Outbox queued.");
      await refreshBoard();
      await openDetail(id);
      if (tab === "review") await loadReview();
    });
  }

  async function markStatus(status) {
    await run("patch", async () => {
      await api(`/api/containers/${selected.id}`, {
        method: "PATCH",
        body: JSON.stringify({ version: selected.version, status }),
      });
      await refreshBoard();
      await openDetail(selected.id);
    });
  }

  async function downloadIcs() {
    await run("ics", async () => {
      const res = await fetch(`/api/containers/${selected.id}/calendar.ics`, { headers: authHeaders() });
      if (!res.ok) throw new Error("ICS download failed");
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${selected.number}.ics`;
      a.click();
      URL.revokeObjectURL(url);
    });
  }

  async function holdCalendar() {
    await run("cal", async () => {
      await api(`/api/containers/${selected.id}/calendar`, { method: "POST" });
      setFlash("ICS hold created for this container.");
      await openDetail(selected.id);
    });
  }

  async function loadReview() {
    setReview(await api("/api/review"));
  }
  async function loadTariffs() {
    setTariffs(await api("/api/tariffs"));
  }
  async function loadAudit() {
    setAudit(await api("/api/audit"));
  }

  useEffect(() => {
    if (!user) return;
    if (tab === "review") loadReview().catch((e) => setError(e.message));
    if (tab === "tariffs") loadTariffs().catch((e) => setError(e.message));
    if (tab === "activity") loadAudit().catch((e) => setError(e.message));
  }, [tab, user]);

  if (!user) {
    return (
      <div className="wrap">
        <h1>LastFreeDay</h1>
        <p className="sub">Demurrage control desk — login</p>
        <form className="panel" onSubmit={doLogin} style={{ maxWidth: 360, marginTop: 20 }}>
          <label className="muted">
            Username
            <input
              value={loginForm.username}
              onChange={(e) => setLoginForm({ ...loginForm, username: e.target.value })}
            />
          </label>
          <label className="muted" style={{ display: "block", marginTop: 8 }}>
            Password
            <input
              type="password"
              value={loginForm.password}
              onChange={(e) => setLoginForm({ ...loginForm, password: e.target.value })}
            />
          </label>
          <p className="sub">Demo: ops/ops · owner/owner · viewer/viewer</p>
          <button disabled={!!busy} style={{ marginTop: 12 }}>
            {busy === "login" ? "…" : "Enter"}
          </button>
          {error && <p className="err">{error}</p>}
        </form>
      </div>
    );
  }

  return (
    <div className="wrap wide">
      <header>
        <div>
          <h1>LastFreeDay</h1>
          <div className="sub">Demurrage control desk · container clocks, money at risk, human review</div>
        </div>
        <div className="sub">
          {user.username} ({user.role}) · Gemini {health?.gemini ? "on" : "offline parse"} · Mail{" "}
          {health?.mail || "dry_run"}{" "}
          <button className="ghost" onClick={logout} style={{ marginLeft: 8 }}>
            Log out
          </button>
        </div>
      </header>

      <nav className="tabs">
        {["board", "ingest", "review", "tariffs", "activity"].map((t) => (
          <button key={t} className={tab === t ? "" : "ghost"} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
        <button className="ghost" disabled={!!busy} onClick={scanAndDrain}>
          {busy === "scan" ? "Running…" : "Scan + drain outbox"}
        </button>
      </nav>

      {tab === "board" && board && (
        <>
          <div className="cards">
            <div className="card">
              <div className="n">{money(board.exposure, board.currency)}</div>
              <div className="l">Exposure (open boxes)</div>
            </div>
            <div className="card">
              <div className={`n ${board.bands.burning ? "hot" : ""}`}>{board.bands.burning}</div>
              <div className="l">Burning</div>
            </div>
            <div className="card">
              <div className="n">{board.bands.critical + board.bands.watch}</div>
              <div className="l">Watch / critical</div>
            </div>
            <div className="card">
              <div className={`n ${board.review ? "hot" : ""}`}>{board.review}</div>
              <div className="l">Review queue</div>
            </div>
          </div>
          <section className="panel">
            <strong>Exposure board</strong>
            <table className="table">
              <thead>
                <tr>
                  <th>Container</th>
                  <th>Band</th>
                  <th>LFD final</th>
                  <th>Days</th>
                  <th>Extracted vs computed</th>
                  <th>Cost</th>
                  <th>Conf</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {(board.containers || []).length === 0 && (
                  <tr>
                    <td className="muted" colSpan={8}>
                      Nothing ingested yet.
                    </td>
                  </tr>
                )}
                {(board.containers || []).map((c) => (
                  <tr key={c.id} onClick={() => { setTab("board"); openDetail(c.id); }}>
                    <td>{c.number}</td>
                    <td>
                      <span className={`chip ${c.risk_band}`}>{c.risk_band}</span>
                    </td>
                    <td>{c.lfd_final || "—"}</td>
                    <td className={c.days_left != null && c.days_left <= 1 ? "hot" : ""}>
                      {c.days_left ?? "—"}
                    </td>
                    <td>
                      {c.lfd_extracted || "—"} / {c.lfd_computed || "—"}
                      {c.conflict ? " ⚠" : ""}
                    </td>
                    <td>{money(c.projected_cost, c.currency)}</td>
                    <td>{c.confidence ?? "—"}</td>
                    <td>{c.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}

      {tab === "ingest" && (
        <section className="panel">
          <strong>Ingest arrival notice</strong>
          <p className="sub">
            Gemini extracts per container. Server computes LFD from discharge + free days + holidays. Mismatch → review,
            no auto-mail.
          </p>
          <textarea value={notice} onChange={(e) => setNotice(e.target.value)} />
          <div className="row">
            <button className="ghost" disabled={!!busy} onClick={loadSample}>
              Load sample
            </button>
            <button disabled={!!busy || notice.length < 20} onClick={parseNotice}>
              {busy === "parse" ? "Parsing…" : "Parse"}
            </button>
            <label className="ghost btn">
              PDF
              <input
                type="file"
                accept="application/pdf"
                hidden
                onChange={(e) => uploadPdf(e.target.files?.[0])}
              />
            </label>
          </div>
          {parsed && (
            <div style={{ marginTop: 16 }}>
              <p>
                Confidence {parsed.confidence} · {parsed.needs_review ? "REVIEW (will not auto-email)" : "can watch"} ·
                prompt {parsed.prompt_version}
              </p>
              <table className="table">
                <thead>
                  <tr>
                    <th>Container</th>
                    <th>Extracted LFD</th>
                    <th>Computed LFD</th>
                    <th>Conflict</th>
                  </tr>
                </thead>
                <tbody>
                  {parsed.reconciled.map((c) => (
                    <tr key={c.number}>
                      <td>{c.number}</td>
                      <td>{c.lfd_extracted || "—"}</td>
                      <td>{c.lfd_computed || "—"}</td>
                      <td className={c.conflict ? "hot" : "ok"}>{c.conflict ? "yes" : "no"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="row">
                <button disabled={!!busy} onClick={saveBl}>
                  Confirm save
                </button>
                <button className="ghost" onClick={() => setParsed(null)}>
                  Discard
                </button>
              </div>
            </div>
          )}
        </section>
      )}

      {tab === "review" && (
        <section className="panel">
          <strong>Review queue</strong>
          <p className="sub">Agent refused to auto-send. Owner approves the nudge.</p>
          {review.length === 0 && <p className="muted">Queue empty.</p>}
          {review.map((c) => (
            <div key={c.id} className="row" style={{ alignItems: "center" }}>
              <span>
                {c.number} · LFD {c.lfd_final} · conf {c.confidence} · {c.conflict ? "conflict" : "low confidence"}
              </span>
              {user.role === "owner" && (
                <button disabled={!!busy} onClick={() => approve(c.id)}>
                  Approve nudge
                </button>
              )}
              <button className="ghost" onClick={() => openDetail(c.id)}>
                Open
              </button>
            </div>
          ))}
        </section>
      )}

      {tab === "tariffs" && (
        <section className="panel">
          <strong>Tariffs</strong>
          <table className="table">
            <thead>
              <tr>
                <th>Line</th>
                <th>Port</th>
                <th>Free days</th>
                <th>Weekday</th>
                <th>Weekend</th>
                <th>Ccy</th>
              </tr>
            </thead>
            <tbody>
              {tariffs.map((t) => (
                <tr key={t.id}>
                  <td>{t.line}</td>
                  <td>{t.port}</td>
                  <td>{t.free_days}</td>
                  <td>{t.weekday_rate}</td>
                  <td>{t.weekend_rate}</td>
                  <td>{t.currency}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {tab === "activity" && (
        <section className="panel">
          <strong>Audit</strong>
          {audit.map((ev) => (
            <pre key={ev.id}>
              {ev.created_at} · {ev.actor} · {ev.type} · req {ev.request_id} · ctr {ev.container_id}
              {"\n"}
              {JSON.stringify(ev.payload, null, 2)}
            </pre>
          ))}
        </section>
      )}

      {selected && (
        <section className="panel">
          <strong>
            {selected.number} · {selected.status}
          </strong>
          <p className="sub">
            Extracted {selected.lfd_extracted || "—"} · computed {selected.lfd_computed || "—"} · final{" "}
            {selected.lfd_final || "—"} · days {selected.days_left ?? "?"} · conf {selected.confidence}
          </p>
          {selected.what_if && (
            <p>
              What-if: gate tomorrow {money(selected.what_if.if_gates_tomorrow, selected.what_if.currency)} · wait 3 days{" "}
              {money(selected.what_if.if_gates_in_3_days, selected.what_if.currency)} · extra{" "}
              {money(selected.what_if.extra_if_waits_3_days, selected.what_if.currency)}
            </p>
          )}
          <div className="row">
            <button disabled={!!busy} onClick={holdCalendar}>
              ICS hold
            </button>
            <button className="ghost" disabled={!!busy} onClick={downloadIcs}>
              Download .ics
            </button>
            <button className="ghost" disabled={!!busy} onClick={() => markStatus("gated")}>
              Mark gated
            </button>
            <button className="ghost" disabled={!!busy} onClick={() => markStatus("cleared")}>
              Mark cleared
            </button>
            {user.role === "owner" && selected.status === "review" && (
              <button disabled={!!busy} onClick={() => approve(selected.id)}>
                Approve nudge
              </button>
            )}
            <button className="ghost" onClick={() => setSelected(null)}>
              Close
            </button>
          </div>
          <h3 style={{ fontSize: 14, margin: "18px 0 8px" }}>Outbox</h3>
          {(selected.outbox || []).length === 0 && <div className="muted">No messages.</div>}
          {(selected.outbox || []).map((m) => (
            <pre key={m.id}>
              {m.status} · window T−{m.window} · {m.to_addr}
              {"\n"}
              {m.subject}
            </pre>
          ))}
          <h3 style={{ fontSize: 14, margin: "18px 0 8px" }}>Audit</h3>
          {(selected.events || []).map((ev) => (
            <pre key={ev.id}>
              {ev.type} · {ev.actor} · {ev.created_at} · {ev.request_id}
            </pre>
          ))}
        </section>
      )}

      {flash && <p className="ok">{flash}</p>}
      {error && <p className="err">{error}</p>}
    </div>
  );
}
