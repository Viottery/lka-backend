// Fixed, read-only DOM projection. No caller-provided JavaScript is evaluated.
() => {
    const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
    const visible = el => {
        if (el.closest('[hidden], [aria-hidden="true"]')) return false;
        const style = getComputedStyle(el);
        return style.display !== 'none' && style.visibility !== 'hidden' && el.getClientRects().length > 0;
    };
    const text = el => clean(el.innerText || el.getAttribute('aria-label')).slice(0, 1000);
    const controls = [], rows = [], links = [], microdata = [], jsonld = [];
    const limits = {nodes: false, controls: false, rows: false, links: false, structured_data: false};
    let scanned = 0;
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
        if (++scanned > 5000) { limits.nodes = true; break; }
        const el = walker.currentNode;
        const tag = el.tagName.toLowerCase();
        if (!visible(el)) continue;
        const role = el.getAttribute('role') || '';
        if (['button', 'select', 'option', 'input'].includes(tag) || ['button', 'option', 'combobox'].includes(role)) {
            if (controls.length >= 80) { limits.controls = true; continue; }
            const label = clean(el.getAttribute('aria-label') || (el.labels && Array.from(el.labels).map(text).join(' ')) || text(el)).slice(0, 1000);
            // A disabled control is a UI state, never proof of sold-out inventory.
            const control = {kind: role || tag, label, disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true'};
            if (tag === 'select') {
                control.options = Array.from(el.options).slice(0, 80).map(o => ({label: clean(o.textContent).slice(0, 300), disabled: o.disabled, selected: o.selected}));
                control.options_truncated = el.options.length > 80;
            }
            controls.push(control);
        }
        if (tag === 'tr' || role === 'row') {
            if (rows.length >= 80) { limits.rows = true; continue; }
            const cells = Array.from(el.querySelectorAll('th,td,[role="cell"],[role="gridcell"]')).filter(visible);
            rows.push({cells: cells.slice(0, 20).map(text), text: text(el), cells_truncated: cells.length > 20});
        }
        if (tag === 'a' && el.href && text(el)) {
            if (links.length >= 80) { limits.links = true; continue; }
            if (el.href.startsWith('https://')) links.push({label: text(el), url: el.href.slice(0, 2048)});
        }
        if ((el.getAttribute('itemtype') || '').match(/^https?:\/\/schema.org\/Offer$/)) {
            if (microdata.length >= 40) { limits.structured_data = true; continue; }
            const offer = {'@type': 'Offer'};
            for (const key of ['name', 'price', 'priceCurrency', 'availability']) {
                const prop = el.querySelector(`[itemprop="${key}"]`);
                if (prop) offer[key] = clean(prop.getAttribute('content') || prop.getAttribute('href') || prop.textContent).slice(0, 500);
            }
            microdata.push(offer);
        }
    }
    let structuredBytes = 0;
    for (const el of document.querySelectorAll('script[type="application/ld+json"]')) {
        const value = el.textContent || '';
        structuredBytes += value.length;
        if (jsonld.length >= 16 || structuredBytes > 131072) { limits.structured_data = true; break; }
        jsonld.push(value);
    }
    const body = document.body ? document.body.innerText : '';
    return {
        title: document.title.slice(0, 200), text: body.slice(0, 200000),
        text_truncated: body.length > 200000,
        projection: {controls, rows, links, limits, scanned_nodes: Math.min(scanned, 5000),
            iframe_count: document.querySelectorAll('iframe').length,
            canvas_count: document.querySelectorAll('canvas').length,
            scope: 'main_document_dom_not_pixels_or_full_inventory'},
        jsonld, microdata
    };
}
