// Swaps the default "ShipSync" mark (any <svg class="brand-logo">) for
// the logo uploaded via admin.html's Super Admin panel, if one has been set.
// Public endpoint - no auth needed, safe to call from every page.
(function () {
    const apiUrl = typeof API_URL !== "undefined" ? API_URL : "https://express-print-saas.onrender.com";

    fetch(`${apiUrl}/site-settings`)
        .then((response) => (response.ok ? response.json() : null))
        .then((data) => {
            if (!data || !data.logo_url) return;

            // Height only (not width) - set via admin.html's logo-size slider,
            // default 32px. Width is left to scale naturally so a wide
            // rectangular logo isn't squeezed/cropped into the square box the
            // default icon mark used.
            const height = Number(data.logo_height) || 32;

            document.querySelectorAll("svg.brand-logo").forEach((svg) => {
                const img = document.createElement("img");
                img.src = data.logo_url;
                img.alt = "Logo";
                img.style.height = `${height}px`;
                img.style.width = "auto";
                img.style.borderRadius = "6px";
                svg.replaceWith(img);
            });
        })
        .catch(() => {
            // No custom logo reachable - the default SVG mark already in the
            // page stays as-is.
        });
})();
