/**
 * A one-time notice (not a consent gate) that this site only uses
 * technically-necessary browser storage: a login-session token and a
 * language preference (see localStorage usage in login.html/register.html
 * and i18n.js) - no analytics/tracking/advertising cookies exist anywhere
 * in this app. Under GDPR/ePrivacy (§25 TTDSG in Germany), storage that's
 * strictly necessary for a service the visitor explicitly requested does
 * NOT require opt-in consent, only transparency - hence a dismiss button,
 * not an accept/reject choice with categories.
 *
 * IMPORTANT: if real analytics/advertising scripts are ever added to this
 * site, this file needs to become an actual consent gate (blocking those
 * scripts until the visitor opts in) rather than a one-time notice - this
 * is not legal advice, just documenting the assumption this file makes.
 */
(function () {
    var STORAGE_KEY = "cookie_notice_ack_v1";
    if (localStorage.getItem(STORAGE_KEY)) return;

    var lang = ((localStorage.getItem("lang") || navigator.language || "de") + "").toLowerCase().indexOf("en") === 0 ? "en" : "de";

    var TEXT = {
        de: {
            message: "Diese Website verwendet ausschließlich technisch notwendige Daten in deinem Browser (Login-Sitzung, Spracheinstellung) – keine Tracking- oder Werbe-Cookies.",
            linkText: "Mehr in der Datenschutzerklärung",
            button: "Verstanden",
        },
        en: {
            message: "This website only uses technically necessary browser storage (login session, language preference) – no tracking or advertising cookies.",
            linkText: "More in our privacy policy",
            button: "Got it",
        },
    };
    var t = TEXT[lang];

    var banner = document.createElement("div");
    banner.setAttribute("role", "region");
    banner.setAttribute("aria-label", "Cookie notice");
    banner.style.cssText =
        "position:fixed;left:0;right:0;bottom:0;z-index:9999;background:#0f172a;color:#e2e8f0;" +
        "padding:14px 20px;display:flex;flex-wrap:wrap;align-items:center;justify-content:center;" +
        "gap:12px;font-family:system-ui,-apple-system,sans-serif;font-size:13px;" +
        "box-shadow:0 -2px 10px rgba(0,0,0,0.15);";

    var text = document.createElement("span");
    text.style.cssText = "max-width:640px;line-height:1.5;";
    text.appendChild(document.createTextNode(t.message + " "));

    var link = document.createElement("a");
    link.href = "privacy.html";
    link.textContent = t.linkText;
    link.style.cssText = "color:#fca5a5;text-decoration:underline;white-space:nowrap;";
    text.appendChild(link);

    var button = document.createElement("button");
    button.type = "button";
    button.textContent = t.button;
    button.style.cssText =
        "background:#e11d48;color:#fff;border:none;padding:8px 18px;border-radius:8px;" +
        "font-weight:600;font-size:13px;cursor:pointer;white-space:nowrap;flex-shrink:0;";
    button.onclick = function () {
        localStorage.setItem(STORAGE_KEY, "1");
        banner.remove();
    };

    banner.appendChild(text);
    banner.appendChild(button);

    function mount() {
        document.body.appendChild(banner);
    }
    if (document.body) {
        mount();
    } else {
        document.addEventListener("DOMContentLoaded", mount);
    }
})();
