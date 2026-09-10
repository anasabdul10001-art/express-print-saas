// Language detection + switching. Loaded after translations.js.
//
// Default is auto-detected from the browser (navigator.language) on first
// visit; a manual choice via the DE/EN switcher overrides that and is
// remembered in localStorage from then on, per device/browser (there's no
// account-level language preference - each browser picks for itself).
(function () {
    const SUPPORTED = ["de", "en"];

    function detectLang() {
        const saved = localStorage.getItem("lang");
        if (saved && SUPPORTED.includes(saved)) return saved;
        const browserLang = (navigator.language || "de").slice(0, 2).toLowerCase();
        return SUPPORTED.includes(browserLang) ? browserLang : "de";
    }

    let currentLang = detectLang();

    function t(key) {
        const dict = window.TRANSLATIONS || {};
        const table = dict[currentLang] || dict.de || {};
        return table[key] || key;
    }
    window.t = t;
    window.getLang = function () {
        return currentLang;
    };

    function applyTranslations() {
        document.documentElement.lang = currentLang;

        document.querySelectorAll("[data-i18n]").forEach((el) => {
            el.textContent = t(el.getAttribute("data-i18n"));
        });
        document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
            el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
        });
        document.querySelectorAll("[data-lang-btn]").forEach((btn) => {
            const active = btn.getAttribute("data-lang-btn") === currentLang;
            btn.classList.toggle("text-red-600", active);
            btn.classList.toggle("text-slate-400", !active);
        });

        // Lets a page's own script re-render dynamic content (e.g. the
        // pricing cards) in the new language without a full reload.
        document.dispatchEvent(new CustomEvent("langchange"));
    }
    window.applyTranslations = applyTranslations;

    function setLang(lang) {
        if (!SUPPORTED.includes(lang) || lang === currentLang) return;
        currentLang = lang;
        localStorage.setItem("lang", lang);
        applyTranslations();
    }
    window.setLang = setLang;

    function init() {
        applyTranslations();
        document.querySelectorAll("[data-lang-btn]").forEach((btn) => {
            btn.addEventListener("click", () => setLang(btn.getAttribute("data-lang-btn")));
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
