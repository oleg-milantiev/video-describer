#!/usr/bin/env python3
"""voice.py — транскрипция аудио из MP4 через faster-whisper."""
#pip install faster-whisper
#python -m pip install --force-reinstall "av==18.1.0"

import argparse
import json
import os
import sys

from faster_whisper import WhisperModel


def main():
    parser = argparse.ArgumentParser(
        description="Транскрипция аудио из MP4-файла с пословными таймкодами."
    )
    parser.add_argument("file", help="Путь к MP4-файлу")
    args = parser.parse_args()

    audio_path = os.path.abspath(args.file)

    if not os.path.isfile(audio_path):
        print(f"Ошибка: файл не найден — {audio_path}", file=sys.stderr)
        sys.exit(1)

    basename = os.path.splitext(os.path.basename(audio_path))[0]
    output_path = os.path.join(os.path.dirname(audio_path), f"{basename}-voice.json")

    print(f"Файл: {audio_path}")
    print(f"Модель: large-v3-turbo (CPU, int8)")
    print()

    model = WhisperModel(
        "large-v3-turbo",
        device="cpu",
        compute_type="int8",
        cpu_threads=8,
        num_workers=1,
    )

    segments, info = model.transcribe(
        audio_path,
        language="ru",
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
        condition_on_previous_text=True,
    )

    result = {
        "language": info.language,
        "duration": info.duration,
        "segments": [],
    }

    for segment in segments:
        words = []

        for word in segment.words or []:
            words.append({
                "start": round(word.start, 3),
                "end": round(word.end, 3),
                "text": word.word,
                "probability": round(word.probability, 4),
            })

        result["segments"].append({
            "start": round(segment.start, 3),
            "end": round(segment.end, 3),
            "text": segment.text,
            "words": words,
        })

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Готово! Сегментов: {len(result['segments'])}")
    print(f"Результат: {output_path}")


if __name__ == "__main__":
    main()
