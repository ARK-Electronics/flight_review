/* Shared browser security boundaries. Keep all model output behind renderMarkdown. */
(function (root) {
    'use strict';
    function escapeHTML(value) {
        return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    }

    function renderMarkdown(value) {
        const text = String(value == null ? '' : value);
        if (!root.marked || !root.DOMPurify || !root.DOMPurify.isSupported) {
            return '<pre>' + escapeHTML(text) + '</pre>';
        }
        return root.DOMPurify.sanitize(root.marked.parse(text), {
            ALLOWED_TAGS: ['p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                'strong', 'em', 'del', 'blockquote', 'pre', 'code', 'ul', 'ol', 'li',
                'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a'],
            ALLOWED_ATTR: ['href', 'title', 'class'],
            ALLOW_DATA_ATTR: false,
            ALLOW_ARIA_ATTR: false
        });
    }

    function xsrfHeaders() {
        const match = root.document.cookie.match(/(?:^|;\s*)_xsrf=([^;]*)/);
        return match ? {'X-XSRFToken': decodeURIComponent(match[1])} : {};
    }

    function request(url, options) {
        options = Object.assign({}, options);
        const target = new URL(url, root.location.href);
        if (target.origin === root.location.origin &&
                !/^(GET|HEAD|OPTIONS)$/i.test(options.method || 'GET')) {
            const headers = new Headers(options.headers || {});
            Object.entries(xsrfHeaders()).forEach(([key, value]) => headers.set(key, value));
            options.headers = headers;
        }
        return root.fetch(url, options);
    }

    root.flightSecurity = {escapeHTML, renderMarkdown, xsrfHeaders, fetch: request};
}(window));
