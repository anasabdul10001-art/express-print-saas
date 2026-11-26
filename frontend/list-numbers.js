// Running numbers (1, 2, 3 ...) for every list of the site. Mark a container with
// data-numbered="list" (rows or cards), "cards" (picture grid) or put the
// attribute "table" on a <table>. CSS counters, so re-rendered, filtered or
// removed rows are renumbered without any script.
// Official forms (EÜR, EKS, USt) keep their own line numbers and are not marked.
(function () {
    const style = document.createElement("style");
    style.textContent = `
[data-numbered="list"], [data-numbered="cards"] { counter-reset: item; }
[data-numbered="list"] > :not(p):not(.no-number), [data-numbered="cards"] > :not(p):not(.no-number) { counter-increment: item; position: relative; }
[data-numbered="list"] > :not(p):not(.no-number) { padding-inline-start: 2.75rem !important; }
[data-numbered="list"] > :not(p):not(.no-number)::before, [data-numbered="cards"] > :not(p):not(.no-number)::before {
    content: counter(item); position: absolute; top: .5rem; inset-inline-start: .4rem; min-width: 1.75rem; padding: 0 .35rem;
    text-align: center; font-size: .75rem; line-height: 1.5rem; font-weight: 700; color: #334155; background: #e2e8f0;
    border-radius: 9999px; box-sizing: border-box; }
[data-numbered="cards"] > :not(p):not(.no-number)::before { z-index: 1; }
table[data-numbered="table"] { counter-reset: row; }
table[data-numbered="table"] > thead > tr::before, table[data-numbered="table"] > tbody > tr::before {
    display: table-cell; vertical-align: middle; padding: .5rem .5rem; width: 2.5rem; color: #64748b; font-weight: 600; text-align: center; }
table[data-numbered="table"] > thead > tr::before { content: "#"; }
table[data-numbered="table"] > tbody > tr::before { counter-increment: row; content: counter(row); }
table[data-numbered="table"] > tbody > tr:has(> th)::before { counter-increment: none; content: "#"; }
table[data-numbered="table"] > tbody > tr:has(> td[colspan])::before { counter-increment: none; content: ""; }
`;
    document.head.appendChild(style);
})();
