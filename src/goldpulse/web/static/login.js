/* Nhập khoá — tách khỏi HTML để CSP chặn được mọi script inline. */
(function () {
  "use strict";
  document.getElementById("showKey").addEventListener("change", function (e) {
    document.getElementById("key").type = e.target.checked ? "text" : "password";
  });
  document.getElementById("loginForm").addEventListener("submit", function () {
    var b = document.getElementById("enter"); b.disabled = true; b.textContent = "Đang vào…";
  });
})();
