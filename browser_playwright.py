#!/usr/bin/env python3
"""Playwright-CDP browser tool for Termux. replaces agent-browser CDP path.

Local Termux Chromium → CDP :9222 → Playwright connect_over_cdp.

Public API:
    browser_navigate(url)       → {success, url, title, snapshot, element_count}
    browser_snapshot(full)      → {success, snapshot, element_count}
    browser_click(ref)          → {success, url} | {success, error}
    browser_type(ref, text)     → {success} | {success, error}
    browser_scroll(direction)   → {success} | {success, error}
    browser_press(key)          → {success} | {success, error}
    browser_back()              → {success, url} | {success, error}
    browser_console(expr)       → {success, result}
    browser_get_images()        → {success, images:[{src,alt}]}
    browser_close()             → {success}
    browser_vision(question)    → {success, answer}
"""

from __future__ import annotations

import atexit
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

try:
    from playwright.sync_api import (
        Browser,
        Error as PwError,
        Page,
        Playwright,
        sync_playwright,
    )
except Exception as exc:  # pragma: no cover - import guard
    raise SystemExit(
        f"playwright import failed: {exc}. Install with: pip install playwright"
    ) from exc

# --- module-level singleton state --------------------------------------------------

_playwright: Optional[Playwright] = None
_browser: Optional[Browser] = None
_context = None  # BrowserContext
_page: Optional[Page] = None
_chrome_pid: Optional[int] = None
_lock = threading.Lock()
_ref_map: Dict[str, Any] = {}  # ref -> locator/element handle snapshot for click/type

# config
CDP_URL = "http://127.0.0.1:9222"
CHROME_BIN = "/data/data/com.termux/files/usr/bin/chromium-browser"
CHROME_ARGS = [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--disable-gpu-sandbox",
    "--remote-debugging-port=9222",
    "--remote-allow-origins=*",
    "--remote-debugging-targets=page",  # expose page targets for CDP
    "--no-first-run",
    "--no-default-browser-check",
    "--ignore-certificate-errors",
    "--ignore-ssl-errors",
    "--disable-features=TranslateUI",
    "--disable-extensions",
    "--disable-background-networking",
]
IDLE_TIMEOUT_SEC = int(os.environ.get("BROWSER_IDLE_TIMEOUT", "600"))  # 10m default
_watchdog_thread: Optional[threading.Thread] = None
_last_activity: float = 0.0

# State dir persists across CLI subprocess calls.
_STATE_DIR = os.path.expanduser("~/.hermes/run/browser-pw")
_PID_FILE = os.path.join(_STATE_DIR, "chrome.pid")
_ACTIVITY_FILE = os.path.join(_STATE_DIR, "last-activity")
# Toolkit dir (since 2026-09-09: ~/.hermes/browser-tools/, resolved from this
# file's location — no hardcoded workspace path).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _read_pid() -> Optional[int]:
    """Read Chromium PID from disk (shared across CLI subprocess calls)."""
    try:
        with open(_PID_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None


def _write_pid(pid: int) -> None:
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(_PID_FILE, "w") as f:
        f.write(str(pid))


def _clear_pid() -> None:
    try:
        os.unlink(_PID_FILE)
    except FileNotFoundError:
        pass


def _touch_activity() -> None:
    """Write last-activity timestamp to disk (shared across CLI subprocess calls)."""
    global _last_activity
    _last_activity = time.time()
    os.makedirs(_STATE_DIR, exist_ok=True)
    with open(_ACTIVITY_FILE, "w") as f:
        f.write(str(_last_activity))


def _read_activity() -> float:
    try:
        with open(_ACTIVITY_FILE) as f:
            return float(f.read().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


# --- chrome lifecycle --------------------------------------------------------------

def _cdp_ready(timeout: float = 2.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _spawn_chrome() -> Optional[int]:
    """Start Termux Chromium detached so it survives our CLI subprocess exit.

    Uses an external launcher script (launch-chrome.sh) that does:
      setsid nohup chromium ... </dev/null >log 2>&1 & ; disown

    The launcher script self-daemonizes Chromium into its own session with
    nohup + disown, so Android does NOT kill it when our Python CLI exits.

    CRITICAL: we call proc.wait() to block until the launcher script finishes
    its `disown` step (~3-5s). If we exit Python before the launcher has
    detached Chromium, Hermes kills the entire process tree including the
    not-yet-detached Chromium. Waiting for the launcher (but NOT for Chromium)
    is the safe window.
    """
    global _chrome_pid
    os.makedirs(_STATE_DIR, exist_ok=True)
    launcher = os.path.join(_SCRIPT_DIR, "launch-chrome.sh")

    proc = subprocess.Popen(
        ["bash", launcher],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )

    # Block until launcher script finishes (detaches Chromium + writes PID).
    # The launcher's setsid+nohup+disown puts Chromium in its own session,
    # so when our Python exits later, Android will NOT kill Chromium.
    try:
        out, _ = proc.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None

    # Wait for CDP readiness, then read PID from disk
    deadline = time.time() + 15
    while time.time() < deadline:
        if _cdp_ready(timeout=1.0):
            pid = _read_pid()
            if pid is not None and pid > 0:
                _chrome_pid = pid
                _touch_activity()
                return pid
        time.sleep(0.5)

    return None


def _chrome_exe_pids() -> List[int]:
    """Find Chromium PIDs by resolving /proc/<pid>/exe.

    Self-match-free: a python/bash process can never have chrome as its exe,
    unlike `pkill -f "chromium.*9222"` which matches our own cmdline and can
    SIGKILL the calling shell before reaching Chromium.
    """
    pids: List[int] = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                exe = os.readlink(f"/proc/{entry}/exe")
            except (OSError, PermissionError):
                continue
            if exe.endswith("/chromium/chrome") or exe.endswith("/chrome_crashpad_handler"):
                pids.append(int(entry))
    except Exception:
        pass
    return pids


def _find_chrome_main() -> Optional[int]:
    """Return the PID of the main Chromium browser process (not a renderer)."""
    for pid in _chrome_exe_pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().decode(errors="replace").split("\0")
        except (OSError, PermissionError):
            continue
        joined = " ".join(args)
        if "--remote-debugging-port" in joined and "--type=" not in joined:
            return pid
    return None


def _chrome_start_time() -> Optional[float]:
    """Wall-clock start time of the main Chromium process, as a UNIX timestamp.

    Used as an idle-baseline fallback when last-activity is missing, so an
    already-orphaned Chromium is charged its real age instead of getting a
    fresh reprieve on every call.

    IMPORTANT (Android/Termux): we use `ps -o etimes=` rather than computing
    from /proc/<pid>/stat starttime. Android blocks reads of BOTH /proc/uptime
    and /proc/stat (PermissionError: Errno 13), so the usual
    "boot_time + starttime/HZ" formula cannot work here — it silently returned
    None and left the idle check disabled.
    """
    pid = _find_chrome_main()
    if pid is None:
        return None
    # Primary: ps elapsed-seconds (works on Termux where /proc/uptime is denied)
    try:
        out = subprocess.check_output(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode().strip()
        if out:
            return time.time() - int(out.split()[0])
    except Exception:
        pass
    # Fallback: /proc/stat btime + starttime ticks (non-Android hosts)
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read()
        fields = stat[stat.rfind(")") + 2:].split()
        starttime_ticks = float(fields[19])
        hertz = os.sysconf("SC_CLK_TCK")
        btime = None
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime"):
                    btime = float(line.split()[1])
                    break
        if btime is not None:
            return btime + (starttime_ticks / hertz)
    except Exception:
        pass
    return None


def _kill_chrome() -> None:
    """Kill Chromium by PID from disk file. Shared across CLI subprocess calls."""
    global _chrome_pid
    pid = _chrome_pid or _read_pid() or _find_chrome_main()
    _chrome_pid = None
    _clear_pid()
    try:
        os.unlink(_ACTIVITY_FILE)
    except FileNotFoundError:
        pass
    except Exception:
        pass
    if pid is not None:
        # children first (renderers / GPU / utility)
        try:
            out = subprocess.check_output(
                ["pgrep", "-P", str(pid)], stderr=subprocess.DEVNULL
            ).decode().split()
            for child in out:
                try:
                    os.kill(int(child), 9)
                except Exception:
                    pass
        except Exception:
            pass
        # then the whole process group (setsid gives Chromium its own PGID)
        try:
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            pass
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        except Exception:
            pass
    # Final sweep: any surviving chrome process, found by exe (never self-matches).
    # Replaces the old `pkill -f "chromium.*9222"`, which could SIGKILL our own
    # shell because the pattern appeared in its cmdline.
    for stray in _chrome_exe_pids():
        try:
            os.kill(stray, 9)
        except Exception:
            pass


# --- playwright lifecycle ---------------------------------------------------------

def _pw_start() -> None:
    global _playwright, _browser, _context, _page
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.connect_over_cdp(CDP_URL)
    # Use the first existing context or create one
    if _browser.contexts:
        _context = _browser.contexts[0]
    else:
        _context = _browser.new_context()
    # Reuse existing page if present, else create one
    if _context.pages:
        _page = _context.pages[-1]
    else:
        _page = _context.new_page()


def _pw_stop() -> None:
    global _playwright, _browser, _context, _page
    # NOTE: we deliberately do NOT call _page.close() here. When connected via
    # connect_over_cdp to a *persistent* Termux Chromium instance, closing the
    # page would kill the tab and the URL state would be lost between CLI
    # invocations (each browser_tool.py call is a separate subprocess). We only
    # tear down the Playwright <-> CDP binding; Chromium itself keeps running
    # with its tab(s) intact so the next CLI process resumes the same session.
    _page = None
    if _browser is not None:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright is not None:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None
    _context = None


import atexit  # noqa: E402

# CRITICAL: On process exit, Python's GC runs __del__ on Playwright objects,
# which can kill the persistent Chromium instance abruptly. Registering
# _pw_stop via atexit guarantees a *clean* CDP disconnect (Chromium survives)
# instead of an implicit/dirty teardown that Android may propagate as SIGKILL
# to the detached Chromium child. Verified: with atexit, Chromium survives CLI
# subprocess exit; without it, Chromium is reaped.
atexit.register(_pw_stop)


# --- watchdog ----------------------------------------------------------------------

# NOTE: The old in-process watchdog thread is removed. In the subprocess-per-call
# CLI model, threads die with the process. Auto-stop is now handled in
# _connect(): every CLI call checks last-activity timestamp on disk and kills
# Chromium if idle > IDLE_TIMEOUT_SEC. No separate watcher process needed.


def _ensure_watchdog() -> None:
    """No-op — auto-stop is handled in _connect() via disk-based timestamps."""
    pass


# --- public api --------------------------------------------------------------------

def _connect() -> Page:
    """Ensure a live CDP connection. Returns Page.

    Implements lazy-start + auto-stop:
    - If Chromium is alive (CDP responding) → reuse it
    - If idle > IDLE_TIMEOUT_SEC → kill Chromium, lazy-start fresh
    - If no Chromium running → lazy-start via setsid+Popen (survives CLI exit)
    """
    global _page, _browser, _chrome_pid
    with _lock:
        # Check idle timeout — kill Chromium if idle too long.
        # NOTE: if the activity file is missing but Chromium is alive, we must
        # NOT skip this check. That was a real bug: a previous kill removed
        # last-activity while Chromium survived, so idle stayed unknown and an
        # orphaned Chromium (PPID=1) lived for 14+ hours holding ~80MB RSS.
        # Fall back to the process's own age when we have no timestamp.
        last_activity = _read_activity()
        if last_activity <= 0 and _cdp_ready(timeout=1.0):
            last_activity = _chrome_start_time() or 0.0
            if last_activity > 0:
                # persist so the cron watchdog sees it too
                try:
                    os.makedirs(_STATE_DIR, exist_ok=True)
                    with open(_ACTIVITY_FILE, "w") as f:
                        f.write(str(last_activity))
                except Exception:
                    pass
        if last_activity > 0:
            idle = time.time() - last_activity
            if idle > IDLE_TIMEOUT_SEC:
                _pw_stop()
                _kill_chrome()

        # Already connected in this process?
        if _page is not None and _browser is not None:
            try:
                _page.evaluate("1+1")
                _touch_activity()
                return _page
            except Exception:
                _pw_stop()

        # Is Chromium alive (CDP responding)?
        if _cdp_ready(timeout=2.0):
            # Chromium alive — find PID via /proc/exe (self-match-free)
            pid = _find_chrome_main()
            if pid is not None:
                _chrome_pid = pid
                _write_pid(pid)
        else:
            # Chromium not running → lazy-start via setsid+Popen
            pid = _spawn_chrome()
            if pid is None:
                raise RuntimeError("Failed to start local Chromium for CDP")
            _chrome_pid = pid

        _pw_start()
        _touch_activity()
        return _page


def _snapshot_and_tag() -> List[Dict[str, str]]:
    """Walk DOM, assign data-pw-ref attributes, return list for snapshot text."""
    page = _page
    if page is None:
        return []
    js = """
    (() => {
      const INTERACTIVE_ROLES = new Set([
        'button','link','textbox','checkbox','radio','combobox','listbox',
        'menuitem','menuitemcheckbox','menuitemradio','slider','switch',
        'searchbox','spinbutton','tab','treeitem','navigation','form',
      ]);
      const FILLABLE_TAGS = new Set(['INPUT','SELECT','TEXTAREA']);
      const SEMANTIC_TAGS = new Set(['H1','H2','H3','H4','H5','H6','NAV','FORM','IMG','BUTTON','A','SELECT','TEXTAREA','INPUT']);
      const seenNodes = new Set();
      const result = [];

      function roleOf(el){
        if (!el || el.nodeType !== 1) return '';
        const r = el.getAttribute('role');
        if (r && r.trim()) return r.trim().toLowerCase();
        const tag = el.tagName.toLowerCase();
        if (tag === 'a' && el.hasAttribute('href')) return 'link';
        if (tag === 'button' || (tag==='input' && /^(button|submit|reset|image)$/.test(el.type||''))) return 'button';
        if (tag==='input') return 'textbox';
        if (tag==='select') return 'combobox';
        if (tag==='textarea') return 'textbox';
        if (/^h[1-6]$/.test(tag)) return 'heading';
        if (tag==='nav') return 'navigation';
        if (tag==='form') return 'form';
        if (tag==='li') return 'listitem';
        return '';
      }

      function nameOf(el){
        const labels = [];
        if (!el) return '';
        const aria = el.getAttribute('aria-label');
        if (aria) labels.push(aria);
        const ph = el.getAttribute('placeholder');
        if (ph) labels.push(ph);
        const txt = (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim();
        if (txt && txt.length<=200) labels.push(txt.substring(0,200));
        const val = el.value;
        if (val && typeof val==='string') labels.push(val);
        const alt = el.getAttribute('alt');
        if (alt) labels.push(alt);
        return labels.join(' ').trim().substring(0,200);
      }

      function walk(el, depth){
        if (!el || el.nodeType !== 1 || depth>50) return;
        if (seenNodes.has(el)) return;
        seenNodes.add(el);
        const role = roleOf(el);
        const name = nameOf(el);
        const tag = el.tagName.toLowerCase();
        const isBody = el===document.body;
        const isHeading = /^h[1-6]$/.test(tag);
        const isLink = tag==='a' && el.hasAttribute('href');
        const isInteractive = INTERACTIVE_ROLES.has(role) || isLink
          || FILLABLE_TAGS.has(el.tagName) || /^h[1-6]$/.test(tag)
          || tag==='button' || tag==='select' || tag==='textarea'
          || tag==='img' || tag==='nav';
        if ((isHeading || isInteractive) && name) {  // exclude body to keep snapshot compact
          result.push({ role, name, tag, href: el.getAttribute('href')||'', placeholder: el.getAttribute('placeholder')||'', level: isHeading ? parseInt(tag[1]) : 0, _el: el });
        }
        const children = el.children;
        for (let i=0; i<children.length && result.length<500; i++) walk(children[i], depth+1);
      }
      walk(document.body, 0);
      // assign ref ids e1..en and tag DOM elements with data-pw-ref attribute
      result.forEach((n, i) => {
        const ref = 'e'+(i+1);
        n.ref = ref;
        try { n._el.setAttribute('data-pw-ref', ref); } catch(e) {}
        delete n._el;  // cannot serialize DOM nodes
      });
      return result;
    })();
    """
    try:
        raw = page.evaluate(js)
    except Exception as exc:
        return []
    # Note: _el was deleted in JS, so we can clean fields
    for n in raw:
        n.pop('_el', None)
    return raw or []


def _snapshot_text() -> str:
    """Generate text-based accessibility tree with refs like agent-browser snapshot."""
    global _ref_map
    elems = _snapshot_and_tag()
    _ref_map = {}
    lines = []
    for el in elems:
        ref = el.get("ref", "")
        role = el.get("role", "") or ""
        name = (el.get("name") or "").strip()
        href = el.get("href") or ""
        placeholder = el.get("placeholder") or ""
        level = el.get("level", 0)
        _ref_map[ref] = el
        if role == "heading":
            lines.append(f'- heading "{name}" [level={level}] [ref={ref}]')
        elif role == "link":
            lines.append(f'- link "{name[:80]}" [ref={ref}]')
        elif role in ("textbox", "combobox", "searchbox"):
            text = name[:80] if name else placeholder
            lines.append(f'- {role} "{text}" [ref={ref}]')
        elif role == "button":
            lines.append(f'- button "{name}" [ref={ref}]')
        else:
            if name or role:
                lines.append(f'- {role} "{name}" [ref={ref}]')
    if not lines:
        return "- (empty page)"
    return "\n".join(lines)


def _find_el(ref: str):
    """Return Playwright Locator for ref via data-pw-ref DOM attribute.

    If DOM elements are not tagged yet (fresh process, refs loaded from disk),
    re-run _snapshot_text() to tag the current DOM before resolving.
    """
    page = _page
    if page is None:
        return None
    if not ref:
        return None
    # ensure refs are loaded (from disk in CLI mode, or in-memory in lib mode)
    if not _ref_map:
        _snapshot_text()  # tags DOM with data-pw-ref AND populates _ref_map
    if ref not in _ref_map:
        return None
    # Check if DOM still has our data-pw-ref attributes. If page navigated
    # since tagging, the attributes are gone — re-tag.
    try:
        has_attr = page.evaluate(
            f"document.querySelector('[data-pw-ref=\"{ref}\"]') !== null"
        )
    except Exception:
        has_attr = False
    if not has_attr:
        # DOM changed (navigation, dynamic update) — re-tag
        _snapshot_text()
        if ref not in _ref_map:
            return None
    return page.locator(f"[data-pw-ref='{ref}']").first


# --- public functions --------------------------------------------------------------

def browser_navigate(url: str, task_id: Optional[str] = None) -> str:
    import json as _j
    try:
        page = _connect()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        snap = _snapshot_text()
        title = page.title()
        return _j.dumps({
            "success": True,
            "url": page.url,
            "title": title,
            "snapshot": snap,
            "element_count": len(_ref_map),
        }, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_snapshot(full: bool = False, task_id: Optional[str] = None) -> str:
    import json as _j
    try:
        page = _connect()
        snap = _snapshot_text()
        return _j.dumps({
            "success": True,
            "url": page.url,
            "title": page.title() if not page.url.startswith("chrome") else "",
            "snapshot": snap,
            "element_count": len(_ref_map),
        }, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_click(ref: str, task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        loc = _find_el(ref)
        if loc is None:
            return _j.dumps({"success": False, "error": f"Unknown ref: {ref}"}, ensure_ascii=False)
        loc.click(timeout=10000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        return _j.dumps({"success": True, "url": page.url}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_type(ref: str, text: str, task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        loc = _find_el(ref)
        if loc is None:
            return _j.dumps({"success": False, "error": f"Unknown ref: {ref}"}, ensure_ascii=False)
        loc.fill(text, timeout=10000)
        return _j.dumps({"success": True}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_scroll(direction: str = "down", task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        if direction == "down":
            page.evaluate("window.scrollBy(0, window.innerHeight)")
        elif direction == "up":
            page.evaluate("window.scrollBy(0, -window.innerHeight)")
        else:
            return _j.dumps({"success": False, "error": f"Unknown direction: {direction}"}, ensure_ascii=False)
        return _j.dumps({"success": True}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_back(task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        # Termux Chromium: go_back(wait_until="domcontentloaded") таймаутит,
        # хотя переход фактически выполняется. Делаем go_back без ожидания и
        # подтверждаем результат по фактическому URL страницы.
        page.go_back()
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        return _j.dumps({"success": True, "url": page.url}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_press(key: str, task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        page.keyboard.press(key)
        return _j.dumps({"success": True}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_console(clear: bool = False, expression: Optional[str] = None, task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        if expression is not None:
            result = page.evaluate(expression)
            return _j.dumps({"success": True, "result": result}, ensure_ascii=False, default=str)
        return _j.dumps({"success": True, "logs": []}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_get_images(task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        imgs = page.evaluate(
            """() => Array.from(document.querySelectorAll('img')).map(i => ({src: i.src||'', alt: i.alt||''})).filter(x => x.src)"""
        )
        return _j.dumps({"success": True, "images": imgs}, ensure_ascii=False, default=str)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_vision(question: str, annotate: bool = False, task_id: Optional[str] = None) -> str:
    try:
        import json as _j
        page = _connect()
        # screenshot → vision_analyze
        png = page.screenshot(type="png", full_page=False)
        # write to temp file
        import tempfile, base64
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(png)
            path = f.name
        # call vision tool via subprocess (Hermes vision tool)
        try:
            from vision_tool import vision_analyze
            answer = vision_analyze(path, question)
        except Exception:
            answer = "(vision tool unavailable)"
        return _j.dumps({"success": True, "answer": answer}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_screenshot(full_page: bool = False, output_path: Optional[str] = None, task_id: Optional[str] = None) -> str:
    """Take a screenshot and save to disk. Returns JSON with file path in data dict."""
    try:
        import json as _j
        page = _connect()
        png = page.screenshot(type="png", full_page=full_page)
        if output_path:
            # Use the caller-specified path (e.g. Hermes cache/screenshots/)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            fpath = output_path
        else:
            # Fallback: save to persistent state dir
            os.makedirs(_STATE_DIR, exist_ok=True)
            import time
            fname = f"screenshot_{int(time.time()*1000)}.png"
            fpath = os.path.join(_STATE_DIR, fname)
        with open(fpath, "wb") as f:
            f.write(png)
        return _j.dumps({"success": True, "data": {"path": fpath}}, ensure_ascii=False)
    except Exception as exc:
        return _j.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def browser_close(task_id: Optional[str] = None) -> str:
    global _page, _browser, _chrome_pid
    with _lock:
        _pw_stop()
        _kill_chrome()
    return _j.dumps({"success": True, "message": "Browser closed"}, ensure_ascii=False)


# Register atexit cleanup
# IMPORTANT: Do NOT kill Chromium on CLI exit — it must persist in its own session
# for the next CLI call. Only disconnect Playwright gracefully.
atexit.register(_pw_stop)
