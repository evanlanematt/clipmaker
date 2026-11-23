"""
Reads a campaign requirements page (e.g. reachclipping.com/CORE) and turns it into
Clip Maker settings: caption tags, rules for the moment picker, clip length, and warnings.
"""
from __future__ import annotations

import json
import re
import ssl
import urllib.request
from html.parser import HTMLParser

CLAUDE_MODEL = "claude-sonnet-5-5"


class _TextGrabber(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0
        self.title, self.in_title, self.h1, self.in_h1 = "", False, "", False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag == "h1":
            self.in_h1 = True
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "li" and not self.skip:
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "h1":
            self.in_h1 = False
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.in_h1 and not self.skip:
            self.h1 += data
        if not self.skip:
            self.parts.append(data)


def fetch_text(url: str) -> tuple[str, str]:
    if not re.match(r"^https?://", url):
        url = "https://" + url
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"})
    try:
        import truststore  # uses Windows' certificates, avoids SSL errors
        ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        html = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    g = _TextGrabber()
    g.feed(html)
    text = "".join(g.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    name = (g.h1 or g.title).strip()
    name = re.split(r"\s+[|\-–—]\s+", name)[0].strip()
    return text.strip(), name[:60]


def _lengths(text: str):
    m = re.search(r"(\d{1,3})\s*(?:s|sec|secs|seconds)?\s*(?:-|–|to)\s*(\d{1,3})\s*(?:s\b|sec|secs|seconds)", text, re.I)
    if m:
        a, b = sorted(int(x) for x in m.groups())
        if 3 <= a < b <= 600:
            return a, b
    lo = re.search(r"(?:at least|minimum|min\.?)\s*(\d{1,3})\s*(?:s\b|sec|seconds)", text, re.I)
    hi = re.search(r"(?:under|max(?:imum)?\.?|no longer than|up to)\s*(\d{1,3})\s*(?:s\b|sec|seconds)", text, re.I)
    if lo or hi:
        return (int(lo.group(1)) if lo else None), (int(hi.group(1)) if hi else None)
    return None, None


def parse_free(text: str) -> dict:
    """No-AI fallback: pull hashtags, rule-like sentences, and clip length."""
    tags = []
    for t in re.findall(r"(?<![\w&/])#([A-Za-z][\w]{1,30})", text):
        if f"#{t}" not in tags:
            tags.append(f"#{t}")
    rule_words = re.compile(r"\b(must|no |not allowed|don't|do not|only|required|banned|prohibited|never|at least)\b", re.I)
    rules = [ln.strip("- ").strip() for ln in text.splitlines()
             if 15 < len(ln) < 300 and rule_words.search(ln)]
    lo, hi = _lengths(text)
    warnings = []
    m = re.search(r"within\s+(\d+)\s*(minutes|mins|hours)", text, re.I)
    if m:
        warnings.append(f"Submit each clip within {m.group(1)} {m.group(2)} of posting.")
    must_know = re.compile(r"(strike|banned|ban\b|not allowed in|must be submitted to|ends \d|end date|"
                           r"minimum [\d,]+ views|min views|hashtags? for each|audience must)", re.I)
    for ln in text.splitlines():
        ln = ln.strip("- ").strip()
        if 15 < len(ln) < 300 and must_know.search(ln) and ln not in warnings:
            warnings.append(ln)
    return {
        "required_text": " ".join(tags[:5]),
        "rules": " | ".join(dict.fromkeys(rules))[:1500],
        "min_len": lo, "max_len": hi,
        "campaign": "",
        "warnings": warnings,
    }


def parse_claude(text: str, api_key: str) -> dict:
    import anthropic

    prompt = f"""Below is the text of a clipping campaign's requirements page. Extract settings for an
automatic clip maker. Reply with ONLY a JSON object:
{{"campaign": "short campaign name",
  "required_text": "exact hashtags/mentions every post caption must include, space separated, or empty",
  "rules": "concise instructions for choosing and captioning moments (who must appear, what kind of moments, tone, banned content, caption language). Max 600 characters.",
  "min_len": seconds or null, "max_len": seconds or null,
  "warnings": ["short must-know items for the clipper that the tool can't enforce, e.g. submission deadlines, audience rules, account rules, strike policy, minimum views, end date"]}}
Only include clip lengths if the page states them.

PAGE TEXT:
{text[:60000]}"""
    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(model=CLAUDE_MODEL, max_tokens=1500,
                                 messages=[{"role": "user", "content": prompt}])
    out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    data = json.loads(out[out.find("{"): out.rfind("}") + 1])
    data.setdefault("warnings", [])
    return data


def read_requirements(url: str, api_key: str = "", log=print) -> tuple[dict, str]:
    log("Reading requirements page...")
    text, name = fetch_text(url)
    if len(text) < 200:
        raise RuntimeError("That page didn't have readable text (it may need a login or load by script). "
                           "Copy the requirements and paste them into the 'Campaign rules' box instead.")
    data = None
    if api_key:
        try:
            log("Asking Claude to pull out the rules...")
            data = parse_claude(text, api_key)
        except Exception as e:
            log(f"Claude couldn't read it ({e}). Using the free method.")
    if data is None:
        data = parse_free(text)
    if not data.get("campaign"):
        data["campaign"] = name
    return data, text
