// Included on every page under app/. Wraps fetch so that a 402 response
// from the API (get_current_user in app/core/deps.py blocking a tenant
// whose free trial has ended) redirects to a clear "please subscribe"
// page instead of leaving each page to fail silently or show a raw
// API error inline.
(function () {
    const originalFetch = window.fetch;

    window.fetch = async function (...args) {
        const response = await originalFetch(...args);

        if (response.status === 402) {
            let message = "";
            try {
                message = (await response.clone().json()).detail || "";
            } catch (err) {
                // Body wasn't JSON - fall back to the page's default message.
            }
            if (message) {
                sessionStorage.setItem("trial_expired_message", message);
            }
            window.location.href = "../trial-expired.html";
        }

        return response;
    };
})();
