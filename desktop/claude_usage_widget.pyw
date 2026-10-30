#!/usr/bin/env python3
"""
AI 사용량 위젯 v5 — Claude + ChatGPT 막대 대시보드 (Windows / macOS)

각 서비스마다 LED처럼 칸이 나뉜 막대 두 개:
  Claude   위 = 현재 세션(5시간), 아래 = 주간 한도
  ChatGPT  위 = 짧은 주기 한도(있을 때), 아래 = 주간 한도  (Codex 사용량 페이지와 같은 값)
막대 위의 흰 세로선은 "리셋 주기 중 지금까지 흐른 시간"이에요.

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

BG = "#0D0F14"        # 창 바탕
CARD = "#161920"      # 서비스 카드
BORDER = "#262B36"
TRACK = "#242833"
FG = "#F2F4F8"
MUTED = "#8D95A7"
DIM = "#5A6273"
GOOD = "#34D399"
BAD = "#FB7185"
BTN_BG = "#1C2029"
BTN_HOVER = "#2C3240"

PROVIDERS = ("claude", "gpt")
NAMES = {"claude": "Claude", "gpt": "ChatGPT"}
MINI_NAMES = {"claude": "Claude", "gpt": "GPT"}
BRAND = {"claude": "#D97757", "gpt": "#10A37F"}   # 카드 제목 옆 점
PALETTES = {                                   # [짧은 주기, 주간] 각각 (시작색, 끝색)
    "claude": [("#DDC4FF", "#8B5CF6"), ("#A9CCFF", "#5B6CF0")],   # 보라, 남색
    "gpt": [("#B4F5D6", "#10B981"), ("#B2F1FA", "#06A6CC")],      # 초록, 청록
}
WARN_PALETTE = ("#FDE68A", "#F59E0B")      # 70% 이상
DANGER_PALETTE = ("#FDA4AF", "#F43F5E")    # 90% 이상

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


def mix(h1, h2, t):
    """두 색을 섞은 hex 색 (t=0이면 h1, t=1이면 h2)"""
    return "#%02X%02X%02X" % lerp(hex_rgb(h1), hex_rgb(h2), t)


def palette_for(provider, slot, pct):
    if pct >= 90:
        return DANGER_PALETTE
    if pct >= 70:
        return WARN_PALETTE
    return PALETTES[provider][slot]


def accent(palette):
    """글자·점에 쓰는 색 — 어두운 바탕에서도 잘 읽히도록 그라데이션 중간보다 밝은 쪽"""
    return mix(palette[0], palette[1], 0.55)


def number_color(provider, slot, pct):
    """큰 숫자 색 — 평소엔 흰색, 70% 이상이면 경고색"""
    return accent(palette_for(provider, slot, pct)) if pct >= 70 else FG


_grad_cache = {}


def h_gradient(w, h, c1, c2):
    """왼쪽 c1 → 오른쪽 c2 가로 그라데이션"""
    key = (w, h, c1, c2)
    if key not in _grad_cache:
        a, b = hex_rgb(c1), hex_rgb(c2)
        row = Image.new("RGB", (w, 1))
        row.putdata([lerp(a, b, x / max(1, w - 1)) for x in range(w)])
        if len(_grad_cache) > 40:
            _grad_cache.clear()
        _grad_cache[key] = row.resize((w, h))
    return _grad_cache[key]


def render_bar(w, h, frac, colors, marker, bg, ss=3):
    """LED처럼 칸이 나뉜 막대.
    사용률만큼 그라데이션으로 채우고, 흐른 시간 위치에 흰 세로선을 긋는다.
    결과 이미지 높이는 h + 위아래 여백(세로선·빛 번짐 자리)."""
    pad = max(2, round(h * 0.5))
    W, H = w * ss, (h + 2 * pad) * ss
    y0, y1 = pad * ss, (pad + h) * ss
    gap = max(1.5, h * 0.26) * ss
    seg = max(3.0, h * 0.62) * ss
    count = max(4, int((W + gap) // (seg + gap)))
    seg = (W - gap * (count - 1)) / count       # 양 끝이 딱 맞도록 칸 폭 보정
    rad = min(seg, y1 - y0) * 0.32

    segs = Image.new("L", (W, H), 0)
    sd = ImageDraw.Draw(segs)
    for i in range(count):
        x = i * (seg + gap)
        sd.rounded_rectangle([x, y0, x + seg - 1, y1 - 1], rad, fill=255)

    base = Image.new("RGB", (W, H), hex_rgb(bg))
    base.paste(hex_rgb(TRACK), (0, 0, W, H), segs)

    frac = max(0.0, min(1.0, frac))
    cut = int(W * frac)
    if cut > 0:
        fill = Image.new("L", (W, H), 0)
        fill.paste(segs.crop((0, 0, cut, H)), (0, 0))
        grad = h_gradient(W, H, *colors)
        k = 4
        blur = fill.resize((W // k, H // k), Image.BILINEAR).filter(ImageFilter.GaussianBlur(h * ss * 0.35 / k))
        base.paste(grad, (0, 0), blur.resize((W, H), Image.BILINEAR).point(lambda v: int(v * 0.2)))
        base.paste(grad, (0, 0), fill)

    if marker is not None:
        lw = max(2.0, h * 0.24) * ss
        x = max(lw, min(W - lw, W * marker))
        d = ImageDraw.Draw(base)
        d.rounded_rectangle([x - lw / 2 - ss, ss, x + lw / 2 + ss, H - ss], lw / 2 + ss, fill=hex_rgb(bg))
        d.rounded_rectangle([x - lw / 2, 2 * ss, x + lw / 2, H - 2 * ss], lw / 2, fill=(244, 246, 250))

    return base.resize((w, h + 2 * pad), Image.LANCZOS)


# ─── 위젯 ─────────────────────────────────────────────
class Widget:
    WIDTH = 340      # 자세히 보기 폭
    PAD = 10         # 창 가장자리 ~ 카드
    CARD_H = 160     # 서비스 카드 높이
    GAP = 8          # 카드 사이 간격
    FOOT_H = 30
    BAR_H = 10       # 자세히 보기 막대 굵기
    MINI_H = 54      # 미니 모드(가로 띠) 높이
    MINI_W = 160     # 미니 모드 서비스 한 칸 폭
    MINI_PAD = 18    # 미니 모드 왼쪽 여백
    MINI_BTN = 14    # 미니 모드 오른쪽 새로고침 버튼 자리

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
        self._bar_cache = {}
        self._shape_cache = {}
        self._font_cache = {}

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
            r.configure(bg=BG)

        self.canvas = tk.Canvas(r, bg="systemTransparent" if self.alpha_mode else BG,
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
        n = len(self.providers())
        key = (w, h, self.alpha_mode, self.compact, n)
        if key not in self._panel_cache:
            S, ss = self.S, 3
            img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0) if self.alpha_mode else hex_rgb(BG) + (255,))
            d = ImageDraw.Draw(img)
            line = max(1, int(S * ss))
            if self.alpha_mode:
                d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=int(18 * S * ss),
                                    fill=hex_rgb(BG) + (255,), outline=hex_rgb(BORDER) + (255,), width=line)
            if self.compact:  # 미니 모드: 서비스 사이 세로 구분선
                for i in range(1, n):
                    x = self.mini_x(i) - int(10 * S)
                    d.rounded_rectangle([x * ss, int(14 * S) * ss, x * ss + line, h * ss - int(14 * S) * ss],
                                        line / 2, fill=hex_rgb(BORDER) + (255,))
            else:
                for i in range(n):  # 서비스 카드
                    x0, y0, x1, y1 = self.card_box(i)
                    d.rounded_rectangle([x0 * ss, y0 * ss, x1 * ss - 1, y1 * ss - 1], radius=int(14 * S * ss),
                                        fill=hex_rgb(CARD) + (255,), outline=hex_rgb(BORDER) + (255,), width=line)
            img = img.resize((w, h), Image.LANCZOS)
            self._panel_cache[key] = self.ImageTk.PhotoImage(img if self.alpha_mode else img.convert("RGB"))
        return self._panel_cache[key]

    def shape_photo(self, kind, w, h, fill, bg, outline=None):
        """부드러운 가장자리의 점(dot)·알약(pill) 이미지"""
        key = (kind, w, h, fill, bg, outline)
        if key not in self._shape_cache:
            ss = 4
            img = Image.new("RGB", (w * ss, h * ss), hex_rgb(bg))
            d = ImageDraw.Draw(img)
            box = [0, 0, w * ss - 1, h * ss - 1]
            if kind == "dot":
                d.ellipse(box, fill=hex_rgb(fill))
            else:
                d.rounded_rectangle(box, radius=h * ss // 2, fill=hex_rgb(fill),
                                    outline=hex_rgb(outline) if outline else None,
                                    width=max(1, int(self.S * ss)))
            self._shape_cache[key] = self.ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        return self._shape_cache[key]

    def font(self, size, bold=False, num=False):
        key = (size, bold, num)
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(root=self.root, family=self.num_font if num else self.txt_font,
                                                size=size, weight="bold" if bold else "normal")
        return self._font_cache[key]

    # ── 크기 계산
    def dims(self):
        S, n = self.S, len(self.providers())
        if self.compact:
            return int((self.MINI_PAD + n * self.MINI_W + self.MINI_BTN) * S), int(self.MINI_H * S)
        return int(self.WIDTH * S), int((self.PAD + n * self.CARD_H + (n - 1) * self.GAP + self.FOOT_H) * S)

    def card_box(self, i):
        S = self.S
        y0 = int((self.PAD + i * (self.CARD_H + self.GAP)) * S)
        return int(self.PAD * S), y0, int((self.WIDTH - self.PAD) * S), y0 + int(self.CARD_H * S)

    def mini_x(self, i):
        return int((self.MINI_PAD + i * self.MINI_W) * self.S)

    def button_pos(self):
        S = self.S
        W, H = self.dims()
        if self.compact:
            return W - 18 * S, H / 2, 8 * S
        return W - (self.PAD + 12) * S, H - self.FOOT_H * S / 2, 10 * S

    # ── 막대 이미지
    def bar_photo(self, w, h, frac, colors, marker, bg):
        key = (w, h, round(frac, 3), colors, None if marker is None else round(marker, 3), bg)
        if key not in self._bar_cache:
            if len(self._bar_cache) > 80:
                self._bar_cache.clear()
            self._bar_cache[key] = self.ImageTk.PhotoImage(render_bar(w, h, frac, colors, marker, bg))
        return self._bar_cache[key]

    def window_bar(self, p, slot, w, h, bg):
        wins = self.data[p]["windows"]
        if slot < len(wins):
            win = wins[slot]
            pct = self.shown.get((p, win["key"]), 0.0)
            return self.bar_photo(w, h, pct / 100, palette_for(p, slot, pct), time_progress(win), bg)
        return self.bar_photo(w, h, 0, PALETTES[p][slot], None, bg)

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
                self.draw_mini(p, self.mini_x(i))
            else:
                self.draw_card(p, *self.card_box(i))

        if not self.compact and self.last_ok:
            bx, by, br = self.button_pos()
            cv.create_text(bx - br - 8 * S, by, text=f"{self.last_ok:%H:%M} 업데이트",
                           anchor="e", fill=DIM, font=self.font(8))
        self.draw_button()

    def draw_number(self, x, cy, pct, big, small, color=FG):
        """'34%' — 숫자는 크게, %는 작게, 기준선을 맞춰 왼쪽 정렬"""
        cv = self.canvas
        fb, fs = self.font(big, True, True), self.font(small, True, True)
        num = f"{pct:.0f}"
        nw = fb.measure(num)
        base = cy + fb.metrics("ascent") * 0.36
        cv.create_text(x, base + fb.metrics("descent"), text=num, anchor="sw", fill=color, font=fb)
        cv.create_text(x + nw + 1, base + fs.metrics("descent"), text="%", anchor="sw", fill=MUTED, font=fs)

    def draw_status(self, p, x, cy, size):
        """값이 없을 때 숫자 자리에 오류(!) / 불러오는 중(···) / 없음(–)"""
        d = self.data[p]
        mark, col = ("!", BAD) if d["error"] and not d["notice"] else ("···" if p in self.fetching else "–", MUTED)
        self.canvas.create_text(x, cy, text=mark, anchor="w", fill=col, font=self.font(size, True, True))

    def draw_pill(self, x, cy, text, color, outline=False, anchor="e"):
        """알약 모양 배지 — anchor="e"면 오른쪽 끝, "w"면 왼쪽 끝을 x에 맞춤"""
        S, cv = self.S, self.canvas
        f = self.font(7, True)
        w, h = int(f.measure(text) + 14 * S), int(17 * S)
        fill = CARD if outline else mix(CARD, color, 0.16)
        img = self.shape_photo("pill", w, h, fill, CARD, mix(CARD, color, 0.45) if outline else None)
        cv.create_image(x, cy, anchor=anchor, image=img)
        cv.create_text(x - w / 2 if anchor == "e" else x + w / 2, cy, text=text, fill=color, font=f)

    def draw_card(self, p, x0, y0, x1, y1):
        S, cv = self.S, self.canvas
        d = self.data[p]
        cx0, cx1 = x0 + 16 * S, x1 - 16 * S

        hy = y0 + 22 * S
        dot = int(8 * S)
        cv.create_image(cx0, hy, anchor="w", image=self.shape_photo("dot", dot, dot, BRAND[p], CARD))
        cv.create_text(cx0 + dot + 7 * S, hy, text=NAMES[p], anchor="w", fill=FG, font=self.font(11, True))
        if d["plan"]:
            self.draw_pill(cx1, hy, d["plan"].upper(), MUTED, outline=True)

        if not d["windows"]:
            if p in self.fetching and not d["error"]:
                msg, col = "불러오는 중…", MUTED
            else:
                msg, col = d["error"] or "", MUTED if d["notice"] else BAD
            cv.create_text(cx0, y0 + 46 * S, text=msg, anchor="nw", width=cx1 - cx0, fill=col, font=self.font(9))
            return

        # 한 줄 구성:  [큰 숫자] [막대 ──────────── ]
        #                       [이름 · 속도 배지   초기화 시각]
        y = y0 + 54 * S
        bx = cx0 + 66 * S
        for slot, w in enumerate(d["windows"][:2]):
            pct = self.shown.get((p, w["key"]), 0.0)
            self.draw_number(cx0, y + 10 * S, pct, 20, 9, number_color(p, slot, pct))
            cv.create_image(bx, y, anchor="w", image=self.window_bar(p, slot, int(cx1 - bx), int(self.BAR_H * S), CARD))
            sy = y + 21 * S
            lab = cv.create_text(bx, sy, text=w["label"], anchor="w", fill=FG, font=self.font(9, True))
            pc = pace(w["pct"], time_progress(w))
            if pc:
                bb = cv.bbox(lab)
                self.draw_pill((bb[2] if bb else bx) + 6 * S, sy, pc[0], pc[1], anchor="w")
            cv.create_text(cx1, sy, text=fmt_reset(w["reset"]), anchor="e", fill=MUTED, font=self.font(8))
            y += 50 * S

        if d["error"]:  # 이전 값은 그대로 두고 아래에 한 줄로
            cv.create_text(cx0, y1 - 8 * S, text=d["error"], anchor="sw", width=cx1 - cx0,
                           fill=MUTED if d["notice"] else BAD, font=self.font(8))
        elif d["extras"]:
            cv.create_text(cx0, y1 - 14 * S, text="  ·  ".join(f"{n} {v:.0f}%" for n, v in d["extras"]),
                           anchor="w", fill=MUTED, font=self.font(8))

    def draw_mini(self, p, x):
        S, cv = self.S, self.canvas
        d = self.data[p]
        first = d["windows"][0] if d["windows"] else None
        cv.create_text(x, 17 * S, text=MINI_NAMES[p], anchor="w", fill=MUTED, font=self.font(7, True))
        if first:
            pct = self.shown.get((p, first["key"]), 0.0)
            self.draw_number(x, 36 * S, pct, 14, 7, number_color(p, 0, pct))
        else:
            self.draw_status(p, x, 36 * S, 13)
        bx = x + 56 * S
        bw = int((self.MINI_W - 56 - 18) * S)
        for slot, (h, cy) in enumerate(((7, 21), (5, 36))):
            if slot < len(d["windows"]) or not d["windows"]:
                cv.create_image(bx, cy * S, anchor="w", image=self.window_bar(p, slot, bw, int(h * S), BG))

    # ── 새로고침 버튼
    def draw_button(self):
        cv, S = self.canvas, self.S
        cv.delete("btn")
        busy = bool(self.fetching)
        if self.compact and not (self.hover_win or busy):
            return
        cx, cy, r = self.button_pos()
        dia = int(2 * r)
        cv.create_image(cx, cy, image=self.shape_photo("dot", dia, dia, BTN_HOVER if self.hover_btn else BTN_BG, BG),
                        tags="btn")
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
