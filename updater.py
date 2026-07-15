import json
import urllib.request
import urllib.error

from config import APP_VERSION, GITHUB_REPO


def parse_version(v):
    parts = []
    for chunk in v.strip().lstrip("v").split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def check_for_updates():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SwearBeeper-UpdateCheck"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest_tag = data.get("tag_name", "")
        html_url = data.get("html_url", f"https://github.com/{GITHUB_REPO}/releases")
        if not latest_tag:
            return None, None
        return latest_tag, html_url
    except Exception:
        return None, None
