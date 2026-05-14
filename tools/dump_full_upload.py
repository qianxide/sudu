"""Dump the FULL body of the imagekit upload request to see all multipart fields."""
import json, sys, re
from pathlib import Path

HAR = sys.argv[1]
har = json.loads(Path(HAR).read_text(encoding="utf-8"))
for e in har["log"]["entries"]:
    if e["request"]["method"] == "POST" and "upload.imagekit.io" in e["request"]["url"]:
        body = (e["request"].get("postData") or {}).get("text", "")
        # body might be truncated; check size
        size = e["request"].get("bodySize", 0)
        print(f"\n=== POST {e['request']['url']} ===")
        print(f"bodySize header: {size}, captured text len: {len(body)}")
        # extract all form fields (everything except the binary "file" part)
        # form parts split by boundary line
        m = re.search(r"boundary=([^\r\n;]+)", e["request"]["postData"].get("mimeType", "") + str([h for h in e["request"]["headers"] if h["name"].lower()=="content-type"]))
        # easier: just find all 'name="..."' parts with values
        parts = re.split(r"--+[a-zA-Z0-9_-]+(?:--)?", body)
        for i, p in enumerate(parts):
            if not p.strip():
                continue
            name_m = re.search(r'name="([^"]+)"', p)
            if not name_m:
                continue
            name = name_m.group(1)
            # body content after blank line
            after = p.split("\r\n\r\n", 1)
            value = after[1] if len(after) > 1 else ""
            # truncate file payload
            if name == "file":
                value = f"<binary {len(value)} chars>"
            else:
                value = value.strip().splitlines()[0] if value.strip() else ""
                value = value[:200]
            print(f"  field name={name!r}  value={value!r}")
        break  # only first one
