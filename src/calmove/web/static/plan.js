// "Re-send" depends on "notify": ticking re-send enables notify, and
// unticking notify clears re-send. The server enforces the same rule.
"use strict";
document.addEventListener("DOMContentLoaded", function () {
  var notify = document.getElementById("notify-box");
  var resend = document.getElementById("resend-box");
  if (notify === null || resend === null) {
    return;
  }
  resend.addEventListener("change", function () {
    if (resend.checked) {
      notify.checked = true;
    }
  });
  notify.addEventListener("change", function () {
    if (!notify.checked) {
      resend.checked = false;
    }
  });
});
