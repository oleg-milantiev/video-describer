# AGENTS.md — Agent Guide

## Video Processing Pipeline

```
MP4 → voice.py → voice.json → describe-video.py → shots.md → director.py → draft.mp4 + .fcpxml
```

## Script Rules

### 1. voice.py — Transcription

- Run: `python voice.py path/to/video.mp4`
- Saves `{basename}-voice.json` in the same directory as the video
- Model: `large-v3-turbo`, CPU int8, 8 threads
- Language: `ru`, word_timestamps=True, vad_filter=True
- **Never pass credentials via terminal.** Use vault if an API key is needed.

### 2. describe-video.py — Video Analysis

- Run: `python describe-video.py path/to/video.mp4`
- Requires `{basename}-voice.json` in the same directory (created by voice.py)
- Extracts JPG frames via ffmpeg, default size 768px side
- Sends to LLM: `[VOICE TRANSCRIPT]...[END VOICE TRANSCRIPT]` block + frames
- Saves structured analysis to `{basename}-shots.md`

**Important details:**
- Frames are deleted after sending to LLM (cleanup)
- System prompt strictly separates: VOICE TRANSCRIPT ≠ visual content
- In the API call, system role uses `"content"` (not `"text"`) — otherwise LLM ignores the prompt

**Parameters:**
- `--fps N` — frame extraction rate (default 1)
- `--size N` — frame side length (default 768)
- `--base_url URL` — LLM endpoint (default `http://127.0.0.1:8080/v1`)
- `--api-key KEY` — Bearer token for authorization
- `--model NAME` — model name

### 3. director.py — AI Editing

- Run: `python director.py ./folder`
- Reads all `*-shots.md` files from the folder
- Creates `draft.mp4` and `{foldername}.fcpxml`
- Uses the same LLM as describe-video.py

## LLM Message Structure (describe-video.py)

```python
messages = [
    {
        "role": "system",
        "content": SYSTEM_PROMPT + EXPECTED_OUTPUT_FORMAT
    },
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "[VOICE TRANSCRIPT]\n[00:00 — 00:05] text...\n[END VOICE TRANSCRIPT]"},
            {"type": "text", "text": "Frame [MM:SS] = 00:00"},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
            ...
        ]
    }
]
```

**Key:** voice text in a separate block, frames separately. Never mix them!

## System Prompt (SYSTEM_PROMPT)

Required LLM answer sections:
1. **Dramaturgical analysis** — message/idea of the video
2. **Story structure** — beginning, development, climax, end with timestamps
3. **Scenes** — if more than one, each with visuals + audio + synchronization
4. **Visual techniques** — camera, composition, key moments

## ffmpeg Path (Windows)

```python
FFMPEG_BIN = r"E:\PORTABLE\ffmpeg\bin"
FFMPEG_EXE = os.path.join(FFMPEG_BIN, "ffmpeg.exe")
FFPROBE_EXE = os.path.join(FFMPEG_BIN, "ffprobe.exe")
```

## Handling Vertical Videos

- Detected automatically via `h > w` (after accounting for rotation from display matrix)
- Horizontal: `scale=768:-1` (width 768)
- Vertical: `scale=-1:768` (height 768)
- ±90° rotation applied via `transpose=1/2` before scaling

## Batch Processing

File `describe-all.cmd` in a video folder runs `describe-video.py` for all `.mp4` files.

## Common Errors and Fixes

| Problem | Solution |
|---------|----------|
| LLM ignores system prompt | Check `"content"` instead of `"text"` in system role |
| Model confuses voice with on-screen text | Voice text strictly inside `[VOICE TRANSCRIPT]...[END VOICE TRANSCRIPT]` block |
| Frames not deleted | Add cleanup after LLM call |
| `OSError: [WinError 123]` in paths | Use forward-slash or raw strings `r"..."` |

## Do Not

- Save `.prompt` files (removed)
- Pass credentials via terminal — use vault
- Invent events between frames
- Describe voice text as visual content
