// Fills the operator's name and address (kept on the server) into the static legal pages.
(async function () {
    const base = ["localhost", "127.0.0.1"].includes(location.hostname) ? location.origin : "https://express-print-saas.onrender.com";
    try {
        const response = await fetch(`${base}/legal/operator`);
        if (!response.ok) return;
        const values = await response.json();
        document.querySelectorAll("[data-operator]").forEach(element => {
            const value = values[element.dataset.operator];
            if (value) element.textContent = value;
        });
    } catch (error) { /* the page keeps its visible "to be added" text */ }
})();
