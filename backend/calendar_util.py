from datetime import date, timedelta


def ics_for_container(container: dict, bl: dict | None = None) -> str:
    lfd = container.get("lfd_final") or date.today().isoformat()
    end = (date.fromisoformat(lfd) + timedelta(days=1)).strftime("%Y%m%d")
    start = date.fromisoformat(lfd).strftime("%Y%m%d")
    number = container.get("number") or "container"
    uid = f"lastday-ctr-{container['id']}@lastday.local"
    vessel = (bl or {}).get("vessel") or "-"
    return "\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//LastFreeDay//EN",
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:Last free day — {number}",
            f"DESCRIPTION:Vessel {vessel} / pickup before end of {lfd}",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
