// Firefox can expose an off-screen canvas before its pixels are repainted.
// Use Bokeh's paint scheduler, not synthetic mouse events or a scroll-time loop.
function installFirefoxPlotRepaint(plotViews, browser = window) {
    if (!/Firefox\//.test(browser.navigator.userAgent) ||
            !browser.IntersectionObserver) return;

    const views = new Map(plotViews.map(view => [view.el, view]));
    const visible = new Set();
    const pending = new Set();
    let scheduled = false;

    function queuePaint(view) {
        if (view.is_destroyed) return;
        pending.add(view);
        if (scheduled) return;
        scheduled = true;
        // Let the browser apply visibility/layout before scheduling the canvas paint.
        browser.requestAnimationFrame(() => {
            scheduled = false;
            for (const item of pending) {
                if (!item.is_destroyed && visible.has(item)) item.request_paint();
            }
            pending.clear();
        });
    }

    const observer = new browser.IntersectionObserver(entries => {
        for (const entry of entries) {
            const view = views.get(entry.target);
            if (!view) continue;
            if (entry.isIntersecting) {
                visible.add(view);
                queuePaint(view);
            } else {
                visible.delete(view);
            }
        }
    });
    for (const element of views.keys()) observer.observe(element);

    function repaintVisible() {
        if (browser.document.visibilityState === 'visible') {
            for (const view of visible) queuePaint(view);
        }
    }
    browser.document.addEventListener('visibilitychange', repaintVisible);
    browser.addEventListener('pageshow', repaintVisible);
}
