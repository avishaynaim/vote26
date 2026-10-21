"""Build the hosted (static) version of the dashboard into web/dist.

The dashboard page (static/index.html) is used unchanged; web/engine.js is added in front
of it and answers its /api/... requests inside the browser, running the same analysis and
the 5-day mock simulation on the 2022 data downloaded from the results site.

    python3 tools/build_web.py
"""

import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "web", "dist")


def main() -> None:
    shutil.rmtree(DIST, ignore_errors=True)
    os.makedirs(os.path.join(DIST, "data"))
    with open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8") as f:
        page = f.read()
    # the host wraps the page in its own <html><head><body> skeleton
    page = re.sub(r"<!doctype html>\s*|</?html[^>]*>\s*|</?head>\s*|</?body>\s*|<meta charset=[^>]*>\s*|<meta name=\"viewport\"[^>]*>\s*", "", page, flags=re.I)
    page = "<meta charset=\"utf-8\">\n" + page
    page = page.replace("<title>תוצאות הבחירות בזמן אמת</title>",
                        "<title>Knesset Election Live</title>\n"
                        "<script>document.documentElement.lang = \"he\"; document.documentElement.dir = \"rtl\";</script>", 1)
    extra_css = ("header { top: env(safe-area-inset-top, 0px); }\n"
                 "body::before { content: \"\"; position: fixed; inset: 0 0 auto; height: env(safe-area-inset-top, 0px); z-index: 10; background: var(--surface); }\n"
                 "a[href=\"/api/export/cities.csv\"] { display: none !important; }\n</style>")
    page = page.replace("</style>", extra_css, 1)
    page = page.replace("<script>\n\"use strict\";", "<script src=\"engine.js\" charset=\"utf-8\"></script>\n<script>\n\"use strict\";", 1)
    with open(os.path.join(DIST, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    shutil.copy(os.path.join(ROOT, "web", "engine.js"), DIST)
    shutil.copy(os.path.join(ROOT, "config.json"), os.path.join(DIST, "data", "config.json"))
    for fn in ("expb.csv", "national.json"):
        shutil.copy(os.path.join(ROOT, "data", "baseline", "k25", fn), os.path.join(DIST, "data", fn))
    print("built", DIST)


if __name__ == "__main__":
    main()
