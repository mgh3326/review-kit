"""Lay out review captures on a Miro board from a declarative list JSON."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import struct

from .miro import MiroError, encode_multipart
from .share import save_ledger


STATE_DIR_DEFAULT = Path.home() / ".local" / "state" / "review-kit"
KEY_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
TAG_ITEM = "rk:item:"
TAG_SOURCE = "rk:src:"
TAG_HEADER = "rk:hdr:"
FRAME_GAP = 80.0
DEFAULT_CELL = {"width": 320.0, "padding": 24.0, "header": 48.0}
DEFAULT_ASPECT = 4.0 / 3.0
_TYPE_PLURALS = {"image": "images", "text": "texts", "sticky_note": "sticky_notes",
                 "frame": "frames", "connector": "connectors", "shape": "shapes"}


# ---------------------------------------------------------------- spec parsing

def _require(mapping, name, what):
    value = mapping.get(name)
    if value is None or value == "":
        raise ValueError("Board list item is missing %s" % what)
    return value


def _parse_aspect(raw, item):
    try:
        if raw.get("aspect") is not None:
            item["aspect"] = float(raw["aspect"])
        elif raw.get("width") and raw.get("height"):
            item["aspect"] = float(raw["width"]) / float(raw["height"])
    except (TypeError, ValueError, ZeroDivisionError):
        raise ValueError("Item aspect or width/height must be positive numbers")
    if item.get("aspect") is not None and item["aspect"] <= 0:
        raise ValueError("Item aspect must be positive")


def _item_image(image, key, base_dir):
    if not isinstance(image, str):
        raise ValueError("Item image must be a path or URL string")
    if image.startswith(("https://", "http://")):
        return {"url": image}
    path = Path(image).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        raise ValueError("Item image file does not exist for key %s" % key)
    return {"file": path}


def _validate_item(raw, base_dir):
    if not isinstance(raw, dict):
        raise ValueError("Board list items must be objects")
    key = _require(raw, "key", "a 'key'")
    if not KEY_PART.fullmatch(key):
        raise ValueError("Item keys may use letters, digits, dot, underscore and dash")
    image = _require(raw, "image", "'image' for key %s" % key)
    item = {"key": key, "caption": str(raw.get("caption") or ""),
            "source": str(raw.get("source") or ""), "group": str(raw.get("group") or "")}
    item.update(_item_image(image, key, base_dir))
    _parse_aspect(raw, item)
    return item


def _parse_link_entry(entry):
    if isinstance(entry, dict):
        source, target, label = entry.get("from"), entry.get("to"), entry.get("label")
    elif isinstance(entry, (list, tuple)) and len(entry) in (2, 3):
        source, target = entry[0], entry[1]
        label = entry[2] if len(entry) == 3 else None
    else:
        raise ValueError("Board links must be {from, to} objects or pairs")
    for name, value in (("from", source), ("to", target)):
        if not isinstance(value, str) or not KEY_PART.fullmatch(value):
            raise ValueError("Board link %s must be an item key" % name)
    return {"from": source, "to": target,
            "label": None if label is None else str(label)}


def _parse_links(raw):
    return [_parse_link_entry(entry) for entry in (raw or [])]


def _parse_frame(raw):
    frame = raw if isinstance(raw, dict) else {}
    title = frame.get("title") or "Review board"
    position = frame.get("position", "auto:right")
    if isinstance(position, dict):
        try:
            x, y = float(position["x"]), float(position["y"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Frame position needs numeric x and y")
        return {"title": str(title), "mode": "fixed", "x": x, "y": y}
    if position in ("auto", "auto:right", "auto:below"):
        return {"title": str(title), "mode": position}
    raise ValueError("Frame position must be {x, y}, 'auto:right' or 'auto:below'")


def _parse_cell(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("cell must be an object of layout numbers")
    cell = {}
    for name, default in DEFAULT_CELL.items():
        try:
            cell[name] = float(raw.get(name, default))
        except (TypeError, ValueError):
            raise ValueError("cell.%s must be a number" % name)
        if cell[name] <= 0:
            raise ValueError("cell.%s must be positive" % name)
    return cell


def _parse_text_updates(raw):
    updates = raw or {}
    if isinstance(updates, list):
        updates = {_require(u, "id", "'id'"): _require(u, "content", "'content'")
                   for u in updates}
    if not isinstance(updates, dict):
        raise ValueError("text_updates must map widget ids to content")
    return {str(key): str(value) for key, value in updates.items()}


def load_spec(path):
    """Read and validate the board list JSON."""
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the board list JSON")
    if not isinstance(data, dict):
        raise ValueError("Board list must be a JSON object")
    try:
        columns = int(data.get("columns", 3))
    except (TypeError, ValueError):
        raise ValueError("columns must be an integer")
    if not 1 <= columns <= 20:
        raise ValueError("columns must be between 1 and 20")
    items = [_validate_item(raw, path.parent) for raw in (data.get("items") or [])]
    keys = [item["key"] for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError("Board list item keys must be unique")
    return {"frame": _parse_frame(data.get("frame")),
            "links": _parse_links(data.get("links")),
            "columns": columns, "cell": _parse_cell(data.get("cell")),
            "items": items, "text_updates": _parse_text_updates(
                data.get("text_updates"))}


# ------------------------------------------------------------------ image size

def image_size(path):
    """(width, height) from an image header, or None when unrecognized."""
    try:
        with open(path, "rb") as source:
            blob = source.read(262144)
    except OSError:
        return None
    if blob[:8] == b"\x89PNG\r\n\x1a\n" and len(blob) >= 24:
        return struct.unpack(">II", blob[16:24])
    if blob[:6] in (b"GIF87a", b"GIF89a") and len(blob) >= 10:
        return struct.unpack("<HH", blob[6:10])
    if blob[:2] == b"BM" and len(blob) >= 26:
        return struct.unpack("<iI", blob[18:22])[0], struct.unpack("<iI", blob[22:26])[0]
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP" and len(blob) >= 30:
        return _webp_size(blob)
    if blob[:2] == b"\xff\xd8":
        return _jpeg_size(blob)
    return None


def _webp_size(blob):
    kind = blob[12:16]
    if kind == b"VP8X" and len(blob) >= 30:
        width = 1 + int.from_bytes(blob[24:27], "little")
        height = 1 + int.from_bytes(blob[27:30], "little")
        return width, height
    if kind == b"VP8 " and len(blob) >= 30:
        index = blob.find(b"\x9d\x01\x2a", 20)
        if index >= 0 and len(blob) >= index + 7:
            width, height = struct.unpack("<HH", blob[index + 3:index + 7])
            return width & 0x3FFF, height & 0x3FFF
    return None


def _jpeg_size(blob):
    offset = 2
    while offset + 9 < len(blob):
        if blob[offset] != 0xFF:
            offset += 1
            continue
        marker = blob[offset + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", blob[offset + 5:offset + 9])
            return width, height
        length = struct.unpack(">H", blob[offset + 2:offset + 4])[0]
        if length < 2:
            return None
        offset += 2 + length
    return None


# ---------------------------------------------------------------------- layout

def _item_aspect(item):
    if item.get("aspect"):
        return item["aspect"]
    if item.get("file"):
        size = image_size(item["file"])
        if size and size[0] and size[1]:
            return size[0] / size[1]
    return DEFAULT_ASPECT


def plan_layout(spec):
    """Frame size and per-widget geometry, relative to the frame top-left."""
    cell_w, pad, header_h = (spec["cell"]["width"], spec["cell"]["padding"],
                             spec["cell"]["header"])
    columns = spec["columns"]
    frame_w = pad + columns * (cell_w + pad)
    placements, headers = {}, []
    groups = {}
    for item in spec["items"]:
        groups.setdefault(item["group"], []).append(item)
    y = pad
    for group_name, members in groups.items():
        if group_name:
            headers.append({"label": group_name, "x": frame_w / 2,
                            "y": y + header_h / 2, "w": frame_w - 2 * pad,
                            "h": header_h})
            y += header_h
        for start in range(0, len(members), columns):
            row = members[start:start + columns]
            heights = [cell_w / _item_aspect(item) for item in row]
            row_h = max(heights)
            for col, (item, height) in enumerate(zip(row, heights)):
                x = pad + col * (cell_w + pad) + cell_w / 2
                placements[item["key"]] = {"x": x, "y": y + row_h / 2,
                                           "w": cell_w, "h": height}
            y += row_h + pad
    return {"width": frame_w, "height": max(y, 2 * pad),
            "placements": placements, "headers": headers}


def _frame_bounds(item):
    position = item.get("position") or {}
    geometry = item.get("geometry") or {}
    try:
        x, y = float(position.get("x", 0)), float(position.get("y", 0))
        w, h = float(geometry.get("width", 0)), float(geometry.get("height", 0))
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)
    return (x - w / 2, y - h / 2, x + w / 2, y + h / 2)


def frame_center(mode, x, y, frames, width, height):
    """Center coordinates for the new frame, never overlapping existing frames."""
    if mode == "fixed":
        return x, y
    bounds = [(item, _frame_bounds(item)) for item in frames]
    if not bounds:
        return 0.0, 0.0
    if mode == "auto:below":
        lowest = max(bounds, key=lambda pair: pair[1][3])
        position = lowest[0].get("position") or {}
        return float(position.get("x", 0)), lowest[1][3] + FRAME_GAP + height / 2
    rightmost = max(bounds, key=lambda pair: pair[1][2])
    position = rightmost[0].get("position") or {}
    return rightmost[1][2] + FRAME_GAP + width / 2, float(position.get("y", 0))


# ---------------------------------------------------------------------- ledger

def board_ledger_path(config, board_id):
    if config and config.get("BOARD_LEDGER_DIR"):
        base = Path(config["BOARD_LEDGER_DIR"]).expanduser()
    else:
        root = os.environ.get("REVIEW_KIT_STATE_DIR")
        base = Path(root).expanduser() if root else STATE_DIR_DEFAULT
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(board_id))
    return base / ("board-%s.json" % safe)


def load_board_ledger(path):
    path = Path(path)
    if not path.exists():
        return {"schema_version": 1, "frame_id": None, "widgets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("Could not read the board ledger")
    if not isinstance(data, dict) or not isinstance(data.get("widgets", {}), dict):
        raise ValueError("Board ledger has an unsupported format")
    data.setdefault("schema_version", 1)
    data.setdefault("frame_id", None)
    data.setdefault("widgets", {})
    return data


def save_board_ledger(path, ledger):
    save_ledger(path, ledger)


# ---------------------------------------------------------------------- widgets

def _item_tag(key):
    return TAG_ITEM + key


def _header_tag(group):
    return TAG_HEADER + group


def _description(item):
    parts = [_item_tag(item["key"])]
    if item["source"]:
        parts.append(TAG_SOURCE + item["source"])
    return " ".join(parts)


_TAG_VALUE = r"([^\s<]+)"


def _extract(pattern, item):
    data = (item or {}).get("data") or {}
    for field in ("description", "title", "content"):
        match = re.search(pattern, str(data.get(field) or ""))
        if match:
            return match.group(1)
    return None


def _item_key(item):
    key = _extract(re.escape(TAG_ITEM) + _TAG_VALUE, item)
    if key:
        return key
    header = _extract(re.escape(TAG_HEADER) + _TAG_VALUE, item)
    return ("hdr:" + header) if header else None


def _item_source(item):
    return _extract(re.escape(TAG_SOURCE) + _TAG_VALUE, item)


def _strip_html(content):
    text = re.sub(r"<!--.*?-->", "", str(content or ""), flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _html(content):
    text = str(content)
    if "<" not in text and ">" not in text:
        return "<p>%s</p>" % text
    return text


def _upsert(client, board_id, widgets, tag, kind, update_payload, create_call):
    """PATCH the recorded widget, recreating it after a 404."""
    entry = widgets.get(tag) or {}
    widget_id = entry.get("id")
    if widget_id:
        try:
            client.update_item(board_id, widget_id, update_payload)
            return widget_id, "updated"
        except MiroError as exc:
            if exc.status != 404:
                raise
    created = create_call()
    widget_id = created.get("id")
    if not widget_id:
        raise MiroError("Miro did not return a widget id")
    widgets[tag] = {"id": widget_id, "type": created.get("type", kind.rstrip("s"))}
    return widget_id, "created"


def _image_create_call(client, board_id, item, payload):
    if item.get("url"):
        data = dict(payload["data"])
        data["url"] = item["url"]
        create_payload = {key: value for key, value in payload.items()
                          if key != "data"}
        create_payload["data"] = data
        return lambda: client.create(board_id, "images",
                                     payload=create_payload)
    path = item["file"]

    def upload():
        body = encode_multipart(
            [("data", json.dumps(payload, ensure_ascii=False))],
            "resource", path.name, path.read_bytes(),
            _content_type(path))
        return client.create(board_id, "images", multipart=body)
    return upload


def _content_type(path):
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".bmp": "image/bmp", ".webp": "image/webp"
            }.get(Path(path).suffix.lower(), "application/octet-stream")


def _link_tag(link):
    return "lnk:%s->%s" % (link["from"], link["to"])


def _expected_tags(spec):
    tags = {_item_tag(item["key"]) for item in spec["items"]}
    tags.update(_header_tag(item["group"]) for item in spec["items"] if item["group"])
    tags.update(_link_tag(link) for link in spec["links"])
    return tags


def _board_plan(result, action, detail):
    result["plan"].append("%s %s" % (action, detail))


def _place_frame(client, board_id, spec, frames, ledger, result):
    layout = plan_layout(spec)
    fx, fy = frame_center(spec["frame"]["mode"], spec["frame"].get("x", 0.0),
                          spec["frame"].get("y", 0.0), frames,
                          layout["width"], layout["height"])
    payload = {"data": {"title": spec["frame"]["title"]},
               "position": {"x": fx, "y": fy},
               "geometry": {"width": layout["width"], "height": layout["height"]}}
    if result["dry_run"]:
        _board_plan(result, "frame", "%s at (%.0f, %.0f) %.0fx%.0f"
                    % (spec["frame"]["title"], fx, fy,
                       layout["width"], layout["height"]))
    else:
        widget_id, action = _upsert(
            client, board_id, ledger["widgets"], "frame", "frames", payload,
            lambda: client.create(board_id, "frames", payload=payload))
        ledger["frame_id"] = widget_id
        _board_plan(result, action, "frame %s" % spec["frame"]["title"])
    return layout, fx, fy


def _place_widgets(client, board_id, spec, layout, fx, fy, ledger, result):
    origin_x, origin_y = fx - layout["width"] / 2, fy - layout["height"] / 2
    widgets = ledger["widgets"]
    frame_id = ledger.get("frame_id")
    parent = {"parent": {"id": frame_id}} if frame_id else {}
    for header in layout["headers"]:
        content = "<p><strong>%s</strong></p><!-- %s -->" % (
            header["label"], _header_tag(header["label"]))
        payload = {"data": {"content": content},
                   "position": {"x": origin_x + header["x"], "y": origin_y + header["y"]},
                   "geometry": {"width": header["w"]}, **parent}
        tag = _header_tag(header["label"])
        if result["dry_run"]:
            _board_plan(result, "header", '"%s"' % header["label"])
            continue
        _upsert(client, board_id, widgets, tag, "texts", payload,
                lambda p=payload: client.create(board_id, "texts", payload=p))
    for item in spec["items"]:
        cell = layout["placements"][item["key"]]
        position = {"x": origin_x + cell["x"], "y": origin_y + cell["y"]}
        geometry = {"width": cell["w"], "height": cell["h"]}
        payload = {"data": {"title": item["caption"], "description": _description(item)},
                   "position": position, "geometry": geometry, **parent}
        tag = _item_tag(item["key"])
        if result["dry_run"]:
            _board_plan(result, "item", "%s (%s)" % (item["key"],
                                                   item.get("url") or item["file"].name))
            continue
        create = _image_create_call(client, board_id, item, payload)
        widget_id, action = _upsert(client, board_id, widgets, tag, "images",
                                    payload, create)
        item["_widget_id"] = widget_id
        _board_plan(result, action, "item %s" % item["key"])


def _place_links(client, board_id, spec, ledger, result):
    widgets = ledger["widgets"]
    spec_keys = {item["key"] for item in spec["items"]}
    key_ids = {item["key"]: (widgets.get(_item_tag(item["key"])) or {}).get("id")
               for item in spec["items"]}
    for link in spec["links"]:
        if link["from"] not in spec_keys or link["to"] not in spec_keys:
            raise ValueError("Link %s->%s refers to a key missing from items"
                             % (link["from"], link["to"]))
        tag = _link_tag(link)
        if result["dry_run"]:
            _board_plan(result, "link", "%s -> %s" % (link["from"], link["to"]))
            continue
        source, target = key_ids.get(link["from"]), key_ids.get(link["to"])
        if not source or not target:
            raise MiroError("Link %s->%s refers to a missing item"
                            % (link["from"], link["to"]))
        if (widgets.get(tag) or {}).get("id"):
            continue
        payload = {"startItem": {"id": source}, "endItem": {"id": target}}
        if link["label"]:
            payload["captions"] = [{"content": _html(link["label"])}]
        created = client.create(board_id, "connectors", payload=payload)
        widgets[tag] = {"id": created.get("id"), "type": "connector"}
        _board_plan(result, "created", "link %s -> %s" % (link["from"], link["to"]))


def _place_text_updates(client, board_id, spec, result):
    for widget_id, content in spec["text_updates"].items():
        if result["dry_run"]:
            _board_plan(result, "text-update", widget_id)
            continue
        client.update_item(board_id, widget_id,
                           {"data": {"content": _html(content)}})
        _board_plan(result, "updated", "text %s" % widget_id)


def _place_prune(client, board_id, spec, ledger, result):
    if not result["prune"]:
        return
    expected = _expected_tags(spec) | {"frame"}
    widgets = ledger["widgets"]
    for tag in sorted(set(widgets) - expected):
        entry = widgets[tag] or {}
        if result["dry_run"]:
            _board_plan(result, "prune", tag)
            continue
        if entry.get("id"):
            try:
                client.delete_item(board_id, entry["id"])
            except MiroError as exc:
                if exc.status != 404:
                    raise
        del widgets[tag]
        _board_plan(result, "pruned", tag)


def place_board(client, board_id, spec_path, config=None, prune=False,
                dry_run=False, state_path=None):
    """Create or update the frame, captures, headers, links and text updates."""
    spec = load_spec(spec_path)
    result = {"dry_run": dry_run, "prune": prune, "plan": []}
    path = state_path or board_ledger_path(config or {}, board_id)
    ledger = load_board_ledger(path)
    known_frames = [item for item in client.items(board_id)
                    if item.get("type") == "frame"]
    layout, fx, fy = _place_frame(client, board_id, spec, known_frames,
                                  ledger, result)
    _place_widgets(client, board_id, spec, layout, fx, fy, ledger, result)
    _place_links(client, board_id, spec, ledger, result)
    _place_text_updates(client, board_id, spec, result)
    _place_prune(client, board_id, spec, ledger, result)
    if not dry_run:
        ledger["board"] = board_id
        ledger["updated_utc"] = dt.datetime.now(dt.timezone.utc).isoformat(
            timespec="seconds")
        save_board_ledger(path, ledger)
        result["ledger_path"] = path
    return result


# ------------------------------------------------------------------- inventory

def _connector_ends(item):
    data = item.get("data") or {}
    start = data.get("startItem") or item.get("startItem") or {}
    end = data.get("endItem") or item.get("endItem") or {}
    return start.get("id"), end.get("id")


def _inventory_rows(items):
    by_id = {item.get("id"): item for item in items}
    links_out, links_in = {}, {}
    for item in items:
        if item.get("type") != "connector":
            continue
        start, end = _connector_ends(item)
        links_out.setdefault(start, []).append(end)
        links_in.setdefault(end, []).append(start)

    def label(widget_id):
        return _item_key(by_id.get(widget_id)) or widget_id or "?"

    rows = []
    for item in sorted(items, key=lambda i: ((i.get("position") or {}).get("y", 0),
                                             (i.get("position") or {}).get("x", 0))):
        if item.get("type") == "connector":
            continue
        data = item.get("data") or {}
        caption = data.get("title") or _strip_html(data.get("content")) or "-"
        rows.append({"key": _item_key(item) or "-", "id": item.get("id"),
                     "type": item.get("type"), "caption": caption,
                     "source": _item_source(item) or "-",
                     "links_out": [label(i) for i in links_out.get(item.get("id"), [])],
                     "links_in": [label(i) for i in links_in.get(item.get("id"), [])]})
    return rows


def inventory_board(client, board_id, frame_id=None):
    """Rows for the markdown table, plus the raw item data for --json."""
    items = client.items(board_id)
    connectors = client.connectors(board_id)
    if frame_id:
        items = [item for item in items
                 if item.get("id") == frame_id
                 or (item.get("parent") or {}).get("id") == frame_id
                 or (item.get("parent") == frame_id)]
        kept = {item.get("id") for item in items}
        connectors = [c for c in connectors
                      if set(_connector_ends(c)) & kept]
    seen = {item.get("id") for item in items}
    rows_items = items + [c for c in connectors if c.get("id") not in seen]
    return _inventory_rows(rows_items), items


def inventory_markdown(rows):
    lines = ["| key | widget id | type | caption | links out | links in | source |",
             "|---|---|---|---|---|---|---|"]
    for row in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            row["key"], row["id"], row["type"],
            str(row["caption"]).replace("|", "\\|"),
            ", ".join(row["links_out"]) or "-",
            ", ".join(row["links_in"]) or "-",
            row["source"]))
    return "\n".join(lines)


COMMENTS_UNAVAILABLE = ("Comments are not available via the Miro REST API; "
                        "use the Miro MCP comment tools or the board UI.")
