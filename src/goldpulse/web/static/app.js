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
  function clock() {
    return new Date().toLocaleTimeString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", hour12: false });
  }

  function api(path, opts) {
    return fetch(path, Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {}))
      .then(function (r) {
        if (r.status === 401) {
          // Hết phiên → S4 kèm lời nhắn, nhập xong quay lại đúng màn đang đứng
          location.href = "/login?het=1&next=" + encodeURIComponent(location.pathname + location.search);
          throw new Error("auth");
        }
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) {
            var e = new Error(typeof body.detail === "string" ? body.detail : "HTTP " + r.status);
            e.status = r.status; throw e;
          }
          return body;
        });
      });
  }

  // Tách hai loại hỏng: máy chủ trả lỗi vs không tới được máy chủ (mất sóng / máy tắt)
  function errText(e) {
    if (e.status >= 500) return "Máy chủ GoldPulse đang gặp lỗi — thử lại sau ít phút.";
    if (e.status) return e.message;
    return "Mất kết nối tới máy chủ — kiểm tra mạng điện thoại, hoặc máy chạy GoldPulse có đang bật không.";
  }
  function errorBlock(msg, retryId) {
    return '<div class="error" role="alert"><p>' + esc(msg) + '</p><button class="btn" type="button" id="' + retryId + '">Thử lại</button></div>';
  }

  /* ---------------- S1 Tổng quan ---------------- */
  var lastOverview = null;
  var receivedAt = 0;        // Date.now() lúc nhận lastOverview — độ trễ cộng thêm thời gian đã trôi
  var offline = null;       // lỗi của lần tải gần nhất (null = đang cập nhật bình thường)
  var wakeClickedAt = null;  // ISO lúc bấm "Thử nạp lại ngay"

  function metricCard(label, valueHtml, pass, note, href) {
    var cls = pass == null ? "card" : pass ? "card pass" : "card fail";
    var tag = href ? "a" : "div";
    return "<" + tag + ' class="' + cls + '"' + (href ? ' href="' + href + '"' : "") + ">" +
      '<div class="label">' + label + "</div>" +
      '<div class="value num">' + valueHtml + "</div>" +
      '<div class="muted">' + note + "</div></" + tag + ">";
  }

  function priceBadge(p) {
    // Độ trễ tính lại mỗi giây ngay trên máy: mất kết nối thì số vẫn chạy và chuyển đỏ, không đứng im màu xanh
    var lag = p.lag_seconds == null ? null : p.lag_seconds + Math.round((Date.now() - receivedAt) / 1000);
    if (!p.market_open) return { cls: "warn", text: "Thị trường đang nghỉ — giá cuối phiên", fail: false };
    if (lag == null || lag >= 30) {
      return { cls: "err", text: "Giá trễ " + (lag == null ? "?" : fmt(lag)) + " giây — đang chờ nguồn", fail: true };
    }
    return { cls: "ok", text: "Trễ " + fmt(lag) + " giây", fail: false };
  }

  function tickPrice() {
    if (!lastOverview || !lastOverview.price) return;
    var b = priceBadge(lastOverview.price);
    var badge = document.getElementById("lagBadge");
    var card = document.getElementById("priceCard");
    if (!badge || !card) return;
    badge.className = "badge " + b.cls; badge.textContent = b.text;
    card.className = b.fail ? "card fail" : "card";
  }

  function renderOverview(d) {
    var h = "";
    var p = d.price;
    if (offline) h += errorBlock("Không cập nhật được. " + errText(offline) + " Số bên dưới là lần tải cuối.", "retry");
    if (p) {
      var b = priceBadge(p);
      h += '<div id="priceCard" class="' + (b.fail ? "card fail" : "card") + '"><div class="label">XAUUSD · giá giao ngay · gold-api.com</div>' +
        '<div class="hero num">' + fmt(p.value, 2) + "</div>" +
        '<div class="mt"><span id="lagBadge" class="badge ' + b.cls + '">' + esc(b.text) + "</span>" +
        (p.source_updated_vn ? ' <span class="muted">cập nhật ' + esc(p.source_updated_vn) + "</span>" : "") + "</div>" +
        '<div class="muted mt">Trễ dưới 30 giây là đạt. Nguồn đổi giá khoảng 30 giây một lần.</div></div>';
    } else {
      h += '<div class="card fail"><div class="label">XAUUSD · giá giao ngay · gold-api.com</div><div class="value">Chưa có giá</div>' +
        '<div class="mt"><span class="badge err">Chưa nhận được mẫu giá nào — đang chờ nguồn</span></div></div>';
    }
    if (!d.worker_alive) {
      h += '<div class="error" role="alert"><p>Máy nạp dữ liệu (worker) không chạy — giá và kho lịch sử đang đứng. Trên máy chạy GoldPulse, chạy lại <span class="num">make dev</span>.</p></div>';
    }

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
    var m = d.metrics;
    if (!hist.ok_days) {
      h += '<div class="empty"><p>' + (srcBad
        ? "Kho lịch sử chưa có ngày nào — đang chờ nguồn dữ liệu (xem khối cảnh báo bên dưới)."
        : "Kho lịch sử chưa có ngày nào — máy đang bắt đầu tải từ 01/01/2004, ngày đầu tiên sẽ hiện sau vài phút.") +
        '</p><a href="/coverage">Xem tiến độ theo năm</a></div>';
    } else {
      h += "<h2>Ba ngưỡng của vòng 1</h2><div class=\"grid\">";
      var c = m.coverage;
      var loading = hist.done_days < hist.total_days;  // đang nạp → chưa phán đạt/trượt
      h += metricCard("Độ phủ từ 2004 · cần ≥ 99%", pct(c.value), c.value == null || loading ? null : c.value >= c.target,
        (loading ? "Đang nạp " + fmt(hist.done_days) + "/" + fmt(hist.total_days) + " ngày — chưa tính xong"
          : c.value == null ? "Chưa đo" : c.value >= c.target ? "Đạt" : "Chưa đạt") + " · xem chi tiết ›", "/coverage");
      var l = m.latency;
      h += metricCard("Phút giá trễ &lt; 30s · cần ≥ 95%", l.value == null ? "—" : fmt(l.value, 1) + "%", l.value == null ? null : l.value >= l.target,
        l.value == null ? "Chưa có phút thị trường mở nào để đo" : (l.value >= l.target ? "Đạt" : "Chưa đạt") + " · đo từ " + esc(l.since_vn) + " · " + fmt(l.minutes) + " phút");
      var sp = m.speed;
      h += metricCard("Tra 20 năm (p95) · cần &lt; 1 giây", sp.value == null ? "—" : fmt(sp.value) + " ms", sp.value == null ? null : sp.value < sp.target,
        sp.value == null ? 'Chưa tra lần nào — <a href="/query">tra thử</a>' : (sp.value < sp.target ? "Đạt" : "Chưa đạt") + " · " + fmt(sp.count) + " lần tra");
      h += "</div>";
    }

    // Nguồn lịch sử hỏng: một câu cho người xem, một câu cho người giữ máy, kèm giờ thử
    if (srcBad) {
      var waiting = wakeClickedAt && (!hist.source_checked || hist.source_checked < wakeClickedAt);
      var src = hist.provider === "histdata" ? "HistData.com" : "Dukascopy";
      var detail = hist.source === "unreachable"
        ? "<p><strong>Kho lịch sử đang tạm dừng</strong> — máy chạy GoldPulse không kết nối được " + src + ". Giá realtime ở trên không bị ảnh hưởng.</p>" +
          (hist.provider === "histdata"
            ? "<p>Cách sửa: kiểm tra mạng Internet của máy chạy GoldPulse. Không cần WARP hay proxy.</p>"
            : "<p>Cách sửa (trên máy chạy GoldPulse): bật Cloudflare WARP.</p>")
        : "<p><strong>Kho lịch sử đang tải chậm lại</strong> — " + src + " đang giới hạn tốc độ. Máy tự thử tiếp, không cần làm gì.</p>";
      var when = (hist.source_checked_vn ? "Lần thử gần nhất " + esc(hist.source_checked_vn) + " — chưa được" : "") +
        (hist.retry_vn ? (Date.parse(hist.retry_at) > Date.now()
          ? " · tự thử lại lúc " + esc(hist.retry_vn) : " · đang thử lại…") : "");
      h += '<div class="error warn" role="alert">' + detail + (when ? '<p class="num">' + when + "</p>" : "") +
        '<button class="btn" type="button" id="wake"' + (waiting ? " disabled" : "") + ">" +
        (waiting ? "Đang thử kết nối…" : "Thử nạp lại ngay") + "</button></div>";
    }

    h += "<h2>Nạp lịch sử</h2><div class=\"card\"><div class=\"label\">Nến 1 phút · HistData (03/2009 → nay) · Dukascopy (2004 → 2009)</div>" +
      '<div class="value num">' + fmt(hist.done_days) + " / " + fmt(hist.total_days) + " ngày</div><div class=\"mt\">";
    if (hist.source === "ok") h += '<span class="badge ok">Nguồn lịch sử tới được</span>';
    else if (hist.source === "unreachable") h += '<span class="badge warn">Không tới được nguồn — ' + (hist.provider === "histdata" ? "kiểm tra mạng" : "kiểm tra WARP") + "</span>";
    else if (hist.source === "throttled") h += '<span class="badge warn">Nguồn đang giới hạn tốc độ</span>';
    else h += '<span class="badge warn">Chưa kết nối nguồn lần nào</span>';
    if (hist.error_days) h += ' <a class="badge err" href="/coverage">' + fmt(hist.error_days) + " ngày lỗi</a>";
    h += "</div>" + (hist.old_pending ? '<div class="muted mt">Còn ' + fmt(hist.old_pending) +
      " ngày 2004 → 03/2009 chưa nạp — đoạn này chỉ Dukascopy có (cần WARP một lần).</div>" : "") +
      (hist.last_backfill_vn ? '<div class="muted mt">Lấy bù lần cuối ' + esc(hist.last_backfill_vn) + "</div>" : "") + "</div>";

    content.innerHTML = h;
    var r = document.getElementById("retry");
    if (r) r.addEventListener("click", function () { r.disabled = true; r.textContent = "Đang tải…"; loadOverview(); });
    var w = document.getElementById("wake");
    if (w && !w.disabled) w.addEventListener("click", function () {
      w.disabled = true; w.textContent = "Đang thử kết nối…";
      wakeClickedAt = new Date().toISOString();
      api("/api/history/wake", { method: "POST" }).catch(function () {
        wakeClickedAt = null; w.disabled = false; w.textContent = "Thử nạp lại ngay";
      });
    });
  }

  function loadOverview() {
    api("/api/overview").then(function (d) {
      offline = null; lastOverview = d; receivedAt = Date.now();
      // Worker chưa phản hồi sau 90 giây → thôi chờ, hiện lại nút
      if (wakeClickedAt && Date.now() - Date.parse(wakeClickedAt) > 90000) wakeClickedAt = null;
      renderOverview(d);
    }).catch(function (e) {
      if (e.message === "auth") return;
      offline = e;
      if (lastOverview) renderOverview(lastOverview);
      else { content.innerHTML = errorBlock(errText(e), "retry"); document.getElementById("retry").addEventListener("click", loadOverview); }
    });
  }

  /* ---------------- S2 Độ phủ ---------------- */
  function vnDateTime(iso) {  // "02/01/2007 20:05" — cùng kiểu dd/mm với ngày, tránh nhầm "02-01" thành 1 tháng 2
    var d = new Date(iso);
    var p = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Ho_Chi_Minh", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(d);
    var v = {}; p.forEach(function (x) { v[x.type] = x.value; });
    return v.day + "/" + v.month + "/" + v.year + " " + v.hour + ":" + v.minute;
  }
  function gapLine(g) {
    if (g.kind === "missing") return esc(vnDateTime(g.from) + " → " + vnDateTime(g.to) + " (giờ VN)");
    var span = g.days > 1 ? vnDate(g.from) + " → " + vnDate(g.last_day) + " (" + g.days + " ngày)" : vnDate(g.from);
    return g.kind === "error" ? '<span class="down">' + esc(span + " — ngày lỗi") + "</span>" : esc(span + " — chưa nạp");
  }

  function sourceWarning(src, provider) {
    var name = provider === "histdata" ? "HistData.com" : "Dukascopy";
    if (src === "unreachable") return '<div class="error warn" role="alert"><p>Kho lịch sử đang tạm dừng — máy chạy GoldPulse không kết nối được ' + name + ". " +
      (provider === "histdata" ? "Cách sửa: kiểm tra mạng Internet của máy đó." : "Cách sửa: bật Cloudflare WARP trên máy đó.") +
      '</p><a class="tap" href="/overview">Xem giờ thử gần nhất ở Tổng quan</a></div>';
    if (src === "throttled") return '<div class="error warn" role="status"><p>' + name + " đang giới hạn tốc độ — kho tải chậm lại, máy tự thử tiếp.</p></div>";
    return "";
  }

  function renderCoverage(d) {
    var warn = sourceWarning(d.source, d.provider);
    var anyData = d.years.some(function (y) { return y.covered_pct; });
    if (!anyData && !d.errors.length) {  // năm có sẵn từ đầu (toàn ngày chờ) — rỗng = chưa ngày nào có nến
      content.innerHTML = warn + '<div class="empty"><p>' + (warn
        ? "Chưa có năm nào được nạp — kho đang chờ nguồn dữ liệu."
        : "Chưa có năm nào được nạp — máy đang bắt đầu tải từ 01/01/2004.") +
        '</p><a href="/overview">Về Tổng quan</a></div>';
      return;
    }
    var t = d.total, h = warn;
    var pass = t.covered_pct != null && t.covered_pct >= 99;
    var pending = d.years.reduce(function (a, y) { return a + y.pending_days; }, 0);
    var cls = t.covered_pct == null || pending ? "" : pass ? "pass" : "fail";  // đang nạp → chưa phán
    h += '<div class="card ' + cls + '"><div class="label">Tổng phút có nến · 2004 → nay · cần ≥ 99%</div>' +
      '<div class="value num">' + pct(t.covered_pct) + "</div>" +
      (pending ? '<div class="muted">Đang nạp — còn ' + fmt(pending) + " ngày chờ tải, số này tăng dần</div>" : "") +
      '<div class="muted">Phút có giao dịch thật: ' + pct(t.traded_pct) + " — năm cũ dữ liệu thưa hơn</div></div>";

    h += "<h2>Ngày lỗi (" + d.errors.length + ")</h2>";
    if (!d.errors.length) h += '<p class="muted">Không có ngày nào lỗi.</p>';
    d.errors.forEach(function (e) {
      h += '<div class="errday" data-day="' + esc(e.day) + '"><div><div class="num">' + vnDate(e.day) +
        ' <span class="badge err state">Lỗi</span></div>' +
        '<div class="muted why">' + esc(e.error || "Lỗi không rõ") + " · thử " + e.attempts + " lần</div></div>" +
        '<button class="btn retry" type="button">Chạy lại</button></div>';
    });

    h += "<h2>Theo năm</h2>";
    // Năm chưa bắt đầu nạp liên tiếp gộp một dòng — trên điện thoại, 18 dòng "0%" đẩy năm đang nạp xuống 4 màn
    var waiting = [];
    function flushWaiting() {
      if (!waiting.length) return;
      var first = waiting[waiting.length - 1], last = waiting[0];
      var days = waiting.reduce(function (a, y) { return a + y.days; }, 0);
      h += '<div class="year"><div class="top"><strong class="num">' + (first.year === last.year ? first.year : first.year + "–" + last.year) +
        '</strong><span class="muted num">Chờ nạp · ' + fmt(days) + " ngày</span></div></div>";
      waiting = [];
    }
    d.years.forEach(function (y) {
      if (y.pending_days && !y.done_days && !y.covered_pct) { waiting.push(y); return; }
      flushWaiting();
      var loading = y.pending_days > 0;
      var hasGap = y.gaps.length > 0;
      var status = !loading ? "Có giao dịch thật: " + pct(y.traded_pct)
        : y.done_days ? "Đang nạp " + fmt(y.done_days) + "/" + fmt(y.days) + " ngày" : "Chờ nạp · " + fmt(y.days) + " ngày";
      h += '<div class="year"><div class="top"><strong class="num">' + y.year + "</strong>" +
        '<span class="num">' + pct(y.covered_pct) + " có nến</span></div>" +
        '<div class="bar' + (hasGap || loading ? " part" : "") + '"><i style="width:' + (y.covered_pct || 0) + '%"></i></div>' +
        '<div class="top mt"><span class="muted num">' + status + "</span>" +
        (hasGap ? '<button class="toggle" type="button" aria-expanded="false">' + y.gaps.length + " đoạn thiếu ›</button>" : '<span class="muted">Không thiếu</span>') +
        "</div>" + (hasGap ? '<div class="gaps">' + y.gaps.map(gapLine).join("<br>") + "</div>" : "") + "</div>";
    });
    flushWaiting();
    content.innerHTML = h;
  }

  function loadCoverage() {
    api("/api/coverage").then(renderCoverage).catch(function (e) {
      if (e.message === "auth") return;
      content.innerHTML = errorBlock(errText(e), "retry");
      document.getElementById("retry").addEventListener("click", function () {
        content.innerHTML = '<div class="skel total"></div><div class="skel yearrow"></div><div class="skel yearrow"></div>';
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
        tg.textContent = tg.textContent.replace(open ? "›" : "⌄", open ? "⌄" : "›");
        return;
      }
      var b = ev.target.closest(".retry");
      if (!b || b.disabled) return;
      var row = b.closest(".errday");
      var badge = row.querySelector(".state");
      b.disabled = true; b.textContent = "Đang gửi…";
      function queued(msg) {
        b.textContent = "Đang chờ";
        badge.className = "badge warn state"; badge.textContent = "Đang chờ";
        row.querySelector(".why").textContent = msg;
      }
      api("/api/crawl-days/" + row.dataset.day + "/retry", { method: "POST" })
        .then(function () { queued("Đã đưa về hàng chờ — máy sẽ tải lại ngày này"); })
        .catch(function (e) {
          if (e.message === "auth") return;
          if (e.status === 404) { queued("Ngày này đã được nạp lại hoặc đang chờ"); return; }
          b.disabled = false; b.textContent = "Chạy lại";
          row.querySelector(".why").textContent = "Không gửi được — " + errText(e);
        });
    });
  }

  /* ---------------- S3 Tra thử ---------------- */
  var SESS = { asia: "Á", europe: "Âu", us: "Mỹ" };
  var lastQuery = null;  // bộ lọc của kết quả đang hiện
  var DOW = ["", "thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu"];

  function querySummary(q) {
    return "Phiên " + SESS[q.session] + " · " + q.year_from + " → " + q.year_to + " · " +
      (q.dow ? DOW[q.dow] : "mọi thứ trong tuần") + " · tra lúc " + clock();
  }

  // [2010, 2011, 2012, 2020] → "2010–2012, 2020"
  function yearRanges(ys) {
    var out = [], a = null, b = null;
    ys.forEach(function (y) {
      if (a !== null && y === b + 1) { b = y; return; }
      if (a !== null) out.push(a === b ? a : a + "–" + b);
      a = b = y;
    });
    if (a !== null) out.push(a === b ? a : a + "–" + b);
    return out.join(", ");
  }

  function revealResult() {
    // Máy nhỏ: kết quả nằm dưới mép màn → cuộn tới, kẻo tưởng bấm chưa ăn
    var rect = content.getBoundingClientRect();
    if (rect.top > window.innerHeight * 0.6) content.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderQuery(r, q) {
    if (!r.count) {
      content.innerHTML = '<div class="empty"><p>Chưa có phiên nào trong khoảng này — các năm đã chọn chưa được nạp vào kho.</p>' +
        '<a href="/coverage">Xem độ phủ</a></div>' +
        '<p class="muted num">' + esc(querySummary(q)) + " · tra mất " + fmt(r.duration_ms, 0) + " ms</p>";
      return;
    }
    content.innerHTML = '<div class="grid g2">' +
      '<div class="card"><div class="label">Số phiên</div><div class="value num">' + fmt(r.count) + "</div></div>" +
      '<div class="card"><div class="label">Biên độ TB</div><div class="value num">' + fmt(r.avg_range, 2) + " USD</div></div>" +
      '<div class="card ' + (r.duration_ms < 1000 ? "pass" : "fail") + '"><div class="label">Thời gian tra</div><div class="value num">' + fmt(r.duration_ms, 0) + " ms</div></div>" +
      '<div class="card"><div class="label">Phiên tăng</div><div class="value num up">' + pct(r.up_pct) + "</div></div>" +
      '<div class="card"><div class="label">Phiên giảm</div><div class="value num down">' + pct(r.down_pct) + "</div></div>" +
      '<div class="card"><div class="label">Đứng giá</div><div class="value num">' + pct(r.flat_pct) + "</div></div></div>" +
      '<p class="muted num">' + esc(querySummary(q)) + " · dữ liệu " + vnDate(r.first_day) + " → " + vnDate(r.last_day) + "</p>" +
      (r.missing_years.length ? '<div class="error warn" role="status"><p>Kết quả chỉ gồm năm đã có trong kho — <strong>thiếu ' +
        esc(yearRanges(r.missing_years)) + '</strong> (chưa nạp). Số trung bình chưa đại diện cho cả khoảng.</p>' +
        '<a href="/coverage">Xem độ phủ</a></div>' : "");
    lastQuery = q;
  }

  if (screen === "s3") {
    var form = document.getElementById("queryForm");
    var run = document.getElementById("run");
    var formErr = document.getElementById("formErr");
    var yearFields = document.querySelectorAll(".year-field");
    var busy = false;
    function qsOf(q) {
      return "session=" + q.session + "&year_from=" + q.year_from + "&year_to=" + q.year_to + (q.dow ? "&dow=" + q.dow : "");
    }
    form.addEventListener("change", function () {
      // Người vội đổi bộ lọc sau khi tra: số đang hiện là của bộ lọc CŨ — nói rõ, đừng để đọc nhầm
      if (!lastQuery || busy || document.getElementById("staleNote")) return;
      content.classList.add("stale");
      content.insertAdjacentHTML("afterbegin", '<p class="notice" id="staleNote" role="status">Bộ lọc đã đổi — số bên dưới là của lần tra trước. Bấm Tra để cập nhật.</p>');
    });
    var params = new URLSearchParams(location.search);
    if (params.get("session")) {
      ["session", "year_from", "year_to", "dow"].forEach(function (k) { if (params.has(k)) form[k].value = params.get(k); });
      setTimeout(function () { form.requestSubmit(); }, 0);
    }
    function markYears(bad) { yearFields.forEach(function (f) { f.classList.toggle("bad", bad); }); }
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
        markYears(true);
        formErr.textContent = "Năm bắt đầu phải trước hoặc bằng năm kết thúc."; formErr.hidden = false;
        return;
      }
      markYears(false); formErr.hidden = true;
      content.classList.remove("stale");
      history.replaceState(null, "", "/query?" + qsOf(q));  // quay lại / tải lại vẫn giữ bộ lọc
      busy = true; run.disabled = true; run.textContent = "Đang tra…";
      content.innerHTML = '<div class="grid g2"><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div><div class="skel"></div></div>';
      api("/api/query?" + qsOf(q))
        .then(function (r) { renderQuery(r, q); revealResult(); })
        .catch(function (e) {
          if (e.message === "auth") return;
          content.innerHTML = '<div class="error" role="alert"><p>Tra không thành công — ' + esc(errText(e)) +
            ' Bộ lọc vẫn giữ nguyên.</p><button class="btn" type="button" id="retry">Thử lại</button></div>';
          revealResult();
          document.getElementById("retry").addEventListener("click", function () { form.requestSubmit(); });
        })
        .then(function () { busy = false; run.disabled = false; run.textContent = "Tra"; });
    });
  }

  /* ---------------- khởi động ---------------- */
  if (screen === "s1") {
    loadOverview();
    setInterval(function () { if (!document.hidden) loadOverview(); }, 5000);
    setInterval(tickPrice, 1000);
  } else if (screen === "s2") {
    loadCoverage();
  }
})();
