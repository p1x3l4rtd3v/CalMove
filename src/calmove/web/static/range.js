// Selecting or typing a custom date implies the "Custom" preset.
"use strict";
document.addEventListener("DOMContentLoaded", function () {
  var custom = document.getElementById("preset-custom");
  if (custom === null) {
    return;
  }
  ["range-start", "range-end"].forEach(function (id) {
    var field = document.getElementById(id);
    if (field === null) {
      return;
    }
    ["focus", "input", "change"].forEach(function (eventName) {
      field.addEventListener(eventName, function () {
        custom.checked = true;
      });
    });
  });
});
