const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/plot_app/templates/firefox_plot_repaint.js', 'utf8');

function fixture(userAgent = 'Mozilla/5.0 Firefox/143.0') {
    let intersect;
    const frames = [];
    const listeners = {};
    const observed = [];
    const browser = {
        navigator: {userAgent},
        document: {visibilityState: 'visible', addEventListener: (name, fn) => listeners[name] = fn},
        addEventListener: (name, fn) => listeners[name] = fn,
        requestAnimationFrame: fn => frames.push(fn),
        IntersectionObserver: class {
            constructor(fn) { intersect = fn; }
            observe(el) { observed.push(el); }
        },
    };
    const context = vm.createContext({});
    vm.runInContext(source, context);
    const view = {el: {}, count: 0, request_paint() { this.count++; }};
    context.installFirefoxPlotRepaint([view], browser);
    return {browser, view, observed, listeners, frames,
        enter: value => intersect([{target: view.el, isIntersecting: value}]),
        flush: () => { while (frames.length) frames.shift()(); }};
}

test('Firefox paints on viewport entry and re-entry without mouse events', () => {
    const f = fixture();
    assert.equal(f.observed.length, 1);
    f.enter(true);
    assert.equal(f.view.count, 0);
    f.flush();
    assert.equal(f.view.count, 1);
    f.enter(false);
    f.enter(true);
    f.flush();
    assert.equal(f.view.count, 2);
});
test('visibility and page restoration coalesce; hidden and destroyed plots are skipped', () => {
    const f = fixture();
    f.enter(true);
    f.listeners.pageshow();
    f.listeners.visibilitychange();
    assert.equal(f.frames.length, 1);
    f.flush();
    assert.equal(f.view.count, 1);
    f.enter(false);
    f.listeners.pageshow();
    f.flush();
    assert.equal(f.view.count, 1);
    f.enter(true);
    f.view.is_destroyed = true;
    f.flush();
    assert.equal(f.view.count, 1);
});
test('other browsers do not acquire repaint observers or handlers', () => {
    const f = fixture('Chrome/140.0');
    assert.equal(f.observed.length, 0);
    assert.equal(Object.keys(f.listeners).length, 0);
});
