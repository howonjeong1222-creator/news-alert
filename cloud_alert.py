# -*- coding: utf-8 -*-
"""
뉴스 키워드 알림 — 클라우드(GitHub Actions)용 1회 실행 버전

PC용(뉴스알림.py)과 달리:
  - 10분마다 GitHub Actions가 1번 실행하고 종료 (상시 실행 아님)
  - 알림은 텔레그램만 (서버에는 화면이 없으므로 토스트 없음)
  - 텔레그램 토큰·chat_id는 GitHub Secrets(환경변수)에서 읽음 (코드에 비밀값 없음)
  - 이미 알림 보낸 기사 목록(seen_links.txt)은 실행 후 저장소에 커밋되어 유지됨
"""

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONF = json.loads((BASE / "config_cloud.json").read_text(encoding="utf-8"))
SEEN_PATH = BASE / "seen_links.txt"
SEEN_MAX_LINES = 30000  # 파일이 무한정 커지지 않도록 최근 기록만 유지

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NewsAlert/1.0"}

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


# ---------------------------------------------------------------- 공통 유틸
def strip_tags(text):
    return re.sub(r"<[^>]+>", "", html.unescape(text or "")).strip()


def title_fp(title):
    return "T:" + re.sub(r"[^0-9A-Za-z가-힣]", "", title)


def find_keyword(text, keywords):
    nt = re.sub(r"\s+", "", text)
    for kw in sorted(keywords, key=len, reverse=True):
        if re.sub(r"\s+", "", kw) in nt:
            return kw
    return None


def press_allowed(item, filt):
    if not filt or not filt.get("사용", False):
        return True
    press = re.sub(r"\s+", "", item["source"].split("/", 1)[-1])
    for ex in filt.get("제외", []):
        if re.sub(r"\s+", "", ex) in press:
            return False
    for ok in filt.get("허용", []):
        if re.sub(r"\s+", "", ok) in press:
            return True
    return False


def is_important(item):
    if "연합뉴스" in item["source"]:
        return True
    return bool(re.search(r"[\[\(<【]\s*단독", item["title"]))


# ---------------------------------------------------------------- 수집기
def fetch_google(keywords):
    items = []
    for i in range(0, len(keywords), 8):
        batch = keywords[i:i + 8]
        q = " OR ".join(f'"{kw}"' for kw in batch)
        try:
            r = requests.get(
                "https://news.google.com/rss/search",
                params={"q": f"{q} when:1d", "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                headers=UA,
                timeout=15,
            )
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            print(f"구글뉴스 RSS 오류({batch[0]} 외): {e}")
            continue
        for it in root.iter("item"):
            title = strip_tags(it.findtext("title", ""))
            press = strip_tags(it.findtext("source", ""))
            if press and title.endswith(" - " + press):
                title = title[: -len(" - " + press)].strip()
            kw = find_keyword(title, keywords) or "본문매칭"
            items.append({
                "keyword": kw,
                "source": f"구글뉴스/{press}" if press else "구글뉴스",
                "title": title,
                "link": (it.findtext("link", "") or "").strip(),
            })
    return items


def fetch_yonhap(url, keywords):
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"연합뉴스 RSS 오류: {e}")
        return []
    items = []
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title", ""))
        desc = strip_tags(it.findtext("description", ""))
        kw = find_keyword(title + " " + desc, keywords)
        if kw:
            items.append({
                "keyword": kw,
                "source": "연합뉴스",
                "title": title,
                "link": (it.findtext("link", "") or "").strip(),
            })
    return items


# ---------------------------------------------------------------- 텔레그램
def send_telegram(item):
    star = "★ " if is_important(item) else ""
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={
            "chat_id": TG_CHAT,
            "text": f"🔔 {star}[{item['keyword']}] {item['source']}\n{item['title']}\n{item['link']}",
        },
        timeout=15,
    )
    return r.json().get("ok", False)


# ---------------------------------------------------------------- 메인
def main():
    if not TG_TOKEN or not TG_CHAT:
        print("오류: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수(Secrets)가 없습니다")
        sys.exit(1)

    keywords = CONF["키워드"]
    filt = CONF.get("언론사필터", {})

    items = fetch_google(keywords)
    for url in CONF.get("연합뉴스RSS", []):
        items += fetch_yonhap(url, keywords)
    print(f"수집: {len(items)}건")

    # 첫 실행(기록 없음): 현재 기사를 기준점으로만 저장, 알림 없음
    if not SEEN_PATH.exists():
        lines = set()
        for it in items:
            if it["link"]:
                lines.add(it["link"])
                lines.add(title_fp(it["title"]))
        SEEN_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"첫 실행 — 기준점 {len(lines)}건 저장 (알림 없음)")
        return

    seen_lines = SEEN_PATH.read_text(encoding="utf-8").splitlines()
    seen = set(seen_lines)
    sent = 0
    for it in items:
        fp = title_fp(it["title"])
        if not it["link"] or it["link"] in seen or fp in seen:
            continue
        if not press_allowed(it, filt):
            # 필터에 걸러진 기사도 '본 것'으로 기록 (나중에 재알림 방지)
            seen.update([it["link"], fp])
            seen_lines += [it["link"], fp]
            continue
        if send_telegram(it):
            seen.update([it["link"], fp])
            seen_lines += [it["link"], fp]
            sent += 1
            print(f"알림: [{it['keyword']}] ({it['source']}) {it['title']}")
            time.sleep(0.5)
        else:
            print(f"전송 실패(다음 실행 때 재시도): {it['title']}")

    SEEN_PATH.write_text("\n".join(seen_lines[-SEEN_MAX_LINES:]) + "\n", encoding="utf-8")
    print(f"완료 — 새 알림 {sent}건")


if __name__ == "__main__":
    main()
