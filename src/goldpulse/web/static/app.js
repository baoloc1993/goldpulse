/* GoldPulse — trang theo dõi. JS thuần, không thư viện ngoài. */
(function () {
  "use strict";
  var screen = document.body.dataset.screen;
  var content = document.getElementById("content");

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(n, d) {
    if (n == null) return "—";
    return Number(n).toLocaleString("en-US", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function pct(n) { return n == null ? "—" : fmt(n, 2) + "%"; }
  function vnDate(iso) { var p = iso.slice(0, 10).split("-"); return p[2] + "/" + p[1] + "/" + p[0]; }
  function vnTime(iso) {
    return new Date(iso).toLocaleString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  function api(path, opts) {
    return fetch(path, Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {}))
      .then(function (r) {
        if (r.status === 401) { location.href = "/login"; throw new Error("auth"); }
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) { var e = new Error(body.detail || ("HTTP " + r.status)); e.status = r.status; e.body = body; throw e; }
          return body;
        });
      });
  }

  function errorBlock(msg, retryId) {
    return '<div class="error" role="alert"><p>' + esc(msg) + '</p><button class="btn" id="' + retryId + '">Thử lại</button></div>';
  }
  var NET_ERR = "Không kết nối được máy chủ. Kiểm tra máy đang chạy GoldPulse rồi thử lại.";

  /* ---------------- S1 Tổng quan ---------------- */
  var lastOverview = null;

  function metricCard(label, valueHtml, pass, note, href) {
    var cls = pass == null ? "card" : pass ? "card pass" : "card fail";
    var tag = href ? "a" : "div";
    return "<" + tag + ' class="' + cls + '"' + (href ? ' href="' + href + '"' : "") + ">" +
      '<div class="label">' + label + "</div>" +
      '<div class="value num">' + valueHtml + "</div>" +
      '<div class="muted">' + note + "</div></" + tag + ">";
  }

  function renderOverview(d) {
    var h = "";
    var p = d.price;
    // Giá + độ trễ
    if (p) {
      var badge, cardCls = "card";
      if (!p.market_open) badge = '<span class="badge warn">Thị trường đang nghỉ — giá cuối phiên</span>';
      else if (p.stale) { badge = '<span class="badge err">Giá trễ ' + (p.lag_seconds == null ? "?" : fmt(p.lag_seconds)) + " giây — đang chờ nguồn</span>"; cardCls = "card fail"; }
      else badge = '<span class="badge ok">Trễ ' + fmt(p.lag_seconds) + " giây</span>";
      h += '<div class="' + cardCls + '"><div class="label">XAUUSD · gold-api.com</div>' +
        '<div class="hero num">' + fmt(p.value, 2) + "</div>" +
        '<div class="mt">' + badge + (p.source_updated_vn ? ' <span class="muted">cập nhật ' + esc(p.source_updated_vn) + "</span>" : "") + "</div></div>";
    } else {
      h += '<div class="card fail"><div class="label">XAUUSD · gold-api.com</div><div class="value">Chưa có giá</div>' +
        '<div class="mt"><span class="badge err">Chưa nhận được mẫu giá nào — đang chờ nguồn</span></div></div>';
    }
    if (!d.worker_alive) {
      h += '<div class="error" role="alert"><p>Worker không chạy — giá và dữ liệu lịch sử đang đứng. Chạy lại bằng <span class="num">make dev</span>.</p></div>';
    }

    // Phiên đang chạy
    var s = d.sessions;
    h += '<div class="card"><div class="label">Phiên đang chạy</div>';
    if (s.length) {
      h += '<div class="value">' + s.map(function (x) { return esc(x.label); }).join(" + ") +
        (s.length > 1 ? ' <span class="muted">(chồng phiên)</span>' : "") + "</div>" +
        '<div class="muted">' + s.map(function (x) { return esc(x.label) + " đóng " + esc(x.closes_vn); }).join(" · ") + " (giờ VN)</div>";
    } else {
      h += '<div class="value">Không có phiên nào</div><div class="muted">Ngoài giờ Á / Âu / Mỹ</div>';
    }
    h += "</div>";

    var hist = d.history;
    var srcBad = hist.source === "unreachable" || hist.source === "throttled";
    if (hist.done_days === 0) {
      // Rỗng: chưa nạp ngày nào
      h += '<div class="empty"><p>Chưa nạp dữ liệu lịch sử — worker đang tải từ 2004.<br><span class="num">' +
        fmt(hist.done_days) + " / " + fmt(hist.total_days) + " ngày</span></p>" +
        '<a href="/coverage">Xem tiến độ theo năm</a></div>';
    } else {
      var m = d.metrics;
      h += "<h2>Ba ngưỡng của vòng 1</h2><div class=\"grid\">";
      var c = m.coverage;
      h += metricCard("Độ phủ từ 2004 · cần ≥ 99%", pct(c.value), c.value == null ? null : c.value >= c.target,
        (c.value == null ? "Chưa đo" : c.value >= c.target ? "Đạt" : "Chưa đạt") + " · xem chi tiết ›", "/coverage");
      var l = m.latency;
      h += metricCard("Phút giá trễ &lt; 30s · cần ≥ 95%", l.value == null ? "—" : fmt(l.value, 1) + "%", l.value == null ? null : l.value >= l.target,
        l.value == null ? "Chưa có phút thị trường mở nào để đo" : (l.value >= l.target ? "Đạt" : "Chưa đạt") + " · đo từ " + esc(l.since_vn) + " · " + fmt(l.minutes) + " phút");
      var sp = m.speed;
      h += metricCard("Tra 20 năm (p95) · cần &lt; 1 giây", sp.value == null ? "—" : fmt(sp.value) + " ms", sp.value == null ? null : sp.value < sp.target,
        sp.value == null ? 'Chưa tra lần nào — <a href="/query">tra thử</a>' : (sp.value < sp.target ? "Đạt" : "Chưa đạt") + " · " + fmt(sp.count) + " lần tra");
      h += "</div>";
    }

    // Nạp lịch sử + nguồn
    if (srcBad) {
      var msg = hist.source === "unreachable"
        ? "Không tới được nguồn lịch sử (Dukascopy) — kiểm tra Cloudflare WARP đã bật. Worker tự thử lại sau 5 phút."
        : "Dukascopy đang giới hạn tốc độ tải — worker tự chậm lại rồi thử tiếp, không cần làm gì.";
      h += '<div class="error warn" role="alert"><p>' + msg + '</p><button class="btn" id="wake">Thử lại ngay</button></div>';
    }
    h += "<h2>Nạp lịch sử</h2><div class=\"card\"><div class=\"label\">Dukascopy · nến 1 phút</div>" +
      '<div class="value num">' + fmt(hist.done_days) + " / " + fmt(hist.total_days) + " ngày</div><div class=\"mt\">";
    if (hist.source === "ok") h += '<span class="badge ok">Nguồn lịch sử tới được</span>';
    else if (hist.source === "unreachable") h += '<span class="badge warn">Không tới được nguồn — kiểm tra WARP</span>';
    else if (hist.source === "throttled") h += '<span class="badge warn">Nguồn đang giới hạn tốc độ</span>';
    else h += '<span class="badge warn">Chưa kết nối nguồn lần nào</span>';
    if (hist.error_days) h += ' <a class="badge err" href="/coverage">' + fmt(hist.error_days) + " ngày lỗi</a>";
    if (hist.last_backfill_vn) h += ' <span class="muted">lấy bù lần cuối ' + esc(hist.last_backfill_vn) + "</span>";
    h += "</div></div>";

    content.innerHTML = h;
    var w = document.getElementById("wake");
    if (w) w.addEventListener("click", function () {
      w.disabled = true; w.textContent = "Đang gửi…";
      api("/api/history/wake", { method: "POST" }).then(function () { w.textContent = "Đã báo worker thử lại"; })
        .catch(function () { w.disabled = false; w.textContent = "Thử lại ngay"; });
    });
  }

  function loadOverview() {
    api("/api/overview").then(function (d) { lastOverview = d; renderOverview(d); })
      .catch(function (e) {
        if (e.message === "auth") return;
        var block = errorBlock(NET_ERR, "retry");
        if (lastOverview) { renderOverview(lastOverview); content.insertAdjacentHTML("afterbegin", block); }
        else content.innerHTML = block;
        document.getElementById("retry").addEventListener("click", loadOverview);
      });
  }

  /* ---------------- S2 Độ phủ ---------------- */
  function gapText(g) {
    if (g.kind === "missing") return vnTime(g.from) + " → " + vnTime(g.to) + " (giờ VN)";
    var span = g.days > 1 ? vnDate(g.from) + " → " + vnDate(g.last_day) + " (" + g.days + " ngày)" : vnDate(g.from);
    return span + (g.kind === "error" ? " — ngày lỗi" : " — chưa nạp");
  }

  function renderCoverage(d) {
    if (!d.years.length) {
      content.innerHTML = '<div class="empty"><p>Chưa có năm nào được nạp. Worker bắt đầu từ 01/01/2004 khi khởi động.</p><a href="/overview">Về Tổng quan</a></div>';
      return;
    }
    var t = d.total, h = "";
    var pass = t.covered_pct != null && t.covered_pct >= 99;
    h += '<div class="card ' + (t.covered_pct == null ? "" : pass ? "pass" : "fail") + '"><div class="label">Tổng phút có nến · 2004 → nay · cần ≥ 99%</div>' +
      '<div class="value num">' + pct(t.covered_pct) + "</div>" +
      '<div class="muted">Phút có giao dịch thật: ' + pct(t.traded_pct) + " — năm cũ dữ liệu thưa hơn</div></div>";

    h += "<h2>Ngày lỗi (" + d.errors.length + ")</h2>";
    if (!d.errors.length) h += '<p class="muted">Không có ngày nào lỗi.</p>';
    d.errors.forEach(function (e) {
      h += '<div class="errday" data-day="' + esc(e.day) + '"><div><div class="num">' + vnDate(e.day) + "</div>" +
        '<div class="muted why">' + esc(e.error || "Lỗi không rõ") + " · thử " + e.attempts + " lần</div></div>" +
        '<button class="btn retry" type="button">Chạy lại</button></div>';
    });

    h += "<h2>Theo năm</h2>";
    d.years.forEach(function (y) {
      var loading = y.pending_days > 0;
      var hasGap = y.gaps.length > 0;
      h += '<div class="year"><div class="top"><strong class="num">' + y.year + "</strong>" +
        '<span class="num">' + pct(y.covered_pct) + " có nến</span></div>" +
        '<div class="bar' + (hasGap || loading ? " part" : "") + '"><i style="width:' + (y.covered_pct || 0) + '%"></i></div>' +
        '<div class="top mt"><span class="muted num">' +
        (loading ? "Đang nạp " + fmt(y.done_days) + "/" + fmt(y.days) + " ngày" : "Có giao dịch thật: " + pct(y.traded_pct)) + "</span>" +
        (hasGap ? '<button class="toggle" type="button" aria-expanded="false">' + y.gaps.length + " đoạn thiếu ›</button>" : '<span class="muted">Không thiếu</span>') +
        "</div>" + (hasGap ? '<div class="gaps">' + y.gaps.map(gapText).map(esc).join("<br>") + "</div>" : "") + "</div>";
    });
    content.innerHTML = h;
  }

  function loadCoverage() {
    api("/api/coverage").then(renderCoverage).catch(function (e) {
      if (e.message === "auth") return;
      content.innerHTML = errorBlock(NET_ERR, "retry");
      document.getElementById("retry").addEventListener("click", function () {
        content.innerHTML = '<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
        loadCoverage();
      });
    });
  }

  if (screen === "s2") {
    content.addEventListener("click", function (ev) {
      var tg = ev.target.closest(".toggle");
      if (tg) {
        var open = tg.closest(".year").classList.toggle("open");
        tg.setAttribute("aria-expanded", open ? "true" : "false");
        return;
      }
      var b = ev.target.closest(".retry");
      if (!b || b.disabled) return;
      var row = b.closest(".errday");
      b.disabled = true; b.textContent = "Đang gửi…";
      api("/api/crawl-days/" + row.dataset.day + "/retry", { method: "POST" })
        .then(function () {
          b.textContent = "Đang chờ";
          row.querySelector(".why").textContent = "Đã đưa về hàng chờ — worker sẽ tải lại";
        })
        .catch(function (e) {
          if (e.status === 404) { b.textContent = "Đang chờ"; row.querySelector(".why").textContent = "Ngày này đã được nạp lại hoặc đang chờ"; return; }
          b.disabled = false; b.textContent = "Chạy lại";
          row.querySelector(".why").textContent = "Không gửi được — kiểm tra kết nối rồi bấm lại";
        });
    });
  }

  /* ---------------- S3 Tra thử ---------------- */
  var SESS = { asia: "Á", europe: "Âu", us: "Mỹ" };
  var DOW = ["", "thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu"];

  function renderQuery(r, q) {
    if (!r.count) {
      content.innerHTML = '<div class="empty"><p>Chưa có dữ liệu cho khoảng này — năm đã chọn chưa nạp xong.</p><a href="/coverage">Xem độ phủ</a></div>';
      return;
    }
    content.innerHTML = '<div class="grid g2">' +
      '<div class="card"><div class="label">Số phiên</div><div class="value num">' + fmt(r.count) + "</div></div>" +
      '<div class="card"><div class="label">Biên độ TB</div><div class="value num">' + fmt(r.avg_range, 2) + " USD</div></div>" +
      '<div class="card ' + (r.duration_ms < 1000 ? "pass" : "fail") + '"><div class="label">Thời gian tra</div><div class="value num">' + fmt(r.duration_ms, 0) + " ms</div></div>" +
      '<div class="card"><div class="label">Phiên tăng</div><div class="value num up">' + pct(r.up_pct) + "</div></div>" +
      '<div class="card"><div class="label">Phiên giảm</div><div class="value num down">' + pct(r.down_pct) + "</div></div>" +
      '<div class="card"><div class="label">Đứng giá</div><div class="value num">' + pct(r.flat_pct) + "</div></div></div>" +
      '<p class="muted">Phiên ' + SESS[q.session] + " · " + q.year_from + " → " + q.year_to + " · " +
      (q.dow ? DOW[q.dow] : "mọi thứ trong tuần") + " · dữ liệu " + vnDate(r.first_day) + " → " + vnDate(r.last_day) + "</p>";
  }

  if (screen === "s3") {
    var form = document.getElementById("queryForm");
    var run = document.getElementById("run");
    var formErr = document.getElementById("formErr");
    var yearField = document.getElementById("yearField");
    var busy = false;
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      if (busy) return;
      var q = {
        session: form.session.value,
        year_from: parseInt(form.year_from.value, 10),
        year_to: parseInt(form.year_to.value, 10),
        dow: form.dow.value ? parseInt(form.dow.value, 10) : null
      };
      if (q.year_from > q.year_to) {
        yearField.classList.add("bad");
        formErr.textContent = "Năm bắt đầu phải trước hoặc bằng năm kết thúc."; formErr.hidden = false;
        return;
      }
      yearField.classList.remove("bad"); formErr.hidden = true;
      busy = true; run.disabled = true; run.textContent = "Đang tra…";
      content.innerHTML = '<div class="grid g2"><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>';
      var qs = "session=" + q.session + "&year_from=" + q.year_from + "&year_to=" + q.year_to + (q.dow ? "&dow=" + q.dow : "");
      api("/api/query?" + qs)
        .then(function (r) { renderQuery(r, q); })
        .catch(function (e) {
          if (e.message === "auth") return;
          content.innerHTML = '<div class="error" role="alert"><p>Tra không thành công — ' +
            (e.status ? esc(e.message) : "máy chủ không phản hồi") + ". Bộ lọc vẫn giữ nguyên, bấm Tra để thử lại.</p></div>";
        })
        .then(function () { busy = false; run.disabled = false; run.textContent = "Tra"; });
    });
  }

  /* ---------------- khởi động ---------------- */
  if (screen === "s1") {
    loadOverview();
    setInterval(function () { if (!document.hidden) loadOverview(); }, 5000);
  } else if (screen === "s2") {
    loadCoverage();
  }
})();
