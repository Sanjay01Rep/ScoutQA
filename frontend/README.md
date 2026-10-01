# ScoutQA web UI (frontend)

React + TypeScript, built with Vite. This is the browser frontend for `scoutqa ui`
(`src/scoutqa/webui/`) — see `docs/ARCHITECTURE.md` §8 for the overall design.

Only needed for **development**. End users of `pip install scoutqa[ui]` never need Node: the built
output already ships in `src/scoutqa/webui/static/` and is served by the Python backend.

## Develop

Run the Python backend and this dev server side by side:

```powershell
scoutqa ui --config scoutqa.yaml --port 8766   # prints a URL with a token
cd frontend
npm install
npm run dev                                     # opens on a different port, proxies /api/* to 8766
```

Open the dev server's URL with the same `?token=...` the backend printed.

## Build

```powershell
npm run build
```

Builds straight into `../src/scoutqa/webui/static/` (see `vite.config.ts`) — commit that output together
with any source change, so a fresh `pip install scoutqa[ui]` keeps working without a Node toolchain.

## Lint / type-check

```powershell
npm run lint   # oxlint
npm run build  # tsc -b also runs as part of the build
```
