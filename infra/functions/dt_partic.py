"""
DT_PARTIC roster import (synchronized skating).

Two ISU OdfBody files are needed and joined on athlete Code:

  * DT_PARTIC_TEAMS — <Team Code Organisation Name> with a <Composition> of
    <Athlete Code> references and a <RegisteredEvent Event="…"> per team.
  * DT_PARTIC — <Participant Code GivenName FamilyName PrintName …> per skater.

A single TEAMS file usually spans several synchro events (e.g. Advanced Novice
and Junior), so callers import one Event at a time. Names are rendered
"FAMILY Given" (ISU protocol style) and rosters are sorted alphabetically.
"""
import logging
import xml.etree.ElementTree as ET


def _local(tag: str) -> str:
    return tag.split('}')[-1] if isinstance(tag, str) else ""


def parse_participants(xml_bytes) -> dict:
    """DT_PARTIC -> {athleteCode: {"given","family","printName"}}."""
    out = {}
    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        logging.error(f"Error parsing DT_PARTIC: {e}")
        return out
    for p in root.iter():
        if _local(p.tag) != "Participant":
            continue
        code = p.get("Code")
        if not code:
            continue
        out[code] = {
            "given": p.get("GivenName") or "",
            "family": p.get("FamilyName") or "",
            "printName": p.get("PrintName") or "",
        }
    return out


def _format_member(part: dict) -> str:
    family = (part.get("family") or "").strip()
    given = (part.get("given") or "").strip()
    if family or given:
        return f"{family.upper()} {given}".strip()
    return (part.get("printName") or "").strip()


def parse_team_rosters(teams_xml_bytes, participants: dict):
    """DT_PARTIC_TEAMS joined with a participants map -> list of team dicts:
        {code, name, org, event, members[]}  (members sorted alphabetically)."""
    teams = []
    try:
        root = ET.fromstring(teams_xml_bytes)
    except Exception as e:
        logging.error(f"Error parsing DT_PARTIC_TEAMS: {e}")
        return teams

    for t in root.iter():
        if _local(t.tag) != "Team":
            continue
        codes, seen = [], set()
        for a in t.iter():
            if _local(a.tag) == "Athlete" and a.get("Code") and a.get("Code") not in seen:
                seen.add(a.get("Code"))
                codes.append(a.get("Code"))
        events = [re.get("Event") for re in t.iter()
                  if _local(re.tag) == "RegisteredEvent" and re.get("Event")]
        members = []
        for c in codes:
            part = participants.get(c)
            members.append(_format_member(part) if part else c)
        members.sort()
        teams.append({
            "code": t.get("Code") or "",
            "name": t.get("Name") or "",
            "org": t.get("Organisation") or "",
            "event": events[0] if events else "",
            "members": members,
        })
    return teams


_EVENT_LABELS = {
    "BASNOV": "Basic Novice",
    "INTNOV": "Intermediate Novice",
    "ADVNOV": "Advanced Novice",
    "JUVENILE": "Juvenile",
    "MIXEDAGE": "Mixed Age",
    "JUNIOR": "Junior",
    "SENIOR": "Senior",
    "ADULT": "Adult",
}


def event_label(code: str) -> str:
    """Human label for an ISU event code, e.g.
    'FSKXSYNCHRONADVNOV----' -> 'Advanced Novice'."""
    token = (code or "").replace("-", "").strip()
    for prefix in ("FSKXSYNCHRON", "FSKSYNCHRON", "SYNCHRON", "FSKX", "FSK"):
        if token.startswith(prefix):
            token = token[len(prefix):]
            break
    return _EVENT_LABELS.get(token, token.title() if token else "Synchronized Skating")


def distinct_events(teams):
    """Ordered [{code, label, count}] for the events present in a team list."""
    order, counts = [], {}
    for t in teams:
        code = t.get("event", "")
        if code not in counts:
            counts[code] = 0
            order.append(code)
        counts[code] += 1
    return [{"code": c, "label": event_label(c), "count": counts[c]} for c in order]
