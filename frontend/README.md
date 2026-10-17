# SIB DOC frontend

A static console (HTML, CSS and vanilla JS, no build step) for the secure NL-to-SPARQL backend.
Ask a question in plain English; the console shows the result if it is allowed, or the reason it was blocked.

## Run

```
QG_BACKEND=http://127.0.0.1:8080 python3 dev_server.py
```

Open http://127.0.0.1:8081/cli/. `dev_server.py` serves the files and forwards `/demo/`, `/agents`, `/health` and
`/nl-to-sparql` to the backend. Other settings: `QG_PORT` (default 8081), `QG_PROXY_TIMEOUT` (default 240 s).
Add `?mock=1` to use built-in fixtures without a backend, `?intro=1` to replay the opening animation.

## Layout

- `cli/`: the console (`index.html`, `cli.css`, `cli.js`, `intro.js`, `precheck.js`, `assets/`).
- `js/`: shared config, API client (`api.js`), fixtures for mock mode, UI helpers.
- `css/tokens.css`: colour and type tokens.

`cli/precheck.js` mirrors the backend's `agents/intent_heuristic.py` to warn while typing; keep them in sync.
