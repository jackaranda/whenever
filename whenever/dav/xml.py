"""Helpers for building and parsing WebDAV/CalDAV XML."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import Union

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CALSERVER = "http://calendarserver.org/ns/"
APPLE = "http://apple.com/ns/ical/"

for _prefix, _ns in (("D", DAV), ("C", CALDAV), ("CS", CALSERVER), ("ICAL", APPLE)):
    ET.register_namespace(_prefix, _ns)


def d(name: str) -> str:
    return f"{{{DAV}}}{name}"


def c(name: str) -> str:
    return f"{{{CALDAV}}}{name}"


def cs(name: str) -> str:
    return f"{{{CALSERVER}}}{name}"


def apple(name: str) -> str:
    return f"{{{APPLE}}}{name}"


# A property value: plain text, or a callable that fills in the property element.
Filler = Union[str, Callable[[ET.Element], None]]
PropMap = dict[str, Filler]

# Properties that allprop must not return (RFC 4791 §9.6).
ALLPROP_EXCLUDE = {c("calendar-data")}


class XMLError(ValueError):
    pass


def parse(body: bytes) -> ET.Element | None:
    if not body or not body.strip():
        return None
    try:
        return ET.fromstring(body)
    except ET.ParseError as exc:
        raise XMLError(str(exc)) from exc


def requested_props(root: ET.Element | None) -> list[str] | None:
    """Property names asked for in a PROPFIND/REPORT body. None means allprop."""
    if root is None:
        return None
    prop = root.find(d("prop"))
    if prop is None:
        return None
    return [child.tag for child in prop]


def hrefs(*values: str) -> Callable[[ET.Element], None]:
    def fill(el: ET.Element) -> None:
        for value in values:
            ET.SubElement(el, d("href")).text = value

    return fill


def empty_children(*tags: str) -> Callable[[ET.Element], None]:
    def fill(el: ET.Element) -> None:
        for tag in tags:
            ET.SubElement(el, tag)

    return fill


def privileges(*tags: str) -> Callable[[ET.Element], None]:
    def fill(el: ET.Element) -> None:
        for tag in tags:
            ET.SubElement(ET.SubElement(el, d("privilege")), tag)

    return fill


def supported_reports(*tags: str) -> Callable[[ET.Element], None]:
    def fill(el: ET.Element) -> None:
        for tag in tags:
            report = ET.SubElement(ET.SubElement(el, d("supported-report")), d("report"))
            ET.SubElement(report, tag)

    return fill


def multistatus() -> ET.Element:
    return ET.Element(d("multistatus"))


def _propstat(resp: ET.Element, items: list[tuple[str, Filler | None]], status: str) -> None:
    propstat = ET.SubElement(resp, d("propstat"))
    prop = ET.SubElement(propstat, d("prop"))
    for tag, value in items:
        el = ET.SubElement(prop, tag)
        if callable(value):
            value(el)
        elif value is not None:
            el.text = value
    ET.SubElement(propstat, d("status")).text = status


def add_response(ms: ET.Element, href: str, props: PropMap, requested: list[str] | None) -> None:
    resp = ET.SubElement(ms, d("response"))
    ET.SubElement(resp, d("href")).text = href
    if requested is None:
        found = [(tag, value) for tag, value in props.items() if tag not in ALLPROP_EXCLUDE]
        missing: list[str] = []
    else:
        found = [(tag, props[tag]) for tag in requested if tag in props]
        missing = [tag for tag in requested if tag not in props]
    if found or not missing:
        _propstat(resp, found, "HTTP/1.1 200 OK")
    if missing:
        _propstat(resp, [(tag, None) for tag in missing], "HTTP/1.1 404 Not Found")


def add_status(ms: ET.Element, href: str, status: str) -> None:
    resp = ET.SubElement(ms, d("response"))
    ET.SubElement(resp, d("href")).text = href
    ET.SubElement(resp, d("status")).text = status


def error(tag: str, href: str | None = None) -> bytes:
    """A DAV:error body naming the failed precondition."""
    root = ET.Element(d("error"))
    el = ET.SubElement(root, tag)
    if href:
        ET.SubElement(el, d("href")).text = href
    return serialize(root)


def serialize(el: ET.Element) -> bytes:
    return ET.tostring(el, encoding="utf-8", xml_declaration=True)
