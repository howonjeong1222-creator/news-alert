# -*- coding: utf-8 -*-
"""
DART 공시 알림 — 클라우드(GitHub Actions)용 1회 실행 버전

PC용(공시알림/공시알림.py)과 달리:
  - 10분마다 GitHub Actions가 1번 실행하고 종료 (상시 실행 아님)
  - 알림은 텔레그램만 (서버에는 화면이 없으므로 토스트 없음)
  - 텔레그램 토큰·chat_id는 GitHub Secrets(환경변수)에서 읽음
    DART_TELEGRAM_BOT_TOKEN / DART_TELEGRAM_CHAT_ID가 있으면 전용 봇 사용,
    없으면 뉴스알림과 같은 봇(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) 사용
  - 이미 알림 보낸 공시 접수번호(seen_dart.txt)는 실행 후 저장소에 커밋되어 유지됨
  - DART 페이지 접속이 안 될 때 OPENDART_API_KEY(Secrets)가 있으면 오픈DART API로 전환

로컬 테스트:  python dart_alert.py --test   (텔레그램 없이 화면 출력만)
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONF = json.loads((BASE / "config_dart.json").read_text(encoding="utf-8"))
SEEN_PATH = BASE / "seen_dart.txt"
SEEN_MAX_LINES = 20000  # 파일이 무한정 커지지 않도록 최근 기록만 유지

KST = timezone(timedelta(hours=9))  # Actions 서버는 UTC — 날짜 계산은 한국시간으로

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DartAlert/1.0",
    "Referer": "https://dart.fss.or.kr/dsac001/mainAll.do",
}
VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={}"

TEST_MODE = "--test" in sys.argv
TG_TOKEN = os.environ.get("DART_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("DART_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")
OPENDART_KEY = os.environ.get("OPENDART_API_KEY", "")


# ---------------------------------------------------------------- 공통 유틸
def strip_tags(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html.unescape(text or ""))).strip()


def find_company(text, companies):
    """공백 제거 후 부분일치. 긴(구체적인) 이름 우선."""
    nt = re.sub(r"\s+", "", text or "")
    for name in sorted(companies, key=len, reverse=True):
        if re.sub(r"\s+", "", name) in nt:
            return name
    return None


def report_allowed(report):
    nr = re.sub(r"\s+", "", report)
    for ex in CONF.get("보고서_제외키워드", []):
        if re.sub(r"\s+", "", ex) in nr:
            return False
    inc = CONF.get("보고서_포함키워드", [])
    if not inc:
        return True
    return any(re.sub(r"\s+", "", kw) in nr for kw in inc)


# ---------------------------------------------------------------- 수집기
def fetch_dart_page():
    """DART 최근공시(오늘 전체) 최신 100건 — 키 불필요, 접수 즉시 반영"""
    r = requests.post(
        "https://dart.fss.or.kr/dsac001/search.ax",
        data={"currentPage": "1", "maxResults": "100", "mdayCnt": "0",
              "selectDate": "", "textCrpCik": ""},
        headers=UA, timeout=20,
    )
    r.raise_for_status()
    r.encoding = "utf-8"

    items = []
    for row in re.findall(r"<tr>(.*?)</tr>", r.text, re.S):
        rcp = re.search(r"rcpNo=(\d+)", row)
        if not rcp:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(tds) < 5:
            continue
        comp = re.search(r"<a[^>]*openCorpInfoNew[^>]*>(.*?)</a>", tds[1], re.S)
        items.append({
            "rcp_no": rcp.group(1),
            "time": strip_tags(tds[0]),
            "company": strip_tags(comp.group(1) if comp else tds[1]),
            "report": strip_tags(tds[2]),
            "filer": strip_tags(tds[3]),
        })
    # 주말·새벽 등 공시가 1건도 없는 시간대는 빈 목록이 정상
    return items


def fetch_opendart():
    """오픈DART API — 오늘 접수 공시 최신 100건 (백업용, OPENDART_API_KEY 필요)"""
    today = datetime.now(KST).strftime("%Y%m%d")
    r = requests.get(
        "https://opendart.fss.or.kr/api/list.json",
        params={"crtfc_key": OPENDART_KEY, "bgn_de": today, "end_de": today,
                "page_count": "100", "sort": "date"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") == "013":  # 조회 결과 없음
        return []
    if data.get("status") != "000":
        raise RuntimeError(f"오픈DART 오류 {data.get('status')}: {data.get('message')}")
    return [{
        "rcp_no": it["rcept_no"],
        "time": "",
        "company": it.get("corp_name", ""),
        "report": it.get("report_nm", ""),
        "filer": it.get("flr_nm", ""),
    } for it in data.get("list", [])]


def collect():
    try:
        return fetch_dart_page()
    except Exception as e:
        if OPENDART_KEY:
            print(f"DART 페이지 오류({e}) → 오픈DART API로 전환")
            return fetch_opendart()
        raise


# ---------------------------------------------------------------- 텔레그램
def send_telegram(item, matched):
    link = VIEWER.format(item["rcp_no"])
    text = (f"📋 [{matched}] 새 공시\n{item['report']}\n"
            f"회사: {item['company']} / 제출인: {item['filer']}\n{link}")
    if TEST_MODE:
        print(f"[테스트] {text}\n")
        return True
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT, "text": text},
        timeout=15,
    )
    return r.json().get("ok", False)


# ---------------------------------------------------------------- 메인
def main():
    if not TEST_MODE and (not TG_TOKEN or not TG_CHAT):
        print("오류: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수(Secrets)가 없습니다")
        sys.exit(1)

    companies = CONF["회사목록"]
    items = collect()
    print(f"수집: {len(items)}건")

    # 첫 실행(기록 없음): 현재 공시를 기준점으로만 저장, 알림 없음
    if not SEEN_PATH.exists():
        SEEN_PATH.write_text(
            "\n".join(it["rcp_no"] for it in items) + "\n", encoding="utf-8")
        print(f"첫 실행 — 기준점 {len(items)}건 저장 (알림 없음)")
        return

    seen_lines = SEEN_PATH.read_text(encoding="utf-8").splitlines()
    seen = set(seen_lines)
    sent = 0
    # 목록은 최신순이므로 뒤집어 오래된 것부터 알림 (시간 순서 유지)
    for it in reversed(items):
        if it["rcp_no"] in seen:
            continue
        matched = find_company(it["company"], companies) or find_company(it["filer"], companies)
        if not matched or not report_allowed(it["report"]):
            # 대상이 아닌 공시도 '본 것'으로 기록 (매번 다시 검사하지 않도록)
            seen.add(it["rcp_no"])
            seen_lines.append(it["rcp_no"])
            continue
        if send_telegram(it, matched):
            seen.add(it["rcp_no"])
            seen_lines.append(it["rcp_no"])
            sent += 1
            print(f"알림: [{matched}] {it['report']} (제출인: {it['filer']})")
            time.sleep(0.5)
        else:
            print(f"전송 실패(다음 실행 때 재시도): {it['report']}")

    SEEN_PATH.write_text("\n".join(seen_lines[-SEEN_MAX_LINES:]) + "\n", encoding="utf-8")
    print(f"완료 — 새 알림 {sent}건")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
