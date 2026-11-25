// Renders the text of an editable site page. Pages may be written in HTML or as
// plain text. HTML is rebuilt from an allow-list, so scripts, event handlers and
// unsafe link targets never reach the page.
(function () {
    const ALLOWED = new Set(["H1", "H2", "H3", "H4", "P", "BR", "HR", "UL", "OL", "LI", "STRONG", "B", "EM", "I",
        "A", "DIV", "SPAN", "SECTION", "TABLE", "THEAD", "TBODY", "TR", "TH", "TD"]);
    const DROPPED = new Set(["SCRIPT", "STYLE", "TEMPLATE", "IFRAME", "OBJECT", "EMBED", "NOSCRIPT", "SVG", "MATH"]);

    function safeHref(value) {
        try {
            return ["http:", "https:", "mailto:", "tel:"].includes(new URL(value, location.href).protocol);
        } catch (err) {
            return false;
        }
    }

    function copy(source, target) {
        source.childNodes.forEach(node => {
            if (node.nodeType === Node.TEXT_NODE) {
                target.appendChild(document.createTextNode(node.textContent));
                return;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) return;
            const tag = node.nodeName.toUpperCase();
            if (DROPPED.has(tag)) return;
            if (!ALLOWED.has(tag)) {
                copy(node, target);
                return;
            }
            const element = document.createElement(tag);
            if (node.hasAttribute("class")) element.setAttribute("class", node.getAttribute("class"));
            if (tag === "A" && node.hasAttribute("href") && safeHref(node.getAttribute("href"))) {
                element.setAttribute("href", node.getAttribute("href"));
                element.classList.add("underline");
            }
            copy(node, element);
            target.appendChild(element);
        });
    }

    window.renderPageContent = function (container, text) {
        container.innerHTML = "";
        if (!/<\/?(h[1-4]|p|ul|ol|li|div|br|a|strong|section|table)\b[^>]*>/i.test(text)) {
            text.split(/\n\s*\n/).forEach(paragraph => {
                if (!paragraph.trim()) return;
                const p = document.createElement("p");
                p.innerText = paragraph.trim();
                container.appendChild(p);
            });
            return;
        }
        copy(new DOMParser().parseFromString(text, "text/html").body, container);
        // The page already shows its title above the content.
        const heading = container.firstElementChild;
        if (heading && heading.tagName === "H1") heading.remove();
    };
})();
