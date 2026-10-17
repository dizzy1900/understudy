"""Opens the running dev server at 1920x1080 and saves screenshots into web/.screens/.

    .venv/bin/python web/scripts/shoot.py --times 5,20,40 --tag run1
    .venv/bin/python web/scripts/shoot.py --times 6 --tag storm --press s
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / ".screens"
GPU_FLAGS = ["--use-gl=angle", "--use-angle=metal", "--ignore-gpu-blocklist"]
SOFTWARE_FLAGS = ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:5173/")
    parser.add_argument("--times", default="5,20,40", help="seconds after load, comma separated")
    parser.add_argument("--tag", default="shot")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--press", action="append", default=[], help="key[@seconds], pressed once at that time")
    parser.add_argument("--click", action="append", default=[], help="text[@seconds], clicks the first button with that text")
    parser.add_argument("--software", action="store_true", help="software WebGL for machines without a GPU (slow)")
    parser.add_argument("--fps", action="store_true", help="measure frames per second after the last shot")
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    times = sorted(float(t) for t in args.times.split(","))
    actions: list[tuple[float, str, str]] = []
    for item in args.press:
        key, _, at = item.partition("@")
        actions.append((float(at or 1), "press", key))
    for item in args.click:
        text, _, at = item.partition("@")
        actions.append((float(at or 1), "click", text))
    actions.sort()

    with sync_playwright() as p:
        if args.software:
            browser = p.chromium.launch(args=SOFTWARE_FLAGS)
        else:
            browser = p.chromium.launch(channel="chromium", args=GPU_FLAGS)
        page = browser.new_page(viewport={"width": args.width, "height": args.height})
        problems: list[str] = []
        page.on("console", lambda m: problems.append(f"{m.type}: {m.text}") if m.type in ("error", "warning") else None)
        page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
        started = time.time()
        page.goto(args.url)
        pending = list(actions)
        for at in times:
            while True:
                now = time.time() - started
                due = [a for a in pending if a[0] <= min(at, now)]
                for a in due:
                    pending.remove(a)
                    if a[1] == "press":
                        page.keyboard.press(a[2])
                    else:
                        page.get_by_role("button", name=a[2]).first.click()
                if now >= at:
                    break
                time.sleep(0.1)
            path = OUT / f"{args.tag}-{int(at):02d}s.png"
            page.screenshot(path=str(path))
            print(path)
        if args.fps:
            frames = page.evaluate(
                """() => new Promise(resolve => { let n = 0; const t0 = performance.now();
                const tick = () => { n++; if (performance.now() - t0 > 3000) resolve(n / 3); else requestAnimationFrame(tick); };
                requestAnimationFrame(tick); })"""
            )
            print(f"fps {frames:.1f}")
        for line in problems[:15]:
            print("console:", line[:300])
        browser.close()


if __name__ == "__main__":
    main()
