#!/usr/bin/env python3
"""
AI 사용량 위젯 v6 — Claude + ChatGPT 젤리 친구들 (Windows / macOS)

서비스마다 젤리 두 마리:
  Claude   큰 젤리 = 현재 세션(5시간), 작은 젤리 = 주간 한도
  ChatGPT  큰 젤리 = 짧은 주기 한도(있을 때), 작은 젤리 = 주간 한도  (Codex 사용량 페이지와 같은 값)
젤리 몸속 액체 높이가 사용률, 표정이 사용 속도(여유/적정/빠름/위험)예요.
몸 오른쪽 작은 ◀ 는 "리셋 주기 중 지금까지 흐른 시간"이에요.

- 드래그: 창 이동 / 클릭: 젤리 콕 / 더블클릭: 미니 ↔ 자세히 / ↻ 버튼: 새로고침
- 마우스 휠: 투명도 조절
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
import random
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
    from PIL import Image, ImageChops, ImageDraw
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

BG = "#131218"        # 창 바탕 (완전한 검정 대신 살짝 색을 섞은 어두운 색)
BORDER = "#2A2833"
FACE = "#17121F"      # 젤리 눈·입
FG = "#F2F4F8"
MUTED = "#8D95A7"
DIM = "#5A6273"
GOOD = "#34D399"
BAD = "#FB7185"
BTN_BG = "#1F1D27"
BTN_HOVER = "#2E2B39"

PROVIDERS = ("claude", "gpt")
NAMES = {"claude": "Claude", "gpt": "ChatGPT"}
MINI_NAMES = {"claude": "Claude", "gpt": "GPT"}
BRAND = {"claude": "#D97757", "gpt": "#10A37F"}   # 카드 제목 옆 점
PALETTES = {                                   # [짧은 주기, 주간] 각각 (밝은 색, 진한 색)
    "claude": [("#E6D6FF", "#8F62F0"), ("#DCD0FF", "#7A5BE0")],   # 보라 계열
    "gpt": [("#C6F7DF", "#12B886"), ("#BDF2E6", "#0E9F8A")],      # 초록 계열
}
WARN_PALETTE = ("#FDE68A", "#F59E0B")      # 70% 이상
DANGER_PALETTE = ("#FDA4AF", "#F43F5E")    # 90% 이상
GREY_PALETTE = ("#9AA1B2", "#5A6273")      # 값이 없을 때

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
    """숫자 색 — 평소엔 흰색, 70% 이상이면 경고색"""
    return accent(palette_for(provider, slot, pct)) if pct >= 70 else FG


def mood_for(pct, tp):
    """젤리 표정: 100% > 90% 이상 > 빠름 > 여유 > 보통"""
    if pct >= 99.5:
        return "melt"
    if pct >= 90:
        return "danger"
    pc = pace(pct, tp)
    if pc and pc[0] == "빠름":
        return "hurry"
    if pc and pc[0] == "여유" and pct < 70:
        return "happy"
    return "ok"


def spring(t, amp=0.15, freq=15.0, decay=4.7):
    """찌르면 통통 튀다 멈추는 감쇠 진동 (약 2.5Hz, 감쇠비 0.3 — 0.8초쯤 뒤 멈춤)"""
    if t < 0 or t > 1.2:
        return 0.0
    return amp * math.exp(-decay * t) * math.sin(freq * t)


def render_jelly(w, h, frac, colors, marker, mood, bg, phase=0.0, squash=0.0, blink=False, gaze=(0, 0), ss=3):
    """젤리 캐릭터 한 마리 (배경이 투명한 RGBA 이미지).
    몸속 액체 높이 = 사용률, 표정 = 상태, 몸 오른쪽 작은 ◀ = 리셋 주기 중 흐른 시간.
    squash > 0 이면 납작하게, < 0 이면 길쭉하게 (넓이는 그대로라 말랑해 보임).
    이미지 크기는 (w*1.4, h*1.3)이고 몸은 가로 45% 지점·바닥에 붙어 있다."""
    iw, ih = int(w * 1.4), int(h * 1.3)
    W, H = iw * ss, ih * ss
    c1, c2 = colors
    img = Image.new("RGB", (W, H), hex_rgb(bg))
    alpha = Image.new("L", (W, H), 0)

    sx = 1 + squash
    bw, bh = w * sx * ss, h / sx * ss
    cx = W * 0.45
    bottom = H - 2 * ss
    top = bottom - bh
    left, right = cx - bw / 2, cx + bw / 2
    rt, rb = min(bw, bh) * 0.5, bw * 0.24

    def shape(inset):
        """몸 모양: 위는 둥근 돔, 아래는 살짝 둥근 모서리"""
        box = [left + inset, top + inset, right - inset, bottom - inset]
        # 반지름이 변 길이의 절반에 너무 가까우면 Pillow가 오류를 내므로 살짝 줄임
        lim = min(box[2] - box[0], box[3] - box[1]) / 2 - 1.5
        m = Image.new("L", (W, H), 0)
        ImageDraw.Draw(m).rounded_rectangle(box, max(1.0, min(rt - inset, lim)), fill=255)
        low = Image.new("L", (W, H), 0)
        ImageDraw.Draw(low).rounded_rectangle(box, max(1.0, min(rb - inset, lim)), fill=255)
        cut = int(top + rt)
        m.paste(low.crop((0, cut, W, H)), (0, cut))
        return m

    body = shape(0)
    alpha.paste(255, (0, 0, W, H), body)

    # 빈 부분(유리) + 액체 — 부드러운 그라데이션 대신 2~3단 톤
    img.paste(hex_rgb(mix(c1, bg, 0.62)), (0, 0, W, H), body)
    frac = max(0.0, min(1.0, frac))
    if frac > 0.004:
        ys = bottom - frac * bh
        amp = 0 if frac > 0.985 else bh * 0.035
        n = 36

        def wave(dy):
            return [(left + (right - left) * i / n, ys + dy + amp * math.sin(phase + 2 * math.pi * 1.1 * i / n))
                    for i in range(n + 1)]

        def region(dy):
            m = Image.new("L", (W, H), 0)
            ImageDraw.Draw(m).polygon(wave(dy) + [(right, H), (left, H)], fill=255)
            return ImageChops.darker(m, body)

        img.paste(hex_rgb(mix(c1, c2, 0.35)), (0, 0, W, H), region(0))          # 윗면 띠
        img.paste(hex_rgb(mix(c1, c2, 0.72)), (0, 0, W, H), region(bh * 0.09))  # 몸통
        deep = Image.new("L", (W, H), 0)
        ImageDraw.Draw(deep).rectangle([0, bottom - bh * 0.22, W, H], fill=255)
        img.paste(hex_rgb(c2), (0, 0, W, H), ImageChops.darker(deep, region(bh * 0.09)))  # 바닥 쪽 진한 띠
        if amp:
            ln = Image.new("L", (W, H), 0)
            ImageDraw.Draw(ln).line(wave(0), fill=255, width=int(1.4 * ss))
            img.paste(hex_rgb(mix(c1, "#FFFFFF", 0.5)), (0, 0, W, H), ImageChops.darker(ln, body))

    # 테두리
    rim = ImageChops.subtract(body, shape(0.9 * ss))
    img.paste(hex_rgb(mix(c1, "#FFFFFF", 0.2)), (0, 0, W, H), rim.point(lambda v: v * 0.6))

    # 돔 위 반사광
    hl = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hl)
    inset = bw * 0.13
    hd.arc([left + inset, top + inset, right - inset, top + 2 * rt - inset], 196, 250, fill=255,
           width=int(bw * 0.075))
    a = math.radians(262)
    r0 = rt - inset - bw * 0.0375
    hx, hy = cx + r0 * math.cos(a), top + rt + r0 * math.sin(a)
    hd.ellipse([hx - bw * 0.035, hy - bw * 0.035, hx + bw * 0.035, hy + bw * 0.035], fill=255)
    img.paste((255, 255, 255), (0, 0, W, H), ImageChops.darker(hl, body).point(lambda v: v * 0.6))

    # 얼굴
    d = ImageDraw.Draw(img)
    face = hex_rgb(FACE)
    ex, ey = bw * 0.19, top + bh * 0.52
    er = min(bw, bh) * 0.062
    lw = max(1, int(er * 0.75))
    my = ey + bh * 0.14
    gx, gy = gaze[0] * er * 0.45, gaze[1] * er * 0.4   # 커서 쪽을 바라보는 눈동자

    def dot_eye(x):
        d.ellipse([x + gx - er, ey + gy - er * 1.2, x + gx + er, ey + gy + er * 1.2], fill=face)
        d.ellipse([x + gx - er * 0.55, ey + gy - er * 0.85, x + gx - er * 0.05, ey + gy - er * 0.35],
                  fill=(255, 255, 255))

    def line_eye(x):
        d.line([x - er * 1.1, ey, x + er * 1.1, ey], fill=face, width=lw)

    def happy_eye(x):
        d.arc([x - er * 1.3, ey - er * 0.6, x + er * 1.3, ey + er * 1.9], 200, 340, fill=face, width=lw)

    def x_eye(x):
        e = er * 0.95
        d.line([x - e, ey - e, x + e, ey + e], fill=face, width=lw)
        d.line([x - e, ey + e, x + e, ey - e], fill=face, width=lw)

    def ring_eye(x):
        d.ellipse([x - er * 1.15, ey - er * 1.15, x + er * 1.15, ey + er * 1.15], outline=face, width=lw)
        d.ellipse([x - er * 0.35, ey - er * 0.35, x + er * 0.35, ey + er * 0.35], fill=face)

    def chevron_eye(x, s):
        d.line([x - er * s, ey - er, x + er * s, ey, x - er * s, ey + er], fill=face, width=lw, joint="curve")

    def blush():
        for s in (-1, 1):
            bx = cx + s * (ex + er * 0.9)
            d.ellipse([bx - er * 1.1, my - er * 0.9, bx + er * 1.1, my - er * 0.1],
                      fill=hex_rgb(mix(c2, "#FF6B9A", 0.55)))

    def smile(wd, opened=False):
        mw = bw * wd
        if opened:
            d.chord([cx - mw, my - mw * 0.9, cx + mw, my + mw * 0.9], 0, 180, fill=face)
        else:
            d.arc([cx - mw, my - mw * 0.9, cx + mw, my + mw * 0.6], 20, 160, fill=face, width=lw)

    def flat():
        d.line([cx - bw * 0.07, my + er * 0.3, cx + bw * 0.07, my + er * 0.3], fill=face, width=lw)

    def wavy():
        pts = [(cx - bw * 0.11 + bw * 0.22 * i / 8, my + er * 0.3 + er * 0.45 * math.sin(i * math.pi / 2))
               for i in range(9)]
        d.line(pts, fill=face, width=lw, joint="curve")

    def o_mouth():
        r = er * 0.75
        d.ellipse([cx - r, my - r * 0.6, cx + r, my + r * 1.2], fill=face)

    def sweat():
        sx_, sy_, r = right - bw * 0.16, top + bh * 0.3, bw * 0.07
        col = (143, 211, 255)
        d.ellipse([sx_ - r, sy_, sx_ + r, sy_ + 2 * r], fill=col)
        d.polygon([(sx_ - r * 0.85, sy_ + r * 0.6), (sx_, sy_ - r * 1.3), (sx_ + r * 0.85, sy_ + r * 0.6)], fill=col)

    eye = line_eye if blink else dot_eye
    if mood == "happy":
        for s in (-1, 1):
            (line_eye if blink else happy_eye)(cx + s * ex)
        blush()
        smile(0.13, opened=True)
    elif mood == "hurry":
        for s in (-1, 1):
            eye(cx + s * ex)
        wavy()
        sweat()
    elif mood == "danger":
        chevron_eye(cx - ex, 1)
        chevron_eye(cx + ex, -1)
        o_mouth()
        sweat()
    elif mood == "melt":  # 100% — 녹아서 어질어질
        for s in (-1, 1):
            ring_eye(cx + s * ex)
        wavy()
    elif mood == "dead":
        for s in (-1, 1):
            x_eye(cx + s * ex)
        flat()
    elif mood == "sleep":
        for s in (-1, 1):
            line_eye(cx + s * ex)
        flat()
    elif mood == "loading":
        for s in (-1, 1):
            eye(cx + s * ex)
        o_mouth()
    else:  # ok
        for s in (-1, 1):
            eye(cx + s * ex)
        smile(0.1)

    # 흐른 시간 표시: 몸 오른쪽 바깥의 작은 흰 삼각형 ◀
    if marker is not None:
        ym = bottom - max(0.0, min(1.0, marker)) * bh
        ym = max(top + bh * 0.08, min(bottom - bh * 0.04, ym))
        dy = (top + rt) - ym
        edge = cx + (bw / 2 - rt) + math.sqrt(max(0.0, rt * rt - dy * dy)) if dy > 0 else right
        t = w * ss * 0.1
        tip = edge + 1.5 * ss
        tri = [(tip, ym), (tip + t * 1.2, ym - t * 0.75), (tip + t * 1.2, ym + t * 0.75)]
        d.polygon(tri, fill=(244, 246, 250))
        ImageDraw.Draw(alpha).polygon(tri, fill=255)

    img.putalpha(alpha)
    return img.resize((iw, ih), Image.LANCZOS)


# ─── 위젯 ─────────────────────────────────────────────
class Widget:
    # 자세히 보기
    WIDTH = 280
    TOP = 6          # 위쪽 여백
    BLOCK_H = 112    # 서비스 한 칸 높이
    FOOT_H = 24
    BIG = (48, 53)   # 큰 젤리(짧은 주기) 몸 크기
    SMALL = (34, 37)  # 작은 젤리(주간)
    # 미니 모드
    MINI_H = 90
    MINI_W = 100     # 서비스 한 칸 폭
    MINI_PAD = 8
    MINI_BIG = (40, 44)
    MINI_SMALL = (28, 31)

    OPACITY_STEPS = (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3)

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
        self.fx_until = 0.0          # 이 시각까지 물결·흔들림 애니메이션
        self.phases = {}             # 서비스별 물결 위상
        self.wave_until = {}         # 서비스별로 이 시각까지 물결이 움직임
        self.wobble_t0 = {}          # 서비스별 흔들기 시작 시각
        self.last_hover_wobble = 0.0
        self.blinking = None         # 지금 눈 깜빡이는 젤리 (서비스, 칸)
        self.gaze = (0, 0)           # 젤리 눈동자가 바라보는 방향 (-1~1, -1~1)
        self._drag = None
        self._press = None
        self._press_btn = False
        self._hold_top = False       # 메뉴·대화상자가 떠 있는 동안은 맨 앞 고정을 잠시 멈춤
        self._hwnd = None
        self.hover_win = False
        self.hover_btn = False
        self.spin = 90.0
        self.spin_job = None
        self._size = None
        self._panel_cache = {}
        self._jelly_cache = {}
        self._shape_cache = {}
        self._font_cache = {}

        self.build()
        self.root.deiconify()
        self.setup_native_window()
        self.raise_top()
        self.fetch_async()
        self.root.after(200, self.poll_queue)
        self.root.after(30_000, self.tick)
        self.root.after(REFRESH_SEC * 1000, self.auto_refresh)
        self.root.after(2000, self.keep_on_top)
        self.root.after(3000, self.blink)
        self.root.after(500, self.track_pointer)

    # ── 표시할 서비스
    def providers(self):
        ps = [p for p in PROVIDERS if self.cfg.get(f"show_{p}", True)]
        return ps or ["claude"]

    # ── 창 구성
    def build(self):
        r = self.root
        r.overrideredirect(True)
        r.attributes("-topmost", self.cfg.get("topmost", True))
        self.apply_opacity(self.opacity())

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
        cv.bind("<Enter>", self.on_enter)
        cv.bind("<Leave>", self.on_leave)
        cv.bind("<MouseWheel>", self.on_wheel)
        cv.bind("<Button-4>", lambda e: self.step_opacity(+1))
        cv.bind("<Button-5>", lambda e: self.step_opacity(-1))
        for seq in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
            cv.bind(seq, self.show_menu)

        self.menu = tk.Menu(r, tearoff=0)
        self.topmost_var = tk.BooleanVar(value=self.cfg.get("topmost", True))
        self.show_vars = {p: tk.BooleanVar(value=self.cfg.get(f"show_{p}", True)) for p in PROVIDERS}
        self.opacity_var = tk.DoubleVar(value=self.opacity())
        self.reveal_var = tk.BooleanVar(value=self.cfg.get("hover_reveal", True))
        self.menu.add_command(label="지금 새로고침", command=self.fetch_async)
        self.menu.add_command(label="미니 / 자세히 보기", command=self.toggle_compact)
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.topmost_var,
                                  command=self.toggle_topmost)
        op = tk.Menu(self.menu, tearoff=0)
        for v in self.OPACITY_STEPS:
            op.add_radiobutton(label=f"{v:.0%}", variable=self.opacity_var, value=v,
                               command=lambda: self.set_opacity(self.opacity_var.get()))
        op.add_separator()
        op.add_checkbutton(label="마우스를 올리면 선명하게", variable=self.reveal_var, command=self.toggle_reveal)
        self.menu.add_cascade(label="투명도 (마우스 휠로도 조절)", menu=op)
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

    def setup_native_window(self):
        """Windows: 창 핸들을 기억해 두고 Win11 둥근 모서리 적용"""
        if not IS_WIN:
            return
        try:
            import ctypes
            self.root.update_idletasks()
            self._hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            pref = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
            rr, gg, bb = hex_rgb(BORDER)
            color = ctypes.c_uint(rr | (gg << 8) | (bb << 16))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(self._hwnd, 34, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            pass

    # ── 항상 맨 앞
    def raise_top(self):
        if not self.cfg.get("topmost", True):
            return
        if IS_WIN and self._hwnd:
            try:
                import ctypes
                u = ctypes.windll.user32
                u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_uint]
                # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER
                u.SetWindowPos(self._hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x1 | 0x2 | 0x10 | 0x40 | 0x200)
                return
            except Exception:
                pass
        self.root.attributes("-topmost", True)
        self.root.lift()

    def keep_on_top(self):
        """다른 창(작업 표시줄, 다른 '항상 위' 창 등)에 밀려 뒤로 가지 않도록 주기적으로 다시 맨 앞으로"""
        if not self._hold_top:
            self.raise_top()
        self.root.after(2000, self.keep_on_top)

    # ── 투명도
    def opacity(self):
        try:
            return max(0.3, min(1.0, float(self.cfg.get("opacity", 0.9))))
        except (TypeError, ValueError):
            return 0.9

    def apply_opacity(self, v):
        try:
            self.root.attributes("-alpha", v)
        except tk.TclError:
            pass

    def set_opacity(self, v):
        v = round(max(0.3, min(1.0, v)), 2)
        self.cfg["opacity"] = v
        self.opacity_var.set(v)
        save_config(self.cfg)
        self.apply_opacity(v)  # 휠로 바꾸는 중에도 바로 보이도록 (마우스가 나갔다 들어오면 다시 선명하게)

    def step_opacity(self, direction):
        self.set_opacity(self.opacity() + 0.1 * direction)

    def on_wheel(self, e):
        if e.delta:
            self.step_opacity(1 if e.delta > 0 else -1)

    def toggle_reveal(self):
        self.cfg["hover_reveal"] = self.reveal_var.get()
        save_config(self.cfg)

    def revealing(self):
        return self.hover_win and self.cfg.get("hover_reveal", True)

    def on_enter(self, e):
        self.set_hover(win=True)
        if self.revealing():
            self.apply_opacity(1.0)
        now = time.perf_counter()
        if now - self.last_hover_wobble > 1.5:  # 마우스를 올리면 한 번 말랑
            self.last_hover_wobble = now
            self.kick(1.2, wobble=self.providers())

    def on_leave(self, e):
        self.set_hover(win=False, btn=False)
        self.apply_opacity(self.opacity())

    # ── 이미지
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
            m = int(12 * S) * ss
            for i in range(1, n):  # 서비스 사이 구분선
                if self.compact:
                    x = self.mini_x(i) * ss
                    d.rectangle([x, m, x + line - 1, h * ss - m], fill=hex_rgb(BORDER) + (255,))
                else:
                    y = self.block_y(i) * ss
                    d.rectangle([m, y, w * ss - m, y + line - 1], fill=hex_rgb(BORDER) + (255,))
            img = img.resize((w, h), Image.LANCZOS)
            a = img.getchannel("A")
            grain = Image.effect_noise((w, h), 24).convert("RGB")  # 밋밋하지 않게 아주 옅은 질감
            img = Image.blend(img.convert("RGB"), grain, 0.035)
            if self.alpha_mode:
                img.putalpha(a)
            self._panel_cache[key] = self.ImageTk.PhotoImage(img)
        return self._panel_cache[key]

    def shape_photo(self, kind, w, h, fill, bg, outline=None):
        """부드러운 가장자리의 점(dot)·알약(pill) 이미지"""
        key = (kind, w, h, fill, bg, outline)
        if key not in self._shape_cache:
            ss = 4
            img = Image.new("RGB", (w * ss, h * ss), hex_rgb(fill))
            mask = Image.new("L", img.size, 0)
            box = [0, 0, w * ss - 1, h * ss - 1]
            if kind == "dot":
                ImageDraw.Draw(mask).ellipse(box, fill=255)
            else:
                ImageDraw.Draw(mask).rounded_rectangle(box, radius=h * ss // 2, fill=255)
                if outline:
                    ImageDraw.Draw(img).rounded_rectangle(box, radius=h * ss // 2, outline=hex_rgb(outline),
                                                          width=max(1, int(self.S * ss)))
            img.putalpha(mask)
            self._shape_cache[key] = self.ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        return self._shape_cache[key]

    def jelly_photo(self, p, slot, size):
        """서비스 p의 slot번째 한도를 젤리로 (값이 없으면 회색 젤리)"""
        S, now = self.S, time.perf_counter()
        w, h = int(size[0] * S), int(size[1] * S)
        d = self.data[p]
        wins = d["windows"]
        if slot < len(wins):
            win = wins[slot]
            pct = self.shown.get((p, win["key"]), 0.0)
            tp = time_progress(win)
            frac, colors, marker, mood = pct / 100, palette_for(p, slot, pct), tp, mood_for(win["pct"], tp)
            melt = 0.3 * max(0.0, min(1.0, (pct - 97) / 3))  # 100%에 가까우면 녹아서 퍼짐
        else:
            frac, colors, marker, melt = 0.0, GREY_PALETTE, None, 0.0
            if p in self.fetching and not d["error"]:
                mood = "loading"
            else:
                mood = "dead" if d["error"] and not d["notice"] else "sleep"
        squash = melt + spring(now - self.wobble_t0.get(p, -9) - 0.07 * slot)
        key = (w, h, round(frac, 3), colors, None if marker is None else round(marker, 3), mood,
               round(self.phases.get(p, 0.0) + slot * 1.7, 2), round(squash, 3),
               self.blinking == (p, slot), self.gaze)
        if key not in self._jelly_cache:
            if len(self._jelly_cache) > 120:
                self._jelly_cache.clear()
            img = render_jelly(w, h, frac, colors, marker, mood, BG, phase=key[6], squash=squash,
                               blink=key[8], gaze=self.gaze)
            self._jelly_cache[key] = self.ImageTk.PhotoImage(img)
        return self._jelly_cache[key]

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
            return int((self.MINI_PAD * 2 + n * self.MINI_W) * S), int(self.MINI_H * S)
        return int(self.WIDTH * S), int((self.TOP + n * self.BLOCK_H + self.FOOT_H) * S)

    def block_y(self, i):
        return int((self.TOP + i * self.BLOCK_H) * self.S)

    def mini_x(self, i):
        return int((self.MINI_PAD + i * self.MINI_W) * self.S)

    def button_pos(self):
        S = self.S
        W, H = self.dims()
        if self.compact:
            return W - 13 * S, 13 * S, 8 * S
        return W - 18 * S, H - self.FOOT_H * S / 2 - 1 * S, 9 * S

    def provider_at(self, x, y):
        ps = self.providers()
        for i, p in enumerate(ps):
            if self.compact:
                if self.mini_x(i) <= x < self.mini_x(i + 1):
                    return p
            elif self.block_y(i) <= y < self.block_y(i + 1):
                return p
        return None

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
                self.draw_block(p, self.block_y(i), W)

        if not self.compact and self.last_ok:
            bx, by, br = self.button_pos()
            cv.create_text(bx - br - 6 * S, by, text=f"{self.last_ok:%H:%M} 업데이트",
                           anchor="e", fill=DIM, font=self.font(7))
        self.draw_button()

    def draw_pct(self, cx, cy, pct, size, color):
        """'34%' — 숫자는 크게, %는 작게, 가운데 정렬"""
        cv = self.canvas
        fb, fs = self.font(size, True, True), self.font(max(6, size * 6 // 10), True, True)
        num = f"{pct:.0f}"
        nw, pw = fb.measure(num), fs.measure("%")
        x = cx - (nw + pw) / 2
        base = cy + fb.metrics("ascent") * 0.36
        cv.create_text(x, base + fb.metrics("descent"), text=num, anchor="sw", fill=color, font=fb)
        cv.create_text(x + nw, base + fs.metrics("descent"), text="%", anchor="sw", fill=MUTED, font=fs)

    def draw_pair(self, p, x, y_bottom, big, small, gap, num_y, sizes):
        """큰 젤리(짧은 주기) + 작은 젤리(주간)와 그 아래 숫자"""
        S, cv = self.S, self.canvas
        wins = self.data[p]["windows"]
        x2 = x + int((big[0] * 1.4 + gap) * S)
        for slot, (jx, size, fsize) in enumerate(((x, big, sizes[0]), (x2, small, sizes[1]))):
            if slot >= len(wins) and wins:
                continue  # 주간 한도만 있는 요금제 등
            img = self.jelly_photo(p, slot, size)
            cv.create_image(jx, y_bottom, anchor="sw", image=img)
            if slot < len(wins):
                pct = self.shown.get((p, wins[slot]["key"]), 0.0)
                self.draw_pct(jx + size[0] * 1.4 * 0.45 * S, num_y, pct, fsize, number_color(p, slot, pct))

    def draw_pill(self, x, cy, text, color, outline=False, anchor="e"):
        """알약 모양 배지 — anchor="e"면 오른쪽 끝, "w"면 왼쪽 끝을 x에 맞춤"""
        S, cv = self.S, self.canvas
        f = self.font(7, True)
        w, h = int(f.measure(text) + 12 * S), int(15 * S)
        fill = BG if outline else mix(BG, color, 0.18)
        img = self.shape_photo("pill", w, h, fill, BG, mix(BG, color, 0.45) if outline else None)
        cv.create_image(x, cy, anchor=anchor, image=img)
        cv.create_text(x - w / 2 if anchor == "e" else x + w / 2, cy, text=text, fill=color, font=f)

    def draw_block(self, p, y0, W):
        S, cv = self.S, self.canvas
        d = self.data[p]
        x0, x1 = 14 * S, W - 14 * S

        self.draw_pair(p, 8 * S, y0 + 90 * S, self.BIG, self.SMALL, 0, y0 + 99 * S, (10, 8))

        hy = y0 + 16 * S
        dot = int(7 * S)
        cv.create_image(x0, hy, anchor="w", image=self.shape_photo("dot", dot, dot, BRAND[p], BG))
        cv.create_text(x0 + dot + 6 * S, hy, text=NAMES[p], anchor="w", fill=FG, font=self.font(10, True))
        if d["plan"]:
            self.draw_pill(x1, hy, d["plan"].upper(), MUTED, outline=True)

        tx = 134 * S
        if not d["windows"]:
            if p in self.fetching and not d["error"]:
                msg, col = "불러오는 중…", MUTED
            else:
                msg, col = d["error"] or "", MUTED if d["notice"] else BAD
            cv.create_text(tx, y0 + 32 * S, text=msg, anchor="nw", width=x1 - tx, fill=col, font=self.font(8))
            return

        y = y0 + 40 * S
        for slot, w in enumerate(d["windows"][:2]):
            cv.create_text(tx, y, text=w["label"], anchor="w", fill=FG, font=self.font(8, True))
            pc = pace(w["pct"], time_progress(w))
            if pc:
                self.draw_pill(x1, y, pc[0], pc[1])
            cv.create_text(tx, y + 15 * S, text=fmt_reset(w["reset"]), anchor="w", fill=MUTED, font=self.font(7))
            y += 32 * S

        if d["error"]:  # 이전 값은 그대로 두고 아래에 한 줄로
            cv.create_text(tx, y0 + self.BLOCK_H * S - 4 * S, text=d["error"], anchor="sw", width=x1 - tx,
                           fill=MUTED if d["notice"] else BAD, font=self.font(7))
        elif d["extras"]:
            cv.create_text(tx, y0 + 104 * S, text="  ·  ".join(f"{n} {v:.0f}%" for n, v in d["extras"]),
                           anchor="w", fill=DIM, font=self.font(7))

    def draw_mini(self, p, x):
        S, cv = self.S, self.canvas
        self.draw_pair(p, x + 2 * S, 70 * S, self.MINI_BIG, self.MINI_SMALL, -2, 79 * S, (10, 8))
        dot = int(6 * S)
        cv.create_image(x + 8 * S, 13 * S, anchor="w", image=self.shape_photo("dot", dot, dot, BRAND[p], BG))
        cv.create_text(x + 8 * S + dot + 4 * S, 13 * S, text=MINI_NAMES[p], anchor="w", fill=MUTED,
                       font=self.font(7, True))

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
    def animate(self, changed=()):
        self.anim_from = dict(self.shown)
        self.target = {(p, w["key"]): w["pct"] for p in PROVIDERS for w in self.data[p]["windows"]}
        self.anim_t0 = time.perf_counter()
        self.kick(1.4, wobble=changed)

    def kick(self, secs, wobble=()):
        """wobble에 있는 서비스의 젤리를 흔들고 secs초 동안 물결을 움직인다"""
        now = time.perf_counter()
        self.fx_until = max(self.fx_until, now + secs)
        for p in wobble:
            self.wobble_t0[p] = now
            self.wave_until[p] = max(self.wave_until.get(p, 0.0), now + secs)
        if not self.anim_job:
            self._anim_step()

    def _anim_step(self):
        now = time.perf_counter()
        t = min(1.0, (now - self.anim_t0) / 0.9)
        e = 1 - (1 - t) ** 3
        for k, v in self.target.items():
            a = self.anim_from.get(k, 0.0)
            self.shown[k] = a + (v - a) * e
        for p, until in self.wave_until.items():
            if now < until:
                self.phases[p] = (self.phases.get(p, 0.0) + 0.45) % (2 * math.pi)
        self.redraw()
        spent = int((time.perf_counter() - now) * 1000)  # 그리는 데 걸린 만큼 빼서 초당 30프레임 근처 유지
        self.anim_job = self.root.after(max(5, 33 - spent), self._anim_step) if (t < 1 or now < self.fx_until) else None

    def blink(self):
        """가끔 젤리 하나가 눈을 깜빡"""
        cands = [(p, s) for p in self.providers() for s in range(min(2, len(self.data[p]["windows"])))]
        if cands and not self.anim_job:
            self.blinking = random.choice(cands)
            self.redraw()
            self.root.after(140, self._unblink)
        self.root.after(random.randint(2500, 6000), self.blink)

    def track_pointer(self):
        """마우스 커서가 있는 쪽을 젤리들이 바라봄 (방향이 바뀔 때만 다시 그림)"""
        try:
            px, py = self.root.winfo_pointerxy()
            W, H = self.dims()
            dx = px - (self.root.winfo_rootx() + W / 2)
            dy = py - (self.root.winfo_rooty() + H / 2)
            dist = math.hypot(dx, dy)
            g = (0, 0) if dist < 24 * self.S else (
                0 if abs(dx) < dist * 0.38 else (1 if dx > 0 else -1),
                0 if abs(dy) < dist * 0.38 else (1 if dy > 0 else -1))
        except tk.TclError:
            g = (0, 0)
        if g != self.gaze:
            self.gaze = g
            if not self.anim_job:
                self.redraw()
        self.root.after(150, self.track_pointer)

    def _unblink(self):
        self.blinking = None
        if not self.anim_job:
            self.redraw()

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
        changed = []
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
                changed.append(p)
        except queue.Empty:
            pass
        if changed:
            self.animate(changed)
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
        self._press = (e.x_root, e.y_root)
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
            px, py = self._press or (e.x_root, e.y_root)
            if abs(e.x_root - px) + abs(e.y_root - py) < 4:  # 움직이지 않고 클릭 → 젤리 콕 찌르기
                p = self.provider_at(e.x, e.y)
                if p:
                    self.kick(1.0, wobble=[p])
                return
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
        self.raise_top()

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
        self._hold_top = True
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()
            self._hold_top = False

    # ── 연결 설정
    def _dialog(self, fn):
        self._hold_top = True
        self.root.attributes("-topmost", False)
        try:
            return fn()
        finally:
            self.root.attributes("-topmost", self.cfg.get("topmost", True))
            self._hold_top = False
            self.raise_top()

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
