#!/usr/bin/env python3
"""
AI 사용량 위젯 v7 — Claude + ChatGPT 스플릿 플랩 (Windows / macOS)

공항 출발 안내판처럼 숫자 카드가 착착 넘어가며 사용률을 보여 줘요.
  Claude   현재 세션(5시간), 주간 한도
  ChatGPT  짧은 주기 한도(있을 때), 주간 한도  (Codex 사용량 페이지와 같은 값)
'속도' 칸은 리셋 주기 중 흐른 시간에 비해 얼마나 빨리 쓰는지(여유/적정/빠름)예요.

- 드래그: 창 이동 / 클릭: 숫자 섞기 / 더블클릭: 미니 ↔ 자세히 / ↻ 버튼: 새로고침
- 마우스 휠: 투명도 조절
- 우클릭 (Mac은 Control+클릭): 메뉴
- 처음 실행하면 컴퓨터를 켤 때 자동으로 실행되도록 등록해요 (메뉴에서 끌 수 있어요)

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
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog

try:
    from PIL import Image, ImageDraw, ImageFont
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

BG = "#0E0E11"        # 창 바탕 (완전한 검정 대신 살짝 색을 섞은 어두운 색)
BORDER = "#26262C"
TILE = "#1C1C21"      # 플랩 카드 아래쪽
TILE_TOP = "#232329"  # 플랩 카드 위쪽 (살짝 밝게)
SPLIT = "#08080A"     # 카드가 접히는 줄
FG = "#F2F2F4"
MUTED = "#8A8A94"
DIM = "#5E5E68"
GOOD = "#3DDC97"
WARN = "#FFB020"
BAD = "#FF5A4E"
BTN_BG = "#1C1C21"
BTN_HOVER = "#2A2A31"

PROVIDERS = ("claude", "gpt")
NAMES = {"claude": "Claude", "gpt": "ChatGPT"}
MINI_NAMES = {"claude": "Claude", "gpt": "GPT"}
BRAND = {"claude": "#D97757", "gpt": "#10A37F"}   # 서비스 이름 옆 작은 네모

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")


# ─── 오류 종류 ─────────────────────────────────────────
class FetchError(Exception):
    pass


class NetworkError(FetchError):
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


# ─── 컴퓨터를 켤 때 자동 실행 ──────────────────────────
AUTOSTART_NAME = "LLM Usage Widget"                # Windows 시작 프로그램(레지스트리 Run) 이름
AUTOSTART_LABEL = "io.github.shun1403.llm-usage"   # Mac LaunchAgent 이름
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_supported():
    return (IS_WIN or IS_MAC) and bool(sys.executable)


def launch_command():
    """지금 실행 중인 파이썬 + 이 파일. Windows는 cmd 창이 뜨지 않게 pythonw로"""
    exe = Path(sys.executable)
    if IS_WIN and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return [str(exe), str(Path(__file__).resolve())]


def mac_agent_path():
    return Path.home() / "Library" / "LaunchAgents" / f"{AUTOSTART_LABEL}.plist"


def in_temp_dir():
    """압축 파일 안에서 바로 연 경우처럼 임시 폴더에서 실행 중인지"""
    try:
        Path(__file__).resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        return True
    except ValueError:
        return False


def set_autostart(on):
    """켜기: 지금 위치의 파일로 (다시) 등록 / 끄기: 등록 지우기. 실패하면 OSError"""
    if IS_WIN:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if on:
                cmd = " ".join(f'"{a}"' for a in launch_command())
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
    elif IS_MAC:
        import plistlib
        path = mac_agent_path()
        if not on:
            path.unlink(missing_ok=True)
            return
        data = plistlib.dumps({"Label": AUTOSTART_LABEL, "ProgramArguments": launch_command(),
                               "RunAtLoad": True, "ProcessType": "Interactive"})
        if path.exists() and path.read_bytes() == data:
            return   # 같은 내용을 다시 쓰면 '백그라운드 항목 추가' 알림이 또 뜰 수 있어서
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


# ─── 중복 실행 방지 ────────────────────────────────────
_instance_lock = None


def already_running():
    """위젯이 이미 떠 있으면 True. 자동 실행과 직접 실행이 겹쳐도 창은 하나만"""
    global _instance_lock
    try:
        if IS_WIN:
            import ctypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateMutexW.restype = ctypes.c_void_p
            _instance_lock = k32.CreateMutexW(None, False, "Local\\LLMUsageWidget")
            return ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
        import fcntl
        _instance_lock = open(Path.home() / ".llm_usage_widget.lock", "w")
        fcntl.flock(_instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    except Exception:
        return False


# ─── 네트워크 공통 ─────────────────────────────────────
def http_get_json(url, headers=None, cookies=None):
    hdrs = {"Accept": "application/json", "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
    hdrs.update(headers or {})
    cookies = cookies or {}
    if cffi_requests:
        try:
            r = cffi_requests.get(url, headers=hdrs, cookies=cookies, impersonate="chrome", timeout=20)
        except Exception as e:
            raise NetworkError(f"네트워크 오류: {e}")
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
            raise NetworkError(f"네트워크 연결을 확인하세요 ({e.reason})")

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


def value_color(pct):
    """숫자 색 — 평소 흰색, 70% 이상 주황, 90% 이상 빨강"""
    return BAD if pct >= 90 else WARN if pct >= 70 else FG


def fmt_reset_short(dt):
    """안내판용 짧은 초기화 시각: '2시간 9분 후' / '11/2(월) 10:06'"""
    s = fmt_reset(dt)
    return s[:-4] if s.endswith(" 초기화") else s


# ── 플랩 카드에 쓸 글꼴 (이미지로 그려야 넘어가는 모습을 만들 수 있어요)
def _font_files(kind):
    if IS_WIN:
        d = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        names = {"num": ["bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf"],
                 "kor": ["malgunbd.ttf", "malgun.ttf"]}[kind]
        return [d / n for n in names]
    if IS_MAC:
        sup = Path("/System/Library/Fonts/Supplemental")
        return {"num": [sup / "DIN Condensed Bold.ttf", sup / "DIN Alternate Bold.ttf",
                        Path("/System/Library/Fonts/Helvetica.ttc")],
                "kor": [Path("/System/Library/Fonts/AppleSDGothicNeo.ttc")]}[kind]
    return {"num": [Path("/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf"),
                    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
                    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")],
            "kor": [Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")]}[kind]


_pil_fonts = {}


def pil_font(kind, px):
    key = (kind, px)
    if key in _pil_fonts:
        return _pil_fonts[key]
    font = None
    for path in _font_files(kind):
        if not path.exists():
            continue
        try:
            index = 0
            if path.suffix.lower() == ".ttc":  # 여러 굵기가 들어 있는 파일이면 Bold를 고름
                for i in range(24):
                    try:
                        style = ImageFont.truetype(str(path), 12, index=i).getname()[1] or ""
                    except OSError:
                        break
                    if style.lower() == "bold":
                        index = i
                        break
            font = ImageFont.truetype(str(path), px, index=index)
            if path.name.lower() == "bahnschrift.ttf":
                try:
                    font.set_variation_by_name("Bold")
                except Exception:
                    pass
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(px)
        except TypeError:
            font = ImageFont.load_default()
    _pil_fonts[key] = font
    return font


_tile_cache = {}


def render_tile(ch, w, h, color, kind="num", ss=3):
    """스플릿 플랩 카드 한 장 (배경이 투명한 RGBA). 가운데 가로줄이 카드가 접히는 자리."""
    key = (ch, w, h, color, kind)
    if key in _tile_cache:
        return _tile_cache[key]
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = max(1.0, h * 0.12) * ss
    half = H / 2
    d.rounded_rectangle([0, 0, W - 1, H - 1], r, fill=hex_rgb(TILE))
    d.rounded_rectangle([0, 0, W - 1, half], r, fill=hex_rgb(TILE_TOP))
    d.rectangle([0, half - r, W - 1, half], fill=hex_rgb(TILE_TOP))
    if ch.strip():
        f = pil_font(kind, int(H * (0.8 if kind == "num" else 0.62)))
        x0, y0, x1, y1 = d.textbbox((0, 0), ch, font=f)
        d.text((W / 2 - (x0 + x1) / 2, half - (y0 + y1) / 2), ch, font=f, fill=hex_rgb(color))
    d.rectangle([0, half - 0.6 * ss, W, half + 0.6 * ss], fill=hex_rgb(SPLIT))   # 접히는 줄
    pin = max(1.0, w * 0.07) * ss
    for x in (0.5 * ss, W - pin - 0.5 * ss):                                    # 양옆 경첩
        d.rectangle([x, half - 1.4 * ss, x + pin, half + 1.4 * ss], fill=hex_rgb(SPLIT))
    img = img.resize((w, h), Image.LANCZOS)
    if len(_tile_cache) > 400:
        _tile_cache.clear()
    _tile_cache[key] = img
    return img


def flip_frame(a, b, t):
    """카드가 a에서 b로 넘어가는 중간 그림 (t: 0~1).
    앞 절반은 위쪽 잎(a의 위 반쪽)이 접히며 내려가고, 뒤 절반은 b의 아래 반쪽이 펼쳐지며 내려온다."""
    w, h = a.size
    hh = h // 2
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.paste(b.crop((0, 0, w, hh)), (0, 0))
    out.paste(a.crop((0, hh, w, h)), (0, hh))
    if t < 0.5:
        s = 1 - t / 0.5
        leaf = a.crop((0, 0, w, hh)).resize((w, max(1, int(hh * s))), Image.BILINEAR)
        leaf = Image.blend(leaf, Image.new("RGBA", leaf.size, (0, 0, 0, 255)), 0.45 * (1 - s))
        out.paste(leaf, (0, hh - leaf.height), leaf)
    else:
        s = (t - 0.5) / 0.5
        leaf = b.crop((0, hh, w, h)).resize((w, max(1, int((h - hh) * s))), Image.BILINEAR)
        leaf = Image.blend(leaf, Image.new("RGBA", leaf.size, (0, 0, 0, 255)), 0.4 * (1 - s))
        out.paste(leaf, (0, hh), leaf)
    return out


CYCLE = " 0123456789"   # 실제 안내판처럼 숫자는 차례대로 넘어가요
FLIP_SEC = 0.09         # 카드 한 장 넘어가는 시간


def flip_sequence(cur, target, cycle=True):
    """cur에서 target까지 넘겨야 할 글자들"""
    if cur == target:
        return []
    if cycle and cur in CYCLE and target in CYCLE:
        i, j = CYCLE.index(cur), CYCLE.index(target)
        steps = (j - i) % len(CYCLE)
        return [CYCLE[(i + k) % len(CYCLE)] for k in range(1, steps + 1)]
    return [target]


# ─── 위젯 ─────────────────────────────────────────────
class Widget:
    # 자세히 보기 (출발 안내판 모양)
    WIDTH = 300
    HEAD_H = 24      # 맨 위 열 제목 줄
    BLOCK_H = 90     # 서비스 한 칸 높이
    PENDING_H = 30   # 등록하지 않은 서비스는 한 줄만 ('등록 대기')
    FOOT_H = 24
    TILE = (14, 21)  # 사용률 카드
    STAT = (13, 18)  # 속도 카드 (여유/적정/빠름)
    COL_VAL = 86     # 열 위치
    COL_STAT = 170
    # 미니 모드
    MINI_H = 66
    MINI_PAD = 12
    MINI_BIG = (15, 23)    # 짧은 주기
    MINI_SMALL = (12, 18)  # 주간
    MINI_GAP = 18          # 서비스 사이 간격
    MINI_PAIR_GAP = 11     # 큰 카드와 작은 카드 사이 (빨간 램프 자리)

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
        self.sync_autostart()

        self.S = self.root.winfo_fpixels("1i") / 96 if IS_WIN else 1.0
        fams = set(tkfont.families(self.root))

        def pick(cands, fallback):
            return next((f for f in cands if f in fams), fallback)

        if IS_MAC:
            self.txt_font = pick(["Apple SD Gothic Neo"], "Helvetica")
        else:
            self.txt_font = pick(["Malgun Gothic", "맑은 고딕"], "Arial")

        self.q = queue.Queue()
        self.data = {p: {"windows": [], "extras": [], "plan": None, "error": None, "notice": False}
                     for p in PROVIDERS}
        self.last_ok = None
        self.fetching = set()
        self.compact = self.cfg.get("compact", False)
        self.flaps = {}              # (서비스, 칸, 종류) → 카드 목록 [{"cur", "queue", "t0", "color"}]
        self.anim_job = None
        self.retry_job = None
        self.last_hover = 0.0
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
        self._photo_cache = {}
        self._shape_cache = {}
        self._font_cache = {}
        self.update_registration()

        self.build()
        self.root.deiconify()
        self.setup_native_window()
        self.raise_top()
        self.fetch_async()
        self.root.after(200, self.poll_queue)
        self.root.after(30_000, self.tick)
        self.root.after(REFRESH_SEC * 1000, self.auto_refresh)
        self.root.after(2000, self.keep_on_top)

    # ── 표시할 서비스
    def providers(self):
        ps = [p for p in PROVIDERS if self.cfg.get(f"show_{p}", True)]
        return ps or ["claude"]

    def update_registration(self):
        """세션 키·토큰(또는 Codex 로그인)이 있는 서비스 = 등록된 서비스"""
        self._reg = {"claude": bool(self.cfg.get("session_key")), "gpt": bool(gpt_sources(self.cfg))}

    def registered(self, p):
        return self._reg.get(p, True)

    def visible(self):
        """지금 모드에서 그릴 서비스. 미니 모드에는 등록한 서비스만 (하나도 없으면 전부)"""
        ps = self.providers()
        if self.compact:
            return [p for p in ps if self.registered(p)] or ps
        return ps

    def block_h(self, p):
        return self.BLOCK_H if self.registered(p) else self.PENDING_H

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
        self.autostart_var = tk.BooleanVar(value=self.cfg.get("autostart", True))
        self.menu.add_command(label="지금 새로고침", command=self.fetch_async)
        self.menu.add_command(label="미니 / 자세히 보기", command=self.toggle_compact)
        self.menu.add_checkbutton(label="항상 위에 표시", variable=self.topmost_var,
                                  command=self.toggle_topmost)
        if autostart_supported():
            self.menu.add_checkbutton(label="컴퓨터 켤 때 자동 실행", variable=self.autostart_var,
                                      command=self.toggle_autostart)
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

    def on_leave(self, e):
        self.set_hover(win=False, btn=False)
        self.apply_opacity(self.opacity())

    # ── 이미지
    def panel_photo(self, w, h):
        ps = self.visible()
        n = len(ps)
        key = (w, h, self.alpha_mode, self.compact, tuple((q, self.registered(q)) for q in ps))
        if key not in self._panel_cache:
            S, ss = self.S, 3
            img = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0) if self.alpha_mode else hex_rgb(BG) + (255,))
            d = ImageDraw.Draw(img)
            line = max(1, int(S * ss))
            if self.alpha_mode:
                d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=int(16 * S * ss),
                                    fill=hex_rgb(BG) + (255,), outline=hex_rgb(BORDER) + (255,), width=line)
            m = int(12 * S) * ss
            for i in range(1, n):  # 서비스 사이 구분선
                if self.compact:
                    x = (self.mini_x(i) - int(self.MINI_GAP * S / 2)) * ss
                    d.rectangle([x, m, x + line - 1, h * ss - m], fill=hex_rgb(BORDER) + (255,))
                else:
                    y = self.block_y(i) * ss
                    d.rectangle([m, y, w * ss - m, y + line - 1], fill=hex_rgb(BORDER) + (255,))
            img = img.resize((w, h), Image.LANCZOS)
            self._panel_cache[key] = self.ImageTk.PhotoImage(img if self.alpha_mode else img.convert("RGB"))
        return self._panel_cache[key]

    def shape_photo(self, kind, w, h, fill, outline=None):
        """부드러운 가장자리의 점(dot)·네모(square)·알약(pill) 이미지"""
        key = (kind, w, h, fill, outline)
        if key not in self._shape_cache:
            ss = 4
            img = Image.new("RGB", (w * ss, h * ss), hex_rgb(fill))
            mask = Image.new("L", img.size, 0)
            box = [0, 0, w * ss - 1, h * ss - 1]
            md = ImageDraw.Draw(mask)
            if kind == "dot":
                md.ellipse(box, fill=255)
            elif kind == "square":
                md.rounded_rectangle(box, radius=ss, fill=255)
            else:
                md.rounded_rectangle(box, radius=h * ss // 2, fill=255)
                if outline:
                    ImageDraw.Draw(img).rounded_rectangle(box, radius=h * ss // 2, outline=hex_rgb(outline),
                                                          width=max(1, int(self.S * ss)))
            img.putalpha(mask)
            self._shape_cache[key] = self.ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
        return self._shape_cache[key]

    def font(self, size, bold=False):
        key = (size, bold)
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(root=self.root, family=self.txt_font, size=size,
                                                weight="bold" if bold else "normal")
        return self._font_cache[key]

    # ── 플랩 카드
    def flap_group(self, key):
        return self.flaps.setdefault(key, [])

    def set_flaps(self, key, text, color, cycle=True, stagger=0.06):
        """key 자리의 카드들이 text를 보이도록 넘김 (이미 같으면 그대로)"""
        tiles = self.flap_group(key)
        now = time.perf_counter()
        while len(tiles) < len(text):
            tiles.insert(0, {"cur": " ", "queue": [], "t0": now, "color": color})
        del tiles[:len(tiles) - len(text)]
        for i, (tile, ch) in enumerate(zip(tiles, text)):
            end = tile["queue"][-1] if tile["queue"] else tile["cur"]
            tile["color"] = color
            if end == ch:
                continue
            start = tile["cur"]
            tile["queue"] = flip_sequence(start, ch, cycle)
            tile["t0"] = now + (len(text) - 1 - i) * stagger   # 오른쪽 카드부터 차례로
        self.start_anim()

    def shuffle_flaps(self, key, rounds=4):
        """클릭하면 숫자가 한 번 섞였다가 제자리로"""
        now = time.perf_counter()
        for i, tile in enumerate(self.flap_group(key)):
            end = tile["queue"][-1] if tile["queue"] else tile["cur"]
            if end not in "0123456789":
                continue
            tile["queue"] = [random.choice("0123456789") for _ in range(rounds)] + [end]
            tile["t0"] = now + i * 0.05
        self.start_anim()

    def step_flaps(self, now):
        """시간이 지난 만큼 카드를 넘김. 아직 넘어가는 카드가 있으면 True."""
        busy = False
        for tiles in self.flaps.values():
            for tile in tiles:
                while tile["queue"] and now - tile["t0"] >= FLIP_SEC:
                    tile["cur"] = tile["queue"].pop(0)
                    tile["t0"] += FLIP_SEC
                busy = busy or bool(tile["queue"])
        return busy

    def tile_photo(self, tile, size, kind, now):
        S = self.S
        w, h = int(size[0] * S), int(size[1] * S)
        if tile["queue"] and now >= tile["t0"]:
            step = min(7, int((now - tile["t0"]) / FLIP_SEC * 8))   # 넘어가는 모습 8단계
            key = ("flip", tile["cur"], tile["queue"][0], w, h, tile["color"], kind, step)
            if key not in self._photo_cache:
                a = render_tile(tile["cur"], w, h, tile["color"], kind)
                b = render_tile(tile["queue"][0], w, h, tile["color"], kind)
                self._photo_cache[key] = self.ImageTk.PhotoImage(flip_frame(a, b, (step + 0.5) / 8))
        else:
            key = ("tile", tile["cur"], w, h, tile["color"], kind)
            if key not in self._photo_cache:
                self._photo_cache[key] = self.ImageTk.PhotoImage(render_tile(tile["cur"], w, h, tile["color"], kind))
        if len(self._photo_cache) > 600:
            self._photo_cache.clear()
            return self.tile_photo(tile, size, kind, now)
        return self._photo_cache[key]

    def draw_flaps(self, key, x, cy, size, kind="num", gap=1.5, now=None):
        """카드들을 왼쪽 x부터 나란히 그리고 오른쪽 끝 x를 돌려줌"""
        S, cv = self.S, self.canvas
        now = now or time.perf_counter()
        for tile in self.flap_group(key):
            cv.create_image(x, cy, anchor="w", image=self.tile_photo(tile, size, kind, now))
            x += (size[0] + gap) * S
        return x - gap * S

    def flap_width(self, key, size, gap=1.5):
        n = max(2, len(self.flap_group(key)))
        return (n * (size[0] + gap) - gap) * self.S

    # ── 값 → 카드
    def sync(self, p=None):
        """데이터(또는 시간 흐름)에 맞게 카드 목표를 다시 정함"""
        for p in ([p] if p else PROVIDERS):
            if not self.registered(p):
                self.set_flaps((p, 0, "pending"), "등록대기", MUTED, cycle=False)
                continue
            d = self.data[p]
            wins = d["windows"][:2]
            for slot in range(2):
                if slot < len(wins):
                    w = wins[slot]
                    pct = max(0, min(100, round(w["pct"])))
                    self.set_flaps((p, slot, "v"), f"{pct:>2}", value_color(pct))
                    pc = pace(w["pct"], time_progress(w))
                    self.set_flaps((p, slot, "s"), pc[0] if pc else "  ", pc[1] if pc else MUTED, cycle=False)
                elif slot == 0 or not wins:
                    if p in self.fetching and not d["error"]:
                        continue  # 불러오는 중에는 step에서 숫자를 섞음
                    col = BAD if d["error"] and not d["notice"] else DIM
                    self.set_flaps((p, slot, "v"), "--", col, cycle=False)
                    self.set_flaps((p, slot, "s"), "  ", MUTED, cycle=False)

    def loading_shuffle(self):
        """불러오는 동안 값이 없는 서비스의 숫자 카드가 계속 돌아감"""
        busy = False
        for p in self.visible():
            d = self.data[p]
            if p not in self.fetching or d["windows"] or d["error"] or not self.registered(p):
                continue
            for slot in range(2):
                tiles = self.flap_group((p, slot, "v"))
                if not tiles:
                    self.set_flaps((p, slot, "v"), "  ", MUTED, cycle=False)
                for tile in tiles:
                    tile["color"] = MUTED
                    if not tile["queue"]:
                        tile["queue"] = [random.choice("0123456789")]
                        tile["t0"] = time.perf_counter()
                busy = True
        return busy

    # ── 크기 계산
    def mini_group_w(self, p):
        S = self.S
        return (self.flap_width((p, 0, "v"), self.MINI_BIG) + self.MINI_PAIR_GAP * S
                + self.flap_width((p, 1, "v"), self.MINI_SMALL, 1.2))

    def dims(self):
        S, ps = self.S, self.visible()
        if self.compact:
            w = self.MINI_PAD * 2 * S + sum(self.mini_group_w(p) for p in ps) + self.MINI_GAP * S * (len(ps) - 1)
            return int(w + 10 * S), int(self.MINI_H * S)
        return int(self.WIDTH * S), int((self.HEAD_H + sum(self.block_h(p) for p in ps) + self.FOOT_H) * S)

    def block_y(self, i):
        return int((self.HEAD_H + sum(self.block_h(p) for p in self.visible()[:i])) * self.S)

    def mini_x(self, i):
        S, ps = self.S, self.visible()
        return int(self.MINI_PAD * S + sum(self.mini_group_w(p) + self.MINI_GAP * S for p in ps[:i]))

    def button_pos(self):
        S = self.S
        W, H = self.dims()
        if self.compact:
            return W - 11 * S, 12 * S, 7 * S
        return W - 18 * S, H - self.FOOT_H * S / 2 - 1 * S, 9 * S

    def provider_at(self, x, y):
        ps = self.visible()
        for i, p in enumerate(ps):
            if self.compact:
                if self.mini_x(i) <= x < self.mini_x(i) + self.mini_group_w(p) + self.MINI_GAP * self.S:
                    return p
            elif self.block_y(i) <= y < self.block_y(i) + self.block_h(p) * self.S:
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
            self._panel_cache.clear()
        cv.create_image(0, 0, anchor="nw", image=self.panel_photo(W, H))

        now = time.perf_counter()
        if self.compact:
            for i, p in enumerate(self.visible()):
                self.draw_mini(p, self.mini_x(i), now)
        else:
            hy = self.HEAD_H * S / 2 + 2 * S   # 열 제목 (안내판 머리줄)
            for text, x, anchor in (("한도", 14 * S, "w"), ("사용률", self.COL_VAL * S, "w"),
                                    ("속도", self.COL_STAT * S, "w"), ("초기화", W - 14 * S, "e")):
                cv.create_text(x, hy, text=text, anchor=anchor, fill=DIM, font=self.font(7))
            for i, p in enumerate(self.visible()):
                if self.registered(p):
                    self.draw_block(p, self.block_y(i), W, now)
                else:
                    self.draw_pending(p, self.block_y(i), W, now)
            if self.last_ok:
                bx, by, br = self.button_pos()
                cv.create_text(bx - br - 6 * S, by, text=f"{self.last_ok:%H:%M} 업데이트",
                               anchor="e", fill=DIM, font=self.font(7))
        self.draw_button()

    def draw_pill(self, x, cy, text, color, anchor="e"):
        """테두리만 있는 알약 모양 배지"""
        S, cv = self.S, self.canvas
        f = self.font(7, True)
        w, h = int(f.measure(text) + 12 * S), int(15 * S)
        cv.create_image(x, cy, anchor=anchor, image=self.shape_photo("pill", w, h, BG, mix(BG, color, 0.45)))
        cv.create_text(x - w / 2 if anchor == "e" else x + w / 2, cy, text=text, fill=color, font=f)

    def draw_name(self, p, x, cy, size=8):
        S, cv = self.S, self.canvas
        sq = int(6 * S)
        cv.create_image(x, cy, anchor="w", image=self.shape_photo("square", sq, sq, BRAND[p]))
        cv.create_text(x + sq + 5 * S, cy, text=NAMES[p].upper() if not self.compact else MINI_NAMES[p].upper(),
                       anchor="w", fill=MUTED, font=self.font(size, True))

    def draw_block(self, p, y0, W, now):
        S, cv = self.S, self.canvas
        d = self.data[p]
        x0, x1 = 14 * S, W - 14 * S
        self.draw_name(p, x0, y0 + 13 * S)
        if d["plan"]:
            self.draw_pill(x1, y0 + 13 * S, d["plan"].upper(), MUTED)

        wins = d["windows"][:2]
        rows = max(1, len(wins))
        for slot in range(min(2, rows if wins else 1)):
            cy = y0 + (36 + slot * 28) * S
            label = wins[slot]["label"] if slot < len(wins) else ""
            cv.create_text(x0, cy, text=label, anchor="w", fill=FG, font=self.font(9))
            xr = self.draw_flaps((p, slot, "v"), self.COL_VAL * S, cy, self.TILE, now=now)
            cv.create_text(xr + 3 * S, cy + 4 * S, text="%", anchor="w", fill=DIM, font=self.font(7, True))
            self.draw_flaps((p, slot, "s"), self.COL_STAT * S, cy, self.STAT, kind="kor", gap=1.2, now=now)
            if slot < len(wins):
                cv.create_text(x1, cy, text=fmt_reset_short(wins[slot]["reset"]), anchor="e", fill=MUTED,
                               font=self.font(8))

        msg = None
        if d["error"]:
            msg, col = d["error"], (MUTED if d["notice"] else BAD)
        elif not wins and p in self.fetching:
            msg, col = "불러오는 중…", MUTED
        if msg and not wins:
            cv.create_text(x0, y0 + 52 * S, text=msg, anchor="nw", width=x1 - x0, fill=col, font=self.font(8))
        elif msg:
            cv.create_text(x0, y0 + 84 * S, text=msg, anchor="sw", width=x1 - x0, fill=col, font=self.font(7))
        elif d["extras"]:
            cv.create_text(x0, y0 + 80 * S, text="  ·  ".join(f"{n} {v:.0f}%" for n, v in d["extras"]),
                           anchor="w", fill=DIM, font=self.font(7))

    def draw_pending(self, p, y0, W, now):
        """등록하지 않은 서비스: 이름 + '등록 대기' 카드 + 안내를 한 줄로"""
        S, cv = self.S, self.canvas
        cy = y0 + self.PENDING_H * S / 2
        self.draw_name(p, 14 * S, cy)
        self.draw_flaps((p, 0, "pending"), self.COL_VAL * S, cy, self.STAT, kind="kor", gap=1.2, now=now)
        cv.create_text(W - 14 * S, cy, text="우클릭 → 연결 설정", anchor="e", fill=DIM, font=self.font(7))

    def draw_mini(self, p, x, now):
        S, cv = self.S, self.canvas
        d = self.data[p]
        wins = d["windows"][:2]
        self.draw_name(p, x, 12 * S, size=7)
        base = 44 * S   # 카드 아래쪽 맞춤
        bx = x
        xr = self.draw_flaps((p, 0, "v"), bx, base - self.MINI_BIG[1] * S / 2, self.MINI_BIG, now=now)
        sx = xr + self.MINI_PAIR_GAP * S
        xr2 = sx
        if len(wins) > 1 or not wins:
            xr2 = self.draw_flaps((p, 1, "v"), sx, base - self.MINI_SMALL[1] * S / 2, self.MINI_SMALL, gap=1.2,
                                  now=now)
        labels = [(wins[0]["short"] if wins else "", bx)]
        if len(wins) > 1:
            labels.append((wins[1]["short"], sx))
        for text, lx in labels:
            cv.create_text(lx, 54 * S, text=text, anchor="w", fill=DIM, font=self.font(7))
        # 시간보다 빨리 쓰고 있으면 카드 오른쪽 옆(윗부분 높이)에 작은 빨간 램프
        for slot, (left, right, top) in enumerate(((bx, xr, base - self.MINI_BIG[1] * S),
                                                  (sx, xr2, base - self.MINI_SMALL[1] * S))):
            if slot < len(wins):
                pc = pace(wins[slot]["pct"], time_progress(wins[slot]))
                if pc and pc[0] == "빠름":
                    r = int(4 * S)
                    cv.create_image(right + 3.5 * S, top + 3 * S, anchor="w", image=self.shape_photo("dot", r, r, BAD))

    # ── 새로고침 버튼
    def draw_button(self):
        cv, S = self.canvas, self.S
        cv.delete("btn")
        busy = bool(self.fetching)
        if self.compact and not (self.hover_win or busy):
            return
        cx, cy, r = self.button_pos()
        dia = int(2 * r)
        cv.create_image(cx, cy, image=self.shape_photo("dot", dia, dia, BTN_HOVER if self.hover_btn else BTN_BG),
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

    # ── 애니메이션 (카드가 넘어가는 동안만 돌아감)
    def start_anim(self):
        if not self.anim_job:
            self.anim_job = self.root.after(1, self._anim_step)

    def _anim_step(self):
        now = time.perf_counter()
        busy = self.loading_shuffle()
        busy = self.step_flaps(now) or busy
        self.redraw()
        spent = int((time.perf_counter() - now) * 1000)
        self.anim_job = self.root.after(max(5, 30 - spent), self._anim_step) if busy else None

    def fetch_async(self, only=None):
        self.update_registration()  # Codex에 새로 로그인했거나 키를 지웠으면 여기서 반영
        targets = [p for p in (only or self.providers()) if p not in self.fetching and self.registered(p)]
        if not targets:
            self.sync()
            self.redraw()
            return
        for p in targets:
            self.fetching.add(p)
            cfg = json.loads(json.dumps(self.cfg))  # 스레드용 복사본

            def worker(p=p, cfg=cfg):
                try:
                    self.q.put(("ok", p, FETCHERS[p](cfg)))
                except NotConfigured as e:
                    self.q.put(("notice", p, str(e)))
                except NetworkError as e:
                    self.q.put(("neterr", p, str(e)))
                except FetchError as e:
                    self.q.put(("err", p, str(e)))
                except Exception as e:
                    self.q.put(("err", p, f"오류: {e}"))

            threading.Thread(target=worker, daemon=True).start()
        self.sync()
        self.redraw()
        self.start_anim()  # 값이 없는 서비스는 불러오는 동안 숫자가 돌아감
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
                    if kind == "neterr" and not self.retry_job:
                        # 컴퓨터를 막 켜서 인터넷이 아직 안 붙은 경우 등 → 2분 기다리지 않고 곧 다시
                        self.retry_job = self.root.after(15_000, self.retry_fetch)
                changed.append(p)
        except queue.Empty:
            pass
        for p in dict.fromkeys(changed):
            self.sync(p)
        if changed:
            self.redraw()
        self.root.after(300, self.poll_queue)

    def retry_fetch(self):
        self.retry_job = None
        self.fetch_async()

    def auto_refresh(self):
        self.fetch_async()
        self.root.after(REFRESH_SEC * 1000, self.auto_refresh)

    def tick(self):
        self.sync()  # 시간이 흐르면 '속도'가 바뀔 수 있음
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
            if abs(e.x_root - px) + abs(e.y_root - py) < 4:  # 움직이지 않고 클릭 → 숫자 한 번 섞기
                p = self.provider_at(e.x, e.y)
                if p:
                    for slot in range(2):
                        self.shuffle_flaps((p, slot, "v"))
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

    def sync_autostart(self):
        """자동 실행이 켜져 있으면(처음엔 켜짐) 지금 파일 위치로 다시 등록 — 파일을 옮겨도 따라가요"""
        if autostart_supported() and self.cfg.get("autostart", True) and not in_temp_dir():
            try:
                set_autostart(True)
            except OSError:
                pass

    def toggle_autostart(self):
        on = self.autostart_var.get()
        try:
            set_autostart(on)
        except OSError as e:
            self.autostart_var.set(not on)
            msg = f"자동 실행 설정을 바꾸지 못했어요.\n{e}"
            self._dialog(lambda: messagebox.showerror("AI 사용량", msg, parent=self.root))
            return
        self.cfg["autostart"] = on
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
            self.update_registration()

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
    if already_running():
        sys.exit(0)
    Widget().run()
