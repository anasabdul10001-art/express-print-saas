// Keeps the open page current: asks the server to read eBay (and Shopify) when the
// page opens, when the seller comes back to the tab and every minute and a half
// while it is visible. After a run the page reloads its data, but never while the
// seller is typing; the reload then waits for the next quiet moment.
(function () {
    const INTERVAL = 90000;
    const MIN_GAP = 30000;
    let reload = null;
    let busy = false;
    let lastStart = 0;
    let pending = false;

    function typing() {
        const el = document.activeElement;
        if (el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName)) return true;
        // Reloading the product table would drop the rows ticked for a bulk change.
        return Boolean(document.querySelector(".row-check:checked"));
    }

    function flush() {
        if (!pending || !reload || typing()) return;
        pending = false;
        reload();
    }

    async function post(path) {
        const response = await fetch(`${API_URL}/${path}`, { method: "POST", headers: authHeaders() });
        return response.ok ? response.json() : null;
    }

    async function sync() {
        if (busy || document.hidden || Date.now() - lastStart < MIN_GAP) return;
        busy = true;
        lastStart = Date.now();
        try {
            const ebay = await post("ebay/live-sync");
            if (ebay && ebay.ran) pending = true;
            const shopify = await post("shopify/sync-orders");
            if (shopify && shopify.created > 0) pending = true;
        } catch (err) {
            // Offline or the server is waking up: the next round tries again.
        } finally {
            busy = false;
            flush();
        }
    }

    window.startAutoSync = function (reloadPage) {
        reload = reloadPage;
        sync();
        setInterval(sync, INTERVAL);
        setInterval(flush, 5000);
        document.addEventListener("visibilitychange", sync);
    };
})();
