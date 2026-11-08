# Mini-COLMAP Workbench

React + TypeScript + Three.js frontend for the local FastAPI service.

## Development

Use Node.js 22.12+ (or 20.19+) and the committed npm lockfile:

```bash
npm ci
npm run dev
```

Run the Python workbench separately on `127.0.0.1:8765`. Vite proxies `/api`
requests to that service; the development frontend is normally served at
`http://127.0.0.1:5173`. The default route is the scene browser, and
`/#workspace` opens reconstruction and training.

## Validation and production assets

```bash
npm test
npm run build
```

The build runs TypeScript checking and writes production assets into
[`../src/minicolmap/ui/static/`](../src/minicolmap/ui/static/). These files are
committed and included in the Python package so normal workbench use requires
neither Node.js nor a CDN. Commit rebuilt assets together with frontend changes.

`npm run format:check` checks source formatting; `npm run format` rewrites it.
Browser scripts under [`e2e/`](e2e/) require a running workbench, matching local
data and, for rendering/training checks, an explicitly selected idle GPU. Run
them separately from `npm test`; see the [validation guide](../docs/evaluation.md).
