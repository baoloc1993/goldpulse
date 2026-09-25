/* GoldPulse (v2) — S5 Chat · S6 Dự đoán · S7 Lịch FED. JS thuần, không thư viện ngoài.
   Câu trả lời hiện MỘT LẦN trọn vẹn (không chữ chạy): gửi → poll 2s tới khi hết "pending". */
(function () {
  "use strict";
  var screen = document.body.dataset.screen;
  var DISCLAIMER_SEP = "\n\n---\n";
  var SUGGESTIONS = [
    "Vàng dự kiến tăng hay giảm, t có nên mua vào ko",
    "FED sắp tới hạ lãi suất, ko biết lúc đó thị trường thay đổi thế nào",
    "giá hiện tại đã là đáy/đỉnh chưa"
  ];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(n, d) {
    if (n == null) return "—";
    return Number(n).toLocaleString("en-US", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function signed(n, d) { return n == null ? "—" : (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n), d); }
  function vnDate(iso) { var p = iso.slice(0, 10).split("-"); return p[2] + "/" + p[1] + "/" + p[0]; }
  function vnTime(iso) {
    return new Date(iso).toLocaleTimeString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", hour: "2-digit", minute: "2-digit", hour12: false });
  }
  // Markdown nhẹ: escape trước, rồi chỉ **đậm** — không bao giờ chèn HTML từ nội dung bot
  function md(s) { return esc(s).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>"); }

  function api(path, opts) {
    var o = Object.assign({ credentials: "same-origin", headers: { Accept: "application/json" } }, opts || {});
    if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
    return fetch(path, o).then(function (r) {
      if (r.status === 401) {
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
  function errText(e) {
    if (e.status >= 500) return "Máy chủ GoldPulse đang gặp lỗi — thử lại sau ít phút.";
    if (e.status) return e.message;
    return "Mất kết nối tới máy chủ — kiểm tra mạng, hoặc máy chạy GoldPulse có đang bật không.";
  }
  function errorBlock(msg, retryId, label) {
    return '<div class="error" role="alert"><p>' + esc(msg) + '</p><button class="btn" type="button" id="' + retryId + '">' + (label || "Thử lại") + "</button></div>";
  }

  /* ================= S5 Chat ================= */
  if (screen === "s5") {
    var head = document.getElementById("head");
    var brainBadge = document.getElementById("brainBadge");
    var brainDown = document.getElementById("brainDown");
    var log = document.getElementById("chatlog");
    var form = document.getElementById("sendForm");
    var ask = document.getElementById("ask");
    var send = document.getElementById("send");
    var sendErr = document.getElementById("sendErr");
    var threadId = null;
    var polling = {};     // message_id → true khi đang poll
    var waiting = 0;      // số câu đang chờ bot — nút Gửi khoá khi > 0
    var brainState = null;
    var headData = null, headAt = 0;
    var sending = false;  // POST đang bay — chặn chạm đúp / Ctrl+Enter ngay sau Gửi (dogfood rushed)
    var prevThread = null;
    var wrap = document.querySelector(".wrap");
    var tabbar = document.querySelector(".tabbar");
    var DRAFT = "gp_chat_draft";
    function saveDraft() { try { sessionStorage.setItem(DRAFT, ask.value); } catch (e) { /* riêng tư */ } }
    function loadDraft() { try { return sessionStorage.getItem(DRAFT) || ""; } catch (e) { return ""; } }
    // Đệm dưới = đúng chiều cao thanh gửi + thanh tab — câu trả lời mới luôn nằm ngay trên ô nhập
    function fitPad() {
      var tb = tabbar && getComputedStyle(tabbar).display !== "none" ? tabbar.offsetHeight : 0;
      wrap.style.paddingBottom = form.offsetHeight + tb + 16 + "px";
    }

    function lagNow() {
      var p = headData && headData.price;
      if (!p || p.lag_seconds == null) return null;
      return Math.max(0, Math.round(p.lag_seconds + (Date.now() - headAt) / 1000));
    }
    function dur(sec) {
      if (sec < 120) return sec + "s";
      if (sec < 7200) return Math.round(sec / 60) + " phút";
      if (sec < 172800) return Math.round(sec / 3600) + " giờ";
      return Math.round(sec / 86400) + " ngày";
    }
    function priceBadge() {
      var p = headData && headData.price;
      if (!p || p.price == null) return '<span class="badge err">Chưa có giá</span>';
      if (!p.market_open) return '<span class="badge warn">Thị trường nghỉ</span>';
      var lag = lagNow();
      if (lag == null || lag >= p.lag_limit) return '<span class="badge err">Trễ ' + (lag == null ? "?" : dur(lag)) + (lag != null && lag >= 120 ? "" : " — chờ nguồn") + "</span>";
      return '<span class="badge ok">Trễ ' + lag + "s</span>";
    }
    function renderHead() {
      var h = headData, p = h.price;
      var sess = h.sessions.length
        ? h.sessions.map(function (s) { return s.label; }).join(" + ")
        : "Không phiên nào";
      var closes = h.sessions.length ? h.sessions[h.sessions.length - 1].label + " đóng " + h.sessions[h.sessions.length - 1].closes_vn : (p.next_open_vn ? "Mở lại " + p.next_open_vn : "");
      var fed = h.fed
        ? '<div class="value num">' + fmt(h.fed.rate_upper, 2) + "%</div><div class=\"muted num\">" + vnDate(h.fed.meeting_date).slice(0, 5) + " · " +
          '<span class="nowrap">' + (h.fed.change_bp < 0 ? "▼ −" + Math.abs(h.fed.change_bp) : h.fed.change_bp > 0 ? "▲ +" + h.fed.change_bp : "— 0") + " bp</span></div>"
        : h.next_meeting
          ? '<div class="value num">Kỳ tới ' + vnDate(h.next_meeting.date).slice(0, 5) + '</div><div class="muted">còn ' + h.next_meeting.days + " ngày · chưa có kết quả kỳ trước</div>"
          : '<div class="value">Chưa có</div><div class="muted">' + (h.worker_alive ? "Đang nạp lịch FED" : "Máy nạp dữ liệu đang dừng") + "</div>";
      head.innerHTML =
        '<div class="card"><div class="label">XAUUSD <span class="nowrap">· gold-api</span></div><div class="value num">' + (p.price == null ? "—" : fmt(p.price, 2)) + '</div><div id="lagBadge">' + priceBadge() + "</div></div>" +
        '<div class="card"><div class="label">Phiên</div><div class="value">' + esc(sess) + '</div><div class="muted">' + esc(closes) + "</div></div>" +
        '<a class="card" href="/fed"><div class="label">FED <span class="wide">gần nhất </span>›</div>' + fed + "</a>";
      var b = h.brain;
      var name = b.backend === "fake" ? "chế độ thử (câu mẫu, không phải Claude)" : (b.backend || "").toUpperCase() + (b.model ? " · " + b.model : "");
      var brainErr = document.getElementById("brainErr");
      brainErr.hidden = b.state !== "error"; brainErr.textContent = b.state === "error" ? "Lỗi gần nhất: " + (b.last_error || "") : "";
      brainState = b.state;
      if (b.state === "down") {
        brainBadge.className = "badge err"; brainBadge.textContent = "Não bot chưa chạy — chạy make brain";
        brainDown.hidden = false;
        brainDown.innerHTML = '<div class="error"><p>Não bot chưa chạy — trên máy chủ chạy <strong>make brain</strong>. Bạn vẫn gửi được câu hỏi, bot sẽ trả lời khi não chạy lại.</p></div>';
      } else {
        brainDown.hidden = true; brainDown.innerHTML = "";
        if (b.state === "error") {
          brainBadge.className = "badge err";
          brainBadge.textContent = "Não bot: " + name + " · lỗi lần cuối " + (b.last_error_at ? vnTime(b.last_error_at) : "");
          brainBadge.title = b.last_error || "";
        } else {
          brainBadge.className = "badge ok"; brainBadge.textContent = "Não bot: " + name + " · đang chạy"; brainBadge.title = "";
        }
      }
      refreshWaitNotes();
    }
    function loadHead() {
      return api("/api/chat/header").then(function (h) { headData = h; headAt = Date.now(); renderHead(); })
        .catch(function (e) {
          if (e.message === "auth") return;
          brainBadge.className = "badge err"; brainBadge.textContent = e.status ? "Không tải được trạng thái" : "Mất kết nối — đang thử lại";
        });
    }

    function botHtml(m) {
      var body = m.content, disc = "";
      var i = body.lastIndexOf(DISCLAIMER_SEP);
      if (i >= 0) { disc = body.slice(i + DISCLAIMER_SEP.length); body = body.slice(0, i); }
      var note = "";
      if (m.prediction) {
        var p = m.prediction;
        note = '<div class="note">Đã ghi dự đoán ' + (p.direction === "up" ? '<span class="up">▲ tăng</span>' : '<span class="down">▼ giảm</span>') +
          " · chấm lúc " + esc(p.due_vn) + ' · <a href="/predictions">xem sổ ›</a></div>';
      }
      return md(body) + note + (disc ? '<div class="disclaimer">' + esc(disc) + "</div>" : "");
    }
    function waitNote(m) {
      if (m._offline) return "Mất kết nối — đang thử nối lại… (câu hỏi đã lưu trên máy chủ)";
      var sec = m.waited_seconds || 0, mins = Math.floor(sec / 60);
      if (brainState === "down") return "Đang chờ não bot…" + (mins ? " (xếp hàng " + mins + " phút)" : "");
      var t = sec >= 10 ? " " + (mins ? mins + " phút " + (sec % 60) + "s" : sec + "s") : "";
      if ((m.attempts || 0) > 1) return "Đang thử lại lần " + m.attempts + "/3…" + t;
      if (headData && headData.pending_jobs > 1 && sec >= 5) return "Đang xếp hàng sau câu khác…" + t;
      return "Đang nghĩ…" + t;
    }
    function nodeFor(m) {
      var el = document.createElement("div");
      el.dataset.id = m.id;
      if (m.role === "user") {
        el.className = "msg user"; el.textContent = m.content;
      } else if (m.status === "pending") {
        el.className = "msg bot skel"; el.setAttribute("aria-busy", "true"); el.textContent = waitNote(m); el._msg = m;
      } else if (m.status === "error") {
        el.className = "error"; el.setAttribute("role", "alert");
        el.innerHTML = "<p>" + esc(m.content || "Não bot không trả lời được.") + " Câu hỏi của bạn vẫn giữ nguyên.</p>" +
          '<button class="btn" type="button">Gửi lại</button>';
        el.querySelector("button").addEventListener("click", function (ev) { resend(m.id, ev.target); });
      } else {
        el.className = "msg bot"; el.innerHTML = botHtml(m);
      }
      return el;
    }
    function refreshWaitNotes() {
      Array.prototype.forEach.call(log.querySelectorAll(".msg.bot.skel"), function (el) { if (el._msg) el.textContent = waitNote(el._msg); });
    }
    function put(m) {
      var old = log.querySelector('[data-id="' + m.id + '"]');
      var el = nodeFor(m);
      if (old) log.replaceChild(el, old); else log.appendChild(el);
      if (m.role === "assistant" && m.status === "pending") poll(m.id);
      return el;
    }
    function setWaiting(delta) {
      waiting = Math.max(0, waiting + delta);
      send.disabled = waiting > 0;
      send.textContent = waiting > 0 ? "Đang chờ…" : "Gửi";
    }
    function scrollEnd() { fitPad(); window.scrollTo(0, document.body.scrollHeight); }

    function poll(id) {
      if (polling[id]) return;
      polling[id] = true; setWaiting(1);
      var fails = 0;
      (function tick() {
        setTimeout(function () {
          api("/api/chat/messages/" + id).then(function (m) {
            fails = 0;
            var el = log.querySelector('[data-id="' + id + '"]');
            if (m.status === "pending") {
              if (el) { el._msg = m; el.textContent = waitNote(m); }
              return tick();
            }
            if (!polling[id]) return;  // đã bấm "Chat mới" — bỏ theo dõi
            delete polling[id]; setWaiting(-1);
            // Câu trả lời thuộc đoạn chat đang xem mới chèn — không lọt sang "Chat mới"
            if (el) { put(m); scrollEnd(); }
          }).catch(function (e) {
            if (e.message === "auth") return;
            // Mất mạng chốc lát: poll tiếp, câu trả lời vẫn ở máy chủ — không đánh rơi, nhưng nói ra
            fails += 1;
            var el2 = log.querySelector('[data-id="' + id + '"]');
            if (el2 && el2._msg && fails >= 2) { el2._msg._offline = true; el2.textContent = waitNote(el2._msg); }
            tick();
          });
        }, fails ? Math.min(10000, 2000 * (fails + 1)) : 2000);
      })();
    }

    function renderEmpty() {
      log.innerHTML = '<div class="empty"><p>Hỏi mình về vàng — mình trả lời bằng 20 năm nến phút, lịch FED và giá hiện tại.</p><div class="suggest">' +
        SUGGESTIONS.map(function (s, i) { return '<button type="button" data-i="' + i + '">' + esc(s) + "</button>"; }).join("") + "</div>" +
        (prevThread ? '<p class="muted mt">Đã mở đoạn chat mới · <a href="/chat?thread_id=' + prevThread + '">Quay lại đoạn trước</a></p>' : "") + "</div>";
      Array.prototype.forEach.call(log.querySelectorAll(".suggest button"), function (b) {
        b.addEventListener("click", function () { submit(SUGGESTIONS[+b.dataset.i]); });
      });
    }
    function loadThread() {
      var want = new URLSearchParams(location.search).get("thread_id");
      return api("/api/chat/thread" + (want ? "?thread_id=" + encodeURIComponent(want) : "")).then(function (t) {
        threadId = t.thread_id;
        log.innerHTML = "";
        if (!t.messages.length) return renderEmpty();
        t.messages.forEach(put);
        scrollEnd();
      }).catch(function (e) {
        if (e.message === "auth") return;
        log.innerHTML = errorBlock("Không tải được đoạn chat — " + errText(e), "reloadThread");
        head.innerHTML = "";
        document.getElementById("reloadThread").addEventListener("click", function () { loadHead(); loadThread(); });
      });
    }

    function submit(text) {
      text = (text || "").trim();
      sendErr.hidden = true;
      if (!text) { sendErr.textContent = "Gõ câu hỏi trước khi gửi."; sendErr.hidden = false; ask.focus(); return; }
      if (waiting > 0 || sending) return;
      sending = true;
      send.disabled = true; send.textContent = "Đang gửi…";
      Array.prototype.forEach.call(log.querySelectorAll(".suggest button"), function (b) { b.disabled = true; });
      api("/api/chat/messages", { method: "POST", body: { content: text, thread_id: threadId } })
        .then(function (r) {
          threadId = r.thread_id;
          var empty = log.querySelector(".empty"); if (empty) log.innerHTML = "";
          if (ask.value.trim() === text) { ask.value = ""; grow(); saveDraft(); }
          put(r.question); put(r.answer); scrollEnd();
        })
        .catch(function (e) {
          if (e.message === "auth") return;
          // Không gửi được: giữ nguyên câu đã gõ trong ô
          if (!ask.value.trim()) { ask.value = text; grow(); }
          sendErr.textContent = "Chưa gửi được — " + errText(e); sendErr.hidden = false;
          Array.prototype.forEach.call(log.querySelectorAll(".suggest button"), function (b) { b.disabled = false; });
        })
        .then(function () { sending = false; if (!waiting) { send.disabled = false; send.textContent = "Gửi"; } });
    }
    function resend(id, btn) {
      btn.disabled = true; btn.textContent = "Đang gửi…";
      api("/api/chat/messages/" + id + "/resend", { method: "POST" }).then(put).catch(function (e) {
        if (e.message === "auth") return;
        btn.disabled = false; btn.textContent = "Gửi lại";
        sendErr.textContent = "Chưa gửi lại được — " + errText(e); sendErr.hidden = false;
      });
    }
    function grow() { ask.style.height = "auto"; ask.style.height = Math.min(ask.scrollHeight + 2, 108) + "px"; fitPad(); }

    form.addEventListener("submit", function (ev) { ev.preventDefault(); submit(ask.value); });
    var askCount = document.getElementById("askCount");
    ask.addEventListener("input", function () {
      grow(); sendErr.hidden = true; saveDraft();
      var n = ask.value.length;
      askCount.hidden = n < 1800;
      askCount.textContent = n + "/2000 ký tự" + (n >= 2000 ? " — không gõ thêm được" : "");
    });
    // Điện thoại: Enter = xuống dòng, bấm Gửi mới gửi (DESIGN-SYSTEM C13). Bàn phím rời: Ctrl/⌘ + Enter gửi
    ask.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submit(ask.value); }
    });
    // Màn thấp + đang gõ (bàn phím ảo mở): giấu thanh tab, thanh gửi xuống sát đáy — chừa chỗ cho hội thoại
    ask.addEventListener("focus", function () { if (innerHeight < 560) { document.body.classList.add("typing"); fitPad(); } });
    ask.addEventListener("blur", function () { document.body.classList.remove("typing"); fitPad(); });
    window.addEventListener("resize", fitPad);
    document.getElementById("newChat").addEventListener("click", function (ev) {
      var b = ev.target; b.disabled = true;
      var old = threadId;
      api("/api/chat/threads", { method: "POST" }).then(function (r) {
        if (old && old !== r.thread_id) prevThread = old;
        threadId = r.thread_id; renderEmpty(); ask.focus();
        if (location.search) history.replaceState(null, "", "/chat");
        // Câu đang chờ của đoạn cũ vẫn được trả lời trên máy chủ; không khoá nút Gửi của đoạn mới
        polling = {}; waiting = 0; setWaiting(0);
      })
        .catch(function (e) { if (e.message !== "auth") { sendErr.textContent = errText(e); sendErr.hidden = false; } })
        .then(function () { b.disabled = false; });
    });

    ask.value = loadDraft(); if (ask.value) grow();
    fitPad();
    loadHead().then(loadThread);
    setInterval(function () { if (!document.hidden) loadHead(); }, 5000);
    setInterval(function () {
      var el = document.getElementById("lagBadge"); if (el && headData) el.innerHTML = priceBadge();
    }, 1000);
  }

  /* ================= S6 Dự đoán ================= */
  if (screen === "s6") {
    var content = document.getElementById("content");
    var SIG = { bollinger: "Bollinger", streak: "Chuỗi nến", rsi9: "RSI 9" };
    var OUT = {
      correct: ["ok", "Đúng"], wrong: ["err", "Sai"], flat: ["muted", "Đứng giá"], unscored: ["muted", "Không chấm được"]
    };
    var lastId = null;
    var CONF = { low: "thấp", medium: "vừa", high: "cao" };

    function rateText(a) { return a.scored ? fmt(a.rate, 1) + "%" : "—"; }
    function rowHtml(r) {
      var out = r.outcome === "pending" ? ["warn", "Chấm lúc " + r.due_vn] : OUT[r.outcome];
      var sig = r.signals || {};
      var fired = (sig.fired || []).map(function (f) { return SIG[f.name] || f.name; }).join(", ") || "không";
      return '<button class="prow" type="button" aria-expanded="false"><div class="top">' +
        '<span class="num">' + esc(r.made_vn) + " · " + (r.source === "auto" ? "tự động" : "chat") + "</span>" +
        (r.direction === "up" ? '<span class="up">▲ tăng</span>' : '<span class="down">▼ giảm</span>') +
        '<span class="num">' + fmt(r.price_at, 2) + " → " + (r.price_due == null ? "…" : fmt(r.price_due, 2)) + "</span>" +
        '<span class="badge ' + out[0] + '">' + esc(out[1]) + "</span></div>" +
        '<div class="more">' + esc(r.reasoning || "(không có lý do)") + "\nTín hiệu bật: " + esc(fired) +
        " · %b " + esc(sig.pct_b) + " · chuỗi " + esc(sig.streak) + " · RSI 9 " + esc(sig.rsi9) +
        "\nĐộ tin cậy: " + esc(CONF[r.confidence] || r.confidence) + " · não: " + esc((r.backend || "") + (r.model ? " · " + r.model : "")) + "</div></button>";
    }
    function bindRows(root) {
      Array.prototype.forEach.call(root.querySelectorAll(".prow:not([data-b])"), function (el) {
        el.dataset.b = "1";
        el.addEventListener("click", function () { var o = el.classList.toggle("open"); el.setAttribute("aria-expanded", o ? "true" : "false"); });
      });
    }
    function sigText(sig) {
      if (!sig) return "chưa có (máy nạp dữ liệu chưa tính)";
      var f = sig.fired.map(function (x) { return SIG[x.name] || x.name; }).join(", ");
      return sig.bar_vn + " · " + (f || "không tín hiệu");
    }
    function render(d) {
      var s = d.summary;
      if (!s.total) {
        var lv = d.live, why = lv.can_suggest ? "" : lv.no_suggest_reason === "market_closed"
          ? " Hiện thị trường đang nghỉ nên bot tạm không dự đoán."
          : " Hiện giá realtime đang trễ nên bot tạm không dự đoán.";
        content.innerHTML = '<div class="empty"><p>Chưa có dự đoán nào — bot chỉ dự đoán khi thị trường mở, giá realtime tươi, và có tín hiệu 15 phút hoặc bạn hỏi "tăng hay giảm".' +
          esc(why) + " Nến 15 phút gần nhất: " + esc(sigText(d.signal)) + '.</p><a href="/chat">Sang Chat hỏi thử</a></div>';
        return;
      }
      var cls = s.scored < s.min_scored ? "card" : s.rate >= s.go ? "card pass" : s.rate < s.kill ? "card fail" : "card";
      var note = s.scored < s.min_scored
        ? s.scored + " / " + s.min_scored + " lần đã chấm · chưa đủ mẫu để kết luận"
        : s.scored + " lần đã chấm · " + (s.verdict === "go" ? "đạt ngưỡng GO" : s.verdict === "kill" ? "dưới 50% — KILL" : "50–55% — PIVOT");
      var bs = s.by_source;
      content.innerHTML =
        '<div class="' + cls + '"><div class="label">Tỉ lệ đúng · cần ≥ ' + s.go + "% khi đủ " + s.min_scored + ' lần</div><div class="value num">' + rateText(s) +
        '</div><div class="muted num">' + esc(note) + " · " + s.correct + " đúng · " + s.wrong + " sai" + (s.pending ? " · " + s.pending + " chờ chấm" : "") + "</div></div>" +
        '<div class="grid g2">' +
        '<div class="card"><div class="label">Tự động (có tín hiệu)</div><div class="value num">' + bs.auto.scored + " · " + rateText(bs.auto) + "</div></div>" +
        '<div class="card"><div class="label">Từ chat</div><div class="value num">' + bs.chat.scored + " · " + rateText(bs.chat) + "</div></div>" +
        '<div class="card"><div class="label">Đứng giá / không chấm</div><div class="value num">' + s.flat + " / " + s.unscored + "</div>" +
        (d.skipped_errors ? '<div class="muted">' + d.skipped_errors + " lần bỏ qua vì lỗi não</div>" : "") + "</div>" +
        '<div class="card"><div class="label">Tín hiệu gần nhất</div><div class="value small num">' + esc(sigText(d.signal)) + "</div></div>" +
        "</div>" +
        '<div class="muted mt">Theo hướng: ▲ tăng ' + s.by_direction.up.scored + " lần · " + rateText(s.by_direction.up) +
        " — ▼ giảm " + s.by_direction.down.scored + " lần · " + rateText(s.by_direction.down) + "</div>" +
        '<h2>Sổ dự đoán</h2><div id="plist">' + d.rows.map(rowHtml).join("") + "</div>" +
        (d.rows.length === 50 ? '<button class="btn line block" type="button" id="more">Tải thêm</button>' : "");
      lastId = d.rows.length ? d.rows[d.rows.length - 1].id : null;
      bindRows(content);
      var more = document.getElementById("more");
      if (more) more.addEventListener("click", function () {
        more.disabled = true; more.textContent = "Đang tải…";
        api("/api/predictions/rows?before=" + lastId).then(function (r) {
          var list = document.getElementById("plist");
          list.insertAdjacentHTML("beforeend", r.rows.map(rowHtml).join(""));
          bindRows(list);
          if (r.rows.length) lastId = r.rows[r.rows.length - 1].id;
          if (r.rows.length < 50) more.remove(); else { more.disabled = false; more.textContent = "Tải thêm"; }
        }).catch(function (e) {
          if (e.message === "auth") return;
          more.disabled = false; more.textContent = "Tải thêm — thử lại";
        });
      });
    }
    function load() {
      api("/api/predictions").then(render).catch(function (e) {
        if (e.message === "auth") return;
        content.innerHTML = errorBlock("Không tải được sổ dự đoán — " + errText(e), "retry");
        document.getElementById("retry").addEventListener("click", load);
      });
    }
    load();
    // Tự tải lại khi có hàng chờ chấm — nhưng không phá hàng đang mở hoặc trang đã "Tải thêm"
    setInterval(function () {
      if (document.hidden || content.querySelector(".prow.open") || document.querySelectorAll(".prow").length > 50) return;
      if (content.querySelector(".badge.warn")) load();
    }, 60000);
  }

  /* ================= S7 Lịch FED ================= */
  if (screen === "s7") {
    var box = document.getElementById("content");
    function bp(n) {
      if (n > 0) return '<span class="down">▲ +' + n + " bp</span>";   // tăng lãi suất: đỏ (DESIGN-SYSTEM C12)
      if (n < 0) return '<span class="up">▼ −' + Math.abs(n) + " bp</span>";
      return '<span class="muted">— giữ</span>';
    }
    function react(label, v) {
      return "<div><div class=\"muted\">" + label + '</div><div class="num ' + (v > 0 ? "up" : v < 0 ? "down" : "") + '">' +
        (v == null ? "—" : signed(v, 2)) + "</div></div>";
    }
    function rowHtml(r) {
      var rate = r.rate_lower === r.rate_upper ? fmt(r.rate_upper, 2) + "%" : fmt(r.rate_lower, 2) + "–" + fmt(r.rate_upper, 2) + "%";
      return '<button class="prow" type="button" aria-expanded="false"><div class="top"><span class="num">' + vnDate(r.meeting_date) + "</span>" +
        (r.kind === "unscheduled" ? '<span class="badge warn">đột xuất</span>' : "") +
        '<span class="num">' + rate + "</span>" + bp(r.change_bp) + "</div>" +
        '<div class="more">Vàng sau công bố (USD, so với giá ' + (r.price_at == null ? "—" : fmt(r.price_at, 2)) + " lúc " + vnTime(r.announced_at) + " giờ VN):" +
        '<div class="react">' + react("+15 phút", r.d15m) + react("+1 giờ", r.d1h) + react("+1 ngày", r.d1d) + "</div></div></button>";
    }
    function render(d) {
      var src = d.source;
      if (!d.rows.length) {
        var nx0 = d.next_meeting;
        var nextRow = nx0 ? '<div class="prow next"><div class="top"><span>Kỳ họp kế tiếp</span><span class="num">' + vnDate(nx0.date) + " · còn " + nx0.days + " ngày</span></div></div>" : "";
        box.innerHTML = nextRow + (src.last_error
          ? errorBlock("Chưa nạp được lịch FED từ FRED — " + src.last_error + ". Máy nạp dữ liệu sẽ tự thử lại mỗi ngày.", "retry")
          : d.worker_alive
            ? '<div class="empty"><p>Đang nạp lịch FED từ FRED — trang tự cập nhật khi xong.</p><a href="/chat">Về Chat</a></div>'
            : '<div class="empty"><p>Chưa có lịch FED — máy nạp dữ liệu đang dừng. Trên máy chủ bật lại GoldPulse (make dev, hoặc bản production) rồi quay lại.</p><a href="/chat">Về Chat</a></div>');
        var rb = document.getElementById("retry"); if (rb) rb.addEventListener("click", load);
        if (d.worker_alive && !src.last_error) setTimeout(load, 10000);
        return;
      }
      var nx = d.next_meeting;
      var badge = src.last_error
        ? '<span class="badge warn">Chưa nạp lại được từ FRED — dùng bản ngày ' + (src.loaded_at ? vnDate(src.loaded_at) : "trước") + "</span>"
        : '<span class="badge ok">FRED tới được</span>';
      box.innerHTML =
        (nx ? '<div class="prow next"><div class="top"><span>Kỳ họp kế tiếp</span><span class="num">' + vnDate(nx.date) + " · còn " + nx.days + " ngày</span></div></div>" : "") +
        '<div class="card"><div class="label">Nguồn</div><div class="value">FRED + lịch FOMC</div><div class="muted">' +
        (src.loaded_at ? "Nạp lúc " + vnTime(src.loaded_at) + " " + vnDate(src.loaded_at) + " · " : "") + d.rows.length + " kỳ họp " +
        d.rows[d.rows.length - 1].meeting_date.slice(0, 4) + " → nay · " + badge + "</div></div>" +
        "<h2>Kỳ họp (mới nhất trước)</h2>" + d.rows.map(rowHtml).join("");
      Array.prototype.forEach.call(box.querySelectorAll(".prow:not(.next)"), function (el) {
        el.addEventListener("click", function () { var o = el.classList.toggle("open"); el.setAttribute("aria-expanded", o ? "true" : "false"); });
      });
    }
    function load() {
      api("/api/fed").then(render).catch(function (e) {
        if (e.message === "auth") return;
        box.innerHTML = errorBlock("Không tải được lịch FED — " + errText(e), "retry");
        document.getElementById("retry").addEventListener("click", load);
      });
    }
    load();
  }
})();
