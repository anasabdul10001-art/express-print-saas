// Shows how many days are left of the free trial or of the paid billing period.
// Fills every element marked with data-days-left; it stays hidden when there is nothing to count.
(function () {
    const apiUrl = ["localhost", "127.0.0.1"].includes(location.hostname) ? location.origin : "https://express-print-saas.onrender.com";
    let tenant = null;

    function message() {
        if (!tenant) return null;
        const trial = tenant.trial_days_left;
        const period = tenant.period_days_left;
        let key, days, end;
        if (period > 0) {
            [key, days, end] = ["daysLeft.period", period, tenant.current_period_end];
        } else if (trial > 0) {
            [key, days, end] = ["daysLeft.trial", trial, tenant.trial_ends_at];
        } else {
            return null;
        }
        const date = new Date(end).toLocaleDateString(window.getLang() === "en" ? "en-GB" : "de-DE");
        return t(days === 1 ? key + "One" : key).replace("{days}", days).replace("{date}", date);
    }

    function render() {
        const text = message();
        document.querySelectorAll("[data-days-left]").forEach((box) => {
            box.classList.toggle("hidden", !text);
            (box.querySelector("[data-days-left-text]") || box).textContent = text || "";
        });
    }

    async function load() {
        const token = localStorage.getItem("access_token");
        if (!token || !document.querySelector("[data-days-left]")) return;
        try {
            const response = await fetch(`${apiUrl}/tenants/me`, { headers: { "Authorization": `Bearer ${token}` } });
            if (!response.ok) return;
            tenant = await response.json();
            render();
        } catch (err) {
            // only a hint - the page works without it
        }
    }

    document.addEventListener("langchange", render);
    document.addEventListener("DOMContentLoaded", load);
})();
