"""Tiny Subsonic response helpers: JSON and XML rendering of the same dict."""
import re
import xml.etree.ElementTree as ET

from fastapi.responses import JSONResponse, Response

NS = "http://subsonic.org/restapi"
VERSION = "1.16.1"


def ok(**extra) -> dict:
    return {"status": "ok", "version": VERSION, "type": "navidrome-tidal-proxy", "openSubsonic": True, **extra}


def failed(code: int, message: str) -> dict:
    return {"status": "failed", "version": VERSION, "error": {"code": code, "message": message}}


_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _scalar(v) -> str:
    """XML 1.0 cannot carry control characters; tag data from Tidal/files can contain them."""
    return ("true" if v else "false") if isinstance(v, bool) else _XML_BAD.sub("", str(v))


def _fill(el: ET.Element, data: dict) -> None:
    """Subsonic JSON -> XML: lists become repeated children, dicts children, scalars attributes."""
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, list):
            for item in v:
                child = ET.SubElement(el, k)
                if isinstance(item, dict):
                    _fill(child, item)
                else:
                    child.text = _scalar(item)
        elif isinstance(v, dict):
            _fill(ET.SubElement(el, k), v)
        elif k == "value":
            el.text = _scalar(v)
        else:
            el.set(k, _scalar(v))


def render(fmt: str | None, payload: dict) -> Response:
    if fmt in ("json", "jsonp"):
        return JSONResponse({"subsonic-response": payload})
    root = ET.Element("subsonic-response", xmlns=NS)
    _fill(root, payload)
    return Response(ET.tostring(root, encoding="utf-8", xml_declaration=True), media_type="text/xml")
