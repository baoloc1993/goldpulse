/* GoldPulse (v3) — S8 Tài khoản giấy · S9 Nhật ký lệnh. TIỀN GIẢ. JS thuần, không thư viện ngoài.
   Số tự tải lại 5 giây như S1; đường số dư là SVG vẽ tay, 4 đường phân biệt bằng nét (DESIGN-SYSTEM C15).
   Chữ viết cho trader, không cho kỹ sư (dogfood newbie 2026-09-27): luật bằng lời thường, nói rõ khi nào có lệnh. */
(function () {
  "use strict";
  var screen = document.body.dataset.screen;
  var content = document.getElementById("content");
  var TIMEOUT_MS = 15000;
  var DASH = { A: "", B: "6 3", C: "2 3", D: "6 3 2 3" };
  var SKIP = {
    rule: "không khớp luật của tài khoản", open_trade: "đang có lệnh mở", margin: "số dư không đủ ký quỹ (sau lỗ trong ngày)",
    burned: "đã cháy hôm nay", disabled: "tài khoản đang tắt", market_closing: "sắp tới giờ nghỉ của thị trường",
    stale: "dự đoán đã cũ hơn 5 phút (máy vắng)"
  };
  var EXIT = { horizon: ["ok", "Hết hạn"], stopout: ["err", "Cháy"], market_close: ["muted", "Thị trường nghỉ"], disabled: ["muted", "Tắt"] };
  var toggleErr = null; // {code, want, msg} — giữ qua các lần vẽ lại cho tới khi đổi được hoặc bỏ qua

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(n, d) { return n == null ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function signed(n) { return n == null ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n), 2); }
  function tone(n) { return n > 0 ? "up" : n < 0 ? "down" : ""; }
  function api(path, opts) {
    var o = Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {});
    if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
    // Máy chủ treo (Wi-Fi LAN chập chờn) → báo sau 15 giây thay vì khung xám mãi (dogfood edge)
    var ctrl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = null;
    if (ctrl) { o.signal = ctrl.signal; timer = setTimeout(function () { ctrl.abort(); }, TIMEOUT_MS); }
    return fetch(path, o).then(function (r) {
      if (r.status === 401) {
        location.href = "/login?het=1&next=" + encodeURIComponent(location.pathname);
        throw new Error("auth");
      }
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) { var e = new Error(typeof body.detail === "string" ? body.detail : "HTTP " + r.status); e.status = r.status; throw e; }
        return body;
      });
    }, function (e) {
      if (e && e.name === "AbortError") { var t = new Error("slow"); t.slow = true; throw t; }
      throw e;
    }).finally(function () { if (timer) clearTimeout(timer); });
  }
  function errText(e) {
    if (e.slow) return "Máy chủ phản hồi chậm quá 15 giây — kiểm tra Wi-Fi rồi thử lại.";
    if (e.status >= 500) return "Máy chủ GoldPulse đang gặp lỗi — thử lại sau ít phút.";
    if (e.status) return e.message;
    return "Mất kết nối tới máy chủ — kiểm tra mạng, hoặc máy chạy GoldPulse có đang bật không.";
  }
  function ruleText(a) {
    var r = a.rule || {}, h = r.horizon_minutes === 60 ? "giữ 60 phút" : "giữ 15 phút";
    if (r.kind === "volatility") return "Chỉ vào lệnh khi giá đang dao động mạnh (trung bình ≥ " + fmt(r.min_vol, 1) + " USD mỗi 15 phút trong 24 giờ qua), " + h + ".";
    if (r.kind === "confidence") return "Chỉ vào lệnh khi bot chắc ≥ " + fmt(100 * (0.5 + r.min_conf), 1) + "% về hướng giá, " + h + ".";
    return "Vào mọi dự đoán tự động của bot, " + h + ".";
  }
  function waitText(live) {
    // Khi nào có lệnh (dogfood newbie ý c)
    if (live && live.market_open === false) return "Thị trường nghỉ — lệnh sớm nhất khi mở lại lúc " + esc(live.next_open_vn || "05:00") + " (giờ VN).";
    return "Chờ dự đoán tự động kế tiếp — bot xét mỗi 15 phút, khoảng 2 dự đoán mỗi ngày.";
  }
  var OFF_TEXT = "Đang tắt — không mở lệnh mới. Lệnh đang mở (nếu có) vẫn đóng đúng hạn; số liệu cũ giữ nguyên.";
  function sw(a) {
    return '<button class="sw" type="button" role="switch" aria-checked="' + (a.enabled ? "true" : "false") + '" data-code="' + esc(a.code) +
      '" aria-label="Tài khoản ' + esc(a.code) + '"><span class="track"></span><span class="lbl">' + (a.enabled ? "Bật" : "Tắt") + "</span></button>";
  }
  function settleToggleErr(accounts) {
    // Máy chủ chậm (> 15 s) nhưng thực ra đã đổi xong → lần tải sau thấy khớp ý người bấm thì bỏ lời báo lỗi (dogfood rushed)
    if (!toggleErr) return;
    accounts.forEach(function (a) { if (toggleErr && a.code === toggleErr.code && a.enabled === toggleErr.want) toggleErr = null; });
  }
  // Rời trang lúc công tắc còn đang gửi → lần bấm có thể chưa tới máy chủ (dogfood rushed)
  window.addEventListener("beforeunload", function (ev) {
    if (document.querySelector(".sw[disabled]")) { ev.preventDefault(); ev.returnValue = ""; }
  });
  function toggleErrBlock() {
    if (!toggleErr) return "";
    return '<div class="error" role="alert" id="swErr"><p>Chưa ' + (toggleErr.want ? "bật" : "tắt") + " được tài khoản " + esc(toggleErr.code) + ": " + esc(toggleErr.msg) +
      '</p><button class="btn" type="button" id="swRetry">Thử lại</button></div>';
  }
  function priceCard(lv) {
    var bad = lv.price == null || !lv.fresh;
    var badge = lv.price == null ? '<span class="badge err">Chưa có giá</span>'
      : !lv.market_open ? '<span class="badge muted">Nghỉ</span>'
      : '<span class="badge ' + (bad ? "err" : "ok") + '">Trễ ' + (lv.lag_seconds == null ? "?" : lv.lag_seconds < 120 ? lv.lag_seconds + "s" : Math.round(lv.lag_seconds / 60) + " phút") + "</span>";
    return '<div class="card"><div class="label">XAUUSD</div><div class="value num">' + fmt(lv.price, 2) + "</div><div>" + badge + "</div></div>";
  }

  /* bật/tắt — dùng chung S8/S9. Lỗi → công tắc về trạng thái cũ + khối lỗi GIỮ qua lần vẽ lại, có Thử lại (PRD §7) */
  function postToggle(code, want, after) {
    return api("/api/paper/accounts/" + code + "/enabled", { method: "POST", body: { enabled: want } })
      .then(function () { toggleErr = null; }, function (e) { if (e.message !== "auth") toggleErr = { code: code, want: want, msg: errText(e) }; })
      .then(after);
  }
  function bindSwitches(root, after) {
    Array.prototype.forEach.call(root.querySelectorAll(".sw:not([data-b])"), function (b) {
      b.dataset.b = "1";
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var want = b.getAttribute("aria-checked") !== "true";
        b.disabled = true;
        b.querySelector(".lbl").textContent = want ? "Đang bật…" : "Đang tắt…";
        postToggle(b.dataset.code, want, after);
      });
    });
    var retry = document.getElementById("swRetry");
    if (retry && !retry.dataset.b) {
      retry.dataset.b = "1";
      retry.addEventListener("click", function () {
        retry.disabled = true; retry.textContent = "Đang thử…";
        postToggle(toggleErr.code, toggleErr.want, after);
      });
    }
  }

  /* ================= S8 ================= */
  if (screen === "s8") {
    var last = null, offline = null, busy = false;

    function accRow(a, live) {
      var eq = a.equity, t = a.today, tot = a.total, o = a.open;
      var cls = "prow acc" + (t.burned ? " burn" : "") + (a.enabled ? "" : " off");
      var state;
      if (o) {
        state = "Đang mở: " + (o.direction === "up" ? '<span class="up">▲ mua</span>' : '<span class="down">▼ bán</span>') + " " + fmt(o.entry_price, 2) +
          " · tạm <span class=\"" + tone(o.unrealized) + '">' + signed(o.unrealized) + " USD</span> · đóng lúc " + esc(o.due_vn);
      } else if (!a.enabled) {
        state = OFF_TEXT;
      } else if (t.burned) {
        state = "Đã cháy hôm nay (số dư về 0) — bắt đầu lại 10.00 USD lúc 05:00 giờ VN.";
      } else {
        state = (t.trades ? "Không có lệnh đang mở. " : "Chưa có lệnh hôm nay. ") + waitText(live);
      }
      var winRate = t.trades ? " · thắng " + Math.round(100 * t.wins / t.trades) + "%" : "";
      return '<div class="' + cls + '" role="link" tabindex="0" data-href="/paper/' + esc(a.code) + '"><div class="top"><div>' +
        '<div class="name">' + esc(a.code) + " · " + esc(a.name) +
        (t.burned ? ' <span class="badge err">Cháy</span>' : "") + (a.enabled ? "" : ' <span class="badge muted">Đã tắt</span>') + '<span class="chev">›</span></div>' +
        '<div class="rule">' + esc(ruleText(a)) + "</div>" +
        '<div class="eq ' + (a.enabled ? tone(eq - a.start_equity) : "") + '">' + fmt(eq, 2) + ' <span class="muted">USD</span></div>' +
        '<div class="sub num">Lãi/lỗ hôm nay <span class="' + (a.enabled ? tone(t.pnl) : "") + '">' + signed(t.pnl) + " USD</span></div>" +
        '<div class="sub">' + state + "</div>" +
        '<div class="sub num">Hôm nay ' + t.trades + " lệnh" + winRate + " · cộng dồn " + tot.days + " ngày " + signed(tot.pnl) + " USD (" + tot.trades + " lệnh) · ngày cháy " +
        tot.burned_days + "/" + tot.days + "</div></div>" + sw(a) + "</div></div>";
    }

    function curve(accs) {
      var have = accs.some(function (a) { return a.curve.length; });
      if (!have) return '<div class="curve"><div class="skel chartbox"></div><div class="muted mt">Có số sau ngày giao dịch đầu tiên.</div></div>';
      var dates = {};
      accs.forEach(function (a) { a.curve.forEach(function (p) { dates[p.date] = 1; }); });
      var days = Object.keys(dates).sort().slice(-20);
      // viewBox theo đúng bề rộng thật → chữ trục không bị kéo giãn (SVG không co giãn tỉ lệ)
      var W = Math.max(240, Math.round(content.clientWidth - 16)), H = 120, padL = 26, padR = 8, padT = 8, padB = 16, max = 10, min = 0;
      accs.forEach(function (a) { a.curve.forEach(function (p) { if (p.end > max) max = p.end; }); });
      max = Math.ceil(max + 1);
      function x(i) { return padL + (W - padL - padR) * (days.length === 1 ? 0.5 : i / (days.length - 1)); }
      function y(v) { return padT + (H - padT - padB) * (1 - (v - min) / (max - min)); }
      var out = '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y(10) + '" y2="' + y(10) + '" stroke="var(--color-border)" stroke-width="1"/>';
      accs.forEach(function (a) {
        var pts = a.curve.filter(function (p) { return days.indexOf(p.date) >= 0; });
        if (!pts.length) return;
        var d = pts.map(function (p, i) { return (i ? "L" : "M") + x(days.indexOf(p.date)).toFixed(1) + " " + y(p.end).toFixed(1); }).join(" ");
        out += '<path d="' + d + '" fill="none" stroke="' + (a.enabled ? "var(--color-text)" : "var(--color-text-muted)") + '" stroke-width="1.5"' +
          (DASH[a.code] ? ' stroke-dasharray="' + DASH[a.code] + '"' : "") + "/>";
        if (pts.length === 1) out += '<circle cx="' + x(days.indexOf(pts[0].date)) + '" cy="' + y(pts[0].end) + '" r="2.5" fill="var(--color-text)"/>';
      });
      var ticks = days.length > 7 ? [0, Math.floor((days.length - 1) / 2), days.length - 1] : days.map(function (_, i) { return i; });
      ticks.forEach(function (i) {
        var p = days[i].split("-");
        out += '<text x="' + x(i) + '" y="' + (H - 1) + '" font-size="10" fill="var(--color-text-muted)" text-anchor="' + (i === 0 ? "start" : i === days.length - 1 ? "end" : "middle") + '">' + p[2] + "/" + p[1] + "</text>";
      });
      out += '<text x="' + (padL - 4) + '" y="' + (y(10) + 3) + '" font-size="10" text-anchor="end" fill="var(--color-text-muted)">10</text>' +
        '<text x="' + (padL - 4) + '" y="' + (y(max) + 8) + '" font-size="10" text-anchor="end" fill="var(--color-text-muted)">' + max + '</text>' +
        '<text x="' + (padL - 4) + '" y="' + (y(0) + 3) + '" font-size="10" text-anchor="end" fill="var(--color-text-muted)">0</text>';
      // Hai tài khoản cùng số dư mọi ngày → nét sau đè kín nét trước; nói thẳng trong chú giải (dogfood mobile)
      function key(a) { return a.curve.map(function (p) { return p.date + ":" + p.end; }).join(","); }
      var dupOf = {};
      accs.forEach(function (a, i) { for (var j = 0; j < i; j++) if (accs[j].curve.length && key(accs[j]) === key(a)) { dupOf[a.code] = accs[j].code; break; } });
      var legend = accs.map(function (a) {
        return '<span><svg viewBox="0 0 28 10" aria-hidden="true"><line x1="0" y1="5" x2="28" y2="5" stroke="' + (a.enabled ? "var(--color-text)" : "var(--color-text-muted)") +
          '" stroke-width="2"' + (DASH[a.code] ? ' stroke-dasharray="' + DASH[a.code] + '"' : "") + "/></svg>" + esc(a.code) + " " + esc(a.name) + (a.enabled ? "" : " (tắt)") +
          (dupOf[a.code] ? " (trùng " + esc(dupOf[a.code]) + ")" : "") + "</span>";
      }).join("");
      return '<div class="curve"><svg class="chart" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Số dư cuối ngày ' + days.length + ' ngày">' + out +
        '</svg><div class="legend">' + legend + "</div></div>";
    }

    function botBadge(d) {
      if (offline) return '<div><span class="badge muted">Chưa rõ trạng thái bot — đang mất kết nối</span></div>';
      if (!d.worker_alive) {
        return '<div class="error" role="alert"><p>Máy chạy bot giao dịch giấy đang dừng — lệnh không được mở và không được đóng. Số dưới đây là lần cập nhật cuối. ' +
          'Trên máy chạy GoldPulse, khởi động lại GoldPulse (<span class="num">make deploy</span>).</p><button class="btn" type="button" data-reload>Tải lại</button></div>';
      }
      return d.live.market_open
        ? '<div><span class="badge ok">Bot đang chạy · kiểm lệnh mỗi phút</span></div>'
        : '<div><span class="badge ok">Bot đang bật · chờ thị trường mở lại ' + esc(d.live.next_open_vn || "") + "</span></div>";
    }

    function render(d) {
      settleToggleErr(d.accounts);
      var h = toggleErrBlock();
      if (offline) h += '<div class="error" role="alert"><p>Không cập nhật được. ' + esc(errText(offline)) + ' Số bên dưới là lần tải cuối.</p><button class="btn" type="button" data-reload>Thử lại</button></div>';
      h += '<div class="head3">' +
        '<div class="card"><div class="label">Lãi/lỗ hôm nay</div><div class="value num ' + tone(d.today_pnl) + '">' + signed(d.today_pnl) + " USD</div></div>" +
        '<div class="card"><div class="label">Lệnh hôm nay</div><div class="value num">' + d.today_trades + (d.today_trades ? " · thắng " + d.today_wins : "") + "</div></div>" +
        priceCard(d.live) + "</div>";
      h += botBadge(d);
      h += '<div class="fake">Tiền giả — sổ ghi trong hệ thống, không nối sàn nào. Mỗi tài khoản bắt đầu lại 10.00 USD mỗi ngày giao dịch (05:00 giờ VN = 18:00 New York).</div>';
      h += d.accounts.map(function (a) { return accRow(a, d.live); }).join("");
      var nDays = {};
      d.accounts.forEach(function (a) { a.curve.forEach(function (p) { nDays[p.date] = 1; }); });
      var nd = Math.min(20, Object.keys(nDays).length);
      h += "<h2>Số dư cuối ngày" + (nd ? " · " + nd + " ngày gần nhất" : "") + "</h2>" + curve(d.accounts);
      var a0 = d.accounts[0] || { oz: 1, leverage: 1000, spread: 0.3 };
      h += '<h2>Cách tính</h2><div class="muted rules">' +
        "<p>Mỗi lệnh " + fmt(a0.oz, 0) + " oz (0.01 lot) · đòn bẩy 1:" + a0.leverage + " · phí spread " + fmt(a0.spread, 2) + " USD/oz trừ lúc vào lệnh. Mỗi tài khoản tối đa 1 lệnh cùng lúc.</p>" +
        "<p>Không cắt lỗ theo điểm: một lệnh xấu có thể mất trọn 10 USD của ngày — gọi là <strong>cháy</strong>, tài khoản nghỉ tới 05:00 hôm sau.</p>" +
        "<p><strong>Cộng dồn</strong> là tổng lãi/lỗ của các ngày; mỗi ngày vẫn bắt đầu lại từ 10.00 USD, tiền không chuyển sang hôm sau.</p>" +
        "<p>Sau 4 tuần: tài khoản nào có ≥ " + d.thresholds.go_min_trades + " lệnh, cộng dồn lãi sau phí > 0 và cháy ≤ " + Math.round(100 * d.thresholds.go_max_burn_ratio) +
        "% số ngày thì luật đó được giữ để chạy tiếp; không tài khoản nào đạt thì dừng bot giao dịch giấy (bot dự đoán vẫn chạy).</p></div>";
      content.innerHTML = h;
      Array.prototype.forEach.call(content.querySelectorAll(".acc"), function (el) {
        function go(ev) { if (!ev.target.closest(".sw")) location.href = el.dataset.href; }
        el.addEventListener("click", go);
        el.addEventListener("keydown", function (ev) { if (ev.key === "Enter") go(ev); });
      });
      bindSwitches(content, function () { load(true); });
      Array.prototype.forEach.call(content.querySelectorAll("[data-reload]"), function (b) { b.addEventListener("click", function () { load(true); }); });
    }

    function load(force) {
      if (busy && !force) return;
      busy = true;
      api("/api/paper/accounts").then(function (d) { last = d; offline = null; render(d); })
        .catch(function (e) {
          if (e.message === "auth") return;
          offline = e;
          if (last) render(last);
          else {
            content.innerHTML = toggleErrBlock() + '<div class="error" role="alert"><p>' + esc(errText(e)) + '</p><button class="btn" type="button" data-reload>Thử lại</button></div>';
            content.querySelector("[data-reload]").addEventListener("click", function () { load(true); });
          }
        })
        .then(function () { busy = false; });
    }
    load(true);
    setInterval(function () { if (!document.hidden && !content.querySelector(".sw[disabled]")) load(false); }, 5000);
  }

  /* ================= S9 ================= */
  if (screen === "s9") {
    var code = content.dataset.code, lastId = null, loaded = false, extra = false, busy9 = false;

    function tradeRow(t) {
      var arrow = t.direction === "up" ? '<span class="up">▲ mua</span>' : '<span class="down">▼ bán</span>';
      var c = t.context || {};
      if (t.status === "skipped") {
        return '<div class="prow skip"><div class="num">' + esc(t.opened_vn) + " · " + arrow + "</div>" +
          "<div>Không vào lệnh — " + esc(SKIP[t.skip_reason] || t.skip_reason) + "</div></div>";
      }
      var badge = t.status === "open" ? ["warn", "Đóng lúc " + t.due_vn] : EXIT[t.exit_reason] || ["muted", t.exit_reason];
      var money = t.status === "open" ? "đang mở" : '<span class="' + tone(t.pnl) + '">' + signed(t.pnl) + " USD</span>";
      var why = "Vì sao vào: bot chắc " + (c.p_up == null ? "—" : fmt(100 * Math.max(c.p_up, 1 - c.p_up), 1) + "%") + " về hướng này · giá dao động TB " +
        (c.vol96 == null ? "—" : fmt(c.vol96, 2)) + " USD mỗi 15 phút (24 giờ qua) · tín hiệu bật: " + ((c.fired || []).join(", ") || "không") +
        "\nPhí: spread " + fmt(t.spread, 2) + " × " + fmt(t.oz, 0) + " oz · dự đoán #" + t.prediction_id;
      if (t.status === "closed") {
        var move = t.direction === "up" ? t.exit_price - t.entry_price : t.entry_price - t.exit_price;
        why += "\nRa: " + (t.exit_reason === "horizon" ? "hết thời gian giữ" : t.exit_reason === "stopout" ? "số dư về 0 (cháy)" : t.exit_reason === "market_close" ? "thị trường nghỉ — không tính vào đánh giá 4 tuần" : "tắt") +
          " · giá đi theo hướng " + signed(move) + " − phí " + fmt(t.spread, 2);
      }
      return '<button class="prow" type="button" aria-expanded="false" data-id="' + t.id + '"><div class="top"><span class="num">' + esc(t.opened_vn) + (t.closed_vn ? " → " + esc(t.closed_vn) : "") + " · " + arrow +
        ' <span class="muted">' + fmt(t.entry_price, 2) + (t.exit_price != null ? " → " + fmt(t.exit_price, 2) : "") + "</span></span>" +
        '<span class="badge ' + badge[0] + '">' + esc(badge[1]) + "</span></div>" +
        '<div class="num">' + money + '</div><div class="more">' + esc(why) + "</div></button>";
    }
    function bindRows(root) {
      Array.prototype.forEach.call(root.querySelectorAll("button.prow:not([data-b])"), function (el) {
        el.dataset.b = "1";
        el.addEventListener("click", function () { var o = el.classList.toggle("open"); el.setAttribute("aria-expanded", o ? "true" : "false"); });
      });
    }
    function render(d) {
      settleToggleErr([d]);
      var openIds = Array.prototype.map.call(content.querySelectorAll(".prow.open"), function (el) { return el.dataset.id; });
      document.getElementById("accTitle").textContent = d.code + " · " + d.name;
      var slot = document.getElementById("swSlot");
      slot.innerHTML = sw(d);
      bindSwitches(slot, function () { load(); });
      var t = d.today, tot = d.total;
      var h = toggleErrBlock() + '<div class="head3">' +
        '<div class="card"><div class="label">Số dư hiện tại</div><div class="value num ' + tone(d.equity - d.start_equity) + '">' + fmt(d.equity, 2) + "</div></div>" +
        '<div class="card"><div class="label">Lãi/lỗ hôm nay</div><div class="value num ' + tone(t.pnl) + '">' + signed(t.pnl) + '</div><div class="muted">' + t.trades + " lệnh</div></div>" +
        '<div class="card"><div class="label">Cộng dồn</div><div class="value num ' + tone(tot.pnl) + '">' + signed(tot.pnl) + '</div><div class="muted">' + tot.days + " ngày</div></div></div>";
      h += '<div class="note num">' + tot.trades + " lệnh" + (tot.trades ? " · thắng " + fmt(100 * tot.wins / tot.trades, 0) + "% sau phí" : "") +
        " · ngày cháy " + tot.burned_days + "/" + tot.days + "</div>";
      h += '<div class="note">' + esc(ruleText(d)) + " Tiền giả.</div>";
      if (!d.enabled) h += '<div class="note">' + OFF_TEXT + "</div>";
      if (!d.rows.length) {
        h += '<div class="empty"><p>Tài khoản này chưa có lệnh. ' + (d.enabled ? waitText(d.live) : "Bật tài khoản để bot bắt đầu vào lệnh theo luật trên.") +
          '</p><a href="/predictions">Xem sổ dự đoán</a></div>';
      } else {
        h += '<h2>Nhật ký lệnh</h2><div id="tlist">' + d.rows.map(tradeRow).join("") + "</div>" +
          (d.rows.length === 50 ? '<button class="btn line block" type="button" id="more">Tải thêm</button>' : "");
        lastId = d.rows[d.rows.length - 1].id;
      }
      h += '<div class="note" id="paused" hidden>Tạm dừng tự cập nhật trong lúc bạn xem chi tiết lệnh — đóng chi tiết để cập nhật lại.</div>';
      content.innerHTML = h;
      openIds.forEach(function (id) {
        var el = content.querySelector('.prow[data-id="' + id + '"]');
        if (el) { el.classList.add("open"); el.setAttribute("aria-expanded", "true"); }
      });
      bindRows(content);
      bindSwitches(content, function () { load(); });
      var more = document.getElementById("more");
      if (more) more.addEventListener("click", function () {
        more.disabled = true; more.textContent = "Đang tải…";
        api("/api/paper/accounts/" + code + "/trades?before=" + lastId).then(function (r) {
          extra = true;
          var list = document.getElementById("tlist");
          list.insertAdjacentHTML("beforeend", r.rows.map(tradeRow).join(""));
          bindRows(list);
          if (r.rows.length) lastId = r.rows[r.rows.length - 1].id;
          if (r.rows.length < 50) more.remove(); else { more.disabled = false; more.textContent = "Tải thêm"; }
        }).catch(function (e) { more.disabled = false; more.textContent = "Tải thêm — lỗi, bấm thử lại"; if (e.message !== "auth") more.title = errText(e); });
      });
    }
    function load() {
      if (busy9) return;
      busy9 = true;
      api("/api/paper/accounts/" + code).then(function (d) { loaded = true; render(d); })
        .catch(function (e) {
          if (e.message === "auth") return;
          if (loaded) return; // giữ số cũ; lần tải sau thử lại
          content.innerHTML = '<div class="error" role="alert"><p>' + esc(errText(e)) + '</p><button class="btn" type="button" id="retry">Thử lại</button></div>';
          document.getElementById("retry").addEventListener("click", load);
        })
        .then(function () { busy9 = false; });
    }
    load();
    // Tự tải lại 5 giây như S8 — trừ khi đang đọc chi tiết một lệnh hoặc đã "Tải thêm" (không để mất chỗ đang xem)
    setInterval(function () {
      var reading = !!content.querySelector(".prow.open");
      var p = document.getElementById("paused");
      if (p) p.hidden = !reading;
      if (!document.hidden && !extra && !reading && !document.querySelector(".sw[disabled]")) load();
    }, 5000);
  }
})();
