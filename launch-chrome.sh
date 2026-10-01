#!/data/data/com.termux/files/usr/bin/bash
# launch-chrome.sh — launches Termux Chromium fully detached from the caller.
#
# Uses setsid + nohup + disown + full redirection so Android does NOT kill
# Chromium when the calling shell/Python process exits.
#
# PID + activity files written to ~/.hermes/run/browser-pw/; the cron watchdog
# (browser-idle-watchdog.sh) kills Chromium after IDLE_TIMEOUT seconds idle.
#
# ___________________________________________________________________________
# DESIGN NOTES (do not regress):
#
# * Hardcoded absolute path to the REAL chrome binary, NOT the chromium-browser
#   symlink chain (chromium-browser → chromium-launcher.sh → exec chrome).
#   We tried `chromium-browser`; it intermittently failed with
#   "cannot open /bin/chromium-browser: No such file" when Hermes spawned this
#   script via a non-Termux /bin/sh, and the launcher.sh shim adds no value for
#   headless CDP. Going straight to /data/data/com.termux/files/usr/lib/chromium/chrome
#   removes the env/PATH dependency entirely.
#
# * --disable-gpu — Android/Termux Vulkan init flakes (CheckVkSuccessImpl →
#   VulkanError) and crashes the browser on startup. Headless CDP does not need
#   GPU. Also passes --use-gl=swiftshader and --disable-features=Vulkan 作为
#   belt-and-suspenders.
#
# * NEVER use `pkill -f "chromium.*9222"` inside this script. When Hermes runs
#   the launcher via `bash -c`, the shell's own cmdline contains that pattern →
#   pkill matches and SIGKILLs its own parent before reaching Chromium. We kill
#   a previous instance by the PID we persist to disk, plus /proc/exe sweep.

LOG="$HOME/.hermes/run/browser-pw/chrome.log"
PIDFILE="$HOME/.hermes/run/browser-pw/chrome.pid"
ACTIVITYFILE="$HOME/.hermes/run/browser-pw/last-activity"
IDLE_TIMEOUT="${BROWSER_IDLE_TIMEOUT:-600}"  # 10 minutes default
CHROME_BIN="/data/data/com.termux/files/usr/lib/chromium/chrome"
RUN_DIR="$HOME/.hermes/run/browser-pw"
# Persistent profile dir (2026-10-01): without an explicit --user-data-dir,
# headless Chromium creates a throwaway org.chromium.Chromium.scoped_dir.*
# profile in $PREFIX/tmp (~200MB per launch — the component updater
# re-downloads TranslateKit/crx_cache/etc. on EVERY launch), and since we only
# ever kill Chromium with SIGKILL it can never clean up after itself:
# 478 orphan dirs / 38G accumulated until the disk filled (2026-09-30).
# A persistent profile stops the leak AND caches components once for all
# future launches.
PROFILE_DIR="$RUN_DIR/chrome-profile"
mkdir -p "$RUN_DIR" "$PROFILE_DIR"

# Self-heal the idle watchdog (2026-10-01). Without the Hermes gateway (its
# cron ticker is frozen), system crond is the ONLY thing that kills an idle
# Chromium — if it died after a reboot, a launched browser would sit in RAM
# for days and re-leak scoped_dir profiles. This is NOT the forbidden `(...)&
# watchdog fork: it is a synchronous call to an idempotent script that
# backgrounds crond via setsid+nohup+disown and exits in <=2s, BEFORE
# Chromium even starts.
bash "$HOME/.hermes/scripts/browser-watchdog-launcher.sh" >/dev/null 2>&1 || true

# Kill a previous Chromium we launched (exact by PID from disk — no pkill -f).
OLD_PID=$(cat "$PIDFILE" 2>/dev/null)
if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
  kill -9 "$OLD_PID" 2>/dev/null
  for op in $(pgrep -P "$OLD_PID" 2>/dev/null); do
    kill -9 "$op" 2>/dev/null
  done
fi
sleep 1

# Launch Chromium in its own session, fully detached.
# --remote-debugging-port=9222 exposes CDP for Playwright connect_over_cdp.
setsid nohup "$CHROME_BIN" \
  --headless=new \
  --user-data-dir="$PROFILE_DIR" \
  --user-agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36" \
  --disable-blink-features=AutomationControlled \
  --no-sandbox \
  --disable-gpu \
  --disable-dev-shm-usage \
  --disable-software-rasterizer \
  --use-gl=swiftshader \
  --remote-debugging-port=9222 \
  --remote-debugging-targets=page \
  --remote-allow-origins=* \
  --no-first-run \
  --disable-features=Vulkan,VulkanFromGPUFuture \
  --ignore-certificate-errors \
  --ignore-ssl-errors \
  about:blank \
  </dev/null >"$LOG" 2>&1 &

CHILD_PID=$!
disown $CHILD_PID
echo "$CHILD_PID" >"$PIDFILE"
echo "Chromium launched with PID $CHILD_PID"
