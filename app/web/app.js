/* Panel del agente de WhatsApp. Sin dependencias ni build.
   Todo el texto que viene del servidor se inserta como texto (nunca como HTML),
   salvo el formato de WhatsApp, que se aplica sobre texto ya escapado. */
(() => {
  "use strict";

  const POLL_MS = 4000;
  const $ = (id) => document.getElementById(id);

  // h("div", {class: "x", onclick: fn}, "texto", otroNodo)
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "class") el.className = v;
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
    return el;
  }

  const store = {
    get(k) { try { return sessionStorage.getItem(k) || ""; } catch { return ""; } },
    set(k, v) { try { sessionStorage.setItem(k, v); } catch { /* modo privado */ } },
    clear() { try { sessionStorage.clear(); } catch { /* nada */ } },
  };

  const state = {
    token: store.get("token"), user: null, view: store.get("view") || "inbox", tab: "pendientes",
    search: "", items: [], counts: {}, current: null, meta: null, sig: "", control: null,
    demo: null, timer: null, flagOpen: null,
  };
  const can = (p) => !!state.user?.permissions?.includes(p);

  // --- API ------------------------------------------------------------------------

  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}) },
      body: options.body && typeof options.body !== "string" ? JSON.stringify(options.body) : options.body,
    });
    if (res.status === 401 && !path.startsWith("/api/auth/login")) {
      logout("Tu sesión caducó. Vuelve a entrar.");
      throw new Error("401");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
    return data;
  }
  const quiet = (err) => { if (err.message !== "401") toast(err.message); };

  // --- utilidades -------------------------------------------------------------------

  const fmtTime = (ts) => {
    const d = new Date(ts * 1000), now = new Date();
    return d.toDateString() === now.toDateString()
      ? d.toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("es", { day: "2-digit", month: "short" });
  };
  const fmtDateTime = (ts) => new Date(ts * 1000).toLocaleString("es", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
  const fmtDay = (ts) => new Date(ts * 1000).toLocaleDateString("es", { weekday: "long", day: "numeric", month: "long" });
  const fmtNum = (n) => new Intl.NumberFormat("es", { maximumFractionDigits: 1 }).format(n);
  const fmtUsd = (n) => new Intl.NumberFormat("es-EC", { style: "currency", currency: "USD" }).format(n);
  const fmtDec = (n, d = 1) => n.toLocaleString("es", { minimumFractionDigits: d, maximumFractionDigits: d });
  const escapeHtml = (s) => s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const waFormat = (s) => escapeHtml(s)
    .replace(/\*([^*\n]+)\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)_([^_\n]+)_(?=\W|$)/g, "$1<em>$2</em>")
    .replace(/~([^~\n]+)~/g, "<s>$1</s>");
  const plain = (s) => s.replace(/[*_~]([^*_~\n]+)[*_~]/g, "$1");
  const maskNumber = (wa) => (wa.length > 6 ? `+${wa.slice(0, wa.length - 6)}···${wa.slice(-3)}` : wa);
  const STATE_LABEL = { bot: "Bot", pendiente_humano: "Pendiente", asignada: "Asignada", cerrada: "Cerrada" };
  const ROLE_LABEL = { owner: "Owner", admin: "Admin", asesor: "Asesor" };
  const REASONS = { dato_incorrecto: "Dato incorrecto", alucino: "Inventó información", tono: "Tono inadecuado",
    no_derivo: "Debió derivar", derivo_sin_motivo: "Derivó sin motivo", duplicado: "Mensaje duplicado", otro: "Otro" };

  let toastTimer;
  function toast(msg) {
    const el = $("toast");
    el.textContent = msg; el.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.hidden = true; }, 4500);
  }

  // --- acceso ----------------------------------------------------------------------

  async function start() {
    try {
      state.user = await api("/api/auth/me");
    } catch { return; }
    $("login").hidden = true; $("app").hidden = false;
    $("user-label").textContent = `${state.user.name} · ${ROLE_LABEL[state.user.role] || state.user.role}`;
    buildNav();
    await loadControl();
    checkDemo();
    show(VIEWS.some((v) => v.id === state.view && v.allowed()) ? state.view : "inbox");
    clearInterval(state.timer);
    state.timer = setInterval(() => { if (!document.hidden) poll(); }, POLL_MS);
  }

  function logout(message) {
    clearInterval(state.timer);
    store.clear();
    Object.assign(state, { token: "", user: null, current: null, items: [] });
    $("app").hidden = true; $("login").hidden = false;
    $("login-error").hidden = !message; $("login-error").textContent = message || "";
    loadDemoHint();
  }

  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/auth/login", { method: "POST", body: { email: $("login-email").value.trim(), password: $("login-password").value } });
      state.token = r.token; store.set("token", r.token);
      $("login-password").value = "";
      start();
    } catch (err) { $("login-error").hidden = false; $("login-error").textContent = err.message; }
  });
  $("logout").addEventListener("click", () => logout());

  $("password-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/auth/password", { method: "POST", body: { current: $("pw-current").value, new: $("pw-new").value } });
      $("pw-current").value = ""; $("pw-new").value = "";
      toast("Contraseña actualizada.");
    } catch (err) { quiet(err); }
  });

  function loadDemoHint() {
    fetch("/demo-info").then((r) => (r.ok ? r.json() : null)).then((info) => {
      if (!info) return;
      const box = $("login-hint");
      box.replaceChildren(h("strong", {}, "Modo demo. "), "Contraseña ", h("code", {}, info.password), ". Entra como:",
        h("div", { class: "row" }, info.users.map((u) => h("button", {
          type: "button", class: "btn small", onclick: () => {
            $("login-email").value = u.email; $("login-password").value = info.password;
            $("login-form").requestSubmit();
          },
        }, `${ROLE_LABEL[u.role]} (${u.email})`))));
      box.hidden = false;
    }).catch(() => {});
  }

  // --- navegación ------------------------------------------------------------------

  const VIEWS = [
    { id: "inbox", label: "Conversaciones", allowed: () => can("conversations.view"), load: () => refreshInbox() },
    { id: "review", label: "Revisión", allowed: () => can("reviews.manage"), load: () => loadReview() },
    { id: "control", label: "Centro de control", allowed: () => can("conversations.view"), load: () => renderControl() },
    { id: "metrics", label: "Métricas", allowed: () => can("metrics.view"), load: () => loadMetrics() },
    { id: "users", label: "Usuarios", allowed: () => can("users.manage"), load: () => loadUsers() },
    { id: "audit", label: "Auditoría", allowed: () => can("audit.view"), load: () => loadAudit() },
  ];

  function buildNav() {
    $("nav").replaceChildren(...VIEWS.filter((v) => v.allowed()).map((v) =>
      h("button", { class: "tab", type: "button", dataset: { view: v.id }, onclick: () => show(v.id) }, v.label)));
  }

  function show(view) {
    state.view = view; store.set("view", view);
    document.querySelectorAll(".tab").forEach((t) => t.setAttribute("aria-current", String(t.dataset.view === view)));
    document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${view}`; });
    VIEWS.find((v) => v.id === view)?.load().catch?.(quiet);
  }

  async function poll() {
    try {
      if (state.view === "inbox") await refreshInbox();
      await loadControl();
    } catch (err) { if (err.message !== "401") console.warn(err); }
  }

  // --- interruptor del bot y Centro de control (datos) --------------------------------

  async function loadControl() {
    const r = await api("/api/admin/control");
    state.control = r;
    const on = r.control.bot_enabled;
    const sw = $("bot-switch");
    sw.className = `switch ${on ? "on" : "off"}`;
    sw.textContent = on ? "Bot activo" : "Bot apagado: todo va a asesores";
    sw.disabled = !can("control.manage");
  }

  $("bot-switch").addEventListener("click", async () => {
    const on = state.control.control.bot_enabled;
    const sw = $("bot-switch");
    if (on && sw.dataset.confirm !== "1") {
      sw.dataset.confirm = "1"; sw.textContent = "¿Apagar el bot? Pulsa otra vez";
      setTimeout(() => { delete sw.dataset.confirm; loadControl().catch(() => {}); }, 4000);
      return;
    }
    delete sw.dataset.confirm;
    try {
      await api("/api/admin/control/bot", { method: "POST", body: { enabled: !on } });
      toast(on ? "Bot apagado: los mensajes nuevos van a la pestaña Pendientes." : "Bot encendido.");
      await loadControl();
    } catch (err) { quiet(err); }
  });

  // --- modo demo -------------------------------------------------------------------------

  async function checkDemo() {
    try {
      const info = await api("/api/admin/demo");
      state.demo = info;
      $("demo-ai").textContent = info.ai;
      $("demo-customer").replaceChildren(...info.customers.map((c) => h("option", { value: c.wa_id }, `${c.name} · ${c.hint}`)));
      $("demo-receipt").hidden = !info.ai.startsWith("reglas");
      $("demo-bar").hidden = false;
    } catch { $("demo-bar").hidden = true; }
  }

  async function demoSend(body) {
    const wa = $("demo-customer").value;
    try {
      await api("/api/admin/demo/incoming", { method: "POST", body: { wa_id: wa, ...body } });
      if (state.view !== "inbox") show("inbox");
      state.tab = "todas"; markTabs();
      if (state.current !== wa) {
        // La conversación se crea cuando el servidor procesa el mensaje (tras agrupar la ráfaga).
        for (let i = 0; i < 15; i++) {
          await new Promise((r) => setTimeout(r, 400));
          try { await api(`/api/admin/conversations/${encodeURIComponent(wa)}`); break; } catch { /* aún no existe */ }
        }
        await openConversation(wa);
      }
      for (const ms of [700, 1600, 3000, 5000]) setTimeout(() => refreshInbox().catch(() => {}), ms);
    } catch (err) { quiet(err); }
  }

  $("demo-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("demo-text").value.trim();
    if (!text) return;
    $("demo-text").value = "";
    demoSend({ text });
  });
  $("demo-receipt").addEventListener("click", () => {
    const today = new Date().toLocaleDateString("en-CA", { timeZone: state.control?.control.timezone || "America/Guayaquil" });
    demoSend({ text: "", receipt: { es_comprobante: true, banco: "Banco Pichincha", numero: String(Date.now()).slice(-6),
      monto: 50, fecha: today, cuenta_destino: "2200123456", titular_destino: "PAALNET S.A.", confianza: 0.95 } });
  });
  $("demo-file").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > 8 * 1024 * 1024) { toast("El archivo supera 8 MB."); return; }
    const reader = new FileReader();
    reader.onload = () => demoSend({ text: "", media_b64: String(reader.result).split(",")[1], mime_type: file.type || "image/jpeg" });
    reader.readAsDataURL(file);
  });

  // --- bandeja -------------------------------------------------------------------------------

  function markTabs() {
    document.querySelectorAll(".chip[data-tab]").forEach((c) => c.setAttribute("aria-pressed", String(c.dataset.tab === state.tab)));
  }
  document.querySelectorAll(".chip[data-tab]").forEach((chip) => chip.addEventListener("click", () => {
    state.tab = chip.dataset.tab; markTabs(); refreshInbox().catch(quiet);
  }));
  $("search").addEventListener("input", (e) => { state.search = e.target.value; renderList(); });

  async function refreshInbox() {
    markTabs();
    const r = await api(`/api/admin/conversations?tab=${state.tab}`);
    state.items = r.items; state.counts = r.counts;
    renderList();
    if (state.current) await loadConversation(state.current, false);
  }

  function renderList() {
    document.querySelectorAll("[data-count]").forEach((el) => {
      const n = state.counts[el.dataset.count];
      el.textContent = n ? `(${n})` : "";
    });
    const pend = state.counts.pendientes || 0;
    document.title = pend ? `(${pend}) Panel del agente` : "Panel del agente";
    const q = state.search.toLowerCase();
    const items = state.items.filter((c) => !q || `${c.name} ${c.wa_id} ${c.last_text}`.toLowerCase().includes(q));
    $("conv-list").replaceChildren(...items.map((c) => {
      const waiting = c.state === "pendiente_humano" || (c.state === "asignada" && c.last_role === "user");
      const open = () => openConversation(c.wa_id);
      const who = { user: "", bot: "Bot: ", agent: "Asesor: ", system: "· " }[c.last_role] || "";
      return h("li", {
        class: `conv${waiting ? " attention" : ""}`, tabindex: 0, role: "button",
        "aria-current": c.wa_id === state.current ? "true" : null, onclick: open,
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } },
      },
      h("span", { class: "name" }, c.name || maskNumber(c.wa_id)),
      h("span", { class: "time" }, c.last_ts ? fmtTime(c.last_ts) : ""),
      h("span", { class: "snippet" }, who + plain(c.last_text)),
      h("span", { class: "tags" },
        c.area ? h("span", { class: "tag" }, c.area) : null,
        c.priority === "alta" && c.state !== "bot" && c.state !== "cerrada" ? h("span", { class: "tag hot" }, "alta") : null,
        h("span", { class: `badge ${c.state}` }, c.state === "asignada" && c.assignee ? c.assignee : STATE_LABEL[c.state])));
    }));
    $("list-empty").hidden = items.length > 0;
  }

  async function openConversation(wa) {
    state.current = wa; state.sig = ""; state.flagOpen = null;
    $("view-inbox").classList.add("show-chat");
    renderList();
    await loadConversation(wa, true);
  }

  $("back").addEventListener("click", () => {
    $("view-inbox").classList.remove("show-chat");
    state.current = null; renderList();
  });

  async function loadConversation(wa, forceScroll) {
    let data;
    try { data = await api(`/api/admin/conversations/${encodeURIComponent(wa)}`); }
    catch (err) {
      if (err.message !== "401") { toast(err.message); state.current = null; $("chat").hidden = true; $("chat-empty").hidden = false; }
      return;
    }
    if (state.current !== wa) return;
    state.meta = data.meta;
    renderHeader(data.meta);
    const last = data.messages.at(-1);
    const sig = `${data.messages.length}:${last?.ts || 0}:${data.messages.filter((m) => m.flagged).length}`;
    if (sig !== state.sig && !state.flagOpen) { renderMessages(data.messages, forceScroll); state.sig = sig; }
    $("chat-empty").hidden = true; $("chat").hidden = false;
  }

  function renderHeader(m) {
    $("chat-name").textContent = m.name || "Sin nombre";
    $("chat-sub").textContent = [`+${m.wa_id}`, m.area && `área ${m.area}`, m.opted_out && "sin avisos (STOP)"].filter(Boolean).join(" · ");
    const st = $("chat-state");
    st.className = `badge ${m.state}`;
    st.textContent = m.state === "asignada" ? `Asignada a ${m.assignee || "un asesor"}` : STATE_LABEL[m.state];
    const act = can("conversations.act");
    const mine = m.state === "asignada" && m.assigned_to === state.user.id;
    $("act-take").hidden = !act || mine;
    $("act-take").textContent = m.state === "asignada" ? "Tomar (reasignar a mí)" : "Tomar";
    $("act-release").hidden = !act || !["pendiente_humano", "asignada"].includes(m.state);
    $("act-close").hidden = !act || m.state === "cerrada";
    const summary = $("chat-summary");
    summary.hidden = !(m.handoff_summary && ["pendiente_humano", "asignada"].includes(m.state));
    summary.textContent = `Motivo de la derivación: ${m.handoff_summary}`;

    const win = $("chat-window");
    if (m.window_open) {
      const hours = Math.max(0, (m.window_closes_at - Date.now() / 1000) / 3600);
      win.textContent = `Ventana 24 h: quedan ${hours >= 1 ? Math.floor(hours) + " h" : Math.ceil(hours * 60) + " min"}`;
      win.className = "small muted";
    } else { win.textContent = "Ventana 24 h cerrada"; win.className = "small error"; }

    const note = $("composer-note");
    $("free-text").hidden = !m.window_open || !act;
    $("template-box").hidden = m.window_open || !act;
    if (!act) { note.hidden = false; note.textContent = "Tu rol solo permite ver conversaciones."; }
    else if (!m.window_open) { note.hidden = false; note.textContent = "La ventana de 24 h está cerrada: WhatsApp solo permite escribir con una plantilla aprobada."; renderTemplates(); }
    else if (m.state === "bot") { note.hidden = false; note.textContent = "Al enviar un mensaje tomarás la conversación y el bot dejará de responder."; }
    else note.hidden = true;
  }

  function renderMessages(messages, forceScroll) {
    const ol = $("messages");
    const nearBottom = ol.scrollHeight - ol.scrollTop - ol.clientHeight < 80;
    const labels = { user: "Cliente", bot: "Bot", agent: "Asesor", system: "" };
    let lastDay = "";
    const nodes = [];
    for (const m of messages) {
      const day = new Date(m.ts * 1000).toDateString();
      if (day !== lastDay) { nodes.push(h("li", { class: "day-sep" }, fmtDay(m.ts))); lastDay = day; }
      const body = h("div", { class: "body" });
      body.innerHTML = waFormat(m.text); // texto escapado antes de aplicar el formato
      const meta = h("span", { class: "meta" },
        [m.role === "agent" && m.author ? `${labels.agent} ${m.author}` : labels[m.role],
          new Date(m.ts * 1000).toLocaleTimeString("es", { hour: "2-digit", minute: "2-digit" }),
          m.role === "bot" && m.prompt_version].filter(Boolean).join(" · "));
      const li = h("li", { class: `msg ${m.role}${m.flagged ? " flagged" : ""}` }, body, meta);
      if (m.role === "bot") {
        if (m.flagged) li.append(h("span", { class: "flag-label" }, "Marcada como mala"));
        else if (can("reviews.flag")) meta.append(h("button", { class: "linkbtn", type: "button", onclick: () => openFlag(li, m) }, "Marcar como mala"));
      }
      nodes.push(li);
    }
    ol.replaceChildren(...nodes);
    if (forceScroll || nearBottom) ol.scrollTop = ol.scrollHeight;
  }

  function openFlag(li, m) {
    if (li.querySelector(".flag-form")) return;
    state.flagOpen = m.id;
    const reason = h("select", { "aria-label": "Motivo" }, Object.entries(REASONS).map(([k, v]) => h("option", { value: k }, v)));
    const note = h("textarea", { rows: 2, maxlength: 1000, placeholder: "¿Qué debió responder o hacer?" });
    const close = () => { form.remove(); state.flagOpen = null; state.sig = ""; };
    const form = h("form", {
      class: "flag-form", onsubmit: async (e) => {
        e.preventDefault();
        try {
          await api(`/api/admin/messages/${m.id}/flag`, { method: "POST", body: { reason: reason.value, note: note.value.trim() } });
          toast("Reporte enviado a Revisión.");
          close(); await loadConversation(state.current, false);
        } catch (err) { quiet(err); }
      },
    }, reason, note, h("div", { class: "row" },
      h("button", { class: "btn small primary", type: "submit" }, "Enviar a Revisión"),
      h("button", { class: "btn small ghost", type: "button", onclick: close }, "Cancelar")));
    li.append(form);
    note.focus();
  }

  async function act(action, okMessage) {
    try {
      await api(`/api/admin/conversations/${encodeURIComponent(state.current)}/${action}`, { method: "POST" });
      toast(okMessage); state.sig = ""; await refreshInbox();
    } catch (err) { quiet(err); }
  }
  $("act-take").addEventListener("click", () => act("take", "Conversación asignada a ti. El bot no responderá."));
  $("act-release").addEventListener("click", () => act("release", "Conversación devuelta al bot."));
  $("act-close").addEventListener("click", () => act("close", "Conversación cerrada. Si el cliente vuelve a escribir, la atiende el bot."));

  $("composer").addEventListener("submit", async (e) => {
    e.preventDefault();
    const box = $("composer-text"), text = box.value.trim();
    if (!text || !state.current) return;
    $("send").disabled = true;
    try {
      await api(`/api/admin/conversations/${encodeURIComponent(state.current)}/messages`, { method: "POST", body: { text } });
      box.value = ""; state.sig = ""; await refreshInbox();
    } catch (err) { quiet(err); } finally { $("send").disabled = false; }
  });
  $("composer-text").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("composer").requestSubmit(); }
  });

  // Plantillas (ventana cerrada)
  function renderTemplates() {
    const templates = state.control?.control.templates || [];
    const sel = $("template-name");
    if (sel.options.length !== templates.length) {
      sel.replaceChildren(...templates.map((t) => h("option", { value: t.name }, `${t.name} (${t.category})`)));
      renderTemplateParams();
    }
  }
  function currentTemplate() { return (state.control?.control.templates || []).find((t) => t.name === $("template-name").value); }
  function renderTemplateParams() {
    const t = currentTemplate();
    if (!t) return;
    $("template-params").replaceChildren(...Array.from({ length: t.params }, (_, i) =>
      h("input", { type: "text", placeholder: `Valor {{${i + 1}}}`, "aria-label": `Valor ${i + 1}`, oninput: previewTemplate,
        value: i === 0 && state.meta?.name ? state.meta.name.split(" ")[0] : "" })));
    previewTemplate();
  }
  function previewTemplate() {
    const t = currentTemplate();
    if (!t) return;
    let text = t.body;
    [...$("template-params").querySelectorAll("input")].forEach((inp, i) => { text = text.replace(`{{${i + 1}}}`, inp.value || `{{${i + 1}}}`); });
    $("template-preview").textContent = text;
  }
  $("template-name").addEventListener("change", renderTemplateParams);
  $("template-send").addEventListener("click", async () => {
    const params = [...$("template-params").querySelectorAll("input")].map((i) => i.value.trim());
    if (params.some((p) => !p)) { toast("Completa todos los valores de la plantilla."); return; }
    try {
      await api(`/api/admin/conversations/${encodeURIComponent(state.current)}/template`, { method: "POST", body: { name: $("template-name").value, params } });
      toast("Plantilla enviada."); state.sig = ""; await refreshInbox();
    } catch (err) { quiet(err); }
  });

  // --- Revisión -------------------------------------------------------------------------------

  let reviewStatus = "abierto";
  async function loadReview() {
    const [reviews, rules] = await Promise.all([api(`/api/admin/reviews?status=${reviewStatus}`), api("/api/admin/rules")]);
    const areas = rules.areas;
    const areaSelect = (value) => h("select", { "aria-label": "Área" }, areas.map((a) => h("option", { value: a, selected: a === value }, a)));
    const statusChips = h("div", { class: "chips" }, [["abierto", "Abiertos"], ["resuelto", "Resueltos"], ["descartado", "Descartados"]].map(([k, v]) =>
      h("button", { class: "chip", "aria-pressed": String(k === reviewStatus), onclick: () => { reviewStatus = k; loadReview().catch(quiet); } }, v)));

    const cards = reviews.items.map((r) => {
      const text = h("textarea", { rows: 2, maxlength: 600, placeholder: "Regla para el prompt: concreta, verificable y con ejemplo." });
      const area = areaSelect(r.area || "todas");
      return h("article", { class: "card review" },
        h("div", { class: "review-head" }, h("span", { class: "tag hot" }, REASONS[r.reason] || r.reason),
          h("span", { class: "muted small" }, `${r.area || "sin área"} · ${r.prompt_version || ""} · ${r.reported_by} · ${fmtDateTime(r.created_at)}`)),
        h("p", {}, h("span", { class: "who-label" }, "Cliente: "), r.customer_text || "—"),
        h("p", {}, h("span", { class: "who-label" }, "Bot: "), r.bot_text),
        r.note ? h("p", { class: "note-ok" }, h("span", { class: "who-label" }, "Debió: "), r.note) : null,
        r.status === "abierto" && h("div", { class: "stack" }, text, h("div", { class: "row" }, area,
          h("button", { class: "btn small", type: "button", onclick: async (e) => {
            e.target.disabled = true;
            try { const s = await api(`/api/admin/reviews/${r.id}/suggest`, { method: "POST" }); text.value = s.text; area.value = s.area; }
            catch (err) { quiet(err); } finally { e.target.disabled = false; }
          } }, "Proponer regla con IA"),
          h("button", { class: "btn small primary", type: "button", onclick: async () => {
            if (text.value.trim().length < 5) { toast("Escribe la regla primero."); return; }
            try { await api(`/api/admin/reviews/${r.id}/rule`, { method: "POST", body: { area: area.value, text: text.value.trim() } }); toast("Regla creada (inactiva). Actívala abajo cuando pase las evaluaciones."); loadReview(); }
            catch (err) { quiet(err); }
          } }, "Crear regla"),
          h("button", { class: "btn small ghost", type: "button", onclick: async () => {
            try { await api(`/api/admin/reviews/${r.id}/dismiss`, { method: "POST" }); loadReview(); } catch (err) { quiet(err); }
          } }, "Descartar"))));
    });

    const newText = h("textarea", { rows: 2, maxlength: 600, placeholder: "Nueva regla manual" });
    const newArea = areaSelect("todas");
    const ruleRows = rules.items.map((r) => h("tr", {},
      h("td", {}, h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: r.active, onchange: async (e) => {
        try { await api(`/api/admin/rules/${r.id}`, { method: "PATCH", body: { active: e.target.checked } }); toast(e.target.checked ? "Regla activa: ya forma parte del prompt." : "Regla desactivada."); loadReview().catch(quiet); }
        catch (err) { e.target.checked = !e.target.checked; quiet(err); }
      } }), r.active ? "Activa" : "Inactiva")),
      h("td", {}, r.area), h("td", { class: "wrap" }, r.text), h("td", { class: "muted small" }, `${r.created_by}, ${fmtDateTime(r.created_at)}`)));

    $("view-review").replaceChildren(
      h("div", { class: "page-head" }, h("h2", {}, "Revisión"), statusChips),
      h("p", { class: "muted" }, "Respuestas del bot marcadas como malas por los asesores. Convierte cada una en una regla; las reglas activas se añaden al prompt de su área."),
      cards.length ? h("div", { class: "cards" }, cards) : h("p", { class: "empty" }, "No hay reportes en este estado."),
      h("section", { class: "card" }, h("h3", {}, "Reglas aprendidas"),
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Estado", "Área", "Regla", "Creada por"].map((t) => h("th", {}, t)))), h("tbody", {}, ruleRows))),
        h("div", { class: "stack" }, newText, h("div", { class: "row" }, newArea, h("button", { class: "btn small primary", type: "button", onclick: async () => {
          if (newText.value.trim().length < 5) return;
          try { await api("/api/admin/rules", { method: "POST", body: { area: newArea.value, text: newText.value.trim() } }); loadReview(); } catch (err) { quiet(err); }
        } }, "Añadir regla")))));
  }

  // --- Centro de control ----------------------------------------------------------------------

  async function renderControl() {
    await loadControl();
    const cc = structuredClone(state.control.control);
    const edit = can("control.manage");
    const num = (obj, key, attrs = {}) => h("input", { type: "number", value: obj[key], disabled: !edit, ...attrs, oninput: (e) => { obj[key] = Number(e.target.value); } });
    const txt = (obj, key, attrs = {}) => h("input", { type: "text", value: obj[key] ?? "", disabled: !edit, ...attrs, oninput: (e) => { obj[key] = e.target.value; } });
    const sel = (obj, key, options) => h("select", { disabled: !edit, onchange: (e) => { obj[key] = isNaN(+e.target.value) ? e.target.value : +e.target.value; } },
      options.map(([v, l]) => h("option", { value: v, selected: String(obj[key]) === String(v) }, l)));
    const field = (label, input, help) => h("label", { class: "field" }, h("span", {}, label), input, help ? h("small", { class: "muted" }, help) : null);
    const section = (title, ...children) => h("section", { class: "card" }, h("h3", {}, title), ...children);
    const listEditor = (items, columns, blank) => {
      const body = h("tbody");
      const render = () => body.replaceChildren(...items.map((it, i) => h("tr", {}, columns.map(([key, label, type]) => h("td", {},
        type === "number" ? num(it, key, { "aria-label": label, step: "any" }) : txt(it, key, { "aria-label": label }))),
        edit ? h("td", {}, h("button", { class: "btn small ghost", type: "button", onclick: () => { items.splice(i, 1); render(); } }, "Quitar")) : null)));
      render();
      return h("div", {}, h("div", { class: "table-wrap" }, h("table", { class: "table form-table" },
        h("thead", {}, h("tr", {}, columns.map(([, label]) => h("th", {}, label)), edit ? h("th", {}) : null)), body)),
        edit ? h("button", { class: "btn small", type: "button", onclick: () => { items.push(structuredClone(blank)); render(); } }, "Añadir") : null);
    };

    const areaRows = Object.entries(cc.areas).map(([name, cfg]) => h("tr", {},
      h("td", {}, h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: cfg.enabled, disabled: !edit, onchange: (e) => { cfg.enabled = e.target.checked; } }), name)),
      h("td", {}, num(cfg, "temperature", { step: "0.1", min: 0, max: 1, "aria-label": `Temperatura ${name}` }))));
    const DAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"];
    const hourRows = DAYS.map((d) => {
      const closed = cc.hours[d] == null;
      const open = h("input", { type: "time", value: cc.hours[d]?.open || "08:00", disabled: !edit || closed, onchange: (e) => { cc.hours[d].open = e.target.value; } });
      const close = h("input", { type: "time", value: cc.hours[d]?.close || "18:00", disabled: !edit || closed, onchange: (e) => { cc.hours[d].close = e.target.value; } });
      return h("tr", {}, h("td", {}, d), h("td", {}, open), h("td", {}, close),
        h("td", {}, h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: closed, disabled: !edit, onchange: (e) => {
          cc.hours[d] = e.target.checked ? null : { open: open.value, close: close.value }; open.disabled = close.disabled = e.target.checked;
        } }), "Cerrado")));
    });
    const holidays = h("textarea", { rows: 4, disabled: !edit, oninput: (e) => { cc.holidays = e.target.value.split(/\s+/).filter(Boolean); } }, cc.holidays.join("\n"));

    const save = h("button", { class: "btn primary", type: "button", onclick: async () => {
      try { await api("/api/admin/control", { method: "PUT", body: cc }); toast("Centro de control guardado."); await renderControl(); }
      catch (err) { quiet(err); }
    } }, "Guardar cambios");

    $("view-control").replaceChildren(
      h("div", { class: "page-head" }, h("h2", {}, "Centro de control"),
        h("span", { class: `badge ${state.control.open_now ? "bot" : "cerrada"}` }, state.control.open_now ? "Atención humana abierta ahora" : "Fuera de horario"),
        edit ? save : h("span", { class: "muted small" }, "Solo lectura: tu rol no puede editar.")),
      h("p", { class: "muted" }, "Reglas del negocio que el código hace cumplir: el modelo no puede saltárselas."),
      h("div", { class: "grid" },
        section("Áreas del bot", h("p", { class: "muted small" }, "Un área apagada deriva sus mensajes a asesores. La temperatura baja da respuestas más predecibles."),
          h("table", { class: "table form-table" }, h("thead", {}, h("tr", {}, h("th", {}, "Área"), h("th", {}, "Temperatura"))), h("tbody", {}, areaRows))),
        section("Cobros",
          field("Meses que cobra el bot", sel(cc, "months_collectable", [[0, "Toda la deuda"], [1, "Solo el mes actual"], [2, "Últimos 2 meses"], [3, "Últimos 3 meses"], [6, "Últimos 6 meses"]])),
          field("Si el monto del comprobante no coincide", sel(cc, "payment_mismatch", [["asesor", "Pasar a un asesor"], ["registrar", "Registrar igual"]])),
          field("Antigüedad máxima del comprobante (días)", num(cc, "receipt_max_age_days", { min: 1, max: 365 })),
          field("Prórroga que el bot aprueba solo (días)", num(cc, "extension_max_days", { min: 0, max: 30 }))),
        section("Soporte",
          field("Minutos entre reinicios de una ONT", num(cc, "reboot_cooldown_minutes", { min: 1, max: 240 })),
          field("Potencia óptica baja (dBm)", num(cc, "low_power_dbm", { step: "0.1" }), "Por debajo de este valor el bot propone visita técnica.")),
        section("Empresa",
          field("Nombre", txt(cc, "company")),
          field("Enlace a la política de privacidad", txt(cc, "privacy_url")),
          field("Zona horaria", txt(cc, "timezone")))),
      section("Cuentas para pagos", h("p", { class: "muted small" }, "Un comprobante solo es válido si la cuenta de destino está en esta lista."),
        listEditor(cc.accounts, [["bank", "Banco"], ["number", "Número"], ["holder", "Titular"], ["kind", "Tipo"]], { bank: "", number: "", holder: "", kind: "corriente" })),
      section("Horario de atención humana",
        h("table", { class: "table form-table" }, h("thead", {}, h("tr", {}, ["Día", "Abre", "Cierra", ""].map((t) => h("th", {}, t)))), h("tbody", {}, hourRows)),
        field("Feriados (AAAA-MM-DD, uno por línea)", holidays, "Verifica cada año el calendario oficial de feriados de Ecuador.")),
      section("Planes", listEditor(cc.plans, [["name", "Nombre"], ["mbps", "Mbps", "number"], ["price_usd", "Precio USD", "number"], ["description", "Descripción"]], { name: "", mbps: 100, price_usd: 0, description: "" })),
      section("Plantillas aprobadas en Meta", h("p", { class: "muted small" }, "Deben coincidir con las aprobadas en WhatsApp Manager. Usa {{1}}, {{2}}… para los valores."),
        listEditor(cc.templates, [["name", "Nombre"], ["language", "Idioma"], ["category", "Categoría"], ["body", "Texto"], ["params", "N.º valores", "number"]], { name: "", language: "es", category: "utility", body: "", params: 0 })),
      edit ? h("div", { class: "page-foot" }, save.cloneNode(true)) : null);
    $("view-control").querySelector(".page-foot .btn")?.addEventListener("click", () => save.click());
  }

  // --- Métricas --------------------------------------------------------------------------------

  async function loadMetrics() {
    const days = Number(store.get("mdays") || 14);
    const r = await api(`/api/admin/metrics?days=${days}`);
    const rows = r.days;
    const sum = (k) => rows.reduce((a, x) => a + (x[k] || 0), 0);
    const turns = sum("turns"), handoffs = sum("handoffs");
    const pct = (n) => (turns ? `${fmtDec((n / turns) * 100)} % de los turnos` : "sin turnos");
    const tiles = [
      ["Turnos atendidos por la IA", fmtNum(turns), `${fmtNum(turns / days)} al día`],
      ["Derivados a asesores", fmtNum(handoffs), pct(handoffs)],
      ["Pagos registrados por el bot", fmtNum(r.payments.count), fmtUsd(r.payments.usd)],
      ["Latencia media de la IA", turns ? `${fmtDec(sum("latency_ms_sum") / turns / 1000)} s` : "—", "objetivo < 3 s"],
      ["Errores", fmtNum(sum("errors") + sum("send_failed")), pct(sum("errors"))],
      ["Respuestas marcadas como malas", fmtNum(sum("flagged")), turns ? `${fmtDec((sum("flagged") / turns) * 100, 2)} por cada 100` : ""],
      ["Duplicados bloqueados", fmtNum(sum("duplicate_blocked")), "guardarraíl de salida"],
      ["Pendientes > 30 min", fmtNum(r.pending_over_30min), "ahora mismo"],
    ];
    const agg = (prefix) => {
      const out = {};
      rows.forEach((x) => Object.entries(x).forEach(([k, v]) => { if (k.startsWith(prefix)) out[k.slice(prefix.length)] = (out[k.slice(prefix.length)] || 0) + v; }));
      return Object.entries(out).sort((a, b) => b[1] - a[1]);
    };
    const table = (headers, data) => h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", {}, h("tr", {}, headers.map((t, i) => h("th", { class: i ? "num" : null }, t)))),
      h("tbody", {}, data.length ? data.map((r2) => h("tr", {}, r2.map((c, i) => h("td", { class: i ? "num" : null }, c)))) : h("tr", {}, h("td", { colspan: headers.length, class: "muted" }, "Sin datos")))));
    const daySel = h("select", { "aria-label": "Periodo", onchange: (e) => { store.set("mdays", e.target.value); loadMetrics().catch(quiet); } },
      [7, 14, 30].map((d) => h("option", { value: d, selected: d === days }, `${d} días`)));
    const chart = h("div", { class: "chart" });
    $("view-metrics").replaceChildren(
      h("div", { class: "page-head" }, h("h2", {}, `Últimos ${days} días`), daySel),
      h("div", { class: "tiles" }, tiles.map(([l, v, s]) => h("div", { class: "tile" }, h("div", { class: "label" }, l), h("div", { class: "value" }, v), h("div", { class: "sub" }, s)))),
      h("section", { class: "card" }, h("h3", {}, "Turnos atendidos por la IA, por día"), chart, h("div", { id: "tooltip", class: "tooltip", hidden: true })),
      h("div", { class: "grid" },
        h("section", { class: "card" }, h("h3", {}, "Turnos por área"), table(["Área", "Turnos"], agg("area:").map(([k, v]) => [k, fmtNum(v)]))),
        h("section", { class: "card" }, h("h3", {}, "Uso de herramientas"), table(["Herramienta", "Turnos"], agg("tool:").map(([k, v]) => [k, fmtNum(v)]))),
        h("section", { class: "card" }, h("h3", {}, "Errores de herramientas"), table(["Herramienta", "Errores"], agg("tool_error:").map(([k, v]) => [k, fmtNum(v)]))),
        h("section", { class: "card" }, h("h3", {}, "Mensajes cobrados por Meta"), table(["Categoría", "Mensajes"], agg("wa_billable:").map(([k, v]) => [k, fmtNum(v)])))),
      h("section", { class: "card" }, h("h3", {}, "Detalle diario"), table(["Día", "Turnos", "Derivados", "Pagos", "Errores", "Latencia media"],
        [...rows].reverse().map((x) => [x.date, fmtNum(x.turns || 0), fmtNum(x.handoffs || 0), fmtUsd((x.payments_cents || 0) / 100), fmtNum(x.errors || 0),
          x.turns ? `${fmtDec((x.latency_ms_sum || 0) / x.turns / 1000)} s` : "—"]))));
    renderChart(chart, rows);
  }

  function renderChart(container, rows) {
    const W = Math.max(320, Math.round(container.clientWidth || 720)), H = 220, L = 40, R = 8, T = 12, B = 28;
    // Paso "redondo" (1, 2, 5, 10… × 10^n) para que las 4 marcas del eje sean números enteros legibles.
    const rawStep = Math.max(...rows.map((r) => r.turns || 0), 4) / 4;
    const p10 = 10 ** Math.floor(Math.log10(rawStep));
    const max = [1, 2, 2.5, 5, 10].find((s) => rawStep / p10 <= s) * p10 * 4;
    const step = (W - L - R) / rows.length, bw = Math.max(4, Math.min(28, step - 4));
    const y = (v) => T + (H - T - B) * (1 - v / max);
    const ns = "http://www.w3.org/2000/svg";
    const mk = (tag, attrs, text) => { const el = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v)); if (text != null) el.textContent = text; return el; };
    const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Turnos por día; el detalle está en la tabla diaria" });
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
        axis.append(mk("text", { x: L + step * i + step / 2, y: H - 8, "text-anchor": "middle" }, `${+d}/${+m}`));
      }
    });
    svg.append(axis);
    const tip = $("tooltip"), card = container.parentElement;
    rows.forEach((r, i) => {
      const v = r.turns || 0, x = L + step * i + (step - bw) / 2, top = y(v), base = y(0), rad = Math.min(4, (base - top) / 2);
      const g = mk("g", {});
      if (v > 0) g.append(mk("path", { class: "bar", d: `M${x} ${base}V${top + rad}Q${x} ${top} ${x + rad} ${top}H${x + bw - rad}Q${x + bw} ${top} ${x + bw} ${top + rad}V${base}Z` }));
      const hit = mk("rect", { x: L + step * i, y: T, width: step, height: H - T - B, fill: "transparent" });
      g.append(hit);
      const showTip = () => {
        g.querySelector(".bar")?.classList.add("hover");
        const box = svg.getBoundingClientRect(), cb = card.getBoundingClientRect(), s = box.width / W;
        tip.textContent = `${r.date}: ${fmtNum(v)} turnos · ${fmtNum(r.handoffs || 0)} derivados`;
        tip.style.left = `${box.left - cb.left + (x + bw / 2) * s}px`; tip.style.top = `${box.top - cb.top + top * s}px`; tip.hidden = false;
      };
      hit.addEventListener("mouseenter", showTip);
      hit.addEventListener("mouseleave", () => { g.querySelector(".bar")?.classList.remove("hover"); tip.hidden = true; });
      hit.addEventListener("touchstart", showTip, { passive: true });
      svg.append(g);
    });
    container.replaceChildren(svg);
  }

  // --- Usuarios --------------------------------------------------------------------------------

  async function loadUsers() {
    const r = await api("/api/admin/users");
    const roleSel = (value, onchange) => h("select", { onchange }, ["asesor", "admin", "owner"].map((x) => h("option", { value: x, selected: x === value }, ROLE_LABEL[x])));
    const patch = async (id, body, ok) => { try { await api(`/api/admin/users/${id}`, { method: "PATCH", body }); toast(ok); } catch (err) { quiet(err); } loadUsers(); };
    const rows = r.items.map((u) => {
      const pw = h("input", { type: "password", placeholder: "Nueva contraseña", autocomplete: "new-password" });
      return h("tr", {}, h("td", {}, u.name), h("td", {}, u.email),
        h("td", {}, roleSel(u.role, (e) => patch(u.id, { role: e.target.value }, "Rol actualizado."))),
        h("td", {}, h("label", { class: "toggle" }, h("input", { type: "checkbox", checked: u.active, onchange: (e) => patch(u.id, { active: e.target.checked }, e.target.checked ? "Usuario activado." : "Usuario desactivado.") }), u.active ? "Activo" : "Inactivo")),
        h("td", {}, h("div", { class: "row" }, pw, h("button", { class: "btn small", type: "button", onclick: () => pw.value && patch(u.id, { password: pw.value }, "Contraseña restablecida.") }, "Restablecer"))));
    });
    const f = { email: h("input", { type: "email", placeholder: "correo@empresa.ec", required: true }), name: h("input", { type: "text", placeholder: "Nombre", required: true }),
      role: roleSel("asesor"), password: h("input", { type: "password", placeholder: "Contraseña (mín. 10)", autocomplete: "new-password", required: true }) };
    $("view-users").replaceChildren(
      h("div", { class: "page-head" }, h("h2", {}, "Usuarios")),
      h("p", { class: "muted" }, "Asesor: atiende conversaciones. Admin: además Revisión, Centro de control y auditoría. Owner: además gestiona usuarios."),
      h("section", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Nombre", "Correo", "Rol", "Estado", "Contraseña"].map((t) => h("th", {}, t)))), h("tbody", {}, rows)))),
      h("section", { class: "card" }, h("h3", {}, "Nuevo usuario"), h("form", { class: "row wrap", onsubmit: async (e) => {
        e.preventDefault();
        try { await api("/api/admin/users", { method: "POST", body: { email: f.email.value, name: f.name.value, role: f.role.value, password: f.password.value } }); toast("Usuario creado."); loadUsers(); }
        catch (err) { quiet(err); }
      } }, f.name, f.email, f.role, f.password, h("button", { class: "btn primary small", type: "submit" }, "Crear"))));
  }

  // --- Auditoría -------------------------------------------------------------------------------

  async function loadAudit() {
    const r = await api("/api/admin/audit?limit=300");
    $("view-audit").replaceChildren(
      h("div", { class: "page-head" }, h("h2", {}, "Auditoría")),
      h("p", { class: "muted" }, "Acciones de asesores, cambios de configuración y operaciones del bot con efecto (pagos, reinicios, tickets)."),
      h("section", { class: "card" }, h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", {}, h("tr", {}, ["Fecha", "Quién", "Acción", "Objeto", "Detalle"].map((t) => h("th", {}, t)))),
        h("tbody", {}, r.items.map((a) => h("tr", {}, h("td", { class: "nowrap" }, fmtDateTime(a.ts)), h("td", {}, a.actor), h("td", {}, h("code", {}, a.action)),
          h("td", {}, a.target), h("td", { class: "wrap muted small" }, Object.keys(a.detail).length ? JSON.stringify(a.detail) : ""))))))));
  }

  // --- inicio ----------------------------------------------------------------------------------

  if (state.token) start(); else logout();
})();
