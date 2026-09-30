// Room Routines sidebar panel. Plain web component: no build step, no dependencies.
// Live data comes from one websocket subscription (room_routines/subscribe); changes go
// through room_routines/save_look, save_room, remove_room, save_periods, save_tracks and
// dismiss. Scenes are saved through Home Assistant's own scene API (config/scene/config,
// the one the scene editor uses), so they stay ordinary Home Assistant scenes. The deeper
// settings are shown to admins only (and the websocket refuses them to anyone else).

// Must match manifest.json (a test checks). Compared with the running integration so a
// tab still holding old page code after an update says so.
const PANEL_VERSION = "0.3.0";

const STATE_LABEL = { idle: "Idle", owned: "Lights on by motion", manual: "Switched on by hand" };
const MODE_LABEL = { off: "Off", log_only: "Log only", live: "Live" };
const TRACK_LABEL = { normal: "Normal", dim: "Dim" };
const TRACK_ICON = { normal: "mdi:weather-sunny", dim: "mdi:weather-cloudy" };
const MODE_HELP = {
  off: "Does nothing.",
  log_only: "Works out what it would do and writes it down, but switches nothing. Use it to check a room before it goes live.",
  live: "Switches the room's lights.",
};
const PERIOD_COLOURS = ["#3949ab", "#8e24aa", "#fb8c00", "#fdd835", "#43a047", "#00acc1", "#e53935", "#6d4c41"];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const ROOM_DEFAULTS = { threshold_lux: 50, timeout_s: 30, fade_out_s: 15, cooldown_s: 30, drift_s: 90 };
const ERRORS = {
  no_name: "Give the room a name.",
  invalid_room: "A room needs at least one light and at least one sensor that switches the lights on, and a sensor can't have both roles.",
  invalid_periods: "Every period needs its own name and a start time no other period uses on the same days.",
  last_period: "There has to be at least one period.",
  unknown_period: "That period no longer exists. Reload the page.",
  unknown_room: "That room no longer exists. Reload the page.",
  invalid_look: "That look isn't valid.",
  unauthorized: "Only an administrator can change this.",
  unknown_scene: "Home Assistant didn't create the scene in time. Try again in a moment.",
  unknown_track: "That track doesn't exist.",
  invalid_tracks: "Normal must start at a higher light level than Dim.",
};

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function loadPref(key, fallback) {
  try {
    const v = localStorage.getItem("room-routines:" + key);
    return v === null ? fallback : JSON.parse(v);
  } catch (e) {
    return fallback;
  }
}
function savePref(key, value) {
  try {
    localStorage.setItem("room-routines:" + key, JSON.stringify(value));
  } catch (e) {
    /* private window or storage blocked: the view just won't be remembered */
  }
}
const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "");
const hhmmss = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "");
function dayTime(iso) {
  const d = new Date(iso);
  const same = d.toDateString() === new Date().toDateString();
  return same ? hhmmss(iso) : `${d.toLocaleDateString([], { weekday: "short" })} ${hhmmss(iso)}`;
}
function countdown(iso) {
  const s = Math.max(0, Math.round((new Date(iso) - new Date()) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
const minutes = (hm) => {
  const [h, m] = String(hm).split(":").map(Number);
  return h * 60 + m;
};

class RoomRoutinesPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = loadPref("tab", "rooms");
    this._roomId = null; // room detail open
    this._editLook = null; // {room, period, draft}
    this._roomDraft = null; // settings: room being added or changed
    this._periodDraft = null; // settings: period list being changed
    this._tracksDraft = null; // settings: the dark-day sensor being changed
    this._track = loadPref("look-track", "normal"); // which track's looks the room page shows
    this._scenePick = null; // {room, period, track}: choosing a scene for a look
    this._history = {}; // room id -> {hours, data | loading | error}
    this._notice = null;
    this._error = null;
    this._data = null;
    this._ticker = null;
    this._lastLive = 0;
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.hass = hass;
    if (first) {
      this._subscribe();
      this._render();
      return;
    }
    // Lights and sensors change all the time: refresh what's shown, but never
    // while something is being edited, and at most once a second.
    if (this._editing()) return;
    const now = Date.now();
    if (now - this._lastLive > 1000) {
      this._lastLive = now;
      this._render();
    }
  }

  set narrow(v) {
    this._narrow = v;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) mb.narrow = v;
  }

  connectedCallback() {
    this._ticker = setInterval(() => {
      this.shadowRoot.querySelectorAll("[data-deadline]").forEach((el) => {
        el.textContent = countdown(el.dataset.deadline);
      });
    }, 1000);
    if (this._hass && !this._unsub) this._subscribe();
  }

  disconnectedCallback() {
    clearInterval(this._ticker);
    if (this._unsub) {
      this._unsub.then((u) => u()).catch(() => {});
      this._unsub = null;
    }
  }

  _subscribe() {
    if (this._unsub) return;
    this._unsub = this._hass.connection
      .subscribeMessage(
        (data) => {
          this._data = data;
          this._error = null;
          if (!this._editing()) this._render();
        },
        { type: "room_routines/subscribe" }
      )
      .catch((err) => {
        this._error = err.message || String(err);
        this._unsub = null;
        this._render();
      });
  }

  _editing() {
    return !!(this._editLook || this._roomDraft || this._periodDraft || this._tracksDraft || this._scenePick);
  }

  get _admin() {
    return !!this._hass?.user?.is_admin;
  }

  // ---- entity helpers ----

  _name(e) {
    return this._hass.states[e]?.attributes?.friendly_name || e;
  }

  _areaOf(e) {
    const ent = this._hass.entities?.[e];
    if (!ent) return null;
    return ent.area_id || this._hass.devices?.[ent.device_id]?.area_id || null;
  }

  _lightState(e) {
    const s = this._hass.states[e];
    if (!s) return { text: "missing", cls: "bad" };
    if (s.state === "unavailable" || s.state === "unknown") return { text: s.state, cls: "muted" };
    if (s.state !== "on") return { text: "off", cls: "muted" };
    const b = s.attributes.brightness;
    return { text: b == null ? "on" : `on ${Math.round((b / 255) * 100)}%`, cls: "on" };
  }

  _target(t) {
    if (!t) return "not set";
    if (t.on === false) return "off";
    const bits = [t.brightness_pct == null ? "last brightness" : `${Math.round(t.brightness_pct)}%`];
    if (t.color_temp_kelvin) bits.push(`${t.color_temp_kelvin} K`);
    if (t.rgb) bits.push(`<i class="swatch" style="background:rgb(${t.rgb.map(Number).join(",")})"></i>`);
    return "on, " + bits.join(", ");
  }

  _sceneName(scene) {
    const s = this._hass.states[scene];
    return s ? s.attributes.friendly_name || scene : `${scene} (missing)`;
  }

  _sceneLink(scene) {
    const id = this._hass.states[scene]?.attributes?.id;
    const name = esc(this._sceneName(scene));
    return id && this._admin
      ? `<ha-icon icon="mdi:palette"></ha-icon> <a href="/config/scene/edit/${encodeURIComponent(id)}" target="_top" title="Open in Home Assistant's scene editor">${name}</a>`
      : `<ha-icon icon="mdi:palette"></ha-icon> ${name}`;
  }

  _lookText(look, lights) {
    if (!look) return "—";
    if (look.nothing) return "Kept dark";
    if (look.scene) return `Scene: ${esc(this._sceneName(look.scene))}`;
    const parts = lights.map((l) => {
      const t = look.lights?.[l];
      return t ? `${esc(this._name(l))}: ${this._target(t)}` : null;
    });
    return parts.filter(Boolean).join(" · ") || "No lights set";
  }

  _periodColour(name) {
    const order = this._data?.house?.order || [];
    const i = Math.max(0, order.indexOf(name));
    return PERIOD_COLOURS[i % PERIOD_COLOURS.length];
  }

  // ---- rendering ----

  _render() {
    if (!this._hass) return;
    const d = this._data;
    const admin = this._admin;
    if (this._tab === "settings" && !admin) this._tab = "rooms";
    const house = d?.house;
    this.shadowRoot.innerHTML = `
      <style>${STYLES}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">Room Routines</div>
        ${house ? this._headerNow(house) : ""}
      </div>
      <div class="tabs">
        <button class="tab ${this._tab === "rooms" ? "on" : ""}" data-tab="rooms">Rooms</button>
        <button class="tab ${this._tab === "day" ? "on" : ""}" data-tab="day">Your day</button>
        ${admin ? `<button class="tab ${this._tab === "settings" ? "on" : ""}" data-tab="settings">Settings</button>` : ""}
      </div>
      <div class="content">
        ${d && d.version && d.version !== PANEL_VERSION ? `<div class="card warn">Room Routines was updated to ${esc(d.version)}, but this page is still the old version (${PANEL_VERSION}). <button class="btn small" data-action="reload">Reload the page</button></div>` : ""}
        ${this._error ? `<div class="card warn">Couldn't load Room Routines: ${esc(this._error)}</div>` : ""}
        ${!d && !this._error ? `<div class="card">Loading…</div>` : ""}
        ${d && !d.set_up ? `<div class="card">Room Routines isn't set up. Add it under Settings → Devices &amp; services.</div>` : ""}
        ${house?.stealth ? this._stealthBanner() : ""}
        ${this._notice ? `<div class="card notice ${this._notice.bad ? "warn" : ""}">${esc(this._notice.text)}</div>` : ""}
        ${d?.set_up ? this._body(d) : ""}
      </div>`;
    const mb = this.shadowRoot.querySelector("ha-menu-button");
    if (mb) {
      mb.hass = this._hass;
      mb.narrow = this._narrow;
    }
    this._bind();
  }

  _headerNow(house) {
    const held = house.overridden ? " · held by hand" : "";
    return `<div class="now-period" title="The period now. It changes to ${esc(house.next_period)} at ${esc(hhmm(house.next_start))}.">
        <span class="dot" style="background:${this._periodColour(house.period)}"></span>
        <span>${esc(house.period)}</span><span class="until">until ${esc(hhmm(house.next_start))}${held}</span>
      </div>
      ${house.tracks?.enabled ? `<div class="now-track" title="${esc(this._trackTitle(house))}"><ha-icon icon="${TRACK_ICON[house.track]}"></ha-icon><span>${esc(TRACK_LABEL[house.track])}</span></div>` : ""}
      <label class="stealth-toggle" title="Stealth mode: every motion sensor reads as nobody here. Lit rooms still go dark.">
        <ha-icon icon="${house.stealth ? "mdi:incognito" : "mdi:incognito-off"}"></ha-icon>
        <input type="checkbox" role="switch" data-action="stealth" ${house.stealth ? "checked" : ""} aria-label="Stealth mode">
      </label>`;
  }

  _trackTitle(house) {
    const t = house.tracks;
    const level = t.level == null ? "light level unknown" : `${t.level} lx`;
    const by = house.track_by_hand ? " Chosen by hand until the next period." : "";
    return `${TRACK_LABEL[house.track]} day: ${level}. Dim below ${t.dim_below} lx, Normal again above ${t.normal_above} lx.${by}`;
  }

  _stealthBanner() {
    return `<div class="card stealth">
      <ha-icon icon="mdi:incognito"></ha-icon>
      <div><b>Stealth mode is on.</b> Motion is being ignored in every room, so no lights come on by themselves. Rooms that are lit still go dark as normal.</div>
      <button class="btn small" data-action="stealth-off">Turn off</button>
    </div>`;
  }

  _body(d) {
    if (this._tab === "day") return this._day(d);
    if (this._tab === "settings") return this._settings(d);
    const room = this._roomId && d.rooms.find((r) => r.id === this._roomId);
    if (room) return this._detail(d, room);
    this._roomId = null;
    return this._rooms(d);
  }

  // ---- rooms ----

  _rooms(d) {
    if (!d.rooms.length) {
      return `<div class="card">No rooms yet.${this._admin ? ` <button class="btn small" data-action="add-room">Add a room</button>` : ""}</div>`;
    }
    return `
      <div class="period-bar card">
        <div>Now: <b>${esc(d.house.period)}</b>. Next: <b>${esc(d.house.next_period)}</b> at ${esc(hhmm(d.house.next_start))}.</div>
        <label class="inline">Hold a period until the next one starts <select data-action="hold">
          ${d.house.order.map((p) => `<option value="${esc(p)}" ${p === d.house.period ? "selected" : ""}>${esc(p)}</option>`).join("")}
        </select></label>
        ${d.house.tracks?.enabled ? `<label class="inline">Today is <select data-action="hold-track" title="${esc(this._trackTitle(d.house))}">
          ${Object.entries(TRACK_LABEL).map(([k, v]) => `<option value="${k}" ${k === d.house.track ? "selected" : ""}>${v}</option>`).join("")}
        </select>${d.house.track_by_hand ? ` <small class="muted">(by hand until the next period)</small>` : ""}</label>` : ""}
      </div>
      <div class="grid">${d.rooms.map((r) => this._card(d, r)).join("")}</div>`;
  }

  _statusLine(room) {
    const bits = [`<b>${esc(STATE_LABEL[room.state] || room.state || "Starting")}</b>`];
    if (room.mode === "log_only") bits.push(`<span class="muted">(log only: switching nothing)</span>`);
    if (room.lights_off_at) bits.push(`off in <span data-deadline="${esc(room.lights_off_at)}">${countdown(room.lights_off_at)}</span>`);
    if (room.cooldown_until && new Date(room.cooldown_until) > new Date()) {
      bits.push(`ignoring motion until ${esc(hhmmss(room.cooldown_until))}`);
    }
    return bits.join(" · ");
  }

  _luxLine(room) {
    const t = room.settings.threshold_lux;
    if (!room.settings.lux_sensor) return t == null ? "" : "No light-level sensor: switches on at any light level";
    if (room.ambient == null) return `Light level unknown, so it counts as dark${t != null ? ` (switches on below ${t} lx)` : ""}`;
    if (t == null) return `${Math.round(room.ambient)} lx · switches on at any light level`;
    return room.dark_enough
      ? `${Math.round(room.ambient)} lx · dark enough (switches on below ${t} lx)`
      : `<span class="warntext">${Math.round(room.ambient)} lx · too bright (switches on below ${t} lx)</span>`;
  }

  _sensorDots(room) {
    const all = [...room.settings.triggers.map((s) => [s, "switches on"]), ...room.settings.holds.map((s) => [s, "keeps on"])];
    return all
      .map(([s, role]) => {
        const on = this._hass.states[s]?.state === "on";
        return `<span class="sensor ${on ? "on" : ""}" title="${esc(this._name(s))}: ${role}${on ? ", sees someone" : ""}"><i></i>${esc(this._name(s))}</span>`;
      })
      .join("");
  }

  _lightChips(room) {
    return room.settings.lights
      .map((l) => {
        const s = this._lightState(l);
        return `<span class="light ${s.cls}"><ha-icon icon="mdi:lightbulb${s.cls === "on" ? "" : "-outline"}"></ha-icon>${esc(this._name(l))} <small>${esc(s.text)}</small></span>`;
      })
      .join("");
  }

  _modePill(room) {
    if (!this._admin) return `<span class="pill mode-${esc(room.mode)}">${esc(MODE_LABEL[room.mode] || room.mode)}</span>`;
    return `<select class="pill mode-${esc(room.mode)}" data-action="mode" data-room="${esc(room.id)}" title="${esc(MODE_HELP[room.mode] || "")}" aria-label="Mode for ${esc(room.name)}">
      ${Object.entries(MODE_LABEL).map(([k, v]) => `<option value="${k}" ${k === room.mode ? "selected" : ""}>${v}</option>`).join("")}
    </select>`;
  }

  _card(d, room) {
    const from = room.look_period && room.look_period !== d.house.period ? ` (uses ${esc(room.look_period)}'s)` : "";
    return `<div class="card room" data-open="${esc(room.id)}" tabindex="0" role="button" aria-label="Open ${esc(room.name)}">
      <div class="roomhead">
        <div><div class="roomname">${esc(room.name)}</div><div class="meta">${esc(room.area_name || "No area")}</div></div>
        <div data-stop>${this._modePill(room)}</div>
      </div>
      <div class="status">${this._statusLine(room)}</div>
      ${room.reason ? `<div class="meta reason">${esc(room.reason)}</div>` : ""}
      <div class="meta">${this._luxLine(room)}</div>
      <div class="chips">${this._sensorDots(room)}</div>
      <div class="chips">${this._lightChips(room)}</div>
      <div class="meta look"><b>${esc(d.house.period)}${room.look_track === "dim" ? " Dim" : ""} look${from}:</b> ${this._lookText(room.current_look, room.settings.lights)}</div>
      ${room.suggestions?.length ? `<div class="meta hint"><ha-icon icon="mdi:lightbulb-on-outline"></ha-icon> ${room.suggestions.length === 1 ? "A suggestion" : `${room.suggestions.length} suggestions`} from how the lights get changed</div>` : ""}
    </div>`;
  }

  // ---- room detail ----

  _detail(d, room) {
    const order = d.house.order;
    const lights = room.settings.lights;
    const track = this._track;
    const dim = track === "dim";
    const table = dim ? room.dim_looks || {} : room.looks;
    const rows = order
      .map((p) => {
        const own = table[p];
        const editing = this._editLook && this._editLook.room === room.id && this._editLook.period === p && this._editLook.track === track;
        const picking = this._scenePick && this._scenePick.room === room.id && this._scenePick.period === p && this._scenePick.track === track;
        const now = p === d.house.period && d.house.track === track;
        let cells;
        if (editing) cells = `<td colspan="${lights.length}">${this._lookEditor(room)}</td>`;
        else if (picking) cells = `<td colspan="${lights.length}">${this._scenePicker(room, own)}</td>`;
        else if (own?.nothing) cells = `<td colspan="${lights.length}" class="muted">Kept dark</td>`;
        else if (own?.scene) cells = `<td colspan="${lights.length}">${this._sceneLink(own.scene)}</td>`;
        else if (own) cells = lights.map((l) => `<td>${this._target(own.lights?.[l])}</td>`).join("");
        else if (dim) {
          cells = `<td colspan="${lights.length}" class="muted">${room.looks[p] ? "Uses its Normal look" : `Uses ${esc(this._borrowedFrom(order, room.looks, p) || "—")}'s Normal look`}</td>`;
        } else {
          const borrowed = this._borrowedFrom(order, room.looks, p);
          cells = lights.map((l) => `<td class="muted">${borrowed ? `as ${esc(borrowed)}` : "—"}</td>`).join("");
        }
        return `<tr class="${now ? "nowrow" : ""}">
          <th><span class="dot" style="background:${this._periodColour(p)}"></span>${esc(p)}${now ? ` <small>now</small>` : ""}</th>
          ${cells}
          <td class="actions">${editing || picking ? "" : this._lookActions(room, p, own)}</td>
        </tr>`;
      })
      .join("");
    const trackTabs = `<div class="subtabs">
      ${Object.entries(TRACK_LABEL).map(([k, v]) => `<button class="subtab ${k === track ? "on" : ""}" data-action="look-track" data-track="${k}"><ha-icon icon="${TRACK_ICON[k]}"></ha-icon> ${v} days</button>`).join("")}
    </div>`;
    const trackHelp = dim
      ? `<p class="explain">Dim looks are used on dark days${d.house.tracks?.enabled ? ` (the light sensor below ${esc(d.house.tracks.dim_below)} lx)` : ", once a light sensor is chosen under Settings → Dark days"}. A period without a Dim look uses its Normal one, so only set the ones that should differ.</p>`
      : `<p class="explain">What the lights do when someone arrives, in each period. A period without its own look uses the one before it. Set the lights how you want them and press <b>Save as now</b>: it becomes a Home Assistant scene you can also use on wall buttons. Or pick a scene you already have, or edit a look by hand.</p>`;
    const s = room.settings;
    return `
      <div class="detailhead">
        <button class="btn small" data-action="back"><ha-icon icon="mdi:arrow-left"></ha-icon> All rooms</button>
      </div>
      <div class="card">
        <div class="roomhead">
          <div><div class="roomname big">${esc(room.name)}</div><div class="meta">${esc(room.area_name || "No area")}</div></div>
          <div>${this._modePill(room)}</div>
        </div>
        <div class="status">${this._statusLine(room)}</div>
        ${room.reason ? `<div class="meta reason">${esc(room.reason)}</div>` : ""}
        <div class="meta">${this._luxLine(room)}</div>
        <div class="chips">${this._sensorDots(room)}</div>
        <div class="chips">${this._lightChips(room)}</div>
      </div>
      ${this._suggestionsSection(room)}
      <h2>Looks</h2>
      <div class="card">
        ${trackTabs}
        ${trackHelp}
        <div class="tablewrap"><table class="looks">
          <tr><th>Period</th>${lights.map((l) => `<th>${esc(this._name(l))}</th>`).join("")}<th></th></tr>
          ${rows}
        </table></div>
      </div>
      ${this._historySection(room)}
      <h2>How this room works</h2>
      <div class="card settings-summary">
        <div>Switches on when ${s.triggers.map((t) => `<b>${esc(this._name(t))}</b>`).join(" or ")} sees someone${s.threshold_lux != null && s.lux_sensor ? ` and it's darker than <b>${esc(s.threshold_lux)} lx</b>` : ""}.</div>
        ${s.holds.length ? `<div>Kept on while ${s.holds.map((t) => `<b>${esc(this._name(t))}</b>`).join(" or ")} sees someone.</div>` : ""}
        <div>Goes dark <b>${esc(s.timeout_s)} s</b> after the room empties, fading out over <b>${esc(s.fade_out_s)} s</b>.</div>
        <div>After a light is switched off by hand, motion is ignored for <b>${esc(s.cooldown_s)} s</b>.</div>
        <div>When the period changes in a lit room, the lights move to the new look over <b>${esc(s.drift_s)} s</b>.</div>
        ${this._admin ? `<div class="row"><button class="btn small" data-action="edit-room" data-room="${esc(room.id)}">Change these settings</button></div>` : ""}
      </div>`;
  }

  _borrowedFrom(order, looks, period) {
    let i = order.indexOf(period);
    for (let n = 0; n < order.length; n++) {
      i = (i - 1 + order.length) % order.length;
      if (looks[order[i]]) return order[i];
    }
    return null;
  }

  _lookActions(room, period, own) {
    const r = esc(room.id);
    const p = esc(period);
    const dim = this._track === "dim";
    const save = this._admin
      ? `<button class="btn tiny" data-action="look-scene-now" data-room="${r}" data-period="${p}" title="Save the lights as they are now as a Home Assistant scene, and use it for this look">Save as now</button>`
      : `<button class="btn tiny" data-action="look-current" data-room="${r}" data-period="${p}" title="Save the lights as they are now as this look">Save as now</button>`;
    return `<div class="actionrow">
      ${save}
      <button class="btn tiny" data-action="look-pick-scene" data-room="${r}" data-period="${p}" title="Use a scene you already have">Pick a scene</button>
      <button class="btn tiny" data-action="look-edit" data-room="${r}" data-period="${p}" title="Set each light by hand">Edit</button>
      <button class="btn tiny" data-action="look-nothing" data-room="${r}" data-period="${p}" title="Keep the room dark in this period">Keep dark</button>
      ${own ? `<button class="btn tiny" data-action="look-borrow" data-room="${r}" data-period="${p}" title="${dim ? "Remove this Dim look: the period uses its Normal one" : "Remove this period's own look: it uses the previous period's"}">${dim ? "Use Normal" : "Use previous"}</button>` : ""}
    </div>`;
  }

  _scenePicker(room, own) {
    const scenes = Object.keys(this._hass.states)
      .filter((e) => e.startsWith("scene."))
      .sort((a, b) => this._name(a).localeCompare(this._name(b)));
    const inArea = (e) => room.area_id && this._areaOf(e) === room.area_id;
    const ordered = [...scenes.filter(inArea), ...scenes.filter((e) => !inArea(e))];
    return `<div class="lookeditor">
      <label class="inline">Scene <select data-scene-choice>
        ${ordered.length ? ordered.map((e) => `<option value="${esc(e)}" ${own?.scene === e ? "selected" : ""}>${esc(this._name(e))}${inArea(e) ? " (in this area)" : ""}</option>`).join("") : `<option value="">No scenes yet</option>`}
      </select></label>
      <div class="row">
        <button class="btn primary small" data-action="scene-pick-save" ${ordered.length ? "" : "disabled"}>Use it</button>
        <button class="btn small" data-action="scene-pick-cancel">Cancel</button>
      </div>
    </div>`;
  }

  _suggestionsSection(room) {
    if (!room.suggestions?.length) return "";
    const cards = room.suggestions
      .map((s) => {
        const act = {
          keep_dark: "Keep it dark",
          save_look: this._admin ? "Save it as the look" : "",
          start_earlier: this._admin ? `Start ${esc(s.move_period)} at ${esc(s.new_start)}` : "",
          start_later: this._admin ? `Start ${esc(s.move_period)} at ${esc(s.new_start)}` : "",
        }[s.kind];
        return `<div class="suggestion"><ha-icon icon="mdi:lightbulb-on-outline"></ha-icon><div class="grow">${esc(s.text)}</div>
          ${act ? `<button class="btn tiny primary" data-action="suggestion-apply" data-room="${esc(room.id)}" data-key="${esc(s.key)}">${act}</button>` : ""}
          <button class="btn tiny" data-action="suggestion-dismiss" data-key="${esc(s.key)}" title="Hide this for four weeks">Not now</button></div>`;
      })
      .join("");
    return `<h2>Suggestions</h2><div class="card">${cards}
      <p class="explain">From changes made by hand to lights this room switched on (Live rooms only), over the last four weeks.</p></div>`;
  }

  _lookEditor(room) {
    const draft = this._editLook.draft;
    const rows = room.settings.lights
      .map((l) => {
        const t = draft.lights[l] || { mode: "skip" };
        const lk = esc(l);
        return `<div class="lookrow">
          <div class="lname">${esc(this._name(l))}</div>
          <select data-look-light="${lk}" data-field="mode" aria-label="${esc(this._name(l))}">
            <option value="skip" ${t.mode === "skip" ? "selected" : ""}>Leave as it is</option>
            <option value="off" ${t.mode === "off" ? "selected" : ""}>Off</option>
            <option value="last" ${t.mode === "last" ? "selected" : ""}>On, last brightness</option>
            <option value="set" ${t.mode === "set" ? "selected" : ""}>On, at a brightness</option>
          </select>
          ${t.mode === "set" ? `<label class="inline">Brightness <input type="range" min="1" max="100" value="${t.brightness_pct ?? 50}" data-look-light="${lk}" data-field="brightness_pct"> <span class="val">${t.brightness_pct ?? 50}%</span></label>` : ""}
          ${t.mode === "set" || t.mode === "last" ? `<label class="inline">Colour temperature <input type="number" min="1500" max="9000" step="100" placeholder="as it is" value="${t.color_temp_kelvin ?? ""}" data-look-light="${lk}" data-field="color_temp_kelvin"> K</label>` : ""}
        </div>`;
      })
      .join("");
    return `<div class="lookeditor">
      <label class="inline"><input type="checkbox" data-look-nothing ${draft.nothing ? "checked" : ""}> Keep the room dark in this period</label>
      ${draft.nothing ? "" : rows}
      <div class="row">
        <button class="btn primary small" data-action="look-save">Save</button>
        <button class="btn small" data-action="look-cancel">Cancel</button>
      </div>
    </div>`;
  }

  _startLookEdit(room, period) {
    const own = (this._track === "dim" ? room.dim_looks || {} : room.looks)[period];
    const lights = {};
    for (const l of room.settings.lights) {
      const t = own?.lights?.[l];
      if (!t) lights[l] = { mode: "skip" };
      else if (t.on === false) lights[l] = { mode: "off" };
      else if (t.brightness_pct == null) lights[l] = { mode: "last", color_temp_kelvin: t.color_temp_kelvin, rgb: t.rgb };
      else lights[l] = { mode: "set", brightness_pct: Math.round(t.brightness_pct), color_temp_kelvin: t.color_temp_kelvin, rgb: t.rgb };
    }
    this._editLook = { room: room.id, period, track: this._track, draft: { nothing: !!own?.nothing, lights } };
  }

  _lookFromDraft(draft) {
    if (draft.nothing) return { nothing: true };
    const lights = {};
    for (const [l, t] of Object.entries(draft.lights)) {
      if (t.mode === "skip") continue;
      if (t.mode === "off") {
        lights[l] = { on: false };
        continue;
      }
      const out = { on: true };
      if (t.mode === "set") out.brightness_pct = Number(t.brightness_pct ?? 50);
      if (t.color_temp_kelvin) out.color_temp_kelvin = Number(t.color_temp_kelvin);
      else if (t.rgb) out.rgb = t.rgb;
      lights[l] = out;
    }
    return { lights };
  }

  // ---- history and the dry-run check ----

  _historySection(room) {
    const h = this._history[room.id];
    const hours = h?.hours ?? loadPref("hours", 72);
    const choose = `<select data-action="history-hours" data-room="${esc(room.id)}" aria-label="How far back">
      ${[24, 72, 168, 336].map((n) => `<option value="${n}" ${n === hours ? "selected" : ""}>${n < 48 ? `${n} hours` : `${n / 24} days`}</option>`).join("")}
    </select>`;
    let body;
    if (!h || h.loading) body = `<div class="meta">Loading history…</div>`;
    else if (h.error) body = `<div class="meta bad">Couldn't read the history: ${esc(h.error)}</div>`;
    else body = this._parity(h.data) + this._activity(h.data);
    if (!h) setTimeout(() => this._loadHistory(room.id, hours), 0);
    return `<h2>What it did</h2><div class="card"><div class="row spread"><span class="meta">From Home Assistant's history, last ${choose}</span>
      <button class="btn tiny" data-action="history-refresh" data-room="${esc(room.id)}">Refresh</button></div>${body}</div>`;
  }

  _parity(data) {
    if (!data.log_only.length) {
      return `<p class="explain">The dry-run check appears here for any time the room spends in <b>Log only</b>: it compares what the room would have done with what its lights really did.</p>`;
    }
    const p = data.parity;
    if (!p.total) {
      return `<div class="parity"><b>Dry-run check:</b> no switching to compare yet in the log-only time.</div>`;
    }
    const pct = Math.round((p.matched / p.total) * 100);
    const rows = p.pairs
      .slice()
      .reverse()
      .slice(0, 60)
      .map(
        (x) => `<tr class="${x.routine_at && x.light_at ? "" : "diff"}">
          <td>${esc(dayTime(x.routine_at || x.light_at))}</td>
          <td>${x.routine_at ? `Would switch ${x.on ? "on" : "off"}` : `<span class="muted">wouldn't have</span>`}</td>
          <td>${x.light_at ? `Switched ${x.on ? "on" : "off"}${x.routine_at ? "" : ` at ${esc(hhmmss(x.light_at))}`}` : `<span class="muted">didn't</span>`}</td>
          <td>${x.delta_s == null ? "" : `${x.delta_s > 0 ? "+" : ""}${x.delta_s} s`}</td>
          <td>${x.routine_at && x.light_at ? "✓" : "✗"}</td>
        </tr>`
      )
      .join("");
    return `<div class="parity">
      <div class="score ${pct >= 95 ? "good" : pct >= 80 ? "ok" : "badscore"}"><b>${p.matched} of ${p.total}</b> switches matched (${pct}%)</div>
      <p class="explain">In <b>Log only</b> the room switches nothing and the sensor's own link still does. Each time the room would have switched is paired with the light's real switch within 20 seconds. A few differences are normal, such as someone switching a light by hand. Many differences mean the room isn't ready to go live.</p>
      <div class="tablewrap"><table class="pairs"><tr><th>When</th><th>Room Routines</th><th>The light</th><th>Gap</th><th></th></tr>${rows}</table></div>
    </div>`;
  }

  _activity(data) {
    const items = [
      ...data.status.map((s) => ({ at: s.at, text: `${STATE_LABEL[s.state] || s.state}${s.reason ? ` — ${s.reason}` : ""}`, kind: "room" })),
      ...Object.entries(data.lights).flatMap(([l, rows]) =>
        rows.slice(1).map((r) => ({ at: r.at, text: `${this._name(l)} ${r.on === null ? "unavailable" : r.on ? "on" : "off"}`, kind: "light" }))
      ),
      ...data.modes.slice(1).map((m) => ({ at: m.at, text: `Mode: ${MODE_LABEL[m.mode] || m.mode}`, kind: "mode" })),
    ]
      .sort((a, b) => new Date(b.at) - new Date(a.at))
      .slice(0, 80);
    if (!items.length) return `<div class="meta">Nothing yet.</div>`;
    return `<details ${loadPref("activity-open", false) ? "open" : ""} data-pref="activity-open"><summary>Everything, newest first</summary>
      <ul class="activity">${items.map((i) => `<li class="${i.kind}"><span class="t">${esc(dayTime(i.at))}</span> ${esc(i.text)}</li>`).join("")}</ul></details>`;
  }

  async _loadHistory(roomId, hours) {
    this._history[roomId] = { hours, loading: true };
    try {
      const data = await this._hass.callWS({ type: "room_routines/history", room_id: roomId, hours });
      this._history[roomId] = { hours, data };
    } catch (err) {
      this._history[roomId] = { hours, error: err.message || String(err) };
    }
    if (!this._editing()) this._render();
  }

  // ---- your day ----

  _day(d) {
    const periods = d.house.periods;
    const bands = (alt) => {
      const starts = periods
        .map((p) => ({ name: p.name, m: minutes(alt && p.alt_start ? p.alt_start : p.start) }))
        .sort((a, b) => a.m - b.m);
      const out = [];
      // The last period of the day runs on past midnight into the first.
      const lastName = starts[starts.length - 1].name;
      if (starts[0].m > 0) out.push({ name: lastName, from: 0, to: starts[0].m });
      starts.forEach((s, i) => out.push({ name: s.name, from: s.m, to: i + 1 < starts.length ? starts[i + 1].m : 1440 }));
      return out;
    };
    const now = new Date();
    const nowM = now.getHours() * 60 + now.getMinutes();
    const todayAlt = d.house.alt_days.includes((now.getDay() + 6) % 7);
    const bar = (alt, label) => `
      <div class="daylabel">${esc(label)}</div>
      <div class="daybar">
        ${bands(alt).map((b) => `<div class="band" style="left:${(b.from / 1440) * 100}%;width:${((b.to - b.from) / 1440) * 100}%;background:${this._periodColour(b.name)}" title="${esc(b.name)}"><span>${b.to - b.from > 70 ? esc(b.name) : ""}</span></div>`).join("")}
        ${alt === todayAlt ? `<div class="nowline" style="left:${(nowM / 1440) * 100}%" title="Now"></div>` : ""}
      </div>`;
    const hours = [0, 3, 6, 9, 12, 15, 18, 21, 24].map((h) => `<span style="left:${(h / 24) * 100}%">${String(h).padStart(2, "0")}</span>`).join("");
    const altNames = d.house.alt_days.map((i) => WEEKDAYS[i]).join(", ");
    const t = d.house.tracks;
    const trackCard = t?.enabled
      ? `<div class="card"><ha-icon icon="${TRACK_ICON[d.house.track]}"></ha-icon> Today is a <b>${esc(TRACK_LABEL[d.house.track])}</b> day. ${esc(this._trackTitle(d.house))}${t.level_sensor ? ` Measured by ${esc(this._name(t.level_sensor))}.` : ""}</div>`
      : "";
    return `
      ${trackCard}
      <div class="card">
        ${bar(false, d.house.alt_days.length ? "Most days" : "Every day")}
        ${d.house.alt_days.length ? bar(true, altNames) : ""}
        <div class="hours">${hours}</div>
      </div>
      <div class="card">
        <table class="plain">
          <tr><th>Period</th><th>Starts</th>${d.house.alt_days.length ? `<th>On ${esc(altNames)}</th>` : ""}</tr>
          ${d.house.order
            .map((name) => {
              const p = periods.find((x) => x.name === name);
              return `<tr class="${name === d.house.period ? "nowrow" : ""}"><td><span class="dot" style="background:${this._periodColour(name)}"></span>${esc(name)}</td><td>${esc(p.start)}</td>${d.house.alt_days.length ? `<td>${esc(p.alt_start || p.start)}</td>` : ""}</tr>`;
            })
            .join("")}
        </table>
        ${this._admin ? `<div class="row"><button class="btn small" data-action="edit-periods">Change the periods</button></div>` : ""}
      </div>`;
  }

  // ---- settings (admins) ----

  _settings(d) {
    if (this._roomDraft) return this._roomForm();
    if (this._periodDraft) return this._periodForm();
    if (this._tracksDraft) return this._tracksForm();
    return `
      <h2>Rooms</h2>
      <div class="card">
        ${d.rooms.length ? `<table class="plain">${d.rooms.map((r) => `<tr><td><b>${esc(r.name)}</b><div class="meta">${esc(r.area_name || "No area")} · ${esc(MODE_LABEL[r.mode] || r.mode)} · ${r.settings.lights.length} light${r.settings.lights.length === 1 ? "" : "s"}</div></td>
          <td class="actions"><button class="btn tiny" data-action="edit-room" data-room="${esc(r.id)}">Change</button> <button class="btn tiny danger" data-action="remove-room" data-room="${esc(r.id)}">Remove</button></td></tr>`).join("")}</table>` : `<div class="meta">No rooms yet.</div>`}
        <div class="row"><button class="btn primary small" data-action="add-room">Add a room</button></div>
      </div>
      <h2>Periods</h2>
      <div class="card">
        <div>${d.house.order.map((p) => `<span class="chip" style="--c:${this._periodColour(p)}">${esc(p)} ${esc(d.house.periods.find((x) => x.name === p).start)}</span>`).join(" ")}</div>
        <div class="row"><button class="btn small" data-action="edit-periods">Change the periods</button></div>
      </div>
      <h2>Dark days</h2>
      <div class="card">
        ${d.house.tracks?.enabled
          ? `<div>Light sensor: <b>${esc(this._name(d.house.tracks.sensor || d.house.tracks.fallback))}</b>${d.house.tracks.sensor && d.house.tracks.fallback ? `, or <b>${esc(this._name(d.house.tracks.fallback))}</b> when it's unavailable` : ""}. Dim below <b>${esc(d.house.tracks.dim_below)} lx</b>, Normal again above <b>${esc(d.house.tracks.normal_above)} lx</b>. Now: ${d.house.tracks.level == null ? "unknown" : `${esc(d.house.tracks.level)} lx`}, a ${esc(TRACK_LABEL[d.house.track])} day.</div>`
          : `<div class="meta">Off: every room uses its Normal looks. Choose a light sensor that sees the daylight to use Dim looks on dark days.</div>`}
        <div class="row"><button class="btn small" data-action="edit-tracks">Change</button></div>
      </div>
      <p class="explain">Settings are only shown to administrators. Everyone who can open this page can see the rooms and change their looks.</p>`;
  }

  _startRoomDraft(room) {
    if (room) {
      this._roomDraft = { id: room.id, name: room.name, area_id: room.area_id, mode: room.mode, ...JSON.parse(JSON.stringify(room.settings)) };
    } else {
      this._roomDraft = { id: null, name: "", area_id: null, lights: [], triggers: [], holds: [], lux_sensor: null, self_fading: [], ...ROOM_DEFAULTS };
    }
    this._roomDraft.search = {};
    this._roomError = null;
  }

  _roomForm() {
    const r = this._roomDraft;
    const areas = Object.values(this._hass.areas || {}).sort((a, b) => a.name.localeCompare(b.name));
    const num = (key, label, unit, help, max) => `<label>${label}
      <span class="inputunit"><input type="number" min="0" max="${max}" step="1" value="${r[key] ?? ""}" data-room-field="${key}" ${key === "threshold_lux" ? `placeholder="any"` : ""}> ${unit}</span>
      ${help ? `<span class="help">${help}</span>` : ""}</label>`;
    const luxOptions = Object.keys(this._hass.states)
      .filter((e) => e.startsWith("sensor."))
      .sort((a, b) => {
        const la = this._hass.states[a].attributes.device_class === "illuminance" ? 0 : 1;
        const lb = this._hass.states[b].attributes.device_class === "illuminance" ? 0 : 1;
        return la - lb || this._name(a).localeCompare(this._name(b));
      });
    return `
      <div class="detailhead"><button class="btn small" data-action="room-cancel"><ha-icon icon="mdi:arrow-left"></ha-icon> Back</button></div>
      <div class="card form">
        <h2 class="inline">${r.id ? `Change ${esc(r.name)}` : "Add a room"}</h2>
        ${this._roomError ? `<div class="warntext">${esc(this._roomError)}</div>` : ""}
        <label>Name <input type="text" value="${esc(r.name)}" data-room-field="name"></label>
        <label>Area <select data-room-field="area_id"><option value="">No area</option>
          ${areas.map((a) => `<option value="${esc(a.area_id)}" ${a.area_id === r.area_id ? "selected" : ""}>${esc(a.name)}</option>`).join("")}
        </select><span class="help">Used to suggest lights and sensors below, and to show the room's status under that area.</span></label>
        ${this._picker("lights", "Lights", "light", "The lights this room switches.")}
        ${this._picker("triggers", "Sensors that switch the lights on", "binary_sensor", "Any motion or presence sensor. When one of these sees someone and it's dark enough, the lights come on.")}
        ${this._picker("holds", "Sensors that only keep the lights on", "binary_sensor", "Optional. These never switch the lights on, but keep them on while they see someone.")}
        <label>Light-level sensor <select data-room-field="lux_sensor"><option value="">None</option>
          ${luxOptions.map((e) => `<option value="${esc(e)}" ${e === r.lux_sensor ? "selected" : ""}>${esc(this._name(e))}${r.area_id && this._areaOf(e) === r.area_id ? " (in this area)" : ""}</option>`).join("")}
        </select><span class="help">Optional. Only read while the room's lights are off.</span></label>
        <div class="twocol">
          ${num("threshold_lux", "Switch on below", "lx", "Leave empty to switch on at any light level.", 100000)}
          ${num("timeout_s", "Go dark after the room is empty for", "s", "", 7200)}
          ${num("fade_out_s", "Fade out over", "s", "0 switches straight off.", 300)}
          ${num("cooldown_s", "After a hand switch-off, ignore motion for", "s", "", 600)}
          ${num("drift_s", "When the period changes, move to the new look over", "s", "Only for a room that is lit when the period changes.", 600)}
        </div>
        ${r.lights.length ? `<fieldset><legend>Lights that fade by themselves</legend>
          <span class="help">For lights whose own device fades them, such as a DALI gateway set to dim to off over a few seconds. They get a plain on or off.</span><div>
          ${r.lights.map((l) => `<label class="inline"><input type="checkbox" data-self-fading="${esc(l)}" ${r.self_fading.includes(l) ? "checked" : ""}> ${esc(this._name(l))}</label>`).join("")}
        </div></fieldset>` : ""}
        ${r.id ? `<label>Mode <select data-room-field="mode">${Object.entries(MODE_LABEL).map(([k, v]) => `<option value="${k}" ${k === r.mode ? "selected" : ""}>${v}</option>`).join("")}</select>
          <span class="help">${esc(MODE_HELP[r.mode] || "")}</span></label>` : `<p class="help">A new room starts in Log only, with every light on at its last brightness in every period.</p>`}
        <div class="row">
          <button class="btn primary" data-action="room-save">${r.id ? "Save" : "Add the room"}</button>
          <button class="btn" data-action="room-cancel">Cancel</button>
        </div>
      </div>`;
  }

  // A searchable list of entities of one domain. The area's own are suggested
  // first, but any entity can be chosen.
  _picker(key, label, domain, help) {
    const r = this._roomDraft;
    const selected = r[key];
    const query = (r.search[key] || "").toLowerCase();
    const inArea = (e) => r.area_id && this._areaOf(e) === r.area_id;
    const hidden = (e) => this._hass.entities?.[e]?.hidden;
    const matches = Object.keys(this._hass.states)
      .filter((e) => e.startsWith(domain + ".") && !selected.includes(e))
      .filter((e) => (query ? (this._name(e) + " " + e).toLowerCase().includes(query) : inArea(e) && !hidden(e)))
      .sort((a, b) => (inArea(b) ? 1 : 0) - (inArea(a) ? 1 : 0) || this._name(a).localeCompare(this._name(b)))
      .slice(0, 30);
    const empty = query ? "Nothing matches." : r.area_id ? "Nothing else in this area. Search to find more." : "Search to find one.";
    return `<fieldset class="picker"><legend>${esc(label)}</legend>
      <span class="help">${esc(help)}</span>
      <div class="chips">${selected.map((e) => `<span class="chip sel">${esc(this._name(e))} <button class="x" data-pick-remove="${esc(key)}" data-entity="${esc(e)}" aria-label="Remove ${esc(this._name(e))}">×</button></span>`).join("") || `<span class="meta">None chosen</span>`}</div>
      <input type="search" placeholder="Search for ${domain === "light" ? "a light" : "a sensor"}…" value="${esc(r.search[key] || "")}" data-pick-search="${esc(key)}">
      <div class="options">${matches.map((e) => `<button class="option" data-pick-add="${esc(key)}" data-entity="${esc(e)}">${esc(this._name(e))} <small>${esc(e)}${inArea(e) ? " · in this area" : ""}</small></button>`).join("") || `<span class="meta">${empty}</span>`}</div>
    </fieldset>`;
  }

  _startTracksDraft(d) {
    const t = d.house.tracks || {};
    this._tracksDraft = { sensor: t.sensor || null, fallback: t.fallback || null, dim_below: t.dim_below ?? 800, normal_above: t.normal_above ?? 1500 };
    this._tracksError = null;
  }

  _tracksForm() {
    const t = this._tracksDraft;
    const sensors = Object.keys(this._hass.states)
      .filter((e) => e.startsWith("sensor."))
      .sort((a, b) => {
        const la = this._hass.states[a].attributes.device_class === "illuminance" ? 0 : 1;
        const lb = this._hass.states[b].attributes.device_class === "illuminance" ? 0 : 1;
        return la - lb || this._name(a).localeCompare(this._name(b));
      });
    const pick = (key, none) => `<select data-tracks-field="${key}"><option value="">${none}</option>
      ${sensors.map((e) => `<option value="${esc(e)}" ${e === t[key] ? "selected" : ""}>${esc(this._name(e))}</option>`).join("")}</select>`;
    return `
      <div class="detailhead"><button class="btn small" data-action="tracks-cancel"><ha-icon icon="mdi:arrow-left"></ha-icon> Back</button></div>
      <div class="card form">
        <h2 class="inline">Dark days</h2>
        <p class="explain">One light sensor for the whole house decides whether today is a Normal or a Dim day, and rooms use their Dim looks on Dim days. Best is a sensor that sees the sky: outdoors, or indoors facing out of a window. Its level is averaged over 15 minutes, and a day stays Normal or Dim for at least 20 minutes, so passing clouds don't flip it.</p>
        ${this._tracksError ? `<div class="warntext">${esc(this._tracksError)}</div>` : ""}
        <label>Light sensor ${pick("sensor", "None (dark days off)")}</label>
        <label>If it's unavailable, use ${pick("fallback", "Nothing: keep the current day")}</label>
        <div class="twocol">
          <label>Dim below <span class="inputunit"><input type="number" min="0" step="10" value="${esc(t.dim_below)}" data-tracks-field="dim_below"> lx</span></label>
          <label>Normal again above <span class="inputunit"><input type="number" min="0" step="10" value="${esc(t.normal_above)}" data-tracks-field="normal_above"> lx</span>
            <span class="help">Higher than Dim, so the day doesn't flip back and forth around one level.</span></label>
        </div>
        <div class="row">
          <button class="btn primary" data-action="tracks-save">Save</button>
          <button class="btn" data-action="tracks-cancel">Cancel</button>
        </div>
      </div>`;
  }

  _startPeriodDraft(d) {
    this._periodDraft = {
      rows: d.house.order.map((name) => {
        const p = d.house.periods.find((x) => x.name === name);
        return { original: name, name, start: p.start, alt_start: p.alt_start || "" };
      }),
      alt_days: [...d.house.alt_days],
    };
    this._periodError = null;
  }

  _periodForm() {
    const pd = this._periodDraft;
    const alt = pd.alt_days.length > 0;
    return `
      <div class="detailhead"><button class="btn small" data-action="periods-cancel"><ha-icon icon="mdi:arrow-left"></ha-icon> Back</button></div>
      <div class="card form">
        <h2 class="inline">Periods</h2>
        <p class="explain">Each period starts at its time and runs until the next one starts. Rooms keep their looks when a period is renamed. Removing a period removes its looks, and rooms then use the previous period's.</p>
        ${this._periodError ? `<div class="warntext">${esc(this._periodError)}</div>` : ""}
        <fieldset><legend>Days with different start times</legend><div>
          ${WEEKDAYS.map((w, i) => `<label class="inline"><input type="checkbox" data-alt-day="${i}" ${pd.alt_days.includes(i) ? "checked" : ""}> ${w}</label>`).join("")}
        </div></fieldset>
        <table class="plain periods">
          <tr><th>Name</th><th>Starts</th>${alt ? `<th>On those days</th>` : ""}<th></th></tr>
          ${pd.rows
            .map(
              (r, i) => `<tr>
                <td><input type="text" value="${esc(r.name)}" data-period-row="${i}" data-field="name" aria-label="Name"></td>
                <td><input type="time" value="${esc(r.start)}" data-period-row="${i}" data-field="start" aria-label="Starts"></td>
                ${alt ? `<td><input type="time" value="${esc(r.alt_start)}" data-period-row="${i}" data-field="alt_start" aria-label="Starts on those days"></td>` : ""}
                <td>${pd.rows.length > 1 ? `<button class="btn tiny danger" data-action="period-remove" data-row="${i}">Remove</button>` : ""}</td>
              </tr>`
            )
            .join("")}
        </table>
        <div class="row"><button class="btn small" data-action="period-add">Add a period</button></div>
        <div class="row">
          <button class="btn primary" data-action="periods-save">Save</button>
          <button class="btn" data-action="periods-cancel">Cancel</button>
        </div>
      </div>`;
  }

  // ---- events ----

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-tab]").forEach((b) =>
      b.addEventListener("click", () => {
        this._tab = b.dataset.tab;
        savePref("tab", this._tab);
        this._roomId = null;
        this._editLook = this._roomDraft = this._periodDraft = this._tracksDraft = this._scenePick = null;
        this._notice = null;
        this._render();
      })
    );
    root.querySelectorAll("[data-open]").forEach((el) => {
      const open = (ev) => {
        if (ev.target.closest("[data-stop]")) return;
        this._roomId = el.dataset.open;
        this._notice = null;
        this._render();
      };
      el.addEventListener("click", open);
      el.addEventListener("keydown", (ev) => {
        if (ev.target === el && (ev.key === "Enter" || ev.key === " ")) {
          ev.preventDefault();
          open(ev);
        }
      });
    });
    root.querySelectorAll("[data-action]").forEach((el) => {
      const kind = el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "click";
      el.addEventListener(kind, (ev) => this._action(el.dataset.action, el, ev));
    });
    root.querySelectorAll("details[data-pref]").forEach((el) => el.addEventListener("toggle", () => savePref(el.dataset.pref, el.open)));

    // look editor
    root.querySelectorAll("[data-look-light]").forEach((el) => {
      const handler = () => {
        const t = this._editLook.draft.lights[el.dataset.lookLight];
        const f = el.dataset.field;
        if (f === "mode") {
          t.mode = el.value;
          this._render();
        } else if (f === "brightness_pct") {
          t.brightness_pct = Number(el.value);
          const val = el.parentElement.querySelector(".val");
          if (val) val.textContent = `${el.value}%`;
        } else if (f === "color_temp_kelvin") {
          t.color_temp_kelvin = el.value ? Number(el.value) : null;
        }
      };
      el.addEventListener(el.type === "range" ? "input" : "change", handler);
    });
    const nothing = root.querySelector("[data-look-nothing]");
    if (nothing) {
      nothing.addEventListener("change", () => {
        this._editLook.draft.nothing = nothing.checked;
        this._render();
      });
    }

    // room form
    root.querySelectorAll("[data-room-field]").forEach((el) =>
      el.addEventListener("change", () => {
        const f = el.dataset.roomField;
        const r = this._roomDraft;
        if (el.type === "number") r[f] = el.value === "" ? null : Number(el.value);
        else r[f] = el.value || null;
        if (f === "area_id" || f === "mode") this._render();
      })
    );
    root.querySelectorAll("[data-pick-search]").forEach((el) =>
      el.addEventListener("input", () => {
        this._roomDraft.search[el.dataset.pickSearch] = el.value;
        const pos = el.selectionStart;
        this._render();
        const again = this.shadowRoot.querySelector(`[data-pick-search="${el.dataset.pickSearch}"]`);
        if (again) {
          again.focus();
          again.setSelectionRange(pos, pos);
        }
      })
    );
    root.querySelectorAll("[data-pick-add]").forEach((el) =>
      el.addEventListener("click", () => {
        const list = this._roomDraft[el.dataset.pickAdd];
        if (!list.includes(el.dataset.entity)) list.push(el.dataset.entity);
        this._render();
      })
    );
    root.querySelectorAll("[data-pick-remove]").forEach((el) =>
      el.addEventListener("click", () => {
        const key = el.dataset.pickRemove;
        this._roomDraft[key] = this._roomDraft[key].filter((e) => e !== el.dataset.entity);
        if (key === "lights") this._roomDraft.self_fading = this._roomDraft.self_fading.filter((e) => e !== el.dataset.entity);
        this._render();
      })
    );
    root.querySelectorAll("[data-self-fading]").forEach((el) =>
      el.addEventListener("change", () => {
        const e = el.dataset.selfFading;
        const list = this._roomDraft.self_fading.filter((x) => x !== e);
        if (el.checked) list.push(e);
        this._roomDraft.self_fading = list;
      })
    );

    // dark-day form
    root.querySelectorAll("[data-tracks-field]").forEach((el) =>
      el.addEventListener("change", () => {
        const f = el.dataset.tracksField;
        this._tracksDraft[f] = el.type === "number" ? (el.value === "" ? null : Number(el.value)) : el.value || null;
      })
    );

    // period form
    root.querySelectorAll("[data-period-row]").forEach((el) =>
      el.addEventListener("change", () => {
        this._periodDraft.rows[Number(el.dataset.periodRow)][el.dataset.field] = el.value;
      })
    );
    root.querySelectorAll("[data-alt-day]").forEach((el) =>
      el.addEventListener("change", () => {
        const i = Number(el.dataset.altDay);
        const days = this._periodDraft.alt_days.filter((x) => x !== i);
        if (el.checked) days.push(i);
        this._periodDraft.alt_days = days.sort();
        this._render();
      })
    );
  }

  async _ws(msg, done) {
    try {
      const result = await this._hass.callWS(msg);
      if (done) this._notice = { text: done };
      return { ok: true, result };
    } catch (err) {
      return { ok: false, error: ERRORS[err.code] || err.message || String(err) };
    }
  }

  // Saves the room's lights (or the given scene entities) as a Home Assistant scene
  // through the scene editor's own API, then points the look at it. Saving again
  // updates the same scene, keeping anything else added to it in the scene editor.
  // Returns the scene's name, or null (with a notice) if it didn't work.
  async _saveScene(room, period, track, entities) {
    try {
      const draft = await this._hass.callWS({ type: "room_routines/scene_draft", room_id: room.id, period, track });
      if (!entities && !draft.any_on) {
        this._notice = { text: "None of the room's lights are on. Set them how you want them first, then press Save as now.", bad: true };
        return null;
      }
      let config = { id: draft.config_id, name: draft.name, entities: entities || draft.entities };
      if (draft.existing) {
        try {
          const current = await this._hass.callApi("GET", `config/scene/config/${draft.config_id}`);
          config = { ...current, id: draft.config_id, entities: { ...(current.entities || {}), ...config.entities } };
        } catch (e) {
          /* not readable (made elsewhere): replace it */
        }
      }
      await this._hass.callApi("POST", `config/scene/config/${draft.config_id}`, config);
      const res = await this._ws({ type: "room_routines/save_look", room_id: room.id, period, track, how: "scene", scene_id: draft.config_id });
      if (!res.ok) {
        this._notice = { text: res.error, bad: true };
        return null;
      }
      return config.name;
    } catch (err) {
      this._notice = { text: `Couldn't save the scene: ${err.body?.message || err.message || String(err)}`, bad: true };
      return null;
    }
  }

  async _call(domain, service, data) {
    try {
      await this._hass.callService(domain, service, data);
    } catch (err) {
      this._notice = { text: err.message || String(err), bad: true };
      this._render();
    }
  }

  async _action(action, el, ev) {
    const d = this._data;
    const room = el.dataset.room && d?.rooms.find((r) => r.id === el.dataset.room);
    switch (action) {
      case "reload":
        location.reload();
        return;
      case "stealth":
        await this._call("switch", el.checked ? "turn_on" : "turn_off", { entity_id: d.house.stealth_entity });
        return;
      case "stealth-off":
        await this._call("switch", "turn_off", { entity_id: d.house.stealth_entity });
        return;
      case "hold":
        await this._call("select", "select_option", { entity_id: d.house.period_entity, option: el.value });
        return;
      case "hold-track":
        await this._call("select", "select_option", { entity_id: d.house.track_entity, option: el.value });
        return;
      case "look-track":
        this._track = el.dataset.track;
        savePref("look-track", this._track);
        this._editLook = this._scenePick = null;
        this._render();
        return;
      case "look-scene-now": {
        const period = el.dataset.period;
        const ok = await this._saveScene(room, period, this._track, null);
        if (ok) this._notice = { text: `Saved the lights as the ${period}${this._track === "dim" ? " Dim" : ""} look, as the scene "${ok}".` };
        this._render();
        return;
      }
      case "look-pick-scene":
        this._scenePick = { room: room.id, period: el.dataset.period, track: this._track };
        this._render();
        return;
      case "scene-pick-cancel":
        this._scenePick = null;
        this._render();
        return;
      case "scene-pick-save": {
        const pick = this._scenePick;
        const scene = this.shadowRoot.querySelector("[data-scene-choice]")?.value;
        if (!scene) return;
        const res = await this._ws(
          { type: "room_routines/save_look", room_id: pick.room, period: pick.period, track: pick.track, how: "scene", scene },
          `${pick.period}${pick.track === "dim" ? " Dim" : ""} now turns on ${this._name(scene)}.`
        );
        if (res.ok) this._scenePick = null;
        else this._notice = { text: res.error, bad: true };
        this._render();
        return;
      }
      case "suggestion-dismiss":
        await this._ws({ type: "room_routines/dismiss", key: el.dataset.key }, "Hidden for four weeks.");
        this._render();
        return;
      case "suggestion-apply": {
        const s = room.suggestions.find((x) => x.key === el.dataset.key);
        if (!s) return;
        let ok = false;
        if (s.kind === "keep_dark") {
          ok = (await this._ws({ type: "room_routines/save_look", room_id: room.id, period: s.period, track: s.track, how: "nothing" }, `${room.name} stays dark in ${s.period}.`)).ok;
        } else if (s.kind === "save_look") {
          const name = await this._saveScene(room, s.period, s.track, s.scene_entities);
          if (name) {
            this._notice = { text: `Saved it as the ${s.period} look, as the scene "${name}".` };
            ok = true;
          }
        } else {
          const periods = d.house.periods.map((p) => ({ name: p.name, start: p.name === s.move_period ? s.new_start : p.start, alt_start: p.alt_start || null }));
          const res = await this._ws({ type: "room_routines/save_periods", periods, alt_days: d.house.alt_days, renames: {} }, `${s.move_period} now starts at ${s.new_start}.`);
          if (!res.ok) this._notice = { text: res.error, bad: true };
          ok = res.ok;
        }
        if (ok) await this._hass.callWS({ type: "room_routines/dismiss", key: s.key }).catch(() => {});
        this._render();
        return;
      }
      case "edit-tracks":
        this._tab = "settings";
        this._startTracksDraft(d);
        this._render();
        return;
      case "tracks-cancel":
        this._tracksDraft = null;
        this._render();
        return;
      case "tracks-save": {
        const t = this._tracksDraft;
        const res = await this._ws(
          { type: "room_routines/save_tracks", sensor: t.sensor, fallback: t.fallback, dim_below: t.dim_below ?? 800, normal_above: t.normal_above ?? 1500 },
          t.sensor || t.fallback ? "Saved the dark-day settings." : "Dark days are off."
        );
        if (!res.ok) {
          this._tracksError = res.error;
          this._render();
          return;
        }
        this._tracksDraft = null;
        this._render();
        return;
      }
      case "mode":
        ev.stopPropagation();
        if (room?.mode_entity) await this._call("select", "select_option", { entity_id: room.mode_entity, option: el.value });
        return;
      case "back":
        this._roomId = null;
        this._editLook = null;
        this._render();
        return;
      case "look-edit":
        this._startLookEdit(room, el.dataset.period);
        this._render();
        return;
      case "look-cancel":
        this._editLook = null;
        this._render();
        return;
      case "look-save": {
        const e = this._editLook;
        const res = await this._ws(
          { type: "room_routines/save_look", room_id: e.room, period: e.period, track: e.track, how: "custom", look: this._lookFromDraft(e.draft) },
          `Saved the ${e.period}${e.track === "dim" ? " Dim" : ""} look.`
        );
        if (res.ok) this._editLook = null;
        else this._notice = { text: res.error, bad: true };
        this._render();
        return;
      }
      case "look-current":
      case "look-nothing":
      case "look-borrow": {
        const how = action.slice(5);
        const period = el.dataset.period;
        const dim = this._track === "dim";
        const text = {
          current: `Saved the lights as they are now as the ${period}${dim ? " Dim" : ""} look.`,
          nothing: `${room.name} stays dark in ${period}${dim ? " on Dim days" : ""}.`,
          borrow: dim ? `${period} now uses its Normal look on Dim days.` : `${period} now uses the previous period's look.`,
        }[how];
        const res = await this._ws({ type: "room_routines/save_look", room_id: room.id, period, track: this._track, how }, text);
        if (!res.ok) this._notice = { text: res.error, bad: true };
        this._render();
        return;
      }
      case "history-hours":
        savePref("hours", Number(el.value));
        this._loadHistory(el.dataset.room, Number(el.value));
        this._render();
        return;
      case "history-refresh":
        this._loadHistory(el.dataset.room, this._history[el.dataset.room]?.hours ?? 72);
        this._render();
        return;
      case "add-room":
        this._tab = "settings";
        this._startRoomDraft(null);
        this._render();
        return;
      case "edit-room":
        this._tab = "settings";
        this._startRoomDraft(room);
        this._render();
        return;
      case "remove-room": {
        if (!confirm(`Remove ${room.name}? Its looks and settings go with it. The lights and sensors themselves are untouched.`)) return;
        const res = await this._ws({ type: "room_routines/remove_room", room_id: room.id }, `Removed ${room.name}.`);
        if (!res.ok) this._notice = { text: res.error, bad: true };
        this._render();
        return;
      }
      case "room-cancel":
        this._roomDraft = null;
        this._render();
        return;
      case "room-save": {
        const r = this._roomDraft;
        const body = {
          name: (r.name || "").trim(),
          area_id: r.area_id || null,
          lights: r.lights,
          triggers: r.triggers,
          holds: r.holds,
          lux_sensor: r.lux_sensor || null,
          threshold_lux: r.threshold_lux ?? null,
          timeout_s: r.timeout_s ?? ROOM_DEFAULTS.timeout_s,
          fade_out_s: r.fade_out_s ?? ROOM_DEFAULTS.fade_out_s,
          cooldown_s: r.cooldown_s ?? ROOM_DEFAULTS.cooldown_s,
          drift_s: r.drift_s ?? ROOM_DEFAULTS.drift_s,
          self_fading: r.self_fading.filter((l) => r.lights.includes(l)),
        };
        const msg = { type: "room_routines/save_room", room: body };
        if (r.id) msg.room_id = r.id;
        const res = await this._ws(msg, r.id ? `Saved ${body.name}.` : `Added ${body.name}. It starts in Log only.`);
        if (!res.ok) {
          this._roomError = res.error;
          this._render();
          return;
        }
        const was = d.rooms.find((x) => x.id === r.id);
        if (was && r.mode && r.mode !== was.mode && was.mode_entity) {
          await this._call("select", "select_option", { entity_id: was.mode_entity, option: r.mode });
        }
        this._roomDraft = null;
        this._tab = "rooms";
        this._roomId = res.result.room_id;
        this._render();
        return;
      }
      case "edit-periods":
        this._tab = "settings";
        this._startPeriodDraft(d);
        this._render();
        return;
      case "periods-cancel":
        this._periodDraft = null;
        this._render();
        return;
      case "period-add":
        this._periodDraft.rows.push({ original: null, name: "", start: "", alt_start: "" });
        this._render();
        return;
      case "period-remove":
        this._periodDraft.rows.splice(Number(el.dataset.row), 1);
        this._render();
        return;
      case "periods-save": {
        const pd = this._periodDraft;
        const kept = new Set(pd.rows.map((r) => r.original).filter(Boolean));
        const renames = {};
        for (const r of pd.rows) if (r.original && r.original !== r.name.trim()) renames[r.original] = r.name.trim();
        for (const name of d.house.order) if (!kept.has(name)) renames[name] = null;
        const res = await this._ws(
          {
            type: "room_routines/save_periods",
            periods: pd.rows.map((r) => ({ name: r.name.trim(), start: r.start, alt_start: pd.alt_days.length && r.alt_start ? r.alt_start : null })),
            alt_days: pd.alt_days,
            renames,
          },
          "Saved the periods."
        );
        if (!res.ok) {
          this._periodError = res.error;
          this._render();
          return;
        }
        this._periodDraft = null;
        this._tab = "day";
        this._render();
        return;
      }
    }
  }
}

const STYLES = `
  :host { display:block; background: var(--primary-background-color); min-height:100vh; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
  .toolbar { display:flex; align-items:center; gap:12px; height:56px; padding:0 8px; background: var(--app-header-background-color, var(--primary-color)); color: var(--app-header-text-color, #fff); }
  .toolbar .title { font-size:20px; flex:1; }
  .now-period { display:flex; align-items:center; gap:6px; font-size:14px; }
  .now-period .until { opacity:.8; }
  .now-track { display:flex; align-items:center; gap:4px; font-size:14px; }
  .now-track ha-icon { --mdc-icon-size:20px; }
  .subtabs { display:flex; gap:4px; margin-bottom:8px; }
  .subtab { font:inherit; font-size:13px; display:inline-flex; align-items:center; gap:4px; background:none; border:1px solid var(--divider-color); border-radius:16px; padding:4px 12px; cursor:pointer; color: var(--secondary-text-color); }
  .subtab.on { border-color: var(--primary-color); color: var(--primary-color); background: color-mix(in srgb, var(--primary-color) 10%, transparent); }
  .subtab ha-icon { --mdc-icon-size:16px; }
  .suggestion { display:flex; align-items:center; gap:8px; padding:8px 0; border-bottom:1px solid var(--divider-color); font-size:14px; }
  .suggestion .grow { flex:1; }
  .suggestion > ha-icon, .meta.hint ha-icon { color: var(--warning-color, #ffa600); --mdc-icon-size:18px; }
  .stealth-toggle { display:flex; flex-direction:row; align-items:center; gap:4px; cursor:pointer; padding-right:8px; margin:0; }
  .tabs { display:flex; gap:4px; padding:8px 16px 0; border-bottom:1px solid var(--divider-color); background: var(--card-background-color); }
  .tab { font:inherit; font-size:14px; background:none; border:none; border-bottom:2px solid transparent; padding:8px 12px; cursor:pointer; color: var(--secondary-text-color); }
  .tab.on { color: var(--primary-color); border-bottom-color: var(--primary-color); }
  .content { padding:16px; max-width:1200px; margin:0 auto; }
  h2 { font-size:16px; font-weight:500; margin:20px 0 8px; }
  h2.inline { margin-top:0; }
  .card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius, 12px); box-shadow: var(--ha-card-box-shadow, none); border:1px solid var(--divider-color); padding:16px; margin-bottom:12px; }
  .card.warn { border-color: var(--error-color, #db4437); }
  .card.notice { border-color: var(--primary-color); }
  .card.stealth { display:flex; align-items:center; gap:12px; background: color-mix(in srgb, var(--warning-color, #ffa600) 18%, var(--card-background-color)); border-color: var(--warning-color, #ffa600); }
  .card.stealth > div { flex:1; }
  .period-bar { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap:12px; }
  .grid .card { margin:0; }
  .room { cursor:pointer; }
  .room:hover, .room:focus-visible { border-color: var(--primary-color); outline:none; }
  .roomhead { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px; }
  .roomname { font-size:18px; font-weight:500; }
  .roomname.big { font-size:22px; }
  .meta { color: var(--secondary-text-color); font-size:13px; margin-top:4px; }
  .meta.reason { font-style:italic; }
  .status { font-size:14px; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .sensor { display:inline-flex; align-items:center; gap:4px; font-size:12px; color: var(--secondary-text-color); }
  .sensor i { width:9px; height:9px; border-radius:50%; background: var(--disabled-text-color, #bbb); display:inline-block; }
  .sensor.on { color: var(--primary-text-color); }
  .sensor.on i { background: var(--success-color, #43a047); box-shadow:0 0 0 3px color-mix(in srgb, var(--success-color, #43a047) 25%, transparent); }
  .light { display:inline-flex; align-items:center; gap:4px; font-size:13px; padding:2px 8px 2px 4px; border-radius:12px; background: var(--secondary-background-color); }
  .light ha-icon { --mdc-icon-size:18px; }
  .light.on ha-icon { color: #fbc02d; }
  .light.bad { color: var(--error-color); }
  .light small { color: var(--secondary-text-color); }
  .pill { font:inherit; font-size:12px; border-radius:12px; padding:3px 8px; border:1px solid var(--divider-color); background: var(--secondary-background-color); color: var(--primary-text-color); }
  .pill.mode-live { background: color-mix(in srgb, var(--success-color, #43a047) 20%, var(--card-background-color)); border-color: var(--success-color, #43a047); }
  .pill.mode-log_only { background: color-mix(in srgb, var(--info-color, #039be5) 18%, var(--card-background-color)); border-color: var(--info-color, #039be5); }
  .pill.mode-off { opacity:.7; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }
  .swatch { display:inline-block; width:12px; height:12px; border-radius:3px; vertical-align:middle; border:1px solid var(--divider-color); }
  .warntext { color: var(--warning-color, #ffa600); }
  .bad { color: var(--error-color, #db4437); }
  .muted { color: var(--secondary-text-color); }
  .explain { color: var(--secondary-text-color); font-size:13px; line-height:1.45; }
  .detailhead { margin-bottom:12px; }
  .tablewrap { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th, td { text-align:left; padding:6px 8px; border-bottom:1px solid var(--divider-color); vertical-align:top; }
  th { font-weight:500; color: var(--secondary-text-color); white-space:nowrap; }
  tr.nowrow { background: color-mix(in srgb, var(--primary-color) 8%, transparent); }
  tr.diff td { background: color-mix(in srgb, var(--warning-color, #ffa600) 10%, transparent); }
  td.actions { white-space:nowrap; text-align:right; }
  .actionrow { display:flex; gap:4px; justify-content:flex-end; flex-wrap:wrap; }
  .lookeditor { display:flex; flex-direction:column; gap:10px; }
  .lookrow { display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
  .lookrow .lname { min-width:160px; font-weight:500; }
  label { display:flex; flex-direction:column; gap:4px; font-size:14px; margin-bottom:12px; }
  label.inline { flex-direction:row; align-items:center; gap:6px; margin:0 12px 6px 0; display:inline-flex; }
  .help { color: var(--secondary-text-color); font-size:12px; }
  input[type=text], input[type=number], input[type=search], input[type=time], select { font:inherit; font-size:14px; padding:8px; border-radius:6px; border:1px solid var(--divider-color); background: var(--card-background-color); color: var(--primary-text-color); }
  input[type=number] { width:110px; }
  .inputunit { display:flex; align-items:center; gap:6px; }
  .twocol { display:grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap:0 16px; }
  fieldset { border:1px solid var(--divider-color); border-radius:8px; padding:8px 12px 12px; margin:0 0 12px; }
  legend { font-size:14px; padding:0 4px; }
  .picker input[type=search] { width:100%; box-sizing:border-box; margin-top:8px; }
  .options { display:flex; flex-direction:column; max-height:220px; overflow:auto; margin-top:6px; }
  .option { font:inherit; text-align:left; background:none; border:none; border-bottom:1px solid var(--divider-color); padding:6px 4px; cursor:pointer; color: var(--primary-text-color); }
  .option:hover, .option:focus-visible { background: var(--secondary-background-color); }
  .option small { color: var(--secondary-text-color); }
  .chip { display:inline-flex; align-items:center; gap:4px; background: var(--c, var(--primary-color)); color:#fff; border-radius:10px; padding:2px 8px; font-size:12px; }
  .chip.sel { background: var(--secondary-background-color); color: var(--primary-text-color); font-size:13px; }
  .chip .x { font:inherit; background:none; border:none; cursor:pointer; color: var(--secondary-text-color); padding:0 2px; }
  .row { display:flex; gap:8px; align-items:center; margin-top:12px; flex-wrap:wrap; }
  .row.spread { justify-content:space-between; margin-top:0; }
  .btn { font:inherit; font-size:14px; padding:8px 16px; border-radius:8px; border:1px solid var(--primary-color); background:none; color: var(--primary-color); cursor:pointer; display:inline-flex; align-items:center; gap:4px; }
  .btn.small { padding:5px 12px; font-size:13px; }
  .btn.tiny { padding:2px 8px; font-size:12px; border-radius:6px; }
  .btn.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .btn.danger { border-color: var(--error-color, #db4437); color: var(--error-color, #db4437); }
  .btn ha-icon { --mdc-icon-size:18px; }
  .daylabel { font-size:13px; color: var(--secondary-text-color); margin:8px 0 4px; }
  .daybar { position:relative; height:36px; border-radius:8px; overflow:hidden; background: var(--secondary-background-color); }
  .band { position:absolute; top:0; bottom:0; display:flex; align-items:center; justify-content:center; color:#fff; font-size:12px; text-shadow:0 1px 2px rgba(0,0,0,.4); overflow:hidden; white-space:nowrap; opacity:.85; }
  .band span { overflow:hidden; text-overflow:ellipsis; padding:0 4px; }
  .nowline { position:absolute; top:-2px; bottom:-2px; width:3px; background: var(--primary-text-color); border-radius:2px; }
  .hours { position:relative; height:18px; margin-top:4px; font-size:11px; color: var(--secondary-text-color); }
  .hours span { position:absolute; transform:translateX(-50%); }
  .hours span:first-child { transform:none; }
  .hours span:last-child { transform:translateX(-100%); }
  .parity .score { font-size:16px; margin:8px 0; }
  .score.good b { color: var(--success-color, #43a047); }
  .score.ok b { color: var(--warning-color, #ffa600); }
  .score.badscore b { color: var(--error-color, #db4437); }
  details summary { cursor:pointer; margin-top:12px; font-size:14px; }
  .activity { list-style:none; padding:0; margin:8px 0 0; font-size:13px; max-height:360px; overflow:auto; }
  .activity li { padding:3px 0; border-bottom:1px solid var(--divider-color); }
  .activity li.light { color: var(--secondary-text-color); }
  .activity .t { font-variant-numeric: tabular-nums; color: var(--secondary-text-color); margin-right:6px; }
  .settings-summary > div { margin:4px 0; font-size:14px; }
  @media (max-width: 600px) {
    .now-period .until { display:none; }
    .content { padding:8px; }
  }
`;

// A tab still open during an update can load the new file next to the old one.
if (!customElements.get("room-routines-panel")) customElements.define("room-routines-panel", RoomRoutinesPanel);
