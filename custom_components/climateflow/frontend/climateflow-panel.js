/**
 * Panou ClimateFlow — web component vanilla (fără build step).
 * Tab-uri: Acasă (hero live + agenda 7 zile), Program (grilă săptămânală + evenimente
 * unice), Învățare (rate pe benzi + istoric), Setări, Ajutor.
 * Citește/scrie prin comenzile websocket climateflow/get|save_events|save_general|
 * toggle_skip|skip_next|boost_now|stop|history.
 */

const TABS = [
  ["acasa", "Acasă"],
  ["program", "Program"],
  ["invatare", "Învățare"],
  ["setari", "Setări"],
  ["ajutor", "Ajutor"],
];

const DAY_NAMES = ["Luni", "Marți", "Miercuri", "Joi", "Vineri", "Sâmbătă", "Duminică"];
const DAY_SHORT = ["Lun", "Mar", "Mie", "Joi", "Vin", "Sâm", "Dum"];

const STATE_META = {
  idle: { label: "În așteptare", cls: "s-idle", icon: "🕐" },
  preconditioning: { label: "Precondiționare", cls: "s-pre", icon: "🌡️" },
  event: { label: "Eveniment în curs", cls: "s-event", icon: "👥" },
  cooldown: { label: "Se oprește…", cls: "s-cool", icon: "⏹" },
};

const WF_META = {
  always: { label: "În fiecare săptămână", short: "" },
  except_transition: { label: "Exceptând săptămâna de tranziție", short: "✕ tranziție" },
  only_transition: { label: "Doar în săptămâna de tranziție", short: "doar tranziție" },
};

const SEASON_LABELS = { summer: "☀️ Vară", winter: "🔥 Iarnă", auto: "✨ Auto" };
const FAN_LABELS = { quiet: "Silențios", low: "Redus", medium: "Mediu", high: "Maxim", auto: "Auto" };

const MODE_LABELS = {
  cool: "Răcire (AC)",
  heat_boiler: "Încălzire (centrală)",
  heat_boiler_boost: "Încălzire (centrală + AC)",
};
const BAND_LABELS = {
  b0: "< −5°", b1: "−5…0°", b2: "0…5°", b3: "5…10°", b4: "> 10°",
  c0: "< 25°", c1: "25…30°", c2: "30…35°", c3: "> 35°",
};
const BAND_ORDER = { cool: ["c0", "c1", "c2", "c3"], heat: ["b0", "b1", "b2", "b3", "b4"] };

const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
  );

const fmt = (v, unit = "", dec = 1) =>
  v == null || v === "" || isNaN(Number(v)) ? "—" : `${Number(v).toFixed(dec)}${unit}`;

const genId = () =>
  (window.crypto && crypto.randomUUID
    ? crypto.randomUUID().replace(/-/g, "").slice(0, 8)
    : Math.random().toString(16).slice(2, 10));

const pad2 = (n) => String(n).padStart(2, "0");

const fmtCountdown = (ms) => {
  let s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  s = s % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(s)}` : `${m}:${pad2(s)}`;
};

const hm = (iso) => {
  const d = new Date(iso);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};

const dayLabel = (dateIso) => {
  const d = new Date(dateIso + "T00:00:00");
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  const base = `${DAY_NAMES[(d.getDay() + 6) % 7]} ${d.getDate()}`;
  if (diff === 0) return `AZI · ${base}`;
  if (diff === 1) return `MÂINE · ${base}`;
  return base;
};

const toMin = (hhmm) => {
  const [h, m] = String(hhmm || "0:0").split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
};

class ClimateFlowPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "acasa";
    this._data = null;
    this._events = null;    // copie locală editabilă (Program)
    this._oneOffs = null;
    this._dirty = false;
    this._loaded = false;
    this._error = null;
    this._sheet = null;     // {eventId} sau {new: {day, hour}} — editorul modal
    this._learnData = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._load();
    }
  }

  connectedCallback() {
    this._ticks = 0;
    this._timer = setInterval(() => this._tick(), 1000);
  }

  disconnectedCallback() {
    if (this._timer) clearInterval(this._timer);
  }

  async _ws(msg) {
    return this._hass.connection.sendMessagePromise(msg);
  }

  async _load() {
    try {
      this._data = await this._ws({ type: "climateflow/get" });
      this._events = JSON.parse(JSON.stringify(this._data.events || []));
      this._oneOffs = JSON.parse(JSON.stringify(this._data.one_offs || []));
      this._dirty = false;
      this._error = null;
    } catch (e) {
      this._error = (e && e.message) || "Eroare la încărcare";
    }
    this._render();
  }

  async _reload(silent) {
    try {
      this._data = await this._ws({ type: "climateflow/get" });
      if (!silent || !this._dirty) {
        this._events = JSON.parse(JSON.stringify(this._data.events || []));
        this._oneOffs = JSON.parse(JSON.stringify(this._data.one_offs || []));
        if (!silent) this._dirty = false;
      }
      this._error = null;
      this._render();
    } catch (e) {
      this._error = (e && e.message) || "Eroare";
      this._render();
    }
  }

  // -------------------------------------------------------------- ticker live
  _cdTarget() {
    const l = (this._data || {}).live || {};
    if (l.state === "preconditioning" && l.run) return [l.run.event_start, "începe în"];
    if (l.state === "event" && l.run) return [l.run.event_end, "se termină în"];
    if (l.precondition_start && new Date(l.precondition_start) > new Date())
      return [l.precondition_start, "pornește în"];
    if (l.next_event) return [l.next_event, "începe în"];
    return [null, ""];
  }

  _tick() {
    if (this._tab === "acasa" && this._data) {
      const [target] = this._cdTarget();
      const el = this.shadowRoot.getElementById("cd-main");
      if (el && target) {
        const rem = new Date(target).getTime() - Date.now();
        el.textContent = fmtCountdown(rem);
        // La expirare, preluăm starea nouă (o singură dată).
        if (rem <= 0 && !this._reloading) {
          this._reloading = true;
          this._reload(true).finally(() => { this._reloading = false; });
        }
      }
    }
    if (++this._ticks % 30 === 0) this._reload(true);
  }

  // ------------------------------------------------------------------- render
  _render() {
    const tabs = TABS.map(
      ([id, label]) =>
        `<button class="tab ${this._tab === id ? "active" : ""}" data-tab="${id}">${label}</button>`
    ).join("");

    let body = "";
    if (this._error) body = `<div class="err">⚠️ ${esc(this._error)}</div>`;
    else if (!this._data) body = `<div class="muted">Se încarcă…</div>`;
    else if (this._tab === "acasa") body = this._renderHome();
    else if (this._tab === "program") body = this._renderProgram();
    else if (this._tab === "invatare") body = this._renderLearn();
    else if (this._tab === "setari") body = this._renderSettings();
    else body = this._renderHelp();

    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="wrap">
        <div class="tabs">${tabs}</div>
        <div class="body">${body}</div>
        ${this._sheet ? this._renderSheet() : ""}
      </div>`;

    this.shadowRoot.querySelectorAll(".tab").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        this._render();
        if (this._tab === "invatare") this._loadLearn();
      })
    );
    this._wire();
  }

  // -------------------------------------------------------------------- Acasă
  _ctrlState(key) {
    const c = (this._data || {}).controls || {};
    const eid = c[key];
    return (eid && this._hass.states[eid]) || null;
  }

  _renderHome() {
    const l = this._data.live || {};
    const meta = STATE_META[l.state] || STATE_META.idle;
    const run = l.run;
    const enabledSt = this._ctrlState("enabled");
    const enabled = enabledSt ? enabledSt.state === "on" : true;

    // Progres: drumul temperaturii de la start spre țintă (funcționează și la răcire).
    const indoor = l.indoor_temp;
    let progress = null;
    let target = run ? run.target : l.next_event_target;
    if (run && run.start_temp != null && indoor != null && target != null) {
      const span = target - run.start_temp;
      progress = Math.abs(span) < 0.05 ? 1 : (indoor - run.start_temp) / span;
      progress = Math.max(0, Math.min(1, progress));
    }

    const [cdTarget, cdLabel] = this._cdTarget();
    const seasonKey = l.season || "winter";
    const modeLabel = l.mode ? MODE_LABELS[l.mode] || l.mode : "—";
    const boost = run && (run.escalated || run.event_boost);

    const alerts = [];
    if (!enabled)
      alerts.push(`<div class="alert warn">⏸ Sistemul e <b>dezactivat</b> — nu se comandă nimic. Activează-l din comutatorul de mai sus.</div>`);
    if (l.comfort_deficit)
      alerts.push(`<div class="alert warn">🌡️ Temperatura nu se menține la țintă cu ventilatorul plafonat. Plafonul rămâne — confortul are prioritate.</div>`);
    if (l.guard_fighting)
      alerts.push(`<div class="alert warn">🛡️ Cineva modifică repetat un aparat de la telecomandă — garda ia o pauză de 5 minute, apoi reia.</div>`);
    if (l.skip_next_armed)
      alerts.push(`<div class="alert info">⏭ Următorul eveniment va fi <b>sărit</b> — climatizarea nu va porni pentru el.</div>`);

    return `
      <div class="statusbar">
        <span class="pill ${meta.cls}"><span class="dot"></span>${meta.icon} ${meta.label}</span>
        <span class="badge">${SEASON_LABELS[seasonKey] || seasonKey}</span>
        ${boost ? `<span class="badge boost">⚡ boost</span>` : ""}
        <span class="spacer"></span>
        <label class="master ${enabled ? "on" : ""}">
          <input type="checkbox" data-ctrl="toggle" data-key="enabled" ${enabled ? "checked" : ""}/>
          <span>${enabled ? "Sistem activ" : "Sistem oprit"}</span>
        </label>
      </div>

      <div class="home-grid">
        <div class="hero card">
          ${this._renderArc(indoor, target, progress)}
          <div class="hero-side">
            <div class="hero-event">${esc((run && run.name) || l.next_event_name || "Niciun eveniment programat")}</div>
            ${cdTarget ? `
              <div class="hero-cd-label">${esc(cdLabel)}</div>
              <div class="hero-cd" id="cd-main">${fmtCountdown(new Date(cdTarget) - Date.now())}</div>` : ""}
            ${l.state === "idle" && l.precondition_start ? `
              <div class="hero-eta">🔛 pornește la <b>${hm(l.precondition_start)}</b>
                <span class="muted">(avans ${fmt(l.lead_minutes, " min", 0)})</span></div>` : ""}
            ${l.state === "preconditioning" ? `
              <div class="hero-eta">țintă la start: <b>${fmt(target, "°", 1)}</b> · mod: ${esc(modeLabel)}</div>` : ""}
            ${l.state === "event" ? `
              <div class="hero-eta">mențin <b>${fmt(target, "°", 1)}</b> · ventilator plafonat</div>` : ""}
            ${progress != null ? `
              <div class="progress"><div class="progress-fill" style="width:${Math.round(progress * 100)}%"></div></div>
              <div class="muted small">${Math.round(progress * 100)}% din drum (pornit de la ${fmt(run.start_temp, "°", 1)})</div>` : ""}
            <div class="hero-temps muted">interior ${fmt(l.indoor_temp, "°", 1)} · exterior ${fmt(l.outdoor_temp, "°", 1)} · rată ${fmt(l.learned_rate_current, "°C/h", 2)}</div>
          </div>
        </div>

        <div class="actions-home">
          <button class="btn stop" data-act="stop">⏹ OPREȘTE TOT</button>
          <button class="btn" data-act="skip-next">${l.skip_next_armed ? "↩️ Anulează săritul" : "⏭ Sari următorul"}</button>
          <button class="btn" data-act="boost-now">▶ Pornește acum</button>
        </div>

        ${alerts.join("")}

        <h3>Următoarele 7 zile</h3>
        ${this._renderAgenda()}
      </div>`;
  }

  _renderArc(indoor, target, progress) {
    // Arc semicircular SVG: fundal + progres (stroke-dasharray pe semicircumferință).
    const HALF = Math.PI * 80; // r=80
    const p = progress == null ? 0 : progress;
    return `
      <div class="arc">
        <svg viewBox="0 0 200 120">
          <path d="M 20 110 A 80 80 0 0 1 180 110" fill="none"
                stroke="var(--divider-color,#333)" stroke-width="13" stroke-linecap="round"/>
          <path d="M 20 110 A 80 80 0 0 1 180 110" fill="none" class="arc-fill"
                stroke-width="13" stroke-linecap="round"
                stroke-dasharray="${(p * HALF).toFixed(1)} ${HALF.toFixed(1)}"/>
          <text x="100" y="86" class="arc-temp" text-anchor="middle">${indoor == null ? "—" : Number(indoor).toFixed(1) + "°"}</text>
          <text x="100" y="106" class="arc-target" text-anchor="middle">${target == null ? "" : "→ " + Number(target).toFixed(1) + "°"}</text>
        </svg>
      </div>`;
  }

  _renderAgenda() {
    const agenda = this._data.agenda || [];
    if (!agenda.length)
      return `<div class="muted">Nimic programat în următoarele 7 zile.</div>`;
    const byDay = new Map();
    for (const a of agenda) {
      if (!byDay.has(a.date)) byDay.set(a.date, []);
      byDay.get(a.date).push(a);
    }
    let out = "";
    for (const [date, items] of byDay) {
      out += `<div class="ag-day">${esc(dayLabel(date))}</div>`;
      out += items
        .map((a) => {
          const wf = a.week_filter === "only_transition"
            ? `<span class="chip wf">săpt. tranziție</span>` : "";
          const oneoff = a.one_off ? `<span class="chip oneoff">unic</span>` : "";
          return `
            <div class="ag-item ${a.skipped ? "skipped" : ""}">
              <span class="ag-time">${hm(a.start)}–${hm(a.end)}</span>
              <span class="ag-name">${esc(a.name)}</span>
              ${wf}${oneoff}
              <span class="ag-target muted">${fmt(a.target, "°", 1)}</span>
              <button class="btn tiny" data-act="toggle-skip" data-key="${esc(a.key)}">
                ${a.skipped ? "↩️ anulează" : "⏭ sari"}
              </button>
            </div>`;
        })
        .join("");
    }
    return `<div class="agenda">${out}</div>`;
  }

  // ------------------------------------------------------------------ Program
  _gridWindow() {
    let lo = 8 * 60, hi = 22 * 60;
    for (const e of this._events || []) {
      lo = Math.min(lo, toMin(e.start));
      hi = Math.max(hi, toMin(e.end));
    }
    lo = Math.floor(lo / 60) * 60;
    hi = Math.ceil(hi / 60) * 60;
    return [lo, hi];
  }

  _renderProgram() {
    const [lo, hi] = this._gridWindow();
    const PX = 44; // px pe oră
    const height = ((hi - lo) / 60) * PX;

    const hours = [];
    for (let m = lo; m <= hi; m += 60)
      hours.push(`<div class="g-hour" style="top:${((m - lo) / 60) * PX}px">${m / 60}:00</div>`);

    const cols = DAY_SHORT.map((short, day) => {
      const blocks = (this._events || [])
        .map((e, idx) => ({ e, idx }))
        .filter(({ e }) => (e.days || []).includes(day))
        .map(({ e, idx }) => {
          const top = ((toMin(e.start) - lo) / 60) * PX;
          const h = Math.max(20, ((toMin(e.end) - toMin(e.start)) / 60) * PX);
          const wf = WF_META[e.week_filter] || WF_META.always;
          const cls =
            e.enabled === false ? "off"
            : e.week_filter === "only_transition" ? "only-tr"
            : e.week_filter === "except_transition" ? "except-tr" : "";
          return `
            <div class="g-block ${cls}" style="top:${top}px;height:${h}px" data-act="edit-event" data-idx="${idx}">
              <div class="g-b-time">${esc(e.start)}–${esc(e.end)}</div>
              <div class="g-b-name">${esc(e.name)}</div>
              ${wf.short ? `<div class="g-b-wf">${wf.short}</div>` : ""}
            </div>`;
        })
        .join("");
      return `
        <div class="g-col">
          <div class="g-head">${short}</div>
          <div class="g-body" style="height:${height}px" data-act="add-at" data-day="${day}" data-lo="${lo}" data-px="${PX}">
            ${blocks}
          </div>
        </div>`;
    }).join("");

    const oneOffs = (this._oneOffs || [])
      .map(
        (o, i) => `
        <div class="ag-item">
          <span class="ag-time">${esc(o.date)} · ${esc(o.start)}–${esc(o.end)}</span>
          <span class="ag-name">${esc(o.name)}</span>
          <span class="ag-target muted">${o.target != null ? fmt(o.target, "°", 1) : "implicită"}</span>
          <button class="btn tiny" data-act="del-oneoff" data-i="${i}">✕</button>
        </div>`
      )
      .join("") || `<div class="muted">Niciun eveniment unic.</div>`;

    return `
      <p class="muted">Programul e <b>preinstalat</b> și rulează singur. Aici doar îl vezi și,
      la nevoie, îl ajustezi: click pe un bloc = editezi, click pe spațiu gol = adaugi.</p>
      <div class="grid-wrap">
        <div class="g-hours" style="height:${height + 34}px">${hours.join("")}</div>
        <div class="grid">${cols}</div>
      </div>
      <div class="legend">
        <span><i class="sw"></i> în fiecare săptămână</span>
        <span><i class="sw except"></i> exceptând săpt. de tranziție</span>
        <span><i class="sw only"></i> doar în săpt. de tranziție</span>
      </div>

      <h3>Evenimente unice <span class="muted small">(nunți, ocazii)</span></h3>
      ${oneOffs}
      <div class="oneoff-add">
        <input id="oo-date" type="date"/>
        <input id="oo-start" type="time" value="16:00"/>
        <input id="oo-end" type="time" value="20:00"/>
        <input id="oo-name" type="text" placeholder="Nume (ex. Nuntă)"/>
        <input id="oo-target" type="number" step="0.5" min="15" max="28" placeholder="°C" class="num"/>
        <button class="btn" data-act="add-oneoff">➕ Adaugă</button>
      </div>

      <div class="savebar">
        <span class="dirty">${this._dirty ? "● modificări nesalvate" : ""}</span>
        <button class="btn primary" data-act="save-events">💾 Salvează programul</button>
      </div>`;
  }

  _renderSheet() {
    const s = this._sheet;
    const e = s.eventId != null
      ? this._events[s.eventId]
      : { name: "", days: [s.day], start: `${pad2(s.hour)}:00`, end: `${pad2(Math.min(23, s.hour + 2))}:00`,
          target: null, week_filter: "always", enabled: true };
    const days = DAY_SHORT.map(
      (d, i) =>
        `<button class="chip day ${(e.days || []).includes(i) ? "sel" : ""}" data-sheet="day" data-i="${i}">${d}</button>`
    ).join("");
    const wf = ["always", "except_transition", "only_transition"]
      .map(
        (k) =>
          `<button class="seg ${e.week_filter === k ? "sel" : ""}" data-sheet="wf" data-k="${k}">${WF_META[k].label}</button>`
      )
      .join("");
    return `
      <div class="overlay" data-act="close-sheet">
        <div class="sheet" data-stop="1">
          <h3>${s.eventId != null ? "Editează evenimentul" : "Eveniment nou"}</h3>
          <label class="lbl">Nume
            <input id="sh-name" type="text" value="${esc(e.name)}" placeholder="ex. Marți seara"/></label>
          <label class="lbl">Zile</label>
          <div class="days-row">${days}</div>
          <div class="row2">
            <label class="lbl">De la <input id="sh-start" type="time" value="${esc(e.start)}"/></label>
            <label class="lbl">Până la <input id="sh-end" type="time" value="${esc(e.end)}"/></label>
            <label class="lbl">Țintă (gol = implicită)
              <input id="sh-target" type="number" step="0.5" min="15" max="28" class="num"
                     value="${e.target != null ? esc(e.target) : ""}" placeholder="—"/></label>
          </div>
          <label class="lbl">Săptămâna</label>
          <div class="seg-row">${wf}</div>
          <p class="muted small">„Săptămâna de tranziție” = săptămâna în care duminica pică pe
          luna următoare (ex.: joi 29 oct → duminica e 1 nov). Joia are „exceptând”, vinerea „doar”.</p>
          <label class="chk"><input id="sh-enabled" type="checkbox" ${e.enabled !== false ? "checked" : ""}/> Activ</label>
          <div class="sheet-actions">
            ${s.eventId != null ? `<button class="btn danger" data-act="sheet-delete">🗑 Șterge</button>` : ""}
            <span class="spacer"></span>
            <button class="btn" data-act="close-sheet">Renunță</button>
            <button class="btn primary" data-act="sheet-save">OK</button>
          </div>
        </div>
      </div>`;
  }

  // ----------------------------------------------------------------- Învățare
  async _loadLearn() {
    try {
      this._learnData = await this._ws({ type: "climateflow/history" });
    } catch (e) {
      this._learnData = { error: (e && e.message) || "Eroare" };
    }
    if (this._tab === "invatare") this._render();
  }

  _renderLearn() {
    const l = this._data.live || {};
    const h = this._learnData;
    const rates = (h && h.rates) || l.rates || {};

    const modeBars = Object.keys(MODE_LABELS)
      .map((mode) => {
        const bands = rates[mode] || {};
        const order = BAND_ORDER[mode === "cool" ? "cool" : "heat"];
        const max = Math.max(0.1, ...Object.values(bands).map(Number));
        const rows = order
          .map((b) => {
            const v = bands[b];
            return `
              <div class="bar-row">
                <span class="bar-label">${BAND_LABELS[b]}</span>
                ${v != null
                  ? `<div class="bar"><div class="bar-fill" style="width:${Math.round((v / max) * 100)}%"></div></div>
                     <span class="bar-val">${Number(v).toFixed(2)} °C/h</span>`
                  : `<span class="muted small">încă învață…</span>`}
              </div>`;
          })
          .join("");
        return `<div class="card learn-card"><div class="learn-title">${MODE_LABELS[mode]}</div>${rows}</div>`;
      })
      .join("");

    let history = `<div class="muted">Se încarcă istoricul…</div>`;
    if (h && h.error) history = `<div class="err">⚠️ ${esc(h.error)}</div>`;
    else if (h) {
      const rows = (h.records || [])
        .filter((r) => r.type === "precondition")
        .map((r) => {
          const d = r.ts ? new Date(r.ts).toLocaleString("ro-RO", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";
          const ok = r.on_time == null ? "—" : r.on_time ? `<span class="ok">✓ la timp</span>` : `<span class="late">✗ întârziat</span>`;
          return `<tr><td>${esc(d)}</td><td>${esc(r.name || "")}</td><td class="r">${fmt(r.minutes, " min", 0)}</td><td class="r">${r.rate != null ? fmt(r.rate, " °C/h", 2) : "—"}</td><td>${ok}</td></tr>`;
        })
        .join("") || `<tr><td colspan="5" class="muted">Nicio precondiționare încă — istoricul apare după primele evenimente.</td></tr>`;
      history = `<table>
        <tr class="muted"><th style="text-align:left">Când</th><th style="text-align:left">Eveniment</th><th class="r">Durată</th><th class="r">Rată</th><th></th></tr>
        ${rows}</table>`;
    }

    return `
      <div class="cards">
        <div class="card kv"><span>Eșantioane învățate</span><b>${l.sample_count != null ? l.sample_count : "—"}</b></div>
        <div class="card kv"><span>Punctualitate (30 zile)</span><b>${l.on_time_pct != null ? l.on_time_pct + "%" : "—"}</b></div>
        <div class="card kv"><span>Rata în condițiile curente</span><b>${fmt(l.learned_rate_current, " °C/h", 2)}</b></div>
      </div>
      <h3>Rate învățate pe temperatura exterioară</h3>
      <div class="learn-grid">${modeBars}</div>
      <h3>Istoric precondiționări</h3>
      ${history}
      <div class="actions"><button class="btn" data-act="learn-refresh">↻ Reîmprospătează</button></div>`;
  }

  // ------------------------------------------------------------------- Setări
  _renderSettings() {
    const g = this._data.general || {};
    const c = this._data.controls || {};
    const st = (key) => this._ctrlState(key);
    const num = (key, dflt) => { const s = st(key); return s ? s.state : dflt; };
    const on = (key, dflt) => { const s = st(key); return s ? s.state === "on" : dflt; };
    const sel = (key, dflt) => { const s = st(key); return s ? s.state : dflt; };

    const sensorOpts = (selected) =>
      (this._data.temp_sensors || [])
        .map((s) => `<option value="${esc(s.entity_id)}" ${s.entity_id === selected ? "selected" : ""}>${esc(s.name)}</option>`)
        .join("");
    const climateOpts = (this._data.climates || [])
      .map((cl) => `<option value="${esc(cl.entity_id)}" ${(g.ac_units || []).includes(cl.entity_id) ? "selected" : ""}>${esc(cl.name)}</option>`)
      .join("");
    const boilerOpts = (this._data.climates || [])
      .map((cl) => `<option value="${esc(cl.entity_id)}" ${cl.entity_id === g.boiler ? "selected" : ""}>${esc(cl.name)}</option>`)
      .join("");

    const seasonSeg = ["summer", "winter", "auto"]
      .map((k) => `<button class="seg ${sel("season", "auto") === k ? "sel" : ""}" data-ctrl="select" data-key="season" data-val="${k}">${SEASON_LABELS[k]}</button>`)
      .join("");
    const fanSeg = ["quiet", "low", "medium", "high", "auto"]
      .map((k) => `<button class="seg ${sel("fan_cap", "low") === k ? "sel" : ""}" data-ctrl="select" data-key="fan_cap" data-val="${k}">${FAN_LABELS[k]}</button>`)
      .join("");

    const numField = (key, label, min, max, step, dflt, unit) => `
      <label class="lbl">${label}
        <span class="numrow">
          <input type="number" min="${min}" max="${max}" step="${step}" value="${esc(num(key, dflt))}"
                 data-ctrl="number" data-key="${key}" class="num"/>
          <span class="unit">${unit}</span>
        </span></label>`;

    return `
      <h3>Comportament</h3>
      <label class="chk"><input type="checkbox" data-ctrl="toggle" data-key="enabled" ${on("enabled", true) ? "checked" : ""}/> Sistem activ</label>
      <label class="chk"><input type="checkbox" data-ctrl="toggle" data-key="guard" ${on("guard", true) ? "checked" : ""}/> Gardă anti-modificare (suprascrie telecomenzile în timpul evenimentelor)</label>
      <label class="chk"><input type="checkbox" data-ctrl="toggle" data-key="notify" ${on("notify", true) ? "checked" : ""}/> Notificări</label>

      <label class="lbl">Sezon</label>
      <div class="seg-row">${seasonSeg}</div>
      <p class="muted small">Pe „Auto”: sub 15° afară → iarnă, peste 22° → vară (decis la pornirea precondiționării).</p>

      <label class="lbl">Ventilator maxim în timpul evenimentului</label>
      <div class="seg-row">${fanSeg}</div>

      <div class="row2">
        ${numField("target", "Țintă temperatură", 15, 28, 0.5, 22, "°C")}
        ${numField("max_lead", "Avans maxim pornire", 30, 360, 10, 180, "min")}
        ${numField("tolerance", "Toleranță confort", 0.2, 2, 0.1, 0.7, "°C")}
        ${numField("overshoot", "Supra-țintă precondiționare", 0, 2, 0.1, 0.5, "°C")}
        ${numField("rate_heat", "Rată implicită încălzire", 0.2, 10, 0.1, 1.5, "°C/h")}
        ${numField("rate_cool", "Rată implicită răcire", 0.2, 10, 0.1, 2.0, "°C/h")}
      </div>

      <h3>Senzori și aparate</h3>
      <label class="lbl">Senzor temperatură interior</label>
      <select id="set-indoor">${sensorOpts(g.indoor_sensor)}</select>
      <label class="lbl">Senzor temperatură exterior</label>
      <select id="set-outdoor">${sensorOpts(g.outdoor_sensor)}</select>
      <label class="lbl">Aere condiționate (selectează mai multe)</label>
      <select id="set-acs" multiple size="5">${climateOpts}</select>
      <label class="lbl">Centrală termică (opțional)</label>
      <select id="set-boiler"><option value="" ${!g.boiler ? "selected" : ""}>— fără centrală —</option>${boilerOpts}</select>
      <label class="lbl">Notificări push către (opțional)</label>
      <select id="set-notify">
        <option value="" ${!g.notify_service ? "selected" : ""}>— doar în interfața HA —</option>
        ${(this._data.notify_services || [])
          .map((s) => `<option value="${esc(s)}" ${s === g.notify_service ? "selected" : ""}>notify.${esc(s)}</option>`)
          .join("")}
      </select>
      <div class="actions"><button class="btn primary" data-act="save-general">💾 Salvează senzorii/aparatele</button></div>
      <p class="muted small">Comutatoarele și valorile numerice se aplică imediat; doar secțiunea
      „Senzori și aparate” are nevoie de Salvează (reîncarcă integrarea).</p>`;
  }

  // ------------------------------------------------------------------- Ajutor
  _renderHelp() {
    return `
      <h3>Cum funcționează</h3>
      <p>ClimateFlow pornește singur climatizarea <b>înainte</b> de fiecare eveniment, astfel
      încât la ora de început sala să fie deja la temperatura țintă. În timpul evenimentului
      menține ținta cu ventilatoarele <b>plafonate</b> (fără zgomot), iar la final
      <b>stinge tot</b> și nu se mai atinge de nimic.</p>
      <h3>Avansul de pornire (învățare)</h3>
      <p>Avans = grade de recuperat ÷ rata învățată (°C/h) × 1.15 marjă, plafonat la „Avans
      maxim”. Rata se învață din fiecare precondiționare, pe <b>benzi de temperatură
      exterioară</b> și pe mod (răcire / centrală / centrală+AC). Până se strâng eșantioane se
      folosesc ratele implicite din Setări — primele porniri pot fi imprecise, apoi devin tot
      mai exacte (vezi tab-ul Învățare).</p>
      <h3>Săptămâna de tranziție</h3>
      <p>Regula programului: <b>joia nu se ține</b> în săptămâna în care duminica pică pe luna
      următoare — în locul ei se ține <b>vinerea</b>. Exemplu: joi 29 oct 2026 → duminica
      acelei săptămâni e 1 nov → joia sare, vineri 30 oct se ține. ClimateFlow aplică regula
      singur; în agenda de pe Acasă vezi mereu programul deja rezolvat.</p>
      <h3>Iarna: centrală + aere</h3>
      <p>Centrala e sursa principală. Dacă estimarea arată că ținta nu se prinde nici cu
      avansul maxim, aerele pornesc și ele pe încălzire (<b>boost</b>). În timpul evenimentului
      aerele pot intra în ajutor doar cu ventilatorul plafonat.</p>
      <h3>Garda anti-telecomandă</h3>
      <p>În timpul precondiționării și al evenimentului, orice modificare făcută din
      telecomandă (temperatură, mod, viteză) este <b>suprascrisă</b> în ~20 de secunde. Dacă
      cineva insistă (3 modificări în 5 minute), garda ia o pauză de 5 minute și te anunță.
      O poți opri din Setări → „Gardă anti-modificare”.</p>
      <h3>Butoanele</h3>
      <ul class="help">
        <li><b>⏹ Oprește tot</b> — stinge imediat aerele și centrala; sistemul așteaptă
        următorul eveniment. Funcționează de pe orice cont, nu cere admin.</li>
        <li><b>⏭ Sari următorul</b> — următorul eveniment nu pornește climatizarea (apăsat
        din nou, anulează). Poți sări orice ocurență din agendă, nu doar următoarea.</li>
        <li><b>▶ Pornește acum</b> — începe precondiționarea imediat pentru următorul
        eveniment; dacă nu urmează nimic, pornește o rulare manuală de 60 de minute.</li>
      </ul>
      <h3>Evenimente unice</h3>
      <p>Nunți, ocazii: tab-ul Program → „Evenimente unice”. Se țin o singură dată, la data
      aleasă, cu precondiționare normală.</p>`;
  }

  // --------------------------------------------------------------------- wire
  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-act]").forEach((el) =>
      el.addEventListener("click", (e) => {
        if (el.dataset.act === "add-at" && e.target !== el) return; // doar click pe gol
        e.stopPropagation();
        this._onAction(el.dataset.act, el.dataset, e);
      })
    );
    root.querySelectorAll("[data-stop]").forEach((el) =>
      el.addEventListener("click", (e) => e.stopPropagation())
    );
    root.querySelectorAll("[data-ctrl]").forEach((el) => {
      const kind = el.dataset.ctrl;
      if (kind === "toggle") el.addEventListener("change", () => this._ctrlToggle(el));
      else if (kind === "number") el.addEventListener("change", () => this._ctrlNumber(el));
      // data-ctrl="select" e un buton — merge prin click-ul de mai jos
    });
    root.querySelectorAll("[data-ctrl='select']").forEach((el) =>
      el.addEventListener("click", () => this._ctrlSelect(el))
    );
    root.querySelectorAll("[data-sheet]").forEach((el) =>
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        this._onSheetField(el);
      })
    );
  }

  _ctrlEid(key) {
    const c = (this._data || {}).controls || {};
    return c[key] || null;
  }

  async _ctrlToggle(el) {
    const eid = this._ctrlEid(el.dataset.key);
    if (!eid) return;
    await this._hass.callService("switch", el.checked ? "turn_on" : "turn_off", { entity_id: eid });
    setTimeout(() => this._reload(true), 400);
  }

  async _ctrlNumber(el) {
    const eid = this._ctrlEid(el.dataset.key);
    if (!eid || el.value === "") return;
    await this._hass.callService("number", "set_value", { entity_id: eid, value: parseFloat(el.value) });
  }

  async _ctrlSelect(el) {
    const eid = this._ctrlEid(el.dataset.key);
    if (!eid) return;
    await this._hass.callService("select", "select_option", { entity_id: eid, option: el.dataset.val });
    setTimeout(() => this._reload(true), 400);
  }

  _onSheetField(el) {
    // Chip-urile de zile / segmentele de filtru comută vizual fără să pierdem inputurile.
    if (el.dataset.sheet === "day") el.classList.toggle("sel");
    else if (el.dataset.sheet === "wf") {
      this.shadowRoot.querySelectorAll("[data-sheet='wf']").forEach((b) => b.classList.remove("sel"));
      el.classList.add("sel");
    }
  }

  _sheetRead() {
    const $ = (id) => this.shadowRoot.getElementById(id);
    const days = Array.from(this.shadowRoot.querySelectorAll("[data-sheet='day'].sel"))
      .map((b) => +b.dataset.i);
    const wfEl = this.shadowRoot.querySelector("[data-sheet='wf'].sel");
    const targetRaw = $("sh-target").value;
    return {
      name: $("sh-name").value || "Eveniment",
      days,
      start: $("sh-start").value || "18:00",
      end: $("sh-end").value || "20:00",
      target: targetRaw === "" ? null : parseFloat(targetRaw),
      week_filter: wfEl ? wfEl.dataset.k : "always",
      enabled: $("sh-enabled").checked,
    };
  }

  async _onAction(act, d, ev) {
    try {
      // ------------------------------------------------------------- acțiuni live
      if (act === "stop") {
        await this._ws({ type: "climateflow/stop" });
        this._toast("Totul a fost oprit.");
        return void this._reload(true);
      }
      if (act === "skip-next") {
        await this._ws({ type: "climateflow/skip_next" });
        return void this._reload(true);
      }
      if (act === "boost-now") {
        await this._ws({ type: "climateflow/boost_now" });
        this._toast("Pornesc climatizarea.");
        return void this._reload(true);
      }
      if (act === "toggle-skip") {
        await this._ws({ type: "climateflow/toggle_skip", key: d.key });
        return void this._reload(true);
      }
      if (act === "learn-refresh") return void this._loadLearn();

      // ---------------------------------------------------------------- program
      if (act === "edit-event") {
        this._sheet = { eventId: +d.idx };
        return this._render();
      }
      if (act === "add-at") {
        const rect = ev.currentTarget.getBoundingClientRect();
        const min = +d.lo + ((ev.clientY - rect.top) / +d.px) * 60;
        this._sheet = { day: +d.day, hour: Math.max(0, Math.min(23, Math.floor(min / 60))) };
        return this._render();
      }
      if (act === "close-sheet") {
        this._sheet = null;
        return this._render();
      }
      if (act === "sheet-save") {
        const vals = this._sheetRead();
        if (!vals.days.length) return this._toast("Alege cel puțin o zi.", true);
        if (this._sheet.eventId != null)
          this._events[this._sheet.eventId] = { ...this._events[this._sheet.eventId], ...vals };
        else this._events.push({ id: genId(), ...vals });
        this._sheet = null;
        this._dirty = true;
        return this._render();
      }
      if (act === "sheet-delete") {
        this._events.splice(this._sheet.eventId, 1);
        this._sheet = null;
        this._dirty = true;
        return this._render();
      }
      if (act === "add-oneoff") {
        const $ = (id) => this.shadowRoot.getElementById(id);
        if (!$("oo-date").value) return this._toast("Alege data.", true);
        const targetRaw = $("oo-target").value;
        this._oneOffs.push({
          id: genId(),
          name: $("oo-name").value || "Eveniment unic",
          date: $("oo-date").value,
          start: $("oo-start").value || "16:00",
          end: $("oo-end").value || "20:00",
          target: targetRaw === "" ? null : parseFloat(targetRaw),
        });
        this._dirty = true;
        return this._render();
      }
      if (act === "del-oneoff") {
        this._oneOffs.splice(+d.i, 1);
        this._dirty = true;
        return this._render();
      }
      if (act === "save-events") {
        await this._ws({
          type: "climateflow/save_events",
          events: this._events,
          one_offs: this._oneOffs,
        });
        this._dirty = false;
        this._toast("Programul a fost salvat.");
        return this._reload(true);
      }

      // ----------------------------------------------------------------- setări
      if (act === "save-general") {
        const $ = (id) => this.shadowRoot.getElementById(id);
        const acs = Array.from($("set-acs").selectedOptions).map((o) => o.value);
        if (!acs.length) return this._toast("Alege cel puțin un aer condiționat.", true);
        await this._ws({
          type: "climateflow/save_general",
          indoor_sensor: $("set-indoor").value,
          outdoor_sensor: $("set-outdoor").value,
          ac_units: acs,
          boiler: $("set-boiler").value || null,
          notify_service: $("set-notify").value || null,
        });
        this._toast("Setările au fost salvate.");
        return this._reload(true);
      }
    } catch (e) {
      this._toast("Eroare: " + ((e && e.message) || e), true);
    }
  }

  _toast(msg, err) {
    const t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = `position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 16px;border-radius:10px;color:#fff;z-index:9999;font-family:sans-serif;background:${err ? "#b00020" : "#2e7d32"}`;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 2600);
  }
}

const STYLE = `
  :host{display:block;color:var(--primary-text-color,#e1e1e1);font-family:var(--paper-font-body1_-_font-family,sans-serif);}
  *{box-sizing:border-box;}
  .wrap{max-width:960px;margin:0 auto;padding:14px;}
  h3{margin:22px 0 10px;font-size:15px;text-transform:uppercase;letter-spacing:.4px;opacity:.75;}
  .muted{opacity:.6;} .small{font-size:12px;} .err{color:#e57373;padding:10px;}
  .spacer{flex:1;}
  .r{text-align:right;font-variant-numeric:tabular-nums;}
  table{width:100%;border-collapse:collapse;} td,th{padding:7px 5px;border-bottom:1px solid var(--divider-color,#2a2a2a);} th{font-weight:normal;}

  /* tabs */
  .tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px;position:sticky;top:0;z-index:5;padding:6px 0;background:var(--primary-background-color,#111);}
  .tab{padding:9px 18px;border:none;border-radius:999px;background:var(--card-background-color,#1c1c1c);color:inherit;cursor:pointer;font:inherit;font-size:14px;transition:background .15s,color .15s;}
  .tab.active{background:var(--primary-color,#03a9f4);color:#fff;}

  /* status bar */
  .statusbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px;}
  .pill{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;font-weight:600;font-size:14px;background:var(--card-background-color,#1c1c1c);}
  .pill .dot{width:9px;height:9px;border-radius:50%;background:#888;}
  .pill.s-pre{background:rgba(246,168,33,.15);color:#f6a821;} .pill.s-pre .dot{background:#f6a821;animation:pulse 1.4s infinite;}
  .pill.s-event{background:rgba(67,160,71,.15);color:#66bb6a;} .pill.s-event .dot{background:#66bb6a;animation:pulse 1.4s infinite;}
  .pill.s-cool{background:rgba(3,169,244,.15);color:#4fc3f7;} .pill.s-cool .dot{background:#4fc3f7;}
  @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.45;transform:scale(.8)}}
  .badge{padding:5px 11px;border-radius:999px;background:var(--card-background-color,#1c1c1c);font-size:13px;}
  .badge.boost{background:rgba(255,179,0,.18);color:#ffb300;font-weight:600;}
  .master{display:inline-flex;align-items:center;gap:8px;padding:7px 14px;border-radius:999px;background:var(--card-background-color,#1c1c1c);cursor:pointer;font-size:14px;opacity:.75;}
  .master.on{background:rgba(67,160,71,.15);color:#81c784;opacity:1;}
  .master input{accent-color:#43a047;width:18px;height:18px;}

  /* hero */
  .card{background:var(--card-background-color,#1c1c1c);border-radius:16px;padding:16px;box-shadow:0 1px 4px rgba(0,0,0,.25);}
  .home-grid{display:block;}
  .hero{display:flex;gap:20px;align-items:center;flex-wrap:wrap;}
  .arc{flex:0 0 230px;max-width:230px;margin:0 auto;}
  .arc svg{width:100%;display:block;}
  .arc-fill{stroke:var(--primary-color,#03a9f4);transition:stroke-dasharray .8s ease;}
  .arc-temp{fill:var(--primary-text-color,#e1e1e1);font-size:30px;font-weight:700;}
  .arc-target{fill:var(--primary-text-color,#e1e1e1);opacity:.55;font-size:13px;}
  .hero-side{flex:1;min-width:230px;display:flex;flex-direction:column;gap:6px;}
  .hero-event{font-size:20px;font-weight:700;}
  .hero-cd-label{font-size:13px;opacity:.6;}
  .hero-cd{font-size:44px;font-weight:800;font-variant-numeric:tabular-nums;line-height:1.05;}
  .hero-eta{font-size:14px;}
  .hero-temps{font-size:13px;margin-top:4px;}
  .progress{height:10px;border-radius:6px;background:var(--divider-color,#333);overflow:hidden;margin-top:6px;}
  .progress-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--primary-color,#03a9f4),#66bb6a);transition:width .8s ease;}

  /* acțiuni + alerte */
  .actions,.actions-home{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px;}
  .btn{padding:11px 18px;border:1px solid var(--divider-color,#444);border-radius:12px;background:var(--card-background-color,#1c1c1c);color:inherit;cursor:pointer;font:inherit;font-size:14px;transition:filter .15s;}
  .btn:hover{filter:brightness(1.15);}
  .btn.primary{background:var(--primary-color,#03a9f4);color:#fff;border-color:transparent;}
  .btn.stop{background:#c62828;color:#fff;border-color:transparent;font-weight:700;}
  .btn.danger{background:transparent;color:#e57373;border-color:#e57373;}
  .btn.tiny{padding:4px 10px;font-size:12px;border-radius:8px;}
  .alert{border-radius:12px;padding:11px 14px;margin-top:12px;font-size:14px;line-height:1.45;}
  .alert.warn{background:rgba(255,179,0,.12);border:1px solid rgba(255,179,0,.4);}
  .alert.info{background:rgba(3,169,244,.12);border:1px solid rgba(3,169,244,.35);}

  /* agenda */
  .agenda{display:flex;flex-direction:column;gap:2px;}
  .ag-day{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;opacity:.55;margin:12px 0 4px;}
  .ag-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:10px;background:var(--card-background-color,#1c1c1c);flex-wrap:wrap;}
  .ag-item.skipped .ag-name,.ag-item.skipped .ag-time{text-decoration:line-through;opacity:.45;}
  .ag-time{font-variant-numeric:tabular-nums;font-weight:600;white-space:nowrap;}
  .ag-name{flex:1;min-width:120px;}
  .ag-target{white-space:nowrap;}
  .chip{font-size:11px;padding:3px 9px;border-radius:999px;background:var(--divider-color,#333);}
  .chip.wf{background:rgba(171,71,188,.2);color:#ce93d8;}
  .chip.oneoff{background:rgba(3,169,244,.18);color:#4fc3f7;}

  /* grila program */
  .grid-wrap{display:flex;gap:0;overflow-x:auto;padding-bottom:6px;}
  .g-hours{flex:0 0 44px;position:relative;margin-top:0;}
  .g-hour{position:absolute;right:8px;font-size:11px;opacity:.5;transform:translateY(28px);}
  .grid{display:flex;flex:1;min-width:660px;gap:4px;}
  .g-col{flex:1;min-width:92px;display:flex;flex-direction:column;}
  .g-head{text-align:center;font-size:13px;font-weight:600;padding:6px 0;opacity:.75;}
  .g-body{position:relative;background:var(--card-background-color,#1c1c1c);border-radius:10px;background-image:repeating-linear-gradient(to bottom,transparent 0,transparent 43px,var(--divider-color,#2a2a2a) 43px,var(--divider-color,#2a2a2a) 44px);cursor:copy;}
  .g-block{position:absolute;left:3px;right:3px;border-radius:8px;padding:5px 7px;background:var(--primary-color,#03a9f4);color:#fff;overflow:hidden;cursor:pointer;transition:filter .15s;border:1.5px solid transparent;}
  .g-block:hover{filter:brightness(1.15);}
  .g-block.except-tr{background:linear-gradient(160deg,var(--primary-color,#03a9f4),#7e57c2);}
  .g-block.only-tr{background:rgba(171,71,188,.22);border:1.5px dashed #ce93d8;color:#e1bee7;}
  .g-block.off{background:var(--divider-color,#333);opacity:.55;color:inherit;}
  .g-b-time{font-size:10px;opacity:.85;font-variant-numeric:tabular-nums;}
  .g-b-name{font-size:12px;font-weight:600;line-height:1.2;}
  .g-b-wf{font-size:9px;opacity:.85;margin-top:1px;text-transform:uppercase;letter-spacing:.4px;}
  .legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;font-size:12px;opacity:.75;}
  .legend .sw{display:inline-block;width:12px;height:12px;border-radius:3px;background:var(--primary-color,#03a9f4);vertical-align:-1px;margin-right:5px;}
  .legend .sw.except{background:linear-gradient(160deg,var(--primary-color,#03a9f4),#7e57c2);}
  .legend .sw.only{background:rgba(171,71,188,.25);border:1px dashed #ce93d8;}

  /* one-off + savebar */
  .oneoff-add{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;}
  .savebar{position:sticky;bottom:0;display:flex;gap:10px;align-items:center;margin-top:16px;padding:12px 14px;background:var(--card-background-color,#1c1c1c);border-radius:12px;box-shadow:0 -2px 8px rgba(0,0,0,.3);}
  .dirty{flex:1;color:#ffb300;font-size:13px;}

  /* sheet modal */
  .overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:50;padding:16px;}
  .sheet{background:var(--card-background-color,#1c1c1c);border-radius:16px;padding:20px;width:100%;max-width:480px;max-height:92vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,.5);}
  .sheet h3{margin-top:0;}
  .days-row{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 8px;}
  .chip.day{cursor:pointer;padding:8px 12px;font-size:13px;border:1px solid var(--divider-color,#444);background:transparent;color:inherit;}
  .chip.day.sel{background:var(--primary-color,#03a9f4);color:#fff;border-color:transparent;}
  .seg-row{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 8px;}
  .seg{padding:9px 13px;border-radius:10px;border:1px solid var(--divider-color,#444);background:transparent;color:inherit;cursor:pointer;font:inherit;font-size:13px;}
  .seg.sel{background:var(--primary-color,#03a9f4);color:#fff;border-color:transparent;}
  .sheet-actions{display:flex;gap:10px;margin-top:16px;align-items:center;}

  /* forms */
  input,select{background:var(--secondary-background-color,#111);color:inherit;border:1px solid var(--divider-color,#444);border-radius:8px;padding:9px 10px;font:inherit;min-width:0;}
  input.num{width:96px;text-align:right;}
  .numrow{display:flex;align-items:center;gap:6px;}
  .unit{opacity:.6;font-size:13px;}
  .lbl{display:block;font-size:13px;opacity:.85;margin:10px 0 4px;}
  .chk{display:flex;align-items:center;gap:10px;margin:8px 0;font-size:14px;cursor:pointer;}
  .chk input{width:18px;height:18px;accent-color:var(--primary-color,#03a9f4);}
  .row2{display:flex;gap:16px;flex-wrap:wrap;margin:6px 0;}
  select[multiple]{min-width:240px;}

  /* învățare */
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;}
  .card.kv{display:flex;flex-direction:column;gap:4px;padding:13px;}
  .card.kv span{font-size:12px;opacity:.65;} .card.kv b{font-size:22px;}
  .learn-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;}
  .learn-card{padding:14px;}
  .learn-title{font-weight:700;margin-bottom:10px;font-size:14px;}
  .bar-row{display:flex;align-items:center;gap:8px;margin:6px 0;}
  .bar-label{flex:0 0 62px;font-size:12px;opacity:.7;text-align:right;font-variant-numeric:tabular-nums;}
  .bar{flex:1;height:12px;border-radius:6px;background:var(--divider-color,#2a2a2a);overflow:hidden;}
  .bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#f6a821,#66bb6a);transition:width .5s ease;}
  .bar-val{font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap;}
  .ok{color:#66bb6a;} .late{color:#e57373;}

  ul.help{line-height:1.6;} ul.help li{margin-bottom:8px;} p{line-height:1.55;}

  /* responsive */
  @media(min-width:1000px){
    .home-grid{display:grid;grid-template-columns:1fr;}
  }
  @media(max-width:600px){
    .hero{flex-direction:column;text-align:center;}
    .hero-side{align-items:center;}
    .hero-cd{font-size:52px;}
    .actions-home .btn{flex:1;text-align:center;padding:14px 10px;}
    .oneoff-add input{flex:1 1 40%;}
  }
`;

// Guard: modulul poate fi evaluat de două ori în aceeași pagină (URL vechi din cache +
// URL nou cu hash) — a doua definire ar arunca „name already used with this registry".
if (!customElements.get("climateflow-panel")) {
  customElements.define("climateflow-panel", ClimateFlowPanel);
}
