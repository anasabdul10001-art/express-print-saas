// "Anbieten": sends one product towards a sales channel. Uses API_URL, authHeaders
// and logout from the products page. Nothing goes live from this dialog.
(function () {
    function close() { document.getElementById("send-to")?.remove(); }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function openSendTo(productId, title) {
        close();
        const back = el("div", "fixed inset-0 z-50 bg-black/50 overflow-y-auto p-3 sm:p-6");
        back.id = "send-to";
        const box = el("div", "max-w-md mx-auto bg-white rounded-xl shadow-xl p-5 space-y-3 text-slate-900");
        box.appendChild(el("h3", "text-lg font-bold", "Produkt anbieten"));
        box.appendChild(el("p", "text-sm text-slate-600 break-words", title));
        const button = (text, onclick, disabled) => {
            const b = el("button", "w-full text-left border border-slate-300 rounded-lg px-4 py-3 text-sm font-semibold hover:bg-slate-50 disabled:opacity-50 disabled:cursor-not-allowed", text);
            b.type = "button";
            b.disabled = !!disabled;
            b.onclick = onclick;
            box.appendChild(b);
            return b;
        };
        const result = el("div", "text-sm space-y-1 break-words");
        button("eBay: Angebot vorbereiten", () => { location.href = "new-listing.html?product=" + encodeURIComponent(productId); });
        const shopify = button("Shopify: als Entwurf anlegen", () => createDraft(productId, shopify, result));
        button("Amazon: noch nicht verfügbar", null, true);
        box.appendChild(el("p", "text-xs text-slate-500", "Hier wird nichts veröffentlicht. Bitte vor dem Anbieten die Pflichtangaben prüfen (z. B. WEEE, Produktsicherheit)."));
        box.appendChild(result);
        button("Schließen", close);
        back.appendChild(box);
        back.onclick = e => { if (e.target === back) close(); };
        document.body.appendChild(back);
    }

    async function createDraft(productId, btn, result) {
        if (!confirm("Das Produkt als Entwurf in Shopify anlegen? Kunden sehen Entwürfe nicht.")) return;
        btn.disabled = true;
        result.textContent = "Wird angelegt …";
        try {
            const response = await fetch(`${API_URL}/shopify/create-draft`, {
                method: "POST", headers: { ...authHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify({ product_id: productId })
            });
            if (response.status === 401) { logout(); return; }
            const data = await response.json().catch(() => ({}));
            if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Shopify: Fehler");
            result.textContent = `Entwurf in ${data.shop} angelegt. Die Menge wurde nicht übertragen.`;
            if (!data.picture_sent) result.appendChild(el("p", "text-amber-800", "Ohne Bild angelegt."));
            data.warnings.forEach(w => result.appendChild(el("p", "text-amber-800", "Preis/SKU bitte in Shopify prüfen: " + w)));
            const link = el("a", "block underline text-blue-700", "In Shopify öffnen");
            link.href = data.admin_url; link.target = "_blank"; link.rel = "noopener";
            result.appendChild(link);
        } catch (e) {
            result.textContent = e.message;
            btn.disabled = false;
        }
    }

    window.openSendTo = openSendTo;
})();
