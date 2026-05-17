"""End-to-end Instagram Reel generator.

Pipeline:
    1. Claude (Opus 4.7) writes hook + slide text + caption + music mood
    2. Pillow renders each slide as a 1080x1920 card
    3. Pixabay Music API fetches a royalty-free track matching the mood
    4. ffmpeg composes slides + BGM into an MP4 reel
    5. (Optional) reels_upload.py publishes to Instagram

Usage:
    python make_reel.py --topic "Python의 GIL이 뭐길래?"
    python make_reel.py --topic "쿼리 최적화 3가지" --handle @yourhandle
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

import anthropic
import requests
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field


# ─── Constants ────────────────────────────────────────────────────────────────
WIDTH, HEIGHT = 1080, 1920
SLIDE_SECONDS = 3.8
CROSSFADE_SECONDS = 0.5
MARGIN = 100

ROOT = Path(__file__).parent
FONT_DIR = ROOT / "assets" / "fonts"
MUSIC_DIR = ROOT / "assets" / "music"

PIXABAY_MUSIC_API = "https://pixabay.com/api/music/"

MusicMood = Literal[
    "chill", "upbeat", "dramatic", "corporate",
    "lofi", "tech", "inspiring", "cinematic",
]


# ─── 1. Content generation via Claude ─────────────────────────────────────────
class Slide(BaseModel):
    title: str = Field(description="크고 굵게 들어갈 제목. 핵심 단어/구. 2~14자 권장.")
    body: str = Field(description="본문 설명. 1~3줄. 짧고 명확한 한국어 문장.")


class ReelContent(BaseModel):
    hook_title: str = Field(description="첫 슬라이드의 후킹 카피. 질문이나 도발적 주장. 5~20자.")
    hook_body: str = Field(description="후킹을 보완하는 짧은 한 줄. 비어있어도 됨.")
    slides: list[Slide] = Field(description="본문 슬라이드. 정확히 3장.", min_length=3, max_length=3)
    summary_title: str = Field(description="요약 슬라이드 제목. '한 줄 요약' 등.")
    summary_body: str = Field(description="요약 슬라이드 본문 한 줄.")
    caption: str = Field(description="인스타 캡션. 자연스러운 한국어. 줄바꿈 OK. 해시태그 제외.")
    hashtags: list[str] = Field(
        description="해시태그 8~12개. '#' 포함. 한국어/영어 혼용 가능.",
        min_length=8, max_length=12,
    )
    music_mood: MusicMood = Field(description="주제 분위기에 맞는 BGM 무드 키워드.")


SYSTEM_PROMPT = """당신은 한국어 IT/기술 인스타그램 릴스 카드 콘텐츠를 만드는 전문 카피라이터입니다.

주어진 주제로 정확히 5장 구성의 9:16 텍스트 카드 시리즈를 작성합니다:
  1. 후킹(hook) — 호기심 자극하는 질문/주장 한 줄
  2~4. 본문 3장 — 각 카드는 1개 핵심 메시지
  5. 요약(summary) — 한 줄로 마무리

작성 원칙:
- 한국어 IT 종사자/입문자가 3초 안에 이해 가능한 문장
- 슬라이드당 한 호흡 분량 (긴 설명 금지)
- 후킹은 "~알고 계셨나요?", "왜 ~일까?", "사실 ~다" 같은 패턴 활용
- 본문은 사실/근거/예시 중심. 추상적 미사여구 X
- 요약은 행동 유도 또는 핵심 정리
- 캡션은 본문보다 조금 더 길게, 친근한 톤
- 해시태그는 #개발자, #코딩, #IT 같은 광범위한 것 + 주제별 구체적 태그 섞기

music_mood 선택 가이드:
- tech: 기술/개념 설명
- corporate: 비즈니스/생산성
- inspiring: 동기부여/커리어
- upbeat: 가벼운 팁/트렌드
- chill: 라이프스타일/회고
- dramatic: 충격적/반전 사실
- lofi: 학습/공부 분위기
- cinematic: 스토리텔링
"""


def generate_content(topic: str) -> ReelContent:
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-4-7",
        max_tokens=16000,
        thinking={"type": "adaptive"},
        cache_control={"type": "ephemeral"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"주제: {topic}"}],
        output_format=ReelContent,
    )
    return response.parsed_output


# ─── 2. Slide rendering with Pillow ───────────────────────────────────────────
def _font(size: int, weight: str = "regular") -> ImageFont.FreeTypeFont:
    files = {
        "regular": "Pretendard-Regular.otf",
        "bold": "Pretendard-Bold.otf",
        "black": "Pretendard-Black.otf",
    }
    path = FONT_DIR / files[weight]
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _gradient_bg(top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (WIDTH, HEIGHT), top)
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):
        t = y / HEIGHT
        c = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        draw.line([(0, y), (WIDTH, y)], fill=c)
    return img


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_w: int, draw: ImageDraw.Draw) -> list[str]:
    """Wrap Korean/English text to fit max_w pixels. Falls back to char-wrap."""
    out: list[str] = []
    for raw in text.split("\n"):
        line = ""
        for token in raw.split(" "):
            candidate = f"{line} {token}".strip()
            if draw.textlength(candidate, font=font) <= max_w:
                line = candidate
                continue
            if line:
                out.append(line)
            # token alone too long → char-wrap
            if draw.textlength(token, font=font) > max_w:
                cur = ""
                for ch in token:
                    if draw.textlength(cur + ch, font=font) <= max_w:
                        cur += ch
                    else:
                        out.append(cur)
                        cur = ch
                line = cur
            else:
                line = token
        if line:
            out.append(line)
    return out


def _draw_text_block(
    draw: ImageDraw.Draw, lines: list[str], font: ImageFont.FreeTypeFont,
    x: int, y: int, color: tuple[int, int, int], line_gap: int,
) -> int:
    for line in lines:
        draw.text((x, y), line, font=font, fill=color)
        bbox = draw.textbbox((x, y), line, font=font)
        y = bbox[3] + line_gap
    return y


def render_slide(
    title: str, body: str, index: int, total: int, out_path: Path,
    handle: str, accent: tuple[int, int, int] = (120, 180, 255),
    is_hook: bool = False, is_summary: bool = False,
) -> None:
    if is_hook:
        bg_top, bg_bottom = (35, 20, 60), (10, 8, 28)
    elif is_summary:
        bg_top, bg_bottom = (40, 25, 35), (12, 10, 18)
    else:
        bg_top, bg_bottom = (20, 30, 55), (8, 12, 28)

    img = _gradient_bg(bg_top, bg_bottom)
    draw = ImageDraw.Draw(img)

    # Top: slide indicator
    indicator_font = _font(38, "regular")
    draw.text((MARGIN, 90), f"{index:02d} / {total:02d}", font=indicator_font, fill=(160, 170, 210))

    # Accent bar under indicator
    draw.rectangle([(MARGIN, 165), (MARGIN + 90, 173)], fill=accent)

    # Title
    title_size = 140 if is_hook else 110
    title_font = _font(title_size, "black" if is_hook else "bold")
    title_lines = _wrap(title, title_font, WIDTH - 2 * MARGIN, draw)
    title_y = 380 if is_hook else 340
    title_end_y = _draw_text_block(
        draw, title_lines, title_font, MARGIN, title_y, (255, 255, 255), line_gap=14,
    )

    # Body
    if body.strip():
        body_font = _font(58, "regular")
        body_lines = _wrap(body, body_font, WIDTH - 2 * MARGIN, draw)
        body_y = title_end_y + 80
        _draw_text_block(
            draw, body_lines, body_font, MARGIN, body_y, (215, 220, 240), line_gap=22,
        )

    # Bottom handle
    handle_font = _font(34, "regular")
    handle_w = draw.textlength(handle, font=handle_font)
    draw.text(
        (WIDTH - MARGIN - handle_w, HEIGHT - 90),
        handle, font=handle_font, fill=(140, 150, 200),
    )

    img.save(out_path, "PNG")


# ─── 3. Music fetching via Pixabay (with local fallback) ──────────────────────
def fetch_music(mood: str, dest: Path) -> Path | None:
    api_key = os.getenv("PIXABAY_API_KEY", "").strip()
    if api_key:
        try:
            resp = requests.get(
                PIXABAY_MUSIC_API,
                params={"key": api_key, "q": mood, "per_page": 30, "safesearch": "true"},
                timeout=30,
            )
            if resp.ok:
                hits = resp.json().get("hits", [])
                if hits:
                    track = random.choice(hits)
                    audio_url = (
                        track.get("audio")
                        or track.get("download_url")
                        or track.get("audio_url")
                    )
                    if audio_url:
                        r = requests.get(audio_url, timeout=180)
                        r.raise_for_status()
                        dest.write_bytes(r.content)
                        print(f"  Pixabay track: {track.get('title') or track.get('user', '?')}")
                        return dest
                else:
                    print(f"  Pixabay returned 0 hits for '{mood}'", file=sys.stderr)
            else:
                print(f"  Pixabay HTTP {resp.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"  Pixabay fetch failed: {e}", file=sys.stderr)

    # Local fallback
    if MUSIC_DIR.exists():
        for candidate in [MUSIC_DIR / f"{mood}.mp3", *MUSIC_DIR.glob("*.mp3")]:
            if candidate.exists():
                shutil.copy(candidate, dest)
                print(f"  Using local track: {candidate.name}")
                return dest

    return None


# ─── 4. Video composition with ffmpeg ─────────────────────────────────────────
def _check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg not found in PATH. Install via 'brew install ffmpeg' or "
            "'sudo apt-get install -y ffmpeg'."
        )


def compose_video(slides: list[Path], music: Path | None, out_path: Path) -> None:
    _check_ffmpeg()
    n = len(slides)
    total = SLIDE_SECONDS * n

    inputs: list[str] = []
    for slide in slides:
        inputs += ["-loop", "1", "-t", f"{SLIDE_SECONDS + CROSSFADE_SECONDS}", "-i", str(slide)]

    if n == 1:
        chain = "[0:v]format=yuv420p[v]"
    else:
        parts = []
        for i in range(n - 1):
            offset = SLIDE_SECONDS * (i + 1) - CROSSFADE_SECONDS
            src = "[0:v]" if i == 0 else f"[x{i}]"
            parts.append(
                f"{src}[{i + 1}:v]xfade=transition=fade:"
                f"duration={CROSSFADE_SECONDS}:offset={offset}[x{i + 1}]"
            )
        parts.append(f"[x{n - 1}]format=yuv420p[v]")
        chain = ";".join(parts)

    cmd = ["ffmpeg", "-y", *inputs]

    if music:
        cmd += ["-i", str(music)]
        fade_start = max(total - 1.0, 0.1)
        chain += (
            f";[{n}:a]volume=0.55,afade=t=in:st=0:d=0.5,"
            f"afade=t=out:st={fade_start}:d=1.0[a]"
        )
        map_args = ["-map", "[v]", "-map", "[a]", "-shortest"]
    else:
        map_args = ["-map", "[v]"]

    cmd += [
        "-filter_complex", chain,
        *map_args,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
        "-t", f"{total}",
        "-movflags", "+faststart",
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")


# ─── 5. CLI ──────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate an Instagram Reel from a topic.")
    parser.add_argument("--topic", required=True, help="릴스 주제 (한국어)")
    parser.add_argument("--handle", default="@your.handle", help="하단 워터마크")
    parser.add_argument("--output", default="reel.mp4", help="출력 mp4 경로")
    parser.add_argument("--keep-workdir", action="store_true", help="중간 파일 보존 (디버깅용)")
    args = parser.parse_args(argv)

    load_dotenv()

    print(f"[1/4] Generating content via Claude — topic: {args.topic!r}")
    content = generate_content(args.topic)
    print(f"      Hook   : {content.hook_title}")
    print(f"      Slides : {len(content.slides)}")
    print(f"      Mood   : {content.music_mood}")

    workdir = Path(tempfile.mkdtemp(prefix="reel_"))
    print(f"[2/4] Rendering slides → {workdir}")

    deck: list[tuple[str, str, bool, bool]] = [
        (content.hook_title, content.hook_body, True, False),
        *[(s.title, s.body, False, False) for s in content.slides],
        (content.summary_title, content.summary_body, False, True),
    ]
    total = len(deck)
    slide_paths: list[Path] = []
    for i, (title, body, is_hook, is_summary) in enumerate(deck, start=1):
        p = workdir / f"slide_{i:02d}.png"
        render_slide(
            title, body, i, total, p, handle=args.handle,
            is_hook=is_hook, is_summary=is_summary,
        )
        slide_paths.append(p)
        print(f"      [{i}/{total}] {title[:30]}")

    print(f"[3/4] Fetching BGM (mood={content.music_mood})")
    music = fetch_music(content.music_mood, workdir / "bgm.mp3")
    if not music:
        print("      No music available — reel will be silent")

    print(f"[4/4] Composing video → {args.output}")
    out_path = Path(args.output)
    try:
        compose_video(slide_paths, music, out_path)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    caption_text = content.caption.rstrip() + "\n\n" + " ".join(content.hashtags)
    caption_path = out_path.with_suffix(".caption.txt")
    caption_path.write_text(caption_text, encoding="utf-8")

    print()
    print(f"Done.")
    print(f"  Video   : {out_path}")
    print(f"  Caption : {caption_path}")
    print()
    print("다음 단계 (수동):")
    print("  1) 영상을 공개 URL(S3, R2, Cloudinary 등)에 업로드")
    print("  2) python reels_upload.py --video-url <URL> \\")
    print(f"       --caption \"$(cat {caption_path})\"")

    if not args.keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"\n  Workdir kept at: {workdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
