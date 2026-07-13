# -*- coding: utf-8 -*-
"""
경제지표 대시보드 — 모바일 웹 (Flask)
- 데스크톱 앱과 동일한 데이터(야후 + 네이버)를 서버에서 수집해 JSON으로 제공.
- 폰은 브라우저로 접속만 하면 되므로 CORS 문제 없음.
- 서버측 캐시(기본 120초)로 빠르고 API 부담 적음.
로컬 실행:  python app.py   →  http://localhost:8000
클라우드:  gunicorn app:app  (Procfile 참조)
"""
import os
import json
import time
import threading
import traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import urllib.parse
import http.cookiejar

from flask import Flask, jsonify, Response

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

FX_DEFS = [("원/달러", "KRW=X", 1, 2), ("원/100엔", "JPYKRW=X", 100, 2),
           ("원/유로", "EURKRW=X", 1, 2), ("원/파운드", "GBPKRW=X", 1, 2)]
COMMO_DEFS = [("금 (온스)", "GC=F", 2), ("WTI 유가", "CL=F", 2),
              ("비트코인", "BTC-USD", 2), ("이더리움", "ETH-USD", 2)]
INDEX_DEFS = [("코스피", "^KS11", "EWY", "kospi"),
              ("S&P 500", "^GSPC", "SPY", "sp500"),
              ("니케이 225", "^N225", "EWJ", "nikkei")]
SP500_TOP5 = ["NVDA", "MSFT", "AAPL", "AMZN", "GOOGL"]
NIKKEI_TOP5 = ["7203.T", "6758.T", "8035.T"]  # 상위 3 (4위 소프트뱅크·5위 키엔스 제외)
# 코스피 목록에 고정 추가: TIGER 미국S&P500, TIGER 미국나스닥100
KOSPI_EXTRA = ["360750", "133690"]
NAVER_H = {"User-Agent": UA}


def _http_get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("배", "").replace("%", "").replace("원", "").strip())
    except Exception:
        return None


class Yahoo:
    def __init__(self):
        self.crumb = None
        self.opener = None

    def _ensure(self):
        if self.crumb:
            return
        try:
            cj = http.cookiejar.CookieJar()
            op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
            op.open(urllib.request.Request(
                "https://finance.yahoo.com/quote/SPY/",
                headers={"User-Agent": UA, "Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"}),
                timeout=12).read()
            crumb = op.open(urllib.request.Request(
                "https://query2.finance.yahoo.com/v1/test/getcrumb",
                headers={"User-Agent": UA, "Accept": "*/*"}), timeout=12).read().decode()
            if crumb and "<" not in crumb and "Unauth" not in crumb:
                self.crumb = crumb
                self.opener = op
        except Exception:
            pass

    def quote(self, symbols):
        self._ensure()
        if not self.crumb:
            return {}
        out = {}
        try:
            u = ("https://query2.finance.yahoo.com/v7/finance/quote?symbols="
                 + urllib.parse.quote(",".join(symbols)) + "&crumb=" + urllib.parse.quote(self.crumb))
            raw = self.opener.open(urllib.request.Request(
                u, headers={"User-Agent": UA, "Accept": "application/json"}), timeout=12).read().decode()
            for q in json.loads(raw).get("quoteResponse", {}).get("result", []):
                out[q.get("symbol")] = q
        except Exception:
            pass
        return out


def fetch_series(symbol):
    try:
        url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
               + urllib.parse.quote(symbol) + "?range=3mo&interval=1d")
        res = json.loads(_http_get(url))["chart"]["result"][0]
        meta = res.get("meta", {})
        ts = res.get("timestamp") or []
        cl = res["indicators"]["quote"][0]["close"]
        pairs = [(t, c) for t, c in zip(ts, cl) if c is not None]
        if not pairs:
            return None
        last_ts, last_close = pairs[-1]
        last = meta.get("regularMarketPrice") or last_close
        prev = pairs[-2][1] if len(pairs) >= 2 else last

        def ago(days):
            target = last_ts - days * 86400
            cand = [c for t, c in pairs if t <= target]
            return cand[-1] if cand else pairs[0][1]

        def pct(a, b):
            return (a - b) / b * 100 if b else None

        return {"last": last, "d1": pct(last, prev), "w1": pct(last, ago(7)), "m1": pct(last, ago(30))}
    except Exception:
        return None


def fetch_naver_stock(code):
    try:
        d = json.loads(_http_get(f"https://m.stock.naver.com/api/stock/{code}/integration", NAVER_H, 10))
        info = {"name": d.get("stockName", code), "per": None, "pbr": None}
        for it in d.get("totalInfos", []):
            if it.get("code") == "per":
                info["per"] = _num(it.get("value"))
            elif it.get("code") == "pbr":
                info["pbr"] = _num(it.get("value"))
        return info
    except Exception:
        return {"name": code, "per": None, "pbr": None}


def fetch_kospi_top5():
    try:
        d = json.loads(_http_get(
            "https://m.stock.naver.com/api/stocks/marketValue/KOSPI?page=1&pageSize=5", NAVER_H, 10))
        return [s["itemCode"] for s in d.get("stocks", [])][:5]
    except Exception:
        return ["005930", "000660", "373220", "207940", "005380"]


def fetch_naver_etf_div(symbol):
    try:
        d = json.loads(_http_get(f"https://api.stock.naver.com/stock/{symbol}/basic", NAVER_H, 10))
        for it in d.get("stockItemTotalInfos", []):
            if it.get("code") == "dividendYieldRatio":
                return _num(it.get("value"))
    except Exception:
        pass
    return None


def _val(q):
    if not q:
        return {"per": None, "pbr": None, "div": None}
    return {"per": q.get("trailingPE"), "pbr": q.get("priceToBook"), "div": None}


def _ser(d):
    d = d or {}
    return {"price": d.get("last"), "d1": d.get("d1"), "w1": d.get("w1"), "m1": d.get("m1")}


def fetch_all():
    result = {"fx": {}, "indices": {}, "commodities": {}, "top5": {}, "ts": None}
    price_syms = [d[1] for d in FX_DEFS] + [d[1] for d in COMMO_DEFS] + [d[1] for d in INDEX_DEFS]
    fund_syms = [d[2] for d in INDEX_DEFS] + SP500_TOP5 + NIKKEI_TOP5

    yahoo = Yahoo()
    with ThreadPoolExecutor(max_workers=16) as ex:
        fund_fut = ex.submit(yahoo.quote, fund_syms)
        rank_fut = ex.submit(fetch_kospi_top5)
        etf_div_futs = {etf: ex.submit(fetch_naver_etf_div, etf) for _, _, etf, _ in INDEX_DEFS}
        kospi_codes = rank_fut.result() + KOSPI_EXTRA  # 시총 top5 + 고정 ETF 2종
        kospi_futs = {c: ex.submit(fetch_naver_stock, c) for c in kospi_codes}
        top5_syms = [c + ".KS" for c in kospi_codes] + SP500_TOP5 + NIKKEI_TOP5
        series_futs = {s: ex.submit(fetch_series, s) for s in price_syms + top5_syms}
        series = {}
        for s, f in series_futs.items():
            try:
                series[s] = f.result()
            except Exception:
                series[s] = None
        qmap = fund_fut.result()

        for name, sym, mult, dig in FX_DEFS:
            s = series.get(sym)
            result["fx"][name] = ({"last": s["last"] * mult, "d1": s["d1"], "w1": s["w1"],
                                   "m1": s["m1"], "dig": dig} if s else None)
        for name, sym, dig in COMMO_DEFS:
            s = series.get(sym)
            result["commodities"][name] = ({"last": s["last"], "d1": s["d1"], "w1": s["w1"],
                                            "m1": s["m1"], "dig": dig} if s else None)
        for name, sym, etf, kind in INDEX_DEFS:
            val = _val(qmap.get(etf))
            try:
                nd = etf_div_futs[etf].result()
                if nd is not None:
                    val["div"] = nd
            except Exception:
                pass
            result["indices"][name] = {"quote": series.get(sym), "val": val}
            lst = []
            if kind == "kospi":
                for c in kospi_codes:
                    try:
                        nv = kospi_futs[c].result()
                    except Exception:
                        nv = {"name": c, "per": None, "pbr": None}
                    e = _ser(series.get(c + ".KS"))
                    e.update({"name": nv["name"], "per": nv["per"], "pbr": nv["pbr"]})
                    lst.append(e)
            else:
                codes = SP500_TOP5 if kind == "sp500" else NIKKEI_TOP5
                for c in codes:
                    q = qmap.get(c) or {}
                    e = _ser(series.get(c))
                    e.update({"name": q.get("shortName") or c,
                              "per": q.get("trailingPE"), "pbr": q.get("priceToBook")})
                    lst.append(e)
            result["top5"][name] = lst

    result["ts"] = datetime.now().strftime("%m/%d %H:%M")
    result["index_order"] = [d[0] for d in INDEX_DEFS]
    result["fx_order"] = [d[0] for d in FX_DEFS]
    result["commo_order"] = [d[0] for d in COMMO_DEFS]
    return result


# ================= 캐시 =================
CACHE_TTL = int(os.environ.get("CACHE_TTL", "120"))
_cache = {"data": None, "at": 0}
_lock = threading.Lock()


def get_data(force=False):
    now = time.time()
    with _lock:
        if not force and _cache["data"] and now - _cache["at"] < CACHE_TTL:
            return _cache["data"]
    data = fetch_all()
    with _lock:
        _cache["data"] = data
        _cache["at"] = time.time()
    return data


# ================= Flask =================
app = Flask(__name__)


@app.route("/api/data")
def api_data():
    force = False
    try:
        from flask import request
        force = request.args.get("force") == "1"
    except Exception:
        pass
    try:
        return jsonify(get_data(force=force))
    except Exception:
        return jsonify({"error": traceback.format_exc()}), 500


@app.route("/manifest.json")
def manifest():
    m = {
        "name": "경제지표 대시보드", "short_name": "경제지표",
        "start_url": "/", "display": "standalone",
        "background_color": "#0f1218", "theme_color": "#0f1218",
        "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
    }
    return Response(json.dumps(m), mimetype="application/manifest+json")


@app.route("/icon.svg")
def icon():
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">'
           '<rect width="192" height="192" rx="36" fill="#0f1218"/>'
           '<text x="96" y="128" font-size="110" text-anchor="middle">📊</text></svg>')
    return Response(svg, mimetype="image/svg+xml")


@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0f1218">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<link rel="apple-touch-icon" href="/icon.svg">
<title>경제지표 대시보드</title>
<style>
  :root{ --bg:#0f1218; --card:#1a1f29; --fg:#e8ebf0; --sub:#9aa3b2;
         --acc:#4dabf7; --up:#e64545; --down:#2f7ae0; --gold:#ffd43b; --line:#252b36; }
  *{box-sizing:border-box; -webkit-tap-highlight-color:transparent;}
  body{margin:0; background:var(--bg); color:var(--fg);
       font-family:"Malgun Gothic","맑은 고딕",-apple-system,system-ui,sans-serif;
       padding:12px 12px 40px; max-width:640px; margin:0 auto;}
  header{display:flex; align-items:center; justify-content:space-between; margin-bottom:10px;}
  h1{font-size:18px; margin:0;}
  .meta{font-size:12px; color:var(--sub);}
  button{background:var(--card); color:var(--acc); border:none; border-radius:10px;
         padding:7px 12px; font-size:13px; font-weight:600;}
  .card{background:var(--card); border-radius:14px; padding:12px 14px; margin-bottom:10px;}
  .ctitle{font-size:14px; font-weight:700; color:var(--acc); margin-bottom:8px;}
  .idxval{font-size:24px; font-weight:800; letter-spacing:.3px;}
  .badges{display:flex; gap:8px; flex-wrap:wrap; margin:6px 0 4px;}
  .badge{font-size:12px; background:#0e1220; border:1px solid var(--line);
         border-radius:8px; padding:3px 8px;}
  .badge b{color:var(--sub); font-weight:600; margin-right:4px;}
  .val{font-size:12px; color:var(--gold); margin:2px 0 2px;}
  .note{font-size:11px; color:var(--sub); margin-bottom:8px;}
  .up{color:var(--up);} .down{color:var(--down);} .flat{color:var(--sub);}
  table{width:100%; border-collapse:collapse; font-size:12px;}
  th{color:var(--sub); font-weight:600; text-align:right; padding:3px 2px; font-size:11px;}
  th:first-child, td:first-child{text-align:left;}
  td{padding:4px 2px; text-align:right; border-top:1px solid var(--line); white-space:nowrap;}
  td.nm{color:var(--fg); max-width:96px; overflow:hidden; text-overflow:ellipsis;}
  td.per,td.pbr{color:var(--sub);}
  .row{display:flex; justify-content:space-between; align-items:center; padding:6px 0;
       border-top:1px solid var(--line);}
  .row:first-of-type{border-top:none;}
  .rlabel{color:var(--sub); font-size:13px; width:74px;}
  .rval{font-size:16px; font-weight:700; width:96px; text-align:right;}
  .rbadges{flex:1; display:flex; gap:6px; justify-content:flex-end; font-size:11px;}
  .rbadges span{min-width:52px; text-align:right;}
  .loading{text-align:center; color:var(--sub); padding:30px;}
  .err{color:var(--up); font-size:13px; white-space:pre-wrap;}
</style>
</head>
<body>
<header>
  <h1>📊 경제지표 대시보드</h1>
  <div style="display:flex; align-items:center; gap:8px;">
    <span class="meta" id="ts"></span>
    <button id="rf">⟳</button>
  </div>
</header>
<div id="app"><div class="loading">불러오는 중…</div></div>

<script>
function pct(v,d){ if(v==null) return "—"; const a=v>0?"▲":(v<0?"▼":"―"); return a+Math.abs(v).toFixed(d==null?2:d)+"%"; }
function ccls(v){ return v==null?"flat":(v>0?"up":(v<0?"down":"flat")); }
function num(v,dig){ if(v==null) return "—"; return v.toLocaleString("en-US",{minimumFractionDigits:dig,maximumFractionDigits:dig}); }
function price(v){ if(v==null) return "—"; return Math.abs(v)>=1000 ? Math.round(v).toLocaleString("en-US") : v.toLocaleString("en-US",{minimumFractionDigits:2,maximumFractionDigits:2}); }

function badges(q){
  return `<div class="badges">
    <span class="badge"><b>일간</b><span class="${ccls(q.d1)}">${pct(q.d1)}</span></span>
    <span class="badge"><b>주간</b><span class="${ccls(q.w1)}">${pct(q.w1)}</span></span>
    <span class="badge"><b>월간</b><span class="${ccls(q.m1)}">${pct(q.m1)}</span></span>
  </div>`;
}

function top5(list, label){
  let rows = list.map(s=>`<tr>
    <td class="nm">${s.name||""}</td>
    <td>${price(s.price)}</td>
    <td class="${ccls(s.d1)}">${pct(s.d1,1)}</td>
    <td class="${ccls(s.w1)}">${pct(s.w1,1)}</td>
    <td class="${ccls(s.m1)}">${pct(s.m1,1)}</td>
    <td class="per">${num(s.per,1)}</td>
    <td class="pbr">${num(s.pbr,2)}</td>
  </tr>`).join("");
  return `<table><thead><tr>
    <th>${label||"시총 상위 5"}</th><th>현재가</th><th>일</th><th>주</th><th>월</th><th>PER</th><th>PBR</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

function indexCard(name, d, top){
  const q = d.quote||{}, v = d.val||{};
  return `<div class="card">
    <div class="ctitle">📈 ${name}</div>
    <div class="idxval">${num(q.last,2)}</div>
    ${badges(q)}
    <div class="val">PER ${num(v.per,2)} · PBR ${num(v.pbr,2)} · 배당 ${num(v.div,2)}%</div>
    <div class="note">※ 밸류에이션은 국가대표 ETF 프록시 기준(EWY/SPY/EWJ)</div>
    ${top5(top||[], name==="코스피" ? "주요 종목" : "시총 상위 5")}
  </div>`;
}

function quoteCard(title, order, map){
  let rows = order.map(name=>{
    const q = map[name];
    if(!q) return `<div class="row"><div class="rlabel">${name}</div><div class="rval flat">—</div><div class="rbadges"></div></div>`;
    return `<div class="row">
      <div class="rlabel">${name}</div>
      <div class="rval">${num(q.last,q.dig)}</div>
      <div class="rbadges">
        <span class="${ccls(q.d1)}">${pct(q.d1)}</span>
        <span class="${ccls(q.w1)}">${pct(q.w1)}</span>
        <span class="${ccls(q.m1)}">${pct(q.m1)}</span>
      </div>
    </div>`;
  }).join("");
  return `<div class="card"><div class="ctitle">${title}</div>${rows}</div>`;
}

function render(data){
  if(data.error){ document.getElementById("app").innerHTML = `<div class="card err">⚠ 오류\n${data.error}</div>`; return; }
  document.getElementById("ts").textContent = "업데이트 " + (data.ts||"");
  let html = "";
  for(const name of data.index_order){
    html += indexCard(name, data.indices[name]||{}, (data.top5||{})[name]);
  }
  html += quoteCard("💱 환율", data.fx_order, data.fx);
  html += quoteCard("🪙 금 · 유가 · 코인", data.commo_order, data.commodities);
  document.getElementById("app").innerHTML = html;
}

let busy=false;
async function load(force){
  if(busy) return; busy=true;
  const btn=document.getElementById("rf"); btn.textContent="…";
  try{
    const r = await fetch("/api/data"+(force?"?force=1":""));
    render(await r.json());
  }catch(e){
    document.getElementById("app").innerHTML = `<div class="card err">⚠ 네트워크 오류: ${e}</div>`;
  }finally{ busy=false; btn.textContent="⟳"; }
}
document.getElementById("rf").addEventListener("click", ()=>load(true));
load(false);
setInterval(()=>load(false), 180000); // 3분마다 자동 갱신
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
