# Hypit runtime findings — Node 22 + Hypit CLI + headless Chromium on this VM

**Date:** 2026-11-15 ~06:30 EST (investigation run, all times UTC-5)
**Scope:** Read-only runtime investigation for the VektorCode `hypit-integration` build.
No push, deploy, or production touch. No paid services. No keys/secrets.
**Test machine:** this Linux VM — Ubuntu 24.04.5 LTS, x86_64, 7.9 GB RAM
(only ~540 MB–1.4 GB *available* during the tests — the box was heavily loaded),
142 GB free on /home. **Not** a Render instance: results below are what was
measured here; Render-specific claims are marked VERIFIED vs OPEN.

**TL;DR:** Node 22 and the Hypit CLI install and run cleanly with no root.
Headless Chromium renders, but Hypit's real browser (full Chrome for Testing)
costs **~480 MB PSS for a trivial static page** — effectively the whole
512 MB Render free allowance before the Python app, Node, or any video work.
**Recommendation: do NOT render on the Render free web service. Keep the
dashboard on Render; run Hypit renders in a Daytona sandbox (or locally).**

---

## 1. What this VM has (baseline)

| Check | Result |
|---|---|
| `node` | **v24.22.1** preinstalled (`/usr/bin/node`) — system node, NOT what Render has |
| `npm` / `npx` | 10.9.4 (`/opt/hatch-image/bin/npm`) |
| `docker` | **not installed** |
| `apt-get` / `dpkg` | present (`/usr/bin/apt-get`, `/usr/bin/dpkg`) |
| `sudo` | **works passwordless here** (`sudo -n true` → exit 0). Render free has **no sudo** — nothing below relies on it except the one `apt-get update` attempt, which is flagged |
| RAM | 7,934 MB total; `free -m` showed 540 MB available at start, 1,384 MB at end |
| Disk | `/home` 142 GB free; `/` 7.5 GB; **`/tmp` is a 512 MB tmpfs — too small for downloads, all work used `~/workspace/scratch`** |
| `ffmpeg` / `ffprobe` | **8.1.2 present** (`/usr/bin/ffmpeg`) — system packages on this VM, NOT on Render's base image |
| OS | Ubuntu 24.04.5 LTS, x86_64 |

Render's `render.yaml` (read, not edited): `plan: free`, Python runtime,
`buildCommand: pip install -r requirements.txt`, `startCommand: python main.py`,
`PYTHON_VERSION: 3.11.9`, `requirements.txt` intentionally empty.

---

## 2. Node 22 — ✅ VERIFIED working

Downloaded the official tarball and extracted it with no root:

```
$ curl -fsSL -o node-v22.tar.xz \
    https://nodejs.org/dist/v22.21.1/node-v22.21.1-linux-x64.tar.xz
# 30,815,588 bytes (30 MB), ~4.9 s download

$ tar --no-same-owner -xf node-v22.tar.xz
# extracted 201 MB, ~4.1 s

$ ./node-v22.21.1-linux-x64/bin/node --version   → v22.21.1
$ ./node-v22.21.1-linux-x64/bin/npm --version    → 10.9.4
$ ./node-v22.21.1-linux-x64/bin/npx --version    → 10.9.4
```

**Gotcha — plain `tar -xf` fails:** the tarball preserves uid/gid 1000, and a
plain extract exits **2** with hundreds of `Cannot change ownership to uid
1000, gid 1000: Operation not permitted` errors (files still extract, but the
non-zero exit would fail a `set -e` buildCommand). **`tar --no-same-owner`
is required** — `scripts/install-node.sh` uses it.

**Verdict: Node 22 install via render.yaml buildCommand is VERIFIED feasible.**
Download tarball → extract to `$RENDER_PROJECT_DIR/.node` → prepend to PATH.
Cost: ~30 MB download, 201 MB disk, ~10 s total.

---

## 3. Hypit CLI — ✅ VERIFIED working

Installed from npm into a local prefix (no system-wide install, no root):

```
$ npm install -g @hypit/hypit --prefix=$HOME/workspace/scratch/hypit-test/prefix
# added 137 packages in 1m31s
# installed size: 157 MB
# binary: prefix/bin/hypit → ../lib/node_modules/@hypit/hypit/bin/hypit.mjs

$ hypit version
@hypit/hypit 0.3.2
Distribution: .../prefix/lib/node_modules/@hypit/hypit
Launcher: .../prefix/lib/node_modules/@hypit/hypit/bin/hypit.mjs

$ hypit --help
# full command list: check/plan/pricing/build, builds/history/logs/status/
# inspect/get, doctor, activity, cancel, auth, version, cli, runtime
# init|use|unset|up|status|logs|down, programs, capture screenshot|run|
# install-browser, download, media probe|cut|frames|tile, estimate, transcribe
```

Version **0.3.2** matches the `hypit-src` clone's `package.json`
(`"name": "@hypit/hypit", "version": "0.3.2"`).

Notes:
- `hypit doctor` outside a Hypit project fails with `CLI_ERROR: No Hypit
  project was found; run inside a package.json with hypit.project set to
  true, or pass --project <directory>` — expected, not a defect.
- Every `hypit` invocation prints a harmless Node warning:
  `[UNDICI-EHPA] Warning: EnvHttpProxyAgent is experimental…` (twice).
  Cosmetic only.

**Verdict: Hypit CLI installs and runs. VERIFIED.** Cost: 137 packages,
157 MB disk, ~90 s install. No paid model services needed for the CLI itself.

---

## 4. Chromium — ⚠️ RENDERS, BUT MEMORY IS THE PROBLEM

### 4a. Playwright chrome-headless-shell — ✅ renders

```
$ npx -y playwright@latest install chromium-headless-shell
# 287 MB total (browser + ffmpeg), ~23 s
# binary: chromium_headless_shell-1255/chrome-headless-shell-linux64/chrome-headless-shell
# 216 MB binary, Google Chrome for Testing 159.0.8154.6

$ chrome-headless-shell --headless --disable-gpu --no-sandbox \
    --screenshot=out.png --window-size=1280,720 file:///.../test.html
# 13415 bytes written to file out.png  (exit 0)
# dbus / vaapi / bluetooth ERROR lines on stderr are non-fatal noise
```

`ldd`: **52 linked libs, zero missing** on this VM (libnss3, libnspr4,
libatk, libatk-bridge, libatspi, libgbm, libasound, libxkbcommon, libdrm,
libX11/Xcomposite/Xdamage/Xext/Xfixes/Xi/Xrandr/Xrender, libdbus, libexpat,
libglib, …).

Memory (measured with a process-tree poller reading
`/proc/<pid>/smaps_rollup` **PSS** — the honest shared-memory-aware number;
plain RSS sums double-count shared pages):

| Run | Peak tree PSS | Peak tree RSS-sum | Procs | Time |
|---|---|---|---|---|
| trivial page #1 | **184.6 MB** | 475.7 MB | 7 | 0.98 s |
| trivial page #2 | **177.2 MB** | 471.1 MB | 7 | 0.35 s |

### 4b. Hypit's own browser (Chrome for Testing 153) — ✅ renders via Hypit, ❌ ~480 MB

```
$ hypit capture install-browser --browser-cache .../cache
# Preparing the capture browser…
# Capture browser ready: .../cache/chrome/linux-153.0.8010.12/chrome-linux64/chrome
# 393 MB, ~17.6 s — no Hypit project needed for install-browser
# binary is 293 MB; Google Chrome for Testing 153.0.8010.12
```

`ldd`: **87 linked libs, zero missing** on this VM — 35 more than
headless-shell, including **libcups, libpango, fontconfig, freetype,
harfbuzz, cairo, libpixman, libpng16, libavahi, libgnutls, libkrb5**,
i.e. the full font/text/print stack a real page needs.

Two failures on the way to a working render, both understood:

1. `hypit capture screenshot ./test.html --to shot.png` **failed**:
   `Running as root without --no-sandbox is not supported` — Puppeteer
   launches Chrome without `--no-sandbox` by default, and this VM runs as
   root. (Render runs non-root, so this exact failure would not occur there —
   but containers generally still need `--no-sandbox` for Chrome's sandbox.)
2. A capture script with `export const options = { args: ["--no-sandbox", …] }`
   **also failed the same way** — wrong shape. Hypit's source
   (`packages/browser-capture/src/cli.ts:150-156`, `index.ts:94`) merges
   script options as `options.launch`. Correct shape:

```js
// cap.mjs — WORKS
export const options = { launch: { args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"] } };
export default async ({ page, screenshot, log }) => {
  await page.goto("file:///…/test.html");
  await screenshot({ path: "shot2.png" });
  log("screenshot saved");
};
```

```
$ hypit capture run ./cap.mjs --browser-cache .../cache
# Saved …/shot2.png (1280×720)
# screenshot saved
# Capture complete: 1 file.   (total ~5.5 s)
# shot2.png: PNG image data, 1280 x 720, 8-bit/color RGB — visually verified:
#   green "Hypit render probe" heading on black, correct text.
```

Also confirmed in Hypit's source: the local-HTML render path
(`packages/provider-html-local/src/capture.ts:189`) already passes
`args: ["--no-sandbox", "--disable-dev-shm-usage", …]` — so Hypit's own
HTML→video path is container-aware; only the `capture screenshot/run`
Puppeteer path needs the launch args supplied.

Memory for **full Chrome 153, trivial static page** (same PSS method):

| Run | Peak tree PSS | Peak tree RSS-sum | Procs | Time |
|---|---|---|---|---|
| trivial page #1 | **479.4 MB** | 1493.3 MB | 14 | 4.04 s |
| trivial page #2 | **476.1 MB** | 1479.7 MB | 14 | 4.85 s |

**This is the decisive number: ~480 MB PSS for one trivial page, before the
Python app, before Node, before ffmpeg encode, before any real video work.**
Render free = 512 MB. That leaves ~30 MB headroom — a real Hypit render
(page + media + recording + ffmpeg) will exceed it.

### 4c. Missing-libs workaround (dpkg -x + LD_LIBRARY_PATH) — ✅ MECHANISM VERIFIED

Render's base image does **not** ship most of the 52/87 libs above, so the
question was whether they can be supplied without root/sudo. Tested:

- `apt-get download <18 packages>` **failed on this VM**: `E: Can't find a
  source to download version '…' of 'libxfixes3:amd64'` (and libxi6,
  libxkbcommon0, libxrandr2, libxrender1) — local apt lists are stale/empty
  (`/var/lib/apt/lists/` holds 3 entries). A no-sudo `apt-get update` hung
  4+ minutes without populating the lists and was killed; a re-test of
  `apt-get download libnss3` afterwards still fails the same way. So the
  apt route needs a working update first, which needs root on this box.
  **On Render, `apt-get download` after an update is untested — do not
  assume it works.**
- **Direct .deb download works with plain curl, no apt lists needed:**
  `curl -fsSL http://archive.ubuntu.com/ubuntu/pool/main/n/nss/libnss3_3.98-1build1_amd64.deb`
  → 1,444,972 bytes, valid Debian package.
- `dpkg -x libnss3.deb local/` extracts usable `.so` files **without root**.
- With `LD_LIBRARY_PATH=…/local/usr/lib/x86_64-linux-gnu`, `ldd` resolves
  `libnss3.so` **from the local dir** instead of `/lib/x86_64-linux-gnu/…`,
  and chrome-headless-shell renders identically (exit 0, same 13,415-byte
  PNG, PSS 183.4 MB).

The 18 packages that provide the non-core libs (via `dpkg -S` on this VM):
`libnss3 libnspr4 libatk1.0-0t64 libatk-bridge2.0-0t64 libatspi2.0-0t64
libgbm1 libasound2t64 libxkbcommon0 libdrm2 libxcomposite1 libxdamage1
libxrandr2 libxfixes3 libxi6 libxrender1 libxext6 libx11-6 libdbus-1-3
libexpat1 libglib2.0-0t64` — plus, for **full** Chrome, the font/text stack
(`libpango`, `fontconfig`, `freetype`, `harfbuzz`, `cairo`, `libcups`, …)
and their transitive deps. Total .deb payload not yet measured.

**Verdict: the extraction mechanism works without root. Whether the full
set satisfies Chrome on Render's minimal image is OPEN — only a real
Render deploy (or a minimal-container simulation) can prove it.**

---

## 5. Other findings

- **ffmpeg/ffprobe 8.1.2 are present on this VM but NOT on Render's base
  image.** Hypit's help states: `Native page recording requires Chrome 153+
  and ffprobe on PATH.` A Render render path would also need a static ffmpeg
  build (~70–80 MB) or the same .deb trick. Not tested here.
- **Disk footprint of a full local install:** Node 201 MB + Hypit CLI 157 MB
  + Hypit Chrome 393 MB = **~751 MB** (Playwright headless-shell adds
  another 287 MB if kept). Render free uses ephemeral disk rebuilt on every
  deploy — with no build cache in the native environment, every deploy
  re-pays the ~2–3 min build time (Node ~10 s + Hypit npm install ~90 s +
  Chrome download ~18 s) and the downloads.
- **Build time added to render.yaml:** ~2–3 min total. Acceptable; not the
  blocker. RAM is the blocker.
- System node v24 on this VM is irrelevant to Render (Render's Python image
  ships no Node) — the tarball install is the whole story.
- No `docker` on this VM, so no container-simulation test was possible.

---

## 6. Render feasibility — judgement

| Piece | Status | Evidence |
|---|---|---|
| Node 22 via buildCommand | **✅ VERIFIED feasible** | Tarball download + `--no-same-owner` extract + PATH works, no root, ~10 s, 201 MB |
| Hypit CLI via buildCommand | **✅ VERIFIED feasible** | `npm install -g` into local prefix works, 0.3.2 runs, ~90 s, 157 MB |
| Chromium binary on Render | **⚠️ MECHANISM verified, RENDER unproven** | `dpkg -x` + `LD_LIBRARY_PATH` works without root here; whether the full lib set resolves on Render's minimal image is **OPEN** |
| Chromium RAM on 512 MB free | **❌ VERIFIED TOO BIG (full Chrome)** | **~480 MB PSS for a trivial page** with Hypit's actual browser; ~180 MB PSS with headless-shell, but Hypit doesn't use headless-shell |
| ffmpeg/ffprobe on Render | **OPEN** | Present here (8.1.2), absent on Render base image; static build not tested |
| Build time / disk | **⚠️ Acceptable but wasteful** | ~2–3 min + ~751 MB re-downloaded on every deploy (no build cache) |

**Bottom line:** Node + Hypit CLI on Render free is a solved problem.
Chromium rendering on the same 512 MB free web service is not viable with
Hypit's real browser — the memory measurement says so, not the docs.
A single render would very likely OOM-kill the web service (and take the
dashboard down with it, since they share the instance).

---

## 7. Recommended path

1. **Render free web service: dashboard + API + job queue ONLY.**
   Install Node 22 + Hypit CLI at build time (`scripts/install-node.sh` +
   an `npm install -g @hypit/hypit` step) so the service can author/plan
   Hypit projects, run `hypit check/plan/pricing`, and queue builds — all
   of which need no browser. No Chromium on Render.
2. **Rendering: Daytona sandbox (preferred) or local run.**
   Daytona gives a real machine with root, apt, and RAM headroom; Hypit's
   `runtime up` / local-runtime model is designed for exactly this shape.
   A Daytona sandbox already exists in this project for the VektorCode voice
   worker — same pattern, new sandbox for Hypit renders. Local (laptop/phone
   via Codex, Hypit's named supported agent) is the fallback.
3. **If Render rendering is ever retried:** use `chrome-headless-shell`
   (~180 MB PSS) instead of full Chrome, `LD_LIBRARY_PATH` with extracted
   .debs, `--disable-dev-shm-usage`, and a **separate Render service** so an
   OOM can't kill the dashboard — and prove it with a real deploy first,
   because the lib set on Render's image is still unverified. Do not bet the
   dashboard on it.
4. **Do not** put Chromium, ffmpeg, or the .deb payload into the dashboard
   service's build until the separate-service deploy proves the memory and
   lib story.

---

## 8. Open questions (only a real Render deploy can answer)

1. Which of the 52/87 shared libs are actually present in Render's native
   Python image? (Determines the .deb list size.)
2. Does `apt-get update` + `apt-get download` work during a Render build,
   or must .debs come from direct archive.ubuntu.com URLs?
3. Can full Chrome 153 render a real Hypit video workload inside 512 MB
   alongside the Python app? (Measured evidence says no for the browser
   alone; a deploy would confirm the OOM.)
4. Render free ephemeral-disk limit for the ~751 MB install payload.
5. Whether Render's build user can write `$RENDER_PROJECT_DIR/.node` and
   whether `LD_LIBRARY_PATH` set in `startCommand` survives to runtime
   (expected yes, unproven).

---

## Appendix — commands & exact outputs (trimmed)

```
# Environment
$ node --version; npm --version; docker --version; sudo -n true; echo $?
v24.22.1 / 10.9.4 / docker: command not found / sudo exit: 0
$ free -m            → Mem: 7934 total, 7393 used, 135 free, 540 available
$ df -h              → /home 142G avail; /tmp tmpfs 512M
$ cat /etc/os-release → Ubuntu 24.04.5 LTS (noble), x86_64

# Node
$ curl -fsSL -o node-v22.tar.xz https://nodejs.org/dist/v22.21.1/node-v22.21.1-linux-x64.tar.xz
  real 0m4.878s, 30,815,588 bytes
$ tar --no-same-owner -xf node-v22.tar.xz   → 201M, real 0m4.147s
$ node --version → v22.21.1 ; npm --version → 10.9.4

# Hypit CLI
$ npm install -g @hypit/hypit --prefix=…/prefix
  added 137 packages in 1m31s ; 157M
$ hypit version → @hypit/hypit 0.3.2

# Playwright headless-shell
$ npx -y playwright@latest install chromium-headless-shell
  287M, real 0m23.030s ; Chrome for Testing 159.0.8154.6
  (note: first attempt with target 'chrome-headless-shell' failed —
   correct target name is 'chromium-headless-shell')
$ ldd chrome-headless-shell → 52 libs, 0 missing

# Hypit browser
$ hypit capture install-browser --browser-cache …/cache
  Capture browser ready: …/chrome/linux-153.0.8010.12/chrome-linux64/chrome
  393M, real 0m17.605s ; Chrome for Testing 153.0.8010.12
$ ldd chrome → 87 libs, 0 missing
$ hypit capture screenshot … → FAILED (root + no --no-sandbox)
$ hypit capture run ./cap.mjs (options.launch.args incl. --no-sandbox)
  Saved …/shot2.png (1280×720) ; Capture complete: 1 file. ; real 0m5.494s

# Memory (PSS, process-tree peak, trivial static page)
headless-shell: 184.6 / 177.2 MB (7 procs)
full Chrome 153: 479.4 / 476.1 MB (14 procs)
headless-shell + LD_LIBRARY_PATH (local libnss3): 183.4 MB

# dpkg -x mechanism
$ curl -fsSL http://archive.ubuntu.com/ubuntu/pool/main/n/nss/libnss3_3.98-1build1_amd64.deb
  1,444,972 bytes, valid .deb
$ dpkg -x libnss3.deb local/   → libnss3.so etc. extracted, no root
$ LD_LIBRARY_PATH=…/local/usr/lib/x86_64-linux-gnu ldd chrome-headless-shell
  libnss3.so => …/local/usr/lib/x86_64-linux-gnu/libnss3.so  (local wins)
$ … render with LD_LIBRARY_PATH → exit 0, 13415-byte PNG, PSS 183.4 MB
```

Scratch dirs (not deliverables, kept for re-checks):
`~/workspace/scratch/node22-test/`, `~/workspace/scratch/hypit-test/`,
`~/workspace/scratch/hypit-probe/`, `~/workspace/scratch/pw-browsers/`,
`~/workspace/scratch/chrome-test/`, `~/workspace/scratch/deb-test/`
