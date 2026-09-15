# Security review — 2026-09-15

Reviewed the HTTP handlers, browser templates, Bokeh sessions, upload/download
paths, account lifecycle, AI/support access checks, pinned Python dependencies,
and repository-managed Sevalla deployment and backup configuration.

## Findings and fixes

| Severity | Finding | Fix |
| --- | --- | --- |
| High | The first public registration on an empty database received administrator privileges. | Public registration always creates an unapproved ordinary account; operators bootstrap with `app/set_admin.py`. |
| High | Bokeh accepted caller-selected document sessions and unsigned credentials, allowing session fixation and cross-account document access. | Sign session credentials, reject explicit session selection, and bind websocket credentials to the authenticated browser session before upgrade. |
| High | Username-only login cookies survived password resets and account deletion/recreation. Reused usernames could inherit logs, jobs, and chat ownership. | Bind cookies to current credentials and account approval, revoke API keys on password reset, and permanently reserve deleted usernames. |
| High | Unverified matching email addresses granted log edit/delete access; the error-label endpoint allowed anonymous writes. | Require the approved uploader or administrator for account-based writes; preserve explicit secret edit-token access. |
| High | Download paths accepted directory traversal identifiers. | Validate IDs before filesystem lookup, validate extensions, and reject trailing-newline IDs in shared helpers. |
| High | Browse search, administrator account/email displays, plot errors/metadata, and AI Markdown could inject HTML or JavaScript. | Enable template escaping, serialize JavaScript data with `tojson`, escape dynamic text, and sanitize report/chat Markdown with a pinned local DOMPurify distribution. |
| Medium | Browser mutations lacked CSRF validation; log deletion and emailed account approvals used GET. | Enable Tornado CSRF protection, send tokens from forms/AJAX, require POST deletion confirmation, and require administrator confirmation for account approvals. Explicit API credentials remain supported. |
| Medium | Multipart headers/parts and abandoned or rejected uploads could exhaust memory, file descriptors, or disk; parser failure could fall back into the web process. | Bound multipart input, charge quotas before streaming, release temporary/orphan files, and keep failed parsing out of the web process. Stream downloads in bounded chunks. |
| Medium | Public credential endpoints lacked throttling; login redirects accepted browser-normalized external destinations. | Rate-limit credential operations and reject external redirect forms. |
| Dependency | The shared footer loaded jQuery 3.1.0, which predates upstream XSS fixes. | PR #3 updates it to jQuery 3.7.1 with a verified integrity hash. No application-specific exploit of this dependency was demonstrated. |
| High | CI found CVE-2026-86145 and CVE-2026-89161 in the container base image's inherited PCRE2 library. | PR #3 upgrades Debian packages during both build stages so inherited libraries receive available security fixes. |

The Markdown renderer does not sanitize HTML itself; the sanitizer must remain
between Markdown parsing and DOM insertion. See the [Marked documentation](https://marked.js.org/)
and [DOMPurify release](https://github.com/cure53/DOMPurify/releases/tag/3.4.15).
The jQuery issue is documented in the [upstream advisory](https://github.com/jquery/jquery/security/advisories/GHSA-gxr4-xjj5-5px2).
The Debian tracker documents the PCRE2 fixes: [CVE-2026-86145](https://security-tracker.debian.org/tracker/CVE-2026-86145)
and [CVE-2026-89161](https://security-tracker.debian.org/tracker/CVE-2026-89161).

## Validation and rollout

Regression tests exercise real local HTTP/WebSocket handlers, malformed multipart
streams and disconnects, credential revocation, deleted-name migration, access
checks, template output, and DOM sanitizer behavior. The complete suite was run
using Python 3.12 and the hash-locked production dependencies. Pylint passes.
Auditing the 42 pinned Python packages found no known vulnerabilities at review
time; the browser-test dependency audit also passed.

Deploy through the existing tested-main workflow. `setup_db.py` creates the
deleted-name reservation table and backfills identifiable orphaned log/job owners.
Existing login cookies become invalid; users must sign in again. Password resets
also revoke that account's API key. Public signup no longer bootstraps an admin;
existing administrators retain their access. The operations guide documents
explicit administrator setup.

## Scope limits

Sevalla settings were assessed from repository configuration. Live account IAM,
edge rules, runtime secrets, and production data were not inspected. Local Docker
image checks could not run because the daemon socket was inaccessible; the PR's
image build, startup smoke test, and vulnerability scan remain required CI gates.
This review does not establish that the application has no other vulnerabilities.

The account migration can identify previous owners in the logs/jobs tables. It
cannot reconstruct deleted identities represented solely by hashed chat-cache
filenames. Parser subprocesses retain the service user's OS permissions; their
resource limits do not constitute an OS security sandbox.
