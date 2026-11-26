// The text of a native file field comes from the browser's own language (e.g.
// Arabic on a German page). This replaces what is visible with a button and a
// file name in the language of the page; the real input keeps doing the work.
(function () {
    const TEXT = {
        de: { button: "Datei auswählen", none: "Keine Datei ausgewählt", many: "Dateien ausgewählt" },
        en: { button: "Choose file", none: "No file chosen", many: "files selected" },
    };
    const lang = () => {
        const current = window.getLang ? window.getLang() : document.documentElement.lang;
        return TEXT[current] ? current : "de";
    };
    const fields = [];

    function enhance(input) {
        const box = document.createElement("span");
        box.className = "mt-1 flex flex-wrap items-center gap-2 text-sm font-normal";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "file-button border border-slate-300 bg-slate-100 hover:bg-slate-200 rounded px-3 py-1.5";
        const name = document.createElement("span");
        name.className = "file-name text-slate-500 break-all";
        name.setAttribute("aria-live", "polite");
        box.append(button, name);
        input.after(box);  // after the input, so a surrounding <label> still belongs to the input
        input.style.cssText = "position:absolute;width:1px;height:1px;opacity:0;overflow:hidden;";
        input.tabIndex = -1;

        const render = () => {
            const text = TEXT[lang()];
            const files = input.files;
            button.textContent = text.button;
            name.textContent = !files || !files.length ? text.none
                : files.length === 1 ? files[0].name : `${files.length} ${text.many}`;
        };
        button.addEventListener("click", () => input.click());
        input.addEventListener("change", render);
        if (input.form) input.form.addEventListener("reset", () => setTimeout(render));
        // pages clear the field with input.value = "", which fires no event
        const native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
        Object.defineProperty(input, "value", {
            configurable: true,
            get() { return native.get.call(this); },
            set(v) { native.set.call(this, v); render(); },
        });
        fields.push(render);
        render();
    }

    // Fields that are hidden already have their own button on the page.
    const init = () => document.querySelectorAll('input[type="file"]:not(.hidden)').forEach(enhance);
    if (document.readyState === "complete") init();
    else window.addEventListener("DOMContentLoaded", init);
    document.addEventListener("langchange", () => fields.forEach(render => render()));
})();
