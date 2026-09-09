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
mkdir -p "$RUN_DIR"

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
