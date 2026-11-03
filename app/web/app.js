/* Panel del agente de WhatsApp. Sin dependencias ni build. */
(() => {
  "use strict";

  const POLL_MS = 4000;
  const $ = (id) => document.getElementById(id);
  const store = {
    get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* modo privado */ } },
    clear() { try { sessionStorage.clear(); } catch { /* nada */ } },
  };

  const state = {
    token: store.get("token"),
    name: store.get("agentName"),
    conversations: [],
    filter: "all",
    search: "",
    current: null,
    currentMeta: null,
    lastSignature: "",
    view: "inbox",
    timer: null,
    metricsAt: 0,
  };

  // --- API -------------------------------------------------------------------

  async function api(path, options = {}) {
    const res = await fetch(`/api/admin${path}`, {
      ...options,
      headers: { "Authorization": `Bearer ${state.token}`, "Content-Type": "application/json", ...(options.headers || {}) },
    });
    if (res.status === 401) { logout("Sesión caducada o token inválido."); throw new Error("401"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
    return data;
  }

  // --- Utilidades --------------------------------------------------------------

  const fmtTime = (ts) => {
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return sameDay ? d.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })
                   : d.toLocaleDateString("es", { day: "2-digit", month: "short" });
  };
  const fmtDay = (ts) => new Date(ts * 1000).toLocaleDateString("es", { weekday: "long", day: "numeric", month: "long" });
  const fmtNum = (n) => new Intl.NumberFormat("es", { notation: n >= 10000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(n);
  const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // Formato de WhatsApp sobre texto ya escapado: *negrita*, _cursiva_, ~tachado~.
  const waFormat = (s) => escapeHtml(s)
    .replace(/\*([^*\n]+)\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)_([^_\n]+)_(?=\W|$)/g, "$1<em>$2</em>")
    .replace(/~([^~\n]+)~/g, "<s>$1</s>");
  const fmtDec = (n, d = 1) => n.toLocaleString("es", { minimumFractionDigits: d, maximumFractionDigits: d });
  const fmtDate = (iso) => { const [y, m, d] = iso.split("-").map(Number); return new Date(y, m - 1, d).toLocaleDateString("es", { weekday: "short", day: "numeric", month: "short" }); };
  const plain = (s) => s.replace(/[*_~]([^*_~\n]+)[*_~]/g, "$1");
  const maskNumber = (wa) => wa.length > 6 ? `+${wa.slice(0, wa.length - 6)}···${wa.slice(-3)}` : wa;

  let toastTimer;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg; el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4000);
  }

  // --- Acceso ------------------------------------------------------------------

  function showApp() {
    $("login").hidden = true; $("app").hidden = false;
    $("agent-name").textContent = state.name ? `Agente: ${state.name}` : "";
    refresh();
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  }

  function logout(message) {
    clearInterval(state.timer);
    store.clear();
    Object.assign(state, { token: "", current: null, conversations: [] });
    $("app").hidden = true; $("login").hidden = false;
    $("login-error").hidden = !message; $("login-error").textContent = message || "";
  }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    state.token = $("login-token").value.trim();
    state.name = $("login-name").value.trim();
    try {
      await api("/conversations?limit=1");
      store.set("token", state.token); store.set("agentName", state.name);
      $("login-token").value = "";
      showApp();
    } catch (err) {
      if (err.message !== "401") { $("login-error").hidden = false; $("login-error").textContent = err.message; }
    }
  });
  $("logout").addEventListener("click", () => logout());

  // --- Pestañas ----------------------------------------------------------------

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    state.view = tab.dataset.view;
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
    $("view-inbox").hidden = state.view !== "inbox";
    $("view-metrics").hidden = state.view !== "metrics";
    state.metricsAt = 0;
    refresh();
  }));

  async function refresh() {
    try {
      if (state.view === "inbox") {
        await loadConversations();
        if (state.current) await loadConversation(state.current, false);
      } else if (Date.now() - state.metricsAt > 60000) {
        await loadMetrics();
      }
    } catch (err) {
      if (err.message !== "401") console.warn(err);
    }
  }

  // --- Lista de conversaciones ---------------------------------------------------

  const needsHuman = (c) => c.mode === "human" && c.last_role === "user";

  async function loadConversations() {
    state.conversations = await api("/conversations");
    renderList();
  }

  function renderList() {
    const q = state.search.toLowerCase();
    const items = state.conversations.filter((c) =>
      (state.filter === "all" || c.mode === state.filter) &&
      (!q || `${c.name} ${c.wa_id} ${c.last_text}`.toLowerCase().includes(q)));
    const attention = state.conversations.filter(needsHuman).length;
    $("human-count").textContent = attention ? `(${attention})` : "";
    document.title = attention ? `(${attention}) Panel del agente` : "Panel del agente";

    const list = $("conv-list");
    list.replaceChildren(...items.map((c) => {
      const li = document.createElement("li");
      li.className = "conv" + (needsHuman(c) ? " attention" : "");
      li.tabIndex = 0;
      li.setAttribute("role", "button");
      if (c.wa_id === state.current) li.setAttribute("aria-current", "true");
      const name = document.createElement("span"); name.className = "name"; name.textContent = c.name || maskNumber(c.wa_id);
      const time = document.createElement("span"); time.className = "time"; time.textContent = c.last_ts ? fmtTime(c.last_ts) : "";
      const snip = document.createElement("span"); snip.className = "snippet";
      const who = { user: "", bot: "Bot: ", agent: "Agente: ", system: "· " }[c.last_role] || "";
      snip.textContent = who + plain(c.last_text);
      const badge = document.createElement("span"); badge.className = `badge ${c.mode}`;
      badge.textContent = c.mode === "human" ? "Humano" : "Bot";
      li.append(name, time, snip, badge);
      const open = () => openConversation(c.wa_id);
      li.addEventListener("click", open);
      li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
      return li;
    }));
    $("list-empty").hidden = items.length > 0;
  }

  $("search").addEventListener("input", (e) => { state.search = e.target.value; renderList(); });
  document.querySelectorAll(".chip").forEach((chip) => chip.addEventListener("click", () => {
    state.filter = chip.dataset.filter;
    document.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", String(c === chip)));
    renderList();
  }));

  // --- Conversación ---------------------------------------------------------------

  async function openConversation(waId) {
    state.current = waId; state.lastSignature = "";
    $("view-inbox").classList.add("show-chat");
    renderList();
    await loadConversation(waId, true);
    $("composer-text").focus();
  }

  $("back").addEventListener("click", () => {
    $("view-inbox").classList.remove("show-chat");
    state.current = null; renderList();
  });

  async function loadConversation(waId, forceScroll) {
    let data;
    try { data = await api(`/conversations/${encodeURIComponent(waId)}`); }
    catch (err) {
      if (err.message !== "401") { toast(err.message); state.current = null; $("chat").hidden = true; $("chat-empty").hidden = false; }
      return;
    }
    if (state.current !== waId) return; // el usuario cambió de conversación mientras cargaba
    state.currentMeta = data.meta;
    renderHeader(data.meta);
    const signature = `${data.messages.length}:${data.messages.at(-1)?.ts || 0}`;
    if (signature !== state.lastSignature) {
      renderMessages(data.messages, forceScroll);
      state.lastSignature = signature;
    }
    $("chat-empty").hidden = true; $("chat").hidden = false;
  }

  function renderHeader(m) {
    $("chat-name").textContent = m.name || "Sin nombre";
    $("chat-number").textContent = `+${m.wa_id}${m.opted_out ? " · sin promociones" : ""}`;
    const mode = $("chat-mode");
    mode.className = `badge ${m.mode}`;
    mode.textContent = m.mode === "human" ? "Atiende un humano" : "Atiende el bot";
    $("takeover").hidden = m.mode === "human";
    $("release").hidden = m.mode !== "human";

    const win = $("chat-window");
    if (m.window_open) {
      const hours = Math.max(0, (m.window_closes_at - Date.now() / 1000) / 3600);
      win.textContent = `Ventana 24 h: quedan ${hours >= 1 ? Math.floor(hours) + " h" : Math.ceil(hours * 60) + " min"}`;
      win.className = "small muted";
    } else {
      win.textContent = "Ventana 24 h cerrada";
      win.className = "small error";
    }
    const note = $("composer-note");
    const closed = !m.window_open;
    note.hidden = !(closed || m.mode === "bot");
    note.textContent = closed
      ? "La ventana de 24 h está cerrada: WhatsApp solo permite escribir con una plantilla aprobada."
      : "Al enviar un mensaje tomarás el control y el bot dejará de responder.";
    $("composer-text").disabled = closed;
    $("send").disabled = closed;
  }

  function renderMessages(messages, forceScroll) {
    const ol = $("messages");
    const nearBottom = ol.scrollHeight - ol.scrollTop - ol.clientHeight < 80;
    const labels = { user: "Cliente", bot: "Bot", agent: "Agente", system: "" };
    let lastDay = "";
    const nodes = [];
    for (const m of messages) {
      const day = new Date(m.ts * 1000).toDateString();
      if (day !== lastDay) {
        const sep = document.createElement("li"); sep.className = "day-sep"; sep.textContent = fmtDay(m.ts);
        nodes.push(sep); lastDay = day;
      }
      const li = document.createElement("li");
      li.className = `msg ${m.role}`;
      li.innerHTML = waFormat(m.text);
      const meta = document.createElement("span"); meta.className = "meta";
      meta.textContent = [labels[m.role], new Date(m.ts * 1000).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })]
        .filter(Boolean).join(" · ");
      li.append(meta);
      nodes.push(li);
    }
    ol.replaceChildren(...nodes);
    if (forceScroll || nearBottom) ol.scrollTop = ol.scrollHeight;
  }

  async function act(path, okMessage) {
    try { await api(path, { method: "POST" }); toast(okMessage); await refresh(); }
    catch (err) { if (err.message !== "401") toast(err.message); }
  }
  $("takeover").addEventListener("click", () => act(`/conversations/${encodeURIComponent(state.current)}/takeover`, "Has tomado el control: el bot no responderá."));
  $("release").addEventListener("click", () => act(`/conversations/${encodeURIComponent(state.current)}/release`, "Conversación devuelta al bot."));

  $("composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const box = $("composer-text");
    const text = box.value.trim();
    if (!text || !state.current) return;
    $("send").disabled = true;
    try {
      await api(`/conversations/${encodeURIComponent(state.current)}/messages`, {
        method: "POST", body: JSON.stringify({ text, agent_name: state.name }),
      });
      box.value = "";
      state.lastSignature = "";
      await loadConversation(state.current, true);
      await loadConversations();
    } catch (err) {
      if (err.message !== "401") toast(err.message);
    } finally {
      $("send").disabled = !state.currentMeta?.window_open;
    }
  });
  $("composer-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("composer").requestSubmit(); }
  });

  // --- Métricas ------------------------------------------------------------------

  $("m-days").addEventListener("change", () => loadMetrics());

  async function loadMetrics() {
    const days = Number($("m-days").value);
    $("m-days-label").textContent = days;
    state.metricsAt = Date.now();
    const { days: rows } = await api(`/metrics?days=${days}`);
    const sum = (k) => rows.reduce((a, r) => a + (r[k] || 0), 0);
    const turns = sum("turns"), handoffs = sum("handoffs"), errors = sum("errors");
    const latency = turns ? sum("latency_ms_sum") / turns : 0;
    const pct = (n) => turns ? `${fmtDec((n / turns) * 100)} % de los turnos` : "sin turnos";

    const tiles = [
      { label: "Turnos atendidos por la IA", value: fmtNum(turns), sub: `${fmtNum(turns / days)} al día de media` },
      { label: "Escaladas a humano", value: fmtNum(handoffs), sub: pct(handoffs) },
      { label: "Latencia media de la IA", value: turns ? `${fmtDec(latency / 1000)} s` : "—", sub: "objetivo < 3 s" },
      { label: "Errores", value: fmtNum(errors), sub: pct(errors) },
      { label: "Mensajes de agentes", value: fmtNum(sum("agent_messages")), sub: "enviados desde el panel" },
    ];
    $("tiles").replaceChildren(...tiles.map((t) => {
      const el = document.createElement("div"); el.className = "tile";
      el.innerHTML = `<div class="label"></div><div class="value"></div><div class="sub"></div>`;
      el.querySelector(".label").textContent = t.label;
      el.querySelector(".value").textContent = t.value;
      el.querySelector(".sub").textContent = t.sub;
      return el;
    }));

    renderChart(rows);

    const tools = {};
    rows.forEach((r) => Object.entries(r).forEach(([k, v]) => { if (k.startsWith("tool:")) tools[k.slice(5)] = (tools[k.slice(5)] || 0) + v; }));
    const toolRows = Object.entries(tools).sort((a, b) => b[1] - a[1]);
    fillTable($("tools-table"), ["Herramienta", "Turnos que la usaron"],
      toolRows.length ? toolRows.map(([k, v]) => [k, fmtNum(v)]) : [["Sin datos", ""]]);
    fillTable($("days-table"), ["Día", "Turnos", "Escaladas", "Errores", "Latencia media"],
      [...rows].reverse().map((r) => [fmtDate(r.date), fmtNum(r.turns || 0), fmtNum(r.handoffs || 0), fmtNum(r.errors || 0),
        r.turns ? `${fmtDec((r.latency_ms_sum || 0) / r.turns / 1000)} s` : "—"]));
  }

  function fillTable(table, headers, rows) {
    const thead = document.createElement("thead"), tbody = document.createElement("tbody");
    const hr = document.createElement("tr");
    headers.forEach((h, i) => { const th = document.createElement("th"); th.textContent = h; if (i) th.className = "num"; hr.append(th); });
    thead.append(hr);
    rows.forEach((r) => {
      const tr = document.createElement("tr");
      r.forEach((c, i) => { const td = document.createElement("td"); td.textContent = c; if (i) td.className = "num"; tr.append(td); });
      tbody.append(tr);
    });
    table.replaceChildren(thead, tbody);
  }

  function niceMax(v) {
    if (v <= 4) return 4;
    const p = 10 ** Math.floor(Math.log10(v)), n = v / p;
    return [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find((s) => n <= s) * p;
  }

  function renderChart(rows) {
    const W = Math.max(320, Math.round($("chart").clientWidth || 720)), H = 220, L = 40, R = 8, T = 12, B = 28;
    const max = niceMax(Math.max(...rows.map((r) => r.turns || 0), 1));
    const step = (W - L - R) / rows.length;
    const bw = Math.max(4, Math.min(28, step - 4));
    const y = (v) => T + (H - T - B) * (1 - v / max);
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Turnos atendidos por la IA por día; el detalle está en la tabla diaria");
    const mk = (tag, attrs, text) => { const el = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v)); if (text != null) el.textContent = text; return el; };

    const axis = mk("g", { class: "axis" });
    for (let i = 0; i <= 4; i++) {
      const v = (max / 4) * i, yy = y(v);
      svg.append(mk("line", { class: "gridline", x1: L, x2: W - R, y1: yy, y2: yy }));
      axis.append(mk("text", { x: L - 6, y: yy + 4, "text-anchor": "end" }, fmtNum(v)));
    }
    const every = Math.ceil(rows.length / 10);
    rows.forEach((r, i) => {
      if (i % every === 0 || i === rows.length - 1) {
        const [, m, d] = r.date.split("-");
        axis.append(mk("text", { x: L + step * i + step / 2, y: H - 8, "text-anchor": "middle" }, `${Number(d)}/${Number(m)}`));
      }
    });
    svg.append(axis);

    const tip = $("tooltip"), card = $("chart").parentElement;
    rows.forEach((r, i) => {
      const v = r.turns || 0, x = L + step * i + (step - bw) / 2, top = y(v), base = y(0), rad = Math.min(4, (base - top) / 2);
      const g = mk("g", {});
      if (v > 0) {
        g.append(mk("path", { class: "bar", d: `M${x} ${base}V${top + rad}Q${x} ${top} ${x + rad} ${top}H${x + bw - rad}Q${x + bw} ${top} ${x + bw} ${top + rad}V${base}Z` }));
      }
      const hit = mk("rect", { x: L + step * i, y: T, width: step, height: H - T - B, fill: "transparent" });
      g.append(hit);
      const show = () => {
        g.querySelector(".bar")?.classList.add("hover");
        const box = svg.getBoundingClientRect(), cb = card.getBoundingClientRect(), scale = box.width / W;
        tip.textContent = `${fmtDate(r.date)}: ${fmtNum(v)} turnos · ${fmtNum(r.handoffs || 0)} escaladas`;
        tip.style.left = `${box.left - cb.left + (x + bw / 2) * scale}px`;
        tip.style.top = `${box.top - cb.top + top * scale}px`;
        tip.hidden = false;
      };
      const hide = () => { g.querySelector(".bar")?.classList.remove("hover"); tip.hidden = true; };
      hit.addEventListener("mouseenter", show); hit.addEventListener("mouseleave", hide);
      hit.addEventListener("touchstart", show, { passive: true });
      svg.append(g);
    });
    $("chart").replaceChildren(svg);
  }

  // --- Inicio ----------------------------------------------------------------------

  if (state.token) showApp(); else logout();
})();
