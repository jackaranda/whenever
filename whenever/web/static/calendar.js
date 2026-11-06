/* whenever web calendar: FullCalendar view plus dialogs for viewing, creating, editing and deleting events. */
(function () {
  "use strict";

  const config = JSON.parse(document.getElementById("config").textContent);
  const TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
  const WEEKDAY_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  const WEEKDAY_LONG = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
  const ORDINALS = { 1: "first", 2: "second", 3: "third", 4: "fourth", "-1": "last" };
  const UNITS = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year" };
  const writable = config.calendars.filter((c) => c.can_write);
  const $ = (id) => document.getElementById(id);
  const form = $("editor-form");

  // ------------------------------------------------------------ storage (per-browser conveniences only)

  function load(key, fallback) {
    try {
      const value = localStorage.getItem("whenever:" + key);
      return value === null ? fallback : JSON.parse(value);
    } catch (e) {
      return fallback;
    }
  }

  function store(key, value) {
    try {
      localStorage.setItem("whenever:" + key, JSON.stringify(value));
    } catch (e) {
      /* storage unavailable: ignore */
    }
  }

  // ------------------------------------------------------------ dates

  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hm = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const weekdayIndex = (d) => (d.getDay() + 6) % 7; // Monday = 0

  function dateOnly(s) {
    const [y, m, d] = s.slice(0, 10).split("-").map(Number);
    return new Date(y, m - 1, d);
  }

  function addDays(d, n) {
    const r = new Date(d.getTime());
    r.setDate(r.getDate() + n);
    return r;
  }

  function combine(dateStr, timeStr) {
    const [y, m, d] = dateStr.split("-").map(Number);
    const [hh, mm] = (timeStr || "00:00").split(":").map(Number);
    return new Date(y, m - 1, d, hh, mm);
  }

  const fmtDate = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const fmtTime = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });

  function describeWhen(item) {
    if (item.all_day) {
      const s = dateOnly(item.start);
      const e = addDays(dateOnly(item.end), -1);
      return e <= s ? `${fmtDate.format(s)} (all day)` : `${fmtDate.format(s)} – ${fmtDate.format(e)}`;
    }
    const s = new Date(item.start);
    const e = new Date(item.end);
    if (ymd(s) === ymd(e)) return `${fmtDate.format(s)}, ${fmtTime.format(s)} – ${fmtTime.format(e)}`;
    return `${fmtDate.format(s)} ${fmtTime.format(s)} – ${fmtDate.format(e)} ${fmtTime.format(e)}`;
  }

  // ------------------------------------------------------------ API

  async function api(method, path, body) {
    const opts = { method, cache: "no-store", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(config.api + path, opts);
    if (res.status === 204) return null;
    let data = null;
    try {
      data = await res.json();
    } catch (e) {
      /* not JSON */
    }
    if (!res.ok) {
      let message = `Something went wrong (${res.status}).`;
      if (data && data.error) message = data.error;
      else if (data && Array.isArray(data.detail)) message = data.detail.map((d) => d.msg).join("; ");
      throw new Error(message);
    }
    return data;
  }

  let toastTimer;
  function toast(message, isError) {
    const t = $("toast");
    t.textContent = message;
    t.className = isError ? "error" : "";
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, isError ? 6000 : 2500);
  }

  // ------------------------------------------------------------ calendar list

  const hidden = new Set(load("hidden", []));

  function renderCalendarList() {
    const list = $("calendar-list");
    list.replaceChildren();
    if (!config.calendars.length) {
      const li = document.createElement("li");
      li.className = "note";
      li.textContent = "You're not in any calendars yet.";
      list.append(li);
      return;
    }
    for (const cal of config.calendars) {
      const li = document.createElement("li");
      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = !hidden.has(cal.slug);
      box.style.accentColor = cal.color;
      box.addEventListener("change", () => {
        if (box.checked) hidden.delete(cal.slug); else hidden.add(cal.slug);
        store("hidden", [...hidden]);
        calendar.refetchEvents();
      });
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = cal.color;
      const name = document.createElement("span");
      name.textContent = cal.name;
      label.append(box, swatch, name);
      if (!cal.can_write) {
        const ro = document.createElement("span");
        ro.className = "ro";
        ro.textContent = "view only";
        label.append(ro);
      }
      li.append(label);
      list.append(li);
    }
  }

  // ------------------------------------------------------------ FullCalendar

  const narrow = window.matchMedia("(max-width: 760px)").matches;

  const calendar = new FullCalendar.Calendar($("calendar"), {
    initialView: load("view", narrow ? "listWeek" : "dayGridMonth"),
    headerToolbar: narrow
      ? { left: "title", right: "prev,next" }
      : { left: "prev,next today", center: "title", right: "dayGridMonth,timeGridWeek,timeGridDay,listMonth" },
    footerToolbar: narrow ? { left: "today", right: "dayGridMonth,timeGridDay,listWeek" } : false,
    buttonText: { today: "Today", month: "Month", week: "Week", day: "Day", list: "List" },
    firstDay: 1,
    height: narrow ? "auto" : "100%",
    nowIndicator: true,
    navLinks: true,
    dayMaxEvents: true,
    scrollTime: "08:00:00",
    selectable: writable.length > 0,
    selectMirror: true,
    events: fetchEvents,
    select: onSelect,
    eventClick(info) {
      info.jsEvent.preventDefault();
      const p = info.event.extendedProps;
      openDetails(p.event_id, p.occurrence);
    },
    eventDrop: onMove,
    eventResize: onMove,
    eventDidMount(info) {
      const p = info.event.extendedProps;
      info.el.title = [info.event.title, p.location, "Created by " + p.owner].filter(Boolean).join("\n");
    },
    datesSet(info) {
      store("view", info.view.type);
    },
  });

  function fetchEvents(info, success, failure) {
    const query = `?start=${encodeURIComponent(info.startStr)}&end=${encodeURIComponent(info.endStr)}`;
    api("GET", query)
      .then((items) => success(items.filter((e) => !hidden.has(e.extendedProps.calendar))))
      .catch((err) => {
        toast(err.message, true);
        failure(err);
      });
  }

  function onSelect(info) {
    calendar.unselect();
    let { start, end, allDay } = info;
    if (allDay && ymd(addDays(start, 1)) === ymd(end)) {
      // A click on a single day: suggest a one-hour event in the morning.
      start = new Date(start.getTime());
      start.setHours(9, 0, 0, 0);
      end = new Date(start.getTime());
      end.setHours(10);
      allDay = false;
    }
    openEditor({ mode: "create", start, end, allDay });
  }

  async function onMove(info) {
    const ev = info.event;
    const p = ev.extendedProps;
    const start = ev.start;
    const end = ev.end || (ev.allDay ? addDays(start, 1) : new Date(start.getTime() + 3600000));
    const body = ev.allDay
      ? { all_day: true, start: ymd(start), end: ymd(addDays(end, -1)) }
      : { all_day: false, start: `${ymd(start)}T${hm(start)}`, end: `${ymd(end)}T${hm(end)}` };
    body.tz = TZ;
    body.etag = p.etag;
    try {
      await api("POST", `/${p.event_id}/move`, body);
      toast("Moved");
      calendar.refetchEvents();
    } catch (err) {
      info.revert();
      toast(err.message, true);
    }
  }

  // ------------------------------------------------------------ details dialog

  let current = null; // event data from the API, for the open details dialog

  async function openDetails(eventId, occurrence) {
    let data;
    try {
      data = await api("GET", `/${eventId}` + (occurrence ? `?occurrence=${encodeURIComponent(occurrence)}` : ""));
    } catch (err) {
      toast(err.message, true);
      return;
    }
    current = data;
    const item = data.occurrence || data.series;
    $("details-title").textContent = item.title || "(no title)";
    $("details-color").style.background = data.color;
    $("details-when").textContent = describeWhen(item);
    setOptional("repeat", data.recurring ? data.repeat_text || "Yes" : "");
    setOptional("location", item.location);
    $("details-description").textContent = item.description;
    $("details-calendar").textContent = data.calendar_name;
    $("details-owner").textContent = data.owner;
    $("details-edit").hidden = !data.can_edit;
    $("details-delete").hidden = !data.can_edit;
    $("details").showModal();
  }

  function setOptional(name, value) {
    document.querySelectorAll(".opt-" + name).forEach((el) => { el.hidden = !value; });
    $("details-" + name).textContent = value || "";
  }

  // ------------------------------------------------------------ editor dialog

  let editor = null; // { mode: "create" | "edit", data? }
  let duration = 3600000;
  let weekdaysTouched = false; // until the user picks weekdays, follow the start date's weekday

  const startValue = () => combine($("f-start-date").value, $("f-allday").checked ? "00:00" : $("f-start-time").value);
  const endValue = () => combine($("f-end-date").value, $("f-allday").checked ? "00:00" : $("f-end-time").value);
  const startDate = () => dateOnly($("f-start-date").value || ymd(new Date()));
  const scopeIsThis = () => !$("scope-row").hidden && form.elements.scope.value === "this";

  function rememberDuration() {
    const d = endValue() - startValue();
    if (!Number.isNaN(d) && d >= 0) duration = d;
  }

  function openEditor(opts) {
    editor = opts;
    form.reset();
    $("editor-error").hidden = true;
    const isEdit = opts.mode === "edit";
    const data = opts.data;
    $("editor-title").textContent = isEdit ? "Edit event" : "New event";

    const select = $("f-calendar");
    select.replaceChildren();
    if (!isEdit) {
      for (const c of writable) select.append(new Option(c.name, c.slug));
      const last = load("lastCalendar", null);
      if (writable.some((c) => c.slug === last)) select.value = last;
    }
    $("calendar-row").hidden = isEdit;

    const instance = isEdit && data.recurring && data.occurrence;
    $("scope-row").hidden = !instance;
    form.elements.scope.value = instance ? "this" : "all";
    $("opt-custom").hidden = !(isEdit && data.repeat_kind === "custom");

    if (isEdit) {
      fillFromData();
    } else {
      setTimes(opts.start, opts.end, opts.allDay);
      setRepeat(null);
    }
    updateVisibility();
    $("editor").showModal();
    $("f-title").focus();
  }

  function fillFromData() {
    const data = editor.data;
    const item = scopeIsThis() && data.occurrence ? data.occurrence : data.series;
    $("f-title").value = item.title;
    $("f-location").value = item.location;
    $("f-description").value = item.description;
    if (item.all_day) setTimes(dateOnly(item.start), dateOnly(item.end), true);
    else setTimes(new Date(item.start), new Date(item.end), false);
    if (data.repeat_kind === "custom") {
      setRepeat(null);
      $("f-repeat").value = "CUSTOM";
      $("custom-text").textContent = data.repeat_text;
    } else {
      setRepeat(data.repeat);
    }
  }

  function setTimes(start, end, allDay) {
    $("f-allday").checked = allDay;
    $("f-start-date").value = ymd(start);
    $("f-start-time").value = allDay ? "09:00" : hm(start);
    const last = allDay ? addDays(end, -1) : end;
    $("f-end-date").value = ymd(last < start ? start : last);
    $("f-end-time").value = allDay ? "10:00" : hm(end);
    rememberDuration();
  }

  function buildWeekdays() {
    const box = $("f-weekdays");
    WEEKDAYS.forEach((code, i) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = code;
      input.setAttribute("aria-label", WEEKDAY_LONG[i]);
      const text = document.createElement("span");
      text.textContent = WEEKDAY_SHORT[i];
      label.append(input, text);
      box.append(label);
    });
  }

  function monthlyNths(d) {
    const n = Math.floor((d.getDate() - 1) / 7) + 1;
    const out = n <= 4 ? [n] : [];
    const daysInMonth = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate();
    if (d.getDate() + 7 > daysInMonth) out.push(-1);
    return out;
  }

  function refreshMonthly(selected) {
    const box = $("f-monthly");
    const checked = box.querySelector("input:checked");
    const want = selected || (checked && checked.value) || "day";
    box.replaceChildren();
    const d = startDate();
    const options = [["day", `On day ${d.getDate()}`]];
    for (const n of monthlyNths(d)) options.push([String(n), `On the ${ORDINALS[n]} ${WEEKDAY_LONG[weekdayIndex(d)]}`]);
    for (const [value, text] of options) {
      const label = document.createElement("label");
      label.className = "inline";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "monthly";
      input.value = value;
      input.checked = value === want;
      label.append(input, " " + text);
      box.append(label);
    }
    if (!box.querySelector("input:checked")) box.querySelector("input").checked = true;
  }

  function setRepeat(rep) {
    $("f-repeat").value = rep ? rep.freq : "";
    $("f-interval").value = rep ? rep.interval : 1;
    const days = rep && rep.byday && rep.byday.length ? rep.byday : [WEEKDAYS[weekdayIndex(startDate())]];
    $("f-weekdays").querySelectorAll("input").forEach((input) => { input.checked = days.includes(input.value); });
    weekdaysTouched = Boolean(rep && rep.byday && rep.byday.length);
    refreshMonthly(rep && rep.freq === "MONTHLY" && rep.monthly_mode === "weekday" ? String(rep.nth) : "day");
    form.elements.ends.value = rep ? rep.ends : "never";
    $("f-until").value = rep && rep.until ? rep.until : "";
    $("f-count").value = rep && rep.count ? rep.count : 10;
  }

  function updateVisibility() {
    form.classList.toggle("all-day", $("f-allday").checked);
    const thisOnly = scopeIsThis();
    $("repeat-section").hidden = thisOnly;
    const freq = $("f-repeat").value;
    const custom = freq === "CUSTOM";
    $("custom-text").hidden = !custom;
    $("repeat-details").hidden = !freq || custom;
    $("weekly-row").hidden = freq !== "WEEKLY";
    $("monthly-row").hidden = freq !== "MONTHLY";
    if (UNITS[freq]) $("interval-unit").textContent = UNITS[freq] + (Number($("f-interval").value) === 1 ? "" : "s");
    const data = editor && editor.data;
    $("exceptions-warning").hidden = !(data && data.has_exceptions && !thisOnly);
  }

  function showFormError(message) {
    const box = $("editor-error");
    box.textContent = message;
    box.hidden = false;
  }

  function onStartChanged() {
    const start = startValue();
    if (Number.isNaN(start.getTime())) return;
    const end = new Date(start.getTime() + duration);
    $("f-end-date").value = ymd(end);
    if (!$("f-allday").checked) $("f-end-time").value = hm(end);
    if (!weekdaysTouched) {
      const code = WEEKDAYS[weekdayIndex(startDate())];
      $("f-weekdays").querySelectorAll("input").forEach((input) => { input.checked = input.value === code; });
    }
    refreshMonthly();
  }

  async function onSubmit(event) {
    event.preventDefault();
    $("editor-error").hidden = true;
    const allDay = $("f-allday").checked;
    const sd = $("f-start-date").value;
    const ed = $("f-end-date").value;
    if (!sd || !ed || (!allDay && (!$("f-start-time").value || !$("f-end-time").value))) {
      showFormError("Enter when the event starts and ends.");
      return;
    }
    if (endValue() < startValue()) {
      showFormError("The event ends before it starts.");
      return;
    }
    const body = {
      title: $("f-title").value,
      location: $("f-location").value,
      description: $("f-description").value,
      all_day: allDay,
      start: allDay ? sd : `${sd}T${$("f-start-time").value}`,
      end: allDay ? ed : `${ed}T${$("f-end-time").value}`,
      tz: TZ,
    };

    const thisOnly = scopeIsThis();
    if (!thisOnly) {
      const freq = $("f-repeat").value;
      if (freq === "CUSTOM") {
        body.keep_repeat = true;
      } else if (freq) {
        const ends = form.elements.ends.value;
        const repeat = { freq, interval: Number($("f-interval").value) || 1, ends };
        if (freq === "WEEKLY") {
          repeat.byday = [...$("f-weekdays").querySelectorAll("input:checked")].map((i) => i.value);
        }
        if (freq === "MONTHLY") {
          const mode = form.elements.monthly.value;
          if (mode === "day") repeat.monthly_mode = "day";
          else Object.assign(repeat, { monthly_mode: "weekday", nth: Number(mode) });
        }
        if (ends === "until") {
          if (!$("f-until").value) {
            showFormError("Choose the date the series ends.");
            return;
          }
          repeat.until = $("f-until").value;
        }
        if (ends === "count") repeat.count = Number($("f-count").value) || 1;
        body.repeat = repeat;
      }
    }

    const saveButton = $("editor-save");
    saveButton.disabled = true;
    try {
      if (editor.mode === "create") {
        body.calendar = $("f-calendar").value;
        store("lastCalendar", body.calendar);
        await api("POST", "", body);
        toast("Event added");
      } else {
        const data = editor.data;
        body.etag = data.etag;
        body.scope = thisOnly ? "this" : "all";
        if (thisOnly) body.occurrence = data.occurrence.id;
        const result = await api("PUT", `/${data.id}`, body);
        toast(result && result.exceptions_cleared ? "Saved. Changes to single occurrences were removed." : "Saved");
      }
      $("editor").close();
      calendar.refetchEvents();
    } catch (err) {
      showFormError(err.message);
    } finally {
      saveButton.disabled = false;
    }
  }

  // ------------------------------------------------------------ delete

  let pendingDelete = null;

  function confirmDelete(data) {
    const instance = data.recurring && data.occurrence;
    const item = data.occurrence || data.series;
    pendingDelete = data;
    $("confirm-title").textContent = instance ? "Delete repeating event?" : "Delete event?";
    $("confirm-text").textContent = `“${item.title || "(no title)"}” will be removed for everyone in ${data.calendar_name}.`;
    $("confirm-this").hidden = !instance;
    $("confirm-all").textContent = instance ? "Delete all occurrences" : "Delete";
    $("details").close();
    $("confirm").showModal();
  }

  async function doDelete(scope) {
    const data = pendingDelete;
    const params = new URLSearchParams({ scope, etag: data.etag });
    if (scope === "this") params.set("occurrence", data.occurrence.id);
    try {
      await api("DELETE", `/${data.id}?${params}`);
      $("confirm").close();
      toast("Deleted");
      calendar.refetchEvents();
    } catch (err) {
      toast(err.message, true);
    }
  }

  // ------------------------------------------------------------ wiring

  function closeOnBackdrop(dialog) {
    dialog.addEventListener("click", (e) => {
      if (e.target !== dialog) return;
      const r = dialog.getBoundingClientRect();
      const inside = e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
      if (!inside) dialog.close();
    });
  }

  function nextHour() {
    const start = new Date();
    start.setMinutes(0, 0, 0);
    start.setHours(start.getHours() + 1);
    return start;
  }

  buildWeekdays();
  renderCalendarList();
  ["details", "editor", "confirm"].forEach((id) => closeOnBackdrop($(id)));

  const newButton = $("new-event");
  newButton.disabled = writable.length === 0;
  if (!writable.length) newButton.title = "You can only view these calendars.";
  newButton.addEventListener("click", () => {
    const start = nextHour();
    openEditor({ mode: "create", start, end: new Date(start.getTime() + 3600000), allDay: false });
  });

  $("details-close").addEventListener("click", () => $("details").close());
  $("details-edit").addEventListener("click", () => {
    $("details").close();
    openEditor({ mode: "edit", data: current });
  });
  $("details-delete").addEventListener("click", () => confirmDelete(current));

  $("confirm-cancel").addEventListener("click", () => $("confirm").close());
  $("confirm-this").addEventListener("click", () => doDelete("this"));
  $("confirm-all").addEventListener("click", () => doDelete("all"));

  $("editor-cancel").addEventListener("click", () => $("editor").close());
  form.addEventListener("submit", onSubmit);
  $("f-allday").addEventListener("change", () => { updateVisibility(); rememberDuration(); });
  $("f-start-date").addEventListener("change", onStartChanged);
  $("f-start-time").addEventListener("change", onStartChanged);
  $("f-end-date").addEventListener("change", rememberDuration);
  $("f-end-time").addEventListener("change", rememberDuration);
  $("f-repeat").addEventListener("change", updateVisibility);
  $("f-weekdays").addEventListener("change", () => { weekdaysTouched = true; });
  $("f-interval").addEventListener("input", updateVisibility);
  form.querySelectorAll('input[name="scope"]').forEach((radio) => {
    radio.addEventListener("change", () => { fillFromData(); updateVisibility(); });
  });

  calendar.render();
})();
