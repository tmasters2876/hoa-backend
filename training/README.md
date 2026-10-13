# Training videos — scripts

One file per video in `scripts/`. Each is a list of scenes. The `say` lines are
the narration, word for word, and become the captions. The other keys direct the
capture: which page, which sandbox account, what to click, what to zoom on.

Voice: American female, natural register. Written for volunteers: invitations and
reassurance, no rules or warnings, no personal names. Recorded in the DEV sandbox
as the dev accounts, so no resident appears on screen.

Delivery: individual MP4s with a caption file (.vtt) beside each, to the OneDrive
Training folder. Captions are a separate track so viewers can turn them on or off.

Member set (everyone): 00–06. Board set (Board accounts): 10–14, written after
the member set is approved.

## Building the videos

```bash
cd training
uv venv .venv --python 3.13 && uv pip install --python .venv/bin/python playwright openai pyyaml requests python-dotenv pillow supabase
../dev/dev.sh up && ../dev/dev.sh run          # the DEV console on 5052 must be running
.venv/bin/python build.py all                   # demo data → capture → narration → captions → MP4s
.venv/bin/python build.py all 04                # one video
.venv/bin/python build.py cleanup               # remove the demo flags and the sample edit from DEV
.venv/bin/python build.py publish               # copy to OneDrive/Plantation Lake Committee/Training
```

- `scripts/*.yaml` — the words (narration = captions). `capture.yaml` — what the browser does, one entry per scene.
- Capture runs in Chrome against the DEV sandbox only, as dev-member / dev-board / dev-superuser, at 1920×1080 (2× for crisp zooms).
  The DEV badge and outbox link are hidden; on the Users page real people's rows are hidden so only sandbox accounts appear.
- Narration: OpenAI `gpt-4o-mini-tts`, voice `coral` (American female, warm, conversational). Cached by text, so only
  changed lines are re-voiced.
- Each MP4 carries a switchable English caption track (mov_text); the same captions sit beside it as a `.vtt`.
- A changed screen means editing `capture.yaml`; changed words mean editing the script. Re-run `build.py all <video>`.
