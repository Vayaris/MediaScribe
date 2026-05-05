(function () {
  const root = document.documentElement;
  const storedTheme = localStorage.getItem("mediascribe-theme") || "light";

  function applyTheme(theme) {
    root.dataset.theme = theme;
    localStorage.setItem("mediascribe-theme", theme);
    document.querySelectorAll("[data-theme-label]").forEach((node) => {
      node.textContent = theme === "dark" ? "Clair" : "Sombre";
    });
  }

  applyTheme(storedTheme);

  window.toggleTheme = function () {
    applyTheme(root.dataset.theme === "dark" ? "light" : "dark");
  };

  window.copyTranscript = async function () {
    const el = document.getElementById("transcript");
    if (!el) return;
    const text = el.value || el.textContent || "";
    await navigator.clipboard.writeText(text);
    const confirmation = document.querySelector("[data-copy-confirm]");
    if (confirmation) {
      confirmation.classList.add("visible");
      setTimeout(() => confirmation.classList.remove("visible"), 1600);
    }
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-drop-zone]").forEach((zone) => {
      const input = zone.querySelector("input[type='file']");
      const fileName = zone.querySelector("[data-file-name]");
      if (!input) return;

      ["dragenter", "dragover"].forEach((eventName) => {
        zone.addEventListener(eventName, () => zone.classList.add("is-dragover"));
      });

      ["dragleave", "drop"].forEach((eventName) => {
        zone.addEventListener(eventName, () => zone.classList.remove("is-dragover"));
      });

      input.addEventListener("change", () => {
        if (fileName) {
          fileName.textContent = input.files && input.files[0] ? input.files[0].name : "";
        }
      });
    });
  });
})();
