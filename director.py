#!/usr/bin/env python3
"""
director.py — AI video editor.

Reads *-shots.md files from a folder, asks a local LLM to create
an edit decision list using the `cut` tool, then produces:

    {foldername}-draft.mp4
    {foldername}.fcpxml

Usage:
    python director.py ./my_movie
    python director.py ./my_movie --prompt "Dynamic adventure montage"
    python director.py ./my_movie --base_url http://127.0.0.1:8080/v1
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path


# Используем полный путь к portable ffmpeg (свежая версия)
FFMPEG_BIN = r"E:\PORTABLE\ffmpeg\bin"
FFMPEG_EXE = os.path.join(FFMPEG_BIN, "ffmpeg.exe")
FFPROBE_EXE = os.path.join(FFMPEG_BIN, "ffprobe.exe")


DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
MODEL = "any"


SYSTEM_PROMPT = """
Ты — ИИ-режиссёр монтажа.

По описаниям шотов составь цельный монтажный ролик из имеющихся
исходных MP4. Ты не создаёшь новые кадры и не меняешь содержимое
исходников: только выбираешь непрерывные интервалы и задаёшь их порядок.

Тебе доступен единственный инструмент cut. Каждый вызов добавляет один
фрагмент в фильм. Порядок вызовов cut — порядок фрагментов на таймлайне.
Можно несколько раз использовать один исходный файл.

Аргументы cut:

- filename: точное имя или путь к исходному MP4, указанному в контексте.
- start: начало фрагмента в секундах от начала исходного файла.
- stop: конец фрагмента в секундах от начала файла.

Правила:

1. Используй только материал, описанный во входных MD.
2. Не выдумывай отсутствующие кадры, действия, ракурсы и события.
3. Учитывай начальное и конечное состояние каждого шота и развитие внутри него.
4. Не считай изменение крупности, движения камеры или перефокусировку
   самостоятельной монтажной склейкой.
5. Выбирай содержательные интервалы, избегай пустых пауз, технически
   неудачных моментов и ненужных повторов.
6. Сохраняй визуальную и причинно-следственную связность.
7. Каждый cut — один непрерывный интервал одного файла.
8. start >= 0, stop > start, stop не выходит за длительность исходника.
9. Не добавляй переходы, эффекты, титры или музыку.
10. Не растягивай хронометраж искусственно.
11. Один хорошо выбранный шот — тоже полноценный монтажный результат.

Если для исходного MP4 доступна текстовая транскрипция с таймкодами
(файл {basename}-voice.json), используй её для точного выбора моментов:
выбирай фрагменты, соответствующие ключевым репликам и смысловым частям
речи. Таймкоды в транскрипции — секунды от начала файла.

Верни монтаж исключительно вызовами cut. Не заменяй их текстовым планом.
Если подходящего материала нет, не вызывай инструмент.
""".strip()


@dataclass
class MediaInfo:
    path: Path
    duration: Fraction
    fps: Fraction
    width: int
    height: int
    has_audio: bool


@dataclass
class Cut:
    path: Path
    start: Fraction
    stop: Fraction

    @property
    def duration(self) -> Fraction:
        return self.stop - self.start


def log(message=""):
    print(message, flush=True)


def as_fraction(value):
    return Fraction(str(value)).limit_denominator(1_000_000)


def fcpx_time(value):
    if value.denominator == 1:
        return f"{value.numerator}s"
    return f"{value.numerator}/{value.denominator}s"


def xml_escape(value):
    return html.escape(value, quote=True)


def run(command, description):
    log("$ " + subprocess.list2cmdline(command))

    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()

        raise RuntimeError(
            f"{description}: ошибка {exc.returncode}\n"
            f"{detail[-10000:]}"
        ) from exc


def probe(path):
    result = run(
        [
            FFPROBE_EXE,
            "-v", "error",
            "-show_entries",
            "stream=codec_type,avg_frame_rate,r_frame_rate,width,height:"
            "format=duration",
            "-of", "json",
            str(path),
        ],
        f"ffprobe {path.name}",
    )

    data = json.loads(result.stdout)
    streams = data.get("streams") or []

    video = next(
        (s for s in streams if s.get("codec_type") == "video"),
        None,
    )

    if video is None:
        raise RuntimeError(f"В файле нет видеопотока: {path}")

    duration_raw = (data.get("format") or {}).get("duration")

    if duration_raw in (None, "N/A"):
        raise RuntimeError(f"Не определена длительность: {path}")

    fps_raw = video.get("avg_frame_rate") or video.get("r_frame_rate")

    try:
        fps = Fraction(fps_raw)
    except (ValueError, TypeError, ZeroDivisionError) as exc:
        raise RuntimeError(
            f"Некорректный FPS {fps_raw!r}: {path}"
        ) from exc

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)

    if fps <= 0 or width <= 0 or height <= 0:
        raise RuntimeError(f"Некорректные параметры видео: {path}")

    return MediaInfo(
        path.resolve(),
        as_fraction(duration_raw),
        fps,
        width,
        height,
        any(s.get("codec_type") == "audio" for s in streams),
    )


def find_descriptions(folder):
    files = sorted(
        (p for p in folder.glob("*-shots.md") if p.is_file()),
        key=lambda p: p.name.casefold(),
    )

    if not files:
        raise RuntimeError(
            f"В папке не найдены файлы *-shots.md: {folder}"
        )

    return files


def paired_mp4(md):
    """
    Conventional mapping:
        clip-shots.md -> clip.mp4
    """

    stem = md.name[:-len("-shots.md")]
    direct = md.parent / f"{stem}.mp4"

    if direct.is_file():
        return direct.resolve()

    matches = [
        p.resolve()
        for p in md.parent.rglob("*.mp4")
        if p.stem.casefold() == stem.casefold()
    ]

    return matches[0] if len(matches) == 1 else None


def load_voice_json(mp4_path):
    """Загружает {basename}-voice.json если существует. Возвращает строку или None."""
    voice_path = mp4_path.with_name(f"{mp4_path.stem}-voice.json")
    if not voice_path.is_file():
        return None
    try:
        data = json.loads(voice_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    lines = []
    lines.append(f"Транскрипция ({data.get('language', '?')}, {data.get('duration', '?')}с):")
    for seg in data.get("segments", []):
        words_text = " ".join(w["text"] for w in seg.get("words", []))
        lines.append(f"  [{seg['start']:.1f}–{seg['end']:.1f}s] {words_text}")
    return "\n".join(lines)


def read_context(files):
    blocks = []

    for n, md in enumerate(files, 1):
        content = md.read_text(encoding="utf-8-sig").strip()

        if not content:
            log(f"  [пропуск пустого файла] {md.name}")
            continue

        video = paired_mp4(md)

        source = (
            f"Связанный исходный MP4: {video}"
            if video
            else "Связанный MP4 по имени автоматически не найден."
        )

        voice_text = None
        if video:
            voice_text = load_voice_json(video)
            if voice_text:
                log(f"  Транскрипция: {video.name}")

        block = (
            f"========== МАТЕРИАЛ {n} ==========\n"
            f"Файл описания: {md.resolve()}\n"
            f"{source}"
        )

        if voice_text:
            block += f"\n\n{voice_text}"

        block += f"\n\n{content}"
        blocks.append(block)

    if not blocks:
        raise RuntimeError("Все файлы *-shots.md пустые.")

    return "\n\n".join(blocks)


def tool_schema():
    return [
        {
            "type": "function",
            "function": {
                "name": "cut",
                "description": (
                    "Добавить один непрерывный фрагмент исходного MP4 "
                    "в фильм. Порядок вызовов определяет порядок "
                    "на таймлайне."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filename": {
                            "type": "string",
                            "description": "Точное имя/путь MP4 из контекста.",
                        },
                        "start": {
                            "type": "number",
                            "description": "Начало фрагмента в секундах.",
                        },
                        "stop": {
                            "type": "number",
                            "description": "Конец фрагмента в секундах.",
                        },
                    },
                    "required": ["filename", "start", "stop"],
                    "additionalProperties": False,
                },
            },
        }
    ]


def ask_llm(base_url, context, extra_prompt, api_key=None, model=None):
    system = SYSTEM_PROMPT

    if extra_prompt and extra_prompt.strip():
        system += (
            "\n\nДополнительное режиссёрское пожелание пользователя:\n"
            + extra_prompt.strip()
        )

    payload = {
        "model": model or MODEL,
        "messages": [
            {
                "role": "system",
                "content": system,
            },
            {
                "role": "user",
                "content": (
                    "Смонтируй фильм из описанного ниже материала. "
                    "Вызывай cut для каждого выбранного фрагмента; "
                    "порядок вызовов важен.\n\n"
                    + context
                ),
            },
        ],
        "tools": tool_schema(),
        "tool_choice": "required",
        "parallel_tool_calls": True,
        "temperature": 0.2,

        # Намеренно не задаём max_tokens:
        # используем ограничение модели/сервера.
    }

    url = base_url.rstrip("/") + "/chat/completions"

    headers = {"Content-Type": "application/json"}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    log(f"Запрос к LLM: {url}")
    log(f"Размер контекста: {len(context):,} символов")
    log("max_tokens/max_completion_tokens не задаются.")

    started = time.perf_counter()

    try:
        with urllib.request.urlopen(request, timeout=3600) as response:
            raw = response.read()

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")

        raise RuntimeError(
            f"LLM HTTP {exc.code}: {body[:10000]}"
        ) from exc

    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Не удалось обратиться к LLM: {exc}"
        ) from exc

    elapsed = time.perf_counter() - started
    result = json.loads(raw)

    choices = result.get("choices") or []

    if not choices:
        raise RuntimeError(f"В ответе LLM нет choices: {result}")

    message = choices[0].get("message") or {}
    calls = message.get("tool_calls") or []

    if not calls:
        raise RuntimeError(
            "LLM не вернула вызовы cut. "
            f"finish_reason={choices[0].get('finish_reason')!r}; "
            f"content={message.get('content')!r}"
        )

    return calls, result.get("usage") or {}, elapsed, result, payload


def resolve_mp4(filename, folder):
    raw = filename.strip().strip('"').strip("'")

    if not raw:
        raise RuntimeError("Пустое filename в cut.")

    candidate = Path(raw).expanduser()

    if candidate.is_absolute() and candidate.is_file():
        return candidate.resolve()

    relative = (folder / candidate).resolve()

    if relative.is_file():
        return relative

    matches = [
        p.resolve()
        for p in folder.rglob(candidate.name)
        if p.is_file() and p.suffix.casefold() == ".mp4"
    ]

    if len(matches) == 1:
        return matches[0]

    if len(matches) > 1:
        raise RuntimeError(
            f"Неоднозначное имя {filename!r}: "
            + ", ".join(map(str, matches))
        )

    raise RuntimeError(
        f"Исходный MP4 не найден: {filename!r} "
        f"(папка {folder})"
    )


def parse_cuts(calls, folder):
    media = {}
    cuts = []

    for i, call in enumerate(calls, 1):
        fn = call.get("function") or {}

        if fn.get("name") != "cut":
            raise RuntimeError(
                f"Неожиданный tool call #{i}: {fn.get('name')!r}"
            )

        raw = fn.get("arguments", {})

        args = json.loads(raw) if isinstance(raw, str) else raw

        try:
            path = resolve_mp4(str(args["filename"]), folder)
            start = as_fraction(args["start"])
            stop = as_fraction(args["stop"])

        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Некорректные аргументы cut #{i}: {raw!r}"
            ) from exc

        if path not in media:
            log(f"ffprobe: {path}")
            media[path] = probe(path)

        duration = media[path].duration

        if start < 0:
            log(f"  [коррекция] CUT #{i}: {path.name} start {float(start):.3f} → 0")
            start = Fraction(0)

        if stop <= start:
            raise RuntimeError(
                f"Некорректный cut #{i}: "
                f"{path.name} stop {float(stop):.3f} ≤ start {float(start):.3f}"
            )

        if stop > duration:
            log(f"  [коррекция] CUT #{i}: {path.name} stop {float(stop):.3f} → {float(duration):.3f} (длительность файла)")
            stop = duration

        cuts.append(Cut(path, start, stop))

        log(
            f"  CUT {i:02d}: {path.name} "
            f"{float(start):.3f}–{float(stop):.3f}s "
            f"({float(stop-start):.3f}s)"
        )

    if not cuts:
        raise RuntimeError("LLM не выбрала ни одного фрагмента.")

    return cuts, media


def render_draft(cuts, media, output, temp):
    first = media[cuts[0].path]

    width = first.width - first.width % 2
    height = first.height - first.height % 2
    fps = first.fps

    segments = []

    log("\nРендер выбранных фрагментов:")

    for i, cut in enumerate(cuts, 1):
        info = media[cut.path]
        segment = temp / f"segment-{i:04d}.mp4"
        dur = float(cut.duration)

        cmd = [
            FFMPEG_EXE,
            "-hide_banner", "-loglevel", "warning", "-y",
            "-ss", f"{float(cut.start):.9f}",
            "-i", str(cut.path),
        ]

        if info.has_audio:
            audio = (
                "[0:a:0]asetpts=PTS-STARTPTS,"
                "aformat=sample_rates=48000:"
                "channel_layouts=stereo[a]"
            )
        else:
            cmd += [
                "-f", "lavfi",
                "-t", f"{dur:.9f}",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]

            audio = (
                "[1:a:0]asetpts=PTS-STARTPTS,"
                "aformat=sample_rates=48000:"
                "channel_layouts=stereo[a]"
            )

        video = (
            f"[0:v:0]trim=duration={dur:.9f},"
            "setpts=PTS-STARTPTS,"
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={fps.numerator}/{fps.denominator},"
            "setsar=1,format=yuv420p[v]"
        )

        cmd += [
            "-t", f"{dur:.9f}",
            "-filter_complex", video + ";" + audio,
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",
            "-movflags", "+faststart",
            str(segment),
        ]

        log(
            f"  [{i}/{len(cuts)}] {cut.path.name}: "
            f"{float(cut.start):.3f}–{float(cut.stop):.3f}s"
        )

        run(cmd, f"Рендер фрагмента #{i}")
        segments.append(segment)

    def quote_concat(path):
        value = str(path.resolve()).replace("\\", "/")
        return "'" + value.replace("'", "'\\''") + "'"

    concat_file = temp / "concat.txt"

    concat_file.write_text(
        "".join(
            f"file {quote_concat(p)}\n"
            for p in segments
        ),
        encoding="utf-8",
    )

    log("\nСклейка draft через concat demuxer…")

    started = time.perf_counter()

    run(
        [
            FFMPEG_EXE,
            "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(output),
        ],
        "Финальная склейка draft",
    )

    return time.perf_counter() - started


def make_fcpxml(cuts, media, name):
    first = media[cuts[0].path]

    assets = []
    ids = {}

    for i, (path, info) in enumerate(media.items(), 1):
        ref = f"r{i}"
        ids[path] = ref

        assets.append(
            f'        <asset id="{ref}" '
            f'name="{xml_escape(path.name)}" '
            f'uid="{xml_escape(str(path))}" '
            f'duration="{fcpx_time(info.duration)}" '
            f'hasVideo="1" '
            f'hasAudio="{int(info.has_audio)}">'
            f'<media-rep kind="original-media" '
            f'src="{xml_escape(path.as_uri())}"/>'
            f'</asset>'
        )

    clips = []
    offset = Fraction(0)

    for i, cut in enumerate(cuts, 1):
        clips.append(
            f'                        <asset-clip '
            f'name="{xml_escape(cut.path.stem)} [{i:02d}]" '
            f'ref="{ids[cut.path]}" '
            f'offset="{fcpx_time(offset)}" '
            f'start="{fcpx_time(cut.start)}" '
            f'duration="{fcpx_time(cut.duration)}" '
            f'enabled="1"/>'
        )

        offset += cut.duration

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.9">
    <resources>
        <format id="r-format"
                name="FFVideoFormat{first.width}x{first.height}"
                frameDuration="{fcpx_time(1 / first.fps)}"
                width="{first.width}"
                height="{first.height}"
                colorSpace="1-1-1 (Rec. 709)"/>
{chr(10).join(assets)}
    </resources>
    <library>
        <event name="{xml_escape(name)}">
            <project name="{xml_escape(name)}">
                <sequence format="r-format"
                          duration="{fcpx_time(offset)}"
                          tcStart="0s"
                          tcFormat="NDF"
                          audioLayout="stereo"
                          audioRate="48k">
                    <spine>
{chr(10).join(clips)}
                    </spine>
                </sequence>
            </project>
        </event>
    </library>
</fcpxml>
"""


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Собрать draft MP4 и FCPXML 1.9 из *-shots.md "
            "с помощью локальной LLM."
        )
    )

    parser.add_argument(
        "folder",
        help="Папка с *-shots.md и исходными MP4.",
    )

    parser.add_argument(
        "--base_url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI-compatible API URL (default: {DEFAULT_BASE_URL})",
    )

    parser.add_argument(
        "--prompt",
        default=None,
        help="Дополнение к системному промпту: желаемый настрой ролика.",
    )

    parser.add_argument(
        "--api-key",
        default=None,
        help="API-ключ для авторизации в LLM-сервере (заголовок Authorization: Bearer).",
    )

    parser.add_argument(
        "--model",
        default=None,
        help="Имя модели для LLM (по умолчанию: any).",
    )

    args = parser.parse_args()
    started = time.perf_counter()

    try:
        folder = Path(args.folder).expanduser().resolve()

        if not folder.is_dir():
            raise RuntimeError(f"Папка не существует: {folder}")

        log(f"Папка: {folder}")
        log(f"FFmpeg: {FFMPEG_EXE}")
        log(f"FFprobe: {FFPROBE_EXE}")

        descriptions = find_descriptions(folder)

        log(f"\nНайдено *-shots.md: {len(descriptions)}")

        for md in descriptions:
            paired = paired_mp4(md)
            log(
                f"  {md.name}"
                + (f" -> {paired.name}" if paired else "")
            )

        context = read_context(descriptions)

        log("\n=== 1. ЗАПРОС МОНТАЖА У LLM ===")

        calls, usage, llm_time, raw_result, payload = ask_llm(
            args.base_url,
            context,
            args.prompt,
            args.api_key,
            args.model,
        )

        # Сохраняем промпт и сырой ответ LLM
        folder_name = folder.name
        (folder / f"{folder_name}.prompt").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (folder / f"{folder_name}.answer").write_text(
            json.dumps(raw_result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        log(
            f"Время LLM: {llm_time:.2f} с; "
            f"tool calls: {len(calls)}"
        )

        if usage:
            log(
                f"Токены: prompt={usage.get('prompt_tokens', 'n/a')}, "
                f"completion={usage.get('completion_tokens', 'n/a')}, "
                f"total={usage.get('total_tokens', 'n/a')}"
            )

        log("\n=== 2. ПРОВЕРКА ВЫЗОВОВ CUT ===")

        cuts, media = parse_cuts(
            calls,
            folder,
        )

        total = sum(
            (c.duration for c in cuts),
            Fraction(0),
        )

        log(
            f"Фрагментов: {len(cuts)}; "
            f"исходников: {len(media)}; "
            f"длительность: {float(total):.3f} с"
        )

        draft = folder / f"{folder.name}-draft.mp4"
        fcpxml = folder / f"{folder.name}.fcpxml"

        log("\n=== 3. СОЗДАНИЕ DRAFT MP4 ===")

        with tempfile.TemporaryDirectory(
            prefix="director-"
        ) as temp_dir:

            ffmpeg_time = render_draft(
                cuts,
                media,
                draft,
                Path(temp_dir),
            )

        log(f"Создано: {draft}")

        log("\n=== 4. СОЗДАНИЕ FCPXML 1.9 ===")

        fcpxml.write_text(
            make_fcpxml(cuts, media, folder.name),
            encoding="utf-8",
            newline="\n",
        )

        log(f"Создано: {fcpxml}")

        log("\n=== МОНТАЖНЫЙ ЛИСТ ===")

        offset = Fraction(0)

        for i, cut in enumerate(cuts, 1):
            end = offset + cut.duration

            log(
                f"{i:02d}. timeline "
                f"{float(offset):.3f}–{float(end):.3f}s | "
                f"{cut.path.name} | "
                f"source {float(cut.start):.3f}–"
                f"{float(cut.stop):.3f}s"
            )

            offset = end

        log("\n=== СТАТИСТИКА ===")
        log(f"MD-файлов:              {len(descriptions)}")
        log(f"Исходных MP4:           {len(media)}")
        log(f"Фрагментов:             {len(cuts)}")
        log(f"Длительность фильма:    {float(total):.3f} с")
        log(f"Время LLM:              {llm_time:.2f} с")
        log(f"Время финальной склейки: {ffmpeg_time:.2f} с")
        log(f"Общее время:            {time.perf_counter() - started:.2f} с")
        log(f"Draft:                  {draft}")
        log(f"FCPXML:                 {fcpxml}")

        return 0

    except KeyboardInterrupt:
        log("\nПрервано пользователем.")
        return 130

    except Exception as exc:
        log(f"\nОШИБКА: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
