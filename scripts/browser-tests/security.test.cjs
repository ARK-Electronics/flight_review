'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {JSDOM} = require('jsdom');
const createDOMPurify = require('../../app/plot_app/static/js/vendor/purify.min.js');

async function browser() {
    const dom = new JSDOM('<!doctype html><body></body>', {
        url: 'https://review.example/ai_analysis', runScripts: 'outside-only'
    });
    dom.window.marked = await import('marked');
    dom.window.DOMPurify = createDOMPurify(dom.window);
    dom.window.Headers = Headers;
    dom.window.eval(fs.readFileSync(path.join(__dirname,
        '../../app/plot_app/static/js/security.js'), 'utf8'));
    return dom.window;
}

test('reports and chat remove executable markup and automatic external loads', async () => {
    const window = await browser();
    const payloads = [
        '<img src="https://attacker.example/leak" onerror="alert(1)">',
        '<svg><a xlink:href="javascript:alert(1)">click</a></svg>',
        '<a href="jav&#x61;script:alert(1)" onclick="alert(1)">click</a>',
        '[click](javascript:alert%281%29)',
        '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
        '<style>body{background:url(https://attacker.example/leak)}</style>',
        '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">',
        '<form id="location"><input name="href" value="javascript:alert(1)"></form>'
    ];
    for (const payload of payloads) {
        window.document.body.innerHTML = window.flightSecurity.renderMarkdown(payload);
        assert.equal(window.document.querySelector(
            'script,img,svg,math,style,iframe,form,input,object,embed'), null, payload);
        for (const element of window.document.body.querySelectorAll('*')) {
            for (const attribute of element.attributes) {
                assert.ok(!attribute.name.startsWith('on'), payload);
            }
        }
        for (const anchor of window.document.querySelectorAll('a[href]')) {
            assert.ok(!/^javascript:/i.test(anchor.href), payload);
        }
    }
    window.close();
});

test('safe markdown remains formatted and sanitizer failure produces only text', async () => {
    const window = await browser();
    window.document.body.innerHTML = window.flightSecurity.renderMarkdown(
        '# Report\n\n**Strong** and [docs](https://docs.example/)\n\n`code`');
    assert.equal(window.document.querySelector('h1').textContent, 'Report');
    assert.equal(window.document.querySelector('strong').textContent, 'Strong');
    assert.equal(window.document.querySelector('a').href, 'https://docs.example/');
    window.DOMPurify = null;
    window.document.body.innerHTML = window.flightSecurity.renderMarkdown('<img src=x onerror=alert(1)>');
    assert.equal(window.document.querySelector('img'), null);
    assert.equal(window.document.body.textContent, '<img src=x onerror=alert(1)>');
    window.close();
});

test('CSRF tokens accompany same-origin mutations only', async () => {
    const window = await browser();
    window.document.cookie = '_xsrf=test-token';
    const sent = [];
    window.fetch = (url, options) => { sent.push({url, options}); };
    window.flightSecurity.fetch('/ai_analysis/api', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'
    });
    window.flightSecurity.fetch('/ai_analysis/jobs/job', {method: 'DELETE'});
    window.flightSecurity.fetch('/ai_analysis/api');
    window.flightSecurity.fetch('https://attacker.example/', {method: 'POST'});
    assert.equal(sent[0].options.headers.get('X-XSRFToken'), 'test-token');
    assert.equal(sent[0].options.headers.get('Content-Type'), 'application/json');
    assert.equal(sent[1].options.headers.get('X-XSRFToken'), 'test-token');
    assert.equal(sent[2].options.headers, undefined);
    assert.equal(sent[3].options.headers, undefined);
    window.close();
});
