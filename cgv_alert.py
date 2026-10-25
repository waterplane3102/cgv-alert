"""
CGV 예매 오픈 알리미
- 지정한 극장/날짜의 상영 시간표를 주기적으로 조회하여,
  조건(영화명·상영관 포맷)에 맞는 회차가 새로 열리면 알림을 보냅니다.
- 알림 수단: ntfy(휴대폰 푸시, 가입 불필요) / 텔레그램 / 디스코드 / 윈도우 알림음
- 예매 자체는 하지 않습니다(알림 전용).

실행: python cgv_alert.py            (config.toml 사용, PC에서 상시 감시)
      python cgv_alert.py --duration 4   (4분간 감시 후 종료, GitHub Actions용)
      python cgv_alert.py --once     (1회 조회 후 종료, 동작 확인용)
      python cgv_alert.py --test-notify  (알림 전송만 시험)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import tomllib
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
STATE_DIR = Path(os.environ.get("STATE_DIR", BASE_DIR))
STATE_PATH = STATE_DIR / "seen.json"
ERROR_PATH = STATE_DIR / "error_count.txt"
KST = timezone(timedelta(hours=9))  # 클라우드 서버는 UTC이므로 한국 시간을 명시


def now_kst() -> datetime:
    return datetime.now(KST)

API_BASE = "https://cgv.co.kr/api/v1"
CO_CD = "A420"  # CGV 회사 코드 (사이트 요청에서 확인)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://cgv.co.kr/cnm/movieBook/cinema",
}

# Cloudflare가 일반 requests를 막는 경우를 대비해, curl_cffi가 설치되어 있으면 크롬으로 위장해 요청
try:
    from curl_cffi import requests as http  # type: ignore

    def _get(url: str, **kw):
        return http.get(url, impersonate="chrome", **kw)

    HTTP_BACKEND = "curl_cffi"
except ImportError:
    import requests as http  # type: ignore

    def _get(url: str, **kw):
        return http.get(url, **kw)

    HTTP_BACKEND = "requests"


def log(msg: str) -> None:
    print(f"[{now_kst():%m-%d %H:%M:%S}] {msg}", flush=True)


# ───────────────────────── 설정 / 상태 ─────────────────────────
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"설정 파일이 없습니다: {CONFIG_PATH}\nconfig.example.toml을 복사해 config.toml로 만들어 주세요.")
    with CONFIG_PATH.open("rb") as f:
        cfg = tomllib.load(f)
    n = cfg.setdefault("notify", {})
    for key in ("ntfy_topic", "telegram_token", "telegram_chat_id", "discord_webhook"):
        env = os.environ.get(key.upper())
        if env:
            n[key] = env
    return cfg


def load_seen() -> set[str]:
    try:
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def load_errors() -> int:
    try:
        return int(ERROR_PATH.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_errors(n: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ERROR_PATH.write_text(str(n))


def save_seen(seen: set[str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1), encoding="utf-8")


# ───────────────────────── CGV 조회 ─────────────────────────
def api(path: str, **params) -> dict:
    params = {"coCd": CO_CD, **params}
    url = f"{API_BASE}/{path}?{urllib.parse.urlencode(params)}"
    res = _get(url, headers=HEADERS, timeout=15)
    res.raise_for_status()
    body = res.json()
    if body.get("statusCode") != 0:
        raise RuntimeError(f"API 오류: {body.get('statusMessage')}")
    return body


def find_site_no(site_name: str) -> str:
    """극장 이름(예: '여의도')으로 극장 코드를 찾습니다."""
    body = api("content/site/searchAllRegionAndSite")
    sites = []

    def walk(o):
        if isinstance(o, dict):
            if "siteNo" in o and "siteNm" in o:
                sites.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(body.get("data"))
    exact = [s for s in sites if s["siteNm"] == site_name]
    if exact:
        return exact[0]["siteNo"]
    partial = sorted({s["siteNm"] for s in sites if site_name in s["siteNm"]})
    raise SystemExit(f"극장 '{site_name}'을 찾지 못했습니다. 비슷한 이름: {partial or '없음'}")


def open_dates(site_no: str) -> set[str]:
    """해당 극장에서 시간표가 열린 날짜 목록(YYYYMMDD)."""
    body = api("booking/searchSiteScnscYmdListBySite", siteNo=site_no)
    return {d["scnYmd"] for d in body.get("data") or []}


def screenings(site_no: str, ymd: str) -> list[dict]:
    body = api("booking/searchMovScnInfo", siteNo=site_no, scnYmd=ymd, rtctlScopCd="08")
    return body.get("data") or []


def match(item: dict, movie_keywords: list[str], formats: list[str]) -> bool:
    title = (item.get("movNm") or item.get("prodNm") or "").replace(" ", "")
    if not all(k.replace(" ", "") in title for k in movie_keywords):
        return False
    if not formats:
        return True
    fmt_text = " ".join(
        str(item.get(k) or "") for k in ("tcscnsGradNm", "scnsNm", "scnsEnm", "movkndDsplNm")
    ).upper()
    return any(f.upper() in fmt_text for f in formats)


def fmt_time(hhmm: str) -> str:
    return f"{hhmm[:2]}:{hhmm[2:]}" if len(hhmm or "") == 4 else (hhmm or "?")


def booking_url(site_no: str, site_name: str, ymd: str) -> str:
    q = urllib.parse.urlencode({"siteNo": site_no, "siteNm": site_name, "scnYmd": ymd})
    return f"https://cgv.co.kr/cnm/movieBook/cinema?{q}"


# ───────────────────────── 알림 ─────────────────────────
def notify(cfg: dict, title: str, body: str, url: str | None = None) -> None:
    n = cfg.get("notify", {})
    sent = False

    topic = n.get("ntfy_topic")
    if topic:
        try:
            headers = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "movie_camera"}
            if url:
                headers["Click"] = url
            http.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"), headers=headers, timeout=10)
            sent = True
        except Exception as e:
            log(f"ntfy 전송 실패: {e}")

    token, chat_id = n.get("telegram_token"), n.get("telegram_chat_id")
    if token and chat_id:
        try:
            text = f"{title}\n{body}" + (f"\n{url}" if url else "")
            http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text}, timeout=10)
            sent = True
        except Exception as e:
            log(f"텔레그램 전송 실패: {e}")

    hook = n.get("discord_webhook")
    if hook:
        try:
            content = f"**{title}**\n{body}" + (f"\n{url}" if url else "")
            http.post(hook, json={"content": content}, timeout=10)
            sent = True
        except Exception as e:
            log(f"디스코드 전송 실패: {e}")

    if n.get("beep", True) and sys.platform == "win32":
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1200, 300)
        except Exception:
            pass

    log(f"알림: {title} / {body.replace(chr(10), ' | ')}" + ("" if sent else " (원격 알림 미설정)"))


# ───────────────────────── 메인 루프 ─────────────────────────
def check_once(cfg: dict, site_no: str, seen: set[str]) -> int:
    w = cfg["watch"]
    site_name, dates = w["theater"], w["dates"]
    keywords, formats = w["movie_keywords"], w.get("formats", [])

    available = open_dates(site_no)
    new_count = 0
    for ymd in dates:
        if ymd not in available:
            log(f"{ymd}: 시간표 미오픈")
            continue
        found = [s for s in screenings(site_no, ymd) if match(s, keywords, formats)]
        fresh = []
        for s in found:
            key = f"{ymd}|{s.get('scnsNo')}|{s.get('scnSseq')}"
            if key not in seen:
                seen.add(key)
                fresh.append(s)
        log(f"{ymd}: 조건 일치 {len(found)}회차 (신규 {len(fresh)})")
        if fresh:
            fresh.sort(key=lambda s: s.get("scnsrtTm") or "")
            lines = [
                f"{fmt_time(s.get('scnsrtTm'))} {s.get('scnsNm', '')} 잔여 {s.get('frSeatCnt', '?')}석"
                for s in fresh
            ]
            title = f"[CGV {site_name}] {fresh[0].get('movNm')} 예매 오픈 ({ymd[4:6]}/{ymd[6:]})"
            notify(cfg, title, "\n".join(lines), booking_url(site_no, site_name, ymd))
            new_count += len(fresh)
    return new_count


def main() -> None:
    ap = argparse.ArgumentParser(description="CGV 예매 오픈 알리미")
    ap.add_argument("--once", action="store_true", help="1회만 조회하고 종료")
    ap.add_argument("--duration", type=float, default=None,
                    help="지정한 분(min) 동안만 감시하고 종료 (GitHub Actions용, 0이면 1회)")
    ap.add_argument("--test-notify", action="store_true", help="알림 전송 시험")
    ap.add_argument("--reset", action="store_true", help="이미 알린 회차 기록 초기화")
    args = ap.parse_args()

    cfg = load_config()
    if args.test_notify:
        notify(cfg, "[CGV 알리미] 테스트", "알림이 정상적으로 도착했습니다.")
        return
    if args.reset:
        for p in (STATE_PATH, ERROR_PATH):
            p.unlink(missing_ok=True)

    w = cfg["watch"]
    today = now_kst().strftime("%Y%m%d")
    if all(d < today for d in w["dates"]):
        log("감시 대상 날짜가 모두 지났습니다. 종료합니다.")
        return

    site_no = w.get("theater_code") or find_site_no(w["theater"])
    log(f"감시 시작: {w['theater']}({site_no}) / {w['movie_keywords']} / {w.get('formats')} / {w['dates']} "
        f"[{HTTP_BACKEND}]")

    seen = load_seen()
    errors = load_errors()
    interval = max(60, int(w.get("interval_sec", 120)))  # 서버 부담 방지를 위해 최소 60초
    deadline = None
    if args.duration is not None:
        deadline = time.monotonic() + args.duration * 60
        interval = 60  # 짧은 실행 시간 안에서는 1분 간격

    while True:
        try:
            check_once(cfg, site_no, seen)
            save_seen(seen)
            if errors >= 3:
                notify(cfg, "[CGV 알리미] 조회 복구", "CGV 조회가 다시 정상 동작합니다.")
            errors = 0
        except Exception as e:
            errors += 1
            log(f"조회 오류({errors}회 연속): {e}")
            if errors == 3 or errors % 50 == 0:
                notify(cfg, "[CGV 알리미] 조회 실패",
                       f"CGV 조회가 {errors}회 연속 실패했습니다.\n{str(e)[:200]}")
            if "403" in str(e) and HTTP_BACKEND == "requests":
                log("403 차단 시: pip install curl_cffi 후 다시 실행해 보세요.")
        save_errors(errors)

        if args.once or args.duration == 0:
            break
        wait = interval + random.randint(-10, 10)
        if deadline is not None and time.monotonic() + wait > deadline:
            break
        if all(d < now_kst().strftime("%Y%m%d") for d in w["dates"]):
            log("감시 대상 날짜가 모두 지나 종료합니다.")
            break
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("사용자 종료")
