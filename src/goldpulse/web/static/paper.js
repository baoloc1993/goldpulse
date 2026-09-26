/* GoldPulse (v3) — S8 Tài khoản giấy · S9 Nhật ký lệnh. TIỀN GIẢ. JS thuần, không thư viện ngoài.
   Số tự tải lại 5 giây như S1; đường số dư là SVG vẽ tay, 4 đường phân biệt bằng nét (DESIGN-SYSTEM C15). */
(function () {
  "use strict";
  var screen = document.body.dataset.screen;
  var content = document.getElementById("content");
  var DASH = { A: "", B: "6 3", C: "2 3", D: "6 3 2 3" };
  var SKIP = {
    rule: "không khớp luật", open_trade: "đang có lệnh mở", margin: "không đủ ký quỹ", burned: "đã cháy hôm nay",
    disabled: "tài khoản đang tắt", market_closing: "sắp tới giờ nghỉ", stale: "dự đoán đã cũ > 5 phút"
  };
  var EXIT = { horizon: ["ok", "Hạn"], stopout: ["err", "Cháy"], market_close: ["muted", "Đóng cửa"], disabled: ["muted", "Tắt"] };

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
    return fetch(path, o).then(function (r) {
      if (r.status === 401) {
        location.href = "/login?het=1&next=" + encodeURIComponent(location.pathname);
        throw new Error("auth");
      }
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (!r.ok) { var e = new Error(typeof body.detail === "string" ? body.detail : "HTTP " + r.status); e.status = r.status; throw e; }
        return body;
      });
    });
  }
  function errText(e) {
    if (e.status >= 500) return "Máy chủ GoldPulse đang gặp lỗi — thử lại sau ít phút.";
    if (e.status) return e.message;
    return "Mất kết nối tới máy chủ — kiểm tra mạng, hoặc máy chạy GoldPulse có đang bật không.";
  }
  function ruleText(a) {
    var r = a.rule || {}, h = r.horizon_minutes === 60 ? "giữ 60 phút" : "giữ 15 phút";
    if (r.kind === "volatility") return "chỉ khi biến động 1 ngày ≥ " + fmt(r.min_vol, 1) + " USD/nến 15 phút · " + h;
    if (r.kind === "confidence") return "chỉ khi mô hình chắc (|p − 0.5| ≥ " + fmt(r.min_conf, 3) + ") · " + h;
    return "mọi dự đoán tự động · " + h;
  }
  function sw(a) {
    return '<button class="sw" type="button" role="switch" aria-checked="' + (a.enabled ? "true" : "false") + '" data-code="' + esc(a.code) +
      '" aria-label="Tài khoản ' + esc(a.code) + '"><span class="track"></span><span class="lbl">' + (a.enabled ? "Bật" : "Tắt") + "</span></button>";
  }
  function priceCard(lv) {
    var bad = lv.price == null || !lv.fresh;
    var badge = lv.price == null ? '<span class="badge err">Chưa có giá</span>'
      : !lv.market_open ? '<span class="badge muted">Thị trường nghỉ</span>'
      : '<span class="badge ' + (bad ? "err" : "ok") + '">Trễ ' + (lv.lag_seconds == null ? "?" : lv.lag_seconds < 120 ? lv.lag_seconds + "s" : Math.round(lv.lag_seconds / 60) + " phút") + "</span>";
    return '<div class="card"><div class="label">XAUUSD · gold-api</div><div class="value num">' + fmt(lv.price, 2) + "</div><div>" + badge + "</div></div>";
  }

  /* bật/tắt — dùng chung S8/S9; lỗi thì trả về trạng thái cũ + khối lỗi (PRD §7) */
  function bindSwitches(root, onDone) {
    Array.prototype.forEach.call(root.querySelectorAll(".sw:not([data-b])"), function (b) {
      b.dataset.b = "1";
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var want = b.getAttribute("aria-checked") !== "true";
        b.disabled = true;
        api("/api/paper/accounts/" + b.dataset.code + "/enabled", { method: "POST", body: { enabled: want } })
          .then(function () { b.setAttribute("aria-checked", want ? "true" : "false"); b.querySelector(".lbl").textContent = want ? "Bật" : "Tắt"; })
          .catch(function (e) { if (e.message !== "auth") showToggleError(errText(e)); })
          .then(function () { b.disabled = false; if (onDone) onDone(); });
      });
    });
  }
  function showToggleError(msg) {
    var old = document.getElementById("swErr");
    if (old) old.remove();
    content.insertAdjacentHTML("afterbegin", '<div class="error" role="alert" id="swErr"><p>Chưa đổi được trạng thái tài khoản: ' + esc(msg) +
      '</p><button class="btn" type="button" id="swErrOk">Thử lại</button></div>');
    document.getElementById("swErrOk").addEventListener("click", function () { document.getElementById("swErr").remove(); });
  }

  /* ================= S8 ================= */
  if (screen === "s8") {
    var last = null, offline = null, busy = false;

    function accRow(a) {
      var eq = a.equity, t = a.today, tot = a.total, o = a.open;
      var cls = "prow acc" + (t.burned ? " burn" : "") + (a.enabled ? "" : " off");
      var openLine;
      if (o) {
        openLine = (o.direction === "up" ? '<span class="up">▲</span>' : '<span class="down">▼</span>') + " " + fmt(o.entry_price, 2) +
          " → tạm <span class=\"" + tone(o.unrealized) + '">' + signed(o.unrealized) + "</span> · đóng lúc " + esc(o.due_vn);
      } else if (t.burned) {
        openLine = "Đã cháy hôm nay — mở lại lúc 05:00 giờ VN (18:00 New York)";
      } else if (!tot.trades && !t.trades) {
        openLine = "Chưa có lệnh — chờ dự đoán tự động kế tiếp (nến 15 phút, thị trường mở)";
      } else {
        openLine = "Không có lệnh đang mở";
      }
      var winRate = t.trades ? " · thắng " + Math.round(100 * t.wins / t.trades) + "%" : "";
      return '<div class="' + cls + '" role="link" tabindex="0" data-href="/paper/' + esc(a.code) + '"><div class="top"><div>' +
        '<div class="name">' + esc(a.code) + " · " + esc(a.name) +
        (t.burned ? ' <span class="badge err">Cháy</span>' : "") + (a.enabled ? "" : ' <span class="badge muted">Đã tắt</span>') + "</div>" +
        '<div class="eq ' + (a.enabled ? tone(eq - a.start_equity) : "") + '">' + fmt(eq, 2) + ' <span class="muted">USD · hôm nay ' + signed(t.pnl) + "</span></div>" +
        '<div class="sub">' + openLine + "</div>" +
        '<div class="sub num">' + t.trades + " lệnh hôm nay" + winRate + " · tích luỹ " + signed(tot.pnl) + " (" + tot.trades + " lệnh) · cháy " +
        tot.burned_days + "/" + tot.days + " ngày</div></div>" + sw(a) + "</div></div>";
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
          (DASH[a.code] ? ' stroke-dasharray="' + DASH[a.code] + '"' : "") + (pts.length === 1 ? ' stroke-linecap="round"' : "") + "/>";
        if (pts.length === 1) out += '<circle cx="' + x(days.indexOf(pts[0].date)) + '" cy="' + y(pts[0].end) + '" r="2.5" fill="var(--color-text)"/>';
      });
      var ticks = days.length > 3 ? [0, Math.floor((days.length - 1) / 2), days.length - 1] : days.map(function (_, i) { return i; });
      ticks.forEach(function (i) {
        var p = days[i].split("-");
        out += '<text x="' + x(i) + '" y="' + (H - 1) + '" font-size="10" fill="var(--color-text-muted)" text-anchor="' + (i === 0 ? "start" : i === days.length - 1 ? "end" : "middle") + '">' + p[2] + "/" + p[1] + "</text>";
      });
      out += '<text x="' + (padL - 4) + '" y="' + (y(10) + 3) + '" font-size="10" text-anchor="end" fill="var(--color-text-muted)">10</text>' +
        '<text x="' + (padL - 4) + '" y="' + (y(max) + 8) + '" font-size="10" text-anchor="end" fill="var(--color-text-muted)">' + max + '</text>' +
        '<text x="' + (padL - 4) + '" y="' + (y(0) + 3) + '" font-size="10" text-anchor="end" fill="var(--color-text-muted)">0</text>';
      var legend = accs.map(function (a) {
        return '<span><svg viewBox="0 0 28 10" aria-hidden="true"><line x1="0" y1="5" x2="28" y2="5" stroke="' + (a.enabled ? "var(--color-text)" : "var(--color-text-muted)") +
          '" stroke-width="2"' + (DASH[a.code] ? ' stroke-dasharray="' + DASH[a.code] + '"' : "") + "/></svg>" + esc(a.code) + " " + esc(a.name) + (a.enabled ? "" : " (tắt)") + "</span>";
      }).join("");
      return '<div class="curve"><svg class="chart" viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Số dư cuối ngày ' + days.length + ' ngày">' + out +
        '</svg><div class="legend">' + legend + "</div></div>";
    }

    function render(d) {
      var h = "";
      if (offline) h += '<div class="error" role="alert"><p>Không cập nhật được. ' + esc(errText(offline)) + ' Số bên dưới là lần tải cuối.</p><button class="btn" type="button" data-reload>Thử lại</button></div>';
      h += '<div class="head3">' +
        '<div class="card"><div class="label">Hôm nay · ' + d.accounts.length + ' TK</div><div class="value num ' + tone(d.today_pnl) + '">' + signed(d.today_pnl) + " USD</div></div>" +
        '<div class="card"><div class="label">Lệnh hôm nay</div><div class="value num">' + d.today_trades + (d.today_trades ? " · thắng " + d.today_wins : "") + "</div></div>" +
        priceCard(d.live) + "</div>";
      h += d.worker_alive
        ? '<div><span class="badge ok">Worker đang chạy · khớp lệnh mỗi phút</span></div>'
        : '<div class="error" role="alert"><p>Worker không chạy — lệnh không được khớp và không được đóng. Số dưới đây là lần cập nhật cuối. Trên máy chủ: <span class="num">make deploy</span> (hoặc <span class="num">make dev</span>).</p><button class="btn" type="button" data-reload>Tải lại</button></div>';
      h += '<div class="fake">Tiền giả — sổ ghi trong hệ thống, không nối sàn nào. Mỗi tài khoản bắt đầu lại 10.00 USD mỗi ngày giao dịch (05:00 giờ VN = 18:00 New York).</div>';
      h += d.accounts.map(accRow).join("");
      h += "<h2>Số dư cuối ngày · 20 ngày</h2>" + curve(d.accounts);
      h += '<h2>Luật</h2><div class="muted rules">' + d.accounts.map(function (a) {
        return "<p><strong>" + esc(a.code) + " " + esc(a.name) + "</strong> — " + esc(ruleText(a)) + ".</p>";
      }).join("") + "<p>Chung: " + fmt(d.accounts[0] ? d.accounts[0].oz : 1, 0) + " oz (0.01 lot) · đòn bẩy 1:" + (d.accounts[0] ? d.accounts[0].leverage : 1000) +
        " · spread " + fmt(d.accounts[0] ? d.accounts[0].spread : 0.3, 2) + " USD/oz · không cắt lỗ theo điểm, chỉ cháy khi số dư về 0. Sau 4 tuần: luật nào ≥ " +
        d.thresholds.go_min_trades + " lệnh, lãi sau phí > 0 và cháy ≤ " + Math.round(100 * d.thresholds.go_max_burn_ratio) + "% số ngày là GO.</p></div>";
      content.innerHTML = h;
      Array.prototype.forEach.call(content.querySelectorAll(".acc"), function (el) {
        function go(ev) { if (!ev.target.closest(".sw")) location.href = el.dataset.href; }
        el.addEventListener("click", go);
        el.addEventListener("keydown", function (ev) { if (ev.key === "Enter") go(ev); });
      });
      bindSwitches(content, load);
      Array.prototype.forEach.call(content.querySelectorAll("[data-reload]"), function (b) { b.addEventListener("click", load); });
    }

    function load() {
      if (busy) return;
      busy = true;
      api("/api/paper/accounts").then(function (d) { last = d; offline = null; render(d); })
        .catch(function (e) {
          if (e.message === "auth") return;
          offline = e;
          if (last) render(last);
          else {
            content.innerHTML = '<div class="error" role="alert"><p>' + esc(errText(e)) + '</p><button class="btn" type="button" data-reload>Thử lại</button></div>';
            content.querySelector("[data-reload]").addEventListener("click", load);
          }
        })
        .then(function () { busy = false; });
    }
    load();
    setInterval(function () { if (!document.hidden && !content.querySelector(".sw[disabled]")) load(); }, 5000);
  }

  /* ================= S9 ================= */
  if (screen === "s9") {
    var code = content.dataset.code, lastId = null, loaded = false;

    function tradeRow(t) {
      var arrow = t.direction === "up" ? '<span class="up">▲</span>' : '<span class="down">▼</span>';
      var c = t.context || {};
      if (t.status === "skipped") {
        return '<div class="prow skip"><div class="top"><span class="num">' + esc(t.opened_vn) + " · " + arrow + '</span><span class="badge muted">Bỏ qua</span></div>' +
          "<div>Bỏ qua — " + esc(SKIP[t.skip_reason] || t.skip_reason) + "</div></div>";
      }
      var badge = t.status === "open" ? ["warn", "Đóng lúc " + t.due_vn] : EXIT[t.exit_reason] || ["muted", t.exit_reason];
      var money = t.status === "open" ? "đang mở" : '<span class="' + tone(t.pnl) + '">' + signed(t.pnl) + " USD</span>";
      var why = "Vào: mô hình p = " + (c.p_up == null ? "—" : fmt(c.p_up, 3)) + " · biến động 1 ngày " + (c.vol96 == null ? "—" : fmt(c.vol96, 2)) +
        " USD/nến · tín hiệu bật: " + ((c.fired || []).join(", ") || "không") +
        "\nPhí: spread " + fmt(t.spread, 2) + " × " + fmt(t.oz, 0) + " oz · dự đoán #" + t.prediction_id;
      if (t.status === "closed") {
        var move = t.direction === "up" ? t.exit_price - t.entry_price : t.entry_price - t.exit_price;
        why += "\nRa: " + (t.exit_reason === "horizon" ? "hết hạn" : t.exit_reason === "stopout" ? "số dư về 0 (cháy)" : t.exit_reason === "market_close" ? "thị trường nghỉ — không tính vào GO/KILL" : "tắt") +
          " · Δ theo hướng " + signed(move) + " − phí " + fmt(t.spread, 2);
      }
      return '<button class="prow" type="button" aria-expanded="false"><div class="top"><span class="num">' + esc(t.opened_vn) + (t.closed_vn ? " → " + esc(t.closed_vn) : "") + " · " + arrow +
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
      document.getElementById("accTitle").textContent = d.code + " · " + d.name;
      var slot = document.getElementById("swSlot");
      slot.innerHTML = sw(d);
      bindSwitches(slot);
      var t = d.today, tot = d.total;
      var h = '<div class="head3">' +
        '<div class="card"><div class="label">Số dư hiện tại</div><div class="value num ' + tone(d.equity - d.start_equity) + '">' + fmt(d.equity, 2) + "</div></div>" +
        '<div class="card"><div class="label">Hôm nay</div><div class="value num ' + tone(t.pnl) + '">' + signed(t.pnl) + " · " + t.trades + " lệnh</div></div>" +
        '<div class="card"><div class="label">Tích luỹ ' + tot.days + ' ngày</div><div class="value num ' + tone(tot.pnl) + '">' + signed(tot.pnl) + " · cháy " + tot.burned_days + "</div></div></div>";
      h += '<div class="muted mt">' + tot.trades + " lệnh" + (tot.trades ? " · thắng " + fmt(100 * tot.wins / tot.trades, 1) + "% sau phí" : "") + " · " + esc(ruleText(d)) +
        " · tiền giả</div>";
      var rows = d.rows.filter(function () { return true; });
      if (!rows.length) {
        h += '<div class="empty"><p>Tài khoản này chưa có lệnh — lệnh mở khi có dự đoán tự động khớp luật (nến 15 phút, thị trường mở).</p><a href="/predictions">Xem sổ dự đoán</a></div>';
      } else {
        h += '<h2>Nhật ký lệnh</h2><div id="tlist">' + rows.map(tradeRow).join("") + "</div>" +
          (rows.length === 50 ? '<button class="btn line block" type="button" id="more">Tải thêm</button>' : "");
        lastId = rows[rows.length - 1].id;
      }
      content.innerHTML = h;
      bindRows(content);
      var more = document.getElementById("more");
      if (more) more.addEventListener("click", function () {
        more.disabled = true; more.textContent = "Đang tải…";
        api("/api/paper/accounts/" + code + "/trades?before=" + lastId).then(function (r) {
          var list = document.getElementById("tlist");
          list.insertAdjacentHTML("beforeend", r.rows.map(tradeRow).join(""));
          bindRows(list);
          if (r.rows.length) lastId = r.rows[r.rows.length - 1].id;
          if (r.rows.length < 50) more.remove(); else { more.disabled = false; more.textContent = "Tải thêm"; }
        }).catch(function (e) { more.disabled = false; more.textContent = "Tải thêm — lỗi, thử lại"; if (e.message !== "auth") more.title = errText(e); });
      });
    }
    function load() {
      api("/api/paper/accounts/" + code).then(function (d) { loaded = true; render(d); })
        .catch(function (e) {
          if (e.message === "auth" || loaded) return;
          content.innerHTML = '<div class="error" role="alert"><p>' + esc(errText(e)) + '</p><button class="btn" type="button" id="retry">Thử lại</button></div>';
          document.getElementById("retry").addEventListener("click", load);
        });
    }
    load();
  }
})();
