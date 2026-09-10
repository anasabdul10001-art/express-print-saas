// Swaps the default "Express Print" mark (any <svg class="brand-logo">) for
// the logo uploaded via admin.html's Super Admin panel, if one has been set.
// Public endpoint - no auth needed, safe to call from every page.
(function () {
    const apiUrl = typeof API_URL !== "undefined" ? API_URL : "https://express-print-saas.onrender.com";

    fetch(`${apiUrl}/site-settings`)
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
            if (!data || !data.logo_url) return;

            document.querySelectorAll("svg.brand-logo").forEach((svg) => {
                const img = document.createElement("img");
                img.src = data.logo_url;
                img.alt = "Logo";
                img.width = Number(svg.getAttribute("width")) || 26;
                img.height = Number(svg.getAttribute("height")) || 26;
                img.style.borderRadius = "6px";
                img.style.objectFit = "cover";
                svg.replaceWith(img);
            });
        })
        .catch(() => {
            // No custom logo reachable - the default SVG mark already in the
            // page stays as-is.
        });
})();
