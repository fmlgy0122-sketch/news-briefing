"""뉴스 RSS 수집 -> 낭독 대본 -> MP3 -> 팟캐스트 피드 + 재생 웹앱 생성.

사용법:  python build.py            (실제 실행: 수집 + MP3 생성)
         python build.py --dry-run  (대본만 출력, 음성 생성 안 함)
         python build.py --force    (방송 시각과 상관없이 지금 생성)

방송 시각은 feeds.json 의 "times" (한국시간)로 정합니다.
이 스크립트는 자주(15분마다) 실행되며, 설정한 시각이 지났는데
그 시각의 방송이 아직 없을 때만 새로 만듭니다.
"""
from __future__ import annotations

import asyncio
import email.utils
import html
import json
import os
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
SRC = ROOT / "site_src"
CONFIG = json.loads((ROOT / "feeds.json").read_text(encoding="utf-8-sig"))
UA = {"User-Agent": "Mozilla/5.0 (news-audio personal use)"}
BYTES_PER_SEC = 6000  # edge-tts MP3 약 48kbps


# ---------------------------------------------------------------- 수집
def fetch_bytes(url: str, timeout: int = 20) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        print(f"[수집 실패] {url} -> {e}")
        return None


def clean(text: str | None) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_feed(data: bytes) -> list[dict]:
    items = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        print(f"[파싱 실패] {e}")
        return items
    for it in root.iter("item"):
        pub = None
        raw = (it.findtext("pubDate") or "").strip()
        if raw:
            try:
                pub = email.utils.parsedate_to_datetime(raw)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=KST)
            except (TypeError, ValueError):
                pub = None
        items.append(
            {
                "title": clean(it.findtext("title")),
                "desc": clean(it.findtext("description")),
                "pub": pub,
            }
        )
    return items


def recent(items: list[dict], hours: int) -> list[dict]:
    limit = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = [i for i in items if i["pub"] is None or i["pub"] >= limit]
    return out or items  # 전부 오래됐으면 원본 유지


# ---------------------------------------------------------------- 낭독용 정리
# 화면용 기호·표기를 음성으로 자연스럽게 읽히도록 바꿉니다.
_BYLINE = re.compile(r"^\s*\([^)]*연합뉴스\)[^=]{0,40}=\s*")  # (서울=연합뉴스) 홍길동 기자 =
_SKIP_TITLE = re.compile(r"(클로징|오프닝|이 시각 헤드라인|뉴스 마칩니다|\[포토\]|\[영상\]|\[그래픽\])")
_UNITS = [
    (r"(?<=\d)\s?(?:kg|㎏)", "킬로그램"),
    (r"(?<=\d)\s?(?:km|㎞)", "킬로미터"),
    (r"(?<=\d)\s?(?:mm|㎜)", "밀리미터"),
    (r"(?<=\d)\s?(?:cm|㎝)", "센티미터"),
    (r"(?<=\d)\s?(?:㎡|m2)", "제곱미터"),
    (r"(?<=\d)\s?℃", "도"),
]


def _paren(m: re.Match) -> str:
    inner = m.group(1).strip()
    # 수치 설명((2.3%) 등)은 살리고, 나이·영문 병기·사진설명 등은 뺍니다.
    if re.search(r"\d", inner) and re.search(r"[%퍼억만조원달러]", inner):
        return f", {inner}, "
    before = m.string[m.start() - 1] if m.start() > 0 else " "
    after = m.string[m.end()] if m.end() < len(m.string) else " "
    # 손흥민(34·LAFC)이 -> 손흥민이  (조사 앞에 공백이 생기지 않게)
    if re.match(r"[가-힣0-9A-Za-z]", before) and re.match(r"[가-힣]", after):
        return ""
    return " "


def _clock(m: re.Match) -> str:
    h, mi = int(m.group(1)), m.group(2)
    return f"{h}시" if mi == "00" else f"{h}시 {int(mi)}분"


def speak(t: str | None) -> str:
    t = html.unescape(t or "")
    t = _BYLINE.sub("", t)
    t = t.replace("폐쇄회로(CC)TV", "CCTV")
    t = re.sub(r"\((?:종합\d*보?|\d보|상보|사진|영상|포토|그래픽|르포)\)", "", t)
    t = re.sub(r"\[(속보|단독|긴급)\]\s*", r"\1. ", t)
    t = re.sub(r"\[\d{4,6}\]", "", t)                      # 종목코드
    t = re.sub(r"\[[^\]]{0,20}\]\s*", "", t)               # [D리포트] 같은 머리표
    t = re.sub(r"\(([^()]{0,40})\)", _paren, t)            # 괄호
    t = re.sub(r"[▲△▼▽■□◆◇●○◎▶▷◀◁※☞★☆☎]", " ", t)       # 장식 기호
    t = t.replace("…", ", ").replace("...", ", ")
    t = re.sub(r"[\"'“”‘’「」『』《》〈〉«»]", "", t)       # 따옴표
    t = re.sub(r"(\d)\s*[∼~～]\s*(\d)", r"\1에서 \2", t)    # 2~3년 -> 2에서 3년
    t = re.sub(r"[∼~～]", " ", t)
    t = re.sub(r"\b(\d{1,2}):(\d{2})\b", _clock, t)        # 18:00 -> 18시
    t = re.sub(r"\$\s?(\d[\d,.]*)", r"\1달러", t)
    t = re.sub(r"€\s?(\d[\d,.]*)", r"\1유로", t)
    t = t.replace("%p", "퍼센트포인트").replace("%P", "퍼센트포인트").replace("%", "퍼센트")
    for pat, rep in _UNITS:
        t = re.sub(pat, rep, t)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)          # 1,234 -> 1234
    t = re.sub(                                            # 3천162억 -> 3162억
        r"(\d+)천\s?(\d{1,3})(?=\s?(?:억|만|조|원|달러))",
        lambda m: str(int(m.group(1)) * 1000 + int(m.group(2))),
        t,
    )
    t = re.sub(r"(?<=[가-힣]{2})[·ㆍ](?=[가-힣]{2})", ", ", t)  # 유가·국채금리 -> 유가, 국채금리
    t = re.sub(r"[·ㆍ]", " ", t)                                # 한·미 -> 한 미
    t = re.sub(r"\s+[-–—―]\s+", ", ", t)
    t = re.sub(r"[―—–]", ", ", t)
    t = t.replace("=", " ").replace("/", " ").replace("&", " 앤 ")
    t = re.sub(r"\s*,(?:\s*,)+", ",", t)
    t = re.sub(r"\s+([,.])", r"\1", t)
    t = re.sub(r"^[\s,.=]+", "", t)
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- 대본
def short_desc(title: str, desc: str, max_len: int = 110) -> str:
    """요약은 '완결된 첫 문장'만 씁니다. 잘린 문장이나 사진설명(▲)은 버립니다."""
    if not desc or "▲" in desc:
        return ""
    d, t = speak(desc), speak(title)
    m = re.match(r"(.+?[다요]\.)(?:\s|$)", d)
    if not m:
        return ""
    first = m.group(1)
    if len(first) > max_len or first.startswith(t[:15]) or t.startswith(first[:15]):
        return ""
    return first


def item_text(it: dict) -> str:
    title = speak(it["title"]).rstrip(" .,")
    extra = short_desc(it["title"], it["desc"])
    return f"{title}. {extra}".strip()


def norm(t: str) -> str:
    t = re.sub(r"^\s*\[[^\]]*\]\s*", "", t)  # [속보] [단독] 같은 머리표는 중복 판단에서 제외
    return re.sub(r"[^0-9A-Za-z가-힣]", "", t)[:18]


def build_script(collected: dict[str, dict[str, list[dict]]], now: datetime) -> str:
    wd = "월화수목금토일"[now.weekday()]
    slot = "오전" if now.hour < 12 else "오후"
    parts = [f"{now.month}월 {now.day}일 {wd}요일 {slot} 뉴스 브리핑입니다."]
    seen: set[str] = set()
    sections = [(s, {k: v for k, v in srcs.items() if v}) for s, srcs in collected.items()]
    sections = [(s, srcs) for s, srcs in sections if srcs]
    if not sections:
        return ""
    sec_budget = CONFIG["target_chars"] // len(sections)
    for sec, srcs in sections:
        sec_start = len(parts)
        parts.append(f"{sec} 뉴스입니다.")
        src_budget = sec_budget // len(srcs)
        for name, items in srcs.items():
            lines: list[str] = []
            used = 0
            for it in items:
                key = norm(it["title"])
                if not it["title"] or _SKIP_TITLE.search(it["title"]) or key in seen:
                    continue
                text = item_text(it)
                if used + len(text) > src_budget and used > 0:
                    break
                seen.add(key)
                lines.append(text)
                used += len(text)
            if lines:  # 새 기사가 없으면 언론사 안내도 생략
                parts.append(f"{name} 소식입니다.")
                parts.extend(lines)
        if len(parts) == sec_start + 1:  # 이 분야에 읽을 기사가 없으면 분야 안내도 생략
            parts.pop()
    parts.append("이상 뉴스 브리핑이었습니다. 좋은 하루 되세요.")
    return "\n".join(parts)


# ---------------------------------------------------------------- 음성
async def synthesize(text: str, out: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, CONFIG["voice"], rate=CONFIG["rate"]).save(str(out))


# ---------------------------------------------------------------- 사이트
def load_previous(site_url: str) -> list[dict]:
    if not site_url:
        return []
    raw = fetch_bytes(f"{site_url}/episodes.json", timeout=15)
    if not raw:
        return []
    try:
        eps = json.loads(raw.decode("utf-8"))
    except ValueError:
        return []
    kept = []
    for ep in eps[: CONFIG["keep_episodes"] - 1]:
        dest = SITE / ep["file"]
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = fetch_bytes(f"{site_url}/{ep['file']}", timeout=60)
        if data:
            dest.write_bytes(data)
            kept.append(ep)
    return kept


def write_feed(episodes: list[dict], site_url: str) -> None:
    esc = html.escape
    items = []
    for ep in episodes:
        pub = email.utils.format_datetime(datetime.fromisoformat(ep["date"]))
        items.append(
            f"""<item><title>{esc(ep['title'])}</title>
<description>{esc(ep['summary'])}</description>
<guid isPermaLink="false">{esc(ep['id'])}</guid><pubDate>{pub}</pubDate>
<enclosure url="{site_url}/{ep['file']}" length="{ep['size']}" type="audio/mpeg"/>
<itunes:duration>{ep['duration']}</itunes:duration></item>"""
        )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"><channel>
<title>{esc(CONFIG['title'])}</title><link>{site_url}/</link><language>ko</language>
<description>RSS 헤드라인 기반 개인용 뉴스 낭독</description>
<itunes:author>개인용</itunes:author><itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
</channel></rss>
"""
    (SITE / "feed.xml").write_text(xml, encoding="utf-8")


# ---------------------------------------------------------------- 방송 시각
DEFAULT_TIMES = ["06:30", "17:30"]


def parse_times(raw: list[str] | None) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for t in raw or DEFAULT_TIMES:
        try:
            h, m = str(t).strip().split(":")
            h, m = int(h), int(m)
            if 0 <= h < 24 and 0 <= m < 60:
                out.append((h, m))
        except ValueError:
            print(f"[시각 오류] '{t}' 는 무시합니다 (예: 06:30)")
    return out or [tuple(map(int, t.split(":"))) for t in DEFAULT_TIMES]


def slot_due(now: datetime, times: list[tuple[int, int]], last_iso: str | None) -> tuple[bool, str]:
    """가장 최근에 지난 방송 시각의 방송이 아직 없으면 True."""
    slots = []
    for off in (0, -1):
        d = (now + timedelta(days=off)).date()
        for h, m in times:
            slots.append(datetime(d.year, d.month, d.day, h, m, tzinfo=KST))
    latest = max(s for s in slots if s <= now)
    label = f"{latest:%m-%d %H:%M}"
    if last_iso:
        try:
            if datetime.fromisoformat(last_iso) >= latest:
                return False, f"{label} 방송은 이미 있음"
        except ValueError:
            pass
    return True, f"{label} 방송 생성 필요"


def last_episode_date(site_url: str) -> str | None:
    if not site_url:
        return None
    raw = fetch_bytes(f"{site_url}/episodes.json", timeout=15)
    if not raw:
        return None
    try:
        eps = json.loads(raw.decode("utf-8"))
        return eps[0]["date"] if eps else None
    except (ValueError, KeyError, IndexError, TypeError):
        return None


def set_output(value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"build={value}\n")


def main() -> int:
    dry = "--dry-run" in sys.argv
    force = "--force" in sys.argv or os.environ.get("FORCE_BUILD") == "1"
    now = datetime.now(KST)
    site_url = os.environ.get("SITE_URL", "").rstrip("/")

    if not dry and not force:
        due, why = slot_due(now, parse_times(CONFIG.get("times")), last_episode_date(site_url))
        print(f"[시각 확인] 현재 {now:%m-%d %H:%M} / {why}")
        if not due:
            set_output("false")
            return 0
    set_output("true")

    collected: dict[str, dict[str, list[dict]]] = {}
    for sec, feeds in CONFIG["sections"].items():
        collected[sec] = {}
        for f in feeds:
            data = fetch_bytes(f["url"])
            items = recent(parse_feed(data), CONFIG["window_hours"]) if data else []
            collected[sec][f["name"]] = items
            print(f"[수집] {sec}/{f['name']}: {len(items)}건")

    script = build_script(collected, now)
    if not script:
        print("수집된 뉴스가 없어 종료합니다.")
        return 1
    print(f"대본 {len(script)}자 (약 {len(script) / 380:.1f}분)")
    if dry:
        print(script)
        return 0

    if SITE.exists():
        shutil.rmtree(SITE)
    (SITE / "episodes").mkdir(parents=True)
    previous = load_previous(site_url)

    eid = now.strftime("%Y%m%d-%H%M")
    slot = "오전" if now.hour < 12 else "오후"
    mp3 = SITE / "episodes" / f"{eid}.mp3"
    asyncio.run(synthesize(script, mp3))
    size = mp3.stat().st_size
    ep = {
        "id": eid,
        "title": f"{now:%Y-%m-%d} {slot} 뉴스",
        "date": now.isoformat(),
        "file": f"episodes/{eid}.mp3",
        "size": size,
        "duration": int(size / BYTES_PER_SEC),
        "summary": script[:200],
    }
    episodes = [ep] + previous
    shutil.copy(mp3, SITE / "latest.mp3")
    (SITE / "episodes.json").write_text(
        json.dumps(episodes, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    write_feed(episodes, site_url or ".")
    for f in SRC.iterdir():
        shutil.copy(f, SITE / f.name)
    (SITE / "script.txt").write_text(script, encoding="utf-8")
    print(f"완료: {ep['title']} / {ep['duration'] // 60}분 {ep['duration'] % 60}초")
    return 0


if __name__ == "__main__":
    sys.exit(main())
