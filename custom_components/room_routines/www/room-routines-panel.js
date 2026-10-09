// Room Routines sidebar panel. Plain web component: no build step, no dependencies.
// Live data comes from one websocket subscription (room_routines/subscribe); changes go
// through room_routines/save_look, save_room, remove_room, save_periods, save_tracks and
// dismiss. Scenes are saved through Home Assistant's own scene API (config/scene/config,
// the one the scene editor uses), so they stay ordinary Home Assistant scenes. The deeper
// settings are shown to admins only (and the websocket refuses them to anyone else).

// Must match manifest.json (a test checks). Compared with the running integration so a
// tab still holding old page code after an update says so.
const PANEL_VERSION = "0.11.0";

const STATE_LABEL = { idle: "Idle", owned: "Lights on by motion", manual: "Switched on by hand" };
const MODE_LABEL = { off: "Off", log_only: "Log only", live: "Live" };
const TRACK_LABEL = { normal: "Normal day", dim: "Dark Day" };
const TRACK_ICON = { normal: "mdi:weather-sunny", dim: "mdi:weather-cloudy" };
const HUE_SCENE_NOTE =
  "This scene comes from another app (such as the Hue app), so Room Routines can turn it on but can't read it: it can't be made dimmer or brighter on Dark Days, lights that can't fade by themselves jump to it instead of moving gradually, and suggestions can't compare with it. A scene saved with Save as now, or made in Home Assistant's scene editor, has none of these limits.";
const MODE_HELP = {
  off: "Does nothing.",
  log_only: "Works out what it would do and writes it down, but switches nothing. Use it to check a room before it goes live.",
  live: "Switches the room's lights.",
};
const PERIOD_COLOURS = ["#3949ab", "#8e24aa", "#fb8c00", "#fdd835", "#43a047", "#00acc1", "#e53935", "#6d4c41"];
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const ROOM_DEFAULTS = { threshold_lux: 50, timeout_s: 30, fade_out_s: 15, cooldown_s: 30, drift_s: 90 };
const ON_BY_HAND = {
  leave: "Leave them as they are",
  routine: "Start the routine (after 3 seconds)",
};
const HAND_HOLD = {
  until_off: "Until the lights are switched off",
  moves_on: "Until the routine moves on",
  minutes: "For a number of minutes",
};
const TIMER_DAYS = { every_day: "Every day", workdays: "Workdays", days: "Chosen days" };
const LIGHTS_OUT_WHEN = { time: "At a time", period: "When a period starts", entity: "When something turns on" };
const LIGHTS_OUT_STYLE = {
  once_empty: "Off once empty",
  now: "All off now",
};
const LIGHTS_OUT_STYLE_HELP = {
  once_empty: "Rooms where nobody is detected go dark now. A room where a sensor sees someone goes fully off once it's empty (after its own timeout, with its own fade), instead of going dark on them. That waiting ends when the next period starts.",
  now: "Everything goes off now, whoever is there.",
};
const RULE_ACTIONS = { nothing: "Do nothing", look: "Use another look", cap: "No brighter than" };
const ERRORS = {
  no_name: "Give the room a name.",
  invalid_room: "A room needs at least one light, a sensor can't both switch the lights on and only keep them on, and every timer needs a time.",
  invalid_signals: "Each Inform needs an entity and a state, and at least one of the room's lights.",
  invalid_lights_out: "Each Lights out needs a time, a period or an entity and its state, and chosen days need at least one day.",
  invalid_rules: "Every condition and rule needs an entity and a state; a rule using another look needs a scene or a period, and a brightness limit a level from 1 to 100 %.",
  invalid_room_times: "The room's own start times must give every period a different time.",
  invalid_periods: "Every period needs its own name and a start time no other period uses on the same days.",
  last_period: "There has to be at least one period.",
  unknown_period: "That period no longer exists. Reload the page.",
  unknown_room: "That room no longer exists. Reload the page.",
  invalid_look: "That look isn't valid.",
  unauthorized: "Only an administrator can change this.",
  unknown_scene: "Home Assistant didn't create the scene in time. Try again in a moment.",
  unknown_track: "That track doesn't exist.",
  invalid_tracks: "Normal again must be above the Dark Day level, and the brightness between 1 and 300 %.",
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
    this._tracksDraft = null; // settings: the Dark Day settings being changed
    this._track = loadPref("look-track", "normal"); // which track's looks the room page shows
    this._lookLayer = loadPref("look-layer", "base"); // the room's looks, or Someone's there's
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
    return !!(this._editLook || this._roomDraft || this._periodDraft || this._tracksDraft || this._scenePick || this._houseRulesDraft || this._loDraft);
  }

  get _admin() {
    return !!this._hass?.user?.is_admin;
  }

  // ---- entity helpers ----

  _name(e) {
    return this._hass.states[e]?.attributes?.friendly_name || e;
  }

  _condText(c) {
    if (!c?.entity) return "?";
    return `${this._name(c.entity)} ${c.negate ? "isn't" : "is"} ${c.state || "on"}`;
  }

  _ruleText(r) {
    let what = "do nothing";
    if (r.action === "cap") what = `no brighter than ${r.max_pct} %`;
    else if (r.action === "look") what = r.scene ? `turn on ${this._name(r.scene)}` : `use the ${r.period} look`;
    return `while ${this._condText(r.when)}: ${what}`;
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

  // Scenes made in Home Assistant list their entities; ones from other apps (the
  // Hue app's) don't, and Room Routines can't read their settings.
  _sceneReadable(scene) {
    return Array.isArray(this._hass.states[scene]?.attributes?.entity_id);
  }

  _sceneWarning(scene) {
    if (!this._hass.states[scene] || this._sceneReadable(scene)) return "";
    return ` <span class="warntext" title="${esc(HUE_SCENE_NOTE)}"><ha-icon icon="mdi:alert-outline"></ha-icon> from another app</span>`;
  }

  _sceneLink(scene) {
    const id = this._hass.states[scene]?.attributes?.id;
    const name = esc(this._sceneName(scene));
    const link = id && this._admin
      ? `<ha-icon icon="mdi:palette"></ha-icon> <a href="/config/scene/edit/${encodeURIComponent(id)}" target="_top" title="Open in Home Assistant's scene editor">${name}</a>`
      : `<ha-icon icon="mdi:palette"></ha-icon> ${name}`;
    return link + this._sceneWarning(scene);
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
        <button class="tab ${this._tab === "rooms" ? "on" : ""}" data-tab="rooms">Status</button>
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
    const by = house.track_by_hand ? " Chosen by hand until the next period." : "";
    return `${TRACK_LABEL[house.track]}: ${t.reason || "not checked yet"}.${by}`;
  }

  // Dark Days in a few lines: today, where the reading comes from, the rules.
  _darkDaySummary(house) {
    const t = house.tracks;
    const list = (xs) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);
    const sensor = t.sensor || t.fallback;
    const sensorText = sensor
      ? `the light sensor <b>${esc(this._name(sensor))}</b>${t.sensor && t.fallback ? ` (or <b>${esc(this._name(t.fallback))}</b> when it's unavailable)` : ""}`
      : "";
    const sources = [];
    if (t.weather) sources.push("<b>the weather</b> (Open-Meteo)");
    if (sensorText) sources.push(sensorText);
    if (t.first === "sensor") sources.reverse();
    const from = sources.length > 1 ? `Checks ${sources[0]} first, then ${sources[1]}.` : `Uses ${sources[0] || "nothing yet"}.`;
    const learned = sensor ? t.learned?.[t.level_sensor || sensor] ?? 0 : 0;
    const now = [];
    if (t.weather) now.push(`Weather: ${this._weatherNow(t)}`);
    if (sensor) now.push(`Light sensor: ${this._sensorNow(t, learned)}`);
    const pct = t.brightness_pct ?? 100;
    const periods = t.periods?.length ? list(t.periods.map(esc)) : "no periods";
    return `<div><ha-icon icon="${TRACK_ICON[house.track]}"></ha-icon> Today is a <b>${esc(TRACK_LABEL[house.track])}</b>${t.reason ? `: ${esc(t.reason)}` : ""}.${house.track_by_hand ? " Chosen by hand until the next period." : ""}</div>
      <div class="meta">Dark Days can happen in ${periods}: when the sun is down,${t.weather && t.dark_below_wm2 ? ` when less than <b>${esc(t.dark_below_wm2)} W/m²</b> of sunlight reaches the ground,` : ""} or when the light is below <b>${esc(t.dark_below_pct)} %</b> of a clear day at the same sun height (Normal again above <b>${esc(t.normal_above_pct)} %</b>). ${from}
      Dark Day brightness for Normal looks: ${pct === 100 ? "unchanged" : `<b>${esc(pct)} %</b>`}.</div>
      <div class="meta nowlines">${now.map((l) => `<div>${l}</div>`).join("")}<div>Sun: ${t.sun_down ? `below the horizon (${esc(t.sun_elevation)}°)` : `${esc(t.sun_elevation)}° above the horizon`}.</div></div>`;
  }

  _weatherNow(t) {
    switch (t.weather_state) {
      case "ok":
        return `<b>${esc(t.weather_pct)} %</b> of a clear day${t.sunlight != null ? `, ${esc(t.sunlight)} W/m² of sunlight` : ""}${t.cloud_cover != null ? `, ${esc(t.cloud_cover)} % cloud` : ""} (at ${esc(hhmm(t.weather_at))}).`;
      case "sun_down":
        return "nothing to compare while the sun is down.";
      case "unreachable":
        return `<span class="warntext">couldn't reach Open-Meteo${t.weather_at ? ` (last answer at ${esc(hhmm(t.weather_at))})` : ""}.</span> It tries again every 15 minutes.`;
      case "stale":
        return `<span class="warntext">no fresh answer since ${esc(hhmm(t.weather_at))}.</span>`;
      case "waiting":
        return "waiting for the first answer.";
      default:
        return "off.";
    }
  }

  _sensorNow(t, learned) {
    const lx = t.level == null ? "" : `${esc(t.level)} lx`;
    switch (t.sensor_state) {
      case "ok":
        return `${lx}, <b>${esc(t.sensor_pct)} %</b> of a clear day.`;
      case "sun_down":
        return `${lx}; nothing to compare while the sun is down.`;
      case "learning":
        return `${lx}; still learning what a clear day looks like to it at this sun height (${esc(learned)} sun heights learned so far).`;
      case "no_reading":
        return `<span class="warntext">no reading (the sensor is unavailable).</span>`;
      default:
        return "none chosen.";
    }
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
        ${d.rooms.some((r) => r.lights_out) ? `<div><ha-icon icon="mdi:weather-night"></ha-icon> ${d.rooms.filter((r) => r.lights_out).map((r) => `<b>${esc(r.name)}</b> ${r.lights_out.at ? `goes off in <span data-deadline="${esc(r.lights_out.at)}">${countdown(r.lights_out.at)}</span>` : "goes off once it's empty"}`).join(", ")} (${esc(d.rooms.find((r) => r.lights_out).lights_out.name)}).</div>` : ""}
      </div>
      ${this._roomGrid(d)}`;
  }

  _roomGrid(d) {
    const on = d.rooms.filter((r) => r.mode !== "off");
    const off = d.rooms.filter((r) => r.mode === "off");
    const openOff = loadPref("off-rooms", false);
    return `
      ${on.length ? `<div class="grid">${on.map((r) => this._card(d, r)).join("")}</div>` : `<div class="card meta">Every room is off. Open one below and give it something to do.</div>`}
      ${off.length ? `<details class="offrooms" data-pref="off-rooms" ${openOff ? "open" : ""}>
        <summary>${off.length} room${off.length === 1 ? "" : "s"} switched off</summary>
        <div class="card"><table class="plain">${off
          .map((r) => `<tr class="openrow" data-open="${esc(r.id)}" tabindex="0" role="button" aria-label="Open ${esc(r.name)}">
            <td><b>${esc(r.name)}</b><div class="meta">${r.settings.lights.length} light${r.settings.lights.length === 1 ? "" : "s"}${r.has_sensors ? "" : ", no sensors"}</div></td>
            <td class="actions" data-stop>${this._modePill(r)}</td></tr>`)
          .join("")}</table>
        <p class="explain">Every area with lights becomes a room, switched off so it does nothing until you set it up. ${this._admin ? "Hide the ones you'll never use under Settings." : ""}</p></div>
      </details>` : ""}`;
  }

  _statusLine(room) {
    let label = STATE_LABEL[room.state] || room.state || "Starting";
    if (room.state === "owned" && room.paused) label = "Changed by hand: left as set";
    else if (room.state === "owned" && !room.has_sensors) label = "Following its routine";
    const bits = [`<b>${esc(label)}</b>`];
    if (room.rule) bits.push(`<span class="warntext">${esc(room.rule)}</span>`);
    const held = Object.entries(room.hand || {});
    if (held.length && !room.paused) bits.push(`changed by hand: ${held.map(([l, u]) => esc(this._name(l)) + (u ? ` until ${esc(hhmm(u))}` : "")).join(", ")}`);
    else if (held.length && held.some(([, u]) => u)) bits.push(`back to the routine at ${esc(hhmm(held.map(([, u]) => u).filter(Boolean).sort()[0]))}`);
    for (const sig of room.signals_now || []) bits.push(`<span class="signal">inform: ${esc(sig.name)} on ${sig.lights.map((l) => esc(this._name(l))).join(", ")}</span>`);
    if (room.lights_out) bits.push(room.lights_out.at
      ? `${esc(room.lights_out.name)}: empty, off in <span data-deadline="${esc(room.lights_out.at)}">${countdown(room.lights_out.at)}</span>`
      : `${esc(room.lights_out.name)}: off once the room is empty (until ${esc(hhmm(room.lights_out.until))})`);
    if (room.unmet) bits.push(`waiting: only when ${esc(room.unmet)}`);
    if (room.blending) bits.push(`blending into ${esc(room.blending.into)} (${Math.round(room.blending.fraction * 100)} %)`);
    if (room.mode === "log_only") bits.push(`<span class="muted">(log only: switching nothing)</span>`);
    if (room.lights_off_at) bits.push(`off in <span data-deadline="${esc(room.lights_off_at)}">${countdown(room.lights_off_at)}</span>`);
    if (room.cooldown_until && new Date(room.cooldown_until) > new Date()) {
      bits.push(`ignoring motion until ${esc(hhmmss(room.cooldown_until))}`);
    }
    return bits.join(" · ");
  }

  _luxLine(room) {
    if (!room.has_sensors) return "";
    const t = room.settings.threshold_lux;
    if (!(room.settings.lux_sensors || []).length) return t == null ? "" : "No light-level sensor: switches on at any light level";
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

  // A room with its own period times, or blends, says so.
  _ownTimesLine(d, room) {
    const s = room.settings;
    const bits = [];
    const starts = Object.entries(s.period_starts || {});
    if (starts.length) bits.push(`its own times: ${starts.map(([p, t]) => `${esc(p)} from ${esc(t)}`).join(", ")}`);
    const blends = Object.entries(s.blends || {});
    if (blends.length) bits.push(blends.map(([p, m]) => `${esc(p)} blends into the next period over ${esc(m)} min`).join(", "));
    if ((s.timers || []).length) bits.push(`${s.timers.length} timer${s.timers.length === 1 ? "" : "s"}`);
    if (!bits.length) return "";
    const differs = room.period && room.period !== d.house.period;
    return `<div class="meta"><ha-icon class="small" icon="mdi:clock-outline"></ha-icon> ${differs ? `In <b>${esc(room.period)}</b> (the house is in ${esc(d.house.period)}); ` : ""}${bits.join("; ")}.</div>`;
  }

  _noLookWarning(room) {
    if (!room.no_look || room.mode === "off") return "";
    return `<div class="warntext"><ha-icon icon="mdi:alert-outline"></ha-icon> No period has a look to switch on, so this room never lights up. Set one in its looks table.</div>`;
  }

  _modePill(room) {
    if (!this._admin) return `<span class="pill mode-${esc(room.mode)}">${esc(MODE_LABEL[room.mode] || room.mode)}</span>`;
    return `<select class="pill mode-${esc(room.mode)}" data-action="mode" data-room="${esc(room.id)}" title="${esc(MODE_HELP[room.mode] || "")}" aria-label="Mode for ${esc(room.name)}">
      ${Object.entries(MODE_LABEL).map(([k, v]) => `<option value="${k}" ${k === room.mode ? "selected" : ""}>${v}</option>`).join("")}
    </select>`;
  }

  _card(d, room) {
    const period = room.period || d.house.period;
    const from = room.look_period && room.look_period !== period ? ` (uses ${esc(room.look_period)}'s)` : "";
    return `<div class="card room" data-open="${esc(room.id)}" tabindex="0" role="button" aria-label="Open ${esc(room.name)}">
      <div class="roomhead">
        <div><div class="roomname">${esc(room.name)}</div><div class="meta">${esc(room.area_name || "No area")}</div></div>
        <div data-stop>${this._modePill(room)}</div>
      </div>
      <div class="status">${this._statusLine(room)}</div>
      ${this._noLookWarning(room)}
      ${room.reason ? `<div class="meta reason">${esc(room.reason)}</div>` : ""}
      ${this._luxLine(room) ? `<div class="meta">${this._luxLine(room)}</div>` : ""}
      ${this._ownTimesLine(d, room)}
      ${room.has_sensors ? `<div class="chips">${this._sensorDots(room)}</div>` : ""}
      <div class="chips">${this._lightChips(room)}</div>
      <div class="meta look"><b>${esc(period)} look${room.look_track === "dim" ? " for Dark Days" : ""}${from}${room.look_factor && room.look_factor !== 1 ? ` at ${Math.round(room.look_factor * 100)} %` : ""}:</b> ${this._lookText(room.current_look, room.settings.lights)}</div>
      ${room.suggestions?.length ? `<div class="meta hint"><ha-icon icon="mdi:lightbulb-on-outline"></ha-icon> ${room.suggestions.length === 1 ? "A suggestion" : `${room.suggestions.length} suggestions`} from how the lights get changed</div>` : ""}
    </div>`;
  }

  // ---- room detail ----

  _layerOf(room) {
    return room.has_sensors ? this._lookLayer : "base";
  }

  _layerTables(room, layer) {
    return layer === "someone"
      ? { looks: room.someone_looks || {}, dim: room.someone_dim_looks || {} }
      : { looks: room.looks || {}, dim: room.dim_looks || {} };
  }

  _detail(d, room) {
    const order = d.house.order;
    const lights = room.settings.lights;
    const track = this._track;
    const dim = track === "dim";
    const layer = this._layerOf(room);
    const someone = layer === "someone";
    const T = this._layerTables(room, layer);
    const table = dim ? T.dim : T.looks;
    const same = (x) => x && x.room === room.id && x.track === track && (x.layer || "base") === layer;
    const rows = order
      .map((p) => {
        const own = table[p];
        const editing = same(this._editLook) && this._editLook.period === p;
        const picking = same(this._scenePick) && this._scenePick.period === p;
        const now = p === (room.period || d.house.period) && d.house.track === track;
        let cells;
        if (editing) cells = `<td colspan="${lights.length}">${this._lookEditor(room)}</td>`;
        else if (picking) cells = `<td colspan="${lights.length}">${this._scenePicker(room, own)}</td>`;
        else if (own?.nothing) cells = `<td colspan="${lights.length}" class="muted">Kept dark</td>`;
        else if (own?.scene) cells = `<td colspan="${lights.length}">${this._sceneLink(own.scene)}</td>`;
        else if (own) cells = lights.map((l) => `<td>${this._target(own.lights?.[l])}</td>`).join("");
        else if (dim) {
          const pct = d.house.tracks?.brightness_pct ?? 100;
          const from = T.looks[p] ? p : this._borrowedFrom(order, T.looks, p);
          const src = T.looks[p] ? "its Normal look" : from ? `${esc(from)}'s Normal look` : someone ? "the room's look" : "—";
          const srcLook = from ? T.looks[from] : null;
          const note = pct !== 100 && srcLook?.scene && !this._sceneReadable(srcLook.scene) ? ` <span class="warntext" title="${esc(HUE_SCENE_NOTE)}">(a scene from another app: unchanged)</span>` : "";
          cells = `<td colspan="${lights.length}" class="muted">${pct !== 100 ? `Uses ${src} at ${esc(pct)} %${note}` : `Uses ${src}`}</td>`;
        } else {
          const borrowed = this._borrowedFrom(order, T.looks, p);
          if (someone) cells = `<td colspan="${lights.length}" class="muted">Not set: the room's look</td>`;
          else cells = lights.map((l) => `<td class="muted">${borrowed ? `as ${esc(borrowed)}` : "—"}</td>`).join("");
        }
        return `<tr class="${now ? "nowrow" : ""}">
          <th class="periodcell"><div><span class="dot" style="background:${this._periodColour(p)}"></span>${esc(p)}${now ? ` <small>now</small>` : ""}</div>
            ${editing || picking ? "" : this._lookActions(room, p, own)}</th>
          ${cells}
        </tr>`;
      })
      .join("");
    const layerTabs = room.has_sensors
      ? `<div class="subtabs">
      <button class="subtab ${!someone ? "on" : ""}" data-action="look-layer" data-layer="base"><ha-icon icon="mdi:lightbulb-outline"></ha-icon> Looks</button>
      <button class="subtab ${someone ? "on" : ""}" data-action="look-layer" data-layer="someone"><ha-icon icon="mdi:walk"></ha-icon> Someone's there</button>
    </div>`
      : "";
    const trackTabs = `${layerTabs}<div class="subtabs">
      ${Object.entries(TRACK_LABEL).map(([k, v]) => `<button class="subtab ${k === track ? "on" : ""}" data-action="look-track" data-track="${k}"><ha-icon icon="${TRACK_ICON[k]}"></ha-icon> ${v}s</button>`).join("")}
    </div>`;
    const someoneHelp = someone
      ? `<p class="explain"><b>Brighter while someone's there.</b> While the room's routine runs (started by a timer, a starter, a button or by hand), motion moves the lights set here to these looks; lights left out stay with the routine. When the room is empty again they fade back to the routine instead of going off. Each look covers only its own period: a period without one uses the room's look. When the routine isn't running, motion uses these looks too. ${room.runs_ambient ? "" : "In a room with sensors, the routine runs until it's stopped only once the room also has a timer or a starter; until then starting it is a visit that switches off when the room is empty."}</p>`
      : "";
    const trackHelp = someoneHelp + (dim
      ? `<p class="explain">These looks are used on Dark Days${d.house.tracks?.enabled ? ` (in ${esc((d.house.tracks.periods || []).join(", ") || "no periods")}, when the sun is down or the light is below ${esc(d.house.tracks.dark_below_pct)} % of a clear day)` : ", once Dark Days are switched on under Settings → Dark Days"}. ${(d.house.tracks?.brightness_pct ?? 100) !== 100 ? `A period without its own Dark Day look uses its Normal one at ${esc(d.house.tracks.brightness_pct)} % brightness (lights set to their last brightness stay as they are)` : "A period without its own Dark Day look uses its Normal one"}, so only set the ones that should differ.</p>`
      : `<p class="explain">What the lights do when the room's routine starts, in each period. A period without its own look uses the one before it. Set the lights how you want them and press <b>Save as now</b>: it becomes a Home Assistant scene you can also use on wall buttons. Or pick a scene you already have, or edit a look by hand.</p>`);
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
        ${this._noLookWarning(room)}
        ${room.reason ? `<div class="meta reason">${esc(room.reason)}</div>` : ""}
        ${this._luxLine(room) ? `<div class="meta">${this._luxLine(room)}</div>` : ""}
        ${this._ownTimesLine(d, room)}
        ${room.has_sensors ? `<div class="chips">${this._sensorDots(room)}</div>` : ""}
        <div class="chips">${this._lightChips(room)}</div>
        ${room.mode !== "off" ? `<div class="row">
          <button class="btn small" data-action="routine-start" data-room="${esc(room.id)}"><ha-icon icon="mdi:play"></ha-icon> Start the routine</button>
          <button class="btn small" data-action="routine-stop" data-room="${esc(room.id)}"><ha-icon icon="mdi:stop"></ha-icon> Switch off</button>
        </div>` : ""}
      </div>
      ${this._suggestionsSection(room)}
      <h2>Looks</h2>
      <div class="card">
        ${trackTabs}
        ${trackHelp}
        <div class="tablewrap"><table class="looks">
          <tr><th>Period</th>${lights.map((l) => `<th>${esc(this._name(l))}</th>`).join("")}</tr>
          ${rows}
        </table></div>
      </div>
      ${this._historySection(room)}
      <h2>How this room works</h2>
      <div class="card settings-summary">
        ${s.triggers.length ? `<div>Switches on when ${s.triggers.map((t) => `<b>${esc(this._name(t))}</b>`).join(" or ")} sees someone${s.threshold_lux != null && s.lux_sensor ? ` and it's darker than <b>${esc(s.threshold_lux)} lx</b>` : ""}.</div>` : ""}
        ${s.holds.length ? `<div>Kept on while ${s.holds.map((t) => `<b>${esc(this._name(t))}</b>`).join(" or ")} sees someone.</div>` : ""}
        ${room.has_sensors
          ? `<div>Goes dark <b>${esc(s.timeout_s)} s</b> after the room empties, fading out over <b>${esc(s.fade_out_s)} s</b>.</div>
        <div>After a light is switched off by hand, motion is ignored for <b>${esc(s.cooldown_s)} s</b>.</div>`
          : `<div>No sensors: the routine starts from ${(s.timers || []).some((t) => t.action !== "off") ? "its timers, " : ""}${s.on_by_hand === "routine" ? "the lights being switched on another way, " : ""}the <b>Start the routine</b> button or the <code>room_routines.switch_on</code> action, and runs until the lights are switched off.</div>`}
        ${(s.ends || []).length ? `<div>Goes dark at once, without waiting or fading, when ${s.ends.map((t) => `<b>${esc(this._name(t))}</b>`).join(" or ")} goes off.</div>` : ""}
        <div>Lights switched on another way (a wall switch, an app): <b>${esc(ON_BY_HAND[s.on_by_hand || "leave"])}</b>. A light changed by hand while the room runs it is left as set <b>${esc(this._handText(s))}</b>; the others carry on.</div>
        ${(s.timers || []).map((t) => `<div>Timer: ${esc(this._timerText(t))}.</div>`).join("")}
        ${(s.starters || []).map((c) => `<div>Starts when <b>${esc(this._condText(c))}</b>.</div>`).join("")}
        ${(s.only_when || []).length ? `<div>Starts only when ${s.only_when.map((c) => `<b>${esc(this._condText(c))}</b>`).join(" and ")}.</div>` : ""}
        ${[...(s.rules || []), ...((d.house.house_rules || []).filter((r) => !(r.rooms || []).length || r.rooms.includes(room.id)))]
          .map((r) => `<div>Rule: ${esc(this._ruleText(r))}${(d.house.house_rules || []).includes(r) ? " <small class='muted'>(house rule)</small>" : ""}.</div>`).join("")}
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

  // The look's actions as a row of icon buttons under the period name, so they
  // never get pushed off screen by a room with many lights.
  _lookActions(room, period, own) {
    const r = esc(room.id);
    const p = esc(period);
    const dim = this._track === "dim";
    const btn = (action, icon, label) =>
      `<button class="iconbtn" data-action="${action}" data-room="${r}" data-period="${p}" title="${esc(label)}" aria-label="${esc(label)} (${p})"><ha-icon icon="${icon}"></ha-icon></button>`;
    const save = this._admin
      ? btn("look-scene-now", "mdi:content-save-outline", "Save as now: save the lights as they are now as a Home Assistant scene, and use it for this look")
      : btn("look-current", "mdi:content-save-outline", "Save as now: save the lights as they are now as this look");
    return `<div class="iconpill">
      ${save}
      ${btn("look-pick-scene", "mdi:palette-outline", "Pick a scene you already have")}
      ${btn("look-edit", "mdi:pencil-outline", "Edit: set each light by hand")}
      ${btn("look-nothing", "mdi:lightbulb-off-outline", "Keep dark in this period")}
      ${own ? btn("look-borrow", "mdi:undo-variant", dim ? "Use Normal: remove this Dark Day look, the period uses its Normal one" : "Use previous: remove this period's own look, it uses the previous period's") : ""}
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
        ${ordered.length ? ordered.map((e) => `<option value="${esc(e)}" ${own?.scene === e ? "selected" : ""}>${esc(this._name(e))}${inArea(e) ? " (in this area)" : ""}${this._sceneReadable(e) ? "" : " ⚠ from another app"}</option>`).join("") : `<option value="">No scenes yet</option>`}
      </select></label>
      ${ordered.some((e) => !this._sceneReadable(e)) ? `<p class="explain"><ha-icon icon="mdi:alert-outline"></ha-icon> Scenes marked “from another app” (such as the Hue app's) can be turned on, but Room Routines can't read them: they can't be made dimmer or brighter on Dark Days, lights that can't fade by themselves jump to them, and suggestions can't compare with them. Saving the lights with <b>Save as now</b> makes a Home Assistant scene without these limits.</p>` : ""}
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
    const layer = this._layerOf(room);
    const T = this._layerTables(room, layer);
    const own = (this._track === "dim" ? T.dim : T.looks)[period];
    const lights = {};
    for (const l of room.settings.lights) {
      const t = own?.lights?.[l];
      if (!t) lights[l] = { mode: "skip" };
      else if (t.on === false) lights[l] = { mode: "off" };
      else if (t.brightness_pct == null) lights[l] = { mode: "last", color_temp_kelvin: t.color_temp_kelvin, rgb: t.rgb };
      else lights[l] = { mode: "set", brightness_pct: Math.round(t.brightness_pct), color_temp_kelvin: t.color_temp_kelvin, rgb: t.rgb };
    }
    this._editLook = { room: room.id, period, track: this._track, layer, draft: { nothing: !!own?.nothing, lights } };
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
      ? `<div class="card">${this._darkDaySummary(d.house)}</div>`
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
    if (this._houseRulesDraft) return this._houseRulesForm();
    if (this._loDraft) return this._lightsOutForm();
    return `
      <h2>Rooms</h2>
      <div class="card">
        ${d.rooms.length ? `<table class="plain">${d.rooms.map((r) => `<tr><td><b>${esc(r.name)}</b><div class="meta">${esc(r.area_name || "No area")} · ${esc(MODE_LABEL[r.mode] || r.mode)} · ${r.settings.lights.length} light${r.settings.lights.length === 1 ? "" : "s"}</div></td>
          <td class="actions"><button class="btn tiny" data-action="edit-room" data-room="${esc(r.id)}">Change</button> <button class="btn tiny danger" data-action="remove-room" data-room="${esc(r.id)}">${r.area_id && r.id.startsWith("area_") ? "Hide" : "Remove"}</button></td></tr>`).join("")}</table>` : `<div class="meta">No rooms yet.</div>`}
        <div class="row"><button class="btn primary small" data-action="add-room">Add a room</button></div>
      </div>
      ${(d.house.hidden_areas || []).length ? `<h2>Hidden areas</h2>
      <div class="card">
        <p class="explain">Areas you removed. They don't come back as rooms until you show them again.</p>
        <table class="plain">${d.house.hidden_areas.map((a) => `<tr><td>${esc(a.name)}</td><td class="actions"><button class="btn tiny" data-action="unhide-area" data-area="${esc(a.id)}">Show again</button></td></tr>`).join("")}</table>
      </div>` : ""}
      <h2>Periods</h2>
      <div class="card">
        <div>${d.house.order.map((p) => `<span class="chip" style="--c:${this._periodColour(p)}">${esc(p)} ${esc(d.house.periods.find((x) => x.name === p).start)}</span>`).join(" ")}</div>
        <div class="row"><button class="btn small" data-action="edit-periods">Change the periods</button></div>
      </div>
      <h2>House rules</h2>
      <div class="card">
        ${(d.house.house_rules || []).length
          ? (d.house.house_rules || []).map((r) => `<div>${esc(this._ruleText(r))} <small class="muted">${(r.rooms || []).length ? `in ${r.rooms.map((id) => esc(d.rooms.find((x) => x.id === id)?.name || id)).join(", ")}` : "in every room"}</small></div>`).join("")
          : `<div class="meta">None. A house rule changes how rooms behave while something holds: away from home, the baby asleep.</div>`}
        <div class="row"><button class="btn small" data-action="edit-house-rules">Change</button></div>
      </div>
      <h2>Lights out</h2>
      <div class="card">
        ${this._lightsOutSummary(d)}
        <div class="row"><button class="btn small" data-action="edit-lights-outs">Change</button></div>
      </div>
      <h2>Dark Days</h2>
      <div class="card">
        ${d.house.tracks?.enabled
          ? this._darkDaySummary(d.house)
          : `<div class="meta">Off: every day is a Normal day and rooms use their Normal looks. Switch Dark Days on to use Dark Day looks (or dimmer or brighter Normal ones) on grey days.</div>`}
        <div class="row"><button class="btn small" data-action="edit-tracks">Change</button></div>
      </div>
      <p class="explain">Settings are only shown to administrators. Everyone who can open this page can see the rooms and change their looks.</p>`;
  }

  _startRoomDraft(room) {
    if (room) {
      this._roomDraft = { id: room.id, name: room.name, area_id: room.area_id, mode: room.mode, ...JSON.parse(JSON.stringify(room.settings)) };
    } else {
      this._roomDraft = { id: null, name: "", area_id: null, lights: [], triggers: [], holds: [], lux_sensors: [], self_fading: [], on_by_hand: "leave", hand_hold: "until_off", hand_minutes: 30, ends: [], blends: {}, period_starts: {}, timers: [], starters: [], only_when: [], rules: [], signals: [], ...ROOM_DEFAULTS };
    }
    const r = this._roomDraft;
    r.on_by_hand = r.on_by_hand || "leave";
    r.lux_sensors = [...(r.lux_sensors || (r.lux_sensor ? [r.lux_sensor] : []))];
    r.hand_hold = r.hand_hold || "until_off";
    r.hand_minutes = r.hand_minutes || 30;
    r.ends = [...(r.ends || [])];
    r.blends = { ...(r.blends || {}) };
    r.period_starts = { ...(r.period_starts || {}) };
    r.timers = (r.timers || []).map((t) => ({ ...t, weekdays: [...(t.weekdays || [])], only_home: [...(t.only_home || [])] }));
    r.starters = (r.starters || []).map((c) => ({ ...c }));
    r.only_when = (r.only_when || []).map((c) => ({ ...c }));
    r.rules = (r.rules || []).map((x) => ({ ...x, when: { ...(x.when || {}) } }));
    r.signals = (r.signals || []).map((x) => ({ ...x, when: { ...(x.when || {}) }, lights: JSON.parse(JSON.stringify(x.lights || {})) }));
    r.search = {};
    this._roomError = null;
    this._entityList = null;
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
        ${this._picker("triggers", "Sensors that switch the lights on", "binary_sensor", "Optional. Any motion or presence sensor. When one of these sees someone and it's dark enough, the lights come on. A room without sensors runs on timers, a button or the lights being switched on.")}
        ${this._picker("holds", "Sensors that only keep the lights on", "binary_sensor", "Optional. These never switch the lights on, but keep them on while they see someone.")}
        ${[...r.triggers, ...r.holds].length ? `<fieldset><legend>Off at once</legend>
          <span class="help">For a sensor whose going off means everyone has left, such as a pantry or cupboard door closing: the lights go off straight away, without waiting for the other sensors, the timeout or the fade.</span><div>
          ${[...r.triggers, ...r.holds].map((e) => `<label class="inline"><input type="checkbox" data-ends="${esc(e)}" ${r.ends.includes(e) ? "checked" : ""}> ${esc(this._name(e))}</label>`).join("")}
        </div></fieldset>` : ""}
        ${this._picker("lux_sensors", "Light-level sensors", "sensor", "Optional. Only read while the room's lights are off. With more than one (each end of a staircase), the room counts as dark when any of them is.")}
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
        ${this._routineFields(r)}
        ${this._rulesFields(r)}
        ${this._signalsFields(r)}
        ${r.id ? `<label>Mode <select data-room-field="mode">${Object.entries(MODE_LABEL).map(([k, v]) => `<option value="${k}" ${k === r.mode ? "selected" : ""}>${v}</option>`).join("")}</select>
          <span class="help">${esc(MODE_HELP[r.mode] || "")}</span></label>` : `<p class="help">A new room starts in Log only, with every light on at its last brightness in every period.</p>`}
        <div class="row">
          <button class="btn primary" data-action="room-save">${r.id ? "Save" : "Add the room"}</button>
          <button class="btn" data-action="room-cancel">Cancel</button>
        </div>
      </div>`;
  }

  _handText(s) {
    if (s.hand_hold === "moves_on") return "until the routine moves on";
    if (s.hand_hold === "minutes") return `for ${s.hand_minutes || 30} min`;
    return "until the lights are switched off";
  }

  _timerText(t) {
    const days = t.days === "days" ? (t.weekdays || []).map((i) => WEEKDAYS[i].slice(0, 3)).join(", ") || "no days" : (TIMER_DAYS[t.days] || "Every day").toLowerCase();
    const bits = [`${t.action === "off" ? "switch off" : "start the routine"} at ${t.at || "?"}, ${days}`];
    if (t.only_dark) bits.push("only when it's dark");
    if ((t.only_home || []).length) bits.push(`only if ${t.only_home.map((p) => this._name(p)).join(" or ")} is home`);
    return bits.join(", ");
  }

  // The time-based half of a room: its own period times, blending, lights
  // switched on another way, and timers.
  _routineFields(r) {
    const d = this._data;
    const order = d?.house?.order || [];
    const people = d?.house?.people || [];
    const houseStart = (p) => d.house.periods.find((x) => x.name === p)?.start || "";
    const timers = r.timers
      .map((t, i) => `<div class="timer">
        <select data-timer="${i}" data-field="action" aria-label="What">
          <option value="on" ${t.action !== "off" ? "selected" : ""}>Start the routine</option>
          <option value="off" ${t.action === "off" ? "selected" : ""}>Switch off</option>
        </select>
        at <input type="time" value="${esc(t.at || "")}" data-timer="${i}" data-field="at" aria-label="At">
        <select data-timer="${i}" data-field="days" aria-label="Days">
          ${Object.entries(TIMER_DAYS).map(([k, v]) => `<option value="${k}" ${k === (t.days || "every_day") ? "selected" : ""}>${v}</option>`).join("")}
        </select>
        <button class="btn tiny danger" data-action="timer-remove" data-row="${i}">Remove</button>
        ${t.days === "days" ? `<div>${WEEKDAYS.map((w, j) => `<label class="inline"><input type="checkbox" data-timer-day="${i}" data-day="${j}" ${(t.weekdays || []).includes(j) ? "checked" : ""}> ${w.slice(0, 3)}</label>`).join("")}</div>` : ""}
        <div><label class="inline"><input type="checkbox" data-timer-check="${i}" data-field="only_dark" ${t.only_dark ? "checked" : ""}> Only when it's dark (the sun is down or it's a Dark Day)</label></div>
        ${people.length ? `<div>Only if one of these is home: ${people.map((p) => `<label class="inline"><input type="checkbox" data-timer-person="${i}" data-person="${esc(p)}" ${(t.only_home || []).includes(p) ? "checked" : ""}> ${esc(this._name(p))}</label>`).join("")}</div>` : ""}
      </div>`)
      .join("");
    const workday = d?.house?.workday_sensor
      ? `Workdays come from ${esc(this._name(d.house.workday_sensor))}.`
      : "Workdays are Monday to Friday; add Home Assistant's Workday integration to skip public holidays.";
    return `<fieldset><legend>Following the day</legend>
      <span class="help">For rooms that should follow the clock rather than (or as well as) motion: an office, a living room.</span>
      <label>When the lights are switched on another way <select data-room-field="on_by_hand">
        ${Object.entries(ON_BY_HAND).map(([k, v]) => `<option value="${k}" ${k === r.on_by_hand ? "selected" : ""}>${v}</option>`).join("")}
      </select><span class="help">A wall switch or another app. Starting the routine gives the lights the period's look and then follows the day.</span></label>
      <label>A light changed by hand is left as set <select data-room-field="hand_hold">
        ${Object.entries(HAND_HOLD).map(([k, v]) => `<option value="${k}" ${k === r.hand_hold ? "selected" : ""}>${v}</option>`).join("")}
      </select><span class="help">Only the lights you change are left alone; the room's other lights carry on with the routine. "Moves on" means a new period starts, the day turns into a Dark Day or back, a rule starts or ends, or someone arrives or leaves. Then the light goes back to what the room is doing.</span></label>
      ${r.hand_hold === "minutes" ? `<label>For <span class="inputunit"><input type="number" min="1" max="1440" step="5" value="${esc(r.hand_minutes)}" data-room-field="hand_minutes" aria-label="Minutes"> min</span></label>` : ""}
      <table class="plain periods">
        <tr><th>Period</th><th>This room's start</th><th>Blend into the next period over</th></tr>
        ${order.map((p) => `<tr>
          <td><span class="dot" style="background:${this._periodColour(p)}"></span>${esc(p)}</td>
          <td><input type="time" value="${esc(r.period_starts[p] || "")}" data-own-start="${esc(p)}" aria-label="${esc(p)} starts in this room"> <small class="muted">house: ${esc(houseStart(p))}</small></td>
          <td><span class="inputunit"><input type="number" min="0" max="1440" step="5" value="${esc(r.blends[p] || "")}" data-blend="${esc(p)}" placeholder="0" aria-label="Blend minutes"> min</span></td>
        </tr>`).join("")}
      </table>
      <span class="help">Leave a start empty to use the house's time; set one to move a period for this room only (an office's Evening after the working day). Blending moves brightness and colour gradually from one period's look to the next's over the minutes before it starts, while the routine runs (looks that set a level and colour; a look of last brightness or a scene from another app doesn't blend).</span>
      <h3>Timers</h3>
      ${timers || `<div class="meta">None.</div>`}
      <div class="row"><button class="btn small" data-action="timer-add">Add a timer</button></div>
      <span class="help">${workday} A timer that starts the routine in an empty room still switches off with the room's sensors, if it has any; without sensors it stays on until switched off.</span>
    </fieldset>`;
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

  // ---- conditions and rules (room form and house rules) ----

  _entityOptions() {
    if (this._entityList) return this._entityList;
    const states = this._hass.states;
    const ids = Object.keys(states).sort((a, b) => this._name(a).localeCompare(this._name(b)));
    this._entityList = `<datalist id="rr-entities">${ids
      .filter((e) => !e.startsWith("scene.") && !e.startsWith("automation.") && !e.startsWith("update."))
      .map((e) => `<option value="${esc(e)}">${esc(this._name(e))}</option>`).join("")}</datalist>`;
    return this._entityList;
  }

  _condFields(root, base, c) {
    const now = c.entity ? this._hass.states[c.entity]?.state : null;
    return `<input class="ent" list="rr-entities" placeholder="entity id, e.g. binary_sensor.baby_bedtime" value="${esc(c.entity || "")}" data-root="${root}" data-edit="${base}.entity" data-rerender aria-label="Entity">
      <select data-root="${root}" data-edit="${base}.negate" data-bool aria-label="Is or isn't">
        <option value="" ${c.negate ? "" : "selected"}>is</option><option value="1" ${c.negate ? "selected" : ""}>isn't</option>
      </select>
      <input class="state" value="${esc(c.state || "on")}" data-root="${root}" data-edit="${base}.state" aria-label="State">
      ${c.entity ? `<small class="muted">${now == null ? "not found" : `now ${esc(now)}`}</small>` : ""}`;
  }

  _ruleFields(root, base, r) {
    const d = this._data;
    const scenes = Object.keys(this._hass.states).filter((e) => e.startsWith("scene.")).sort((a, b) => this._name(a).localeCompare(this._name(b)));
    const target = r.scene ? `scene:${r.scene}` : r.period ? `period:${r.period}` : "";
    let extra = "";
    if (r.action === "look") {
      extra = `<select data-root="${root}" data-edit="${base}.@target" aria-label="Which look"><option value="">Choose…</option>
        <optgroup label="Another period's look">${(d.house.order || []).map((p) => `<option value="period:${esc(p)}" ${target === `period:${p}` ? "selected" : ""}>${esc(p)}</option>`).join("")}</optgroup>
        <optgroup label="A scene">${scenes.map((e) => `<option value="scene:${esc(e)}" ${target === `scene:${e}` ? "selected" : ""}>${esc(this._name(e))}</option>`).join("")}</optgroup>
      </select>`;
    } else if (r.action === "cap") {
      extra = `<span class="inputunit"><input type="number" min="1" max="100" step="1" value="${esc(r.max_pct ?? 20)}" data-root="${root}" data-edit="${base}.max_pct" data-number aria-label="Brightness limit"> %</span>`;
    }
    return `While ${this._condFields(root, `${base}.when`, r.when)}
      <div><select data-root="${root}" data-edit="${base}.action" data-rerender aria-label="What to do">
        ${Object.entries(RULE_ACTIONS).map(([k, v]) => `<option value="${k}" ${k === r.action ? "selected" : ""}>${v}</option>`).join("")}
      </select> ${extra}</div>`;
  }

  _condList(key, title, help) {
    const list = this._roomDraft[key];
    return `<h3>${esc(title)}</h3>
      ${list.map((c, i) => `<div class="cond">${this._condFields("room", `${key}.${i}`, c)} <button class="btn tiny danger" data-action="cond-remove" data-list="${key}" data-row="${i}">Remove</button></div>`).join("") || `<div class="meta">None.</div>`}
      <div class="row"><button class="btn small" data-action="cond-add" data-list="${key}">Add</button></div>
      <span class="help">${help}</span>`;
  }

  _rulesFields(r) {
    return `<fieldset><legend>Listening to the house</legend>
      ${this._entityOptions()}
      ${this._condList("starters", "Start the routine when", "Any entity: a computer switching on, a door opening, a person coming home. Unlike a motion sensor it doesn't switch the room off when it changes back. Type the state it must reach (on, home, open, playing…).")}
      ${this._condList("only_when", "Only start when", "Every way of starting (motion, a starter, a timer, lights taken over, the switch_on action) needs all of these. Use “isn't” for “not while”: Baby bedtime isn't on.")}
      <h3>While something holds</h3>
      ${r.rules.map((rule, i) => `<div class="cond rule">${this._ruleFields("room", `rules.${i}`, rule)} <button class="btn tiny danger" data-action="rule-remove" data-root="room" data-row="${i}">Remove</button></div>`).join("") || `<div class="meta">None.</div>`}
      <div class="row"><button class="btn small" data-action="rule-add" data-root="room">Add a rule</button></div>
      <span class="help">Do nothing: the room doesn't start at all (a lit room still goes dark as usual). Another look: a scene, or another period's look. No brighter than: every light kept at or below a level. The first rule that holds wins, this room's before the house's. When a rule starts or ends, a lit room moves to its new look.</span>
    </fieldset>`;
  }

  _signalsFields(r) {
    const hex = (rgb) => (rgb ? "#" + rgb.map((c) => Math.max(0, Math.min(255, c | 0)).toString(16).padStart(2, "0")).join("") : "#ffffff");
    const rows = r.signals.map((sig, i) => {
      const lights = r.lights.map((l) => {
        const t = sig.lights[l];
        const on = !!t;
        return `<div class="siglight">
          <label class="inline"><input type="checkbox" data-sig="${i}" data-sig-light="${esc(l)}" data-sig-field="include" ${on ? "checked" : ""}> ${esc(this._name(l))}</label>
          ${on ? `<input type="color" value="${esc(hex(t.rgb))}" data-sig="${i}" data-sig-light="${esc(l)}" data-sig-field="rgb" aria-label="Colour for ${esc(this._name(l))}">
          <span class="inputunit"><input type="number" min="1" max="100" step="1" value="${esc(t.brightness_pct ?? 100)}" data-sig="${i}" data-sig-light="${esc(l)}" data-sig-field="brightness_pct" aria-label="Brightness for ${esc(this._name(l))}"> %</span>` : ""}
        </div>`;
      }).join("");
      return `<div class="cond signal-row">
        <input class="name" placeholder="Name, e.g. In a call" value="${esc(sig.name || "")}" data-root="room" data-edit="signals.${i}.name" aria-label="Signal name">
        <div>While ${this._condFields("room", `signals.${i}.when`, sig.when)}</div>
        <div class="siglights">${lights || `<span class="meta">Add lights to the room first.</span>`}</div>
        <div><label class="inline"><input type="checkbox" data-sig="${i}" data-sig-field="flash" ${sig.flash ? "checked" : ""}> Flash once when it starts</label>
          <input class="effect" placeholder="Light effect (optional)" value="${esc(sig.effect || "")}" data-root="room" data-edit="signals.${i}.effect" aria-label="Light effect"></div>
        <button class="btn tiny danger" data-action="signal-remove" data-row="${i}">Remove</button>
      </div>`;
    }).join("");
    return `<fieldset><legend>Inform</legend>
      ${this._entityOptions()}
      ${rows || `<div class="meta">None.</div>`}
      <div class="row"><button class="btn small" data-action="signal-add">Add one</button></div>
      <span class="help">Some of the room's lights tell you something while it's true: in a call, the desk lamp purple; muted, green. While one holds a light, nothing else in the room touches it. It shows even when the room is off, and when it ends the light goes back to what the room is doing, or to how it was before. If two want the same light, the first in the list wins. A light effect only works if the light offers one by that name.</span>
    </fieldset>`;
  }

  _cleanSignals(signals) {
    return signals
      .filter((x) => x.when?.entity && Object.keys(x.lights || {}).length)
      .map((x) => {
        const out = { name: (x.name || "").trim() || "Inform", when: { entity: x.when.entity.trim(), state: (x.when.state || "on").trim() }, lights: x.lights };
        if (x.when.negate) out.when.negate = true;
        if (x.flash) out.flash = true;
        if ((x.effect || "").trim()) out.effect = x.effect.trim();
        return out;
      });
  }

  _roomNames(ids) {
    const d = this._data;
    return (ids || []).map((id) => esc(d.rooms.find((x) => x.id === id)?.name || id)).join(", ");
  }

  _lightsOutRun(r) {
    const when = new Date(r.at);
    const day = when.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
    let what;
    if (r.skipped) what = `skipped: ${esc(r.skipped)}`;
    else {
      const bits = [];
      const live = r.mode === "live";
      if ((r.off || []).length) bits.push(`${live ? "switched off" : "would switch off"} ${this._roomNames(r.off)}`);
      if ((r.waiting || []).length) bits.push(`${live ? "waited for" : "would wait for"} ${this._roomNames(r.waiting)}`);
      if ((r.loose || []).length) bits.push(`${live ? "switched off" : "would switch off"} ${r.loose.map((l) => esc(this._name(l))).join(", ")} (in no room)`);
      what = bits.join("; ") || "nothing was on";
      if (!live) what = `<span class="muted">log only:</span> ${what}`;
    }
    return `<div class="logrow"><span class="meta">${esc(day)} ${esc(hhmm(r.at))}</span> <b>${esc(r.name)}</b> <span class="meta">(${esc(r.why)})</span>: ${what}</div>`;
  }

  _lightsOutSummary(d) {
    const items = d.house.lights_outs || [];
    const runs = (d.house.lights_out_runs || []).slice(0, 6);
    return `
      ${items.length
        ? items.map((x) => `<div><b>${esc(x.name)}</b> ${esc(x.text || "")} · ${esc(LIGHTS_OUT_STYLE[x.style] || x.style)} · ${(x.rooms || []).length ? `in ${this._roomNames(x.rooms)}` : "the whole house"} · <span class="pill mode-${esc(x.mode)}">${esc(MODE_LABEL[x.mode] || x.mode)}</span></div>`).join("")
        : `<div class="meta">None. A Lights out switches the house off at the end of the day, without going dark on someone still in a room.</div>`}
      ${items.length && d.house.skip_entity ? `<div class="row"><label class="inline"><input type="checkbox" data-action="skip-lights-out" ${d.house.skip_next ? "checked" : ""}> Skip the next Lights out <span class="meta">(switches itself off after one)</span></label></div>` : ""}
      ${runs.length ? `<details data-pref="lights-out-runs"${loadPref("lights-out-runs", false) ? " open" : ""}><summary>Recent runs</summary>${runs.map((r) => this._lightsOutRun(r)).join("")}</details>` : ""}`;
  }

  _lightsOutForm() {
    const d = this._data;
    const items = this._loDraft.items;
    const row = (x, i) => `<div class="cond rule lightsout">
        <label>Name <input type="text" value="${esc(x.name || "")}" data-lo="${i}" data-field="name" aria-label="Name"></label>
        <label>When <select data-lo="${i}" data-field="when" data-rerender>
          ${Object.entries(LIGHTS_OUT_WHEN).map(([k, v]) => `<option value="${k}" ${k === x.when ? "selected" : ""}>${v}</option>`).join("")}
        </select></label>
        ${x.when === "time" ? `<label>At <input type="time" value="${esc(x.at || "")}" data-lo="${i}" data-field="at" aria-label="At"></label>` : ""}
        ${x.when === "period" ? `<label>Period <select data-lo="${i}" data-field="period">
          ${d.house.order.map((p) => `<option value="${esc(p)}" ${p === x.period ? "selected" : ""}>${esc(p)}</option>`).join("")}
        </select></label>` : ""}
        ${x.when === "entity" ? `<label>Entity <input class="ent" list="rr-entities" placeholder="e.g. input_boolean.bedtime" value="${esc(x.entity || "")}" data-lo="${i}" data-field="entity" aria-label="Entity"></label>
          <label>turns <input type="text" class="short" value="${esc(x.state || "on")}" data-lo="${i}" data-field="state" aria-label="State"></label>` : ""}
        <label>On <select data-lo="${i}" data-field="days" data-rerender>
          ${Object.entries(TIMER_DAYS).map(([k, v]) => `<option value="${k}" ${k === x.days ? "selected" : ""}>${v}</option>`).join("")}
        </select></label>
        ${x.days === "days" ? `<div>${WEEKDAYS.map((w, j) => `<label class="inline"><input type="checkbox" data-lo-day="${i}" data-day="${j}" ${(x.weekdays || []).includes(j) ? "checked" : ""}> ${w.slice(0, 3)}</label>`).join("")}</div>` : ""}
        <label>How <select data-lo="${i}" data-field="style" data-rerender>
          ${Object.entries(LIGHTS_OUT_STYLE).map(([k, v]) => `<option value="${k}" ${k === x.style ? "selected" : ""}>${v}</option>`).join("")}
        </select><span class="help">${esc(LIGHTS_OUT_STYLE_HELP[x.style] || "")} Rooms without sensors, and lights in no room, go off at once either way. Lights an Inform holds and power circuits are never touched.</span></label>
        <div><label class="inline"><input type="checkbox" data-lo-whole="${i}" ${(x.rooms || []).length ? "" : "checked"}> The whole house <span class="meta">(every room, and every light that's in no room, apart from groups and hidden lights)</span></label></div>
        ${(x.rooms || []).length || x._chosen ? `<div class="roomticks">${d.rooms.map((room) => `<label class="inline"><input type="checkbox" data-lo-room="${i}" data-room-id="${esc(room.id)}" ${(x.rooms || []).includes(room.id) ? "checked" : ""}> ${esc(room.name)}</label>`).join("")}</div>` : ""}
        <label>Mode <select data-lo="${i}" data-field="mode">
          ${["off", "log_only", "live"].map((k) => `<option value="${k}" ${k === x.mode ? "selected" : ""}>${MODE_LABEL[k]}</option>`).join("")}
        </select><span class="help">Log only says what it would have done (on this page and in the logbook) and switches nothing, so it can run beside an automation it replaces.</span></label>
        <div class="row">
          <button class="btn tiny" data-action="lo-preview" data-row="${i}">Check now</button>
          <button class="btn tiny danger" data-action="lo-remove" data-row="${i}">Remove</button>
        </div>
        ${x._preview ? `<div class="meta">If it ran now: ${esc(x._preview)}</div>` : ""}
      </div>`;
    return `
      <div class="detailhead"><button class="btn small" data-action="lo-cancel"><ha-icon icon="mdi:arrow-left"></ha-icon> Back</button></div>
      <div class="card form">
        <h2 class="inline">Lights out</h2>
        <p class="explain">Switch the house off at the end of the day, at a time, when a period starts or when something turns on (a bedtime switch). Once a room has gone off, motion works in it as usual. A new Lights out starts in log only.</p>
        ${this._loError ? `<div class="warntext">${esc(this._loError)}</div>` : ""}
        ${this._entityOptions()}
        ${items.map(row).join("") || `<div class="meta">None.</div>`}
        <div class="row"><button class="btn small" data-action="lo-add">Add a Lights out</button></div>
        <div class="row">
          <button class="btn primary" data-action="lo-save">Save</button>
          <button class="btn" data-action="lo-cancel">Cancel</button>
        </div>
      </div>`;
  }

  _cleanLightsOut(x) {
    const out = { name: (x.name || "").trim() || "Lights out", when: x.when, days: x.days || "every_day", style: x.style || "once_empty", mode: x.mode || "log_only" };
    if (x.id) out.id = x.id;
    if (x.when === "time") out.at = x.at || "";
    if (x.when === "period") out.period = x.period;
    if (x.when === "entity") {
      out.entity = (x.entity || "").trim();
      out.state = (x.state || "on").trim();
    }
    if (out.days === "days") out.weekdays = [...(x.weekdays || [])].sort();
    if ((x.rooms || []).length) out.rooms = [...x.rooms];
    return out;
  }

  _houseRulesForm() {
    const d = this._data;
    const rules = this._houseRulesDraft.rules;
    return `
      <div class="detailhead"><button class="btn small" data-action="house-rules-cancel"><ha-icon icon="mdi:arrow-left"></ha-icon> Back</button></div>
      <div class="card form">
        <h2 class="inline">House rules</h2>
        <p class="explain">While something holds, change how rooms behave: away from home (do nothing), the baby asleep (a night look, or no brighter than a level). Tick the rooms a rule is for; none ticked means every room. A room's own rules come first.</p>
        ${this._houseRulesError ? `<div class="warntext">${esc(this._houseRulesError)}</div>` : ""}
        ${this._entityOptions()}
        ${rules.map((rule, i) => `<div class="cond rule">${this._ruleFields("house", `${i}`, rule)}
          <div class="roomticks">${d.rooms.map((room) => `<label class="inline"><input type="checkbox" data-rule-room="${i}" data-room-id="${esc(room.id)}" ${(rule.rooms || []).includes(room.id) ? "checked" : ""}> ${esc(room.name)}</label>`).join("")}</div>
          <button class="btn tiny danger" data-action="rule-remove" data-root="house" data-row="${i}">Remove</button></div>`).join("") || `<div class="meta">None.</div>`}
        <div class="row"><button class="btn small" data-action="rule-add" data-root="house">Add a rule</button></div>
        <div class="row">
          <button class="btn primary" data-action="house-rules-save">Save</button>
          <button class="btn" data-action="house-rules-cancel">Cancel</button>
        </div>
      </div>`;
  }

  _editRoot(name) {
    return name === "house" ? this._houseRulesDraft.rules : this._roomDraft;
  }

  _setPath(obj, path, value) {
    const keys = path.split(".");
    let o = obj;
    for (const k of keys.slice(0, -1)) o = o[/^\d+$/.test(k) ? Number(k) : k];
    const last = keys[keys.length - 1];
    if (last === "@target") {
      const [kind, ...rest] = value.split(":");
      const v = rest.join(":");
      delete o.scene;
      delete o.period;
      if (kind === "scene") o.scene = v;
      if (kind === "period") o.period = v;
      return;
    }
    o[last] = value;
  }

  _cleanRules(rules, withRooms) {
    return rules
      .filter((r) => r.when?.entity)
      .map((r) => {
        const out = { when: { entity: r.when.entity.trim(), state: (r.when.state || "on").trim() }, action: r.action || "nothing" };
        if (r.when.negate) out.when.negate = true;
        if (out.action === "look") {
          if (r.scene) out.scene = r.scene;
          else if (r.period) out.period = r.period;
        }
        if (out.action === "cap") out.max_pct = Number(r.max_pct ?? 20);
        if (withRooms && (r.rooms || []).length) out.rooms = r.rooms;
        return out;
      });
  }

  _cleanConds(list) {
    return list.filter((c) => c.entity).map((c) => {
      const out = { entity: c.entity.trim(), state: (c.state || "on").trim() };
      if (c.negate) out.negate = true;
      return out;
    });
  }

  _startTracksDraft(d) {
    const t = d.house.tracks || {};
    this._tracksDraft = {
      on: !!t.on,
      weather: t.weather !== false,
      sensor: t.sensor || null,
      fallback: t.fallback || null,
      first: t.first || "weather",
      periods: [...(t.periods || [])],
      dark_below_pct: t.dark_below_pct ?? 40,
      normal_above_pct: t.normal_above_pct ?? 55,
      brightness_pct: t.brightness_pct ?? 100,
      dark_below_wm2: t.dark_below_wm2 ?? 150,
    };
    this._tracksError = null;
  }

  _tracksForm() {
    const t = this._tracksDraft;
    const order = this._data?.house?.order || [];
    const sensors = Object.keys(this._hass.states)
      .filter((e) => e.startsWith("sensor."))
      .sort((a, b) => {
        const la = this._hass.states[a].attributes.device_class === "illuminance" ? 0 : 1;
        const lb = this._hass.states[b].attributes.device_class === "illuminance" ? 0 : 1;
        return la - lb || this._name(a).localeCompare(this._name(b));
      });
    const pick = (key, none) => `<select data-tracks-field="${key}"><option value="">${none}</option>
      ${sensors.map((e) => `<option value="${esc(e)}" ${e === t[key] ? "selected" : ""}>${esc(this._name(e))}</option>`).join("")}</select>`;
    const pct = Number(t.brightness_pct ?? 100);
    const brighten = pct > 100
      ? `<div class="warntext"><ha-icon icon="mdi:alert-outline"></ha-icon> Brightening only works on lights set below their maximum: a light at 60 % at ${esc(pct)} % comes on at ${Math.min(100, Math.round(60 * pct / 100))} %, but one already at 100 % can't go any higher.</div>`
      : "";
    return `
      <div class="detailhead"><button class="btn small" data-action="tracks-cancel"><ha-icon icon="mdi:arrow-left"></ha-icon> Back</button></div>
      <div class="card form">
        <h2 class="inline">Dark Days</h2>
        <p class="explain">On a Dark Day rooms use their Dark Day looks: grey, overcast days, or mornings before sunrise. A day counts as dark by comparing the light now with a clear day at the same height of the sun, so an ordinary evening or a bright cloudy day isn't dark. The reading is averaged over 15 minutes, and a day stays Normal or Dark for at least 20 minutes, so passing clouds don't flip it.</p>
        ${this._tracksError ? `<div class="warntext">${esc(this._tracksError)}</div>` : ""}
        <label class="inline"><input type="checkbox" data-tracks-check="on" ${t.on ? "checked" : ""}> Use Dark Days</label>
        <h3>Where the light reading comes from</h3>
        <label class="inline"><input type="checkbox" data-tracks-check="weather" ${t.weather ? "checked" : ""}> The weather</label>
        <span class="help">The sunlight reaching the ground at Home Assistant's location, from Open-Meteo (free, no account; asked four times an hour). Enough on its own.</span>
        <label>Light sensor (optional) ${pick("sensor", "None")}
          <span class="help">One that sees the sky: outdoors, or indoors facing out of a window. It learns what a clear day looks like to it from its own history, so it needs a few days (and at least one bright one) before it can answer.</span></label>
        <label>If it's unavailable, use ${pick("fallback", "Nothing")}</label>
        <label>Check first <select data-tracks-field="first">
          <option value="weather" ${t.first !== "sensor" ? "selected" : ""}>The weather</option>
          <option value="sensor" ${t.first === "sensor" ? "selected" : ""}>The light sensor</option>
        </select>
          <span class="help">The other is used when the first has no answer (the weather service can't be reached, or the sensor is unavailable or still learning).</span></label>
        <h3>When</h3>
        <div>${order.map((p) => `<label class="inline"><input type="checkbox" data-tracks-period="${esc(p)}" ${t.periods.includes(p) ? "checked" : ""}> ${esc(p)}</label>`).join("")}</div>
        <span class="help">Dark Days only happen in these periods; any other period is always a Normal day. Choose the daytime ones: the sun being down counts as dark.</span>
        <div class="twocol">
          <label>Dark Day below <span class="inputunit"><input type="number" min="0" max="199" step="5" value="${esc(t.dark_below_pct)}" data-tracks-field="dark_below_pct"> %</span>
            <span class="help">Of a clear day's light at the same sun height.</span></label>
          <label>Normal again above <span class="inputunit"><input type="number" min="1" max="200" step="5" value="${esc(t.normal_above_pct)}" data-tracks-field="normal_above_pct"> %</span>
            <span class="help">Higher than the Dark Day level, so the day doesn't flip back and forth around one level.</span></label>
        </div>
        <label>Also a Dark Day below <span class="inputunit"><input type="number" min="0" max="600" step="10" value="${esc(t.dark_below_wm2)}" data-tracks-field="dark_below_wm2"> W/m²</span>
          <span class="help">Of sunlight reaching the ground, from the weather, whatever a clear day would give. In winter the sun stays so low that even a clear noon is darker than a grey summer day: at 150, mid-winter days are dark all day, and summer barely changes. Normal again above 1.2 times this. 0 switches it off.</span></label>
        <h3>Normal looks on Dark Days</h3>
        <label>Brightness <span class="inputunit"><input type="number" min="1" max="300" step="5" value="${esc(t.brightness_pct)}" data-tracks-field="brightness_pct"> %</span>
          <span class="help">For periods without their own Dark Day look: their Normal look at this brightness. 100 leaves it as it is; below 100 dims (a light at 66 % at 50 % comes on at 33 %); above 100 brightens, up to each light's maximum. Colours stay; lights set to their last brightness, and scenes from other apps (such as the Hue app), are left as they are.</span></label>
        ${brighten}
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
        this._editLook = this._roomDraft = this._periodDraft = this._tracksDraft = this._scenePick = this._houseRulesDraft = this._loDraft = null;
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
        if (f === "area_id" || f === "mode" || f === "hand_hold") this._render();
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
    root.querySelectorAll("[data-edit]").forEach((el) =>
      el.addEventListener("change", () => {
        let value = el.value;
        if (el.dataset.bool !== undefined) value = el.value === "1";
        else if (el.dataset.number !== undefined) value = el.value === "" ? null : Number(el.value);
        this._setPath(this._editRoot(el.dataset.root), el.dataset.edit, value);
        if (el.dataset.rerender !== undefined) this._render();
      })
    );
    root.querySelectorAll("[data-sig-field]").forEach((el) =>
      el.addEventListener("change", () => {
        const sig = this._roomDraft.signals[Number(el.dataset.sig)];
        const light = el.dataset.sigLight;
        const field = el.dataset.sigField;
        if (field === "flash") sig.flash = el.checked;
        else if (field === "include") {
          if (el.checked) sig.lights[light] = { on: true, brightness_pct: 100, rgb: [255, 255, 255] };
          else delete sig.lights[light];
          this._render();
        } else if (field === "rgb") {
          const v = el.value.replace("#", "");
          sig.lights[light] = { ...sig.lights[light], on: true, rgb: [0, 2, 4].map((i) => parseInt(v.slice(i, i + 2), 16)) };
          delete sig.lights[light].color_temp_kelvin;
        } else if (field === "brightness_pct") {
          sig.lights[light] = { ...sig.lights[light], on: true, brightness_pct: Math.max(1, Math.min(100, Number(el.value) || 100)) };
        }
      })
    );
    root.querySelectorAll("[data-rule-room]").forEach((el) =>
      el.addEventListener("change", () => {
        const rule = this._houseRulesDraft.rules[Number(el.dataset.ruleRoom)];
        rule.rooms = (rule.rooms || []).filter((x) => x !== el.dataset.roomId);
        if (el.checked) rule.rooms.push(el.dataset.roomId);
      })
    );
    root.querySelectorAll("[data-own-start]").forEach((el) =>
      el.addEventListener("change", () => {
        if (el.value) this._roomDraft.period_starts[el.dataset.ownStart] = el.value;
        else delete this._roomDraft.period_starts[el.dataset.ownStart];
      })
    );
    root.querySelectorAll("[data-blend]").forEach((el) =>
      el.addEventListener("change", () => {
        const m = Number(el.value);
        if (m > 0) this._roomDraft.blends[el.dataset.blend] = m;
        else delete this._roomDraft.blends[el.dataset.blend];
      })
    );
    root.querySelectorAll("[data-lo]").forEach((el) =>
      el.addEventListener("change", () => {
        const x = this._loDraft.items[Number(el.dataset.lo)];
        x[el.dataset.field] = el.value;
        x._preview = null;
        if (el.dataset.rerender !== undefined) this._render();
      })
    );
    root.querySelectorAll("[data-lo-day]").forEach((el) =>
      el.addEventListener("change", () => {
        const x = this._loDraft.items[Number(el.dataset.loDay)];
        const day = Number(el.dataset.day);
        x.weekdays = (x.weekdays || []).filter((w) => w !== day);
        if (el.checked) x.weekdays.push(day);
      })
    );
    root.querySelectorAll("[data-lo-whole]").forEach((el) =>
      el.addEventListener("change", () => {
        const x = this._loDraft.items[Number(el.dataset.loWhole)];
        x._chosen = !el.checked;
        if (el.checked) x.rooms = [];
        x._preview = null;
        this._render();
      })
    );
    root.querySelectorAll("[data-lo-room]").forEach((el) =>
      el.addEventListener("change", () => {
        const x = this._loDraft.items[Number(el.dataset.loRoom)];
        x.rooms = (x.rooms || []).filter((r) => r !== el.dataset.roomId);
        if (el.checked) x.rooms.push(el.dataset.roomId);
        x._preview = null;
      })
    );
    root.querySelectorAll("[data-timer]").forEach((el) =>
      el.addEventListener("change", () => {
        this._roomDraft.timers[Number(el.dataset.timer)][el.dataset.field] = el.value;
        if (el.dataset.field === "days") this._render();
      })
    );
    root.querySelectorAll("[data-timer-check]").forEach((el) =>
      el.addEventListener("change", () => {
        this._roomDraft.timers[Number(el.dataset.timerCheck)][el.dataset.field] = el.checked;
      })
    );
    root.querySelectorAll("[data-timer-day]").forEach((el) =>
      el.addEventListener("change", () => {
        const t = this._roomDraft.timers[Number(el.dataset.timerDay)];
        const j = Number(el.dataset.day);
        t.weekdays = (t.weekdays || []).filter((x) => x !== j);
        if (el.checked) t.weekdays.push(j);
        t.weekdays.sort();
      })
    );
    root.querySelectorAll("[data-timer-person]").forEach((el) =>
      el.addEventListener("change", () => {
        const t = this._roomDraft.timers[Number(el.dataset.timerPerson)];
        t.only_home = (t.only_home || []).filter((x) => x !== el.dataset.person);
        if (el.checked) t.only_home.push(el.dataset.person);
      })
    );
    root.querySelectorAll("[data-ends]").forEach((el) =>
      el.addEventListener("change", () => {
        const e = el.dataset.ends;
        const list = this._roomDraft.ends.filter((x) => x !== e);
        if (el.checked) list.push(e);
        this._roomDraft.ends = list;
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
        if (f === "brightness_pct") this._render(); // the warning follows the number
      })
    );
    root.querySelectorAll("[data-tracks-check]").forEach((el) =>
      el.addEventListener("change", () => {
        this._tracksDraft[el.dataset.tracksCheck] = el.checked;
      })
    );
    root.querySelectorAll("[data-tracks-period]").forEach((el) =>
      el.addEventListener("change", () => {
        const p = el.dataset.tracksPeriod;
        const set = new Set(this._tracksDraft.periods);
        if (el.checked) set.add(p);
        else set.delete(p);
        this._tracksDraft.periods = (this._data?.house?.order || []).filter((x) => set.has(x));
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
  async _saveScene(room, period, track, entities, layer = "base") {
    try {
      const draft = await this._hass.callWS({ type: "room_routines/scene_draft", room_id: room.id, period, track, layer });
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
      const res = await this._ws({ type: "room_routines/save_look", room_id: room.id, period, track, layer, how: "scene", scene_id: draft.config_id });
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
      case "look-layer":
        this._lookLayer = el.dataset.layer;
        savePref("look-layer", this._lookLayer);
        this._editLook = this._scenePick = null;
        this._render();
        return;
      case "look-track":
        this._track = el.dataset.track;
        savePref("look-track", this._track);
        this._editLook = this._scenePick = null;
        this._render();
        return;
      case "look-scene-now": {
        const period = el.dataset.period;
        const ok = await this._saveScene(room, period, this._track, null, this._layerOf(room));
        if (ok) this._notice = { text: `Saved the lights as the ${period} look${this._track === "dim" ? " for Dark Days" : ""}, as the scene "${ok}".` };
        this._render();
        return;
      }
      case "look-pick-scene":
        this._scenePick = { room: room.id, period: el.dataset.period, track: this._track, layer: this._layerOf(room) };
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
          { type: "room_routines/save_look", room_id: pick.room, period: pick.period, track: pick.track, layer: pick.layer || "base", how: "scene", scene },
          `${pick.period}${pick.track === "dim" ? " on Dark Days" : ""} now turns on ${this._name(scene)}.`
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
          {
            type: "room_routines/save_tracks",
            on: !!t.on,
            weather: !!t.weather,
            sensor: t.sensor,
            fallback: t.fallback,
            first: t.first || "weather",
            periods: t.periods,
            dark_below_pct: t.dark_below_pct ?? 40,
            normal_above_pct: t.normal_above_pct ?? 55,
            brightness_pct: t.brightness_pct ?? 100,
            dark_below_wm2: t.dark_below_wm2 ?? 150,
          },
          t.on ? "Saved the Dark Day settings." : "Dark Days are off."
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
          { type: "room_routines/save_look", room_id: e.room, period: e.period, track: e.track, layer: e.layer || "base", how: "custom", look: this._lookFromDraft(e.draft) },
          `Saved the ${e.period} look${e.track === "dim" ? " for Dark Days" : ""}.`
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
          current: `Saved the lights as they are now as the ${period} look${dim ? " for Dark Days" : ""}.`,
          nothing: `${room.name} stays dark in ${period}${dim ? " on Dark Days" : ""}.`,
          borrow: dim ? `${period} now uses its Normal look on Dark Days.` : `${period} now uses the previous period's look.`,
        }[how];
        const res = await this._ws({ type: "room_routines/save_look", room_id: room.id, period, track: this._track, layer: this._layerOf(room), how }, text);
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
        const hide = room.area_id && room.id.startsWith("area_");
        if (!confirm(hide
          ? `Hide ${room.name}? Its looks and settings go, and the area doesn't come back as a room until you show it again under Hidden areas. The lights themselves are untouched.`
          : `Remove ${room.name}? Its looks and settings go with it. The lights and sensors themselves are untouched.${room.area_id ? " Its area is hidden, so it doesn't come back as a room by itself." : ""}`)) return;
        const res = await this._ws({ type: "room_routines/remove_room", room_id: room.id }, `Removed ${room.name}.`);
        if (!res.ok) this._notice = { text: res.error, bad: true };
        this._render();
        return;
      }
      case "cond-add":
        this._roomDraft[el.dataset.list].push({ entity: "", state: "on", negate: false });
        this._render();
        return;
      case "cond-remove":
        this._roomDraft[el.dataset.list].splice(Number(el.dataset.row), 1);
        this._render();
        return;
      case "signal-add":
        this._roomDraft.signals.push({ name: "", when: { entity: "", state: "on" }, lights: {} });
        this._render();
        return;
      case "signal-remove":
        this._roomDraft.signals.splice(Number(el.dataset.row), 1);
        this._render();
        return;
      case "rule-add":
        (el.dataset.root === "house" ? this._houseRulesDraft.rules : this._roomDraft.rules).push({ when: { entity: "", state: "on" }, action: "nothing", rooms: [] });
        this._render();
        return;
      case "rule-remove":
        (el.dataset.root === "house" ? this._houseRulesDraft.rules : this._roomDraft.rules).splice(Number(el.dataset.row), 1);
        this._render();
        return;
      case "skip-lights-out":
        await this._call("switch", el.checked ? "turn_on" : "turn_off", { entity_id: d.house.skip_entity });
        return;
      case "edit-lights-outs":
        this._loDraft = { items: (d.house.lights_outs || []).map((x) => ({ ...x, weekdays: [...(x.weekdays || [])], rooms: [...(x.rooms || [])] })) };
        this._loError = null;
        this._entityList = null;
        this._render();
        return;
      case "lo-add":
        this._loDraft.items.push({ name: "Lights out", when: "time", at: "01:00", days: "every_day", weekdays: [], rooms: [], style: "once_empty", mode: "log_only" });
        this._render();
        return;
      case "lo-remove":
        this._loDraft.items.splice(Number(el.dataset.row), 1);
        this._render();
        return;
      case "lo-cancel":
        this._loDraft = null;
        this._render();
        return;
      case "lo-preview": {
        const x = this._loDraft.items[Number(el.dataset.row)];
        const res = await this._ws({ type: "room_routines/lights_out_preview", lights_out: this._cleanLightsOut(x) });
        x._preview = res.ok ? res.result.text : res.error;
        this._render();
        return;
      }
      case "lo-save": {
        const items = this._loDraft.items;
        if (items.some((x) => x.when === "time" && !x.at)) {
          this._loError = "Every Lights out at a time needs the time.";
          this._render();
          return;
        }
        const res = await this._ws({ type: "room_routines/save_lights_outs", lights_outs: items.map((x) => this._cleanLightsOut(x)) }, "Saved the Lights outs.");
        if (!res.ok) {
          this._loError = res.error;
          this._render();
          return;
        }
        this._loDraft = null;
        this._render();
        return;
      }
      case "edit-house-rules":
        this._houseRulesDraft = { rules: (d.house.house_rules || []).map((x) => ({ ...x, when: { ...x.when }, rooms: [...(x.rooms || [])] })) };
        this._houseRulesError = null;
        this._entityList = null;
        this._render();
        return;
      case "house-rules-cancel":
        this._houseRulesDraft = null;
        this._render();
        return;
      case "house-rules-save": {
        const res = await this._ws({ type: "room_routines/save_house_rules", rules: this._cleanRules(this._houseRulesDraft.rules, true) }, "Saved the house rules.");
        if (!res.ok) {
          this._houseRulesError = res.error;
          this._render();
          return;
        }
        this._houseRulesDraft = null;
        this._render();
        return;
      }
      case "timer-add":
        this._roomDraft.timers.push({ at: "", action: "on", days: "every_day", weekdays: [], only_dark: false, only_home: [] });
        this._render();
        return;
      case "timer-remove":
        this._roomDraft.timers.splice(Number(el.dataset.row), 1);
        this._render();
        return;
      case "routine-start":
      case "routine-stop":
        await this._call("room_routines", action === "routine-start" ? "switch_on" : "switch_off", { entity_id: room.status_entity });
        return;
      case "unhide-area": {
        const res = await this._ws({ type: "room_routines/unhide_area", area_id: el.dataset.area }, "Shown again: it comes back as a room, switched off.");
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
          lux_sensors: r.lux_sensors,
          threshold_lux: r.threshold_lux ?? null,
          timeout_s: r.timeout_s ?? ROOM_DEFAULTS.timeout_s,
          fade_out_s: r.fade_out_s ?? ROOM_DEFAULTS.fade_out_s,
          cooldown_s: r.cooldown_s ?? ROOM_DEFAULTS.cooldown_s,
          drift_s: r.drift_s ?? ROOM_DEFAULTS.drift_s,
          self_fading: r.self_fading.filter((l) => r.lights.includes(l)),
          on_by_hand: r.on_by_hand || "leave",
          hand_hold: r.hand_hold || "until_off",
          hand_minutes: Math.max(1, Math.min(1440, Number(r.hand_minutes) || 30)),
          ends: r.ends.filter((e) => r.triggers.includes(e) || r.holds.includes(e)),
          blends: r.blends,
          period_starts: r.period_starts,
          starters: this._cleanConds(r.starters),
          only_when: this._cleanConds(r.only_when),
          rules: this._cleanRules(r.rules, false),
          signals: this._cleanSignals(r.signals),
          timers: r.timers.map((t) => {
            const out = { at: t.at || "", action: t.action || "on", days: t.days || "every_day" };
            if (out.days === "days") out.weekdays = t.weekdays || [];
            if (t.only_dark) out.only_dark = true;
            if ((t.only_home || []).length) out.only_home = t.only_home;
            return out;
          }),
        };
        if (body.timers.some((t) => !t.at)) {
          this._roomError = "Every timer needs a time.";
          this._render();
          return;
        }
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
  h3 { font-size:14px; font-weight:500; margin:16px 0 4px; }
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
  .logrow { padding:3px 0; border-top:1px solid var(--divider-color); }
  input.short { width:6em; }
  .offrooms { margin-top:12px; }
  .offrooms summary { cursor:pointer; color: var(--secondary-text-color); font-size:14px; padding:8px 0; }
  .openrow { cursor:pointer; }
  .openrow:hover td, .openrow:focus-visible td { background: var(--secondary-background-color); }
  .timer { border-top:1px solid var(--divider-color); padding:8px 0; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  .timer > div { flex-basis:100%; }
  th.periodcell { white-space:nowrap; vertical-align:top; }
  .iconpill { display:inline-flex; gap:2px; margin-top:6px; padding:2px; border:1px solid var(--divider-color); border-radius:16px; background: var(--secondary-background-color); }
  .iconbtn { display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; padding:0; border:none; border-radius:50%; background:none; color: var(--secondary-text-color); cursor:pointer; }
  .iconbtn:hover, .iconbtn:focus-visible { background: color-mix(in srgb, var(--primary-color) 15%, transparent); color: var(--primary-color); outline:none; }
  .iconbtn ha-icon { --mdc-icon-size:18px; }
  .cond { border-top:1px solid var(--divider-color); padding:8px 0; display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  .cond > div { flex-basis:100%; }
  .cond input.ent { flex:1 1 220px; min-width:180px; }
  .cond input.state { width:90px; }
  .roomticks { display:flex; flex-wrap:wrap; gap:4px 12px; }
  ha-icon.small { --mdc-icon-size:16px; }
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
  .signal { color: var(--accent-color, #7a3fd1); font-weight: 600; }
  .signal-row .name { width: 100%; max-width: 22rem; }
  .siglights { display: flex; flex-wrap: wrap; gap: 6px 16px; margin: 6px 0; }
  .siglight { display: flex; align-items: center; gap: 6px; }
  .siglight input[type=color] { width: 2.4rem; height: 1.8rem; padding: 0; border: 1px solid var(--divider-color); border-radius: 4px; background: none; }
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
