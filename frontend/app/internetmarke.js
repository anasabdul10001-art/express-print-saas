// Deutsche Post INTERNETMARKE: Portokasse settings and "buy a stamp" on the orders page.
// Uses the page's API_URL, authHeaders() and logout(). Nothing is shown until the
// server has its API credentials.
const Internetmarke = (() => {
    const TERMS_URL = "https://www.internetmarke.de/agb.html";
    let status = null;
    let purchases = [];

    const esc = (value) => String(value ?? "").replace(/[&<>"']/g,
        (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    const price = (cents) => (cents / 100).toLocaleString("de-DE", { style: "currency", currency: "EUR" });

    const cm = (mm) => (mm / 10).toLocaleString("de-DE");
    const range = (min, max) => (min ? `${cm(min)}–${cm(max)} cm` : `bis ${cm(max)} cm`);

    // Length, width, height and weight, exactly as Deutsche Post's list limits them.
    function limits(p) {
        const parts = [`Länge ${range(p.min_length_mm, p.max_length_mm)}`,
            `Breite ${range(p.min_width_mm, p.max_width_mm)}`, `Höhe ${range(0, p.max_height_mm)}`];
        if (p.max_weight_g) {
            parts.push(p.min_weight_g > 1 ? `Gewicht ${p.min_weight_g}–${p.max_weight_g} g` : `Gewicht bis ${p.max_weight_g} g`);
        }
        return parts.join(" · ");
    }

    function productRow(p, checked) {
        const tags = [p.tracking ? "mit Sendungsnummer/Nachweis laut Liste (T&T)" : "",
            p.contract_required ? "nur mit Vertrag bei der Deutschen Post" : ""].filter(Boolean).join(" · ");
        return `
            <label data-row data-search="${esc(p.name.toLowerCase())}" class="flex gap-2 p-2 border border-slate-300 rounded cursor-pointer">
                <input type="radio" name="im-product" value="${p.code}" class="mt-1" ${checked ? "checked" : ""}>
                <span class="flex-1">
                    <span class="flex justify-between gap-3"><span class="font-semibold">${esc(p.name)}</span><b class="whitespace-nowrap">${price(p.price_cents)}</b></span>
                    <span class="block text-xs text-slate-600">${esc(limits(p))}</span>
                    ${p.note ? `<span class="block text-xs text-slate-500">${esc(p.note)}</span>` : ""}
                    ${tags ? `<span class="block text-xs text-amber-700">${esc(tags)}</span>` : ""}
                </span>
            </label>`;
    }

    async function api(path, options = {}) {
        const response = await fetch(`${API_URL}/internetmarke${path}`, {
            ...options,
            headers: { ...authHeaders(), ...(options.body ? { "Content-Type": "application/json" } : {}) },
        });
        if (response.status === 401) { logout(); throw new Error("Bitte neu anmelden."); }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : `Fehler ${response.status}`);
        return data;
    }

    async function downloadPdf(purchaseId) {
        const response = await fetch(`${API_URL}/internetmarke/purchases/${purchaseId}/label`, { headers: authHeaders() });
        if (!response.ok) throw new Error("Das PDF konnte nicht geladen werden.");
        const link = document.createElement("a");
        link.href = URL.createObjectURL(await response.blob());
        link.download = "internetmarke.pdf";
        link.click();
        URL.revokeObjectURL(link.href);
    }

    // ---- orders page ----

    async function loadOrdersData() {
        try {
            status = await api("/status");
            if (status.configured && status.connected) purchases = await api("/purchases");
        } catch (e) { status = null; }
        return !!(status && status.configured && status.connected);
    }

    // The purchase that matters for an order: one that cost money, or might have.
    const purchaseFor = (orderId) => purchases.find((p) => p.order_id === orderId && p.status !== "FAILED");

    function button(order) {
        if (!status || !status.configured || !status.connected) return "";
        const bought = purchaseFor(order.id);
        const css = "ml-2 bg-white hover:bg-slate-50 border border-slate-300 text-slate-700 text-xs font-semibold py-1 px-3 rounded transition";
        const country = order.recipient_address?.country_code || "";
        const make = (action, id, text) =>
            `<button type="button" class="${css}" data-im="${action}" data-id="${esc(id)}" data-country="${esc(country)}">${text}</button>`;
        if (bought && bought.status === "BOUGHT") {
            if (bought.has_label) return make("pdf", bought.id, "Briefmarke als PDF");
            return bought.can_fetch_label ? make("fetch", bought.id, "Etikett erneut laden (bezahlt)")
                : '<span class="ml-2 text-xs text-slate-500">Briefmarke gekauft (PDF gelöscht)</span>';
        }
        if (bought) {
            return `<span class="ml-2 text-xs font-semibold text-red-700">Kauf unklar – Portokasse prüfen (Nr. ${esc(bought.shop_order_id)})</span>`
                + make("release", bought.id, "Nicht abgebucht");
        }
        return order.status === "shipped" || order.status === "canceled" ? "" : make("buy", order.id, "Briefmarke kaufen");
    }

    function openBuyDialog(orderId, country, done) {
        const abroad = !!country && country.toUpperCase() !== "DE";
        const products = status.products.filter((p) => p.international === abroad);
        if (!products.length) { showBanner("Zurzeit ist kein Porto-Produkt hinterlegt.", true); return; }
        const dialog = document.createElement("dialog");
        dialog.className = "rounded-xl p-0 w-full max-w-lg shadow-xl";
        dialog.innerHTML = `
            <div class="p-5 space-y-3 text-sm">
                <h3 class="text-lg font-bold">Briefmarke kaufen (INTERNETMARKE)</h3>
                <p class="text-slate-500">Bezahlt wird aus deiner eigenen Portokasse.</p>
                <p class="font-semibold">Porto – ${abroad ? "Ausland" : "Deutschland"} (${products.length} Produkte)</p>
                <input type="search" data-search-box placeholder="Suchen, z. B. Großbrief oder Einschreiben" class="w-full p-2 border border-slate-300 rounded">
                <div class="space-y-2 overflow-y-auto pr-1" style="max-height:40vh" role="radiogroup">
                    ${products.map((p, index) => productRow(p, !abroad && index === 0)).join("")}
                </div>
                ${abroad ? '<p class="text-xs text-amber-700">Waren in Länder außerhalb der EU brauchen zusätzlich eine Zollerklärung. Diese erstellt NettoDesk nicht.</p>' : ""}
                <label class="flex gap-2"><input type="checkbox" data-terms>
                    <span>Ich akzeptiere die <a class="underline" href="${TERMS_URL}" target="_blank" rel="noopener">AGB INTERNETMARKE</a> der Deutschen Post.</span></label>
                <p data-error class="text-red-700" aria-live="polite"></p>
                <div class="flex justify-end gap-2">
                    <button type="button" data-cancel class="border border-slate-300 rounded px-4 py-2">Abbrechen</button>
                    <button type="button" data-confirm class="bg-slate-900 text-white font-semibold rounded px-4 py-2 disabled:opacity-50" disabled></button>
                </div>
            </div>`;
        document.body.appendChild(dialog);
        const terms = dialog.querySelector("[data-terms]");
        const confirmButton = dialog.querySelector("[data-confirm]");
        const radios = [...dialog.querySelectorAll('input[name="im-product"]')];
        const chosen = () => {
            const radio = radios.find((r) => r.checked);
            return radio && products.find((p) => String(p.code) === radio.value);
        };
        const refresh = () => {
            confirmButton.textContent = chosen() ? `Kostenpflichtig kaufen: ${price(chosen().price_cents)}` : "Bitte Porto wählen";
            confirmButton.disabled = !terms.checked || !chosen();
        };
        dialog.querySelector("[data-search-box]").addEventListener("input", (event) => {
            const words = event.target.value.toLowerCase().split(/\s+/).filter(Boolean);
            dialog.querySelectorAll("[data-row]").forEach((row) => {
                row.hidden = !words.every((word) => row.dataset.search.includes(word));
                row.style.display = row.hidden ? "none" : "";
            });
        });
        let busy = false;
        radios.forEach((radio) => radio.addEventListener("change", refresh));
        terms.addEventListener("change", refresh);
        dialog.querySelector("[data-cancel]").addEventListener("click", () => dialog.close());
        dialog.addEventListener("cancel", (event) => { if (busy) event.preventDefault(); });
        dialog.addEventListener("close", () => dialog.remove());
        confirmButton.addEventListener("click", async () => {
            busy = true;
            confirmButton.disabled = true;
            dialog.querySelector("[data-cancel]").disabled = true;
            confirmButton.textContent = "Wird gekauft …";
            try {
                const result = await api(`/orders/${orderId}/buy`, {
                    method: "POST",
                    body: JSON.stringify({ product_code: chosen().code, price_cents: chosen().price_cents, accept_terms: terms.checked }),
                });
                dialog.close();
                showBanner(result.message, !result.has_label);
                if (result.has_label && !result.print_job_id) await downloadPdf(result.id);
            } catch (e) {
                // No second click from this dialog: the page reload shows what really happened.
                dialog.querySelector("[data-error]").textContent = e.message;
                confirmButton.textContent = "Nicht erfolgreich";
                dialog.querySelector("[data-cancel]").disabled = false;
                dialog.querySelector("[data-cancel]").textContent = "Schließen";
                busy = false;
            }
            done();
        });
        refresh();
        dialog.showModal();
    }

    function watchOrders(container, reload) {
        const refresh = async () => { await loadOrdersData(); reload(); };
        container.addEventListener("click", async (event) => {
            const target = event.target.closest("[data-im]");
            if (!target) return;
            const { im, id } = target.dataset;
            try {
                if (im === "buy") { openBuyDialog(id, target.dataset.country, refresh); return; }
                if (im === "pdf") { await downloadPdf(id); return; }
                if (im === "fetch") {
                    const result = await api(`/purchases/${id}/fetch-label`, { method: "POST" });
                    showBanner(result.message, !result.has_label);
                }
                if (im === "release") {
                    if (!confirm("Hast du in der Portokasse geprüft, dass für diese Nummer NICHTS abgebucht wurde? Sonst bezahlst du doppelt.")) return;
                    await api(`/purchases/${id}/not-charged`, { method: "POST" });
                }
                await refresh();
            } catch (e) { showBanner(e.message, true); }
        });
    }

    // ---- settings page ----

    async function mountSettings(box) {
        try { status = await api("/status"); } catch (e) { return; }
        if (!status.configured) return;
        box.classList.remove("hidden");
        const input = (name, label, value = "", type = "text", max = 50) => `
            <label class="block">${label}
                <input name="${name}" type="${type}" maxlength="${max}" value="${esc(value)}" autocomplete="${type === "password" ? "new-password" : "off"}"
                       class="mt-1 w-full p-2 border border-slate-300 rounded"></label>`;
        const sender = status.sender || {};
        box.innerHTML = `
            <h3 class="text-lg font-bold">Deutsche Post – Briefmarken (INTERNETMARKE)</h3>
            <p class="text-slate-500">${status.connected ? "Deine Portokasse ist verbunden."
                : "Verbinde deine eigene Portokasse. Gib NettoDesk dort unter „Meine Daten > Geschäftsanwendungen“ frei."}</p>
            <form class="space-y-3" data-connect>
                <div class="grid sm:grid-cols-2 gap-3">
                    ${input("username", "E-Mail der Portokasse", "", "text", 100)}
                    ${input("password", "Passwort der Portokasse", "", "password", 22)}
                    ${input("sender_name", "Absender: Name / Firma", sender.name)}
                    ${input("sender_street", "Straße und Hausnummer", sender.street)}
                    ${input("sender_zip", "PLZ", sender.zip, "text", 10)}
                    ${input("sender_city", "Ort", sender.city, "text", 35)}
                </div>
                <label class="flex gap-2"><input type="checkbox" name="accept_terms">
                    <span>Ich akzeptiere die <a class="underline" href="${TERMS_URL}" target="_blank" rel="noopener">AGB INTERNETMARKE</a> der Deutschen Post.</span></label>
                <button class="bg-slate-900 text-white font-semibold rounded px-4 py-2 disabled:opacity-50">Portokasse verbinden</button>
            </form>
            <div data-formats class="${status.connected ? "" : "hidden"} space-y-2">
                <button type="button" data-load-formats class="border border-slate-300 rounded px-4 py-2">Druckformat wählen${status.page_format_id !== null ? " (gespeichert)" : ""}</button>
                <select data-format class="hidden w-full p-2 border border-slate-300 rounded"></select>
                <button type="button" data-disconnect class="block text-red-700 underline">Portokasse trennen</button>
            </div>
            <p data-result aria-live="polite"></p>`;
        const result = box.querySelector("[data-result]");
        const form = box.querySelector("[data-connect]");
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const body = Object.fromEntries(new FormData(form));
            body.accept_terms = form.accept_terms.checked;
            const submit = form.querySelector("button");
            submit.disabled = true;
            try {
                await api("/connect", { method: "POST", body: JSON.stringify(body) });
                form.password.value = "";
                await mountSettings(box);
                box.querySelector("[data-result]").textContent = "Portokasse verbunden. Bitte jetzt ein Druckformat wählen.";
            } catch (e) { result.textContent = e.message; submit.disabled = false; }
        });
        const select = box.querySelector("[data-format]");
        box.querySelector("[data-load-formats]").addEventListener("click", async () => {
            try {
                const data = await api("/page-formats");
                select.innerHTML = '<option value="">Bitte wählen …</option>' + data.formats.map((f) =>
                    `<option value="${f.id}" ${f.id === status.page_format_id ? "selected" : ""}>${esc(f.name)}</option>`).join("");
                select.classList.remove("hidden");
            } catch (e) { result.textContent = e.message; }
        });
        select.addEventListener("change", async () => {
            if (!select.value) return;
            try {
                await api("/page-format", { method: "PUT", body: JSON.stringify({ page_format_id: Number(select.value) }) });
                status.page_format_id = Number(select.value);
                result.textContent = "Druckformat gespeichert.";
            } catch (e) { result.textContent = e.message; }
        });
        box.querySelector("[data-disconnect]").addEventListener("click", async () => {
            if (!confirm("Verbindung zur Portokasse trennen?")) return;
            try { await api("/disconnect", { method: "POST" }); await mountSettings(box); }
            catch (e) { result.textContent = e.message; }
        });
    }

    return { loadOrdersData, button, watchOrders, mountSettings };
})();
