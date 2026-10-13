"""Training videos for the PLCA admin console, built from scripts/*.yaml + capture.yaml.

    .venv/bin/python build.py all            # setup → capture → voice → render, every video
    .venv/bin/python build.py setup          # fresh demo flags in the DEV sandbox
    .venv/bin/python build.py capture [v…]   # browser walk-through, screenshots per zoom
    .venv/bin/python build.py voice [v…]     # narration (American female) + captions
    .venv/bin/python build.py render [v…]    # zooms, highlights, title card → MP4 + VTT
    .venv/bin/python build.py publish        # copy MP4 + VTT to OneDrive/…/Training
    .venv/bin/python build.py cleanup        # remove the demo data from DEV

Runs only against the local DEV console (http://127.0.0.1:5052). The scripts are the
words; capture.yaml is what the browser does. Nothing here touches production.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml
from dotenv import dotenv_values
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
BUILD = HERE / "build"
SCRIPTS = HERE / "scripts"
BASE = "http://127.0.0.1:5052"
ACCOUNTS = {"member": "dev-member", "board": "dev-board", "superuser": "dev-superuser"}
DEV_PASSWORD = "devpass123"
VIEW_W, VIEW_H, SCALE = 1920, 1080, 2
OUT_W, OUT_H, FPS = 1920, 1080, 30
TITLE_SECONDS = 2.8
LEAD, TAIL = 0.35, 0.6                      # silence around each scene's narration
VOICE = "coral"
VOICE_MODEL = "gpt-4o-mini-tts"
VOICE_STYLE = ("American woman, warm and natural, conversational pace, like a friendly neighbor "
               "showing someone around a tool she likes. Clear, unhurried, not a broadcaster.")
ACCENT = (15, 110, 86)
ONEDRIVE = Path.home() / "Library/CloudStorage/OneDrive-Personal/Plantation Lake Committee/Training"

assert BASE.startswith("http://127.0.0.1"), "training capture runs against the local DEV console only"

INIT_JS = r"""
(() => {
  const css = `.dev-badge{display:none!important} a.nav-item[href*='/admin/dev/mail']{display:none!important}
               *{caret-color:transparent!important}`;
  const tidy = () => {
    if (!document.getElementById('__train_css')) {
      const s = document.createElement('style'); s.id='__train_css'; s.textContent = css; document.head.appendChild(s);
    }
    document.querySelectorAll('.flash').forEach(f => { if (f.textContent.includes('DEV · local sandbox')) f.remove(); });
  };
  if (document.readyState !== 'loading') tidy(); else document.addEventListener('DOMContentLoaded', tidy);
})();
"""
# The Users and Activity tables list real people; the recording shows only the sandbox accounts.
HIDE_REAL_USERS = r"""
document.querySelectorAll('table tbody tr').forEach(tr => {
  const cells = [...tr.querySelectorAll('td')].map(td => td.textContent.trim());
  if (!cells.length) return;
  const hasDev = cells.some(t => t.startsWith('dev-'));
  if (!hasDev) tr.style.display = 'none';
});
"""


# ── helpers ──────────────────────────────────────────────────────────────────

def log(*a):
    print(*a, flush=True)


def load_scripts(only: list[str] | None = None) -> list[dict]:
    out = []
    for p in sorted(SCRIPTS.glob("*.yaml")):
        d = yaml.safe_load(p.read_text())
        if only and d["video"] not in only and not any(d["video"].startswith(o) for o in only):
            continue
        out.append(d)
    return out


def demo() -> dict:
    p = BUILD / "demo.json"
    return json.loads(p.read_text()) if p.exists() else {}


def session(role: str) -> requests.Session:
    s = requests.Session()
    r = s.post(f"{BASE}/login", data={"username": ACCOUNTS[role], "password": DEV_PASSWORD}, allow_redirects=False)
    assert r.status_code == 302 and "login" not in r.headers.get("Location", ""), f"login failed for {role}"
    return s


def dev_db():
    from supabase import create_client
    env = dotenv_values(REPO / "dev" / ".env.dev")
    assert "127.0.0.1" in env["SUPABASE_URL"] or "localhost" in env["SUPABASE_URL"], "dev DB must be local"
    return create_client(env["SUPABASE_URL"], env["SUPABASE_SERVICE_ROLE_KEY"])


def flag_spec(name: str) -> dict:
    return yaml.safe_load((REPO.parent / "plca-revision" / "flags" / name).read_text())


# ── setup / state / cleanup (DEV sandbox only) ───────────────────────────────

def cmd_cleanup():
    db = dev_db()
    d = demo()
    for key in ("A", "B"):
        fid = d.get(key)
        if fid:
            db.table("clause_flag_comments").delete().eq("flag_id", fid).execute()
            db.table("clause_flags").delete().eq("id", fid).execute()
            log(f"removed demo flag {key} {fid}")
    # the sample summary edit from video 12: decline it through the console, so the clause is untouched
    su = session("superuser")
    pend = db.table("pending_changes").select("id").eq("status", "pending").eq("submitted_by", "dev-board").execute().data or []
    for p in pend:
        su.post(f"{BASE}/admin/pending/{p['id']}/reject", data={"review_notes": "Sample edit recorded for the training video."})
    if pend:
        log(f"declined {len(pend)} sample edit(s)")
    (BUILD / "demo.json").unlink(missing_ok=True)


def cmd_setup():
    BUILD.mkdir(exist_ok=True)
    cmd_cleanup()
    m = session("member")
    a = flag_spec("01-roof-framing-current-code.yaml")
    r = m.post(f"{BASE}/admin/flags", data={"flag_type": "clause", "clause_id": a["clause_id"], "flag_notes": a["notes"].strip()}).json()
    assert r["ok"], r
    A = r["flag_id"]
    b = flag_spec("07-outbuildings-before-the-dwelling.yaml")
    r = m.post(f"{BASE}/admin/flags", data={"flag_type": "clause", "clause_id": b["clause_id"], "flag_notes": b["notes"].strip()}).json()
    assert r["ok"], r
    B = r["flag_id"]
    m.post(f"{BASE}/admin/flags/{B}/comment", data={"comment": "Agreed. The recorded commitment is what makes this enforceable."})
    m.post(f"{BASE}/admin/flags/{B}/proposal", data={"proposal_current": b["proposal"]["current"].strip(),
                                                     "proposal_text": b["proposal"]["proposed"].strip(),
                                                     "proposal_source": b["proposal"]["source"]})
    m.post(f"{BASE}/admin/flags/{B}/submit")
    (BUILD / "demo.json").write_text(json.dumps({"A": A, "B": B}, indent=1))
    log(f"demo flags: A={A} (roof framing, open)  B={B} (outbuildings, awaiting Board)")


def run_state(op: str):
    d = demo()
    key, action = op.split(".")
    fid = d[key]
    m = session("member")
    if action == "proposal":
        a = flag_spec("01-roof-framing-current-code.yaml")
        m.post(f"{BASE}/admin/flags/{fid}/proposal", data={"proposal_current": a["proposal"]["current"].strip(),
                                                           "proposal_text": a["proposal"]["proposed"].strip(),
                                                           "proposal_source": a["proposal"]["source"]})
    elif action == "submit":
        m.post(f"{BASE}/admin/flags/{fid}/submit")
    elif action == "recall":
        m.post(f"{BASE}/admin/flags/{fid}/recall", data={"reason": "Sent a little early; one more look first."})
    elif action == "close":
        m.post(f"{BASE}/admin/flags/{fid}/close", data={"reason": "Sample close for the training video."})
    elif action == "reopen":
        m.post(f"{BASE}/admin/flags/{fid}/reopen")
    elif action == "approve":
        session("board").post(f"{BASE}/admin/flags/{fid}/decide",
                              data={"decision": "approved", "decision_notes": "Approved for attorney review and the owner vote."})
    else:
        raise SystemExit(f"unknown state op {op}")


# ── capture ──────────────────────────────────────────────────────────────────

def cmd_capture(only=None):
    from playwright.sync_api import sync_playwright

    plan = yaml.safe_load((HERE / "capture.yaml").read_text())
    d = demo()
    if not d:
        raise SystemExit("run `build.py setup` first")
    sub = lambda s: s.replace("{A}", d["A"]).replace("{B}", d["B"]) if isinstance(s, str) else s
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        pages: dict[str, object] = {}

        def page_for(role):
            if role not in pages:
                ctx = browser.new_context(viewport={"width": VIEW_W, "height": VIEW_H}, device_scale_factor=SCALE)
                ctx.add_init_script(INIT_JS)
                pg = ctx.new_page()
                pg.on("dialog", lambda dlg: dlg.accept())
                if role != "anon":
                    pg.goto(f"{BASE}/login")
                    pg.fill("#username", ACCOUNTS[role]); pg.fill("#password", DEV_PASSWORD)
                    pg.click(".login-btn"); pg.wait_for_load_state("networkidle")
                pages[role] = pg
            return pages[role]

        for script in load_scripts(only):
            v = script["video"]
            role_default = {"member": "member", "board": "board", "superuser": "superuser"}[script["audience"]]
            out = BUILD / v / "shots"
            shutil.rmtree(out, ignore_errors=True); out.mkdir(parents=True)
            scenes = plan[v]
            segments = []
            role = role_default
            for si, steps in enumerate(scenes, 1):
                role = next((s["as"] for s in steps if "as" in s), role_default)
                pg = page_for(role)
                segs, last_img, dirty = [], None, True
                for st in steps:
                    st = {k: (sub(x) if not isinstance(x, list) else [sub(y) for y in x]) for k, x in st.items()}
                    if "as" in st:
                        continue
                    if "goto" in st:
                        pg.goto(BASE + st["goto"]); pg.wait_for_load_state("networkidle"); dirty = True
                    elif "click" in st:
                        pg.locator(st["click"]).first.click(); pg.wait_for_load_state("networkidle"); dirty = True
                    elif "fill" in st:
                        loc = pg.locator(st["fill"][0]).first
                        loc.fill(""); loc.type(st["fill"][1], delay=4); dirty = True
                    elif "wait" in st:
                        pg.locator(st["wait"]).first.wait_for(timeout=120000); dirty = True
                    elif "sleep" in st:
                        pg.wait_for_timeout(st["sleep"]); dirty = True
                    elif "js" in st:
                        pg.evaluate(HIDE_REAL_USERS if st["js"] == "HIDE_REAL_USERS" else st["js"]); dirty = True
                    elif "state" in st:
                        run_state(st["state"]); dirty = True
                    elif "shot" in st:
                        for focus in st["shot"]:
                            bbox = None
                            if focus != "FULL":
                                try:
                                    loc = pg.locator(focus).first
                                    loc.wait_for(state="visible", timeout=8000)
                                    before = pg.evaluate("window.scrollY")
                                    loc.evaluate("el => el.scrollIntoView({block: el.getBoundingClientRect().height > window.innerHeight * 0.6 ? 'start' : 'center', inline: 'nearest'})")
                                    pg.wait_for_timeout(250)
                                    if pg.evaluate("window.scrollY") != before:
                                        dirty = True
                                    b = loc.bounding_box()
                                    bbox = [b["x"] * SCALE, b["y"] * SCALE, b["width"] * SCALE, b["height"] * SCALE] if b else None
                                except Exception as e:
                                    log(f"  ! {v} scene {si}: focus {focus!r} not found ({str(e)[:80]}); using full frame")
                            if dirty or last_img is None:
                                last_img = out / f"s{si:02d}_{len(segs):02d}.png"
                                pg.screenshot(path=str(last_img))
                                dirty = False
                            segs.append({"img": last_img.name, "bbox": bbox, "full": focus == "FULL"})
                if not segs:
                    last_img = out / f"s{si:02d}_00.png"; pg.screenshot(path=str(last_img))
                    segs.append({"img": last_img.name, "bbox": None, "full": True})
                segments.append(segs)
                log(f"  {v} scene {si}: {len(segs)} zoom(s) as {role}")
            (BUILD / v / "segments.json").write_text(json.dumps(segments, indent=1))
            if v.startswith("02"):
                ans = pages["member"].locator(".answer-panel").first.inner_text() if "member" in pages else ""
                if "other as approved" not in ans.lower():
                    log("  ! search answer does not mention 'Other as approved by ARC' — check video 02 narration")
            log(f"captured {v}")
        browser.close()


# ── voice + captions ─────────────────────────────────────────────────────────

def ffprobe_dur(p: Path) -> float:
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(p)]).strip())


def tts(text: str, path: Path):
    key = hashlib.sha256(f"{VOICE}|{VOICE_MODEL}|{VOICE_STYLE}|{text}".encode()).hexdigest()[:16]
    cache = BUILD / "tts-cache" / f"{key}.mp3"
    if not cache.exists():
        from openai import OpenAI
        cache.parent.mkdir(parents=True, exist_ok=True)
        client = OpenAI(api_key=dotenv_values(REPO / ".env")["OPENAI_API_KEY"])
        with client.audio.speech.with_streaming_response.create(model=VOICE_MODEL, voice=VOICE, input=text,
                                                                 instructions=VOICE_STYLE) as r:
            r.stream_to_file(str(cache))
    shutil.copy(cache, path)


def cue_chunks(text: str, max_chars=84) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    out = []
    for s in sentences:
        while len(s) > max_chars:
            cut = s.rfind(", ", 0, max_chars)
            cut = cut + 1 if cut > 30 else s.rfind(" ", 0, max_chars)
            out.append(s[:cut].strip()); s = s[cut:].strip()
        if s:
            out.append(s)
    merged = []
    for c in out:
        if merged and len(merged[-1]) + 1 + len(c) <= max_chars and len(c) < 30:
            merged[-1] += " " + c
        else:
            merged.append(c)
    return merged


def wrap2(s: str, width=44) -> str:
    if len(s) <= width:
        return s
    cut = s.rfind(" ", 0, len(s) // 2 + 8)
    return s[:cut] + "\n" + s[cut + 1:]


def vtt_time(t: float) -> str:
    h, rem = divmod(t, 3600); m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}"


def cmd_voice(only=None):
    for script in load_scripts(only):
        v = script["video"]
        adir = BUILD / v / "audio"; adir.mkdir(parents=True, exist_ok=True)
        cues, t = [], TITLE_SECONDS
        wavs = [adir / "title.wav"]
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(TITLE_SECONDS), str(wavs[0])], check=True)
        durs = []
        for i, sc in enumerate(script["scenes"], 1):
            text = " ".join(sc["say"].split())
            mp3 = adir / f"s{i:02d}.mp3"; wav = adir / f"s{i:02d}.wav"
            tts(text, mp3)
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp3), "-af",
                            f"adelay={int(LEAD*1000)}:all=1,apad=pad_dur={TAIL}", "-ar", "24000", "-ac", "1", str(wav)], check=True)
            speech = ffprobe_dur(mp3); total = ffprobe_dur(wav)
            chunks = cue_chunks(text); n = sum(len(c) for c in chunks)
            ct = t + LEAD
            for c in chunks:
                d = speech * len(c) / n
                cues.append((ct, ct + d, c)); ct += d
            t += total; durs.append(total); wavs.append(wav)
        lst = adir / "concat.txt"
        lst.write_text("".join(f"file '{w.name}'\n" for w in wavs))
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "pcm_s16le", str(BUILD / v / "narration.wav")], check=True)
        vtt = ["WEBVTT", ""]
        for k, (a, b, c) in enumerate(cues, 1):
            vtt += [str(k), f"{vtt_time(a)} --> {vtt_time(b - 0.05)}", wrap2(c), ""]
        (BUILD / v / "captions.vtt").write_text("\n".join(vtt))
        (BUILD / v / "durations.json").write_text(json.dumps(durs))
        log(f"voiced {v}: {len(durs)} scenes, {t:.0f}s")


# ── render ───────────────────────────────────────────────────────────────────

def font(size, bold=False):
    for f in (("/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf"),
              "/System/Library/Fonts/Helvetica.ttc"):
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default()


def title_card(script) -> Image.Image:
    im = Image.new("RGB", (OUT_W, OUT_H), (26, 40, 32))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([OUT_W // 2 - 36, 300, OUT_W // 2 + 36, 372], 16, fill=(36, 56, 45))
    d.ellipse([OUT_W // 2 - 16, 320, OUT_W // 2 + 16, 352], outline=(170, 190, 178), width=4)
    def centre(text, y, f, fill):
        w = d.textlength(text, font=f); d.text(((OUT_W - w) / 2, y), text, font=f, fill=fill)
    num = script["video"].split("-")[0]
    centre(script["title"], 430, font(76, True), (232, 237, 233))
    who = {"member": "Committee members", "board": "Board members", "superuser": "Administrators"}[script["audience"]]
    centre(f"{who} · Video {num}", 540, font(36), (170, 190, 178))
    centre("Plantation Lakes Community Association · Committee Console", OUT_H - 120, font(28), (120, 140, 128))
    return im


def target_rect(bbox, W, H):
    if not bbox:
        return (0.0, 0.0, float(W), float(H))
    x, y, w, h = bbox
    pad = 70
    tw = max(w + 2 * pad, (h + 2 * pad) * OUT_W / OUT_H, W * 0.36)
    if tw >= W * 0.86:
        return (0.0, 0.0, float(W), float(H))
    th = tw * OUT_H / OUT_W
    cx, cy = x + w / 2, y + h / 2
    rx = min(max(cx - tw / 2, 0), W - tw); ry = min(max(cy - th / 2, 0), H - th)
    return (rx, ry, tw, th)


def ease(u):
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, u)))


def lerp_rect(a, b, u):
    return tuple(a[i] + (b[i] - a[i]) * u for i in range(4))


def draw_frame(img: Image.Image, rect, bbox, glow: float, fast: bool) -> bytes:
    x, y, w, h = rect
    fr = img.resize((OUT_W, OUT_H), Image.BILINEAR if fast else Image.LANCZOS,
                    box=(x, y, x + w, y + h))
    if bbox and glow > 0:
        sx, sy = OUT_W / w, OUT_H / h
        bx0, by0 = (bbox[0] - x) * sx - 10, (bbox[1] - y) * sy - 10
        bx1, by1 = (bbox[0] + bbox[2] - x) * sx + 10, (bbox[1] + bbox[3] - y) * sy + 10
        ov = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
        od = ImageDraw.Draw(ov)
        od.rounded_rectangle([bx0, by0, bx1, by1], 14, outline=ACCENT + (int(230 * glow),), width=5)
        fr = Image.alpha_composite(fr.convert("RGBA"), ov).convert("RGB")
    return fr.tobytes()


def cmd_render(only=None):
    out_dir = BUILD / "out"; out_dir.mkdir(parents=True, exist_ok=True)
    for script in load_scripts(only):
        v = script["video"]
        vdir = BUILD / v
        segments = json.loads((vdir / "segments.json").read_text())
        durs = json.loads((vdir / "durations.json").read_text())
        assert len(segments) == len(durs) == len(script["scenes"]), f"{v}: capture/voice out of step; re-run both"
        num, title = v.split("-")[0], script["title"]
        mp4 = out_dir / f"{num} {title}.mp4"
        silent = vdir / "picture.mp4"
        ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}",
                               "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                               "-pix_fmt", "yuv420p", str(silent)], stdin=subprocess.PIPE)
        frames = 0
        card = title_card(script).tobytes()
        for _ in range(int(TITLE_SECONDS * FPS)):
            ff.stdin.write(card); frames += 1
        prev_img_name, prev_rect = None, None
        cache: dict[str, Image.Image] = {}
        for segs, dur in zip(segments, durs):
            nframes_scene = round(dur * FPS)
            per = [nframes_scene // len(segs)] * len(segs)
            per[-1] += nframes_scene - sum(per)
            for seg, nf in zip(segs, per):
                if seg["img"] not in cache:
                    cache = {seg["img"]: Image.open(vdir / "shots" / seg["img"]).convert("RGB")}
                img = cache[seg["img"]]
                W, H = img.size
                full = (0.0, 0.0, float(W), float(H))
                tgt = full if seg["full"] else target_rect(seg["bbox"], W, H)
                start = prev_rect if (prev_img_name == seg["img"] and prev_rect) else full
                move = 0 if start == tgt else min(int(FPS * (1.8 if seg["full"] else 1.0)), int(nf * 0.45))
                glow_frames = int(FPS * 0.35)
                held = None
                for f in range(nf):
                    if f < move:
                        rect = lerp_rect(start, tgt, ease((f + 1) / move))
                        ff.stdin.write(draw_frame(img, rect, None, 0, True))
                    else:
                        g = 0.0 if seg["full"] or not seg["bbox"] or tgt == full else min(1.0, (f - move + 1) / glow_frames)
                        static = g == 0.0 or g >= 1.0          # nothing moving: render once, reuse
                        if static and held is not None and held[0] == g:
                            ff.stdin.write(held[1])
                        else:
                            fr = draw_frame(img, tgt, seg["bbox"] if g > 0 else None, g, not static)
                            if static:
                                held = (g, fr)
                            ff.stdin.write(fr)
                    frames += 1
                prev_img_name, prev_rect = seg["img"], tgt
        ff.stdin.close(); ff.wait()
        # second pass: narration + a captions track the viewer can switch on or off
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(silent), "-i", str(vdir / "narration.wav"),
                        "-i", str(vdir / "captions.vtt"), "-map", "0:v", "-map", "1:a", "-map", "2:s",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-c:s", "mov_text",
                        "-metadata:s:s:0", "language=eng", "-metadata:s:s:0", "title=English",
                        "-metadata", f"title={title}", "-movflags", "+faststart", str(mp4)], check=True)
        shutil.copy(vdir / "captions.vtt", mp4.with_suffix(".vtt"))
        log(f"rendered {mp4.name}  {frames / FPS:.0f}s")


def cmd_publish():
    out = BUILD / "out"
    for p in sorted(out.glob("*.mp4")):
        num = int(p.name.split(" ")[0])
        folder = ONEDRIVE / ("1 Committee members" if num < 10 else "2 Board and administration")
        folder.mkdir(parents=True, exist_ok=True)
        for f in (p, p.with_suffix(".vtt")):
            shutil.copy(f, folder / f.name)
        log(f"published {folder.name}/{p.name}")


def main(argv):
    cmd, rest = (argv[0] if argv else "all"), argv[1:]
    only = rest or None
    if cmd == "setup": cmd_setup()
    elif cmd == "cleanup": cmd_cleanup()
    elif cmd == "capture": cmd_capture(only)
    elif cmd == "voice": cmd_voice(only)
    elif cmd == "render": cmd_render(only)
    elif cmd == "publish": cmd_publish()
    elif cmd == "all":
        cmd_setup(); cmd_capture(only); cmd_voice(only); cmd_render(only)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
