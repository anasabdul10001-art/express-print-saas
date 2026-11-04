// Turns a label PDF (a small label somewhere on a big page, e.g. eBay's A4
// files) into a PDF whose pages match the printer's paper, with the label
// filling it. Runs entirely in the browser, so buyers' addresses never leave
// the seller's computer. Needs pdf-lib and pdf.js loaded on the page.
(function () {
    const MM_TO_PT = 72 / 25.4;
    const RENDER_SCALE = 3;
    const INK_THRESHOLD = 200;
    const MARGIN_MM = 2;
    const PADDING_PT = 1;
    // eBay's corner crop marks reach about 5 mm in from the content's edge,
    // and the label's own text starts just past that.
    const CROP_MARK_BAND_MM = 5.5;

    function inkBox(isInk, x0, y0, x1, y1) {
        let minX = Infinity, minY = Infinity, maxX = -1, maxY = -1;
        for (let y = y0; y < y1; y++) {
            for (let x = x0; x < x1; x++) {
                if (!isInk(x, y)) continue;
                if (x < minX) minX = x;
                if (x > maxX) maxX = x;
                if (y < minY) minY = y;
                if (y > maxY) maxY = y;
            }
        }
        return maxX < 0 ? null : { minX, minY, maxX: maxX + 1, maxY: maxY + 1 };
    }

    // Bounding box of the ink, in pixels from the top-left. Crop marks are left
    // out only when all four corners hold ink and the edges between them are
    // empty, so a label that really does reach its corners is never cut.
    function findContentBox(isInk, width, height, bandPx) {
        const box = inkBox(isInk, 0, 0, width, height);
        if (!box) return null;

        const { minX, minY, maxX, maxY } = box;
        const b = bandPx;
        if (maxX - minX <= 2 * b || maxY - minY <= 2 * b) return box;

        const corners = [
            [minX, minY], [maxX - b, minY], [minX, maxY - b], [maxX - b, maxY - b],
        ];
        const allCornersInked = corners.every(([x, y]) => inkBox(isInk, x, y, x + b, y + b));
        const edgesEmpty =
            !inkBox(isInk, minX + b, minY, maxX - b, minY + b) &&
            !inkBox(isInk, minX + b, maxY - b, maxX - b, maxY) &&
            !inkBox(isInk, minX, minY + b, minX + b, maxY - b) &&
            !inkBox(isInk, maxX - b, minY + b, maxX, maxY - b);

        if (!allCornersInked || !edgesEmpty) return box;
        return inkBox(isInk, minX + b, minY + b, maxX - b, maxY - b) || box;
    }

    // Largest fit of the label on the paper, turned 90 degrees if that's bigger.
    function computePlacement(labelW, labelH, paperW, paperH, margin) {
        const availW = paperW - 2 * margin;
        const availH = paperH - 2 * margin;
        const straight = Math.min(availW / labelW, availH / labelH);
        const turned = Math.min(availW / labelH, availH / labelW);
        const rotate = turned > straight;
        const scale = rotate ? turned : straight;
        const drawnW = (rotate ? labelH : labelW) * scale;
        const drawnH = (rotate ? labelW : labelH) * scale;
        const left = (paperW - drawnW) / 2;
        const bottom = (paperH - drawnH) / 2;
        // A 90-degree turn pivots around the label's bottom-left corner, which
        // then sits at the drawn area's bottom-right.
        return { rotate, scale, x: rotate ? left + drawnW : left, y: bottom };
    }

    async function pageContentBox(page) {
        const viewport = page.getViewport({ scale: RENDER_SCALE, rotation: 0 });
        const canvas = document.createElement("canvas");
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        const ctx = canvas.getContext("2d", { willReadFrequently: true });
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        await page.render({ canvasContext: ctx, viewport }).promise;

        const { data, width, height } = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const isInk = (x, y) => {
            const i = (y * width + x) * 4;
            return data[i] * 0.299 + data[i + 1] * 0.587 + data[i + 2] * 0.114 < INK_THRESHOLD;
        };
        const bandPx = Math.round(CROP_MARK_BAND_MM * MM_TO_PT * RENDER_SCALE);
        const box = findContentBox(isInk, width, height, bandPx);
        if (!box) return null;

        const [vbLeft, , , vbTop] = page.view;
        return {
            left: vbLeft + box.minX / RENDER_SCALE - PADDING_PT,
            right: vbLeft + box.maxX / RENDER_SCALE + PADDING_PT,
            top: vbTop - box.minY / RENDER_SCALE + PADDING_PT,
            bottom: vbTop - box.maxY / RENDER_SCALE - PADDING_PT,
        };
    }

    async function fitLabelToPaper(bytes, paperWidthMm, paperHeightMm) {
        const { PDFDocument, degrees } = window.PDFLib;
        const paperW = paperWidthMm * MM_TO_PT;
        const paperH = paperHeightMm * MM_TO_PT;
        const margin = MARGIN_MM * MM_TO_PT;

        const reader = await window.pdfjsLib.getDocument({ data: bytes.slice(0) }).promise;
        const source = await PDFDocument.load(bytes);
        const output = await PDFDocument.create();

        for (let n = 1; n <= reader.numPages; n++) {
            const box = await pageContentBox(await reader.getPage(n));
            if (!box) continue; // blank page

            const embedded = await output.embedPage(source.getPage(n - 1), box);
            const place = computePlacement(box.right - box.left, box.top - box.bottom, paperW, paperH, margin);
            output.addPage([paperW, paperH]).drawPage(embedded, {
                x: place.x,
                y: place.y,
                xScale: place.scale,
                yScale: place.scale,
                rotate: degrees(place.rotate ? 90 : 0),
            });
        }

        if (output.getPageCount() === 0) throw new Error("empty_pdf");
        return output.save();
    }

    window.LabelTools = { findContentBox, computePlacement, fitLabelToPaper };
})();
