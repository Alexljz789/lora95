/* LoRa95 front-end — Windows 95 styled, dependency free. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  const state = {
    data: null,
    selected: null,
    detail: null,
    detailKey: null,
    fetchingDetail: false,
    tab: "overview",
    settingsLoaded: false,
    showSignals: true,
    sort: "last",
    filter: "",
    profiles: {},
    planKey: null,
    eventSource: null,
    lastStateAt: 0,
    lastDetailAt: 0,
    clockTimer: null,
    saveTimer: null,
    profileTimer: null,
    modalResolve: null,
    autoSelect: new URLSearchParams(window.location.search).get("select"),
    autoTab: new URLSearchParams(window.location.search).get("tab"),
    autoStarted: false,
  };

  const ICONS = {
    meshtastic: "i-node",
    lorawan: "i-sensor",
    raw: "i-raw",
    signal: "i-signal",
  };
  const KIND_LABEL = {
    meshtastic: "Meshtastic",
    lorawan: "LoRaWAN",
    raw: "私有 LoRa",
    signal: "载波",
  };
  const KIND_BADGE = {
    meshtastic: "meshtastic",
    lorawan: "lorawan",
    raw: "raw",
    signal: "signal",
  };

  // ---------------------------------------------------------------------
  // helpers
  // ---------------------------------------------------------------------
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  }

  function icon(kind) {
    return `<svg><use href="#${ICONS[kind] || "i-raw"}"/></svg>`;
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function fmtDate(ts) {
    if (!ts) return "—";
    const d = new Date(ts * 1000);
    return d.toLocaleString("zh-CN", { hour12: false });
  }

  function fmtAgo(ts) {
    if (!ts) return "—";
    const age = Math.max(0, Date.now() / 1000 - ts);
    if (age < 2) return "刚刚";
    if (age < 60) return `${Math.floor(age)} 秒前`;
    if (age < 3600) return `${Math.floor(age / 60)} 分前`;
    if (age < 86400) return `${Math.floor(age / 3600)} 小时前`;
    return `${Math.floor(age / 86400)} 天前`;
  }

  function fmtFreq(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n.toFixed(3) : "—";
  }

  function fmtNum(value, digits = 0) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "—";
    return n.toFixed(digits);
  }

  function fmtBytes(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    if (v < 1024) return `${v} B`;
    if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
    return `${(v / 1024 / 1024).toFixed(1)} MB`;
  }

  function fmtDuration(seconds) {
    const s = Math.max(0, Math.floor(Number(seconds) || 0));
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return `${d}天 ${h}小时`;
    if (h) return `${h}小时 ${m}分`;
    if (m) return `${m}分`;
    return `${s}秒`;
  }

  function short(text, n = 120) {
    const value = String(text ?? "");
    return value.length > n ? `${value.slice(0, n)}…` : value;
  }

  function pretty(value) {
    try {
      return JSON.stringify(value, null, 2);
    } catch (_) {
      return String(value);
    }
  }

  function rssiBars(rssi) {
    if (rssi === null || rssi === undefined) return '<span class="rssi-bars"><i></i><i></i><i></i><i></i></span>';
    const value = Number(rssi);
    const level = value >= -80 ? 4 : value >= -92 ? 3 : value >= -105 ? 2 : 1;
    const cls = value < -105 ? "low" : value < -92 ? "mid" : "";
    let bars = "";
    for (let i = 1; i <= 4; i += 1) bars += `<i class="${i <= level ? "on" : ""}" style="height:${2 + i * 1.6}px"></i>`;
    return `<span class="rssi-bars ${cls}">${bars}</span>`;
  }

  function hexdump(hex) {
    const clean = String(hex || "").replace(/[^0-9a-fA-F]/g, "");
    if (!clean) return '<div class="hexdump">无数据</div>';
    const bytes = [];
    for (let i = 0; i < clean.length; i += 2) bytes.push(parseInt(clean.slice(i, i + 2), 16));
    const lines = [];
    for (let offset = 0; offset < bytes.length; offset += 16) {
      const chunk = bytes.slice(offset, offset + 16);
      const hexPart = chunk.map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(47, " ");
      const ascii = chunk.map((b) => (b >= 32 && b < 127 ? String.fromCharCode(b) : ".")).join("");
      lines.push(`<span class="off">${offset.toString(16).padStart(6, "0")}</span>  <span class="hex">${hexPart}</span>  <span class="asc">${esc(ascii)}</span>`);
    }
    return `<div class="hexdump">${lines.join("\n")}</div>`;
  }

  function copyText(text, label = "已复制") {
    const value = String(text ?? "");
    const done = () => flash(`${label} (${value.length} 字符)`);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done).catch(() => fallbackCopy(value, done));
    } else {
      fallbackCopy(value, done);
    }
  }

  function fallbackCopy(value, done) {
    const area = document.createElement("textarea");
    area.value = value;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    try { document.execCommand("copy"); done(); } catch (_) { flash("复制失败"); }
    document.body.removeChild(area);
  }

  function flash(message) {
    const el = $("#status-last");
    if (el) el.textContent = message;
  }

  // ---------------------------------------------------------------------
  // dialogs
  // ---------------------------------------------------------------------
  function closeModal(value) {
    $("#modal-overlay").classList.remove("show");
    if (state.modalResolve) {
      const resolve = state.modalResolve;
      state.modalResolve = null;
      resolve(value === undefined ? false : value);
    }
  }

  function openModal(title, bodyHtml, buttons, iconId = "i-info") {
    $("#modal-title").textContent = title;
    $("#modal-body").innerHTML = bodyHtml;
    const bar = $("#modal-buttons");
    bar.innerHTML = "";
    (buttons || [{ label: "确定", primary: true, value: true }]).forEach((button) => {
      const el = document.createElement("button");
      el.className = `btn${button.primary ? " primary" : ""}`;
      el.textContent = button.label;
      el.addEventListener("click", () => {
        if (typeof button.onClick === "function") button.onClick();
        closeModal(button.value === undefined ? true : button.value);
      });
      bar.appendChild(el);
    });
    $("#modal-overlay").classList.add("show");
    setTimeout(() => {
      const first = bar.querySelector("button");
      if (first) first.focus();
    }, 30);
  }

  function confirmDialog(title, bodyHtml, okLabel = "是") {
    return new Promise((resolve) => {
      state.modalResolve = resolve;
      openModal(title, bodyHtml, [
        { label: okLabel, primary: true, value: true },
        { label: "取消", value: false },
      ], "i-info");
    });
  }

  function showError(message, title = "LoRa95") {
    openModal(title, `<p>${esc(message)}</p>`, [{ label: "确定", primary: true, value: true }], "i-info");
  }

  function showAbout() {
    openModal(
      "关于 LoRa95",
      `<h3>LoRa95 1.0</h3>
       <p>Windows 95 风格的 LoRa 被动扫描器，为 ClockworkPi uConsole + HackerGadgets AIO v2 编写。</p>
       <p><b>硬件</b>：Semtech SX126x @ SPI1，GPIO24 BUSY / GPIO25 RESET / GPIO26 DIO1，1.8 V TCXO。</p>
       <p><b>协议</b>：Meshtastic（默认 PSK 可解密）、LoRaWAN MAC 层、CayenneLPP、私有 LoRa 帧。</p>
       <p>本程序只接收。它不会替你发送 Meshtastic 消息，也不会上传任何数据。</p>
       <p style="color:#606060">98.css (MIT) · W95FA 字体 · 本工程以 MIT 许可证发布。</p>`,
      [{ label: "确定", primary: true, value: true }]
    );
  }

  function showLimits() {
    openModal(
      "扫描能力与限制",
      `<h3>为什么不是“一键发现所有 LoRa 设备”？</h3>
       <p>LoRa 是物理层调制，不像 Wi-Fi/蓝牙那样有广播式发现机制。接收机必须与发射端使用相同的
       <b>频率、带宽、扩频因子（SF）、编码率和同步字</b> 才能解调。一次只能锁定一组参数。</p>
       <p>LoRa95 的做法：</p>
       <ul>
         <li>按 <b>区域计划</b> 轮询 Meshtastic 常用预设与 LoRaWAN 上行信道；</li>
         <li>对不同 SF/带宽做多次驻留，抓到帧后解析；</li>
         <li>用 <b>CAD</b> 检测无法解调的载波，至少把“有信号”显示出来；</li>
         <li>私有协议帧会以十六进制/ASCII 广播内容显示，但无法凭空得知厂商型号。</li>
       </ul>
       <p>因此，不发射的设备无法被发现；使用未知同步字/编码率的设备只能显示为载波或原始帧。
       这不是软件缺陷，而是 LoRa 的工作方式。</p>
       <p>本机 SX1262 与 meshtasticd 共用 SPI1；扫描时 LoRa95 会暂停 meshtasticd，退出后自动恢复。</p>`,
      [{ label: "确定", primary: true, value: true }]
    );
  }

  // ---------------------------------------------------------------------
  // settings / form
  // ---------------------------------------------------------------------
  function fillSettings(settings) {
    if (!settings) return;
    const region = $("#set-region");
    if (region && !region.options.length) {
      // populated by loadRegions()
    }
    if (region) region.value = settings.region || "US";
    $("#set-mode").value = settings.mode || "quick";
    $("#set-dwell").value = Math.round((Number(settings.dwell) || 1.2) * 1000);
    $("#set-rssi").value = settings.min_rssi ?? -140;
    $("#set-cad").checked = !!settings.cad;
    $("#set-raw").checked = settings.include_raw !== false;
    $("#set-freq").value = settings.custom_frequency_mhz ?? 906.875;
    $("#set-sf").value = settings.custom_sf ?? 11;
    $("#set-bw").value = String(settings.custom_bw_khz ?? 250);
    $("#set-cr").value = String(settings.custom_cr ?? 5);
    $("#set-sync").value = String(settings.custom_sync_word ?? 0x2B);
    $("#set-preamble").value = settings.custom_preamble ?? 16;
    $("#device-sort").value = settings.sort || "last";
    state.sort = settings.sort || "last";
    state.filter = settings.filter || "";
    $("#device-filter").value = state.filter;
    state.settingsLoaded = true;
    updateCustomVisibility();
    updatePlanCount();
  }

  function collectSettings() {
    const mode = $("#set-mode").value;
    return {
      region: $("#set-region").value || "US",
      mode,
      dwell: Math.max(0.3, Math.min(8, Number($("#set-dwell").value || 1200) / 1000)),
      min_rssi: Math.max(-150, Math.min(-20, Number($("#set-rssi").value || -140))),
      cad: $("#set-cad").checked,
      include_raw: $("#set-raw").checked,
      custom_frequency_mhz: Number($("#set-freq").value || 906.875),
      custom_sf: Number($("#set-sf").value || 11),
      custom_bw_khz: Number($("#set-bw").value || 250),
      custom_cr: Number($("#set-cr").value || 5),
      custom_sync_word: Number($("#set-sync").value || 43),
      custom_preamble: Number($("#set-preamble").value || 16),
      sort: state.sort,
      filter: state.filter,
      theme: "classic",
    };
  }

  function updateCustomVisibility() {
    const isCustom = $("#set-mode").value === "custom";
    $("#custom-grid").classList.toggle("show", isCustom);
  }

  function scheduleSave() {
    clearTimeout(state.saveTimer);
    state.saveTimer = setTimeout(async () => {
      try {
        await fetch("/api/settings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(collectSettings()),
        });
      } catch (_) { /* offline is fine */ }
    }, 450);
  }

  async function updatePlanCount() {
    const region = $("#set-region").value || "US";
    const mode = $("#set-mode").value || "quick";
    const key = `${region}:${mode}`;
    if (state.planKey === key) return;
    state.planKey = key;
    try {
      const res = await fetch(`/api/profiles?region=${encodeURIComponent(region)}&mode=${encodeURIComponent(mode)}`);
      const data = await res.json();
      if (data.ok) {
        $("#plan-pill").textContent = `${data.count} 频点`;
        state.profiles[key] = data.items || [];
      }
    } catch (_) {
      $("#plan-pill").textContent = "—";
    }
  }

  async function loadRegions() {
    try {
      const res = await fetch("/api/profiles?region=US&mode=quick");
      const data = await res.json();
      if (!data.ok) return;
      const select = $("#set-region");
      select.innerHTML = "";
      (data.regions || []).forEach((region) => {
        const option = document.createElement("option");
        option.value = region.id;
        option.textContent = region.label;
        select.appendChild(option);
      });
      if (state.data?.settings?.region) select.value = state.data.settings.region;
      updatePlanCount();
    } catch (_) { /* keep fallback */ }
  }

  async function startScan(silent = false) {
    const payload = collectSettings();
    $("#btn-scan-start").disabled = true;
    $("#btn-start").disabled = true;
    flash("正在启动扫描…");
    try {
      const res = await fetch("/api/scan/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "无法启动扫描");
      state.planKey = null;
      flash("扫描已启动");
      setTimeout(fetchState, 400);
    } catch (err) {
      if (silent) flash(`自动扫描未启动：${String(err.message || err)}`);
      else showError(String(err.message || err), "无法启动扫描");
    } finally {
      $("#btn-scan-start").disabled = false;
      $("#btn-start").disabled = false;
    }
  }

  async function stopScan() {
    try {
      await fetch("/api/scan/stop", { method: "POST" });
      flash("扫描已停止");
      setTimeout(fetchState, 300);
    } catch (err) {
      showError(String(err.message || err));
    }
  }

  async function monitorSelected() {
    const profile = state.detail?.last_profile || state.detail?.profile || state.data?.devices?.find((d) => d.id === state.selected)?.profile;
    if (!profile) { showError("选中的设备还没有可用的频点信息。"); return; }
    try {
      const res = await fetch("/api/monitor", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ profile }),
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "锁定失败");
      flash(`已锁定 ${profile.frequency_mhz} MHz · SF${profile.sf}/BW${profile.bw_khz}`);
      setTimeout(fetchState, 300);
    } catch (err) {
      showError(String(err.message || err));
    }
  }

  // ---------------------------------------------------------------------
  // state / status
  // ---------------------------------------------------------------------
  function handleState(data) {
    if (!data || typeof data !== "object") return;
    state.data = data;
    state.lastStateAt = Date.now();
    if (!state.settingsLoaded) fillSettings(data.settings);
    $("#demo-badge").style.display = data.demo ? "inline-block" : "none";
    if (data.demo) $("#title-sub").textContent = "uConsole · DEMO 演示数据";
    updateStatus(data);
    renderDevices();
    if (state.selected && !(data.devices || []).some((d) => d.id === state.selected)) {
      state.selected = null;
      state.detail = null;
      state.detailKey = null;
      renderHead(null);
      renderDetailBody();
    }
    refreshDetail();
    if (state.tab === "band") renderDetailBody();
    if (state.autoSelect && (data.devices || []).some((d) => d.id === state.autoSelect)) {
      const id = state.autoSelect;
      state.autoSelect = null;
      selectDevice(id);
      if (state.autoTab) setTab(state.autoTab);
    }
    maybeAutoStart();
  }

  function maybeAutoStart() {
    if (state.autoStarted || !state.data) return;
    if (state.data.demo) { state.autoStarted = true; return; }
    if (state.data.settings?.auto_scan === false) return;
    if (state.data.scan?.running) { state.autoStarted = true; return; }
    if (!state.data.connected) return;
    state.autoStarted = true;
    setTimeout(() => startScan(true), 150);
  }

  function updateStatus(data) {
    const connected = !!data.connected;
    const scan = data.scan || {};
    const radio = data.radio || {};
    const stats = data.stats || {};
    const led = $("#radio-led");
    led.className = `led ${connected ? "ok" : "bad"}`;
    $("#status-led").className = `led ${connected ? "ok" : "bad"}`;
    const radioText = connected
      ? (scan.running ? "扫描中" : "无线服务已连接")
      : "无线服务未连接";
    $("#radio-text").textContent = radioText;
    $("#status-radio").textContent = connected
      ? (scan.running
        ? `${radio.radio?.chip || "SX126x"} · 扫描中`
        : `${radio.radio?.chip || "SX126x"} · ${radio.meshtasticd === "active" ? "meshtasticd 运行中" : "radio ready"}`)
      : (data.link_error || "radio: offline");
    $("#scan-spinner").classList.toggle("on", !!scan.running);
    const profile = scan.profile || null;
    const profileLabel = profile ? `${fmtFreq(profile.frequency_mhz)} MHz · SF${profile.sf}/BW${Number(profile.bw_khz)}` : "—";
    $("#scan-text").textContent = scan.monitor ? "锁定监听" : scan.running ? "扫描中" : "空闲";
    $("#status-profile").textContent = `profile: ${profileLabel}`;
    const deviceCount = (data.devices || []).length;
    $("#status-stats").textContent = `${deviceCount} 设备 / ${stats.packets || 0} 包`;
    $("#device-count").textContent = String(deviceCount);
    const emptyBox = $("#list-empty");
    if (emptyBox) {
      const title = emptyBox.querySelector("b");
      const spans = emptyBox.querySelectorAll("span");
      if (scan.running) {
        if (title) title.textContent = "正在扫描…";
        if (spans[0]) spans[0].textContent = "正在轮换频点与参数，收到帧后会出现在这里。";
        if (spans[1]) spans[1].textContent = "Meshtastic / LoRaWAN / 私有协议";
      } else {
        if (title) title.textContent = "尚未发现设备";
        if (spans[0]) spans[0].textContent = "按“开始”扫描附近的 LoRa 信号。";
        if (spans[1]) spans[1].textContent = "Meshtastic / LoRaWAN / 私有协议";
      }
    }
    const progress = scan.total > 0 ? Math.min(100, Math.round((scan.index / scan.total) * 100)) : 0;
    $("#scan-progress").style.width = `${scan.running ? Math.max(2, progress) : 0}%`;
    const progressText = scan.running
      ? (scan.total > 0
        ? `${profileLabel} · ${Math.min(scan.index + 1, scan.total)}/${scan.total} · 第 ${(scan.round || 0) + 1} 轮`
        : `${profileLabel} · DEMO`)
      : "就绪";
    $("#scan-progress-text").textContent = progressText;
    $("#scan-note-text").textContent = scan.running
      ? (scan.total > 0
        ? `正在轮询 ${scan.total} 个频点/参数组合；已收 ${scan.packets || 0} 帧，载波命中 ${scan.signals || 0}。`
        : `DEMO 数据流运行中；已收 ${scan.packets || 0} 帧，载波 ${scan.signals || 0}。`)
      : "LoRa 接收机一次只能解调一组频点/SF，扫描会按计划轮询。";
    $("#btn-start").disabled = !!scan.running;
    $("#btn-scan-start").disabled = !!scan.running;
    $("#btn-stop").disabled = !scan.running;
    $("#btn-scan-stop").disabled = !scan.running;
    $("#btn-monitor").disabled = !state.selected;
    if (data.logs && data.logs.length) {
      const last = data.logs[data.logs.length - 1];
      $("#status-last").textContent = last.message || "";
    }
    const foot = scan.running
      ? `扫描中 · ${scan.packets || 0} 帧 · ${scan.signals || 0} 载波 · 第 ${(scan.round || 0) + 1} 轮`
      : `共 ${deviceCount} 个对象 · ${stats.packets || 0} 个数据包`;
    $("#device-foot").textContent = foot;
  }

  async function fetchState() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      if (!res.ok) return;
      handleState(await res.json());
    } catch (_) { /* keep last state */ }
  }

  // ---------------------------------------------------------------------
  // device list
  // ---------------------------------------------------------------------
  function renderDevices() {
    const data = state.data;
    if (!data) return;
    let devices = (data.devices || []).slice();
    if (!state.showSignals) devices = devices.filter((d) => d.kind !== "signal");
    const filter = state.filter.trim().toLowerCase();
    if (filter) {
      devices = devices.filter((d) =>
        `${d.name} ${d.model} ${d.summary || ""}`.toLowerCase().includes(filter)
      );
    }
    const sort = state.sort;
    devices.sort((a, b) => {
      if (sort === "rssi") return (b.rssi_dbm ?? -999) - (a.rssi_dbm ?? -999);
      if (sort === "name") return String(a.name).localeCompare(String(b.name), "zh-CN");
      if (sort === "packets") return (b.packet_count || 0) - (a.packet_count || 0);
      return (b.last_seen || 0) - (a.last_seen || 0);
    });
    const wrap = $("#device-list-wrap");
    const scroll = wrap ? wrap.scrollTop : 0;
    const rows = devices.map((device) => {
      const selected = device.id === state.selected;
      const rssi = device.rssi_dbm;
      const sub = `${device.model || "未知型号"} · ${short(device.summary || "", 46)}`;
      return `<tr class="device-row${selected ? " selected" : ""}${device.kind === "signal" ? " signal-row" : ""}" data-id="${esc(device.id)}">
        <td class="c-ico">${icon(device.kind)}</td>
        <td class="c-name"><div class="dn">${esc(device.name)}</div><div class="dm">${esc(sub)}</div></td>
        <td class="c-rssi"><span class="rssi-num">${rssi === null || rssi === undefined ? "—" : Math.round(rssi)}</span>${rssiBars(rssi)}</td>
      </tr>`;
    });
    $("#device-list").innerHTML = rows.join("");
    $("#list-empty").classList.toggle("show", rows.length === 0);
    if (wrap) wrap.scrollTop = scroll;
  }

  // ---------------------------------------------------------------------
  // detail head / body
  // ---------------------------------------------------------------------
  function renderHead(device) {
    const head = $("#detail-head");
    if (!device) {
      head.innerHTML = `<div class="detail-empty-head">
        <svg class="big-ico"><use href="#i-radio"/></svg>
        <div><div class="detail-title">未选择设备</div>
        <div class="detail-sub">在左侧列表中选择一个对象，查看它的身份、协议与广播内容。</div></div></div>`;
      return;
    }
    const kind = device.kind || "raw";
    const profile = device.last_profile || device.profile || {};
    const encrypted = device.encrypted ? `<span class="badge raw">加密</span>` : "";
    const candidate = device.candidate ? `<span class="badge signal">候选</span>` : "";
    const demo = state.data?.demo ? `<span class="badge demo">DEMO</span>` : "";
    const stats = [];
    if (device.rssi_dbm !== null && device.rssi_dbm !== undefined) stats.push(`RSSI <b>${Math.round(device.rssi_dbm)} dBm</b>`);
    if (device.snr_db !== null && device.snr_db !== undefined) stats.push(`SNR <b>${fmtNum(device.snr_db, 1)} dB</b>`);
    if (profile.frequency_mhz) stats.push(`${fmtFreq(profile.frequency_mhz)} MHz <b>SF${profile.sf}/BW${Number(profile.bw_khz)}</b>`);
    if (device.battery !== null && device.battery !== undefined) stats.push(`电池 <b>${device.battery}%</b>`);
    stats.push(`包 <b>${device.packet_count || 0}</b>`);
    stats.push(`${esc(fmtAgo(device.last_seen))}`);
    head.innerHTML = `<div class="detail-head-inner">
      <div class="detail-top">
        <svg class="detail-head-icon"><use href="#${ICONS[kind] || "i-raw"}"/></svg>
        <div style="min-width:0;flex:1">
          <div class="detail-name">${esc(device.name)}</div>
          <div class="detail-model"><span class="badge ${KIND_BADGE[kind] || "raw"}">${KIND_LABEL[kind] || kind}</span>${encrypted}${candidate}${demo}${esc(device.model || "未知型号")}</div>
          <div class="detail-stats">${stats.join(" · ")}</div>
        </div>
        <div class="detail-actions">
          <button class="btn" id="act-monitor" title="锁定到此设备的频点">锁定</button>
          <button class="btn" id="act-copy" title="复制设备 JSON">复制</button>
          <button class="btn" id="act-delete" title="从列表移除">移除</button>
        </div>
      </div>
    </div>`;
    $("#act-monitor").addEventListener("click", monitorSelected);
    $("#act-copy").addEventListener("click", () => copyText(pretty(device), "设备 JSON 已复制"));
    $("#act-delete").addEventListener("click", async () => {
      const ok = await confirmDialog("移除设备", `从列表中移除 <b>${esc(device.name)}</b>？<br>历史数据包也会删除。`, "移除");
      if (!ok) return;
      await fetch(`/api/device/${encodeURIComponent(device.id)}`, { method: "DELETE" });
      state.selected = null;
      state.detail = null;
      state.detailKey = null;
      renderHead(null);
      renderDetailBody();
      fetchState();
    });
    $("#btn-monitor").disabled = false;
  }

  async function refreshDetail(force = false) {
    if (!state.selected) return;
    const summary = (state.data?.devices || []).find((d) => d.id === state.selected);
    if (!summary) return;
    const key = `${summary.packet_count}:${summary.last_seen}`;
    if (!force && state.detailKey === key) return;
    if (state.fetchingDetail) return;
    const now = Date.now();
    if (!force && now - state.lastDetailAt < 500) return;
    state.fetchingDetail = true;
    state.lastDetailAt = now;
    try {
      const res = await fetch(`/api/device/${encodeURIComponent(state.selected)}`, { cache: "no-store" });
      if (res.ok) {
        const data = await res.json();
        if (state.selected === data.device.id) {
          state.detail = data.device;
          state.detailKey = `${data.device.packet_count}:${data.device.last_seen}`;
          renderHead(data.device);
          renderDetailBody();
        }
      }
    } catch (_) { /* ignore */ }
    state.fetchingDetail = false;
  }

  function selectDevice(id) {
    if (state.selected === id) return;
    state.selected = id;
    state.detail = null;
    state.detailKey = null;
    state.tab = "overview";
    $$("#tabs .tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === "overview"));
    renderDevices();
    renderHead(null);
    renderDetailBody();
    refreshDetail(true);
  }

  function setTab(tab) {
    state.tab = tab;
    $$("#tabs .tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
    renderDetailBody();
  }

  function renderDetailBody() {
    const body = $("#detail-body");
    if (state.tab === "band") {
      body.innerHTML = renderBand();
      loadBandProfiles();
      return;
    }
    if (!state.selected) {
      body.innerHTML = `<div class="detail-empty">
        <svg class="huge-ico"><use href="#i-lora"/></svg>
        <div class="de-title">LoRa95</div>
        <div class="de-text">Windows 95 风格的 LoRa 被动扫描器</div>
        <div class="de-hint">支持 Meshtastic · LoRaWAN · 私有 LoRa 帧</div>
        <div class="de-keys"><span><b>F5</b> 开始</span><span><b>Esc</b> 停止</span><span><b>M</b> 锁定频点</span></div>
      </div>`;
      return;
    }
    if (!state.detail) {
      body.innerHTML = `<div class="empty-note">正在读取设备信息…</div>`;
      return;
    }
    if (state.tab === "overview") body.innerHTML = renderOverview(state.detail);
    else if (state.tab === "messages") body.innerHTML = renderMessages(state.detail);
    else if (state.tab === "broadcast") body.innerHTML = renderBroadcast(state.detail);
    else if (state.tab === "packets") body.innerHTML = renderPackets(state.detail);
    bindDetailActions();
  }

  function bindDetailActions() {
    $$("#detail-body [data-copy]").forEach((button) => {
      button.addEventListener("click", () => copyText(button.dataset.copy, "已复制"));
    });
    $$("#detail-body .packet-row").forEach((row) => {
      row.addEventListener("click", () => {
        const index = Number(row.dataset.index);
        const packet = state.detail?.packets?.[index];
        if (packet) showPacketModal(packet);
      });
    });
  }

  function kvSection(title, rows) {
    const filtered = rows.filter((row) => row[1] !== undefined && row[1] !== null && row[1] !== "");
    if (!filtered.length) return "";
    const cells = filtered.map(([k, v, mono, wide]) =>
      `<div class="kv${wide ? " wide" : ""}"><span class="k">${esc(k)}</span><span class="v${mono ? " mono" : ""}">${v}</span></div>`
    ).join("");
    return `<div class="section"><div class="section-title">${esc(title)}</div><div class="kv-grid">${cells}</div></div>`;
  }

  function renderOverview(device) {
    const profile = device.last_profile || device.profile || {};
    const kind = device.kind;
    const identity = [
      ["名称", esc(device.name)],
      ["类型", `<span class="badge ${KIND_BADGE[kind] || "raw"}">${KIND_LABEL[kind] || kind}</span>`],
      ["型号", esc(device.model || "未知")],
      ["节点 ID", device.node_id ? `<span class="mono">${esc(device.node_id)}</span>` : ""],
      ["短名", esc(device.short_name || "")],
      ["设备角色", esc(device.role || "")],
      ["硬件", esc(device.hw_model || "")],
      ["MAC", device.macaddr ? `<span class="mono">${esc(device.macaddr)}</span>` : ""],
      ["DevAddr", device.dev_addr ? `<span class="mono">${esc(device.dev_addr)}</span>` : ""],
      ["DevEUI", device.dev_eui ? `<span class="mono">${esc(device.dev_eui)}</span>` : ""],
      ["JoinEUI", device.join_eui ? `<span class="mono">${esc(device.join_eui)}</span>` : ""],
      ["公钥", device.public_key ? `<span class="mono">${esc(device.public_key)}</span>` : "", true, true],
    ];
    const radio = [
      ["频点", profile.frequency_mhz ? `${fmtFreq(profile.frequency_mhz)} MHz` : "—"],
      ["调制", profile.sf ? `SF${profile.sf} · BW${Number(profile.bw_khz)} kHz · CR 4/${profile.cr || 5}` : "—"],
      ["同步字", profile.sync_word !== undefined ? `0x${Number(profile.sync_word).toString(16).toUpperCase()}` : "—"],
      ["RSSI", device.rssi_dbm !== null && device.rssi_dbm !== undefined ? `${Math.round(device.rssi_dbm)} dBm` : "—"],
      ["SNR", device.snr_db !== null && device.snr_db !== undefined ? `${fmtNum(device.snr_db, 1)} dB` : "—"],
      ["最佳 / 最差", device.best_rssi !== null && device.best_rssi !== undefined ? `${Math.round(device.best_rssi)} / ${Math.round(device.worst_rssi ?? device.best_rssi)} dBm` : ""],
      ["首次发现", fmtDate(device.first_seen)],
      ["最后收到", `${fmtDate(device.last_seen)} (${fmtAgo(device.last_seen)})`],
      ["数据包", `${device.packet_count || 0} (解出 ${device.decoded_count || 0})`],
      ["加密帧", device.encrypted_count ? String(device.encrypted_count) : ""],
      ["配置", device.last_profile?.label ? esc(device.last_profile.label) : "", false, true],
    ];
    const telemetry = [];
    if (device.battery !== null && device.battery !== undefined) telemetry.push(["电池", `${device.battery}%`]);
    if (device.voltage !== null && device.voltage !== undefined) telemetry.push(["电压", `${fmtNum(device.voltage, 2)} V`]);
    if (device.uptime) telemetry.push(["运行时间", fmtDuration(device.uptime)]);
    const tm = device.telemetry || {};
    const env = tm.environment_metrics || {};
    Object.entries(env).forEach(([key, value]) => {
      if (value && typeof value === "object") {
        const n = Number(value.value);
        telemetry.push([envLabel(key), `${Number.isFinite(n) ? String(Math.round(n * 100) / 100) : value.value}${value.unit || ""}`]);
      }
    });
    const dm = tm.device_metrics || {};
    if (dm.channel_utilization) telemetry.push(["信道占用", `${fmtNum(dm.channel_utilization, 1)}%`]);
    if (dm.air_util_tx) telemetry.push(["发射占用", `${fmtNum(dm.air_util_tx, 1)}%`]);
    let position = [];
    if (device.latitude !== null && device.latitude !== undefined) {
      position = [
        ["纬度", `<span class="mono">${Number(device.latitude).toFixed(6)}</span>`],
        ["经度", `<span class="mono">${Number(device.longitude).toFixed(6)}</span>`],
        ["海拔", device.altitude !== null && device.altitude !== undefined ? `${device.altitude} m` : ""],
        ["定位时间", device.position_time ? fmtDate(device.position_time) : ""],
        ["坐标", `<button class="btn" data-copy="${esc(`${device.latitude}, ${device.longitude}`)}" style="height:19px;font-size:9px;padding:0 5px">复制坐标</button>`],
      ];
    }
    let protocolRows = [];
    if (kind === "meshtastic") {
      const latest = device.latest || {};
      protocolRows = [
        ["信道", latest.channel !== undefined ? String(latest.channel) : ""],
        ["最后端口", esc(latest.port_name || latest.portnum || "")],
        ["消息数", String((device.messages || []).length)],
        ["中继邻居", device.neighbors ? String(device.neighbors.length) : ""],
      ];
    } else if (kind === "lorawan") {
      protocolRows = [
        ["FCnt", device.fcnt !== null && device.fcnt !== undefined ? String(device.fcnt) : ""],
        ["FPort", device.fport !== null && device.fport !== undefined ? String(device.fport) : ""],
        ["ADR / ACK", device.latest?.adr !== undefined ? `${device.latest.adr ? "ADR" : "—"} / ${device.latest.ack ? "ACK" : "—"}` : ""],
        ["CayenneLPP", device.cayenne?.length ? `${device.cayenne.length} 个通道` : ""],
      ];
    } else if (kind === "raw") {
      protocolRows = [
        ["指纹", device.raw_kind ? esc(device.raw_kind) : ""],
        ["长度", device.latest?.length ? `${device.latest.length} B` : ""],
        ["文本", device.text ? esc(short(device.text, 100)) : ""],
      ];
    } else {
      protocolRows = [
        ["载波", device.last_profile?.label ? esc(device.last_profile.label) : ""],
        ["RSSI", device.rssi_dbm !== null ? `${Math.round(device.rssi_dbm)} dBm` : ""],
        ["说明", "检测到 LoRa 载波，但当前参数下无法解调帧。"],
      ];
    }
    const latest = device.latest || {};
    const broadcast = [
      ["最后广播", esc(short(device.summary || "", 160))],
      ["最后时间", fmtTime(device.last_seen)],
      ["端口", esc(latest.port_name || "")],
      ["载荷", latest.length ? `${latest.length} B` : ""],
    ];
    return kvSection("身份 / Identity", identity)
      + kvSection("无线参数 / Radio", radio)
      + kvSection("遥测 / Telemetry", telemetry)
      + kvSection("位置 / Position", position)
      + kvSection("协议 / Protocol", protocolRows)
      + kvSection("最新广播 / Last broadcast", broadcast);
  }

  function envLabel(key) {
    const labels = {
      temperature: "温度", relative_humidity: "湿度", barometric_pressure: "气压",
      gas_resistance: "气体电阻", voltage: "电压", current: "电流", iaq: "空气质量",
      distance: "距离", lux: "光照", white_lux: "白光", ir_lux: "红外", uv_lux: "紫外",
      wind_direction: "风向", wind_speed: "风速", weight: "重量", wind_gust: "阵风",
      wind_lull: "微风", radiation: "辐射", rainfall_1h: "1h 雨量", rainfall_24h: "24h 雨量",
      soil_moisture: "土壤湿度", soil_temperature: "土壤温度",
    };
    return labels[key] || key;
  }

  function renderMessages(device) {
    const messages = (device.messages || []).slice(-100);
    if (!messages.length) {
      let note = "尚未收到可解码的文本消息。";
      if (device.kind !== "meshtastic") note = "该设备不是 Meshtastic 文本节点；请查看“广播”页的原始载荷。";
      return `<div class="section"><div class="section-title">通信 / Messages</div><div class="msg-list"><div class="msg-empty">${note}</div></div></div>`;
    }
    const rows = messages.map((msg) => `
      <div class="msg">
        <div class="msg-head"><span class="from">${esc(msg.from || "")}</span><span>${fmtTime(msg.ts)}</span><span>${msg.rssi_dbm !== null && msg.rssi_dbm !== undefined ? Math.round(msg.rssi_dbm) + " dBm" : ""}</span><span>${msg.snr_db !== null && msg.snr_db !== undefined ? fmtNum(msg.snr_db, 1) + " dB" : ""}</span></div>
        <div class="msg-text">${esc(msg.text)}</div>
        <div class="msg-meta">${esc(msg.to || "")} · packet ${esc(msg.packet_id ?? "")} · channel ${esc(msg.channel ?? "")}</div>
      </div>`).join("");
    return `<div class="section"><div class="section-title">通信 / Messages (${messages.length})</div><div class="msg-list">${rows}</div></div>
      <div class="scan-note" style="margin-top:4px"><svg><use href="#i-info"/></svg><span>LoRa95 为被动接收；发送消息请使用 Meshtastic 客户端。锁定频点后可持续接收此信道的聊天。</span></div>`;
  }

  function renderBroadcast(device) {
    const latest = device.latest || (device.packets || [])[(device.packets || []).length - 1] || {};
    const kind = device.kind;
    let html = `<div class="section"><div class="section-title">最新广播 / Broadcast</div>`;
    if (kind === "meshtastic") {
      const rows = [
        ["端口", esc(latest.port_name || latest.portnum || "—")],
        ["摘要", esc(short(latest.summary || device.summary || "", 220))],
        ["节点", esc(device.node_id || "")],
        ["RSSI / SNR", `${Math.round(latest.rssi_dbm ?? device.rssi_dbm ?? 0)} dBm / ${fmtNum(latest.snr_db ?? device.snr_db ?? 0, 1)} dB`],
      ];
      html += kvSection("解析结果", rows);
      if (latest.nodeinfo) html += kvSection("节点信息 (NODEINFO)", userRows(latest.nodeinfo));
      if (latest.position) html += kvSection("位置 (POSITION)", positionRows(latest.position));
      if (latest.telemetry) html += kvSection("遥测 (TELEMETRY)", telemetryRows(latest.telemetry));
      if (latest.text) html += `<div class="section"><div class="section-title">文本</div><div class="payload-actions"><button class="btn" data-copy="${esc(latest.text)}">复制文本</button></div><div class="hexdump" style="font-family:var(--font);font-size:11px;line-height:16px;white-space:pre-wrap">${esc(latest.text)}</div></div>`;
    } else if (kind === "lorawan") {
      const rows = [
        ["MType", esc(latest.mtype || "—")],
        ["DevAddr", latest.dev_addr ? `<span class="mono">${esc(latest.dev_addr)}</span>` : ""],
        ["DevEUI", latest.dev_eui ? `<span class="mono">${esc(latest.dev_eui)}</span>` : ""],
        ["FCnt", latest.fcnt !== undefined ? String(latest.fcnt) : ""],
        ["FPort", latest.fport !== undefined ? String(latest.fport) : ""],
        ["摘要", esc(short(latest.summary || "", 200))],
      ];
      html += kvSection("LoRaWAN MAC", rows);
      const lpp = latest.cayenne || device.cayenne || [];
      if (lpp.length) {
        const rows2 = lpp.map((entry) => [`CH${entry.channel} ${entry.type_name}`, `${entry.value}${entry.unit || ""}`]);
        html += kvSection("CayenneLPP", rows2);
      }
      if (latest.text) html += `<div class="section"><div class="section-title">载荷文本</div><div class="hexdump" style="font-family:var(--font);font-size:11px;white-space:pre-wrap">${esc(latest.text)}</div></div>`;
    } else if (kind === "raw") {
      html += `<div class="section"><div class="section-title">私有载荷</div>`;
      if (latest.text || device.text) {
        html += `<div class="payload-actions"><button class="btn" data-copy="${esc(latest.text || device.text)}">复制文本</button></div>
          <div class="hexdump" style="font-family:var(--font);font-size:11px;white-space:pre-wrap">${esc(latest.text || device.text)}</div>`;
      }
      if (device.json) {
        html += `<div class="payload-actions"><button class="btn" data-copy="${esc(pretty(device.json))}">复制 JSON</button></div><div class="hexdump" style="font-size:9px">${esc(pretty(device.json))}</div>`;
      }
      html += `<div class="payload-actions" style="margin-top:4px"><button class="btn" data-copy="${esc(latest.hex || "")}">复制 HEX</button></div>${hexdump(latest.hex || "")}</div>`;
    } else {
      html += `<div class="empty-note">这是一个仅有载波活动的对象：当前频点/SF/同步字下检测到能量，但没有可解调的帧。<br><br>${esc(device.last_profile?.label || "")}</div>`;
    }
    if (latest.hex) {
      html += `<div class="section"><div class="section-title">原始帧 / Raw frame (${(latest.hex.length / 2) | 0} B)</div>${hexdump(latest.hex)}</div>`;
    }
    return html;
  }

  function userRows(info) {
    return [
      ["Node ID", esc(info.id || "")],
      ["长名", esc(info.long_name || "")],
      ["短名", esc(info.short_name || "")],
      ["硬件", esc(info.hw_model_name || "")],
      ["角色", esc(info.role_name || "")],
      ["MAC", esc(info.macaddr || "")],
      ["公钥", esc(info.public_key || ""), true, true],
      ["HAM", info.is_licensed ? "是" : ""],
    ];
  }

  function positionRows(pos) {
    return [
      ["纬度", Number(pos.latitude).toFixed(6)],
      ["经度", Number(pos.longitude).toFixed(6)],
      ["海拔", pos.altitude !== undefined ? `${pos.altitude} m` : ""],
      ["时间", pos.time ? fmtDate(pos.time) : pos.timestamp ? fmtDate(pos.timestamp) : ""],
      ["精度", pos.gps_accuracy ? `${pos.gps_accuracy} m` : ""],
    ];
  }

  function telemetryRows(telemetry) {
    const rows = [];
    const dm = telemetry.device_metrics || {};
    if (dm.battery_level !== undefined) rows.push(["电池", `${dm.battery_level}%`]);
    if (dm.voltage) rows.push(["电压", `${fmtNum(dm.voltage, 2)} V`]);
    if (dm.uptime_seconds) rows.push(["运行", fmtDuration(dm.uptime_seconds)]);
    const env = telemetry.environment_metrics || {};
    Object.entries(env).forEach(([key, value]) => {
      if (value && typeof value === "object") {
        const n = Number(value.value);
        rows.push([envLabel(key), `${Number.isFinite(n) ? String(Math.round(n * 100) / 100) : value.value}${value.unit || ""}`]);
      }
    });
    return rows;
  }

  function renderPackets(device) {
    const packets = (device.packets || []).slice().reverse();
    if (!packets.length) return `<div class="empty-note">还没有数据包。</div>`;
    const rows = packets.slice(0, 300).map((packet, index) => {
      const originalIndex = (device.packets.length - 1) - index;
      return `<tr class="packet-row" data-index="${originalIndex}">
        <td class="p-time">${fmtTime(packet.ts)}</td>
        <td class="p-rssi">${packet.rssi_dbm !== null && packet.rssi_dbm !== undefined ? Math.round(packet.rssi_dbm) : "—"}</td>
        <td class="p-snr">${packet.snr_db !== null && packet.snr_db !== undefined ? fmtNum(packet.snr_db, 1) : "—"}</td>
        <td class="p-len">${packet.length ?? "—"}</td>
        <td class="p-sum">${esc(short(packet.summary || packet.port_name || packet.kind || "", 70))}</td>
      </tr>`;
    }).join("");
    return `<div class="section"><div class="section-title">数据包 / Packets (${packets.length})</div>
      <table class="packet-table"><thead><tr><th class="p-time">时间</th><th class="p-rssi">RSSI</th><th class="p-snr">SNR</th><th class="p-len">长</th><th>摘要</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="band-hint">点击任意行查看完整载荷与原始帧。</div></div>`;
  }

  function showPacketModal(packet) {
    const body = `<div class="section"><div class="section-title">数据包详情</div>
      ${kvSection("元数据", [
        ["时间", `${fmtDate(packet.ts)} (${fmtAgo(packet.ts)})`],
        ["类型", esc(packet.kind || "")],
        ["摘要", esc(packet.summary || "")],
        ["RSSI", packet.rssi_dbm !== null && packet.rssi_dbm !== undefined ? `${packet.rssi_dbm} dBm` : ""],
        ["SNR", packet.snr_db !== null && packet.snr_db !== undefined ? `${packet.snr_db} dB` : ""],
        ["频点", packet.frequency_mhz ? `${fmtFreq(packet.frequency_mhz)} MHz · SF${packet.sf}/BW${Number(packet.bw_khz)}` : ""],
        ["长度", packet.length !== undefined ? `${packet.length} B` : ""],
      ])}</div>
      <div class="section"><div class="section-title">原始帧</div>${hexdump(packet.hex || "")}</div>
      <div class="section"><div class="section-title">JSON</div><div class="hexdump" style="font-size:9px">${esc(pretty(packet))}</div></div>`;
    openModal("数据包", body, [{ label: "关闭", primary: true, value: true }], "i-raw");
  }

  function renderBand() {
    const data = state.data || {};
    const scan = data.scan || {};
    const progress = scan.total > 0 ? Math.round((scan.index / scan.total) * 100) : 0;
    const current = scan.profile;
    const hits = { ...(scan.hits || {}) };
    if (data.demo) {
      (data.devices || []).forEach((device) => {
        const p = device.profile || device.last_profile;
        if (p && p.id) hits[p.id] = (hits[p.id] || 0) + (device.packet_count || 0);
      });
    }
    const region = data.settings?.region || scan.region || "US";
    const mode = data.settings?.mode || scan.mode || "quick";
    const key = `${region}:${mode}`;
    const items = state.profiles[key] || [];
    let list = "";
    if (!items.length) {
      list = `<div class="empty-note">正在读取扫描计划…（${esc(region)} / ${esc(mode)}）</div>`;
    } else {
      const sorted = items.slice().sort((a, b) => {
        const ha = hits[a.id] || 0;
        const hb = hits[b.id] || 0;
        if (ha !== hb) return hb - ha;
        return (a.frequency_mhz || 0) - (b.frequency_mhz || 0);
      }).slice(0, 300);
      const total = scan.total || items.length;
      const maxHits = Math.max(1, ...sorted.map((p) => hits[p.id] || 0));
      list = sorted.map((p) => {
        const h = hits[p.id] || 0;
        const active = current && (p.id === current.id
          || (data.demo && p.frequency_mhz === current.frequency_mhz && p.protocol === "meshtastic"));
        const width = Math.round((h / maxHits) * 100);
        return `<div class="band-row${active ? " active" : ""}">
          <span class="b-label">${esc(p.label || p.id)}</span>
          <span class="b-hits">${h || ""}</span>
          <span class="b-bar"><span class="b-fill" style="width:${h ? Math.max(4, width) : 0}%"></span></span>
        </div>`;
      }).join("");
    }
    return `<div class="section">
      <div class="section-title">频段计划 / Band plan</div>
      <div style="padding:4px;background:var(--field);box-shadow:inset 1px 1px var(--shadow)">
        <div class="band-head"><span class="band-title">${esc(region)} · ${esc(mode)}</span><span class="band-sub">${scan.running ? "扫描中" : "空闲"} · ${scan.total || items.length} 个组合</span></div>
        <div class="progress-track"><div class="progress-fill" style="width:${scan.running ? Math.max(2, progress) : 0}%"></div></div>
        <div class="progress-text">${current ? esc(current.label || "") : "—"}</div>
        <div class="band-sub" style="margin-top:3px">已收 ${scan.packets || 0} 帧 · 载波命中 ${scan.signals || 0} · 第 ${(scan.round || 0) + 1} 轮</div>
      </div>
      <div class="band-list" style="margin-top:4px">${list}</div>
      <div class="band-hint">“命中”表示该频点/参数组合收到了可解调帧；空白的组合仍在轮询中。</div>
    </div>`;
  }

  async function loadBandProfiles() {
    const data = state.data || {};
    const region = data.settings?.region || data.scan?.region || "US";
    const mode = data.settings?.mode || data.scan?.mode || "quick";
    const key = `${region}:${mode}`;
    if (state.profiles[key] && state.profiles[key].length) return;
    try {
      const res = await fetch(`/api/profiles?region=${encodeURIComponent(region)}&mode=${encodeURIComponent(mode)}`);
      const json = await res.json();
      if (json.ok) {
        state.profiles[key] = json.items || [];
        if (state.tab === "band") renderDetailBody();
      }
    } catch (_) { /* ignore */ }
  }

  // ---------------------------------------------------------------------
  // events / menus
  // ---------------------------------------------------------------------
  function setupMenus() {
    $$(".menu-item").forEach((item) => {
      item.addEventListener("click", (event) => {
        event.stopPropagation();
        const name = item.dataset.menu;
        const popup = $(`#menu-${name}`);
        const open = popup.classList.contains("open");
        closeMenus();
        if (!open) {
          const rect = item.getBoundingClientRect();
          popup.style.left = `${rect.left}px`;
          popup.style.top = `${rect.bottom + 1}px`;
          popup.classList.add("open");
          item.classList.add("open");
        }
      });
    });
    $$(".menu-popup .menu-row").forEach((row) => {
      row.addEventListener("click", () => {
        const action = row.dataset.action;
        closeMenus();
        handleAction(action);
      });
    });
    document.addEventListener("click", closeMenus);
    document.addEventListener("contextmenu", (event) => event.preventDefault());
  }

  function closeMenus() {
    $$(".menu-popup").forEach((popup) => popup.classList.remove("open"));
    $$(".menu-item").forEach((item) => item.classList.remove("open"));
  }

  function handleAction(action) {
    if (!action) return;
    if (action === "export-json") window.location.href = "/api/export.json";
    else if (action === "export-csv") window.location.href = "/api/export.csv";
    else if (action === "start") startScan();
    else if (action === "stop") stopScan();
    else if (action === "clear") clearDevices();
    else if (action === "sort-last") { state.sort = "last"; $("#device-sort").value = "last"; renderDevices(); scheduleSave(); }
    else if (action === "sort-rssi") { state.sort = "rssi"; $("#device-sort").value = "rssi"; renderDevices(); scheduleSave(); }
    else if (action === "sort-name") { state.sort = "name"; $("#device-sort").value = "name"; renderDevices(); scheduleSave(); }
    else if (action === "toggle-signals") { state.showSignals = !state.showSignals; renderDevices(); flash(state.showSignals ? "显示载波对象" : "隐藏载波对象"); }
    else if (action === "monitor-selected") monitorSelected();
    else if (action === "restart-radio") restartRadio();
    else if (action === "about") showAbout();
    else if (action === "limits") showLimits();
    else if (action === "exit") exitApp();
  }

  async function clearDevices() {
    const ok = await confirmDialog("清空设备列表", "将删除所有已发现的设备、消息和数据包。此操作不可撤销。", "清空");
    if (!ok) return;
    await fetch("/api/clear", { method: "POST" });
    state.selected = null;
    state.detail = null;
    state.detailKey = null;
    renderHead(null);
    renderDetailBody();
    fetchState();
  }

  async function restartRadio() {
    const ok = await confirmDialog("重启无线服务", "将短暂停止扫描并重启 LoRa95 无线守护进程；meshtasticd 会在停止后恢复。继续？", "重启");
    if (!ok) return;
    flash("正在重启无线服务…");
    try {
      await fetch("/api/radio/stop", { method: "POST" });
      await new Promise((r) => setTimeout(r, 900));
      const res = await fetch("/api/radio/start", { method: "POST" });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || "启动失败");
      flash("无线服务已重启");
      setTimeout(fetchState, 600);
    } catch (err) {
      showError(String(err.message || err));
    }
  }

  function exitApp() {
    confirmDialog("退出 LoRa95", "关闭窗口并恢复到桌面？<br><br>无线服务会停止，meshtasticd 会自动恢复。", "退出").then((ok) => {
      if (!ok) return;
      fetch("/api/radio/stop", { method: "POST" }).catch(() => {});
      window.close();
      setTimeout(() => {
        document.body.innerHTML = '<div style="padding:20px;font-family:sans-serif">LoRa95 已退出，可以关闭此窗口。</div>';
      }, 300);
    });
  }

  function setupControls() {
    $("#btn-start").addEventListener("click", () => startScan());
    $("#btn-scan-start").addEventListener("click", () => startScan());
    $("#btn-stop").addEventListener("click", stopScan);
    $("#btn-scan-stop").addEventListener("click", stopScan);
    $("#btn-clear").addEventListener("click", clearDevices);
    $("#btn-scan-clear").addEventListener("click", clearDevices);
    $("#btn-monitor").addEventListener("click", monitorSelected);
    $("#btn-export").addEventListener("click", () => { window.location.href = "/api/export.json"; });
    $("#win-close").addEventListener("click", exitApp);
    $("#win-min").addEventListener("click", () => flash("Kiosk 模式：窗口已最大化"));
    $("#win-max").addEventListener("click", () => flash("Kiosk 模式：窗口已最大化"));
    $("#modal-close").addEventListener("click", () => closeModal(false));
    $("#modal-overlay").addEventListener("click", (event) => {
      if (event.target === $("#modal-overlay")) closeModal(false);
    });

    $("#tabs").addEventListener("click", (event) => {
      const tab = event.target.closest(".tab");
      if (tab) setTab(tab.dataset.tab);
    });
    $("#device-list").addEventListener("click", (event) => {
      const row = event.target.closest(".device-row");
      if (row) selectDevice(row.dataset.id);
    });
    $("#device-filter").addEventListener("input", (event) => {
      state.filter = event.target.value;
      renderDevices();
      scheduleSave();
    });
    $("#device-sort").addEventListener("change", (event) => {
      state.sort = event.target.value;
      renderDevices();
      scheduleSave();
    });
    $$("#set-region, #set-mode, #set-dwell, #set-rssi, #set-freq, #set-sf, #set-bw, #set-cr, #set-sync, #set-preamble, #set-cad, #set-raw").forEach((el) => {
      el.addEventListener("change", () => {
        updateCustomVisibility();
        state.planKey = null;
        updatePlanCount();
        scheduleSave();
      });
      el.addEventListener("input", () => {
        if (el.type === "number" || el.type === "text") scheduleSave();
      });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "F5") { event.preventDefault(); startScan(); }
      else if (event.key === "Escape") {
        if ($("#modal-overlay").classList.contains("show")) closeModal(false);
        else if ($$(".menu-popup.open").length) closeMenus();
        else stopScan();
      } else if (event.key.toLowerCase() === "m" && !isTyping(event)) monitorSelected();
      else if (event.key === "1" && !isTyping(event)) { state.sort = "last"; $("#device-sort").value = "last"; renderDevices(); }
      else if (event.key === "2" && !isTyping(event)) { state.sort = "rssi"; $("#device-sort").value = "rssi"; renderDevices(); }
      else if (event.key === "3" && !isTyping(event)) { state.sort = "name"; $("#device-sort").value = "name"; renderDevices(); }
      else if (event.key === "4" && !isTyping(event)) { state.showSignals = !state.showSignals; renderDevices(); }
    });
  }

  function isTyping(event) {
    const tag = (event.target?.tagName || "").toLowerCase();
    return tag === "input" || tag === "select" || tag === "textarea";
  }

  // ---------------------------------------------------------------------
  // connectivity
  // ---------------------------------------------------------------------
  function connectEvents() {
    if (state.eventSource) {
      try { state.eventSource.close(); } catch (_) {}
    }
    try {
      const source = new EventSource("/api/events");
      state.eventSource = source;
      source.onmessage = (event) => {
        try { handleState(JSON.parse(event.data)); } catch (_) { /* ignore malformed */ }
      };
      source.onerror = () => {
        try { source.close(); } catch (_) {}
        state.eventSource = null;
        setTimeout(connectEvents, 2200);
      };
    } catch (_) {
      setTimeout(connectEvents, 2200);
    }
  }

  function startClock() {
    const tick = () => {
      const now = new Date();
      const text = now.toLocaleTimeString("zh-CN", { hour12: false });
      const clock = $("#menu-clock");
      const status = $("#status-clock");
      if (clock) clock.textContent = text;
      if (status) status.textContent = text;
    };
    tick();
    state.clockTimer = setInterval(tick, 1000);
  }

  async function init() {
    setupMenus();
    setupControls();
    startClock();
    await loadRegions();
    await fetchState();
    connectEvents();
    setInterval(() => {
      if (Date.now() - state.lastStateAt > 4500) fetchState();
    }, 3000);
    // no-op placeholder removed
  }

  document.addEventListener("DOMContentLoaded", init);
})();
