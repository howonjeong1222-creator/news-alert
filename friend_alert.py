# -*- coding: utf-8 -*-
"""
친구 기자 기사 알림 — 클라우드(GitHub Actions)용 1회 실행 버전

PC용(친구기사알림/친구기사알림.py)과 달리:
  - 10분마다 GitHub Actions가 1번 실행하고 종료 (상시 실행 아님)
  - 알림은 텔레그램만 (서버에는 화면이 없으므로 토스트 없음)
  - 텔레그램 토큰·chat_id는 GitHub Secrets(환경변수)에서 읽음
    FRIEND_TELEGRAM_BOT_TOKEN / FRIEND_TELEGRAM_CHAT_ID가 있으면 전용 봇 사용,
    없으면 뉴스알림과 같은 봇(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) 사용
  - 이미 알림 보낸 기사(seen_friends.txt)는 실행 후 저장소에 커밋되어 유지됨

수집: config_friend.json의 각 기자 네이버 기자홈 페이지에서 기사 목록 추출.

로컬 테스트:  python friend_alert.py --test   (텔레그램 없이 화면 출력만)
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

KST = timezone(timedelta(hours=9))  # Actions 서버는 UTC — 날짜 판정은 한국시간으로

BASE = Path(__file__).resolve().parent
CONF = json.loads((BASE / "config_friend.json").read_text(encoding="utf-8"))
SEEN_PATH = BASE / "seen_friends.txt"
SEEN_MAX_LINES = 20000  # 파일이 무한정 커지지 않도록 최근 기록만 유지

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FriendAlert/1.0"}

TEST_MODE = "--test" in sys.argv
TG_TOKEN = os.environ.get("FRIEND_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("FRIEND_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------- 날짜 판정
def is_recent(date_str, days=3):
    """목록에 표시된 날짜가 최근인지 판정.

    네이버 기자홈은 요청마다 목록 구성이 조금씩 바뀌어 오래된 기사가
    끼어들 수 있음 — 날짜가 최근일 때만 알림해 오탐을 막는다.
    형식: "방금전" / "3분전" / "2시간전" / "1일전" / "2026.10.21." / "2026-11-02"
    """
    s = re.sub(r"\s+", "", date_str or "")
    if not s:
        return False
    if re.search(r"(방금|분전|시간전)", s):
        return True
    m = re.match(r"(\d+)일전", s)
    if m:
        return int(m.group(1)) <= days
    m = re.search(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", s)
    if m:
        d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=KST)
        return (datetime.now(KST) - d).days <= days
    return False


# ---------------------------------------------------------------- 수집기
def fetch_articles(journalist_url):
    """기자 페이지에서 기사 목록 추출 (최신 기사가 위쪽). 주소로 사이트 자동 구분.

    반환: [{key, url, title, date}, ...]
      key = "언론사번호/기사번호" (네이버) 또는 "kyeongin/기사번호" — 중복 방지용 고유값
    """
    if "kyeongin.com" in journalist_url:
        return fetch_kyeongin(journalist_url)
    return fetch_naver(journalist_url)


def fetch_kyeongin(journalist_url):
    """경인일보 기자 페이지 (www.kyeongin.com/reporter/아이디)"""
    r = requests.get(journalist_url, headers=UA, timeout=20)
    r.raise_for_status()

    items = []
    found = set()
    for m in re.finditer(
            r'<h2 class="headline">\s*<a href="//www\.kyeongin\.com/article/(\d+)"[^>]*>\s*(.*?)\s*</a>',
            r.text, re.S):
        key = f"kyeongin/{m.group(1)}"
        if key in found:
            continue
        found.add(key)
        # 날짜: 해당 기사 블록 뒤쪽 byline의 <span class="date">2026-10-31</span>
        d = re.search(r'<span class="date">([\d-]+)</span>', r.text[m.end():m.end() + 2000])
        items.append({
            "key": key,
            "url": f"https://www.kyeongin.com/article/{m.group(1)}",
            "title": html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(),
            "date": d.group(1) if d else "",
        })
    if not items:
        raise RuntimeError("기사 목록이 비어 있음 (페이지 구조 변경 또는 접속 차단 가능성)")
    return items


def fetch_naver(journalist_url):
    """네이버 기자홈 (media.naver.com/journalist/...)"""
    r = requests.get(journalist_url, headers=UA, timeout=20)
    r.raise_for_status()

    items = []
    found = set()
    for block in re.findall(
            r'<li class="press_edit_news_item.*?</li>', r.text, re.S):
        m = re.search(r'href="https://n\.news\.naver\.com/article/(\d+)/(\d+)', block)
        if not m:
            continue
        key = f"{m.group(1)}/{m.group(2)}"
        if key in found:
            continue
        found.add(key)
        t = re.search(r'press_edit_news_title">(.*?)</span>', block, re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip() if t else "(제목 없음)"
        # 날짜: 오래된 기사는 "2026.10.21.", 최근 기사는 "3시간전" 형식
        d = re.search(r"<b>([^<]+)</b>", block)
        items.append({
            "key": key,
            "url": f"https://n.news.naver.com/article/{m.group(1)}/{m.group(2)}",
            "title": title,
            "date": d.group(1).strip() if d else "",
        })
    if not items:
        raise RuntimeError("기사 목록이 비어 있음 (페이지 구조 변경 또는 접속 차단 가능성)")
    return items


# ---------------------------------------------------------------- 텔레그램
def send_telegram(reporter, item):
    who = f"{reporter['이름']} ({reporter['소속']})"
    text = f"📰 {who} 새 기사\n{item['title']}\n{item['url']}"
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

    reporters = CONF["기자목록"]
    all_items = []  # (reporter, item) — 수집 성공한 기자만
    for reporter in reporters:
        try:
            items = fetch_articles(reporter["기자홈"])
            print(f"수집: {reporter['이름']} ({reporter['소속']}) {len(items)}건")
            # 목록은 최신순이므로 뒤집어 오래된 것부터 (시간 순서 유지)
            all_items.extend((reporter, it) for it in reversed(items))
        except Exception as e:
            print(f"⚠ {reporter['이름']} 기자홈 수집 실패: {e}")
        time.sleep(1)  # 기자 간 1초 간격 (네이버 부담 최소화)

    if not all_items:
        print("수집된 기사가 없어 종료 (다음 실행 때 재시도)")
        return

    # 첫 실행(기록 없음): 현재 기사를 기준점으로만 저장, 알림 없음
    if not SEEN_PATH.exists():
        SEEN_PATH.write_text(
            "\n".join(it["key"] for _, it in all_items) + "\n", encoding="utf-8")
        print(f"첫 실행 — 기준점 {len(all_items)}건 저장 (알림 없음)")
        return

    seen_lines = SEEN_PATH.read_text(encoding="utf-8").splitlines()
    seen = set(seen_lines)
    sent = 0
    for reporter, it in all_items:
        if it["key"] in seen:
            continue
        if not is_recent(it["date"]):
            # 오래된 기사가 목록에 회전 노출된 것 — 기록만 하고 알림 생략
            seen.add(it["key"])
            seen_lines.append(it["key"])
            continue
        if send_telegram(reporter, it):
            seen.add(it["key"])
            seen_lines.append(it["key"])
            sent += 1
            print(f"알림: [{reporter['이름']}] {it['title']}")
            time.sleep(0.5)
        else:
            print(f"전송 실패(다음 실행 때 재시도): {it['title']}")

    SEEN_PATH.write_text("\n".join(seen_lines[-SEEN_MAX_LINES:]) + "\n", encoding="utf-8")
    print(f"완료 — 새 알림 {sent}건")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
