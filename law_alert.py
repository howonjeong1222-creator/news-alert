# -*- coding: utf-8 -*-
"""
국회 입법현황 알림 — 정무위원회 법안 발의·진행 텔레그램 알림

국민참여입법센터(opinion.lawmaking.go.kr)의 '국회입법현황'에서
상임위가 정무위원회인 법안의 최근 추진 내역을 가져와,
  1) 새 법안 발의          → 🆕 알림
  2) 진행 단계 변경(회부·상정·소위·의결 등) → 📈 알림
을 텔레그램으로 보낸다.

뉴스알림(cloud_alert.py)과 같은 방식:
  - GitHub Actions가 주기적으로 1번 실행하고 종료
  - 텔레그램 토큰·chat_id는 GitHub Secrets(환경변수)에서 읽음
  - 이미 알린 상태(seen_bills.json)는 실행 후 저장소에 커밋되어 유지

로컬 테스트: python law_alert.py --test   (텔레그램 없이 화면 출력만, 상태 저장 안 함)
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
CONF = json.loads((BASE / "config_law.json").read_text(encoding="utf-8"))
SEEN_PATH = BASE / "seen_bills.json"

LIST_URL = "https://opinion.lawmaking.go.kr/gcom/nsmLmSts/out"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LawAlert/1.0"}
KST = timezone(timedelta(hours=9))

# 입법알림 전용 봇 (뉴스알림 봇과 별개 — Secrets 이름이 다름)
TG_TOKEN = os.environ.get("LAW_TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("LAW_TELEGRAM_CHAT_ID", "")
TEST_MODE = "--test" in sys.argv


# ---------------------------------------------------------------- 수집·파싱
def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def parse_rows(html):
    """목록 HTML에서 법안 행들을 뽑는다. [{의안번호, 의안명, 제안자, 제안일, 소관부처, 현황, 추진일, 의결, 링크}]"""
    rows = []
    # 한 행 = <tr> ... detailRP ... </tr>
    for tr in re.findall(r"<tr>.*?</tr>", html, re.S):
        m = re.search(r'href="(/gcom/nsmLmSts/out/(\d+)/detailRP)"[^>]*>(.*?)</a>', tr, re.S)
        if not m:
            continue
        link = "https://opinion.lawmaking.go.kr" + m.group(1)
        billno = m.group(2)
        title = strip_tags(m.group(3))

        def cell(name):
            c = re.search(r'<td data-th="' + name + r'"[^>]*>(.*?)</td>', tr, re.S)
            return strip_tags(c.group(1)) if c else ""

        proposer_raw = cell(r"제안자\(제안일자\)")          # 예: "한창민의원 등 15인 (2026. 10. 22.)"
        ministry_raw = cell(r"상임위원회\(소관부처\)")      # 예: "정무 (공정거래위원회)"
        status_raw = cell(r"국회현황\(추진일자\)")          # 예: "위원회 회부 (2026. 10. 23.)"
        result_raw = cell(r"의결현황\(의결일자\)")          # 예: "원안가결 (2026. 10. 24.)" 또는 ""

        def split_date(raw):
            m2 = re.search(r"^(.*?)\s*\((\d{4}\.\s*\d{1,2}\.\s*\d{1,2}\.)\)\s*$", raw)
            if m2:
                return m2.group(1).strip(), re.sub(r"\s+", "", m2.group(2)).rstrip(".")
            return raw, ""

        proposer, proposed_date = split_date(proposer_raw)
        status, status_date = split_date(status_raw)
        result, result_date = split_date(result_raw)
        ministry = ""
        m3 = re.search(r"\((.+?)\)", ministry_raw)
        if m3:
            ministry = m3.group(1)

        rows.append({
            "의안번호": billno, "의안명": title,
            "제안자": proposer, "제안일": proposed_date,
            "소관부처": ministry,
            "현황": status, "추진일": status_date,
            "의결": result, "의결일": result_date,
            "링크": link,
        })
    return rows


def fetch_recent(committee, days):
    """최근 `days`일 사이 추진일자가 찍힌 해당 상임위 법안 전부 (페이지 순회)"""
    today = datetime.now(KST)
    params = {
        "sugCd": str(CONF.get("대수", 22)),
        "endSugCd": str(CONF.get("대수", 22)),
        "scCptPpostCmt": committee,
        "pageSize": "100",
        "stDtFmt": (today - timedelta(days=days)).strftime("%Y. %m. %d."),
        "edDtFmt": today.strftime("%Y. %m. %d."),
    }
    bills, page = [], 1
    while page <= 10:  # 안전장치: 최대 1,000건
        params["pageIndex"] = str(page)
        r = requests.get(LIST_URL, params=params, headers=UA, timeout=20)
        r.raise_for_status()
        rows = parse_rows(r.text)
        bills += rows
        m = re.search(r"전체<span>([\d,]+)</span>건", r.text)
        total = int(m.group(1).replace(",", "")) if m else len(bills)
        if len(bills) >= total or not rows:
            break
        page += 1
        time.sleep(0.5)
    return bills


# ---------------------------------------------------------------- 알림
def fmt_date(d):
    """'2026.10.23' → '10/23'"""
    p = d.split(".")
    return f"{int(p[1])}/{int(p[2])}" if len(p) >= 3 else d


def build_message(bill, kind, old=None):
    committee_short = bill.get("상임위", CONF["상임위"][0]).replace("위원회", "위")
    if kind == "new":
        head = f"🆕 [{committee_short}] 새 법안 발의"
    else:
        old_status = (old or {}).get("현황") or "?"
        head = f"📈 [{committee_short}] {old_status} → {bill['현황']}"
    lines = [head, bill["의안명"]]
    sub = bill["제안자"]
    if bill["제안일"]:
        sub += f" ({fmt_date(bill['제안일'])} 발의)"
    if bill["소관부처"]:
        sub += f" · {bill['소관부처']}"
    lines.append(sub)
    stat = f"현황: {bill['현황']}"
    if bill["추진일"]:
        stat += f" ({fmt_date(bill['추진일'])})"
    if bill["의결"]:
        stat += f" / 의결: {bill['의결']}"
        if bill["의결일"]:
            stat += f" ({fmt_date(bill['의결일'])})"
    lines.append(stat)
    lines.append(bill["링크"])
    return "\n".join(lines)


def send_telegram(text):
    if TEST_MODE:
        print("--- [테스트 출력] ---")
        print(text)
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
        print("오류: LAW_TELEGRAM_BOT_TOKEN / LAW_TELEGRAM_CHAT_ID 환경변수(Secrets)가 없습니다")
        sys.exit(1)

    days = int(CONF.get("조회일수", 14))
    bills = []
    for committee in CONF["상임위"]:
        for b in fetch_recent(committee, days):
            b["상임위"] = committee
            bills.append(b)
    print(f"수집: 최근 {days}일 추진 법안 {len(bills)}건")

    seen = {}
    if SEEN_PATH.exists():
        seen = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    first_run = not seen

    sent = 0
    for b in bills:
        key = b["의안번호"]
        state = {"의안명": b["의안명"], "현황": b["현황"], "추진일": b["추진일"],
                 "의결": b["의결"], "의결일": b["의결일"]}
        old = seen.get(key)
        if old is None:
            kind = "new"
        elif (old.get("현황"), old.get("추진일"), old.get("의결")) != (b["현황"], b["추진일"], b["의결"]):
            kind = "change"
        else:
            continue

        if first_run:
            # 첫 실행: 현재 상태를 기준점으로만 저장, 알림 없음
            seen[key] = state
            continue

        if send_telegram(build_message(b, kind, old)):
            seen[key] = state
            sent += 1
            print(f"알림({kind}): {b['의안명']} — {b['현황']}")
            time.sleep(0.5)
        else:
            print(f"전송 실패(다음 실행 때 재시도): {b['의안명']}")

    if first_run:
        print(f"첫 실행 — 기준점 {len(seen)}건 저장 (알림 없음)")

    if not TEST_MODE or first_run:
        SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"완료 — 새 알림 {sent}건")


if __name__ == "__main__":
    main()
