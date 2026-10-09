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
_SKIP_TITLE = re.compile(r"(클로징|오프닝|이 시각 헤드라인|뉴스 마칩니다|\[포토\]|\[영상\]|\[그래픽\]|오늘의 경기|내일의 경기|오늘의 운세)")
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
    t = re.sub(r"한[·ㆍ](미|중|일|러)", lambda m: "한국과 " + {"미":"미국","중":"중국","일":"일본","러":"러시아"}[m.group(1)], t)
    t = re.sub(r"한[·ㆍ](?=[가-힣]{2})", "한국과 ", t)
    for a, b in (("UNIPOD", "유니팟"), ("UNIMOA", "유니모아"), ("UNIQUBE", "유니큐브"), ("SSD", "에스에스디"), ("GPU", "지피유"), ("AX", "에이엑스")):
        t = t.replace(a, b)
    t = re.sub(r"유니팟(와|는|를|가)", lambda m: "유니팟" + {"와": "과", "는": "은", "를": "을", "가": "이"}[m.group(1)], t)
    t = t.replace("S&P500", "에스앤피 500").replace("S&P", "에스앤피")
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
    if not title:
        return ""
    extra = short_desc(it["title"], it["desc"])
    head = title if title[-1] in "?!" else title + "."   # '얼마?.' 처럼 겹치지 않게
    return f"{head} {extra}".strip()


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
                if not text.strip(" ."):
                    continue
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
VOICES = {
    "선희": "ko-KR-SunHiNeural", "sunhi": "ko-KR-SunHiNeural", "여성": "ko-KR-SunHiNeural",
    "인준": "ko-KR-InJoonNeural", "injoon": "ko-KR-InJoonNeural", "남성": "ko-KR-InJoonNeural",
    "현수": "ko-KR-HyunsuMultilingualNeural", "hyunsu": "ko-KR-HyunsuMultilingualNeural",
}
SAMPLES = [  # (이름, 음성, 속도, 음높이, 음량)
    ("선희 기본", "ko-KR-SunHiNeural", "+15%", "+0Hz", "+0%"),
    ("선희 활기", "ko-KR-SunHiNeural", "+15%", "+6Hz", "+8%"),
    ("인준 활기", "ko-KR-InJoonNeural", "+15%", "+5Hz", "+8%"),
    ("현수 활기", "ko-KR-HyunsuMultilingualNeural", "+15%", "+5Hz", "+8%"),
]
TONES = {"활기": ("+15%", "+6Hz", "+8%"), "활기찬": ("+15%", "+6Hz", "+8%"), "차분": ("+0%", "+0Hz", "+0%"), "기본": ("+15%", "+0Hz", "+0%")}


WEEKDAY_VOICES = ["인준", "현수", "선희", "인준", "현수", "선희", "인준"]  # 월~일


def resolve_voice(v: str | None) -> str:
    v = (v or "").strip()
    if v.lower() in ("요일별", "요일", "weekday"):
        kst = datetime.now(timezone(timedelta(hours=9)))
        v = WEEKDAY_VOICES[kst.weekday()]
    return VOICES.get(v.lower(), VOICES.get(v, v)) or "ko-KR-SunHiNeural"


def norm_hz(v: str | None) -> str:
    m = re.search(r"-?\d+", v or "")
    return f"{int(m.group()):+d}Hz" if m else "+0Hz"


def norm_rate(v: str) -> str:
    m = re.search(r"-?\d+", v or "")
    return f"{int(m.group()):+d}%" if m else "+0%"


_HEAD = re.compile(r"^(먼저|다음은|이어서|마지막으로|이제|하나씩|그리고 오늘|첫째|둘째|셋째|첫 번째는|두 번째는|세 번째는)")


def plan_segments(text: str) -> list[tuple[str, int, float]]:
    """(문장, 속도 가감(%), 뒤 쉼(초)) 목록. 제목은 약간 느리게 읽고 길게 쉽니다."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    segs: list[tuple[str, int, float]] = []
    for i, ln in enumerate(lines):
        last = i == len(lines) - 1
        if len(ln) <= 32 and (_HEAD.match(ln) or ln.endswith(("뉴스입니다.", "소식입니다."))):
            segs.append((ln, -2, 0.6))
        elif ln.endswith("?"):
            segs.append((ln, 0, 0.5))
        elif last:
            segs.append((ln, -2, 0.3))
        elif i == 0:
            segs.append((ln, 0, 0.6))
        else:
            segs.append((ln, 0, 0.3))
    return segs


def _pitch(base: str, delta: int) -> str:
    m = re.search(r"-?\d+", base or "")
    return f"{(int(m.group()) if m else 0) + delta:+d}Hz"


def _rate(base: str, delta: int) -> str:
    try:
        v = int(base.replace("%", "").replace("+", "")) + delta
    except ValueError:
        v = delta
    return f"{v:+d}%"


async def synthesize_plain(text: str, out: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, CONFIG["voice"], rate=CONFIG["rate"], pitch=CONFIG["pitch"], volume=CONFIG["volume"]).save(str(out))


async def synthesize_multi(parts: list[tuple], out: Path) -> None:
    """성우 샘플: 같은 원고를 여러 목소리로 읽어 이어 붙입니다."""
    import subprocess

    work = out.parent / "_smp"
    work.mkdir(parents=True, exist_ok=True)
    try:
        base = (CONFIG["voice"], CONFIG["rate"], CONFIG["pitch"], CONFIG["volume"])
        files = []
        for i, (txt, voice, rt, pt, vo) in enumerate(parts):
            CONFIG["voice"], CONFIG["rate"], CONFIG["pitch"], CONFIG["volume"] = voice, rt, pt, vo
            f = work / f"{i}.mp3"
            await synthesize(txt, f)
            files.append(f)
        CONFIG["voice"], CONFIG["rate"], CONFIG["pitch"], CONFIG["volume"] = base
        sp = work / "gap.mp3"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                        "-t", "1.5", "-c:a", "libmp3lame", "-b:a", "48k", str(sp)], check=True)
        lst = work / "list.txt"
        with open(lst, "w", encoding="utf-8") as fh:
            for f in files:
                fh.write(f"file '{f.as_posix()}'\n")
                fh.write(f"file '{sp.as_posix()}'\n")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", str(out)], check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


async def synthesize(text: str, out: Path) -> None:
    """문장 단위로 나눠 읽고 사이에 쉼을 넣습니다. 실패하면 통째로 읽는 방식으로 대체합니다."""
    import subprocess
    import edge_tts

    if not shutil.which("ffmpeg"):
        return await synthesize_plain(text, out)
    segs = plan_segments(text)
    work = out.parent / "_seg"
    work.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(6)

    async def one(i: int, txt: str, delta: int) -> None:
        path = work / f"{i:04d}.mp3"
        async with sem:
            for attempt in range(3):
                try:
                    await edge_tts.Communicate(txt, CONFIG["voice"], rate=_rate(CONFIG["rate"], delta), pitch=_pitch(CONFIG["pitch"], 3 if delta < 0 else 0), volume=CONFIG["volume"]).save(str(path))
                    if path.stat().st_size > 0:
                        return
                except Exception as e:  # noqa: BLE001
                    print(f"[음성 재시도 {attempt + 1}] {i}: {e}")
                await asyncio.sleep(1.5)
            raise RuntimeError(f"segment {i} failed")

    try:
        await asyncio.gather(*(one(i, t, d) for i, (t, d, _) in enumerate(segs)))
        sil: dict[float, Path] = {}
        for pause in {p for _, _, p in segs}:
            sp = work / f"sil_{int(pause * 100)}.mp3"
            subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                 "-t", str(pause), "-c:a", "libmp3lame", "-b:a", "48k", str(sp)], check=True)
            sil[pause] = sp
        lst = work / "list.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for i, (_, _, pause) in enumerate(segs):
                f.write(f"file '{(work / f'{i:04d}.mp3').as_posix()}'\n")
                f.write(f"file '{sil[pause].as_posix()}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-ar", "24000", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", str(out)], check=True)
        print(f"[음성] {len(segs)}개 문장, 쉼 포함 합성 완료")
    except Exception as e:  # noqa: BLE001
        print(f"[문장별 합성 실패 -> 통째로 읽기] {e}")
        await synthesize_plain(text, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    CONFIG["voice"] = resolve_voice(CONFIG.get("voice"))
    CONFIG["rate"] = norm_rate(CONFIG.get("rate", "+0%"))
    CONFIG["pitch"] = norm_hz(CONFIG.get("pitch"))
    CONFIG["volume"] = norm_rate(CONFIG.get("volume", "+0%"))
    sample_parts: list[tuple] = []

    why = ""
    manual_file = ROOT / "manual" / "today.txt"
    manual = "--manual" in sys.argv or os.environ.get("MANUAL_BUILD") == "1"
    if manual and not manual_file.exists():
        print("manual/today.txt 가 없습니다.")
        set_output("false")
        return 1

    if not dry and not force and not manual:
        due, why = slot_due(now, parse_times(CONFIG.get("times")), last_episode_date(site_url))
        print(f"[시각 확인] 현재 {now:%m-%d %H:%M} / {why}")
        if not due:
            set_output("false")
            return 0
    set_output("true")

    collected: dict[str, dict[str, list[dict]]] = {}
    for sec, feeds in ({} if manual else CONFIG["sections"]).items():
        collected[sec] = {}
        for f in feeds:
            data = fetch_bytes(f["url"])
            items = recent(parse_feed(data), CONFIG["window_hours"]) if data else []
            collected[sec][f["name"]] = items
            print(f"[수집] {sec}/{f['name']}: {len(items)}건")

    if manual:
        raw = manual_file.read_text(encoding="utf-8-sig")
        body = []
        for x in raw.splitlines():
            m = re.match(r"^\s*@(성우|목소리|voice|속도|rate|톤|tone|음높이|pitch|음량|volume)\s*[:=]\s*(.+?)\s*$", x, re.I)
            if not m:
                body.append(x)
            elif m.group(1).lower() in ("성우", "목소리", "voice"):
                if m.group(2).strip() in ("샘플", "비교", "sample"):
                    sample_parts = [("", "", "", "", "")]
                else:
                    CONFIG["voice"] = resolve_voice(m.group(2))
            elif m.group(1).lower() in ("톤", "tone"):
                t = TONES.get(m.group(2).strip())
                if t:
                    CONFIG["rate"], CONFIG["pitch"], CONFIG["volume"] = t
            elif m.group(1).lower() in ("음높이", "pitch"):
                CONFIG["pitch"] = norm_hz(m.group(2))
            elif m.group(1).lower() in ("음량", "volume"):
                CONFIG["volume"] = norm_rate(m.group(2))
            else:
                CONFIG["rate"] = norm_rate(m.group(2))
        lines = [speak(x) for x in body]
        script = "\n".join(x for x in lines if x.strip(" .,"))
        if sample_parts:
            demo = "\n".join(script.splitlines()[:9])
            sample_parts = [(f"{name} 목소리입니다.\n{demo}", v, r, p, vo) for name, v, r, p, vo in SAMPLES]
            script = "\n".join(t[0] for t in sample_parts)
    else:
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
    mt = re.search(r"(\d\d):(\d\d) 방송", why)
    slot = "오전" if (int(mt.group(1)) if mt else now.hour) < 12 else "오후"
    mp3 = SITE / "episodes" / f"{eid}.mp3"
    print(f"[음성] {CONFIG['voice']} / 속도 {CONFIG['rate']}")
    if sample_parts:
        asyncio.run(synthesize_multi(sample_parts, mp3))
    else:
        asyncio.run(synthesize(script, mp3))
    size = mp3.stat().st_size
    ep = {
        "id": eid,
        "title": (f"{now:%Y-%m-%d} 성우 샘플 (선희 기본, 선희 활기, 인준 활기, 현수 활기)" if sample_parts else f"{now:%Y-%m-%d} 직접 작성 브리핑") if manual else f"{now:%Y-%m-%d} {slot} 뉴스",
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
