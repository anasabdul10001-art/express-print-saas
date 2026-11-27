// Dashboard chart: sales or sales margin as a curve, selectable period, euro axis with
// grid lines.
// Needs API_URL, authHeaders(), logout() (dashboard.html) and t()/getLang() (i18n.js).
(function () {
    const STORAGE_KEY = "dashboard_chart_period";
    const METRIC_KEY = "dashboard_chart_metric";
    // Longer ranges are summed per month, otherwise the days sit too close to read.
    const MAX_DAILY_POINTS = 92;

    const select = document.getElementById("chart-period");
    const metricSelect = document.getElementById("chart-metric");
    const customBox = document.getElementById("chart-custom");
    const fromInput = document.getElementById("chart-from");
    const toInput = document.getElementById("chart-to");
    const totals = document.getElementById("chart-totals");
    const container = document.getElementById("profit-chart");

    let history = null;
    let monthly = false;
    let requestNo = 0;

    const pad = n => String(n).padStart(2, "0");
    const iso = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const parse = s => new Date(s + "T00:00:00");
    const locale = () => getLang() === "de" ? "de-DE" : "en-US";

    // "Today" as the server counts it (sales are grouped by the Berlin calendar day).
    function today() {
        return parse(new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin" }).format(new Date()));
    }

    // First day of the range "the last N months up to end" (26.11. and 1 month -> 27.10.).
    function monthsBack(end, months) {
        const first = new Date(end.getFullYear(), end.getMonth() - months, 1);
        const lastDay = new Date(first.getFullYear(), first.getMonth() + 1, 0).getDate();
        first.setDate(Math.min(end.getDate(), lastDay));
        first.setDate(first.getDate() + 1);
        return first;
    }

    function selectedRange() {
        const value = select.value;
        const end = today();
        if (value === "custom") {
            if (!fromInput.value || !toInput.value) return null;
            const [a, b] = [fromInput.value, toInput.value].sort();
            const days = Math.round((parse(b) - parse(a)) / 86400000) + 1;
            return { start: a, end: b, monthly: days > MAX_DAILY_POINTS };
        }
        const unit = value.slice(-1);
        const count = Number(value.slice(0, -1));
        if (unit === "d") {
            const start = new Date(end);
            start.setDate(start.getDate() - count + 1);
            return { start: iso(start), end: iso(end), monthly: false };
        }
        if (count >= 6) {
            // Whole calendar months, the running one included.
            const start = new Date(end.getFullYear(), end.getMonth() - count + 1, 1);
            return { start: iso(start), end: iso(end), monthly: true };
        }
        return { start: iso(monthsBack(end, count)), end: iso(end), monthly: false };
    }

    function showError() {
        history = null;
        totals.textContent = "";
        container.innerHTML = `<p class="text-sm text-slate-500">${t("dashboard.chartError")}</p>`;
    }

    async function load() {
        const range = selectedRange();
        if (!range) return;
        const mine = ++requestNo;
        const query = `start=${range.start}&end=${range.end}&group=${range.monthly ? "month" : "day"}`;
        try {
            const response = await fetch(`${API_URL}/orders/profit-history?${query}`, { headers: authHeaders() });
            if (response.status === 401) { logout(); return; }
            const data = await response.json();
            if (mine !== requestNo) return; // a newer choice is already on its way
            if (!response.ok) { showError(); return; }
            history = data;
            monthly = range.monthly;
            render();
        } catch (err) {
            if (mine === requestNo) showError();
        }
    }

    const money = (value, digits = 2) => new Intl.NumberFormat(locale(), {
        style: "currency", currency: "EUR", minimumFractionDigits: digits, maximumFractionDigits: digits,
    }).format(value);

    // The axis is compressed: the higher the amount, the closer the euros sit together,
    // so small days stay readable next to a very strong one.
    const scale = v => Math.sign(v) * Math.log10(1 + Math.abs(v) / 5);

    // 0, 5, 10, 20, 50, 100, 200, 500, 1000, ... up to the first step that covers max.
    function steps(max) {
        const list = [0];
        if (max <= 0) return list;
        let step = 5;
        while (true) {
            list.push(step);
            if (step >= max) return list;
            // 5 -> 10 -> 20 -> 50 -> 100 -> 200 -> 500 ...
            step = String(step)[0] === "2" ? step * 2.5 : step * 2;
        }
    }

    function render() {
        if (!history || !history.length) return;
        const metric = metricSelect.value; // "revenue" or "profit"
        // Blue for sales, red for the margin, so the two are told apart at a glance.
        const color = metric === "revenue"
            ? { line: "#2563eb", fill: "#dbeafe" }
            : { line: "#dc2626", fill: "#fee2e2" };
        const values = history.map(p => Number(p[metric]));
        const sum = key => history.reduce((s, p) => s + Number(p[key]), 0);
        totals.textContent = `${t("dashboard.chartRevenue")}: ${money(sum("revenue"))}  ·  `
            + `${t("dashboard.chartMargin")}: ${money(sum("profit"))}  ·  `
            + `${t("dashboard.chartOrders")}: ${sum("orders")}`
            + (monthly ? `  ·  ${t("dashboard.chartPerMonth")}` : "");

        const width = Math.max(container.clientWidth, 260);
        const height = 300;
        const margin = { top: 12, right: 24, bottom: 26, left: 64 };
        const plotW = width - margin.left - margin.right;
        const plotH = height - margin.top - margin.bottom;

        const positive = steps(Math.max(...values, 10));
        const hi = scale(positive[positive.length - 1]);
        // Below zero the axis ends just under the lowest value, not at the next full step:
        // a loss of a few cents must not take up a whole empty band.
        const lowest = Math.min(...values, 0);
        const lo = lowest < 0 ? scale(lowest) - hi * 0.04 : 0;
        const negative = steps(-lowest).slice(1).map(v => -v).filter(v => scale(v) >= lo);
        const ticks = negative.reverse().concat(positive);
        const yOf = v => margin.top + plotH - ((scale(v) - lo) / (hi - lo)) * plotH;

        // Grid lines from the top down; a label that would touch its neighbour is left out
        // (0 and the topmost step always stay).
        const zeroY = yOf(0);
        let lastY = -Infinity;
        const grid = ticks.slice().reverse().map((v, i) => {
            const y = yOf(v);
            if (v !== 0 && i !== 0 && (y - lastY < 14 || Math.abs(y - zeroY) < 14)) return "";
            lastY = y;
            return `<line x1="${margin.left}" y1="${y.toFixed(1)}" x2="${width - margin.right}" y2="${y.toFixed(1)}" stroke="${v === 0 ? "#94a3b8" : "#e2e8f0"}" stroke-width="1"/>
                <text x="${margin.left - 8}" y="${(y + 4).toFixed(1)}" font-size="11" fill="#64748b" text-anchor="end">${money(v, 0)}</text>`;
        }).join("");

        const stepX = history.length > 1 ? plotW / (history.length - 1) : 0;
        const points = history.map((p, i) => ({
            ...p,
            x: history.length > 1 ? margin.left + i * stepX : margin.left + plotW / 2,
            y: yOf(values[i]),
        }));
        const line = points.map((c, i) => `${i === 0 ? "M" : "L"} ${c.x.toFixed(1)} ${c.y.toFixed(1)}`).join(" ");
        const area = `${line} L ${points[points.length - 1].x.toFixed(1)} ${zeroY.toFixed(1)} L ${points[0].x.toFixed(1)} ${zeroY.toFixed(1)} Z`;

        const dateText = point => parse(point.date).toLocaleDateString(locale(), monthly
            ? { month: "2-digit", year: "2-digit" }
            : { day: "2-digit", month: "2-digit" });

        // Date labels are counted back from the newest point, so the last one is always
        // there and none of them overlap.
        const every = Math.max(1, Math.ceil(history.length / Math.max(2, Math.floor(plotW / 62))));
        const labels = points.map((c, i) => (points.length - 1 - i) % every !== 0 ? "" :
            `<text x="${c.x.toFixed(1)}" y="${height - 6}" font-size="11" fill="#94a3b8" text-anchor="middle">${dateText(c)}</text>`
        ).join("");

        const radius = history.length > 45 ? 2 : 3.5;
        const dots = points.map(c => `<circle cx="${c.x.toFixed(1)}" cy="${c.y.toFixed(1)}" r="${radius}" fill="${color.line}"/>`).join("");

        container.innerHTML = `
            <svg viewBox="0 0 ${width} ${height}" width="100%" style="display:block;touch-action:pan-y">
                ${grid}
                <path d="${area}" fill="${color.fill}" fill-opacity="0.7" stroke="none"/>
                <path class="chart-line" d="${line}" fill="none" stroke="${color.line}" stroke-width="2"/>
                <line id="chart-cursor" y1="${margin.top}" y2="${margin.top + plotH}" stroke="#94a3b8" stroke-dasharray="3 3" visibility="hidden"/>
                ${dots}
                ${labels}
            </svg>
            <div id="chart-tip" class="hidden absolute pointer-events-none bg-slate-900 text-white text-xs rounded-lg px-3 py-2 shadow-lg whitespace-nowrap"></div>
        `;

        const svg = container.querySelector("svg");
        const tip = container.querySelector("#chart-tip");
        const cursor = container.querySelector("#chart-cursor");
        const show = event => {
            const box = svg.getBoundingClientRect();
            const x = (event.clientX - box.left) * (width / box.width);
            const c = points.reduce((best, p) => Math.abs(p.x - x) < Math.abs(best.x - x) ? p : best);
            cursor.setAttribute("x1", c.x);
            cursor.setAttribute("x2", c.x);
            cursor.setAttribute("visibility", "visible");
            const title = parse(c.date).toLocaleDateString(locale(), monthly
                ? { month: "long", year: "numeric" }
                : { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric" });
            tip.innerHTML = `<div class="font-semibold">${title}</div>
                <div>${t("dashboard.chartRevenue")}: ${money(Number(c.revenue))}</div>
                <div>${t("dashboard.chartMargin")}: ${money(Number(c.profit))}</div>
                <div>${t("dashboard.chartOrders")}: ${c.orders}</div>`;
            tip.classList.remove("hidden");
            const px = c.x * (box.width / width);
            const left = px + 12 + tip.offsetWidth > box.width ? px - 12 - tip.offsetWidth : px + 12;
            tip.style.left = `${Math.max(0, left)}px`;
            tip.style.top = "8px";
        };
        svg.addEventListener("pointermove", show);
        svg.addEventListener("pointerdown", show);
        svg.addEventListener("pointerleave", () => {
            tip.classList.add("hidden");
            cursor.setAttribute("visibility", "hidden");
        });
    }

    function applySelection() {
        const custom = select.value === "custom";
        customBox.classList.toggle("hidden", !custom);
        if (custom && !toInput.value) {
            const end = today();
            toInput.value = iso(end);
            fromInput.value = iso(monthsBack(end, 1));
        }
        load();
    }

    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved && [...select.options].some(o => o.value === saved)) select.value = saved;

    if (localStorage.getItem(METRIC_KEY) === "profit") metricSelect.value = "profit";
    metricSelect.addEventListener("change", () => {
        localStorage.setItem(METRIC_KEY, metricSelect.value);
        render(); // both figures are already loaded
    });

    select.addEventListener("change", () => {
        localStorage.setItem(STORAGE_KEY, select.value);
        applySelection();
    });
    fromInput.addEventListener("change", load);
    toInput.addEventListener("change", load);

    let resizeTimer = null;
    window.addEventListener("resize", () => {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(render, 150);
    });
    document.addEventListener("langchange", render);
    document.addEventListener("datachanged", load);

    applySelection();
})();
