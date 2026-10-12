"""edge-tts 가 거절당할 때(403) 통과하는 Edge 버전을 찾아 EDGE_VER 로 알려줍니다."""
import asyncio, os, sys
import edge_tts
from edge_tts import constants as C, communicate as M

def patch(major, full):
    C.CHROMIUM_FULL_VERSION = full
    C.CHROMIUM_MAJOR_VERSION = str(major)
    C.SEC_MS_GEC_VERSION = f"1-{full}"
    M.SEC_MS_GEC_VERSION = C.SEC_MS_GEC_VERSION
    ua = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          f"Chrome/{major}.0.0.0 Safari/537.36 Edg/{major}.0.0.0")
    for h in (C.WSS_HEADERS, C.BASE_HEADERS):
        h["User-Agent"] = ua

async def ok():
    try:
        await edge_tts.Communicate("시험", "ko-KR-SunHiNeural").save("/tmp/_probe.mp3")
        return os.path.getsize("/tmp/_probe.mp3") > 0
    except Exception as e:
        print("  실패:", str(e)[:80])
        return False

async def main():
    if await ok():
        print("기본 버전 통과"); return
    for major in range(160, 142, -1):
        full = f"{major}.0.0.0"
        patch(major, full)
        print("시험", full)
        if await ok():
            print("통과", full)
            open(os.environ.get("GITHUB_ENV", "/dev/null"), "a").write(f"EDGE_VER={major}\n")
            return
    print("통과하는 버전을 찾지 못했습니다")

asyncio.run(main())
