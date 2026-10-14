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
- Narration: OpenAI `gpt-4o-mini-tts`, voice `nova` with a bright, cheerful, professional style (owner: "bubbly and cheerful, but professional and helpful"). Cached by text, so only
  changed lines are re-voiced.
- Each MP4 carries a switchable English caption track (mov_text); the same captions sit beside it as a `.vtt`.
- An animated arrow pointer glides to each highlighted element as the narrator reaches it and taps on arrival (owner request); it slips off-screen on wide shots.
- A changed screen means editing `capture.yaml`; changed words mean editing the script. Re-run `build.py all <video>`.

## Links in the console

`videos.yaml` is the one list. The videos play **inside the console** (`/admin/training/<number>`, a `<video>` player
with a Captions button) from the **private** `training` bucket of the console's Supabase project, where each lives as
`<number>.mp4` + `<number>.vtt`. The page mints one-hour signed URLs per visit, so only a signed-in account of the
right role ever receives a link (the Board set is a 404 for members, and no link is minted for it).

```bash
.venv/bin/python build.py publish         # OneDrive copies + web copies (crf 23, ≤2.5 Mb/s, stereo 44.1 kHz, no embedded captions)
.venv/bin/python build.py upload          # push the web copies to the bucket (creates it, private, on first run)
.venv/bin/python build.py upload 04       # one video — replaces 04.mp4 / 04.vtt in place, nothing in the console changes
```

`upload` reads the production `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` from `../.env`. The console shows "Watch the
video" under each page's title and lists every video in Help & Reference and on the Guide. Captions are the `.vtt` as a
`<track>`; the viewer's on/off choice is remembered in the browser. The DEV sandbox's local Supabase has no bucket, so
there the player page renders with a "not available" notice — the page is testable, the stream is not.

Google Drive is the archive: the web copies also go to *Plantation Lakes - HOA Documents / Training videos* and the
file id goes in `drive_id`, which the player page shows as "Also in Google Drive". Drive's own player never finished
processing these files (2026-10-14), which is why the console hosts them. The Supabase org is on the Pro plan
(100 GB storage included); the twelve web copies total ~200 MB.
