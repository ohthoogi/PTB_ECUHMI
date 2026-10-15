"""Lossless, local XPD source workspace. Python 3.9+, no dependencies.

This is a preservation-based editor, NOT a complete XP-Builder schema.
Observed script bodies and screen CString fields are editable.
"""
import argparse
import difflib
import hashlib
import json
import re
import shutil
import tempfile
from datetime import datetime
from uuid import uuid4
from urllib.parse import quote
from pathlib import Path, PurePosixPath
import struct
import sys
import unicodedata

VERSION = "0.3.1"
MARKER = b"\xff\xfe\xff"
SCRIPT_PREFIX = bytes.fromhex("0c000000000000006500")
SCRIPT_PREFIXES = (SCRIPT_PREFIX, bytes.fromhex("0c000000010000006500"))
FORMAT = "xpd-source/v3"
MAX_FILE = 128 * 1024 * 1024


class XpdError(Exception):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    if path.stat().st_size > MAX_FILE:
        raise XpdError("File exceeds 128 MiB limit: " + str(path))
    return path.read_bytes()


def json_bytes(obj):
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def read_json(path):
    return json.loads(read_bytes(path).decode("utf-8-sig"))


def safe_path(root, name):
    if not isinstance(name, str):
        raise XpdError("Invalid source path")
    rel = PurePosixPath(name)
    if rel.is_absolute() or any(x in name for x in ("\\", ":")) or ".." in rel.parts:
        raise XpdError("Unsafe source path: " + name)
    p = (root / name).resolve()
    if root.resolve() not in p.parents:
        raise XpdError("Source path escapes workspace: " + name)
    return p


def parse_string(data, offset):
    """Read MFC Unicode CString framing, including extended lengths."""
    if data[offset:offset+3] != MARKER:
        raise XpdError("Missing CString marker")
    try:
        n = data[offset+3]
        pos = offset + 4
        if n == 0xff:
            n = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if n == 0xffff:
                n = struct.unpack_from("<I", data, pos)[0]
                pos += 4
        end = pos + 2*n
        if end > len(data):
            raise XpdError("Truncated CString")
        text = data[pos:end].decode("utf-16le", errors="strict")
        return end, text, data[offset:pos]
    except (IndexError, struct.error, UnicodeError) as exc:
        raise XpdError("Invalid CString") from exc


def encode_string(text):
    payload = text.encode("utf-16le", errors="strict")
    n = len(payload) // 2  # UTF-16 code units, NOT Python character count.
    if n < 255:
        prefix = bytes([n])
    elif n < 65535:
        prefix = b"\xff" + struct.pack("<H", n)
    else:
        prefix = b"\xff\xff\xff" + struct.pack("<I", n)
    return MARKER + prefix + payload


def readable(text):
    return all(c in "\r\n\t" or not unicodedata.category(c).startswith("C") for c in text)


def scan(data):
    """Candidates, not a semantic screen parser. Never scan inside accepted strings."""
    records = []
    pos = 0
    while True:
        at = data.find(MARKER, pos)
        if at < 0:
            break
        pos = at+3
        try:
            end, text, prefix = parse_string(data, at)
        except XpdError:
            continue
        if not readable(text):
            continue
        pos = end
        # Empty values stay opaque; no semantic meaning can be assigned to them.
        if text:
            records.append({"start": at, "end": end, "text": text,
                            "prefix": prefix.hex(), "kind": "candidate_text"})
    # Recognize only the observed named script record. Keep all metadata opaque.
    by_start = {r["start"]: r for r in records}
    ids = set()
    for r in records:
        at = r["start"]
        if at < 14 or data[at-14:at-4] not in SCRIPT_PREFIXES:
            continue
        body = by_start.get(r["end"])
        if body is None:
            # Empty script bodies are explicitly recognizable at this boundary.
            try:
                end, text, prefix = parse_string(data, r["end"])
            except XpdError:
                continue
            if text != "":
                continue
            body = {"start": r["end"], "end": end, "text": "",
                    "prefix": prefix.hex(), "kind": "candidate_text"}
            by_start[body["start"]] = body
        script_id = struct.unpack_from("<I", data, at-4)[0]
        if script_id > 65535 or script_id in ids:
            raise XpdError("Ambiguous script ID; this XPD layout is not supported")
        ids.add(script_id)
        r["kind"] = "script_name"
        body.update(kind="script", script_id=script_id, script_name=r["text"])
    return sorted(by_start.values(), key=lambda r: r["start"])


def header_check(data):
    # This intentionally does not claim support for every .xpd version.
    if len(data) < 256 or "LSIS_XPBUILDER".encode("utf-16le") not in data[:4096]:
        raise XpdError("Unrecognized XPD header; expected LSIS_XPBUILDER")


def sections(data):
    """Observed top-level TLV chain: 4-byte header, then uint32 ID/byte-size."""
    header_check(data)
    result = []
    pos = 4
    expected = 0
    while pos < len(data):
        if pos+8 > len(data):
            raise XpdError("Truncated section header")
        sid, size = struct.unpack_from("<II", data, pos)
        end = pos+8+size
        if sid != expected or end > len(data):
            raise XpdError("Unsupported or corrupt top-level section layout")
        result.append((sid, pos+8, end))
        pos = end
        expected += 1
    if len(result) != 17:
        raise XpdError("Only the observed 17-section XPD layout is supported")
    return result


def screen_blocks(data, section_list):
    """Screen containers observed in section 3; no object-field schema assumed."""
    _, pos, limit = section_list[3]
    result = []
    seen = set()
    while pos < limit:
        if pos+12 > limit:
            raise XpdError("Truncated screen header")
        packed, size = struct.unpack_from("<II", data, pos)
        end = pos+8+size
        kind = (packed >> 16) & 255
        if packed >> 24 != 255 or kind not in (0,1,2) or end > limit or size < 8:
            raise XpdError("Unsupported screen container layout")
        prefix = "BWP"[kind]
        number = packed & 65535
        name_end, name, _ = parse_string(data, pos+12)
        if name != "{}-{}".format(prefix, number):
            raise XpdError("Screen header/name mismatch")
        key = "{}-{:04d}".format(prefix, number)
        if key in seen:
            raise XpdError("Duplicate screen ID")
        seen.add(key)
        title = ''
        # Observed title header repeats screen kind and number. Do not guess
        # from the first caption when this exact layout is absent.
        if data[name_end:name_end+8] == struct.pack('<II', 0x0e740000 | kind, number):
            try:
                title_end, candidate, _ = parse_string(data, name_end+8)
                if title_end <= end and readable(candidate):
                    title = candidate
            except XpdError:
                pass
        result.append({"start": pos+8, "end": end, "id": packed, "key": key, "title": title})
        pos = end
    return result


def write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as f:
        f.write(data)


def script_paths(scripts):
    """Keep display names; disambiguate names unsuitable for Windows files."""
    result, used = {}, set()
    reserved = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
    reserved.update(prefix + str(i) for prefix in ('COM', 'LPT') for i in range(1, 10))
    reserved.update(prefix + i for prefix in ('COM', 'LPT') for i in '¹²³')
    for record in sorted(scripts, key=lambda r: r['script_id']):
        sid = record['script_id']
        stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', record['script_name']).strip().rstrip('.')
        if not stem:
            stem = 'script'
        if stem.split('.')[0].upper() in reserved:
            stem = '_' + stem
        stem = stem[:100].rstrip(' .')
        name, suffix = stem, 0
        while unicodedata.normalize('NFC', name).casefold() in used:
            suffix += 1
            name = '{}__{}{}'.format(stem, sid, '' if suffix == 1 else '_'+str(suffix))
        used.add(unicodedata.normalize('NFC', name).casefold())
        result[sid] = 'scripts/' + name + '.txt'
    return result


def screen_paths(screens):
    stems = {}
    reserved = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$'}
    reserved.update(p+str(i) for p in ('com', 'lpt') for i in range(1, 10))
    reserved.update(p+i for p in ('com', 'lpt') for i in '¹²³')
    keys = {s['key'].casefold() for s in screens}
    for screen in screens:
        stem = unicodedata.normalize('NFC', screen.get('title', '')).strip().lower()
        stem = re.sub(r'\s+', '_', stem)
        if (not stem or len(stem) > 100 or stem.endswith('.') or
                re.search(r'[<>:"/\\|?*\x00-\x1f]', stem) or
                stem.split('.')[0] in reserved or stem.casefold() in keys):
            stem = ''
        stems[screen['key']] = stem
    return {key: 'screens/' + (stem if stem and
            sum(s.casefold() == stem.casefold() for s in stems.values()) == 1 else key) + '.json'
            for key, stem in stems.items()}


def export_project(input_path, dest):
    data = read_bytes(input_path)
    header_check(data)
    if dest.exists():
        raise XpdError("Destination already exists; export into a new directory")
    section_list = sections(data)
    screens = screen_blocks(data, section_list)
    screen_files = screen_paths(screens)
    records = []
    for sid, start, end in section_list:
        for r in scan(data[start:end]):
            r["start"] += start
            r["end"] += start
            if r["kind"] == "script" and sid != 7:
                raise XpdError("Named script record outside observed script section")
            r["section_id"] = sid
            if sid == 3:
                screen = next((s for s in screens if s["start"] <= r["start"] and r["end"] <= s["end"]), None)
                if screen is None:
                    raise XpdError("String crosses a screen boundary")
                r["screen_key"] = screen["key"]
                r["kind"] = "screen_name" if r["start"] == screen["start"]+4 else "screen_text"
            records.append(r)
    scripts = [r for r in records if r["kind"] == "script"]
    paths = script_paths(scripts)
    if not scripts:
        raise XpdError("No supported named script records found; refusing unknown layout")
    dest.mkdir(parents=True)
    manifest = {"format": FORMAT, "tool_version": VERSION,
                "source_name": input_path.name, "baseline_sha256": sha(data),
                "baseline_size": len(data), "opaque_file": "preserved/opaque.hex",
                "opaque_sha256": "", "script_layout": "observed-0c-65-v1",
                "segments": []}
    opaque = bytearray()
    groups = {}
    pos = 4
    opaque.extend(data[:4])
    manifest["segments"].append({"kind": "opaque", "size": 4})
    text_id = 0
    screen_text_ids = {}
    # Interleave explicit section boundaries and string records. Section byte
    # lengths MUST be recomputed when a script body changes length.
    events = []
    for sid, start, end in section_list:
        events.append((start-8, 2, {"event": "section", "section_id": sid}))
        events.append((end, 1, {"event": "end_section"}))
    for screen in screens:
        events.append((screen["start"]-8, 3, {"event": "screen", "id": screen["id"], "key": screen["key"]}))
        events.append((screen["end"], 0, {"event": "end_screen"}))
    events.extend((r["start"], 4, r) for r in records)
    for offset, _, r in sorted(events, key=lambda event: event[:2]):
        if "event" in r:
            gap = data[pos:offset]
            if gap:
                manifest["segments"].append({"kind": "opaque", "size": len(gap)})
                opaque.extend(gap)
            if r["event"] == "section":
                manifest["segments"].append({"kind": "section", "id": r["section_id"]})
                pos = offset+8
            elif r["event"] == "screen":
                manifest["segments"].append({"kind": "screen", "id": r["id"], "key": r["key"]})
                pos = offset+8
            else:
                manifest["segments"].append({"kind": r["event"]})
                pos = offset
            continue
        gap = data[pos:r["start"]]
        if gap:
            manifest["segments"].append({"kind": "opaque", "size": len(gap)})
            opaque.extend(gap)
        original = r["text"]
        # Store original header to retain unusual/noncanonical length encodings.
        item = {"kind": r["kind"], "section_id": r["section_id"], "prefix": r["prefix"],
                "original_sha256": sha(original.encode("utf-8"))}
        if r["kind"] == "script":
            name = paths[r["script_id"]]
            item.update(path=name, script_id=r["script_id"], name=r["script_name"])
            write_new(dest/name, original.encode("utf-8"))
        elif r["kind"] in ("screen_name", "screen_text"):
            screen_key = r["screen_key"]
            name = screen_files[screen_key]
            index = screen_text_ids.get(screen_key, 0)
            key = "t{:06d}".format(index)
            screen_text_ids[screen_key] = index+1
            item.update(path=name, key=key, screen=screen_key)
            groups.setdefault(name, {})[key] = original
        else:
            name = "texts/strings_{:04d}.json".format(text_id//100)
            key = "t{:06d}".format(text_id)
            item.update(path=name, key=key)
            groups.setdefault(name, {})[key] = original
            text_id += 1
        manifest["segments"].append(item)
        pos = r["end"]
    gap = data[pos:]
    if gap:
        manifest["segments"].append({"kind": "opaque", "size": len(gap)})
        opaque.extend(gap)
    manifest["opaque_sha256"] = sha(opaque)
    # Hex is preservation data, not editable screen source. No original XPD needed.
    hex_text = "\n".join(opaque[i:i+32].hex() for i in range(0, len(opaque), 32))+"\n"
    write_new(dest/manifest["opaque_file"], hex_text.encode("ascii"))
    for name, values in groups.items():
        write_new(dest/name, json_bytes(values))
    manifest["layout_sha256"] = sha(json_bytes(manifest))
    write_new(dest/"project.json", json_bytes(manifest))
    write_new(dest/".gitattributes", b"* text=auto\n*.json text eol=lf\n*.txt -text diff\n*.hex text eol=lf\n*.xpd binary\n")
    write_new(dest/".gitignore", b"build/\n*.bak\n*.OSTS\n")
    write_new(dest/"SOURCE_README.md", SOURCE_README.encode("utf-8"))
    index = "# 스크립트 목록\n\n파일 이름은 스크립트 이름입니다. 내부 ID는 project.json에 보존합니다.\n\n| ID | 이름 | 파일 |\n|---|---|---|\n"
    for r in sorted(scripts, key=lambda s: s["script_id"]):
        sid = r["script_id"]
        label = r["script_name"].replace("|", "\\|").replace("\n", " ")
        index += "| {} | {} | [{}]({}) |\n".format(sid, label, paths[sid], quote(paths[sid]))
    write_new(dest/"SCRIPTS.md", index.encode("utf-8"))
    index = '# 화면 목록\n\n내부 화면 ID는 파일 이름과 별개로 project.json에 보존합니다.\n\n| ID | 화면 제목 | JSON |\n|---|---|---|\n'
    for screen in sorted(screens, key=lambda s: s['key']):
        path = screen_files[screen['key']]
        title = screen['title'].replace('|', '\\|').replace('\n', ' ')
        index += '| {} | {} | [{}]({}) |\n'.format(screen['key'], title or '(미확인)', path, quote(path))
    write_new(dest/'SCREENS.md', index.encode('utf-8'))
    # Mandatory proof of exact reconstruction before reporting a successful export.
    rebuilt, report = assemble(dest)
    if rebuilt != data:
        raise XpdError("Internal error: export failed byte-identical round trip")
    return {"workspace": str(dest), "scripts": len(scripts), "screens": len(screens),
            "screen_strings": sum(screen_text_ids.values()),
            "candidate_texts": text_id, "baseline_bytes": len(data),
            "preserved_bytes": len(opaque), "roundtrip_identical": True,
            "sha256": sha(data)}


def load_sources(root):
    manifest = read_json(root/"project.json")
    if manifest.get("format") != FORMAT:
        raise XpdError("Unsupported workspace format")
    seal = manifest.pop("layout_sha256", None)
    if sha(json_bytes(manifest)) != seal:
        raise XpdError("project.json was modified; layout metadata is read-only")
    opaque = bytes.fromhex(read_bytes(safe_path(root, manifest["opaque_file"])).decode("ascii"))
    if sha(opaque) != manifest["opaque_sha256"]:
        raise XpdError("Preserved bytes changed; opaque.hex must not be edited")
    sources, expected = {}, {}
    for item in manifest["segments"]:
        if item["kind"] in ("opaque", "section", "end_section", "screen", "end_screen"):
            continue
        name = item["path"]
        if name not in sources:
            p = safe_path(root, name)
            if item["kind"] == "script":
                # Read bytes to preserve CR/LF. Do not silently normalize source.
                sources[name] = read_bytes(p).decode("utf-8")
            else:
                sources[name] = read_json(p)
        if item["kind"] != "script":
            keys = expected.setdefault(name, set())
            if item["key"] in keys:
                raise XpdError("Duplicate text ID")
            keys.add(item["key"])
    for name, keys in expected.items():
        if not isinstance(sources[name], dict) or set(sources[name]) != keys:
            raise XpdError("Text IDs added/removed in " + name)
    referenced = set(sources)
    # Do not silently ignore new files the user expects to be compiled.
    for directory in ("scripts", "texts", "screens"):
        for p in (root/directory).rglob("*"):
            if p.is_file() and p.relative_to(root).as_posix() not in referenced:
                raise XpdError("Unregistered source file: " + str(p))
    return manifest, opaque, sources


def assemble(root, allow_unverified_text=False):
    manifest, opaque, sources = load_sources(root)
    result = bytearray()
    at = 0
    changes = []
    seen_scripts = set()
    changed_any = False
    frames = []
    # Original texts are deliberately NOT duplicated. The baseline checksum proves
    # unmodified workspaces, while per-record hashes identify edits.
    for item in manifest["segments"]:
        kind = item["kind"]
        if kind in ("section", "screen"):
            if (kind == "section" and frames) or (kind == "screen" and len(frames) != 1):
                raise XpdError("Invalid container nesting")
            frames.append((kind, len(result)))
            result.extend(struct.pack("<II", item["id"], 0))
            continue
        if kind in ("end_section", "end_screen"):
            if not frames or "end_"+frames[-1][0] != kind:
                raise XpdError("Unmatched container end")
            _, frame_start = frames.pop()
            struct.pack_into("<I", result, frame_start+4, len(result)-frame_start-8)
            continue
        if kind == "opaque":
            size = item["size"]
            if type(size) is not int or size <= 0 or at+size > len(opaque):
                raise XpdError("Invalid opaque segment length")
            result.extend(opaque[at:at+size])
            at += size
            continue
        if kind not in ("script", "script_name", "candidate_text", "screen_name", "screen_text"):
            raise XpdError("Unknown segment type")
        name = item["path"]
        value = sources[name] if kind == "script" else sources[name][item["key"]]
        if not isinstance(value, str) or not readable(value):
            raise XpdError("Invalid text/control character: " + name)
        changed = sha(value.encode("utf-8")) != item["original_sha256"]
        if kind == "script":
            if item["script_id"] in seen_scripts:
                raise XpdError("Duplicate script ID")
            seen_scripts.add(item["script_id"])
        if changed:
            changed_any = True
            if kind in ("script_name", "screen_name"):
                raise XpdError("Renaming script/screen records is not supported: " + name)
            if kind == "candidate_text" and not allow_unverified_text:
                raise XpdError("Unverified text edited: " + name + ":" + item["key"] +
                               "; use --allow-unverified-text only for experimental validation")
            payload = encode_string(value)
            changes.append({"path": name, "key": item.get("key"),
                            "kind": kind, "script_id": item.get("script_id"),
                            "name": item.get("name"), "screen": item.get("screen"),
                            "new_utf16_units": len(value.encode("utf-16le"))//2})
        else:
            payload = bytes.fromhex(item["prefix"]) + value.encode("utf-16le")
            end, decoded, _ = parse_string(payload, 0)
            if end != len(payload) or decoded != value:
                raise XpdError("Invalid saved CString framing")
        result.extend(payload)
    if at != len(opaque):
        raise XpdError("Unused opaque data")
    if frames:
        raise XpdError("Unclosed container")
    if not changed_any and (sha(result) != manifest["baseline_sha256"] or
                            len(result) != manifest["baseline_size"]):
        raise XpdError("Manifest/layout changed: baseline checksum mismatch")
    screen_blocks(result, sections(result))
    # Re-read script identities and content at their newly shifted positions.
    actual = {r["script_id"]: r["text"] for r in scan(result) if r["kind"] == "script"}
    wanted = {item["script_id"]: sources[item["path"]]
              for item in manifest["segments"] if item["kind"] == "script"}
    if actual != wanted:
        raise XpdError("Rebuilt script records failed structural verification")
    return bytes(result), {"changed_records": len(changes), "changes": changes,
                           "bytes": len(result), "sha256": sha(result),
                           "baseline_identical": sha(result) == manifest["baseline_sha256"],
                           "xp_builder_validated": False,
                           "notice": "Binary reconstruction checked; validate edited XPD in XP-Builder before use."}


def build_project(root, output, allow_unverified_text=False):
    data, report = assemble(root, allow_unverified_text)
    # No overwrite: accidental replacement of the user's original is impossible.
    if output.exists() or output.with_suffix(output.suffix+".report.json").exists():
        raise XpdError("Output or report already exists; choose a new output filename")
    if root.resolve() in output.resolve().parents and "build" not in output.relative_to(root).parts:
        raise XpdError("Inside a workspace, outputs must be in build/")
    write_new(output, data)
    write_new(output.with_suffix(output.suffix+".report.json"), json_bytes(report))
    return report


def sync_project(input_path, root):
    """Replace managed sources from an XPD after validation and local backup.

    This is a one-way refresh, not a merge. Keep build/, Git and user notes.
    """
    root = root.resolve()
    if not root.exists():
        return export_project(input_path, root)
    manifest, _, _ = load_sources(root)
    def managed(m):
        return {i['path'] for i in m['segments'] if 'path' in i} | {
            m['opaque_file'], 'project.json', 'SCRIPTS.md', 'SCREENS.md', 'SOURCE_README.md'}
    old_names = managed(manifest)
    build = safe_path(root, 'build')
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='sync-stage-', dir=build) as temporary:
        stage = Path(temporary)/'source'
        exported = export_project(input_path, stage)
        incoming = read_json(stage/'project.json')
        new_names = managed(incoming)
        paths = {name: safe_path(root, name) for name in old_names | new_names}
        for name in new_names - old_names:
            if paths[name].exists():
                raise XpdError('Sync would replace an unregistered file: ' + name)
        snapshot = {name: read_bytes(paths[name]) for name in old_names if paths[name].exists()}
        fresh = {name: read_bytes(safe_path(stage, name)) for name in new_names}
        changed = sorted(name for name in old_names | new_names
                         if snapshot.get(name) != fresh.get(name))
        backup = safe_path(root, 'build/sync-backups/' +
                           datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:8])
        # Complete the backup before mutating any managed source.
        for name, data in snapshot.items():
            write_new(safe_path(backup, name), data)
        write_new(backup/'SYNC_BACKUP.json', json_bytes({
            'input': str(input_path), 'input_sha256': exported['sha256'],
            'changed_files': changed, 'notice': 'Pre-sync managed sources; build/ and Git not included.'}))
        try:
            for name in sorted(new_names):
                target = paths[name]
                target.parent.mkdir(parents=True, exist_ok=True)
                replacement = Path(temporary)/'replacement'
                replacement.write_bytes(fresh[name])
                replacement.replace(target)
            for name in old_names - new_names:
                if paths[name].exists():
                    paths[name].unlink()
            rebuilt, _ = assemble(root)
            if sha(rebuilt) != exported['sha256']:
                raise XpdError('Synced workspace does not match input XPD')
        except Exception:
            for name in old_names | new_names:
                target = paths[name]
                if name in snapshot:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(snapshot[name])
                elif target.exists():
                    target.unlink()
            raise
    return dict(exported, workspace=str(root), backup=str(backup),
                changed_files=changed, sync_mode='replace-from-xpd-not-merge')


def compare(a, b):
    """Supported text diff independent of shifted byte offsets."""
    def script_map(p):
        if p.is_dir():
            m, _, s = load_sources(p)
            return {i["script_id"]: (i["name"], s[i["path"]])
                    for i in m["segments"] if i["kind"] == "script"}
        data = read_bytes(p)
        header_check(data)
        return {r["script_id"]: (r["script_name"], r["text"])
                for r in scan(data) if r["kind"] == "script"}
    left, right = script_map(a), script_map(b)
    output = []
    for sid in sorted(set(left) | set(right)):
        old = left.get(sid, ("<missing>", ""))
        new = right.get(sid, ("<missing>", ""))
        if old != new:
            output.append("Script {:04d}: {} -> {}\n".format(sid, old[0], new[0]))
            output.extend(difflib.unified_diff(old[1].splitlines(True), new[1].splitlines(True),
                                               fromfile="before", tofile="after"))
    def screen_map(p):
        if p.is_dir():
            m, _, sources = load_sources(p)
            return {(i["screen"], i["key"]): sources[i["path"]][i["key"]]
                    for i in m["segments"] if i["kind"] in ("screen_name", "screen_text")}
        data = read_bytes(p)
        result = {}
        for block in screen_blocks(data, sections(data)):
            for index, r in enumerate(scan(data[block["start"]:block["end"]])):
                result[(block["key"], "t{:06d}".format(index))] = r["text"]
        return result
    left_screens, right_screens = screen_map(a), screen_map(b)
    for key in sorted(set(left_screens) | set(right_screens)):
        old, new = left_screens.get(key), right_screens.get(key)
        if old != new:
            output.append("Screen {} / {}\n- {}\n+ {}\n".format(key[0],key[1],
                          json.dumps(old,ensure_ascii=False),json.dumps(new,ensure_ascii=False)))
    output.append("Scope: script bodies and screen strings only; other properties are not compared.\n")
    return "".join(output)


SOURCE_README = """# XPD 텍스트 소스 프로젝트

이 폴더 전체가 빌드 입력입니다. 원본 XPD 없이 재구성할 수 있습니다.

- scripts/스크립트이름.txt: 스크립트 이름별 UTF-8 원문. 본문 수정 가능.
  내부 ID는 project.json에 보존합니다. 중복/파일명 금지 문자는 안전한 이름으로 조정합니다.
- screens/main.json, login.json 등: 확인된 화면 제목별 문자열. 기존 표시 문구 수정 가능.
  중복/미확인/파일명 부적합 제목은 B-NNNN.json, W-NNNN.json 등 내부 ID 사용.
  SCREENS.md에서 화면 제목과 파일을 확인하세요. ID는 project.json에 보존합니다.
  폰트명/내부 문자열도 섞여 있으므로 의미가 확인되는 값만 수정하세요.
  t000000은 화면 식별 이름이므로 변경할 수 없습니다.
- texts/: 발견된 문자열 후보. 화면별 의미 해석이 아닌 검색/검토용입니다.
- preserved/opaque.hex: 아직 해석하지 않은 바이너리의 16진수 보존본. 수정 금지.
- project.json: 조립 순서와 검증 정보. 수정 금지.

전체 폴더를 Git에 추가하세요. build/는 제외합니다.
XP-Builder에서 저장한 XPD는 xpd.py sync 입력.xpd 이폴더 명령으로 같은 폴더에 반영합니다.
sync는 자동 병합이 아닌 교체이며, 이전 소스는 build/sync-backups/에 먼저 백업합니다.
스크립트 파일은 UTF-8(BOM 없음)으로 저장하고 기존 줄바꿈을 유지하세요.
파일 추가/삭제, 스크립트 이름/번호 변경, 화면 객체 추가/삭제는 지원하지 않습니다.
기존 스크립트/화면 문자열 파일을 수정한 후 xpd.py build 명령으로 새 XPD를 만듭니다.
변경한 XPD는 XP-Builder에서 열기, 스크립트 검사, 저장/재열기, 시뮬레이션 검증이 필요합니다.
하드웨어 전송은 이 도구에서 수행하지 않습니다.

주의: 프로젝트 전체의 의미를 텍스트로 해석한 것은 아닙니다. 불명 데이터를 hex로
보존해서 왕복을 가능하게 한 방식입니다. 화면 좌표/색상/통신 설정의 편집기는 아닙니다.
"""


def main(argv=None):
    parser = argparse.ArgumentParser(description="XPD lossless source workspace (experimental)")
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    for cmd, help_text in (("extract", "Export XPD to a new text-source directory"),
                           ("sync", "Refresh existing sources from XPD, backing up before replacement (not a merge)")):
        p = commands.add_parser(cmd, help=help_text)
        p.add_argument("input", type=Path)
        p.add_argument("directory", type=Path)
    for cmd in ("build", "check"):
        p = commands.add_parser(cmd)
        p.add_argument("directory", type=Path)
        if cmd == "build":
            p.add_argument("output", type=Path)
        p.add_argument("--allow-unverified-text", action="store_true")
    p = commands.add_parser("diff", help="Compare scripts and screen strings in XPDs or source directories")
    p.add_argument("before", type=Path)
    p.add_argument("after", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "extract":
            result = export_project(args.input.resolve(), args.directory.resolve())
        elif args.command == "sync":
            result = sync_project(args.input.resolve(), args.directory.resolve())
        elif args.command == "build":
            result = build_project(args.directory.resolve(), args.output.resolve(), args.allow_unverified_text)
        elif args.command == "check":
            _, result = assemble(args.directory.resolve(), args.allow_unverified_text)
        else:
            print(compare(args.before.resolve(), args.after.resolve()))
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (XpdError, OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
