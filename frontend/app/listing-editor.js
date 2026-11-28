// Editor for one live eBay listing: pictures (whole listing and per colour/size) and
// price + quantity per variation. Uses API_URL, authHeaders, showBanner, loadProducts
// and logout from the page. Nothing reaches eBay before "Speichern" is confirmed.
(function () {
    const MAX_GALLERY = 24, MAX_PER_VARIATION = 12;
    let state = null;

    const esc = v => String(v ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/'/g, "&#39;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
    const el = id => document.getElementById(id);

    function overlay() {
        let box = el("listing-editor");
        if (box) return box;
        box = document.createElement("div");
        box.id = "listing-editor";
        box.className = "fixed inset-0 z-50 bg-black/50 overflow-y-auto p-3 sm:p-6";
        box.innerHTML = `<div class="max-w-4xl mx-auto bg-white rounded-xl shadow-xl p-4 sm:p-6 space-y-5 text-slate-900">
            <div class="flex items-start justify-between gap-3">
                <h3 id="le-title" class="text-lg font-bold">eBay-Angebot bearbeiten</h3>
                <button type="button" data-act="close" class="text-slate-500 hover:text-slate-900 text-sm px-3 py-2">Schließen</button>
            </div>
            <div id="le-body" class="space-y-5"></div>
            <div id="le-error" class="hidden p-3 rounded text-sm bg-red-50 text-red-700 border border-red-200"></div>
            <div id="le-actions" class="hidden flex flex-wrap items-center gap-3">
                <button type="button" id="le-save" data-act="save" class="bg-slate-800 hover:bg-slate-900 text-white font-semibold py-2 px-4 rounded">Speichern + eBay</button>
                <span class="text-xs text-slate-500">Erst beim Speichern wird das Angebot bei eBay geändert.</span>
            </div></div>`;
        box.addEventListener("click", onClick);
        box.addEventListener("change", onChange);
        document.body.appendChild(box);
        return box;
    }

    function showError(text) {
        const box = el("le-error");
        box.innerText = text || "";
        box.classList.toggle("hidden", !text);
    }

    function close() {
        if (state && state.busy) return;
        if (state && state.info && changes().count && !confirm("Änderungen verwerfen?")) return;
        const box = el("listing-editor");
        if (box) box.remove();
        state = null;
    }

    async function openListingEditor(productId) {
        overlay();
        const mine = state = { productId, info: null, busy: false };
        el("le-body").innerHTML = `<p class="text-sm text-slate-500">Angebot wird von eBay geladen …</p>`;
        el("le-actions").classList.add("hidden");
        showError("");
        try {
            const response = await fetch(`${API_URL}/ebay/listing-editor/${productId}`, { headers: authHeaders() });
            if (response.status === 401) { logout(); return; }
            const info = await response.json();
            if (state !== mine) return;
            if (!response.ok) { el("le-body").innerHTML = ""; showError("eBay: " + (info.detail || "Fehler")); return; }
            start(info);
        } catch (err) {
            if (state === mine) { el("le-body").innerHTML = ""; showError("Serverfehler, bitte erneut versuchen."); }
        }
    }

    function start(info) {
        const name = info.picture_name || Object.keys(info.specific_values)[0] || null;
        Object.assign(state, {
            info,
            pictures: [...info.pictures],
            pictureName: name,
            sets: JSON.parse(JSON.stringify(info.picture_sets || {})),
            rows: info.variations.map(v => ({ price: v.price ?? "", quantity: String(v.available) })),
        });
        el("le-title").innerText = `${info.title || "eBay-Angebot"} (${info.item_id})`;
        el("le-actions").classList.remove("hidden");
        render();
    }

    function strip(urls, group, max) {
        const key = esc(group === null ? "" : group);
        const kind = group === null ? "gallery" : "set";
        const button = (act, i, label, title) => `<button type="button" data-act="${act}" data-kind="${kind}" data-group="${key}" data-i="${i}" title="${title}" aria-label="${title}" class="px-2 text-slate-600 hover:text-slate-900">${label}</button>`;
        return `<div class="flex flex-wrap gap-2 items-start">
            ${urls.map((url, i) => `<div class="border border-slate-200 rounded p-1 text-center">
                <img src="${esc(url)}" alt="" class="w-24 h-24 object-contain">
                ${kind === "gallery" && i === 0 ? `<div class="text-[11px] font-semibold text-emerald-700">Hauptbild</div>` : ""}
                <div class="text-sm">${button("left", i, "◀", "Nach vorne")}${button("remove", i, "✕", "Bild entfernen")}${button("right", i, "▶", "Nach hinten")}</div>
            </div>`).join("")}
            ${urls.length < max ? `<label class="cursor-pointer w-24 h-24 flex items-center justify-center text-center text-xs font-semibold bg-slate-100 hover:bg-slate-200 rounded">+ Bilder hochladen
                <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple class="hidden" data-kind="${kind}" data-group="${key}"></label>` : ""}
        </div>`;
    }

    function variationName(v) {
        return Object.entries(v.specifics).map(([k, value]) => `${k}: ${value}`).join(" · ") || "Variante";
    }

    function render() {
        const { info, rows } = state;
        let html = `<section class="space-y-2"><h4 class="font-semibold">Bilder des Angebots (${state.pictures.length}/${MAX_GALLERY})</h4>${strip(state.pictures, null, MAX_GALLERY)}</section>`;
        if (info.variations.length) {
            const names = Object.keys(info.specific_values);
            const locked = Object.keys(info.picture_sets || {}).length > 0 || names.length < 2;
            html += `<section class="space-y-2"><h4 class="font-semibold">Preis und Menge je Variante</h4>
                <div class="overflow-x-auto"><table class="w-full text-sm"><thead><tr class="text-left text-slate-500"><th class="py-1 pr-3">Variante</th><th class="py-1 pr-3">Preis (€)</th><th class="py-1">Verfügbare Menge</th></tr></thead><tbody>
                ${info.variations.map((v, i) => `<tr class="border-t border-slate-200"><td class="py-1 pr-3">${esc(variationName(v))}</td>
                    <td class="py-1 pr-3"><input type="number" min="0.01" step="0.01" data-row="${i}" data-field="price" value="${esc(rows[i].price)}" class="p-1 w-24 rounded border border-slate-300"></td>
                    <td class="py-1"><input type="number" min="0" step="1" data-row="${i}" data-field="quantity" value="${esc(rows[i].quantity)}" class="p-1 w-24 rounded border border-slate-300"></td></tr>`).join("")}
                </tbody></table></div>
                <div class="flex flex-wrap gap-2 items-center text-sm">
                    <input type="number" min="0.01" step="0.01" id="le-all-price" placeholder="Preis" class="p-1 w-24 rounded border border-slate-300">
                    <button type="button" data-act="all-price" class="bg-slate-100 hover:bg-slate-200 py-1 px-2 rounded">Preis für alle</button>
                    <input type="number" min="0" step="1" id="le-all-quantity" placeholder="Menge" class="p-1 w-24 rounded border border-slate-300">
                    <button type="button" data-act="all-quantity" class="bg-slate-100 hover:bg-slate-200 py-1 px-2 rounded">Menge für alle</button>
                </div></section>`;
            if (names.length) {
                html += `<section class="space-y-3"><h4 class="font-semibold">Bilder je Variante</h4>
                    <label class="block text-sm">Bilder richten sich nach:
                        <select id="le-picture-name" ${locked ? "disabled" : ""} class="p-1 rounded border border-slate-300">${names.map(n => `<option value="${esc(n)}" ${n === state.pictureName ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
                    ${(info.specific_values[state.pictureName] || []).map(value => `<div class="space-y-1"><div class="text-sm font-semibold">${esc(value)}</div>${strip(state.sets[value] || [], value, MAX_PER_VARIATION)}</div>`).join("")}
                </section>`;
            }
        } else {
            html += `<p class="text-sm text-slate-500">Dieses Angebot hat keine Varianten. Preis und Menge änderst du mit ✎ in der Produktliste.</p>`;
        }
        el("le-body").innerHTML = html;
    }

    function listFor(kind, group) {
        if (kind === "gallery") return state.pictures;
        return state.sets[group] || (state.sets[group] = []);
    }

    function onClick(event) {
        const target = event.target.closest("[data-act]");
        if (!target || !state) return;
        const act = target.dataset.act;
        if (act === "close") return close();
        if (state.busy || !state.info) return;
        if (act === "save") return save();
        if (act === "all-price" || act === "all-quantity") {
            const field = act === "all-price" ? "price" : "quantity";
            const value = el("le-all-" + field).value;
            if (value === "") return;
            state.rows.forEach(row => row[field] = value);
            return render();
        }
        const list = listFor(target.dataset.kind, target.dataset.group);
        const i = Number(target.dataset.i);
        if (act === "remove") list.splice(i, 1);
        const j = act === "left" ? i - 1 : act === "right" ? i + 1 : -1;
        if (j >= 0 && j < list.length) [list[i], list[j]] = [list[j], list[i]];
        render();
    }

    function onChange(event) {
        if (!state || !state.info) return;
        const input = event.target;
        if (input.dataset.field) state.rows[Number(input.dataset.row)][input.dataset.field] = input.value;
        else if (input.id === "le-picture-name") { state.pictureName = input.value; state.sets = {}; render(); }
        else if (input.type === "file") upload(input);
    }

    // eBay refuses pictures whose longest side is below 500 px.
    async function longestSide(file) {
        const bitmap = await createImageBitmap(file);
        const size = Math.max(bitmap.width, bitmap.height);
        bitmap.close();
        return size;
    }

    async function upload(input) {
        const mine = state;
        const { kind, group } = input.dataset;
        const files = [...input.files];
        const max = kind === "gallery" ? MAX_GALLERY : MAX_PER_VARIATION;
        const problems = [];
        mine.busy = true;
        el("le-save").disabled = true;
        el("le-save").innerText = "Bilder werden hochgeladen …";
        showError("");
        for (const file of files) {
            if (listFor(kind, group).length >= max) { problems.push(`Höchstens ${max} Bilder.`); break; }
            try {
                if (await longestSide(file) < 500) { problems.push(`${file.name}: zu klein (mindestens 500 Pixel an der längsten Seite).`); continue; }
                const form = new FormData();
                form.append("file", file);
                const response = await fetch(`${API_URL}/uploads/product-image`, { method: "POST", headers: authHeaders(), body: form });
                if (response.status === 401) { logout(); return; }
                const data = await response.json();
                if (!response.ok) { problems.push(`${file.name}: ${data.detail || "Upload fehlgeschlagen."}`); continue; }
                listFor(kind, group).push(data.image_url);
            } catch (err) {
                problems.push(`${file.name}: Upload fehlgeschlagen.`);
            }
        }
        mine.busy = false;
        el("le-save").disabled = false;
        el("le-save").innerText = "Speichern + eBay";
        render();
        showError(problems.join("\n"));
    }

    // Only what differs from what eBay returned is sent.
    function changes() {
        const { info, rows } = state;
        const variations = [];
        const lines = [];
        let invalid = null, soldOut = false;
        info.variations.forEach((v, i) => {
            const change = { sku: v.sku, specifics: v.specifics };
            const price = Number(rows[i].price), quantity = Number(rows[i].quantity);
            if (rows[i].price === "" || !(price > 0)) invalid = `${variationName(v)}: bitte einen Preis größer als 0 eingeben.`;
            else if (price !== Number(v.price)) {
                change.price = price.toFixed(2);
                lines.push(`${variationName(v)}: Preis ${v.price} € → ${change.price} €`);
            }
            if (rows[i].quantity === "" || !Number.isInteger(quantity) || quantity < 0) invalid = `${variationName(v)}: bitte eine gültige Menge eingeben.`;
            else if (quantity !== v.available) {
                change.quantity = quantity;
                change.quantity_seen = v.available;
                soldOut = soldOut || quantity === 0;
                lines.push(`${variationName(v)}: Menge ${v.available} → ${quantity}`);
            }
            if ("price" in change || "quantity" in change) variations.push(change);
        });

        const body = { item_id: info.item_id, variations };
        if (!same(state.pictures, info.pictures)) {
            body.pictures = state.pictures;
            lines.push(`Bilder des Angebots geändert (jetzt ${state.pictures.length})`);
        }
        const before = info.picture_sets || {};
        const values = info.specific_values[state.pictureName] || [];
        if (values.some(value => !same(state.sets[value] || [], before[value] || []))) {
            body.picture_name = state.pictureName;
            body.picture_sets = {};
            values.forEach(value => { if ((state.sets[value] || []).length) body.picture_sets[value] = state.sets[value]; });
            lines.push("Bilder je Variante geändert");
        }
        const count = variations.length + (body.pictures ? 1 : 0) + (body.picture_sets ? 1 : 0);
        return { body, lines, count: count || (invalid ? 1 : 0), invalid, soldOut };
    }

    async function save() {
        const { body, lines, count, invalid, soldOut } = changes();
        if (invalid) return showError(invalid);
        if (!count) return showError("Es wurde nichts geändert.");
        if (body.pictures && !body.pictures.length) return showError("Das Angebot braucht mindestens ein Bild.");
        if (body.picture_sets && !Object.keys(body.picture_sets).length) return showError("Mindestens eine Variante muss ein Bild behalten.");
        const warning = soldOut ? "\n\nAchtung: Bei Menge 0 ist die Variante ausverkauft; eBay kann sie aus dem Angebot entfernen." : "";
        if (!confirm(`Diese Änderungen werden jetzt bei eBay gespeichert:\n\n${lines.slice(0, 15).join("\n")}${lines.length > 15 ? "\n…" : ""}${warning}`)) return;

        const mine = state;
        mine.busy = true;
        el("le-save").disabled = true;
        el("le-save").innerText = "Wird an eBay gesendet …";
        showError("");
        try {
            const response = await fetch(`${API_URL}/ebay/listing-editor/${mine.productId}`, {
                method: "POST", headers: authHeaders(true), body: JSON.stringify(body),
            });
            if (response.status === 401) { logout(); return; }
            const data = await response.json();
            if (response.ok) {
                el("listing-editor").remove();
                state = null;
                showBanner("Das Angebot wurde bei eBay geändert.", false);
                loadProducts();
                return;
            }
            showError("eBay: " + (typeof data.detail === "string" ? data.detail : "Fehler"));
        } catch (err) {
            showError("Keine Antwort vom Server. Bitte den Editor neu öffnen und prüfen, ob die Änderung bei eBay angekommen ist.");
        }
        mine.busy = false;
        el("le-save").disabled = false;
        el("le-save").innerText = "Speichern + eBay";
    }

    // For the quick ✎ fields: a listing with variations is edited per variation, not with one number.
    async function editorIfVariations(productId) {
        const p = (window.__products || []).find(x => x.id === productId);
        if (!p || !/^\d{9,}$/.test(p.sku)) return false;
        try {
            const response = await fetch(`${API_URL}/ebay/listing-info/${p.sku}`, { headers: authHeaders() });
            if (!response.ok) return false;
            if (!(await response.json()).variations.length) return false;
        } catch (err) {
            return false;
        }
        showBanner("Dieses Angebot hat Varianten – bitte je Variante ändern.", false);
        openListingEditor(productId);
        return true;
    }

    window.openListingEditor = openListingEditor;
    window.editorIfVariations = editorIfVariations;
})();
