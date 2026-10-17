"""Drives the voice features in a real browser and prints what happened.

    .venv/bin/python web/scripts/voice_check.py --url http://localhost:5173/ --audio /tmp/understudy-voice/storm.wav

Chromium gets a fake microphone that plays --audio, so holding Space records that clip. Speech synthesis is wrapped to
log every line the page asks for, and the REST calls the page makes are logged with their bodies.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / ".screens"
GPU_FLAGS = ["--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"]

SPY = """
(() => {
  window.__spoken = [];
  const synth = window.speechSynthesis;
  if (!synth) return;
  const speak = synth.speak.bind(synth);
  synth.speak = (u) => {
    const entry = { text: u.text, rate: u.rate, voice: u.voice ? u.voice.name : null, at: performance.now(), start: null, end: null, error: null };
    u.addEventListener('start', () => { entry.start = performance.now(); });
    u.addEventListener('end', () => { entry.end = performance.now(); });
    u.addEventListener('error', (e) => { entry.error = e.error; });
    window.__spoken.push(entry);
    speak(u);
  };
})();
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5173/")
    parser.add_argument("--audio", default="/tmp/understudy-voice/storm.wav")
    parser.add_argument("--tag", default="voice")
    parser.add_argument("--skip-back", action="store_true")
    parser.add_argument("--hold", type=float, default=3.5, help="seconds Space is held")
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    flags = GPU_FLAGS + [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={args.audio}",
        "--autoplay-policy=no-user-gesture-required",
    ]
    log: list[str] = []
    problems: list[str] = []
    started = time.time()

    def stamp(text: str) -> None:
        line = f"[{time.time() - started:6.1f}s] {text}"
        log.append(line)
        print(line, flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chromium", args=flags)
        context = browser.new_context(viewport={"width": 1920, "height": 1080})
        context.grant_permissions(["microphone"])
        page = context.new_page()
        page.add_init_script(SPY)
        page.on("console", lambda m: problems.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))

        def on_request(request) -> None:
            if "/api/" in request.url and request.method == "POST":
                body = request.post_data_buffer
                shown = body.decode() if body and request.headers.get("content-type", "").startswith("application/json") else f"{len(body or b'')} bytes {request.headers.get('content-type')}"
                head = f" starts {bytes(body[:4])!r}" if body and not shown.startswith("{") else ""
                stamp(f"POST {request.url.split('/api/')[1]} {shown}{head}")

        def on_response(response) -> None:
            if "/api/" in response.url and response.request.method == "POST":
                try:
                    text = response.text()
                except Exception:
                    text = ""
                stamp(f"  -> {response.status} {text[:260]}")

        page.on("request", on_request)
        page.on("response", on_response)

        page.goto(args.url)
        page.wait_for_timeout(4000)
        voices = page.evaluate("speechSynthesis.getVoices().map(v => v.name + '/' + v.lang).slice(0, 8)")
        stamp(f"voices: {voices}")
        stamp(f"understudyVoice: {page.evaluate('Object.keys(window.understudyVoice || {})')}")

        page.keyboard.press("m")
        page.wait_for_timeout(300)
        stamp(f"after M, muted tag shown: {page.locator('.tag-warn').count() > 0}")
        page.keyboard.press("m")
        page.wait_for_timeout(300)
        stamp(f"after M again, muted tag shown: {page.locator('.tag-warn').count() > 0}")

        page.locator("input[aria-label='Type a command']").click()
        page.keyboard.type("storm rows two and three")
        page.screenshot(path=str(OUT / f"{args.tag}-typed.png"))
        page.keyboard.press("Enter")
        page.wait_for_timeout(2500)
        page.screenshot(path=str(OUT / f"{args.tag}-typed-after.png"))
        page.keyboard.press("Escape")

        stamp("hold Space")
        page.locator("body").click(position={"x": 900, "y": 500})
        page.keyboard.down("Space")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(OUT / f"{args.tag}-listening.png"))
        page.wait_for_timeout(int(max(0, args.hold - 1.2) * 1000))
        page.keyboard.up("Space")
        stamp("release Space")
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / f"{args.tag}-sending.png"))
        page.wait_for_timeout(4500)
        page.screenshot(path=str(OUT / f"{args.tag}-heard.png"))

        if not args.skip_back:
            stamp("press B")
            page.keyboard.press("b")
            waited = 0.0
            for at in (2, 8, 14, 20, 26, 32, 38):
                page.wait_for_timeout(int((at - waited) * 1000))
                waited = at
                banner = page.locator(".replay-banner, .replay").count()
                stamp(f"B+{at}s: briefing card {page.locator('.briefing').count()}, replay banner {banner}")
                page.screenshot(path=str(OUT / f"{args.tag}-back-{at:02d}.png"))

        spoken = page.evaluate("window.__spoken")
        stamp("speech log: " + json.dumps(spoken, indent=1)[:3000])
        for line in problems[:12]:
            stamp("console: " + line[:240])
        browser.close()


if __name__ == "__main__":
    main()
