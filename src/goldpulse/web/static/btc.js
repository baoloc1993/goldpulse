/* GoldPulse (v4) — BTC · Nhật ký lệnh BTC. TIỀN GIẢ. JS thuần, không thư viện ngoài.
   Cùng khuôn với paper.js (Tài khoản giấy/Nhật ký lệnh): số tự tải lại 5 giây, đường số dư SVG vẽ tay, 4 đường phân biệt bằng nét.
   Chiến thuật nói bằng lời thường, không công thức . */
(function () {
  "use strict";
  var screen = document.body.dataset.screen;
  var content = document.getElementById("content");
  var TIMEOUT_MS = 15000;
  var DASH = { "BTC-A": "", "BTC-B": "6 3", "BTC-C": "2 3", "BTC-D": "6 3 2 3" };
  var SKIP = {
    open_trade: "đang có lệnh mở", burned: "đã cháy hôm nay", disabled: "tài khoản đang tắt",
    stale: "bot xử lý giờ này chậm hơn 5 phút so với giờ tròn nên không vào ở giá cũ"
  };
  var EXIT = { horizon: ["ok", "Hết hạn"], day_close: ["muted", "Hết ngày"], stopout: ["err", "Cháy"] };
  var toggleErr = null;
  var pressing = 0; // chạm gần nhất — tự tải lại không dựng lại DOM đúng lúc ngón tay đang bấm
  ["pointerdown", "touchstart", "keydown"].forEach(function (ev) { document.addEventListener(ev, function () { pressing = Date.now(); }, true); });
  function touching() { return Date.now() - pressing < 700; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(n, d) { return n == null ? "—" : Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function signed(n) { return n == null ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n), 2); }
  function tone(n) { return n > 0 ? "up" : n < 0 ? "down" : ""; }
  function letter(code) { return String(code).replace("BTC-", ""); }
  function api(path, opts) {
    var o = Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {});
    if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
    var ctrl = typeof AbortController === "function" ? new AbortController() : null;
    var timer = null;
    if (ctrl) { o.signal = ctrl.signal; timer = setTimeout(function () { ctrl.abort(); }, TIMEOUT_MS); }
    return fetch(path, o).then(function (r) {
      if (r.status === 401) {
        location.href = "/login?het=1&next=" + encodeURIComponent(location.pathname);
        throw new Error("auth");
      }
      return r.json().then(function (b) { return { ok: true, body: b }; }, function () { return { ok: false, body: {} }; }).then(function (x) {
        var body = x.body;
        if (!r.ok) { var e = new Error(typeof body.detail === "string" ? body.detail : "HTTP " + r.status); e.status = r.status; throw e; }
        if (!x.ok || body === null || typeof body !== "object") { var e2 = new Error("shape"); e2.shape = true; throw e2; }
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
    if (e.shape) return "Máy chủ trả dữ liệu lạ (mạng Wi-Fi có thể đang chặn hoặc bắt đăng nhập) — thử lại sau ít phút.";
    if (e.status) return e.message;
    return "Mất kết nối tới máy chủ — kiểm tra mạng, hoặc máy chạy GoldPulse có đang bật không.";
  }
  function holdText(s) { return "giữ " + (s.hold_hours || 1) + " giờ"; }
  function ruleText(a) {
    var s = a.strategy || {};
    if (s.kind === "model") return "Mỗi giờ tròn, mô hình chấm hàng chục chỉ số của BTC; chỉ vào ở 1 trong 20 giờ nó chắc nhất, " + holdText(s) + ".";
    if (s.kind === "model_trend") return "Như A nhưng lấy 1 trong 10 giờ chắc nhất, và chỉ khi cùng chiều xu hướng 24 giờ (giá trên đường trung bình thì chỉ mua, dưới thì chỉ bán), " + holdText(s) + ".";
    if (s.kind === "spike") return "Nến 1 giờ chạy mạnh gấp " + fmt(s.k_sigma || 3, 0) + " lần bình thường → đánh theo chiều đó, " + holdText(s) + ".";
    if (s.kind === "squeeze") return "Giá đi ngang hẹp nhất trong 30 ngày rồi một nến phá ra khỏi dải Bollinger → đánh theo chiều phá, " + holdText(s) + ".";
    return "";
  }
  function waitText(lv) {
    // Khi nào có lệnh + người dùng có phải làm gì không 
    return "Bot tự vào lệnh khi có tín hiệu — bạn không cần làm gì. Kiểm mỗi giờ tròn (lần tới " + esc(lv.next_check_vn || "") +
      " giờ VN); có thể nhiều giờ, thậm chí cả ngày không có lệnh.";
  }
  var OFF_TEXT = "Đang tắt — không mở lệnh mới. Lệnh đang mở (nếu có) vẫn đóng đúng luật; số liệu cũ giữ nguyên.";
  function sw(a) {
    return '<button class="sw" type="button" role="switch" aria-checked="' + (a.enabled ? "true" : "false") + '" data-code="' + esc(a.code) +
      '" aria-label="Tài khoản ' + esc(a.code) + '"><span class="track"></span><span class="lbl">' + (a.enabled ? "Bật" : "Tắt") + "</span></button>";
  }
  function settleToggleErr(accounts) {
    if (!toggleErr) return;
    accounts.forEach(function (a) { if (toggleErr && a.code === toggleErr.code && a.enabled === toggleErr.want) toggleErr = null; });
  }
  window.addEventListener("beforeunload", function (ev) {
    if (document.querySelector(".sw[disabled]")) { ev.preventDefault(); ev.returnValue = ""; }
  });
  function toggleErrBlock() {
    if (!toggleErr) return "";
    return '<div class="error" role="alert" id="swErr"><p>Chưa ' + (toggleErr.want ? "bật" : "tắt") + " được tài khoản " + esc(toggleErr.code) + ": " + esc(toggleErr.msg) +
      '</p><button class="btn" type="button" id="swRetry">Thử lại</button></div>';
  }
  function lagText(s) { return s == null ? "?" : s < 120 ? s + "s" : Math.round(s / 60) + " phút"; }
  function priceCard(lv) {
    var badge = lv.price == null ? '<span class="badge err">Chưa có giá</span>'
      : '<span class="badge ' + (lv.fresh ? "ok" : "err") + '">Nến trễ ' + lagText(lv.lag_seconds) + "</span>";
    return '<div class="card"><div class="label">BTCUSDT · Binance</div><div class="value num">' + fmt(lv.price, 2) + "</div><div>" + badge + "</div></div>";
  }
  function postToggle(code, want, after) {
    return api("/api/btc/accounts/" + code + "/enabled", { method: "POST", body: { enabled: want } })
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
  var VERDICT = { go: "đạt điều kiện GO", pivot: "lãi nhưng chưa đủ điều kiện (PIVOT)", none: "chưa lãi" };
  // Con số mà quyết định GO/PIVOT/KILL thực sự dựa vào: số lệnh đánh giá (không tính lệnh hết ngày) / ngưỡng
  function evalText(tot, thr) {
    return "Đánh giá 8 tuần: " + tot.eval_n + "/" + thr.go_min_trades + " lệnh · cộng dồn " + signed(tot.pnl) + " USD · " + (VERDICT[tot.verdict] || "—");
  }
  function arrowOf(dir) { return dir === 1 ? '<span class="up">▲ mua</span>' : '<span class="down">▼ bán</span>'; }

  /* ================= BTC ================= */
  if (screen === "s10") {
    var last = null, offline = null, busy = false, shownErr = "", thr = { go_min_trades: 30 };

    function accRow(a, lv) {
      var eq = a.equity, t = a.today, tot = a.total, o = a.open;
      var cls = "prow acc" + (t.burned ? " burn" : "") + (a.enabled ? "" : " off");
      var state;
      if (o) {
        state = "Đang mở: " + arrowOf(o.direction) + " " + fmt(o.entry_price, 2) + " · lệnh " + fmt(o.notional, 2) + " USD · tạm <span class=\"" +
          tone(o.unrealized) + '">' + signed(o.unrealized) + " USD</span> · đóng lúc " + esc(o.due_vn);
      } else if (!a.enabled) {
        state = OFF_TEXT;
      } else if (t.burned) {
        state = "Đã cháy hôm nay (lỗ hết số dư) — bắt đầu lại 10.00 USD lúc 07:00 sáng.";
      } else {
        state = (t.trades ? "Không có lệnh đang mở. " : "Chưa có lệnh hôm nay. ") + waitText(lv);
      }
      var winRate = t.trades ? " · thắng " + Math.round(100 * t.wins / t.trades) + "%" : "";
      var rowErr = toggleErr && toggleErr.code === a.code ? toggleErrBlock() : "";
      return '<div class="' + cls + '" role="link" tabindex="0" data-href="/btc/' + esc(a.code) + '"><div class="top"><div>' +
        '<div class="name">' + esc(letter(a.code)) + " · " + esc(a.name) +
        (t.burned ? ' <span class="badge err">Cháy</span>' : "") + (a.enabled ? "" : ' <span class="badge muted">Đã tắt</span>') + '<span class="chev">›</span></div>' +
        '<div class="rule">' + esc(ruleText(a)) + "</div>" +
        '<div class="eq ' + (a.enabled ? tone(eq - a.start_equity) : "") + '">' + fmt(eq, 2) + ' <span class="muted">USD</span></div>' +
        '<div class="sub num">Lãi/lỗ hôm nay <span class="' + (a.enabled ? tone(t.pnl) : "") + '">' + signed(t.pnl) + " USD</span></div>" +
        '<div class="sub">' + state + "</div>" +
        '<div class="sub num">Hôm nay ' + t.trades + " lệnh" + winRate + " · cộng dồn " + tot.days + " ngày " + signed(tot.pnl) + " USD (" + tot.trades + " lệnh) · cháy " +
        tot.burned_days + " trong " + tot.days + " ngày</div>" +
        '<div class="sub num">' + esc(evalText(tot, thr)) + "</div>" + rowErr + "</div>" + sw(a) + "</div></div>";
    }

    function curve(accs) {
      var have = accs.some(function (a) { return a.curve.length; });
      if (!have) return '<div class="curve"><div class="muted">Chưa có ngày nào có lệnh — đường số dư hiện sau ngày đầu tiên có lệnh.</div></div>';
      var dates = {};
      accs.forEach(function (a) { a.curve.forEach(function (p) { dates[p.date] = 1; }); });
      var days = Object.keys(dates).sort().slice(-20);
      var W = Math.max(240, Math.round(content.clientWidth - 16)), H = 140, padL = 30, padR = 8, padT = 10, padB = 20, max = 10, min = 10;
      accs.forEach(function (a) { a.curve.forEach(function (p) { if (p.end > max) max = p.end; if (p.end < min) min = p.end; }); });
      max = Math.ceil(max + 1); min = Math.max(0, Math.floor(min - 1));
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
        out += '<text x="' + x(i) + '" y="' + (H - 1) + '" font-size="13" fill="var(--color-text-muted)" text-anchor="' + (i === 0 ? "start" : i === days.length - 1 ? "end" : "middle") + '">' + p[2] + "/" + p[1] + "</text>";
      });
      out += '<text x="' + (padL - 4) + '" y="' + (y(10) + 3) + '" font-size="13" text-anchor="end" fill="var(--color-text-muted)">10</text>' +
        '<text x="' + (padL - 4) + '" y="' + (y(max) + 8) + '" font-size="13" text-anchor="end" fill="var(--color-text-muted)">' + max + '</text>' +
        '<text x="' + (padL - 4) + '" y="' + (y(min) + 3) + '" font-size="13" text-anchor="end" fill="var(--color-text-muted)">' + min + '</text>';
      var legend = accs.map(function (a) {
        return '<span><svg viewBox="0 0 28 10" aria-hidden="true"><line x1="0" y1="5" x2="28" y2="5" stroke="' + (a.enabled ? "var(--color-text)" : "var(--color-text-muted)") +
          '" stroke-width="2"' + (DASH[a.code] ? ' stroke-dasharray="' + DASH[a.code] + '"' : "") + "/></svg>" + esc(letter(a.code)) + " " + esc(a.name) + (a.enabled ? "" : " (tắt)") + "</span>";
      }).join("");
      return '<div class="curve"><svg class="chart btc" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Số dư cuối ngày ' + days.length + ' ngày">' + out +
        '</svg><div class="legend">' + legend + "</div></div>";
    }

    function statusBlock(d) {
      var lv = d.live;
      if (offline) return '<div><span class="badge muted">Chưa rõ trạng thái bot — đang mất kết nối</span></div>';
      if (!lv.worker_alive) {
        return '<div class="error" role="alert"><p>Máy chạy bot BTC đang dừng — không lấy giá, lệnh không được mở và không được đóng. Số dưới đây là lần cập nhật cuối. ' +
          'Trên máy chạy GoldPulse, khởi động lại instance BTC.</p><button class="btn" type="button" data-reload>Tải lại</button></div>';
      }
      if (lv.price == null || !lv.fresh) {
        return '<div class="error" role="alert"><p>Không lấy được giá BTC từ Binance' + (lv.error ? " (" + esc(lv.error) + ")" : "") +
          " — không mở lệnh mới; lệnh đang mở chờ giá và được tính lại đúng khi có nến bù. Kiểm tra mạng máy chủ.</p>" +
          '<button class="btn" type="button" data-reload>Thử lại</button></div>';
      }
      return '<div><span class="badge ok">Bot đang chạy · kiểm tín hiệu mỗi giờ tròn · lần tới ' + esc(lv.next_check_vn) + "</span></div>";
    }

    function render(d) {
      settleToggleErr(d.accounts);
      var lv = d.live;
      thr = d.thresholds;
      var h = "";
      if (offline) h += '<div class="error" role="alert"><p>Không cập nhật được. ' + esc(errText(offline)) + ' Số bên dưới là lần tải cuối (' + esc(d.now_vn) + ' giờ VN).</p><button class="btn" type="button" data-reload>Thử lại</button></div>';
      h += '<div class="head3 btc">' +
        '<div class="card"><div class="label">Lãi/lỗ hôm nay</div><div class="value num ' + tone(d.today_pnl) + '">' + signed(d.today_pnl) + " USD</div></div>" +
        '<div class="card"><div class="label">Lệnh hôm nay</div><div class="value num">' + d.today_trades + (d.today_trades ? " · thắng " + d.today_wins : "") + "</div></div>" +
        priceCard(lv) + "</div>";
      if (lv.history_days < 30) {
        h += '<div class="empty"><p>Đang nạp lịch sử BTC — ' + fmt(lv.history_days, 0) + "/" + lv.history_target +
          " ngày. Chiến thuật bắt đầu chạy khi đủ 30 ngày (vài phút).</p><a href=\"/btc\">Tải lại</a></div>";
      } else {
        h += statusBlock(d);
      }
      h += '<div class="fake">Tiền giả — sổ ghi trong hệ thống, không nối sàn nào. Mỗi tài khoản bắt đầu lại 10.00 USD lúc 07:00 sáng giờ VN mỗi ngày (00:00 UTC). <strong>Cháy</strong> = lỗ hết 10.00 USD trong ngày, nghỉ tới sáng hôm sau. Giá BTCUSDT của Binance (1 USDT ≈ 1 USD).</div>';
      h += d.accounts.map(function (a) { return accRow(a, lv); }).join("");
      var nDays = {};
      d.accounts.forEach(function (a) { a.curve.forEach(function (p) { nDays[p.date] = 1; }); });
      var nd = Math.min(20, Object.keys(nDays).length);
      h += "<h2>Số dư cuối ngày" + (nd ? " · " + nd + " ngày gần nhất" : "") + "</h2>" + curve(d.accounts);
      var a0 = d.accounts[0] || { leverage: 20, cost_pct: 0.14 };
      h += '<h2>Cách tính</h2><div class="muted rules">' +
        "<p>Mỗi lệnh có giá trị = " + fmt(a0.leverage, 0) + " × số dư lúc vào (đòn bẩy x" + fmt(a0.leverage, 0) + "). Phí 0.05% + trượt giá 0.02% mỗi chiều — tổng " +
        fmt(a0.cost_pct, 2) + "% giá trị lệnh mỗi lần vào/ra. Mỗi tài khoản tối đa 1 lệnh cùng lúc.</p>" +
        "<p>Không cắt lỗ theo điểm: giá đi ngược khoảng 5% là mất trọn số dư của ngày — gọi là <strong>cháy</strong>, tài khoản nghỉ tới 07:00 sáng hôm sau. Lệnh còn mở lúc 06:59 sáng (cuối ngày UTC) được đóng.</p>" +
        "<p><strong>Cộng dồn</strong> là tổng lãi/lỗ của các ngày; mỗi ngày vẫn bắt đầu lại từ 10.00 USD.</p>" +
        "<p>Tua lịch sử 2025–26: cả 4 chiến thuật đều lỗ sau phí — đây là thí nghiệm. Sau 8 tuần: tài khoản nào có ≥ " + d.thresholds.go_min_trades +
        " lệnh, cộng dồn lãi > 0 và cháy ≤ " + Math.round(100 * d.thresholds.go_max_burn_ratio) + "% số ngày thì chiến thuật đó được giữ; không tài khoản nào lãi thì dừng bot giao dịch BTC.</p></div>";
      content.innerHTML = h;
      Array.prototype.forEach.call(content.querySelectorAll(".acc"), function (el) {
        function go(ev) { if (!ev.target.closest(".sw, #swErr")) location.href = el.dataset.href; }
        el.addEventListener("click", go);
        el.addEventListener("keydown", function (ev) { if (ev.key === "Enter") go(ev); });
      });
      bindSwitches(content, function () { load(true); });
      bindReload();
      var se = document.getElementById("swErr");
      var key = toggleErr ? toggleErr.code + toggleErr.want : "";
      if (se && key !== shownErr) se.scrollIntoView({ block: "nearest" }); // lỗi nằm trong hàng vừa bấm: cuộn cho thấy, một lần
      shownErr = key;
    }

    function good10(d) { return d && Array.isArray(d.accounts) && d.live && d.thresholds; }
    function bindReload() {
      Array.prototype.forEach.call(content.querySelectorAll("[data-reload]"), function (b) {
        b.addEventListener("click", function () { b.disabled = true; b.textContent = "Đang thử…"; load(true); });
      });
    }
    function load(force) {
      if (busy && !force) return;
      busy = true;
      api("/api/btc/accounts").then(function (d) {
        if (!good10(d)) { var e = new Error("shape"); e.shape = true; throw e; }
        var same = last && offline === null && JSON.stringify(last) === JSON.stringify(d);
        last = d; offline = null;
        if ((same || touching()) && !force) return;
        render(d);
      })
        .catch(function (e) {
          if (e.message === "auth") return;
          offline = e;
          try {
            if (last) { render(last); return; }
          } catch (e2) { last = null; }
          content.innerHTML = toggleErrBlock() + '<div class="error" role="alert"><p>' + esc(errText(e)) + '</p><button class="btn" type="button" data-reload>Thử lại</button></div>';
          bindReload();
        })
        .finally(function () { busy = false; });
    }
    load(true);
    setInterval(function () { if (!document.hidden && !content.querySelector(".sw[disabled]") && !touching()) load(false); }, 5000);
  }

  /* ================= Nhật ký lệnh BTC ================= */
  if (screen === "s11") {
    var code = content.dataset.code, lastId = null, loaded = false, extra = false, busy11 = false;

    function whyIn(t) {
      var c = t.context || {};
      if (c.kind === "model" || c.kind === "model_trend") {
        var sure = c.p_up == null ? "—" : fmt(100 * Math.max(c.p_up, 1 - c.p_up), 1) + "%";
        var s = "Vì sao vào: mô hình nghiêng " + (c.p_up > 0.5 ? "tăng" : "giảm") + " " + sure + " (độ chắc " + fmt(c.conf, 3) + " ≥ ngưỡng " + fmt(c.thr, 3) + ")";
        if (c.kind === "model_trend") s += " · giá " + (c.ema_gap > 0 ? "trên" : "dưới") + " đường trung bình 24 giờ " + fmt(100 * Math.abs(c.ema_gap), 2) + "% → cùng chiều";
        return s;
      }
      if (c.kind === "spike") return "Vì sao vào: nến 1 giờ chạy " + fmt(Math.abs(c.z1h), 1) + " lần mức dao động bình thường (ngưỡng 3) → đánh theo chiều nến";
      if (c.kind === "squeeze") {
        return "Vì sao vào: giờ trước dải Bollinger hẹp (" + fmt(100 * c.bb_width_prev, 2) + "% < mốc 20% hẹp nhất 30 ngày " + fmt(100 * c.bb_q20_prev, 2) +
          "%), giờ này giá đóng " + (t.direction === 1 ? "trên dải trên " + fmt(c.bb_upper, 2) : "dưới dải dưới " + fmt(c.bb_lower, 2));
      }
      return "";
    }
    function tradeRow(t) {
      var arrow = arrowOf(t.direction);
      if (t.status === "skipped") {
        return '<div class="prow skip"><div class="num">Bỏ qua giờ ' + esc(t.bar_vn) + ' (giờ VN)</div>' +
          "<div>Chiến thuật báo " + (t.direction === 1 ? "mua" : "bán") + " nhưng không vào lệnh — " + esc(SKIP[t.skip_reason] || t.skip_reason) + "</div></div>";
      }
      var badge = t.status === "open" ? ["warn", "Đóng lúc " + t.due_vn] : EXIT[t.exit_reason] || ["muted", t.exit_reason];
      var money = t.status === "open" ? "đang mở" : '<span class="' + tone(t.pnl) + '">' + signed(t.pnl) + " USD</span>";
      var why = whyIn(t) + "\nLệnh " + fmt(t.notional, 2) + " USD (x20 × số dư " + fmt((t.context || {}).equity, 2) + ")";
      if (t.status === "closed") {
        var move = t.direction * (t.exit_price / t.entry_price - 1);
        why += "\nRa: " + (t.exit_reason === "horizon" ? "hết thời gian giữ" : t.exit_reason === "stopout" ? "lỗ hết số dư (cháy)" : "hết ngày lúc 06:59 sáng — không tính vào số lệnh đánh giá 8 tuần") +
          (t.exit_reason === "stopout" ? "" : " · giá đi theo hướng " + (move >= 0 ? "+" : "−") + fmt(Math.abs(100 * move), 3) + "% − phí 0.14% → " + signed(t.pnl) + " USD");
      }
      return '<button class="prow" type="button" aria-expanded="false" data-id="' + esc(t.id) + '"><div class="top"><span class="num">' + esc(t.opened_vn) + (t.closed_vn ? " → " + esc(t.closed_vn) : "") + " · " + arrow +
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
    var headJson = null, listBuilt = false, listJson = null;
    function renderHead(d, paused) {
      settleToggleErr([d]);
      document.getElementById("accTitle").textContent = letter(d.code) + " · " + d.name;
      var slot = document.getElementById("swSlot");
      var swState = String(d.enabled);
      if (slot.dataset.s !== swState) {
        slot.dataset.s = swState;
        slot.innerHTML = sw(d);
        bindSwitches(slot, function () { load(true); });
      }
      var t = d.today, tot = d.total;
      var h = toggleErrBlock() + '<div class="head3 btc">' +
        '<div class="card"><div class="label">Số dư hiện tại</div><div class="value num ' + tone(d.equity - d.start_equity) + '">' + fmt(d.equity, 2) + "</div></div>" +
        '<div class="card"><div class="label">Lãi/lỗ hôm nay</div><div class="value num ' + tone(t.pnl) + '">' + signed(t.pnl) + '</div><div class="muted">' + t.trades + " lệnh</div></div>" +
        '<div class="card"><div class="label">Cộng dồn</div><div class="value num ' + tone(tot.pnl) + '">' + signed(tot.pnl) + '</div><div class="muted">' + tot.days + " ngày · cháy " + tot.burned_days + "</div></div></div>";
      h += '<div class="note num">' + tot.trades + " lệnh" + (tot.trades ? " · thắng " + fmt(100 * tot.wins / tot.trades, 0) + "% sau phí" : "") +
        " · " + holdText(d.strategy || {}) + " · lệnh = " + fmt(d.leverage, 0) + " × số dư · phí + trượt " + fmt(d.cost_pct, 2) + "% khứ hồi</div>";
      h += '<div class="note num">' + esc(evalText(tot, d.thresholds)) + "</div>";
      h += '<div class="note">' + esc(ruleText(d)) + " Tiền giả.</div>";
      if (!d.enabled) h += '<div class="note">' + OFF_TEXT + "</div>";
      if (paused) h += '<div class="note" id="paused">Danh sách lệnh tạm dừng cập nhật (bạn đang xem chi tiết hoặc đã bấm Tải thêm) — số ở trên vẫn mới. Tải lại trang để cập nhật danh sách.</div>';
      var head = document.getElementById("head");
      head.innerHTML = h;
      bindSwitches(head, function () { load(true); });
    }
    function renderList(d) {
      var box = document.getElementById("listbox");
      var h = "";
      if (!d.rows.length) {
        h += '<div class="empty"><p>Tài khoản này chưa có lệnh. ' + (d.enabled ? waitText(d.live) : "Bật tài khoản để bot bắt đầu vào lệnh theo chiến thuật trên.") +
          '</p><a href="/btc">Về màn BTC</a></div>';
      } else {
        h += '<h2>Nhật ký lệnh (giờ VN)</h2><div id="tlist">' + d.rows.map(tradeRow).join("") + "</div>" +
          (d.rows.length === 50 ? '<button class="btn line block" type="button" id="more">Tải thêm</button>' : "");
        lastId = d.rows[d.rows.length - 1].id;
      }
      box.innerHTML = h;
      listBuilt = true;
      bindRows(box);
      var more = document.getElementById("more");
      if (more) more.addEventListener("click", function () {
        more.disabled = true; more.textContent = "Đang tải…";
        api("/api/btc/accounts/" + code + "/trades?before=" + lastId).then(function (r) {
          extra = true;
          var list = document.getElementById("tlist");
          list.insertAdjacentHTML("beforeend", r.rows.map(tradeRow).join(""));
          bindRows(list);
          if (r.rows.length) lastId = r.rows[r.rows.length - 1].id;
          if (r.rows.length < 50) more.remove(); else { more.disabled = false; more.textContent = "Tải thêm"; }
        }).catch(function (e) { more.disabled = false; more.textContent = "Tải thêm — lỗi, bấm thử lại"; if (e.message !== "auth") more.title = errText(e); });
      });
    }
    function render(d, force) {
      if (!document.getElementById("head")) content.innerHTML = '<div id="head"></div><div id="listbox"></div>';
      var reading = !!content.querySelector(".prow.open");
      var paused = listBuilt && (extra || reading);
      var j = JSON.stringify([d.enabled, d.today, d.total, d.equity, d.name, toggleErr, paused]);
      if (j !== headJson || force) { headJson = j; renderHead(d, paused); }
      if (!listBuilt || (!extra && !reading && !touching())) {
        var openIds = Array.prototype.map.call(content.querySelectorAll(".prow.open"), function (el) { return el.dataset.id; });
        var lj = JSON.stringify(d.rows.map(function (r) { return [r.id, r.status, r.pnl, r.exit_reason]; }));
        if (!listBuilt || lj !== listJson) {
          listJson = lj;
          renderList(d);
          openIds.forEach(function (id) {
            var el = content.querySelector('.prow[data-id="' + id + '"]');
            if (el) { el.classList.add("open"); el.setAttribute("aria-expanded", "true"); }
          });
        }
      }
    }
    function load(force) {
      if (busy11 && !force) return;
      busy11 = true;
      api("/api/btc/accounts/" + code).then(function (d) {
        if (!d || !d.code || !Array.isArray(d.rows) || !d.total || !d.today) { var e = new Error("shape"); e.shape = true; throw e; }
        loaded = true; render(d, force);
      })
        .catch(function (e) {
          if (e.message === "auth") return;
          if (loaded) return;
          content.innerHTML = '<div class="error" role="alert"><p>' + esc(errText(e)) + '</p><button class="btn" type="button" id="retry">Thử lại</button></div>';
          document.getElementById("retry").addEventListener("click", function () { this.disabled = true; this.textContent = "Đang thử…"; load(true); });
        })
        .finally(function () { busy11 = false; });
    }
    load(true);
    setInterval(function () { if (!document.hidden && !document.querySelector(".sw[disabled]") && !touching()) load(false); }, 5000);
  }
})();
