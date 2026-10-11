// Progressive enhancement for the confirmation form. The server re-validates
// everything; without JavaScript the form stays fully usable.
"use strict";
document.addEventListener("DOMContentLoaded", function () {
  var notify = document.getElementById("notify-box");
  var resend = document.getElementById("resend-box");
  if (notify !== null && resend !== null) {
    // "Re-send" depends on "notify": ticking re-send enables notify, and
    // unticking notify clears re-send.
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
  }

  var confirmField = document.getElementById("confirm-word");
  var migrateButton = document.getElementById("migrate-button");
  if (confirmField !== null && migrateButton !== null) {
    // The button unlocks only once the confirmation word is typed.
    var sync = function () {
      migrateButton.disabled = confirmField.value.trim() !== "migrate";
    };
    confirmField.addEventListener("input", sync);
    sync();
  }
});
