// Adds an "eye" button to every password field: click to show/hide.
(function () {
    var EYE = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
    var EYE_OFF = '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.94 10.94 0 0 1 12 20c-7 0-11-8-11-8a18.5 18.5 0 0 1 5.06-5.94M9.9 4.24A10.9 10.9 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19M14.12 14.12a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

    function attach(input) {
        if (input.dataset.eye) return;
        input.dataset.eye = "1";
        var wrap = document.createElement("div");
        wrap.style.position = "relative";
        input.parentNode.insertBefore(wrap, input);
        wrap.appendChild(input);
        input.style.paddingRight = "2.5rem";
        var btn = document.createElement("button");
        btn.type = "button";
        btn.tabIndex = -1;
        btn.setAttribute("aria-label", "Passwort anzeigen");
        btn.innerHTML = EYE;
        btn.style.cssText = "position:absolute;top:0;bottom:0;right:0;width:2.5rem;display:flex;align-items:center;justify-content:center;color:#64748b;background:none;border:0;cursor:pointer";
        btn.onclick = function () {
            var show = input.type === "password";
            input.type = show ? "text" : "password";
            btn.innerHTML = show ? EYE_OFF : EYE;
            btn.setAttribute("aria-label", show ? "Passwort verbergen" : "Passwort anzeigen");
        };
        wrap.appendChild(btn);
    }

    function run() {
        document.querySelectorAll('input[type="password"]').forEach(attach);
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
    else run();
})();
