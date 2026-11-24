#!/usr/bin/env python3
"""describe-video.py — анализ видео через визуальный LLM с кадровой синхронизацией и расшифровкой."""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

# Используем полный путь к portable ffmpeg (свежая версия)
FFMPEG_BIN = r"E:\PORTABLE\ffmpeg\bin"
FFMPEG_EXE = os.path.join(FFMPEG_BIN, "ffmpeg.exe")
FFPROBE_EXE = os.path.join(FFMPEG_BIN, "ffprobe.exe")

SYSTEM_PROMPT = (
    "Ты — аналитик видеопотока и драматург. На вход ты получаешь два чётко разделённых блока:\n\n"
    "БЛОК 1: [VOICE TRANSCRIPT]...[END VOICE TRANSCRIPT]\n"
    "Это расшифровка речи диктора (Whisper ASR). Это НЕ визуальный контент — это голосовая дорожка.\n"
    "Никогда не описывай текст внутри этого блока как «текст на экране», \"overlay\", \"надпись\", «титры».\n\n"
    "БЛОК 2: Кадры с таймкодами\n"
    "Каждый кадр — визуальное изображение экрана. Перед каждым кадром указана метка времени [MM:СС].\n\n"
    "Твоя задача:\n"
    "1. Синхронизировать визуальные события на кадрах с голосовой дорожкой из блока VOICE TRANSCRIPT.\n"
    "2. Восстанавливать целостную картину происходящего: кто что делает, что меняется на экране "
    "и как это связано со словами диктора.\n"
    "3. Опираться строго на факты из кадров и текста. Не выдумывай события, которые могли "
    "произойти в промежутках между кадрами, если это не очевидно из контекста.\n"
    "4. При ответе ссылайся на точные таймкоды (например, [00:04]), чтобы подтвердить свои выводы.\n\n"
    "Ты ОБЯЗАН дать драматургический анализ: описать начало, развитие сюжета, кульминацию (если есть), конец. "
    "Выделить сцены (если их больше одной). Определить месседж/идею видео.\n"
    "Ответить строго по структуре из ФОРМАТА ОТВЕТА ниже."
)

EXPECTED_OUTPUT_FORMAT = (
    "\n\nСТРОГИЙ ФОРМАТ ОТВЕТА:\n"
    "Ты ОБЯЗАН ответить в следующей структуре. Не пропускай разделы, не добавляй свои.\n\n"
    "## Драматургический анализ\n\n"
    "**Месседж/идея:** [кратко — о чём видео, что хочет донести автор]\n\n"
    "## Структура и развитие сюжета\n\n"
    "- **Начало [ММ:СС]:** завязка, исходная ситуация.\n"
    "- **Развитие:** ключевые события с таймкодами, как меняется действие.\n"
    "- **Кульминация/пик:** момент наибольшего напряжения или значимости (если есть).\n"
    "- **Конец [ММ:СС]:** развязка, финальное состояние.\n\n"
    "## Сцены (если более одной)\n\n"
    "### Сцена 1 [ММ:СС — ММ:СС]\n"
    "- **Визуальное содержание:** что происходит на экране.\n"
    "- **Аудиосопровождение:** что говорит диктор.\n"
    "- **Синхронизация:** как речь соотносится с визуалом.\n\n"
    "## Визуальные приёмы и ключевые моменты\n\n"
    "- Движение камеры, композиция, ракурс.\n"
    "- Значимые события с точными таймкодами [ММ:СС].\n"
)


def parse_args():
    p = argparse.ArgumentParser(
        description="Анализ видео через визуальный LLM с кадровой синхронизацией и расшифровкой."
    )
    p.add_argument("file", help="Путь к видеофайлу")
    p.add_argument("--fps", type=float, default=1.0, help="Частота извлечения кадров (по умолчанию 1)")
    p.add_argument("--size", type=int, default=768, help="Ширина для горизонтальных / высота для вертикальных (по умолчанию 768)")
    p.add_argument("--base_url", default="http://127.0.0.1:8080/v1", help="Базовый URL OpenAPI endpoint")
    p.add_argument("--api-key", default=None, help="API-ключ для авторизации в LLM-сервере")
    p.add_argument("--model", default=None, help="Имя модели для LLM (по умолчанию: any)")
    return p.parse_args()


def probe_video(path):
    """Возвращает dict с параметрами видео через ffprobe."""
    cmd = [
        FFPROBE_EXE,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    out = subprocess.check_output(cmd, text=True)
    return json.loads(out)


def extract_frames(video_path, basename, fps, size, w, h, is_vertical, rotation=0):
    """Извлекает кадры через ffmpeg как JPG. Возвращает список (путь, время_начало)."""
    out_dir = os.path.dirname(os.path.abspath(video_path))
    out_pattern = os.path.join(out_dir, f"{basename}-shot-%04d.jpg")

    if is_vertical:
        scale = "scale=-1:%d" % size
    else:
        scale = "scale=%d:-1" % size

    # Применяем поворот если есть
    if abs(rotation) == 90:
        if rotation == 90:
            rotate_filter = "transpose=1"
        else:
            rotate_filter = "transpose=2"
        vf = f"{rotate_filter},{scale},fps={fps}"
    else:
        vf = f"{scale},fps={fps}"

    cmd = [
        FFMPEG_EXE,
        "-i", video_path,
        "-vf", vf,
        "-q:v", "2",
        "-y",
        out_pattern,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    frames = sorted(
        [
            os.path.join(out_dir, f)
            for f in os.listdir(out_dir)
            if f.startswith(basename + "-shot-") and f.endswith(".jpg")
        ]
    )
    return frames


def encode_frame(path):
    """Кодирует кадр в base64."""
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def format_time(seconds):
    """Форматирует секунды как MM:SS."""
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m:02d}:{s:02d}"


def build_message(frames, voice_data):
    """Формирует multi-modal сообщение для LLM.

    Структура (разделена на два блока, чтобы избежать смешивания):
    1. Весь голосовой текст в отдельном блоке [VOICE TRANSCRIPT]...[/VOICE]
    2. Кадры с таймкодами: [MM:SS] → кадр → [MM:SS] → кадр ...
    """
    content = []
    segments = voice_data.get("segments", [])

    # Блок 1: вся расшифровка в одном месте
    transcript_lines = []
    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg["end"]
        seg_time_str = f"[{format_time(seg_start)} — {format_time(seg_end)}]"
        transcript_lines.append(f"{seg_time_str} {seg['text']}")

    content.append({
        "type": "text",
        "text": "[VOICE TRANSCRIPT]\n" + "\n".join(transcript_lines) + "\n[END VOICE TRANSCRIPT]",
    })

    # Блок 2: кадры с таймкодами
    frame_times = []
    for i, _ in enumerate(frames):
        t = round(i / args.fps, 2) if args.fps > 0 else 0
        frame_times.append(t)

    for i, frame_path in enumerate(frames):
        t = format_time(frame_times[i])
        content.append({"type": "text", "text": f"Кадр [MM:SS] = {t}"})
        frame_b64 = encode_frame(frame_path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{frame_b64}"},
        })

    return content


def call_llm(base_url, messages, api_key=None, model=None):
    """Отправляет сообщение на Visual LLM. Возвращает текст ответа."""
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model or "any",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + EXPECTED_OUTPUT_FORMAT},
            {"role": "user", "content": messages},
        ],
        "max_tokens": 4096,
        "temperature": 0.3,
    }
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read())
    elapsed = time.time() - t0
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    tokens = usage.get("total_tokens", 0)
    return content, elapsed, tokens


if __name__ == "__main__":
    args = parse_args()
    video_path = os.path.abspath(args.file)
    t_start = time.time()

    if not os.path.isfile(video_path):
        print(f"Ошибка: файл не найден — {video_path}", file=sys.stderr)
        sys.exit(1)

    # --- probe ---
    info = probe_video(video_path)
    stream = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            stream = s
            break
    if stream is None:
        print("Ошибка: видеострим не найден", file=sys.stderr)
        sys.exit(1)

    w = int(stream.get("width", 0))
    h = int(stream.get("height", 0))

    # Проверяем поворот из display matrix
    rotation = 0
    for sd in stream.get("side_data_list", []):
        if sd.get("side_data_type") == "Display Matrix":
            rotation = int(round(sd.get("rotation", 0)))
            break

    # Если поворот ±90 — меняем ширину/высоту местами
    if abs(rotation) == 90:
        w, h = h, w

    is_vertical = h > w
    duration = float(info["format"].get("duration", 0))
    basename = os.path.splitext(os.path.basename(video_path))[0]

    print(f"Видео: {video_path}")
    print(f"  Размер: {w}x{h}, {'вертикальное' if is_vertical else 'горизонтальное'}")
    print(f"  Длительность: {format_time(duration)}")
    print(f"  FPS кадров: {args.fps}, Size: {args.size}")
    print()

    # --- load voice JSON ---
    voice_path = os.path.join(os.path.dirname(video_path), f"{basename}-voice.json")
    if not os.path.isfile(voice_path):
        print(f"Ошибка: файл расшифровки не найден — {voice_path}", file=sys.stderr)
        print("Сначала запустите: python voice.py <video>", file=sys.stderr)
        sys.exit(1)

    with open(voice_path, "r", encoding="utf-8") as f:
        voice_data = json.load(f)

    seg_count = len(voice_data.get("segments", []))
    print(f"Расшифровка: {voice_path} ({seg_count} сегментов)")
    print()

    # --- extract frames ---
    print("Извлечение кадров…")
    frames = extract_frames(video_path, basename, args.fps, args.size, w, h, is_vertical, rotation)
    print(f"  Получено кадров: {len(frames)}")
    if len(frames) == 0:
        print("Ошибка: кадры не извлечены", file=sys.stderr)
        sys.exit(1)

    # --- build message ---
    print("Формирование запроса…")
    messages = build_message(frames, voice_data)
    print(f"  Элементов в сообщении: {len(messages)}")

    # --- call LLM ---
    print(f"\nОтправка запроса к LLM ({args.base_url})…")
    try:
        result, elapsed, tokens = call_llm(
            args.base_url, messages, args.api_key, args.model
        )
    except Exception as e:
        print(f"Ошибка LLM: {e}", file=sys.stderr)
        sys.exit(1)

    # --- save markdown ---
    out_dir = os.path.dirname(os.path.abspath(video_path))
    md_path = os.path.join(out_dir, f"{basename}-shots.md")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(result)

    # --- cleanup frames ---
    for fp in frames:
        os.remove(fp)

    print(f"\nРезультат сохранён: {md_path}")
    total_elapsed = time.time() - t_start
    print(f"Время обработки: {format_time(total_elapsed)} ({total_elapsed:.1f} с)")
    print(f"Токены: {tokens}, время LLM: {elapsed:.1f} с")
