"""Decoder/encoder for TamaPoke's save format -- the same wire format
save.cpp's EXPORT/IMPORT produces, plus the petA/petB/plyA/plyB checkpoint
blobs that ride inside it as SK_BYTES fields.

Pure logic, no serial/GUI dependency, so it can be unit tested and reused by
both the live monitor and an offline "decode this backup file" path.

Every offset and struct layout here was taken directly from save.cpp/pet.cpp/
party.h, not guessed -- see the comments on each constant for where. Keep it
that way: if the firmware's layout changes, this drifts out of sync silently,
the same trap CLAUDE.md warns about for anything that "restates" a table
instead of deriving it. There is no way to derive this one from Python, so
re-check by hand against the current C++ when any of those files change.
"""
import re
import struct
from collections import OrderedDict

# ---- the outer EXPORT/IMPORT wire format (save.cpp) -----------------------

SAVE_MAGIC = b"TKPS"
SAVE_VERSION = 1
SAVE_HDR = 8

SK_U8, SK_I8, SK_BOOL, SK_U16, SK_I16, SK_U32, SK_BYTES, SK_STR = range(1, 9)
SK_NAMES = {SK_U8: "u8", SK_I8: "i8", SK_BOOL: "bool", SK_U16: "u16",
            SK_I16: "i16", SK_U32: "u32", SK_BYTES: "bytes", SK_STR: "str"}

# Every key save.cpp's SAVE_FIELDS lists, in the same order. Kept as a plain
# list here (not re-derived from the firmware) because it is the wire format
# for the backup itself -- a python-side copy is exactly as load-bearing as
# save.cpp's own table, and just as append-only.
SAVE_FIELDS = [
    ("init", SK_BOOL), ("full", SK_U8), ("joy", SK_U8),
    ("ene", SK_U8), ("hyg", SK_U8), ("poop", SK_U8),
    ("wgt", SK_U8), ("age", SK_U32), ("dexn", SK_I16),
    ("eggT2", SK_I16), ("crack", SK_U8), ("mist", SK_U8),
    ("sleep", SK_BOOL), ("lend", SK_U8), ("seen", SK_U32),
    ("petA", SK_BYTES), ("petB", SK_BYTES),
    ("plyA", SK_BYTES), ("plyB", SK_BYTES),
    ("bond", SK_U8), ("nick", SK_STR), ("froz", SK_BOOL),
    ("ivat", SK_U8), ("ivdf", SK_U8), ("ivsp", SK_U8),
    ("ivhp", SK_U8), ("tatk", SK_U8), ("tdef", SK_U8),
    ("tspe", SK_U8),
    ("mvs", SK_BYTES), ("mvlv", SK_U8),
    ("bk", SK_BOOL), ("shy", SK_BOOL), ("eshy", SK_BOOL),
    ("stpk", SK_BOOL), ("evop", SK_U8), ("slpa", SK_U8), ("rtpn", SK_BOOL),
    ("tnam", SK_STR), ("avtr", SK_U8), ("badg", SK_U16),
    ("reg", SK_U8), ("eggR", SK_BYTES), ("regn", SK_U8),
    ("badgX", SK_BYTES), ("badhX", SK_BYTES),
    ("badh", SK_U16), ("dexreg", SK_BYTES), ("dexsh", SK_BYTES),
    ("strk", SK_U16), ("bstrk", SK_U16), ("cday", SK_U32),
    ("medal", SK_U16), ("tmedal", SK_U16), ("mstone", SK_U16),
    ("ghi", SK_U16), ("shi", SK_U16), ("qhi", SK_U16),
    ("wlt", SK_U32), ("stps", SK_U32),
    ("party", SK_BYTES), ("box", SK_BYTES), ("bag", SK_BYTES),
    ("rivals", SK_BYTES),
    ("lang", SK_U8), ("snd", SK_BOOL), ("vol", SK_U8),
]
SAVE_FIELD_KIND = dict(SAVE_FIELDS)

# Legacy scalar keys that mirror the LIVE pet -- shown/edited as one group in
# the editor. Deliberately excludes petA/petB/party/box/etc, which get their
# own decoded views.
LIVE_PET_KEYS = [
    "full", "joy", "ene", "hyg", "poop", "wgt", "age", "dexn", "eggT2",
    "crack", "mist", "sleep", "lend", "seen", "bond", "nick", "froz",
    "ivat", "ivdf", "ivsp", "ivhp", "tatk", "tdef", "tspe", "mvlv",
    "bk", "shy", "eshy", "stpk", "evop", "slpa", "rtpn",
]
PLAYER_KEYS = [
    "tnam", "avtr", "badg", "reg", "regn", "badh", "strk", "bstrk", "cday",
    "medal", "tmedal", "mstone", "wlt", "stps", "ghi", "shi", "qhi",
    "lang", "snd", "vol",
]

# Legacy scalar keys that are actually booleans on the wire -- the editor
# shows these as checkboxes rather than free text, so there is no "true"/
# "false"/"1"/"yes" typo to get wrong.
BOOL_LIVE_KEYS = {"sleep", "froz", "bk", "shy", "eshy", "stpk", "rtpn"}
BOOL_PLAYER_KEYS = {"snd"}

# Human labels for the cryptic NVS key names and PartyMon field names. Display
# only -- the underlying key/field name (the wire format) never changes, this
# just says what it MEANS. Falls back to the raw name via label_for() if a
# key is missing here, so a future field never crashes the editor for being
# unlabeled.
FRIENDLY_NAMES = {
    # live pet, legacy scalars (save.cpp's SAVE_FIELDS)
    "full": "Fullness (food)", "joy": "Joy", "ene": "Energy", "hyg": "Hygiene",
    "poop": "Poops (uncleaned)", "wgt": "Weight", "age": "Age (minutes)",
    "dexn": "Species (dex #)", "eggT2": "Egg target species",
    "crack": "Egg cracks/taps", "mist": "Care mistakes", "sleep": "Sleeping",
    "lend": "Last ending kind (0=none 1=farewell 2=runaway 3=release)",
    "seen": "Last seen (epoch seconds)", "bond": "Bond", "nick": "Nickname",
    "froz": "Frozen (banked/revived -- no longer ages)",
    "ivat": "IV: Attack", "ivdf": "IV: Defense", "ivsp": "IV: Speed", "ivhp": "IV: HP",
    "tatk": "Training: Attack", "tdef": "Training: Defense", "tspe": "Training: Speed",
    "mvlv": "Last move-learn level",
    "bk": "Berry known", "shy": "Shiny", "eshy": "Egg is shiny",
    "stpk": "Starter pick (true only before first boot)",
    "evop": "Evolution penalty (days)", "slpa": "Auto-sleep state", "rtpn": "Retire pending",
    # PartyMon fields (party/box slots, and the checkpoint tail handover)
    "dex": "Species (dex #)", "ageMinutes": "Age (minutes)",
    "ivAtk": "IV: Attack", "ivDef": "IV: Defense", "ivSpe": "IV: Speed", "ivHp": "IV: HP",
    "trAtk": "Training: Attack", "trDef": "Training: Defense", "trSpe": "Training: Speed",
    "shiny": "Shiny", "fullness": "Fullness (food)", "energy": "Energy",
    "hygiene": "Hygiene", "poops": "Poops (uncleaned)", "weight": "Weight",
    "berryKnown": "Berry known", "careMistakes": "Care mistakes",
    "evoDeclinedLv": "Evolution declined at level", "lastLearnLevel": "Last move-learn level",
    "stateVersion": "Care-state version (0 = predates care tracking)",
    # the player -- outlives every creature (save.cpp's SAVE_FIELDS)
    "tnam": "Trainer name", "avtr": "Avatar index", "badg": "Kanto badges (easy, bitmask)",
    "reg": "Current region", "eggR": "Egg region history (bytes)",
    "regn": "Region count unlocked", "badgX": "Other-region badges, easy (bytes)",
    "badhX": "Other-region badges, hard (bytes)", "badh": "Kanto badges (hard, bitmask)",
    "dexreg": "Pokedex: registered (bitmask bytes)", "dexsh": "Pokedex: shiny seen (bitmask bytes)",
    "strk": "Daily streak", "bstrk": "Best daily streak", "cday": "Current streak day (epoch)",
    "medal": "Medals (current)", "tmedal": "Medals (lifetime total)",
    "mstone": "Milestone flags (bitmask)",
    "ghi": "Minigame high score: punching bag", "shi": "Minigame high score: reaction test",
    "qhi": "Minigame high score: joy game",
    "wlt": "Poke Mart wallet ($, 1 pedometer step = $1, capped at 999999)",
    "stps": "Lifetime pedometer step count (never spent, display only)",
    "lang": "Language index", "snd": "Sound enabled", "vol": "Volume (0-10)",
}


def label_for(key: str) -> str:
    return FRIENDLY_NAMES.get(key, key)


# ---- input range validation, so a value that would overflow the wire ------
# format is caught with a clear message instead of crashing struct.pack (or
# worse, wrapping silently) when the editor tries to encode it.
_SCALAR_RANGES = {
    SK_U8: (0, 255), SK_I8: (-128, 127), SK_U16: (0, 65535),
    SK_I16: (-32768, 32767), SK_U32: (0, 4294967295),
}
_STRUCT_CHAR_RANGES = {"B": (0, 255), "H": (0, 65535), "h": (-32768, 32767),
                        "I": (0, 4294967295)}


def range_for_kind(kind: int):
    """(lo, hi) for a SK_* scalar kind, or None if it has no numeric range
    (bool/str/bytes)."""
    return _SCALAR_RANGES.get(kind)


def range_for_party_field(name: str):
    """(lo, hi) for a PartyMon field by name, derived from its actual struct
    format character -- never a hand-restated number, so it can't drift from
    the real on-disk width. 'moves' is special-cased by the caller (it's a
    4-element list, not a scalar); this covers every other field."""
    return _STRUCT_CHAR_RANGES.get(_PARTY_MON_FIELD_FMT.get(name))


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: init 0xFFFF, poly 0x1021, MSB first, no final xor.
    Identical algorithm to save.cpp's crc16() and pet.cpp's ckptCrc() -- both
    are the same CRC wearing two names because one is the wire format and the
    other is the checkpoint format, and the firmware keeps its own two copies
    for the same reason (see pet.cpp's comment on ckptCrc)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


class SaveError(Exception):
    pass


def parse_export(raw: bytes) -> "OrderedDict":
    """Parses the outer wire format. Returns an OrderedDict of
    key -> (kind, value_bytes). Raises SaveError with a human reason on any
    validation failure -- mirrors saveImport()'s pass-one checks exactly, so
    a file this rejects is a file the firmware would reject too."""
    if len(raw) < SAVE_HDR + 2:
        raise SaveError("too short to hold a header")
    if raw[0:4] != SAVE_MAGIC:
        raise SaveError("bad magic (not a TamaPoke export)")
    if raw[4] != SAVE_VERSION:
        raise SaveError(f"unknown save version {raw[4]}")
    count = raw[5] | (raw[6] << 8)
    want = raw[-2] | (raw[-1] << 8)
    got = crc16(raw[:-2])
    if got != want:
        raise SaveError(f"CRC mismatch (file says 0x{want:04X}, computed 0x{got:04X})")

    fields = OrderedDict()
    at = SAVE_HDR
    end = len(raw) - 2
    seen = 0
    while at + 4 <= end:
        klen = raw[at]
        if not klen or klen > 15 or at + 1 + klen + 3 > end:
            raise SaveError(f"malformed field at byte {at}")
        key = raw[at + 1:at + 1 + klen].decode("ascii", "replace")
        vat = at + 1 + klen
        kind = raw[vat]
        vlen = raw[vat + 1] | (raw[vat + 2] << 8)
        if vat + 3 + vlen > end:
            raise SaveError(f"field '{key}' overruns the buffer")
        fields[key] = (kind, raw[vat + 3:vat + 3 + vlen])
        at = vat + 3 + vlen
        seen += 1
    if at != end or seen != count:
        raise SaveError(f"trailer mismatch: declared {count} fields, parsed {seen}")
    return fields


def encode_export(fields: "OrderedDict") -> bytes:
    """Reverses parse_export(). fields is key -> (kind, value_bytes)."""
    out = bytearray(SAVE_HDR)
    for key, (kind, val) in fields.items():
        klen = len(key)
        if klen > 15:
            raise SaveError(f"key '{key}' too long for NVS")
        out.append(klen)
        out += key.encode("ascii")
        out.append(kind)
        out.append(len(val) & 0xFF)
        out.append((len(val) >> 8) & 0xFF)
        out += val
    out[0:4] = SAVE_MAGIC
    out[4] = SAVE_VERSION
    out[5] = len(fields) & 0xFF
    out[6] = (len(fields) >> 8) & 0xFF
    out[7] = 0
    c = crc16(bytes(out))
    out.append(c & 0xFF)
    out.append((c >> 8) & 0xFF)
    return bytes(out)


def decode_scalar(kind: int, raw: bytes):
    """Wire bytes -> a plain python value, for display and for editing."""
    if kind == SK_U8:
        return raw[0] if raw else 0
    if kind == SK_I8:
        return struct.unpack("<b", raw)[0] if raw else 0
    if kind == SK_BOOL:
        return bool(raw[0]) if raw else False
    if kind == SK_U16:
        return struct.unpack("<H", raw)[0] if len(raw) == 2 else 0
    if kind == SK_I16:
        return struct.unpack("<h", raw)[0] if len(raw) == 2 else 0
    if kind == SK_U32:
        return struct.unpack("<I", raw)[0] if len(raw) == 4 else 0
    if kind == SK_STR:
        return raw.decode("ascii", "replace")
    return raw  # SK_BYTES: caller decodes further (party/box/checkpoints)


def encode_scalar(kind: int, value) -> bytes:
    """Reverses decode_scalar() for the scalar kinds. Raises SaveError on a
    value that will not fit, rather than silently truncating it."""
    try:
        if kind == SK_U8:
            v = int(value)
            if not 0 <= v <= 255:
                raise ValueError
            return bytes([v])
        if kind == SK_I8:
            return struct.pack("<b", int(value))
        if kind == SK_BOOL:
            return bytes([1 if value else 0])
        if kind == SK_U16:
            return struct.pack("<H", int(value))
        if kind == SK_I16:
            return struct.pack("<h", int(value))
        if kind == SK_U32:
            return struct.pack("<I", int(value))
        if kind == SK_STR:
            b = str(value).encode("ascii", "replace")
            if len(b) >= 64:
                raise ValueError
            return b
    except (ValueError, struct.error):
        raise SaveError(f"'{value}' does not fit a {SK_NAMES.get(kind, kind)}")
    return value  # SK_BYTES: caller already has raw bytes


# ---- PartyMon: 48 bytes, party.h --------------------------------------
# Field order and widths straight from `struct PartyMon` in party.h. No
# padding falls between any of these on a real build (every multi-byte field
# already lands on its natural boundary given this order), which is what lets
# a flat struct.unpack do the whole thing in one call -- verified against a
# real device capture (backups/export-2026-09-10-020034.txt) during tonight's
# session: this format decoded party slot 0 back to the exact IVs/training
# STATS had printed moments earlier on the board.
PARTY_MON_FIELDS = [
    ("dex", "h"), ("level", "H"), ("medals", "H"),
    ("ivAtk", "B"), ("ivDef", "B"), ("ivSpe", "B"), ("ivHp", "B"),
    ("trAtk", "B"), ("trDef", "B"), ("trSpe", "B"), ("shiny", "B"),
    ("nick", "12s"),
    ("move0", "B"), ("move1", "B"), ("move2", "B"), ("move3", "B"),
    ("stateVersion", "B"),
    ("fullness", "B"), ("joy", "B"), ("energy", "B"), ("hygiene", "B"),
    ("poops", "B"), ("weight", "B"), ("bond", "B"),
    ("berryKnown", "B"), ("careMistakes", "B"),
    ("evoDeclinedLv", "B"), ("lastLearnLevel", "B"),
    ("_pad", "2x"),
    ("ageMinutes", "I"),
]
PARTY_MON_FMT = "<" + "".join(f for _, f in PARTY_MON_FIELDS)
PARTY_MON_SIZE = struct.calcsize(PARTY_MON_FMT)
assert PARTY_MON_SIZE == 48, f"PartyMon layout drifted: {PARTY_MON_SIZE} != 48"
_PMON_NAMES = [n for n, _ in PARTY_MON_FIELDS if n != "_pad"]
# name -> format char, with move0..move3 collapsed to "moveN" so
# range_for_party_field("moveN") also works for the dialog's move entries.
_PARTY_MON_FIELD_FMT = {n: f for n, f in PARTY_MON_FIELDS if n != "_pad"}
_PARTY_MON_FIELD_FMT["moveN"] = "B"


def decode_party_mon(raw48: bytes) -> dict:
    vals = struct.unpack(PARTY_MON_FMT, raw48)
    d = dict(zip(_PMON_NAMES, vals))
    d["nick"] = d["nick"].split(b"\x00", 1)[0].decode("ascii", "replace")
    d["moves"] = [d.pop(f"move{i}") for i in range(4)]
    d["empty"] = d["dex"] < 1
    return d


def encode_party_mon(d: dict) -> bytes:
    nick = d.get("nick", "").encode("ascii", "replace")[:11]
    moves = d.get("moves", [0, 0, 0, 0])
    vals = []
    for name, _ in PARTY_MON_FIELDS:
        if name == "_pad":
            continue
        if name == "nick":
            vals.append(nick.ljust(12, b"\x00"))
        elif name.startswith("move"):
            vals.append(moves[int(name[-1])])
        else:
            vals.append(d.get(name, 0))
    out = bytearray(48)
    off = 0
    it = iter(vals)
    for name, f in PARTY_MON_FIELDS:
        if name == "_pad":
            off += 2
            continue
        size = struct.calcsize("<" + f)
        struct.pack_into("<" + f, out, off, next(it))
        off += size
    return bytes(out)


def decode_mon_list(raw: bytes) -> list:
    n = len(raw) // PARTY_MON_SIZE
    return [decode_party_mon(raw[i * PARTY_MON_SIZE:(i + 1) * PARTY_MON_SIZE]) for i in range(n)]


def encode_mon_list(mons: list) -> bytes:
    return b"".join(encode_party_mon(m) for m in mons)


# ---- the bag: one byte per item key, indexed by key (inventory.h) ---------
# `counts[ITEM_COUNT]`, written raw with no header -- inventory.cpp's save()
# is a single putBytes(counts, sizeof(counts)). Slot count is DERIVED from
# the blob's own length rather than a restated ITEM_COUNT, the same reasoning
# as decode_mon_list() above.

def decode_bag(raw: bytes) -> list:
    """Returns one dict per slot: {"key": item index, "count": stack size}.
    Index 0 is the firmware's own unused filler slot and is always count 0."""
    return [{"key": i, "count": c} for i, c in enumerate(raw)]


def encode_bag(slots: list) -> bytes:
    return bytes(s["count"] for s in slots)


_ITEM_NAME_RE = re.compile(r'\{\s*"([^"]+)"')


def parse_item_names(items_h_path: str) -> dict:
    """Reads ITEM_TBL out of items.h by parsing the source directly, the same
    reasoning as parse_dex_names(). Returns {} on any failure."""
    try:
        with open(items_h_path, encoding="utf-8") as f:
            text = f.read()
        start = text.index("ITEM_TBL[ITEM_COUNT]")
        body = text[start:text.index("\n};", start)]
        return {i: m.group(1) for i, m in enumerate(_ITEM_NAME_RE.finditer(body))}
    except Exception:
        return {}


def item_name(key: int, names: dict) -> str:
    if key in names and names[key] != "-":
        return names[key]
    return f"item #{key}" if key else "-"


# ---- LAN rival records: pet.h's RivalRecord, RIVAL_CAP slots --------------
# mac[6] + name[12] + wins(u16) + losses(u16) = 22 bytes, no padding (both
# multi-byte fields already land on an even offset given this order). Written
# raw via prefs.putBytes("rivals", rivals, sizeof(rivals)) -- see pet.cpp.
RIVAL_RECORD_FMT = "<6s12sHH"
RIVAL_RECORD_SIZE = struct.calcsize(RIVAL_RECORD_FMT)
assert RIVAL_RECORD_SIZE == 22, f"RivalRecord layout drifted: {RIVAL_RECORD_SIZE} != 22"


def decode_rival(raw22: bytes) -> dict:
    mac, name, wins, losses = struct.unpack(RIVAL_RECORD_FMT, raw22)
    return {
        "mac": mac.hex().upper(),
        "name": name.split(b"\x00", 1)[0].decode("ascii", "replace"),
        "wins": wins, "losses": losses,
        "empty": mac == b"\x00" * 6,
    }


def encode_rival(d: dict) -> bytes:
    mac = bytes.fromhex(d.get("mac", "") or "00" * 6)
    if len(mac) != 6:
        raise SaveError(f"MAC '{d.get('mac')}' is not 12 hex characters")
    name = d.get("name", "").encode("ascii", "replace")[:11].ljust(12, b"\x00")
    return struct.pack(RIVAL_RECORD_FMT, mac, name, d.get("wins", 0), d.get("losses", 0))


def decode_rivals_blob(raw: bytes) -> list:
    n = len(raw) // RIVAL_RECORD_SIZE
    return [decode_rival(raw[i * RIVAL_RECORD_SIZE:(i + 1) * RIVAL_RECORD_SIZE]) for i in range(n)]


def encode_rivals_blob(rivals: list) -> bytes:
    return b"".join(encode_rival(r) for r in rivals)


# ---- checkpoints: petA/petB (the creature), plyA/plyB (the player) --------
# pet.cpp's format exactly: 12-byte header (magic u32, version u16, size u16,
# generation u32), a body, then a trailing 2-byte crc over everything before
# it. `size` is self-describing and includes the crc.
CKPT_HDR = 12
CKPT_CRC = 2
PET_CORE_MAGIC = 0x31504B54   # "TKP1", pet.cpp
PLAYER_MAGIC = 0x31594B54     # "TKY1", pet.cpp
PET_FIXED = 70                # pet.cpp PET_FIXED
PET_TAIL_MON = 74             # pet.cpp PET_TAIL_MON

# PetCoreSnapshot, byte for byte -- see pet.cpp. Nothing here is padding: this
# struct was deliberately written with every multi-byte field grouped before
# the bytes, so a flat little-endian unpack matches the on-wire layout with no
# compiler-inserted gaps.
PET_CORE_FIELDS = [
    ("magic", "I"), ("version", "H"), ("size", "H"), ("generation", "I"),
    ("ageMinutes", "I"), ("lastSeenEpoch", "I"),
    ("speciesId", "h"), ("eggTarget", "h"), ("medals", "H"),
    ("fullness", "B"), ("joy", "B"), ("energy", "B"), ("hygiene", "B"),
    ("poops", "B"), ("weight", "B"),
    ("ivAtk", "B"), ("ivDef", "B"), ("ivSpe", "B"), ("ivHp", "B"),
    ("trAtk", "B"), ("trDef", "B"), ("trSpe", "B"),
    ("move0", "B"), ("move1", "B"), ("move2", "B"), ("move3", "B"),
    ("lastLearnLevel", "B"),
    ("berryKnown", "B"), ("shiny", "B"), ("eggShiny", "B"), ("starterPick", "B"),
    ("evoPen", "B"), ("sleepAuto", "B"), ("retirePending", "B"), ("eggTaps", "B"),
    ("careMistakes", "B"), ("sleeping", "B"), ("lastEnd", "B"), ("frozen", "B"),
    ("bond", "B"),
    ("nick", "12s"),
    ("_pad0", "B"),
]
PET_CORE_FMT = "<" + "".join(f for _, f in PET_CORE_FIELDS)
assert struct.calcsize(PET_CORE_FMT) == PET_FIXED, "PetCoreSnapshot layout drifted"
_PCORE_NAMES = [n for n, _ in PET_CORE_FIELDS if n != "_pad0"]


def _ckpt_header_ok(raw: bytes, magic: int):
    """Validates header + crc exactly as pet.cpp's ckptRead() does. Returns
    (generation, version) or raises SaveError."""
    if len(raw) < CKPT_HDR + CKPT_CRC:
        raise SaveError("too short to be a checkpoint")
    got_magic, version, size, generation = struct.unpack_from("<IHHI", raw, 0)
    if got_magic != magic:
        raise SaveError(f"bad checkpoint magic 0x{got_magic:08X}")
    if size != len(raw):
        raise SaveError(f"checkpoint declares size {size}, blob is {len(raw)}")
    want = raw[-2] | (raw[-1] << 8)
    got = crc16(raw[:-2])
    if got != want:
        raise SaveError(f"checkpoint CRC mismatch (0x{want:04X} != 0x{got:04X})")
    return generation, version


def decode_pet_checkpoint(raw: bytes) -> dict:
    """Decodes a petA/petB blob: the fixed PetCoreSnapshot plus the v2 tail
    (the handed-over creature still waiting for a party slot), if present.
    Returns {"valid": False, "error": ...} on anything that fails validation
    rather than raising, since a broken checkpoint is itself a normal, useful
    thing for the editor to be able to show."""
    try:
        generation, version = _ckpt_header_ok(raw, PET_CORE_MAGIC)
    except SaveError as e:
        return {"valid": False, "error": str(e)}
    body = raw[:PET_FIXED] if len(raw) - CKPT_CRC >= PET_FIXED else raw[:-CKPT_CRC]
    body = body.ljust(PET_FIXED, b"\x00")
    vals = struct.unpack(PET_CORE_FMT, body)
    d = dict(zip(_PCORE_NAMES, vals))
    d["nick"] = d["nick"].split(b"\x00", 1)[0].decode("ascii", "replace")
    d["moves"] = [d.pop(f"move{i}") for i in range(4)]
    d["valid"] = True
    d["error"] = None
    d["generation"] = generation
    d["version"] = version
    d["level"] = min(100, 1 + d["ageMinutes"] // 60)
    d["dex"] = d["speciesId"]   # alias so _identity() matches PartyMon dicts

    body_len = len(raw) - CKPT_CRC
    d["ended_mon"] = None
    d["ended_kind"] = 0
    if body_len >= PET_TAIL_MON:
        kind = raw[PET_FIXED]
        mon_len = struct.unpack_from("<H", raw, PET_FIXED + 2)[0]
        d["ended_kind"] = kind
        if mon_len:
            take = min(mon_len, PARTY_MON_SIZE, body_len - PET_TAIL_MON)
            chunk = raw[PET_TAIL_MON:PET_TAIL_MON + take].ljust(PARTY_MON_SIZE, b"\x00")
            d["ended_mon"] = decode_party_mon(chunk)
    return d


def decode_player_checkpoint(raw: bytes) -> dict:
    """Header/generation/CRC only -- the player record's tail is a set of
    variable-length arrays sized by DEX_COUNT/REGION_COUNT/GYM_REGIONS, which
    this tool does not track. Good enough to answer "is it valid, and which
    of plyA/plyB is newer" without re-deriving the firmware's dex/region
    tables in python."""
    try:
        generation, version = _ckpt_header_ok(raw, PLAYER_MAGIC)
        return {"valid": True, "error": None, "generation": generation,
                "version": version, "body_len": len(raw) - CKPT_HDR - CKPT_CRC}
    except SaveError as e:
        return {"valid": False, "error": str(e)}


def newer_checkpoint(a: "dict|None", b: "dict|None"):
    """Which of two decoded checkpoints (may be None/invalid) is the one the
    firmware would actually load -- same wrap-safe comparison as pet.cpp's
    generationAfter()."""
    def gen(x):
        return x["generation"] if x and x.get("valid") else None
    ga, gb = gen(a), gen(b)
    if ga is None and gb is None:
        return None
    if ga is None:
        return b
    if gb is None:
        return a
    return a if ((ga - gb) & 0xFFFFFFFF) < 0x80000000 and ga != gb else (a if ga == gb else b)


# ---- species names, derived from dex.h, never restated --------------------

_DEX_NAME_RE = re.compile(r'\{\s*"([^"]+)"')


def parse_dex_names(dex_h_path: str) -> dict:
    """Reads DEX_TBL out of dex.h by parsing the source directly, so this
    never carries its own copy of the species list to drift out of sync.
    Index 0 is the firmware's own "?" placeholder. Returns {} on any failure
    -- a missing dex.h (offline replay of a file with no repo checkout
    nearby) should degrade to plain dex numbers, not crash the decoder."""
    try:
        with open(dex_h_path, encoding="utf-8") as f:
            text = f.read()
        start = text.index("DEX_TBL[DEX_COUNT + 1]")
        body = text[start:text.index("\n};", start)]
        names = {}
        for i, m in enumerate(_DEX_NAME_RE.finditer(body)):
            names[i] = m.group(1)
        return names
    except Exception:
        return {}


def species_name(dex: int, names: dict) -> str:
    if dex in names and names[dex] != "?":
        return f"{names[dex]} (#{dex})"
    return f"#{dex}" if dex else "-"


# ---- the full decode, and the integrity checks --------------------------

IDENTITY_KEYS = ("dex", "level", "ivAtk", "ivDef", "ivSpe", "ivHp",
                 "trAtk", "trDef", "trSpe", "ageMinutes")


def _identity(rec: dict) -> tuple:
    return tuple(rec.get(k, 0) for k in IDENTITY_KEYS)


def decode_save(raw: bytes, dex_h_path: str = None, items_h_path: str = None) -> dict:
    """The one entry point the GUI calls. Returns a dict with every layer
    decoded: raw fields, the live pet's legacy scalars, party/box lists, the
    bag, LAN rival records, both checkpoints, and a list of integrity
    findings. Never raises -- a file that fails outer validation comes back
    with ok=False and nothing else."""
    result = {"ok": False, "error": None}
    try:
        fields = parse_export(raw)
    except SaveError as e:
        result["error"] = str(e)
        return result

    result["ok"] = True
    result["fields"] = fields
    result["field_count"] = len(fields)
    names = parse_dex_names(dex_h_path) if dex_h_path else {}
    result["names"] = names
    item_names = parse_item_names(items_h_path) if items_h_path else {}
    result["item_names"] = item_names

    live = {}
    for key in LIVE_PET_KEYS:
        if key in fields:
            live[key] = decode_scalar(*fields[key])
    result["live_pet"] = live

    player = {}
    for key in PLAYER_KEYS:
        if key in fields:
            player[key] = decode_scalar(*fields[key])
    result["player"] = player

    result["party"] = decode_mon_list(fields["party"][1]) if "party" in fields else []
    result["box"] = decode_mon_list(fields["box"][1]) if "box" in fields else []
    result["bag"] = decode_bag(fields["bag"][1]) if "bag" in fields else []
    result["rivals"] = decode_rivals_blob(fields["rivals"][1]) if "rivals" in fields else []

    pet_a = decode_pet_checkpoint(fields["petA"][1]) if "petA" in fields else None
    pet_b = decode_pet_checkpoint(fields["petB"][1]) if "petB" in fields else None
    result["petA"], result["petB"] = pet_a, pet_b
    result["pet_checkpoint"] = newer_checkpoint(pet_a, pet_b)

    ply_a = decode_player_checkpoint(fields["plyA"][1]) if "plyA" in fields else None
    ply_b = decode_player_checkpoint(fields["plyB"][1]) if "plyB" in fields else None
    result["plyA"], result["plyB"] = ply_a, ply_b
    result["player_checkpoint"] = newer_checkpoint(ply_a, ply_b)

    result["findings"] = find_integrity_issues(result)
    return result


def find_integrity_issues(d: dict) -> list:
    """The automated version of tonight's by-hand analysis: flags any two
    creatures (live pet, any party slot, any box slot) that share identical
    dex/level/IV/training/age, and flags a checkpoint-tail handover that was
    never cleared. Returns a list of {"severity", "message"} dicts."""
    findings = []
    individuals = []  # (label, identity tuple)

    live = d.get("live_pet", {})
    legacy_rec = None
    if live.get("dexn", 0) and live["dexn"] > 0:
        legacy_rec = {
            "dex": live.get("dexn", 0),
            "level": min(100, 1 + live.get("age", 0) // 60),
            "ivAtk": live.get("ivat", 0), "ivDef": live.get("ivdf", 0),
            "ivSpe": live.get("ivsp", 0), "ivHp": live.get("ivhp", 0),
            "trAtk": live.get("tatk", 0), "trDef": live.get("tdef", 0),
            "trSpe": live.get("tspe", 0), "ageMinutes": live.get("age", 0),
        }

    ck = d.get("pet_checkpoint")
    ck_rec = ck if (ck and ck.get("valid") and ck.get("speciesId", 0) > 0) else None

    # The legacy scalars and the checkpoint are BOTH mirrors of the one live
    # pet, so agreeing is the healthy, expected case -- fold them into a
    # single "live pet" individual rather than letting them collide with each
    # other in the dedup pass below. Disagreeing gets its own finding instead.
    if legacy_rec and ck_rec:
        if _identity(legacy_rec) == _identity(ck_rec):
            individuals.append(("live pet", legacy_rec))
        else:
            individuals.append(("live pet (legacy)", legacy_rec))
            individuals.append(("live pet (checkpoint)", ck_rec))
            findings.append({
                "severity": "info",
                "message": (f"legacy scalars and the newest valid checkpoint disagree about "
                             f"the live pet (dex {legacy_rec['dex']} vs {ck_rec['dex']}) -- "
                             f"the firmware trusts the checkpoint on boot, so that is what "
                             f"will actually load."),
            })
    elif legacy_rec:
        individuals.append(("live pet (legacy)", legacy_rec))
    elif ck_rec:
        individuals.append(("live pet (checkpoint)", ck_rec))

    for i, mon in enumerate(d.get("party", [])):
        if not mon.get("empty"):
            individuals.append((f"party slot {i}", mon))
    for i, mon in enumerate(d.get("box", [])):
        if not mon.get("empty"):
            individuals.append((f"box slot {i}", mon))

    # Checked against live pet + party + box, but BEFORE adding the tail
    # itself to `individuals` -- otherwise it would trivially match itself.
    if ck_rec and ck_rec.get("ended_mon") and not ck_rec["ended_mon"].get("empty"):
        em = ck_rec["ended_mon"]
        for label, rec in individuals:
            if _identity(rec) != _identity(em):
                continue
            findings.append({
                "severity": "warning",
                "message": (f"checkpoint tail holds a handover ({em.get('dex')}, "
                             f"lv{em.get('level')}) that exactly matches '{label}' -- "
                             f"looks like it was never cleared after being placed."),
            })
        individuals.append(("checkpoint tail (unconsumed handover)", em))

    seen = {}
    for label, rec in individuals:
        key = _identity(rec)
        if all(v == 0 for v in key):
            continue
        if key in seen:
            findings.append({
                "severity": "error",
                "message": (f"'{seen[key]}' and '{label}' have identical species/level/"
                             f"IVs/training/age -- almost certainly the same creature "
                             f"stored twice, not a coincidence."),
            })
        else:
            seen[key] = label

    return findings
