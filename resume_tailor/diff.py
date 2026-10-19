"""Bullet-by-bullet diff of a tailored resume against its base, for the review panel. Word-level opcodes from
difflib so the UI can highlight changed words."""
import difflib


def _ops(a: str, b: str) -> list:
    aw, bw = a.split(), b.split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aw, bw, autojunk=False).get_opcodes():
        out.append([tag, " ".join(aw[i1:i2]), " ".join(bw[j1:j2])])
    return out


def _pair(a: str, b: str) -> dict:
    changed = a.strip() != b.strip()
    return {"base": a, "new": b, "changed": changed, "ops": _ops(a, b) if changed else []}


def bullet_diff(base, tailored) -> dict:
    """base / tailored: docxio.Structure."""
    skills = []
    for i, s in enumerate(tailored.skills):
        b = base.skills[i] if i < len(base.skills) else None
        skills.append({"category": s.category, "base_category": b.category if b else "",
                       "items": _pair(b.items if b else "", s.items)})
    jobs = []
    for i, job in enumerate(tailored.jobs):
        bj = base.jobs[i] if i < len(base.jobs) else {"bullets": [], "company": "", "title": "", "dates": ""}
        bullets = [_pair(bj["bullets"][k] if k < len(bj["bullets"]) else "", text)
                   for k, text in enumerate(job["bullets"])]
        jobs.append({"company": job["company"], "title": job["title"], "dates": job["dates"], "bullets": bullets})
    return {"headline": _pair(base.headline, tailored.headline), "summary": _pair(base.summary, tailored.summary),
            "skills": skills, "jobs": jobs,
            "changed_bullets": sum(1 for j in jobs for b in j["bullets"] if b["changed"]),
            "total_bullets": sum(len(j["bullets"]) for j in jobs)}
