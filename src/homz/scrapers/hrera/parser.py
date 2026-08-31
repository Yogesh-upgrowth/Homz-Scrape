"""Parser for Haryana RERA's public "Registered Projects" table.

Page: https://haryanarera.gov.in/admincontrol/registered_projects/2 — linked
from the HRERA Gurugram portal's own home page as "Registered Projects" (see
`homz.scrapers.hrera` module docstring for why this is a legitimate public
source, not an internal admin endpoint). It renders as one large table
(~1,000+ rows in a single response — no server-side pagination) inside
`<table id="compliant_hearing">`.

Column order (confirmed from the live header row, verified 2026-08-31):
  Serial No. | Registration Certificate Number | Project ID | Project Name |
  Builder | Project Location | Project District | Registered With |
  Details of Project(Form A-H) | Registration Up-to | View Certificate |
  View Quarterly Progress | Monitoring Orders | View OC/CC/PCC

Only Gurugram district is covered by this page — Faridabad and the rest of
Haryana fall under the separate HRERA Panchkula authority, whose equivalent
public listing was not resolved (the site's `project_search_public` search
form needs a session/CSRF flow this module does not implement). See
`homz.enrichment.rera_matching` for the consumer of this data.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from bs4 import BeautifulSoup, Tag

from homz.common.parsing import clean_text, normalize_name

_DETAIL_HREF_RE = re.compile(r"searchprojectDetail/(\d+)")
_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
# A handful of rows put the literal status text "Lapsed Project" in the
# Project ID column instead of a RERA number — verified live 2026-08-31, e.g.
# source_id 1634 ("102 EDEN ESTATE II") appears twice: once with "Lapsed
# Project" as the link text, once with its real superseding registration
# "RERA-GRG-874-2021". Reject anything that isn't the real ID shape rather
# than storing a status string where a RERA number belongs.
_RERA_NUMBER_RE = re.compile(r"^RERA-[A-Z]+-\d+-\d{4}$")


def _parse_registration_upto(text: str | None) -> date | None:
    if not text:
        return None
    m = _DATE_RE.search(text)
    if not m:
        return None
    day, month, year = (int(x) for x in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _row_to_dict(anchor: Tag) -> dict[str, Any] | None:
    """`anchor` is the `<a href=".../searchprojectDetail/{id}">RERA-...</a>`
    link — the one stable landmark in an otherwise plain, id-less row."""
    tr = anchor.find_parent("tr")
    if tr is None:
        return None
    cells = tr.find_all("td", recursive=False)
    if len(cells) < 11:
        return None

    rera_number = clean_text(anchor.get_text())
    if not rera_number or not _RERA_NUMBER_RE.match(rera_number):
        return None
    match = _DETAIL_HREF_RE.search(anchor.get("href", ""))
    source_id = match.group(1) if match else None

    cert_anchor = cells[10].find("a", href=True)
    certificate_url = cert_anchor["href"] if cert_anchor else None

    project_name = clean_text(cells[3].get_text(" ", strip=True))
    builder_name = clean_text(cells[4].get_text(" ", strip=True))

    return {
        "rera_number": rera_number,
        "source_id": source_id,
        "certificate_number": clean_text(cells[1].get_text(" ", strip=True)),
        "project_name": project_name,
        "project_name_normalized": normalize_name(project_name),
        "builder_name": builder_name,
        "builder_name_normalized": normalize_name(builder_name),
        "location_text": clean_text(cells[5].get_text(" ", strip=True)),
        "district": clean_text(cells[6].get_text(" ", strip=True)),
        "registered_with": clean_text(cells[7].get_text(" ", strip=True)),
        "registration_upto": _parse_registration_upto(cells[9].get_text()),
        "certificate_url": certificate_url,
    }


def parse_registered_projects_page(html: str) -> list[dict[str, Any]]:
    """Parse the full registered-projects table into a list of row dicts.

    Finds rows by the presence of the project-detail link (the
    `searchprojectDetail` href pattern) rather than by table id or column
    position, so a markup reshuffle elsewhere on the page doesn't silently
    return zero rows — same "search by a stable leaf, not a fixed path"
    reasoning as `homz.scrapers.housing.parser`.
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    seen_rera: set[str] = set()
    for anchor in soup.find_all("a", href=_DETAIL_HREF_RE):
        row = _row_to_dict(anchor)
        if row is None or row["rera_number"] in seen_rera:
            continue
        seen_rera.add(row["rera_number"])
        rows.append(row)
    return rows
