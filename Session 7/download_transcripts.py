import os
import re
from youtube_transcript_api import YouTubeTranscriptApi

VIDEOS = [
    ("4HAcUG-3Sgg", "Class 1"),
    ("7v_wwtXkmMQ", "Class 2"),
    ("v4dojhYgDqg", "Class 3"),
    ("QB3HdCwZslM", "Class 4 Part 1"),
    ("5lZXK79GoXk", "Class 4 Part 2"),
    ("Mfi9ZMIesnc", "Class 5"),
    ("ZD3mcMBurWg", "Class 6"),
    ("UpQAsfecvuI", "Updated V40 and V40 Next Companies"),
]

OUT_DIR = os.path.join(os.path.dirname(__file__), "transcripts")
os.makedirs(OUT_DIR, exist_ok=True)


def slug(s):
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def fmt_ts(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


api = YouTubeTranscriptApi()

for vid, title in VIDEOS:
    try:
        fetched = api.fetch(vid)
    except Exception as e:
        print(f"[FAIL] {title} ({vid}): {e}")
        continue

    snippets = fetched.snippets
    fname = os.path.join(OUT_DIR, f"{slug(title)}_{vid}.txt")
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n")
        f.write(f"# https://www.youtube.com/watch?v={vid}\n\n")
        for sn in snippets:
            f.write(f"[{fmt_ts(sn.start)}] {sn.text}\n")
    print(f"[OK]   {title}: {len(snippets)} lines -> {fname}")

print("Done.")
