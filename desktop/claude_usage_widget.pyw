#!/usr/bin/env python3
"""
AI 사용량 위젯 v3 — Claude + ChatGPT 링 게이지 (Windows / macOS)

각 서비스마다 링 두 개:
  Claude   바깥 = 현재 세션(5시간), 안쪽 = 주간 한도
  ChatGPT  바깥 = 짧은 주기 한도(있을 때), 안쪽 = 주간 한도  (Codex 사용량 페이지와 같은 값)
링 위의 흰 점은 "리셋 주기 중 지금까지 흐른 시간"이에요.

- 드래그: 창 이동 / 더블클릭: 미니 ↔ 자세히 / ↻ 버튼: 새로고침
- 우클릭 (Mac은 Control+클릭): 메뉴

필요: pip install pillow curl_cffi

주의: 두 서비스 모두 공식 API가 아니라 각 사이트의 사용량 페이지가 내부적으로 쓰는
주소를 불러와요. 사이트 구조가 바뀌면 작동하지 않을 수 있어요.
세션 키·토큰은 로그인 정보와 같으니 다른 사람과 공유하지 마세요.
"""

import base64
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog

try:
    from PIL import Image, ImageDraw, ImageFilter
except ImportError:
    _r = tk.Tk()
    _r.withdraw()
    messagebox.showerror("AI 사용량",
                         "Pillow가 필요해요.\n터미널에서 'pip install pillow'를 실행한 뒤 다시 켜 주세요.")
    sys.exit(1)

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

# ─── 설정 ──────────────────────────────────────────────
CONFIG_PATH = Path.home() / ".claude_usage_widget.json"   # 이전 버전 설정을 그대로 이어 써요
REFRESH_SEC = 120

PANEL = "#161A23"
BORDER = "#2A3142"
TRACK = "#242A37"
FG = "#EEF1F6"
MUTED = "#8A93A6"
DIM = "#5C6578"
GOOD = "#6EE7A8"
BAD = "#FF5C7A"
BTN_BG = "#222836"
BTN_HOVER = "#313A4D"

PROVIDERS = ("claude", "gpt")
NAMES = {"claude": "Claude", "gpt": "ChatGPT"}
MINI_NAMES = {"claude": "Claude", "gpt": "GPT"}
PALETTES = {                                   # [바깥 링, 안쪽 링] 각각 (시작색, 끝색)
    "claude": [("#5EE7DF", "#3FA3FF"), ("#B18CFF", "#FF7EB6")],
    "gpt": [("#C2F27C", "#2FD48F"), ("#8FE3FF", "#8B7CFF")],
}
WARN_PALETTE = ("#FFD36E", "#FF9F43")      # 70% 이상
DANGER_PALETTE = ("#FF8A7A", "#FF3D6E")    # 90% 이상

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


# ─── 오류 종류 ─────────────────────────────────────────
class FetchError(Exception):
    pass


class AuthError(FetchError):
    pass


class BlockedError(FetchError):
    pass


class NotFoundError(FetchError):
    pass


class NotConfigured(FetchError):
    pass


# ─── 설정 파일 ─────────────────────────────────────────
def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name == "posix":
        os.chmod(CONFIG_PATH, 0o600)


# ─── 네트워크 공통 ─────────────────────────────────────
def http_get_json(url, headers=None, cookies=None):
    hdrs = {"Accept": "application/json", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    hdrs.update(headers or {})
    cookies = cookies or {}
    if cffi_requests:
        try:
            r = cffi_requests.get(url, headers=hdrs, cookies=cookies, impersonate="chrome", timeout=20)
        except Exception as e:
            raise FetchError(f"네트워크 오류: {e}")
        status, text = r.status_code, r.text
    else:
        import urllib.error
        import urllib.request

        hdrs["User-Agent"] = BROWSER_UA
        if cookies:
            hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status, text = resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise FetchError(f"네트워크 연결을 확인하세요 ({e.reason})")

    if status == 200:
        try:
            return json.loads(text)
        except ValueError:
            raise FetchError("응답을 해석하지 못했어요")
    head = text[:3000].lower()
    if status in (403, 503) and ("just a moment" in head or "cloudflare" in head):
        raise BlockedError("요청이 차단됐어요 · 'pip install curl_cffi' 후 다시 실행해 보세요")
    if status in (401, 403):
        raise AuthError("인증 실패")
    if status == 404:
        raise NotFoundError("찾을 수 없음")
    raise FetchError(f"서버 응답 오류 (HTTP {status})")


# ─── 시간 계산 ─────────────────────────────────────────
def parse_time(v):
    """ISO 문자열 또는 유닉스 초 → datetime(UTC)"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        try:
            return datetime.fromtimestamp(float(v), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_reset(dt):
    if not dt:
        return ""
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "곧 초기화"
    if secs < 24 * 3600:
        h, m = int(secs // 3600), int(secs % 3600 // 60)
        return f"{h}시간 {m}분 후 초기화" if h else f"{m}분 후 초기화"
    local = dt.astimezone()
    wd = "월화수목금토일"[local.weekday()]
    return f"{local.month}/{local.day}({wd}) {local:%H:%M} 초기화"


def time_progress(win):
    dt, secs = win.get("reset"), win.get("secs")
    if not dt or not secs:
        return None
    remaining = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, min(1.0, 1 - remaining / secs))


def pace(pct, tp):
    if tp is None or tp < 0.1:
        return None
    diff = pct - tp * 100
    if diff > 8:
        return "빠름", BAD
    if diff < -8:
        return "여유", GOOD
    return "적정", MUTED


def window_names(secs):
    """주기 길이(초) → (긴 이름, 짧은 이름)"""
    if not secs:
        return "사용 한도", "한도"
    if abs(secs - 7 * 86400) < 3600:
        return "주간 한도", "주간"
    if secs < 86400:
        h = round(secs / 3600)
        return f"{h}시간 한도", f"{h}시간"
    d = round(secs / 86400)
    return f"{d}일 한도", f"{d}일"


# ─── Claude ───────────────────────────────────────────
CLAUDE_BASE = "https://claude.ai/api"
CLAUDE_MAIN = {"five_hour": ("현재 세션", "세션", 5 * 3600), "seven_day": ("주간 한도", "주간", 7 * 86400)}
CLAUDE_EXTRAS = {"seven_day_opus": "Opus", "seven_day_sonnet": "Sonnet"}


def claude_get(path, key):
    try:
        return http_get_json(f"{CLAUDE_BASE}{path}", headers={"Referer": "https://claude.ai/settings/usage"},
                             cookies={"sessionKey": key})
    except AuthError:
        raise AuthError("Claude 세션 키가 만료됐어요 · 우클릭 > Claude 세션 키 변경")


def fetch_claude(cfg):
    key = cfg.get("session_key")
    if not key:
        raise NotConfigured("연결 안 됨 · 우클릭 > Claude 세션 키 변경")
    org = cfg.get("org_id")
    for attempt in range(2):
        if not org:
            orgs = claude_get("/organizations", key)
            if not isinstance(orgs, list) or not orgs:
                raise FetchError("Claude 조직 정보를 찾지 못했어요")
            org = next((o["uuid"] for o in orgs if "chat" in (o.get("capabilities") or [])), orgs[0]["uuid"])
        try:
            data = claude_get(f"/organizations/{org}/usage", key)
            break
        except NotFoundError:
            if attempt == 1:
                raise FetchError("Claude 사용량 정보를 찾지 못했어요")
            org = None

    windows, extras = [], []
    for k, v in (data or {}).items():
        if not (isinstance(v, dict) and v.get("utilization") is not None):
            continue
        try:
            pct = float(v["utilization"])
        except (TypeError, ValueError):
            continue
        if k in CLAUDE_MAIN:
            label, short, secs = CLAUDE_MAIN[k]
            windows.append({"key": k, "label": label, "short": short, "pct": pct,
                            "reset": parse_time(v.get("resets_at")), "secs": secs})
        elif k in CLAUDE_EXTRAS:
            extras.append((CLAUDE_EXTRAS[k], pct))
    order = list(CLAUDE_MAIN)
    windows.sort(key=lambda w: order.index(w["key"]))
    if not windows:
        raise FetchError("Claude 사용량 항목이 비어 있어요 (구조가 바뀌었을 수 있어요)")
    return {"windows": windows, "extras": extras, "plan": None, "cfg_updates": {"org_id": org}}


# ─── ChatGPT ──────────────────────────────────────────
GPT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
GPT_SESSION_URL = "https://chatgpt.com/api/auth/session"
GPT_COOKIE = "__Secure-next-auth.session-token"


def codex_auth_path():
    home = os.environ.get("CODEX_HOME")
    return (Path(home) if home else Path.home() / ".codex") / "auth.json"


def jwt_claims(token):
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def account_from_claims(claims):
    auth = claims.get("https://api.openai.com/auth") or {}
    return auth.get("chatgpt_account_id")


def gpt_sources(cfg):
    """사용 가능한 인증 방법 (우선순위 순)"""
    srcs = []
    if codex_auth_path().exists():
        srcs.append("codex")
    if cfg.get("gpt_session_token"):
        srcs.append("cookie")
    return srcs


def gpt_token_from_codex():
    try:
        data = json.loads(codex_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AuthError("Codex 로그인 파일을 읽지 못했어요")
    tokens = data.get("tokens") or {}
    at = tokens.get("access_token")
    if not at:
        raise AuthError("Codex가 ChatGPT 계정으로 로그인되어 있지 않아요")
    claims = jwt_claims(at)
    if claims.get("exp") and claims["exp"] < time.time() + 60:
        raise AuthError("Codex 로그인이 만료됐어요 · 터미널에서 codex를 한 번 실행하세요")
    return at, tokens.get("account_id") or account_from_claims(claims), None


def gpt_token_from_cookie(cfg, force=False):
    cache = cfg.get("gpt_token_cache") or {}
    if not force and cache.get("access_token") and cache.get("exp", 0) > time.time() + 300:
        return cache["access_token"], cache.get("account_id"), None

    tok = cfg["gpt_session_token"]
    if len(tok) > 3900:  # 브라우저처럼 .0 .1 조각으로 나눠 보내기
        cookies = {f"{GPT_COOKIE}.{i}": tok[j:j + 3936] for i, j in enumerate(range(0, len(tok), 3936))}
    else:
        cookies = {GPT_COOKIE: tok}
    try:
        data = http_get_json(GPT_SESSION_URL, headers={"Referer": "https://chatgpt.com/"}, cookies=cookies)
    except (AuthError, NotFoundError):
        data = {}
    at = (data or {}).get("accessToken")
    if not at:
        raise AuthError("ChatGPT 세션 토큰이 만료됐어요 · 우클릭 > ChatGPT 연결 설정")
    claims = jwt_claims(at)
    acct = account_from_claims(claims)
    upd = {"gpt_token_cache": {"access_token": at, "account_id": acct,
                               "exp": claims.get("exp", time.time() + 3600)}}
    return at, acct, upd


def parse_gpt(data):
    windows = []
    rl = (data or {}).get("rate_limit") or {}
    for slot in ("primary_window", "secondary_window"):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        try:
            if w.get("used_percent") is not None:
                pct = float(w["used_percent"])
            elif w.get("remaining_percent") is not None:
                pct = 100 - float(w["remaining_percent"])
            else:
                continue
        except (TypeError, ValueError):
            continue
        try:
            secs = int(float(w.get("limit_window_seconds") or 0)) or None
        except (TypeError, ValueError):
            secs = None
        reset = parse_time(w.get("reset_at"))
        if reset is None and w.get("reset_after_seconds") is not None:
            try:
                reset = datetime.now(timezone.utc) + timedelta(seconds=float(w["reset_after_seconds"]))
            except (TypeError, ValueError):
                pass
        label, short = window_names(secs)
        windows.append({"key": slot, "label": label, "short": short, "pct": pct, "reset": reset, "secs": secs})
    windows.sort(key=lambda w: w["secs"] or 10 ** 9)  # 짧은 주기가 바깥 링

    extras = []
    cr = ((data or {}).get("code_review_rate_limit") or {}).get("primary_window")
    if isinstance(cr, dict) and cr.get("used_percent") is not None:
        try:
            extras.append(("코드 리뷰", float(cr["used_percent"])))
        except (TypeError, ValueError):
            pass

    if not windows:
        raise FetchError("ChatGPT 사용량 항목이 없어요 (Codex 한도가 없는 요금제일 수 있어요)")
    plan = (data or {}).get("plan_type")
    return {"windows": windows, "extras": extras, "plan": plan.capitalize() if isinstance(plan, str) else None}


def fetch_gpt(cfg):
    srcs = gpt_sources(cfg)
    if not srcs:
        raise NotConfigured("연결 안 됨 · 우클릭 > ChatGPT 연결 설정")
    last = None
    for src in srcs:
        for force in ((False,) if src == "codex" else (False, True)):
            try:
                if src == "codex":
                    at, acct, upd = gpt_token_from_codex()
                else:
                    at, acct, upd = gpt_token_from_cookie(cfg, force=force)
                headers = {"Authorization": f"Bearer {at}", "Referer": "https://chatgpt.com/codex/settings/usage"}
                if acct:
                    headers["ChatGPT-Account-Id"] = acct
                try:
                    data = http_get_json(GPT_USAGE_URL, headers=headers)
                except AuthError:
                    raise AuthError("Codex 로그인이 만료됐어요 · 터미널에서 codex를 한 번 실행하세요"
                                    if src == "codex" else
                                    "ChatGPT 세션 토큰이 만료됐어요 · 우클릭 > ChatGPT 연결 설정")
                except NotFoundError:
                    raise FetchError("ChatGPT 사용량 정보를 찾지 못했어요")
                result = parse_gpt(data)
                result["cfg_updates"] = upd or {}
                return result
            except AuthError as e:
                last = e
    raise last


FETCHERS = {"claude": fetch_claude, "gpt": fetch_gpt}


# ─── 그리기 ───────────────────────────────────────────
def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lerp(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def palette_for(provider, slot, pct):
    if pct >= 90:
        return DANGER_PALETTE
    if pct >= 70:
        return WARN_PALETTE
    return PALETTES[provider][slot]


def render_rings(size, rings, ss=3):
    n = size * ss
    c = n / 2
    base = Image.new("RGB", (n, n), hex_rgb(PANEL))
    draw = ImageDraw.Draw(base)

    def box(r, w):
        ro = r + w / 2
        return [c - ro, c - ro, c + ro, c + ro]

    def point(r, ang):
        a = math.radians(ang)
        return c + r * math.cos(a), c + r * math.sin(a)

    for rg in rings:
        r, w = rg["radius"] * ss, rg["width"] * ss
        draw.arc(box(r, w), 0, 360, fill=hex_rgb(TRACK), width=int(w))

    fills = []
    for rg in rings:
        frac = max(0.0, min(1.0, rg["frac"]))
        if frac <= 0.002:
            continue
        r, w = rg["radius"] * ss, rg["width"] * ss
        c1, c2 = (hex_rgb(x) for x in rg["colors"])
        start, span = -90.0, 360.0 * frac
        end = start + span
        mask = Image.new("L", (n, n), 0)
        md = ImageDraw.Draw(mask)
        md.arc(box(r, w), start, end, fill=255, width=int(w))
        for ang in (start, end):
            x, y = point(r, ang)
            md.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=255)
        grad = Image.new("RGB", (n, n), c1)
        gd = ImageDraw.Draw(grad)
        steps = max(2, int(span / 3))
        for i in range(steps):
            a0 = start + span * i / steps
            gd.pieslice([0, 0, n, n], a0, a0 + span / steps + 1, fill=lerp(c1, c2, i / (steps - 1)))
        gd.pieslice([0, 0, n, n], end, end + 30, fill=c2)
        fills.append((rg, mask, grad))

    for rg, mask, grad in fills:
        k = 4
        small = mask.resize((n // k, n // k), Image.BILINEAR)
        blur = small.filter(ImageFilter.GaussianBlur(rg["width"] * ss * 0.5 / k))
        glow = blur.resize((n, n), Image.BILINEAR).point(lambda v: int(v * 0.55))
        base.paste(grad, (0, 0), glow)

    for rg, mask, grad in fills:
        base.paste(grad, (0, 0), mask)

    for rg in rings:
        if rg.get("marker") is None:
            continue
        r, w = rg["radius"] * ss, rg["width"] * ss
        x, y = point(r, -90 + 360 * rg["marker"])
        d = w * 0.2
        draw.ellipse([x - d, y - d, x + d, y + d], fill=(240, 244, 250))

    return base.resize((size, size), Image.LANCZOS)


def ring_geometry(tile):
    w = tile * 0.08
    margin = w * 1.1
    r1 = tile / 2 - w / 2 - margin
    r2 = r1 - w - tile * 0.03
    return (r1, w), (r2, w)


# ─── 위젯 ─────────────────────────────────────────────
class Widget:
    WIDTH = 340      # 자세히 보기 폭
    SEC_H = 160      # 서비스 한 칸 높이
    FOOT_H = 28
    TILE = 140
    MINI = 96

    def __init__(self):
        from PIL import ImageTk
        self.ImageTk = ImageTk

        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("AI 사용량")

        if not self.cfg.get("session_key") and not gpt_sources(self.cfg):
            claude_ok = self.ask_claude_key()
            gpt_ok = self.ask_gpt_token()
            if not (claude_ok or gpt_ok):
                messagebox.showinfo("AI 사용량", "연결된 서비스가 없어서 종료할게요.")
                sys.exit(0)
            self.cfg["show_claude"], self.cfg["show_gpt"] = claude_ok, gpt_ok
            save_config(self.cfg)

        self.S = self.root.winfo_fpixels("1i") / 96 if IS_WIN else 1.0
        fams = set(tkfont.families(self.root))

        def pick(cands, fallback):
            return next((f for f in cands if f in fams), fallback)

        if IS_MAC:
            self.num_font = pick(["Avenir Next", "Helvetica Neue"], "Helvetica")
            self.txt_font = pick(["Apple SD Gothic Neo"], "Helvetica")
        else:
            self.num_font = pick(["Segoe UI Variable Display", "Segoe UI"], "Arial")
            self.txt_font = pick(["Malgun Gothic", "맑은 고딕"], "Arial")

        self.q = queue.Queue()
        self.data = {p: {"windows": [], "extras": [], "plan": None, "error": None, "notice": False}
                     for p in PROVIDERS}
        self.last_ok = None
        self.fetching = set()
        self.compact = self.cfg.get("compact", False)
        self.shown, self.target, self.anim_from = {}, {}, {}
        self.anim_t0 = 0.0
        self.anim_job = None
        self._drag = None
        self._press_btn = False
        self.hover_win = False
        self.hover_btn = False
        self.spin = 90.0
        self.spin_job = None
        self._size = None
        self._panel_cache = {}
        self._ring_cache = {}

        self.build()
        self.root.deiconify()
        self.round_corners_windows()
        self.fetch_async()
        self.root.after(200, self.poll_queue)
        self.root.after(30_000, self.tick)
        self.root.after(REFRESH_SEC * 1000, self.auto_refresh)

    # ── 표시할 서비스
    def providers(self):
        ps = [p for p in PROVIDERS if self.cfg.get(f"show_{p}", True)]
        return ps or ["claude"]

    # ── 창 구성
    def build(self):
        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", self.cfg.get("topmost", True))
        try:
            r.attributes("-alpha", 0.97)
        except tk.TclError:
            pass

        self.alpha_mode = False
        if IS_MAC:
            try:
                r.configure(bg="systemTransparent")
                r.attributes("-transparent", True)
                self.alpha_mode = True
            except tk.TclError:
                pass
        if not self.alpha_mode:
            r.configure(bg=PANEL)

        self.canvas = tk.Canvas(r, bg="systemTransparent" if self.alpha_mode else PANEL,
                                highlightthickness=0, bd=0)
        self.canvas.pack()
        r.geometry(f"+{self.cfg.get('x', 40)}+{self.cfg.get('y', 40)}")

        cv = self.canvas
        cv.bind("<ButtonPress-1>", self.drag_start)
        cv.bind("<B1-Motion>", self.drag_move)
        cv.bind("<ButtonRelease-1>", self.drag_end)
        cv.bind("<Double-Button-1>", self.toggle_compact)
        cv.bind("<Motion>", self.on_motion)
        cv.bind("<Enter>", lambda e: self.set_hover(win=True))
        cv.bind("<Leave>", lambda e: self.set_hover(win=False, btn=False))
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            cv.bind(seq, self.show_menu)

        self.menu = tk.Menu(r, tearoff=0)
        self.topmost_var = tk.BooleanVar(value=self.cfg.get("topmost", True))
        self.show_vars = {p: tk.BooleanVar(value=self.cfg.get(f"show_{p}", True)) for p in PROVIDERS}
        self.menu.add_command(label="지금 새로고침", command=self.fetch_async)
        self.menu.add_command(label="미니 / 자세히 보기", command=self.toggle_compact)
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.topmost_var,
                                  command=self.toggle_topmost)
        self.menu.add_separator()
        for p in PROVIDERS:
            self.menu.add_checkbutton(label=f"{NAMES[p]} 표시", variable=self.show_vars[p],
                                      command=lambda p=p: self.toggle_provider(p))
        self.menu.add_separator()
        self.menu.add_command(label="Claude 세션 키 변경", command=self.change_claude_key)
        self.menu.add_command(label="ChatGPT 연결 설정", command=self.change_gpt)
        self.menu.add_separator()
        self.menu.add_command(label="종료", command=r.destroy)

        self.redraw()

    def round_corners_windows(self):
        if not IS_WIN:
            return
        try:
            import ctypes
            self.root.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            rr, gg, bb = hex_rgb(BORDER)
            color = ctypes.c_uint(rr | (gg << 8) | (bb << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            pass

    def panel_photo(self, w, h):
        key = (w, h, self.alpha_mode)
        if key not in self._panel_cache:
            if self.alpha_mode:
                ss = 3
                img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
                ImageDraw.Draw(img).rounded_rectangle(
                    [0, 0, w * ss - 1, h * ss - 1], radius=int(18 * self.S * ss),
                    fill=hex_rgb(PANEL) + (255,), outline=hex_rgb(BORDER) + (255,),
                    width=max(1, int(self.S * ss)))
                img = img.resize((w, h), Image.LANCZOS)
            else:
                img = Image.new("RGB", (w, h), hex_rgb(PANEL))
            self._panel_cache[key] = self.ImageTk.PhotoImage(img)
        return self._panel_cache[key]

    # ── 크기 계산
    def dims(self):
        S, n = self.S, len(self.providers())
        if self.compact:
            return int(self.MINI * S * n), int(self.MINI * S)
        return int(self.WIDTH * S), int((self.SEC_H * n + self.FOOT_H) * S)

    def button_pos(self):
        S = self.S
        W, H = self.dims()
        if self.compact:
            return W - 14 * S, H - 14 * S, 8 * S
        return W - 18 * S, H - self.FOOT_H * S / 2 - 2 * S, 9 * S

    # ── 링 이미지
    def ring_photo(self, p, tile):
        geo = ring_geometry(tile)
        wins = self.data[p]["windows"][:2]
        specs = []
        for slot, (radius, width) in enumerate(geo):
            if slot < len(wins):
                w = wins[slot]
                pct = self.shown.get((p, w["key"]), 0.0)
                tp = time_progress(w)
                specs.append({"radius": radius, "width": width, "frac": pct / 100,
                              "colors": palette_for(p, slot, pct), "marker": tp})
            elif not wins or slot == 0:
                specs.append({"radius": radius, "width": width, "frac": 0,
                              "colors": PALETTES[p][slot], "marker": None})
        key = (p, tile, self.alpha_mode, tuple(
            (round(s["frac"], 3), s["colors"], None if s["marker"] is None else round(s["marker"], 3))
            for s in specs))
        if key not in self._ring_cache:
            if len(self._ring_cache) > 60:
                self._ring_cache.clear()
            img = render_rings(tile, specs)
            if self.alpha_mode:
                circle = Image.new("L", img.size, 0)
                ImageDraw.Draw(circle).ellipse([1, 1, tile - 2, tile - 2], fill=255)
                img = img.convert("RGBA")
                img.putalpha(circle)
            self._ring_cache[key] = self.ImageTk.PhotoImage(img)
        return self._ring_cache[key]

    # ── 화면 그리기
    def redraw(self):
        S, cv = self.S, self.canvas
        W, H = self.dims()
        cv.delete("all")
        if self._size != (W, H):
            self._size = (W, H)
            cv.config(width=W, height=H)
            self.root.geometry(f"{W}x{H}")
        cv.create_image(0, 0, anchor="nw", image=self.panel_photo(W, H))

        for i, p in enumerate(self.providers()):
            if self.compact:
                tile = int(self.MINI * S)
                tx, ty = i * tile, 0
            else:
                tile = int(self.TILE * S)
                y0 = int(i * self.SEC_H * S)
                tx, ty = int(4 * S), y0 + (int(self.SEC_H * S) - tile) // 2
                if i > 0:
                    cv.create_line(16 * S, y0, W - 16 * S, y0, fill=BORDER)
            cv.create_image(tx, ty, anchor="nw", image=self.ring_photo(p, tile))
            self.draw_center(p, tx + tile / 2, ty + tile / 2)
            if not self.compact:
                self.draw_section(p, int(i * self.SEC_H * S), tx + tile + int(4 * S), W)

        if not self.compact and self.last_ok:
            cv.create_text(W - 32 * S, H - self.FOOT_H * S / 2 - 2 * S, text=f"{self.last_ok:%H:%M} 업데이트",
                           anchor="e", fill=DIM, font=(self.txt_font, 8))
        self.draw_button()

    def draw_center(self, p, cx, cy):
        cv, S = self.canvas, self.S
        d = self.data[p]
        first = d["windows"][0] if d["windows"] else None
        if self.compact:
            if first:
                pct = self.shown.get((p, first["key"]), 0.0)
                cv.create_text(cx, cy - 5 * S, text=f"{pct:.0f}", fill=FG, font=(self.num_font, 13, "bold"))
            else:
                mark, col = ("!", BAD) if d["error"] and not d["notice"] else ("···" if p in self.fetching else "–", MUTED)
                cv.create_text(cx, cy - 5 * S, text=mark, fill=col, font=(self.num_font, 12, "bold"))
            cv.create_text(cx, cy + 10 * S, text=MINI_NAMES[p], fill=MUTED, font=(self.txt_font, 7))
            return
        if first:
            pct = self.shown.get((p, first["key"]), 0.0)
            cv.create_text(cx, cy - 6 * S, text=f"{pct:.0f}%", fill=FG, font=(self.num_font, 17, "bold"))
            cv.create_text(cx, cy + 14 * S, text=first["short"], fill=MUTED, font=(self.txt_font, 8))
        elif d["error"] and not d["notice"]:
            cv.create_text(cx, cy, text="!", fill=BAD, font=(self.num_font, 18, "bold"))
        else:
            cv.create_text(cx, cy, text="···" if p in self.fetching else "–", fill=MUTED,
                           font=(self.num_font, 14, "bold"))

    def draw_section(self, p, y0, x0, W):
        S, cv = self.S, self.canvas
        d = self.data[p]
        xr = W - 20 * S

        title = NAMES[p].upper() + (f"  ·  {d['plan'].upper()}" if d["plan"] else "")
        cv.create_text(x0, y0 + 22 * S, text=title, anchor="w", fill=DIM, font=(self.txt_font, 8, "bold"))

        y = y0 + 48 * S
        for slot, w in enumerate(d["windows"][:2]):
            pct = self.shown.get((p, w["key"]), 0.0)
            c2 = palette_for(p, slot, pct)[1]
            cv.create_oval(x0, y - 4 * S, x0 + 8 * S, y + 4 * S, fill=c2, outline="")
            cv.create_text(x0 + 15 * S, y, text=w["label"], anchor="w", fill=FG, font=(self.txt_font, 10))
            cv.create_text(xr, y, text=f"{pct:.0f}%", anchor="e", fill=c2, font=(self.num_font, 15, "bold"))
            sub = cv.create_text(x0 + 15 * S, y + 19 * S, text=fmt_reset(w["reset"]), anchor="w",
                                 fill=MUTED, font=(self.txt_font, 8))
            pc = pace(w["pct"], time_progress(w))
            if pc:
                bx = cv.bbox(sub)
                cv.create_text((bx[2] if bx else x0) + 8 * S, y + 19 * S, text=pc[0], anchor="w",
                               fill=pc[1], font=(self.txt_font, 8, "bold"))
            y += 48 * S

        bottom = y0 + (self.SEC_H - 16) * S
        if d["error"]:
            cv.create_text(x0, bottom + 6 * S, text=d["error"], anchor="sw", width=xr - x0,
                           fill=MUTED if d["notice"] else BAD, font=(self.txt_font, 8))
        elif d["extras"]:
            cv.create_text(x0, bottom, text="   ".join(f"{n} {v:.0f}%" for n, v in d["extras"]),
                           anchor="w", fill=MUTED, font=(self.txt_font, 8))

    # ── 새로고침 버튼
    def draw_button(self):
        cv, S = self.canvas, self.S
        cv.delete("btn")
        busy = bool(self.fetching)
        if self.compact and not (self.hover_win or busy):
            return
        cx, cy, r = self.button_pos()
        cv.create_oval(cx - r, cy - r, cx + r, cy + r, outline="",
                       fill=BTN_HOVER if self.hover_btn else BTN_BG, tags="btn")
        ri, lw = r * 0.5, max(1.0, 1.4 * S)
        color = FG if (self.hover_btn or busy) else MUTED
        if busy:
            cv.create_arc(cx - ri, cy - ri, cx + ri, cy + ri, start=self.spin, extent=270,
                          style="arc", outline=color, width=lw, tags="btn")
        else:
            pts = []
            for i in range(19):
                a = math.radians(100 - 270 * i / 18)
                pts += [cx + ri * math.cos(a), cy - ri * math.sin(a)]
            cv.create_line(*pts, fill=color, width=lw, smooth=True, arrow="last",
                           arrowshape=(3.2 * S, 3.6 * S, 2.2 * S), capstyle="round", tags="btn")

    def _spin_step(self):
        if not self.fetching:
            self.spin_job = None
            self.draw_button()
            return
        self.spin = (self.spin - 24) % 360
        self.draw_button()
        self.spin_job = self.root.after(40, self._spin_step)

    def set_hover(self, win=None, btn=None):
        changed = False
        if win is not None and win != self.hover_win:
            self.hover_win, changed = win, True
        if btn is not None and btn != self.hover_btn:
            self.hover_btn, changed = btn, True
        if changed:
            self.canvas.config(cursor="hand2" if self.hover_btn else "")
            self.draw_button()

    def over_button(self, x, y):
        if self.compact and not (self.hover_win or self.fetching):
            return False
        cx, cy, r = self.button_pos()
        r += 2 * self.S
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def on_motion(self, e):
        self.set_hover(win=True, btn=self.over_button(e.x, e.y))

    # ── 애니메이션
    def animate(self):
        self.anim_from = dict(self.shown)
        self.target = {(p, w["key"]): w["pct"] for p in PROVIDERS for w in self.data[p]["windows"]}
        self.anim_t0 = time.perf_counter()
        if self.anim_job:
            self.root.after_cancel(self.anim_job)
        self._anim_step()

    def _anim_step(self):
        t = min(1.0, (time.perf_counter() - self.anim_t0) / 0.9)
        e = 1 - (1 - t) ** 3
        for k, v in self.target.items():
            a = self.anim_from.get(k, 0.0)
            self.shown[k] = a + (v - a) * e
        self.redraw()
        self.anim_job = self.root.after(16, self._anim_step) if t < 1 else None

    # ── 데이터 갱신
    def fetch_async(self, only=None):
        targets = [p for p in (only or self.providers()) if p not in self.fetching]
        if not targets:
            return
        for p in targets:
            self.fetching.add(p)
            cfg = json.loads(json.dumps(self.cfg))  # 스레드용 복사본

            def worker(p=p, cfg=cfg):
                try:
                    self.q.put(("ok", p, FETCHERS[p](cfg)))
                except NotConfigured as e:
                    self.q.put(("notice", p, str(e)))
                except FetchError as e:
                    self.q.put(("err", p, str(e)))
                except Exception as e:
                    self.q.put(("err", p, f"오류: {e}"))

            threading.Thread(target=worker, daemon=True).start()
        self.redraw()
        if not self.spin_job:
            self._spin_step()

    def poll_queue(self):
        changed = False
        try:
            while True:
                kind, p, payload = self.q.get_nowait()
                self.fetching.discard(p)
                d = self.data[p]
                if kind == "ok":
                    d.update(windows=payload["windows"], extras=payload["extras"], plan=payload["plan"],
                             error=None, notice=False)
                    upd = payload.get("cfg_updates") or {}
                    if upd:
                        self.cfg.update(upd)
                        save_config(self.cfg)
                    self.last_ok = datetime.now()
                else:
                    d.update(error=payload, notice=(kind == "notice"))
                    if kind == "notice":
                        d.update(windows=[], extras=[], plan=None)
                changed = True
        except queue.Empty:
            pass
        if changed:
            self.animate()
        self.root.after(300, self.poll_queue)

    def auto_refresh(self):
        self.fetch_async()
        self.root.after(REFRESH_SEC * 1000, self.auto_refresh)

    def tick(self):
        if not self.anim_job:
            self.redraw()
        self.root.after(30_000, self.tick)

    # ── 조작
    def drag_start(self, e):
        if self.over_button(e.x, e.y):
            self._press_btn, self._drag = True, None
            return
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        if self._drag:
            dx, dy = self._drag
            self.root.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def drag_end(self, e):
        if self._press_btn:
            self._press_btn = False
            if self.over_button(e.x, e.y):
                self.fetch_async()
            return
        if self._drag:
            self._drag = None
            self.cfg["x"], self.cfg["y"] = self.root.winfo_x(), self.root.winfo_y()
            save_config(self.cfg)

    def toggle_compact(self, e=None):
        if e is not None and self.over_button(e.x, e.y):
            return
        self.compact = not self.compact
        self.cfg["compact"] = self.compact
        save_config(self.cfg)
        self.redraw()

    def toggle_topmost(self):
        self.cfg["topmost"] = self.topmost_var.get()
        self.root.attributes("-topmost", self.cfg["topmost"])
        save_config(self.cfg)

    def toggle_provider(self, p):
        self.cfg[f"show_{p}"] = self.show_vars[p].get()
        if not any(self.cfg.get(f"show_{q}", True) for q in PROVIDERS):  # 하나는 남기기
            self.cfg[f"show_{p}"] = True
            self.show_vars[p].set(True)
        save_config(self.cfg)
        self.redraw()
        if self.cfg[f"show_{p}"]:
            self.fetch_async(only=[p])

    def show_menu(self, e):
        self._drag = None
        self.menu.tk_popup(e.x_root, e.y_root)

    # ── 연결 설정
    def _dialog(self, fn):
        self.root.attributes("-topmost", False)
        try:
            return fn()
        finally:
            self.root.attributes("-topmost", self.cfg.get("topmost", True))

    def ask_claude_key(self):
        msg = ("claude.ai에 로그인한 브라우저에서 개발자 도구 > 쿠키 > sessionKey 값을\n"
               "복사해 붙여넣으세요. (Claude를 쓰지 않으면 취소)\n"
               "이 값은 이 컴퓨터의 설정 파일에만 저장돼요.")
        key = simpledialog.askstring("Claude 세션 키", msg, parent=self.root, show="•")
        if not key or not key.strip():
            return False
        self.cfg["session_key"] = key.strip()
        self.cfg.pop("org_id", None)
        save_config(self.cfg)
        return True

    def ask_gpt_token(self):
        msg = ("chatgpt.com에 로그인한 브라우저에서 개발자 도구 > 쿠키 >\n"
               f"{GPT_COOKIE} 값을 복사해 붙여넣으세요.\n"
               "이름 끝에 .0 / .1 이 붙어 두 개로 나뉘어 있으면\n"
               ".0 값 뒤에 .1 값을 그대로 이어 붙여 넣으세요.")
        if codex_auth_path().exists():
            msg += "\n\nCodex 로그인이 감지됐어요. 비워 두고 확인을 눌러도 자동으로 연결돼요."
        val = simpledialog.askstring("ChatGPT 연결", msg, parent=self.root, show="•")
        if val is None:
            return bool(gpt_sources(self.cfg))
        val = val.strip()
        if val:
            self.cfg["gpt_session_token"] = val
            self.cfg.pop("gpt_token_cache", None)
            save_config(self.cfg)
        return bool(gpt_sources(self.cfg))

    def change_claude_key(self):
        if self._dialog(self.ask_claude_key):
            self.data["claude"].update(windows=[], extras=[], error=None, notice=False)
            self.cfg["show_claude"] = True
            self.show_vars["claude"].set(True)
            save_config(self.cfg)
            self.redraw()
            self.fetch_async(only=["claude"])

    def change_gpt(self):
        if self._dialog(self.ask_gpt_token):
            self.data["gpt"].update(windows=[], extras=[], plan=None, error=None, notice=False)
            self.cfg["show_gpt"] = True
            self.show_vars["gpt"].set(True)
            save_config(self.cfg)
            self.redraw()
            self.fetch_async(only=["gpt"])

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    Widget().run()
