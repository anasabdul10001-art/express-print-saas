// Shared across every public page: injects the footer partial, and (on
// content pages) loads a page's title/content from the API by slug.
// Deliberately doesn't declare a top-level API_URL - several pages that
// include this file already declare their own `const API_URL` in an inline
// script, and a second top-level `const` with the same name in the same
// document would throw.

async function loadFooter() {
    const mount = document.getElementById("site-footer");
    if (!mount) return;
    try {
        const response = await fetch("partials/footer.html");
        mount.innerHTML = await response.text();
    } catch (err) {
        // Footer is a non-critical enhancement - the page still works without it.
    }
}

async function loadPage(slug) {
    const titleEl = document.getElementById("page-title");
    const contentEl = document.getElementById("page-content");
    const loadingEl = document.getElementById("page-loading");
    const errorEl = document.getElementById("page-error");

    try {
        const response = await fetch(`https://express-print-saas.onrender.com/pages/${slug}`);
        if (!response.ok) throw new Error("request failed");
        const page = await response.json();

        document.title = `${page.title} — Express Print`;
        if (titleEl) titleEl.innerText = page.title;
        if (contentEl) contentEl.innerHTML = page.content;
        if (loadingEl) loadingEl.classList.add("hidden");
        if (contentEl) contentEl.classList.remove("hidden");
    } catch (err) {
        if (loadingEl) loadingEl.classList.add("hidden");
        if (errorEl) errorEl.classList.remove("hidden");
    }
}

loadFooter();
