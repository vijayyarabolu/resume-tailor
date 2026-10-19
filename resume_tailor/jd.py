"""Job description from a URL (only where robots.txt allows; LinkedIn and friends refuse bots, so the user pastes
the text) and company/role guesses from the text."""
import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser

USER_AGENT = "resume_tailor/1.0 (personal, on-demand; one request per click)"
TIMEOUT_S = 20
MAX_REDIRECTS = 4
ALLOWED_PORTS = {80, 443}
MAX_BYTES = 3 * 1024 * 1024
BLOCKED_HOSTS = {"linkedin.com": "LinkedIn", "indeed.com": "Indeed", "glassdoor.com": "Glassdoor",
                 "ziprecruiter.com": "ZipRecruiter", "dice.com": "Dice"}
_BLOCK_TAGS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article",
               "header", "footer", "table", "dd", "dt", "blockquote", "pre"}
_ROLE_WORDS = re.compile(r"\b(engineer|scientist|developer|analyst|architect|manager|lead|specialist|researcher|"
                         r"consultant|intern|director|designer|administrator)\b", re.I)
# "Company: Acme" / "Title: Data Engineer" lines on top of a pasted JD (a tracker's copy header)
_LABELLED = re.compile(r"(?im)^[ \t]*(company|employer|(?:job[ \t]+)?title|role|position)[ \t]*:[ \t]*(\S.*?)[ \t]*$")


class JdError(ValueError):
    pass


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self._skip, self.title, self._in_title, self.ld = [], 0, "", False, []
        self._ld = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
            if tag == "script" and dict(attrs).get("type", "").lower() == "application/ld+json":
                self._ld = True
        if tag == "title":
            self._in_title = True
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)
            self._ld = False
        if tag == "title":
            self._in_title = False
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._ld:
            self.ld.append(data)
        elif self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _Text()
    p.feed(markup)
    text = "".join(p.parts)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _job_posting(blocks: list) -> dict | None:
    for raw in blocks:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for node in [item, *(item.get("@graph") or [])]:
                if isinstance(node, dict) and "JobPosting" in str(node.get("@type", "")):
                    return node
    return None


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


def _public_url(url: str) -> None:
    """JdError unless the URL is http(s) on a standard port to a host whose every address is public (no loopback,
    LAN, link-local, CGNAT or metadata addresses): the server must not become a fetch-anything proxy."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise JdError("that is not a web address")
    if (parts.port or (443 if parts.scheme == "https" else 80)) not in ALLOWED_PORTS:
        raise JdError("only standard web ports are fetched: paste the job text instead")
    try:
        infos = socket.getaddrinfo(parts.hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as err:
        raise JdError(f"the site's name does not resolve: paste the job text instead") from err
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global or addr in ipaddress.ip_network("100.64.0.0/10"):
            raise JdError("that address is private or local, not a public job page: paste the job text instead")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                      # every hop is re-validated by fetch()


_OPENER = urllib.request.build_opener(_NoRedirect)


def blocked_site(url: str) -> str | None:
    host = _host(url)
    return next((label for domain, label in BLOCKED_HOSTS.items() if host == domain or host.endswith("." + domain)),
                None)


def robots_allows(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        req = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            rp.parse(resp.read(200_000).decode(errors="replace").splitlines())
    except urllib.error.HTTPError as err:
        return err.code in (401, 403, 404)            # no robots.txt = allowed (RFC 9309)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
    return rp.can_fetch(USER_AGENT, url) and rp.can_fetch("*", url)


def _get(url: str, hops: int = 0) -> tuple:
    """(raw bytes, content type) after validating the URL and, separately, every redirect hop."""
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise JdError("that is not a web address")
    label = blocked_site(url)
    if label:
        raise JdError(f"{label} does not allow bots to read postings: paste the job text instead")
    _public_url(url)
    if not robots_allows(url):
        raise JdError("this site's robots.txt does not allow fetching the page: paste the job text instead")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            return resp.read(MAX_BYTES), resp.headers.get("Content-Type", "")
    except urllib.error.HTTPError as err:
        location = err.headers.get("Location") if err.headers else None
        if err.code in (301, 302, 303, 307, 308) and location and hops < MAX_REDIRECTS:
            return _get(urllib.parse.urljoin(url, location), hops + 1)
        raise JdError(f"the page answered {err.code}: paste the job text instead") from err
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise JdError("could not reach the page: paste the job text instead") from err


_GH_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io")      # EU boards: the page, as before


def _ats_api(url: str) -> tuple | None:
    """(kind, api url, slug, job id) for a Greenhouse / Lever / Ashby job link. Their pages load the job with
    JavaScript (or redirect to a company careers page that does), so the page text is the site's menu."""
    parts = urllib.parse.urlsplit(url.strip())
    host, path = (parts.hostname or "").lower(), [p for p in parts.path.split("/") if p]
    query = urllib.parse.parse_qs(parts.query)
    if host in _GH_HOSTS and len(path) >= 3 and path[1] == "jobs" and path[2].isdigit():
        board, jid = path[0], path[2]
    elif query.get("gh_jid") and query.get("board") and query["gh_jid"][0].isdigit():
        board, jid = query["board"][0], query["gh_jid"][0]
    elif host == "jobs.lever.co" and len(path) >= 2:
        return "lever", f"https://api.lever.co/v0/postings/{path[0]}/{path[1]}", path[0], path[1]
    elif host == "jobs.ashbyhq.com" and len(path) >= 2:
        return "ashby", f"https://api.ashbyhq.com/posting-api/job-board/{path[0]}", path[0], path[1]
    else:
        return None
    return "greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}?content=true", board, jid


def _slug_name(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", slug) if w)


def _from_api(kind: str, data: dict, slug: str, jid: str) -> dict:
    if kind == "greenhouse":
        title, company = data.get("title"), data.get("company_name")
        text = html_to_text(html.unescape(str(data.get("content") or "")))
    elif kind == "lever":
        title, company = data.get("text"), None
        lists = "\n\n".join(f"{x.get('text', '')}\n{html_to_text(str(x.get('content') or ''))}"
                             for x in data.get("lists") or [] if isinstance(x, dict))
        parts = (data.get("descriptionPlain"), lists, data.get("additionalPlain"))
        text = "\n\n".join(str(t).strip() for t in parts if t)
    else:
        job = next((j for j in data.get("jobs") or [] if isinstance(j, dict) and j.get("id") == jid), None)
        if job is None:
            raise JdError("that job is no longer on the company's board: paste the job text instead")
        title, company = job.get("title"), None
        text = str(job.get("descriptionPlain") or "").strip() or html_to_text(str(job.get("descriptionHtml") or ""))
    title = str(title or "").strip()
    return {"text": text.strip(), "title": title, "role": title, "source": kind,
            "company": str(company or "").strip() or _slug_name(slug)}


def fetch(url: str) -> dict:
    """{text, title, company, role, source} or JdError with the reason to show the user."""
    ats = _ats_api(url)
    if ats:
        raw, _ = _get(ats[1])
        try:
            got = _from_api(ats[0], json.loads(raw), ats[2], ats[3])
        except (ValueError, AttributeError) as err:
            if isinstance(err, JdError):
                raise
            raise JdError("the job board answered in an unexpected way: paste the job text instead") from err
        if len(got["text"]) < 100:
            raise JdError("the posting has no readable job text: paste the job text")
        return got
    raw, ctype = _get(url)
    markup = raw.decode("utf-8", errors="replace")
    if "html" not in ctype and "<" not in markup[:200]:
        text, title, posting = markup.strip(), "", None
    else:
        parser = _Text()
        parser.feed(markup)
        posting = _job_posting(parser.ld)
        title = parser.title.strip()
        text = html_to_text(markup)
    company, role = "", ""
    if posting:
        role = str(posting.get("title") or "").strip()
        org = posting.get("hiringOrganization")
        company = str((org.get("name") if isinstance(org, dict) else org) or "").strip()
        desc = posting.get("description")
        if isinstance(desc, str) and len(desc) > 200:
            text = html_to_text(desc) if "<" in desc else desc.strip()
    if not text or len(text) < 100:
        raise JdError("the page has no readable job text (it probably needs JavaScript): paste the job text")
    g_company, g_role = guess(text, title)
    return {"text": text, "title": title, "company": company or g_company, "role": role or g_role,
            "source": "json-ld" if posting else "html"}


def guess(text: str, title: str = "") -> tuple:
    """(company, role) guessed from a page title ('Senior ML Engineer - Acme' / 'Acme | AI Engineer') or text."""
    company, role = "", ""
    if title:
        parts = [p.strip() for p in re.split(r"\s+[|\-–—:]\s+|\s+at\s+|\s+@\s+", title) if p.strip()]
        roles = [p for p in parts if _ROLE_WORDS.search(p)]
        others = [p for p in parts if p not in roles and not re.search(
            r"\b(careers?|jobs?|apply|hiring|openings?|positions?|opportunities|vacancies|explore|join us)\b", p, re.I)]
        role = roles[0] if roles else ""
        company = others[0] if others else ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for label, value in _LABELLED.findall("\n".join(lines[:8])):
        if label.lower() in ("company", "employer"):
            company = company or value
        else:
            role = role or value
    if not role:
        role = next((ln for ln in lines[:8] if len(ln) <= 80 and _ROLE_WORDS.search(ln)), "")
    if not company:
        m = re.search(r"\b(?:About|Join|at)[ \t]+([A-Z][A-Za-z0-9&.'-]*(?:[ \t]+[A-Z][A-Za-z0-9&.'-]*){0,3})", text)
        company = m.group(1).strip() if m else ""
        company = re.sub(r"\s+(?:is|are|was|the|team|we|our|you)$", "", company, flags=re.I)
    return company[:80], role[:120]
