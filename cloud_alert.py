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
    """제외 목록은 언론사명뿐 아니라 출처 도메인(예: sentv.co.kr)으로도 매칭"""
    if not filt or not filt.get("사용", False):
        return True
    press = re.sub(r"\s+", "", item["source"].split("/", 1)[-1])
    press_url = item.get("press_url", "")
    for ex in filt.get("제외", []):
        ex_clean = re.sub(r"\s+", "", ex)
        if ex_clean in press or (press_url and ex_clean.lower() in press_url):
            return False
    for ok in filt.get("허용", []):
        if re.sub(r"\s+", "", ok) in press:
            return True
    return False


def blocked_by_word(item, words):
    """제외어 — 제목(연합뉴스는 요약문 포함)에 있으면 알림 제외 (스포츠단·공연장 기사 등)"""
    text = item["title"] + " " + item.get("desc", "")
    return any(w in text for w in words)


def resolve_url(link):
    """구글뉴스 링크를 실제 기사 주소로 변환 (실패 시 원본 그대로 반환)"""
    try:
        if "news.google.com" in link:
            from googlenewsdecoder import gnewsdecoder
            r = gnewsdecoder(link)
            if r.get("success"):
                return r["decoded_url"]
    except Exception:
        pass
    return link


def is_sports_url(url):
    low = url.lower()
    return "/sports" in low or "sports." in low or "/sport/" in low


def is_sports_page(html_text):
    """기사 페이지의 섹션 메타태그(article:section, articleSection, genre 등)가
    스포츠·연예면 True (SBS처럼 주소에 /sports가 없는 언론사 대응)"""
    for tag in re.findall(r"<meta[^>]+>", html_text):
        tl = tag.lower()
        if ("section" in tl or "genre" in tl) and (
                "스포츠" in tag or "sports" in tl or "연예" in tag or "entertain" in tl):
            return True
    return False


def blocked_by_body(item, words):
    """알림 직전 검사 — 모든 기사: 실제 기사 주소나 섹션 메타태그가 스포츠면이면 차단.
    본문매칭 기사는 추가로 본문을 열어 제외어 검사.
    확인에 실패하면 차단하지 않음 (알림 누락 방지)"""
    try:
        url = resolve_url(item["link"])
        if is_sports_url(url):
            return True  # 기사 주소 자체가 스포츠면
        resp = requests.get(url, headers=UA, timeout=10)
        if is_sports_page(resp.text):
            return True
        if item.get("keyword") != "본문매칭" or not words:
            return False
        text = re.sub(r"<script.*?</script>|<style.*?</style>", "", resp.text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return any(w in text for w in words)
    except Exception:
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
                # when:1h — RSS는 쿼리당 최대 100건이라 하루치(1d)로는 광범위 키워드에서
                # 기사가 밀려 누락됨. 10분마다 실행되므로 1시간 범위면 충분
                params={"q": f"{q} when:1h", "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
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
            src_el = it.find("source")
            press = strip_tags(src_el.text if src_el is not None else "")
            press_url = (src_el.get("url", "") if src_el is not None else "").lower()
            if press and title.endswith(" - " + press):
                title = title[: -len(" - " + press)].strip()
            kw = find_keyword(title, keywords) or "본문매칭"
            items.append({
                "keyword": kw,
                "source": f"구글뉴스/{press}" if press else "구글뉴스",
                "press_url": press_url,
                "title": title,
                "link": (it.findtext("link", "") or "").strip(),
            })
    return items


def fetch_yonhap(url, keywords, name="연합뉴스"):
    """언론사 자체 RSS — 구글뉴스에 등재되지 않는 기사도 잡기 위한 직접 수집원"""
    try:
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"{name} RSS 오류: {e}")
        return []
    items = []
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title", ""))
        desc = strip_tags(it.findtext("description", ""))
        kw = find_keyword(title + " " + desc, keywords)
        if kw:
            items.append({
                "keyword": kw,
                "source": name,
                "title": title,
                "desc": desc,  # 요약문 — 제외어 검사에 사용
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
    block_words = CONF.get("제외어", [])

    items = fetch_google(keywords)
    for url in CONF.get("연합뉴스RSS", []):
        items += fetch_yonhap(url, keywords)
    for feed in CONF.get("언론사RSS", []):
        items += fetch_yonhap(feed["url"], keywords, feed["이름"])
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
        if (not press_allowed(it, filt) or blocked_by_word(it, block_words)
                or blocked_by_body(it, block_words)):
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
