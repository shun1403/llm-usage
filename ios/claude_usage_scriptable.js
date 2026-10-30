// Claude 사용량 위젯 — iPhone용 Scriptable 스크립트
// 홈 화면(작게/중간/크게)과 잠금 화면(직사각형/한 줄) 위젯을 지원해요.
//
// 처음 한 번은 Scriptable 앱 안에서 이 스크립트를 실행해 세션 키를 입력하세요.
// 세션 키는 아이폰 키체인에만 저장돼요. 로그인 정보와 같으니 공유하지 마세요.
// 주의: 공식 API가 아니라 claude.ai 사용량 페이지가 쓰는 내부 주소를 불러와요.

const KEY_NAME = "claude_usage_session_key"
const ORG_NAME = "claude_usage_org_id"
const BASE = "https://claude.ai/api"

const LABELS = {
  five_hour: "현재 세션",
  seven_day: "주간 (전체)",
  seven_day_opus: "주간 (Opus)",
  seven_day_sonnet: "주간 (Sonnet)",
}
const ORDER = Object.keys(LABELS)

const C = {
  bg: new Color("#1E2430"),
  track: new Color("#333B4A"),
  fg: new Color("#E8ECF2"),
  muted: new Color("#8C96A8"),
  ok: new Color("#4FB3A9"),
  warn: new Color("#E0A93B"),
  danger: new Color("#E5566A"),
}

// ─── 세션 키 ─────────────────────────────────────
async function askKey() {
  const a = new Alert()
  a.title = "Claude 세션 키"
  a.message = "PC 브라우저에서 claude.ai 쿠키의 sessionKey 값을 복사해 붙여넣으세요."
  a.addSecureTextField("sk-ant-sid01-…", "")
  a.addAction("저장")
  a.addCancelAction("취소")
  if ((await a.presentAlert()) === -1) return null
  const v = a.textFieldValue(0).trim()
  if (!v) return null
  Keychain.set(KEY_NAME, v)
  if (Keychain.contains(ORG_NAME)) Keychain.remove(ORG_NAME)
  return v
}

function getKey() {
  return Keychain.contains(KEY_NAME) ? Keychain.get(KEY_NAME) : null
}

// ─── 네트워크 ─────────────────────────────────────
async function getJSON(url, key) {
  const r = new Request(url)
  r.timeoutInterval = 20
  r.headers = {
    Cookie: `sessionKey=${key}`,
    Accept: "application/json",
    "Accept-Language": "ko-KR,ko;q=0.9",
    Referer: "https://claude.ai/settings/usage",
    "User-Agent":
      "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
  }
  let text
  try {
    text = await r.loadString()
  } catch (e) {
    throw new Error("네트워크 연결을 확인하세요")
  }
  const status = r.response ? r.response.statusCode : 0
  if (status === 200) {
    try { return JSON.parse(text) } catch (e) { throw new Error("응답을 해석하지 못했어요") }
  }
  if (/just a moment|cloudflare/i.test(text.slice(0, 3000))) throw new Error("claude.ai가 요청을 차단했어요")
  if (status === 401 || status === 403) throw new Error("세션 키 만료 — 앱에서 다시 입력하세요")
  if (status === 404) { const e = new Error("사용량 정보를 찾지 못했어요"); e.notFound = true; throw e }
  throw new Error(`서버 응답 오류 (HTTP ${status})`)
}

async function getOrgId(key) {
  if (Keychain.contains(ORG_NAME)) return Keychain.get(ORG_NAME)
  const orgs = await getJSON(`${BASE}/organizations`, key)
  if (!Array.isArray(orgs) || orgs.length === 0) throw new Error("조직 정보를 찾지 못했어요")
  const org = orgs.find(o => (o.capabilities || []).includes("chat")) || orgs[0]
  Keychain.set(ORG_NAME, org.uuid)
  return org.uuid
}

async function fetchUsage(key) {
  for (let attempt = 0; attempt < 2; attempt++) {
    const org = await getOrgId(key)
    try {
      const data = await getJSON(`${BASE}/organizations/${org}/usage`, key)
      const rows = []
      for (const [k, v] of Object.entries(data || {})) {
        if (v && typeof v === "object" && v.utilization != null && !isNaN(Number(v.utilization))) {
          rows.push({ key: k, label: LABELS[k] || k, pct: Number(v.utilization), reset: v.resets_at })
        }
      }
      rows.sort((a, b) => (ORDER.indexOf(a.key) + 1 || 99) - (ORDER.indexOf(b.key) + 1 || 99))
      if (rows.length === 0) throw new Error("사용량 항목이 비어 있어요")
      return rows
    } catch (e) {
      if (e.notFound && attempt === 0) { Keychain.remove(ORG_NAME); continue }
      throw e
    }
  }
}

// ─── 표시 도우미 ─────────────────────────────────
function colorFor(p) { return p >= 90 ? C.danger : p >= 70 ? C.warn : C.ok }

function fmtReset(s, short) {
  if (!s) return ""
  const d = new Date(s)
  if (isNaN(d)) return ""
  const secs = (d - new Date()) / 1000
  if (secs <= 0) return "곧 초기화"
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60)
  if (secs < 86400) {
    if (short) return h ? `${h}h${m}m` : `${m}m`
    return h ? `${h}시간 ${m}분 후 초기화` : `${m}분 후 초기화`
  }
  const wd = "일월화수목금토"[d.getDay()]
  const hh = String(d.getHours()).padStart(2, "0"), mm = String(d.getMinutes()).padStart(2, "0")
  return short ? `${wd} ${hh}:${mm}` : `${d.getMonth() + 1}/${d.getDate()}(${wd}) ${hh}:${mm} 초기화`
}

function barImage(pct, w, h) {
  const dc = new DrawContext()
  dc.size = new Size(w, h)
  dc.opaque = false
  dc.respectScreenScale = true
  const track = new Path()
  track.addRoundedRect(new Rect(0, 0, w, h), h / 2, h / 2)
  dc.addPath(track)
  dc.setFillColor(C.track)
  dc.fillPath()
  const fw = Math.max(0, Math.min(1, pct / 100)) * w
  if (fw > 0) {
    const p = new Path()
    p.addRoundedRect(new Rect(0, 0, Math.max(fw, h), h), h / 2, h / 2)
    dc.addPath(p)
    dc.setFillColor(colorFor(pct))
    dc.fillPath()
  }
  return dc.getImage()
}

// ─── 위젯 ─────────────────────────────────────────
function lockScreenWidget(family, rows, error) {
  const w = new ListWidget()
  const get = k => rows && rows.find(r => r.key === k)
  const s = get("five_hour"), wk = get("seven_day")

  if (family === "accessoryInline") {
    w.addText(error ? "Claude 사용량 오류"
      : `Claude 세션 ${s ? Math.round(s.pct) : "-"}% · 주간 ${wk ? Math.round(wk.pct) : "-"}%`)
    return w
  }
  // accessoryRectangular / accessoryCircular
  if (error) { w.addText("Claude 오류").font = Font.boldSystemFont(12); w.addText(error).font = Font.systemFont(10); return w }
  const t = w.addText("Claude")
  t.font = Font.boldSystemFont(12)
  for (const r of [s, wk].filter(Boolean)) {
    const line = w.addText(`${r.key === "five_hour" ? "세션" : "주간"} ${Math.round(r.pct)}% · ${fmtReset(r.reset, true)}`)
    line.font = Font.systemFont(12)
  }
  return w
}

function homeWidget(family, rows, error) {
  const w = new ListWidget()
  w.backgroundColor = C.bg
  w.setPadding(14, 14, 14, 14)
  w.url = "https://claude.ai/settings/usage"

  const title = w.addText("Claude 사용량")
  title.font = Font.mediumSystemFont(11)
  title.textColor = C.muted

  if (error) {
    w.addSpacer(8)
    const e = w.addText(error)
    e.font = Font.systemFont(12)
    e.textColor = C.danger
    w.addSpacer()
    return w
  }

  const limit = family === "large" ? rows.length : 2
  const barW = family === "small" ? 125 : 290
  for (const r of rows.slice(0, limit)) {
    w.addSpacer(family === "large" ? 14 : 8)
    const head = w.addStack()
    const l = head.addText(r.label)
    l.font = Font.systemFont(12)
    l.textColor = C.fg
    head.addSpacer()
    const p = head.addText(`${Math.round(r.pct)}%`)
    p.font = Font.boldSystemFont(13)
    p.textColor = colorFor(r.pct)
    w.addSpacer(4)
    w.addImage(barImage(r.pct, barW, 6)).imageSize = new Size(barW, 6)
    const rt = fmtReset(r.reset, family === "small")
    if (rt) {
      w.addSpacer(3)
      const t = w.addText(rt)
      t.font = Font.systemFont(10)
      t.textColor = C.muted
    }
  }
  w.addSpacer()
  const df = new DateFormatter()
  df.useShortTimeStyle()
  const upd = w.addText(`${df.string(new Date())} 업데이트`)
  upd.font = Font.systemFont(9)
  upd.textColor = C.muted
  upd.rightAlignText()
  return w
}

// ─── 실행 ─────────────────────────────────────────
async function main() {
  let key = getKey()

  if (config.runsInApp) {
    if (!key) {
      key = await askKey()
      if (!key) return
    } else {
      const a = new Alert()
      a.title = "Claude 사용량"
      a.addAction("미리보기")
      a.addAction("세션 키 변경")
      a.addDestructiveAction("세션 키 삭제")
      a.addCancelAction("닫기")
      const i = await a.presentSheet()
      if (i === 1) { key = await askKey(); if (!key) return }
      if (i === 2) {
        Keychain.remove(KEY_NAME)
        if (Keychain.contains(ORG_NAME)) Keychain.remove(ORG_NAME)
        return
      }
      if (i === -1) return
    }
  }

  let rows = null, error = null
  if (!key) error = "Scriptable 앱에서 이 스크립트를 한 번 실행해 세션 키를 입력하세요"
  else {
    try { rows = await fetchUsage(key) } catch (e) { error = e.message }
  }

  const family = config.widgetFamily || "medium"
  const widget = family.startsWith("accessory")
    ? lockScreenWidget(family, rows, error)
    : homeWidget(family, rows, error)
  widget.refreshAfterDate = new Date(Date.now() + 10 * 60 * 1000)

  if (config.runningInWidget) Script.setWidget(widget)
  else await widget.presentMedium()
}

await main()
Script.complete()
