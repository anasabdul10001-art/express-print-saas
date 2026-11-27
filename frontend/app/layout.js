// What happened at eBay after a quantity change, as {text, isError}, or null.
window.ebayQuantityNote = function (results) {
    if (!results || !results.length) return null;
    const reason = {
        failed: r => `eBay hat die Menge NICHT übernommen: ${r.error || "unbekannter Fehler"}`,
        ambiguous: () => "mehrere eBay-Angebote gehören dazu, bitte die Menge bei eBay ändern",
        no_listing: () => "kein aktives eBay-Angebot, nur in NettoDesk gespeichert",
    };
    const done = results.filter(r => r.status === "ok").map(r => `bei eBay jetzt ${r.quantity}: ${r.title}`);
    const open = results.filter(r => reason[r.status]).map(r => `${r.title}: ${reason[r.status](r)}`);
    if (results.some(r => r.status === "failed"))
        open.push("Ohne Änderung bei eBay setzt die Synchronisierung wieder die eBay-Menge ein.");
    const text = done.concat(open).join(" | ");
    return text ? { text, isError: results.some(r => r.status === "failed" || r.status === "ambiguous") } : null;
};

// Included on every page under app/. Lets the page content use the whole
// browser width and makes the desktop sidebar collapsible to an icon rail.
// The choice is remembered in this browser.
(function () {
    const KEY = "nav_collapsed";
    const root = document.documentElement;

    function stored() {
        try { return localStorage.getItem(KEY) === "1"; } catch (err) { return false; }
    }

    // Set before the body is painted, so the sidebar does not jump on load.
    root.classList.toggle("nav-collapsed", stored());

    const style = document.createElement("style");
    style.textContent = `
        body main { max-width: none !important; }

        .nav-toggle { display: flex; align-items: center; gap: .75rem; margin: .75rem .75rem 0; padding: .5rem .75rem;
            border-radius: .5rem; font-size: .75rem; font-weight: 600; color: #94a3b8; }
        .nav-toggle:hover { background: #f8fafc; color: #0f172a; }
        .nav-toggle svg { flex-shrink: 0; transition: transform .15s; }

        @media (min-width: 768px) {
            body aside { position: sticky; top: 0; height: 100vh; align-self: flex-start; }

            .nav-collapsed body aside { width: 4rem; }
            .nav-collapsed aside > a:first-of-type { justify-content: center; padding: .75rem .25rem; }
            .nav-collapsed aside > a:first-of-type > * { max-width: 3rem; max-height: 3rem; object-fit: contain; }
            .nav-collapsed aside > a:first-of-type > img[src$="nettodesk-logo.svg"] { content: url("../assets/nettodesk-icon.svg"); height: 2rem !important; width: 2rem !important; }
            .nav-collapsed aside nav { padding-left: .5rem; padding-right: .5rem; overflow-x: hidden; }
            .nav-collapsed aside nav a { justify-content: center; padding-left: 0; padding-right: 0; }
            .nav-collapsed aside nav a > span,
            .nav-collapsed aside .nav-toggle > span,
            .nav-collapsed aside #sidebar-user-name,
            .nav-collapsed aside #sidebar-user-email,
            .nav-collapsed aside button[onclick="logout()"] > span { display: none; }
            .nav-collapsed aside > a:not(:first-of-type) { justify-content: center; margin-left: .25rem; margin-right: .25rem; padding-left: 0; padding-right: 0; gap: 0; }
            .nav-collapsed aside > a:not(:first-of-type) > span.min-w-0 { display: none; }
            .nav-collapsed aside button[onclick="logout()"] { justify-content: center; padding-left: 0; padding-right: 0; }
            .nav-collapsed aside .nav-toggle { justify-content: center; margin-left: .5rem; margin-right: .5rem; padding-left: 0; padding-right: 0; }
            .nav-collapsed aside .nav-toggle svg { transform: rotate(180deg); }
        }
    `;
    document.head.appendChild(style);

    document.addEventListener("DOMContentLoaded", () => {
        const aside = document.querySelector("body aside");
        const nav = aside && aside.querySelector("nav");
        if (!nav) return;

        const button = document.createElement("button");
        button.type = "button";
        button.className = "nav-toggle";
        button.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m11 17-5-5 5-5"/><path d="m18 17-5-5 5-5"/></svg><span></span>';
        aside.insertBefore(button, nav);

        function render() {
            const collapsed = root.classList.contains("nav-collapsed");
            const english = root.lang === "en";
            const label = collapsed
                ? (english ? "Expand menu" : "Menü ausklappen")
                : (english ? "Collapse menu" : "Menü einklappen");
            button.querySelector("span").textContent = label;
            button.title = label;
            button.setAttribute("aria-label", label);
            button.setAttribute("aria-expanded", String(!collapsed));
        }

        button.addEventListener("click", () => {
            const collapsed = root.classList.toggle("nav-collapsed");
            try { localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (err) { /* private mode */ }
            render();
        });

        // Icon-only entries show their name as a tooltip. Read at hover time,
        // so it follows the language switch and entries added later (Admin).
        aside.addEventListener("mouseover", (event) => {
            const item = event.target.closest('nav a, a[href="settings.html"], button[onclick="logout()"]');
            if (!item) return;
            if (root.classList.contains("nav-collapsed")) item.title = item.textContent.replace(/\s+/g, " ").trim();
            else item.removeAttribute("title");
        });

        new MutationObserver(render).observe(root, { attributes: true, attributeFilter: ["lang"] });
        render();
    });
})();
