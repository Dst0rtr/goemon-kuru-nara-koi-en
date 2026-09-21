#!/usr/bin/env python3
"""
Mystical Ninja: Shadey Business -- English patch for SLPM-86155, rebuilt from YOUR disc.

    python3 patch_goemon.py  <your original .bin>  [output folder]

What it does, in order:

  1. Checks that the file you gave it is the Japanese disc this patch was made
     for (SHA-256, before anything else is read or written).
  2. Reads the game's files out of that disc image.
  3. Applies this project's changes to the game data *after* decompressing it,
     so every byte of Konami's content comes from your disc, not from this
     download. Then it re-compresses with the project's encoder.
  4. Writes a new disc image, sector by sector, with every file at the position
     the English build expects, recomputes each rewritten sector's error-
     correction codes, and checks the result against the SHA-256 of the English
     build. Nothing is left behind unless that final check passes.

Python 3.6 or newer, standard library only. Nothing to install. It never writes to
your original disc image.

The payload (goemon-en.ggp) holds only this project's own work: translated text,
redrawn art, executable patch words, and the opening movie's replaced frames.
patcher/README section "What is in the download" lists what is and is not in it.

Format notes for the curious are at the bottom of this file.
"""

import hashlib
import json
import operator
import os
import shutil
import struct
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import kgd  # noqa: E402  -- the project's LZSS codec, shipped beside this file

PAYLOAD_NAME = "goemon-en.ggp"
MAGIC = b"GGRP"
FORMAT_VERSION = 1

RAW = 2352          # bytes per raw MODE2/2352 sector
USER = 2048         # Form 1 user data
SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


class PatchError(Exception):
    """A failure the player can act on. The message is printed as-is."""


# ---------------------------------------------------------------------------------
# CD-ROM sector encoding: address, EDC and ECC (Mode 2 Form 1)
# ---------------------------------------------------------------------------------

def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


def sector_header(lba):
    """Sync + BCD MSF address + mode byte 2, for a sector at `lba`."""
    x = lba + 150
    return SYNC + bytes((_bcd(x // 4500), _bcd((x // 75) % 60), _bcd(x % 75), 2))


def _tables():
    f_lut = bytearray(256)
    b_lut = bytearray(256)
    edc_lut = [0] * 256
    for i in range(256):
        j = ((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF
        f_lut[i] = j
        b_lut[i ^ j] = i
        e = i
        for _ in range(8):
            e = (e >> 1) ^ (0xD8018001 if e & 1 else 0)
        edc_lut[i] = e
    return bytes(f_lut), bytes(b_lut), edc_lut


ECC_F, ECC_B, EDC_LUT = _tables()


def _gf_pow_tables():
    """POW[k] maps a byte t to t * x^k in GF(2^8), poly 0x11D, as a translate table."""
    out = [bytes(range(256))]
    for _ in range(45):
        out.append(out[-1].translate(ECC_F))
    return out


_POW = _gf_pow_tables()


def edc(data):
    """The CD-ROM EDC (CRC-32, reflected polynomial 0xD8018001, init 0)."""
    crc = 0
    lut = EDC_LUT
    for b in data:
        crc = (crc >> 8) ^ lut[(crc ^ b) & 0xFF]
    return crc


def _xor(a, b):
    n = len(a)
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(n, "little")


def _ecc_plan(major_count, minor_count, major_mult, minor_inc):
    """For each minor step, an itemgetter that gathers that step's byte of every major."""
    size = major_count * minor_count
    rows = []
    for minor in range(minor_count):
        idx = []
        for major in range(major_count):
            i = (major >> 1) * major_mult + (major & 1) + minor * minor_inc
            idx.append(i % size)
        rows.append(operator.itemgetter(*idx))
    return rows


_P_ROWS = _ecc_plan(86, 24, 2, 86)
_Q_ROWS = _ecc_plan(52, 43, 86, 88)


def _ecc_block(src, rows):
    """ECC-P or ECC-Q parity, vectorised across the majors.

    The reference loop computes, per major, a = f(a ^ t) over the minors and then
    b_lut[f(a) ^ b]. That is Horner's rule in GF(2^8), so a = XOR_k t_k * x^(m-k+1)
    before the final step, which is one translate per minor across all majors.
    """
    m = len(rows)
    count = None
    acc_a = acc_b = None
    for k, get in enumerate(rows):
        row = bytes(get(src))
        if count is None:
            count = len(row)
            acc_a = bytes(count)
            acc_b = bytes(count)
        acc_a = _xor(acc_a, row.translate(_POW[m - k + 1]))
        acc_b = _xor(acc_b, row)
    a = _xor(acc_a, acc_b).translate(ECC_B)
    return a + _xor(a, acc_b)


def form1_sector(lba, subheader, data):
    """A complete 2352-byte Mode 2 Form 1 sector."""
    if len(subheader) != 4 or len(data) != USER:
        raise ValueError("bad Form 1 input")
    sec = bytearray(RAW)
    sec[16:20] = subheader
    sec[20:24] = subheader
    sec[24:24 + USER] = data
    struct.pack_into("<I", sec, 2072, edc(memoryview(sec)[16:2072]))
    # Mode 2: ECC is computed with the 4-byte address header zeroed.
    body = bytes(sec[12:2076])
    p = _ecc_block(body, _P_ROWS)
    sec[2076:2248] = p
    q = _ecc_block(body + p, _Q_ROWS)
    sec[2248:2352] = q
    sec[0:16] = sector_header(lba)
    return bytes(sec)


# ---------------------------------------------------------------------------------
# the payload
# ---------------------------------------------------------------------------------

def read_varint(buf, pos):
    shift = 0
    val = 0
    while True:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, pos
        shift += 7


def load_payload(path):
    try:
        with open(path, "rb") as fh:
            blob = fh.read()
    except OSError:
        raise PatchError(
            "The patch data file %s is missing. Keep %s in the same folder as "
            "patch_goemon.py -- they come together in the download." % (path, PAYLOAD_NAME))
    if blob[:4] != MAGIC:
        raise PatchError("%s is not a patch data file for this patcher." % path)
    version, mlen = struct.unpack_from("<BI", blob, 4)
    if version != FORMAT_VERSION:
        raise PatchError("%s is format version %d; this patcher reads version %d. "
                         "Download the patcher and its data file together."
                         % (path, version, FORMAT_VERSION))
    manifest = json.loads(blob[9:9 + mlen].decode("utf-8"))
    packed = blob[9 + mlen:]
    if hashlib.sha256(packed).hexdigest() != manifest["body_sha256"]:
        raise PatchError("The patch data file is damaged (its checksum does not match). "
                         "Download it again.")
    body = zlib.decompress(packed)
    return manifest, body


def to_bits(data):
    """An STR v2 bitstream as '0'/'1' in consumption order (16-bit LE words, MSB first)."""
    if len(data) % 2:
        data = data + b"\x00"
    sw = bytearray(len(data))
    sw[0::2] = data[1::2]
    sw[1::2] = data[0::2]
    return bin(int.from_bytes(bytes(sw), "big"))[2:].zfill(len(data) * 8) if data else ""


def from_bits(bits, nbytes):
    """Inverse of to_bits, zero-padded to `nbytes`."""
    if len(bits) > nbytes * 8:
        raise PatchError("internal: bitstream does not fit its frame")
    bits = bits + "0" * (nbytes * 8 - len(bits))
    sw = int(bits, 2).to_bytes(nbytes, "big") if nbytes else b""
    out = bytearray(nbytes)
    out[0::2] = sw[1::2]
    out[1::2] = sw[0::2]
    return bytes(out)


def apply_bits(comp, body, sources):
    """Rebuild a movie frame from bit-level COPY/ADD ops.

    COPY names a bit range of the player's own frame (its untouched macroblocks,
    bit for bit); ADD takes the next bits of this component's add stream (the
    macroblocks this project re-encoded). The add stream is plain big-endian bits.
    """
    ops = body[comp["ops"][0]:comp["ops"][0] + comp["ops"][1]]
    adds = body[comp["adds"][0]:comp["adds"][0] + comp["adds"][1]]
    addbits = bin(int.from_bytes(adds, "big"))[2:].zfill(len(adds) * 8) if adds else ""
    views = [to_bits(s) for s in sources]
    parts = []
    pos = ap = 0
    while pos < len(ops):
        kind = ops[pos]
        pos += 1
        if kind == 0:
            si, pos = read_varint(ops, pos)
            off, pos = read_varint(ops, pos)
            ln, pos = read_varint(ops, pos)
            parts.append(views[si][off:off + ln])
        elif kind == 1:
            ln, pos = read_varint(ops, pos)
            parts.append(addbits[ap:ap + ln])
            ap += ln
        else:
            raise PatchError("internal: unknown bit op %d" % kind)
    return from_bits("".join(parts), comp["size"])


def apply_delta(comp, body, sources):
    """Rebuild one component from COPY/ADD ops. `sources` is a list of bytes."""
    ops = body[comp["ops"][0]:comp["ops"][0] + comp["ops"][1]]
    adds = body[comp["adds"][0]:comp["adds"][0] + comp["adds"][1]]
    out = bytearray()
    pos = 0
    ap = 0
    n = len(ops)
    while pos < n:
        kind = ops[pos]
        pos += 1
        if kind == 0:                       # COPY source, offset, length
            si, pos = read_varint(ops, pos)
            off, pos = read_varint(ops, pos)
            ln, pos = read_varint(ops, pos)
            src = sources[si]
            if off + ln > len(src):
                raise PatchError("internal: COPY past the end of %s" % comp["sources"][si])
            out += src[off:off + ln]
        elif kind == 1:                     # ADD length
            ln, pos = read_varint(ops, pos)
            out += adds[ap:ap + ln]
            ap += ln
        else:
            raise PatchError("internal: unknown delta op %d" % kind)
    return bytes(out)


# ---------------------------------------------------------------------------------
# reading the player's disc
# ---------------------------------------------------------------------------------

class Disc(object):
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "rb")
        self.size = os.path.getsize(path)

    def raw(self, lba, count=1):
        self.fh.seek(lba * RAW)
        data = self.fh.read(count * RAW)
        if len(data) != count * RAW:
            raise PatchError("The disc image ends early (at sector %d)." % lba)
        return data

    def user(self, lba, count=1):
        raw = self.raw(lba, count)
        return b"".join(raw[i * RAW + 24:i * RAW + 24 + USER] for i in range(count))

    def file_bytes(self, lba, size):
        n = -(-size // USER)
        return self.user(lba, n)[:size]

    def listing(self):
        """{name: (lba, size)} from the ISO9660 root directory."""
        pvd = self.user(16)
        if pvd[1:6] != b"CD001":
            return None
        root_lba = struct.unpack_from("<I", pvd, 156 + 2)[0]
        root_len = struct.unpack_from("<I", pvd, 156 + 10)[0]
        data = self.user(root_lba, max(1, -(-root_len // USER)))
        out = {}
        off = 0
        while off < len(data):
            reclen = data[off]
            if reclen == 0:
                off = (off // USER + 1) * USER
                continue
            lba = struct.unpack_from("<I", data, off + 2)[0]
            size = struct.unpack_from("<I", data, off + 10)[0]
            nlen = data[off + 32]
            name = data[off + 33:off + 33 + nlen].decode("ascii", "replace").split(";")[0]
            out[name] = (lba, size)
            off += reclen
        return out

    def close(self):
        self.fh.close()


def sha256_file(path, label=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    last = 0
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1 << 22)
            if not b:
                break
            h.update(b)
            done += len(b)
            if label and time.time() - last > 0.5:
                last = time.time()
                _progress("%s %3d%%" % (label, done * 100 // max(total, 1)))
    if label:
        _progress("%s 100%%" % label, end=True)
    return h.hexdigest()


def _progress(msg, end=False):
    if sys.stdout.isatty():
        sys.stdout.write("\r   " + msg + ("\n" if end else ""))
        sys.stdout.flush()
    elif end:
        print("   " + msg)


def identify(disc):
    """A best-effort description of an unexpected disc, for the error message."""
    try:
        lst = disc.listing()
    except Exception:
        return None
    if not lst:
        return None
    if "SYSTEM.CNF" in lst:
        lba, size = lst["SYSTEM.CNF"]
        try:
            cnf = disc.file_bytes(lba, min(size, 2048)).decode("ascii", "replace")
        except Exception:
            cnf = ""
        for line in cnf.splitlines():
            if line.strip().upper().startswith("BOOT"):
                return line.split("\\")[-1].split(";")[0].strip()
    return None


# ---------------------------------------------------------------------------------
# the build
# ---------------------------------------------------------------------------------

def _compress_job(args):
    name, payload = args
    stream = kgd.compress(payload)
    blob = kgd.pad_to_sector(stream)
    if len(blob) == len(stream):
        blob += b"\x00" * kgd.SECTOR       # terminator padding sector (insert.py's rule)
    return name, blob


def compress_all(jobs, workers=None):
    """Run the LZSS encoder over every job, in parallel where the OS allows it.

    The encoder is a pure function (docs/disc-system.md 6c), so the order and the
    number of processes cannot change a byte of the result.
    """
    sizes = dict((n, len(p)) for n, p in jobs)
    total = sum(sizes.values()) or 1
    pending = sorted(jobs, key=lambda j: -len(j[1]))      # longest first
    results = {}
    t0 = time.time()
    done = [0]

    def took(name, blob):
        results[name] = blob
        done[0] += sizes[name]
        _progress("compressing %3d%%" % (done[0] * 100 // total))

    if workers != 1:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=workers) as ex:
                for name, blob in ex.map(_compress_job, pending):
                    took(name, blob)
        except Exception:
            # No usable process pool (restricted sandbox, frozen interpreter, a
            # worker that died): finish whatever is left in this process.
            pass
    for job in pending:
        if job[0] not in results:
            took(*_compress_job(job))
    _progress("compressing 100%% (%.0f s)" % (time.time() - t0), end=True)
    return results


def build(manifest, body, disc, out_path, workers=None, log=print):
    """Build the English image at `out_path` from `disc`. Returns its SHA-256."""
    comps = {c["name"]: c for c in manifest["components"]}

    # --- sources: everything read from the player's own disc ---------------------
    files = {}
    for name, info in manifest["files"].items():
        data = disc.file_bytes(info["lba"], info["size"])
        if hashlib.sha256(data).hexdigest() != info["sha256"]:
            raise PatchError("%s on your disc does not match the expected original. "
                             "The disc image may be damaged." % name)
        files[name] = data
    exe = files["SLPM_861.55"]
    kgd_orig = files["GOEMON.KGD"]
    table = []
    for i in range(kgd.EXE_TABLE_COUNT):
        table.append(kgd.Chunk(i, *struct.unpack_from(
            "<4I", exe, kgd.EXE_TABLE_OFF + i * 16)))

    cache = {}

    def source(name):
        if name in cache:
            return cache[name]
        kind, _, arg = name.partition(":")
        if kind == "chunk":
            c = table[int(arg)]
            data = kgd.decompress(kgd_orig[c.offset:c.offset + c.sectors * kgd.SECTOR])
            if len(data) != c.dsize:
                raise PatchError("Chunk %d of your disc did not decompress cleanly." % c.index)
        elif kind == "file":
            data = files[arg]
        elif kind == "sectors":
            lba, count = (int(x) for x in arg.split(","))
            data = disc.user(lba, count)
        elif kind == "strframe":
            # one movie frame, demultiplexed: each video sector's 2016 bytes after
            # its 32-byte STR header, in the order listed
            data = b"".join(disc.user(int(x))[32:2048] for x in arg.split(","))
        elif kind == "comp":
            data = component(arg)
        else:
            raise PatchError("internal: unknown source %r" % name)
        cache[name] = data
        return data

    built = {}

    def component(name):
        if name in built:
            return built[name]
        c = comps[name]
        if c["kind"] == "delta":
            data = apply_delta(c, body, [source(s) for s in c["sources"]])
        elif c["kind"] == "bits":
            data = apply_bits(c, body, [source(s) for s in c["sources"]])
        else:
            raise PatchError("internal: component %s has kind %s" % (name, c["kind"]))
        if len(data) != c["size"] or hashlib.sha256(data).hexdigest() != c["sha256"]:
            raise PatchError("internal: %s did not rebuild to the expected bytes" % name)
        built[name] = data
        return data

    log("-> Applying the translation to the decompressed game data...")
    t0 = time.time()
    jobs = []
    for c in manifest["components"]:
        if c["kind"] == "lzss":
            jobs.append((c["name"], component(c["of"])))
        elif c["kind"] in ("delta", "bits"):
            component(c["name"])
    log("   %d parts rebuilt from your disc (%.0f s)" % (len(built), time.time() - t0))

    log("-> Re-compressing %d chunks of game data (this is the slow step)..." % len(jobs))
    blobs = compress_all(jobs, workers=workers)
    for c in manifest["components"]:
        if c["kind"] != "lzss":
            continue
        blob = blobs[c["name"]]
        if len(blob) != c["size"] or hashlib.sha256(blob).hexdigest() != c["sha256"]:
            raise PatchError("internal: %s re-compressed to different bytes than the "
                             "English build. Please report this." % c["name"])
        built[c["name"]] = blob
    cache.clear()

    log("-> Writing the English disc image...")
    h = hashlib.sha256()
    total = manifest["output"]["sectors"]
    written = 0
    t0 = time.time()
    last = 0
    with open(out_path, "wb") as out:
        for op in manifest["recipe"]:
            if op[0] == "copy":
                _, t, n, s = op
                step = 512
                for k in range(0, n, step):
                    m = min(step, n - k)
                    raw = bytearray(disc.raw(s + k, m))
                    if s != t:
                        for j in range(m):
                            raw[j * RAW:j * RAW + 16] = sector_header(t + k + j)
                    out.write(raw)
                    h.update(raw)
                    written += m
                    if time.time() - last > 0.5:
                        last = time.time()
                        _progress("writing %3d%%" % (written * 100 // total))
            elif op[0] == "form1":
                _, t, n, name, off, sub = op
                data = built[name]
                subh = bytes.fromhex(sub)
                for j in range(n):
                    a = off + j * USER
                    chunk = data[a:a + USER]
                    if len(chunk) < USER:
                        chunk = chunk + b"\x00" * (USER - len(chunk))
                    sec = form1_sector(t + j, subh, chunk)
                    out.write(sec)
                    h.update(sec)
                written += n
            else:
                raise PatchError("internal: unknown recipe op %r" % op[0])
    _progress("writing 100%% (%.0f s)" % (time.time() - t0), end=True)
    if written != total:
        raise PatchError("internal: wrote %d sectors, expected %d" % (written, total))
    return h.hexdigest()


def write_cue(bin_path):
    cue = os.path.splitext(bin_path)[0] + ".cue"
    with open(cue, "wb") as fh:
        fh.write(('FILE "%s" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n'
                  % os.path.basename(bin_path)).encode("utf-8"))
    return cue


# ---------------------------------------------------------------------------------
# the command
# ---------------------------------------------------------------------------------

USAGE = """\
usage: python3 patch_goemon.py <original .bin> [output folder]

  <original .bin>   your rip of Ganbare Goemon: Kuru nara Koi! Ayashige Ikka no
                    Kuroi Kage (Japan), SLPM-86155 -- the .bin file, not the .cue
  [output folder]   where to write the English disc (default: the folder the
                    original is in). Your original is never modified.
"""


def check_input(path, manifest):
    """Refuse anything but the exact original disc. Returns the resolved path."""
    want = manifest["input"]
    if not os.path.exists(path):
        raise PatchError("There is no file at %s." % path)
    if os.path.isdir(path):
        raise PatchError("%s is a folder. Give the path to the .bin file inside it." % path)
    low = path.lower()
    if low.endswith(".cue"):
        hint = ""
        try:
            with open(path, "r", errors="replace") as fh:
                for line in fh:
                    if line.strip().upper().startswith("FILE") and '"' in line:
                        hint = os.path.join(os.path.dirname(path), line.split('"')[1])
                        break
        except OSError:
            pass
        raise PatchError(
            "That is the .cue file. Give the patcher the .bin file instead%s."
            % ((": try\n     " + hint) if hint else ""))
    size = os.path.getsize(path)
    if size != want["size"]:
        if size == 170387 * USER:
            raise PatchError(
                "This looks like a 2048-byte-per-sector .iso image. The patcher needs "
                "the raw .bin (2352 bytes per sector, %s bytes), as Redump distributes "
                "it." % format(want["size"], ","))
        if low.endswith((".7z", ".zip", ".rar", ".ecm", ".chd", ".pbp")):
            raise PatchError("That file is compressed or converted (%s). Extract or "
                             "convert it back to the original .bin/.cue first."
                             % os.path.splitext(low)[1])
        raise PatchError(
            "This file is %s bytes; the Japanese disc this patch is for is %s bytes. "
            "It is a different disc, or a different kind of image."
            % (format(size, ","), format(want["size"], ",")))
    return path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    workers = None
    if "--single-core" in argv:
        argv.remove("--single-core")
        workers = 1
    payload = os.path.join(HERE, PAYLOAD_NAME)
    if "--payload" in argv:                  # testing: a payload other than the one beside us
        k = argv.index("--payload")
        if k + 1 >= len(argv):
            sys.stdout.write(USAGE)
            return 2
        payload = argv[k + 1]
        del argv[k:k + 2]
    if not argv or argv[0] in ("-h", "--help") or len(argv) > 2:
        sys.stdout.write(USAGE)
        return 0 if argv and argv[0] in ("-h", "--help") else 2
    src = argv[0]
    print("\n== Mystical Ninja: Shadey Business -- English patch (rebuild from your disc) ==\n")
    try:
        manifest, body = load_payload(payload)
        check_input(src, manifest)
        out_dir = argv[1] if len(argv) > 1 else os.path.dirname(os.path.abspath(src))
        if os.path.exists(out_dir) and not os.path.isdir(out_dir):
            raise PatchError("%s is a file, not a folder. The second argument is the "
                             "folder to write the English disc into." % out_dir)
        out_bin = os.path.join(out_dir, manifest["output"]["name"])
        if os.path.realpath(out_bin) == os.path.realpath(src):
            raise PatchError("The output would overwrite your original. Choose another "
                             "output folder.")
        if os.path.exists(out_bin):
            raise PatchError("%s already exists. Move or delete it first; the patcher "
                             "never overwrites a file." % out_bin)
        need = manifest["output"]["size"] + (64 << 20)
        probe = os.path.abspath(out_dir)
        while not os.path.exists(probe):          # the folder may not exist yet
            probe = os.path.dirname(probe)
        free = shutil.disk_usage(probe).free
        if free < need:
            raise PatchError("Not enough disk space in %s: the English disc needs %d MB "
                             "and there are %d MB free." % (out_dir, need >> 20, free >> 20))

        print("-> Checking your disc (SHA-256)...")
        got = sha256_file(src, "reading")
        if got == manifest["output"]["sha256"]:
            raise PatchError("This disc is already the English version -- there is "
                             "nothing to do. Give the patcher the original Japanese "
                             "disc if you want to rebuild it.")
        if got != manifest["input"]["sha256"]:
            disc = Disc(src)
            boot = identify(disc)
            disc.close()
            extra = ""
            if boot and boot.upper().replace("_", "-").replace(".", "") != "SLPM-86155":
                extra = ("\n   The disc says it boots %s, which is not SLPM-86155 (the "
                         "Konami the Best reprint, SLPM-86572, is a different disc and "
                         "is not supported)." % boot)
            raise PatchError(
                "This is not the disc this patch was made for.%s\n"
                "   expected SHA-256: %s\n"
                "   your file:        %s\n"
                "   The patch needs an unmodified Redump-verified rip of SLPM-86155 "
                "(the original 1998 pressing). A bad dump, a different region or "
                "pressing, or a file that was already patched by something else "
                "will not match." % (extra, manifest["input"]["sha256"], got))
        print("   OK: this is the original SLPM-86155 disc.")

        os.makedirs(out_dir, exist_ok=True)
        tmp = out_bin + ".partial"
        disc = Disc(src)
        try:
            t0 = time.time()
            sha = build(manifest, body, disc, tmp, workers=workers)
        except BaseException:
            disc.close()
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        disc.close()
        print("-> Verifying the English disc...")
        if sha != manifest["output"]["sha256"]:
            os.remove(tmp)
            raise PatchError(
                "The rebuilt disc does not match the English build (got %s, expected "
                "%s). Nothing was written. Please report this, with your Python "
                "version (%s) and operating system." % (sha, manifest["output"]["sha256"],
                                                         sys.version.split()[0]))
        os.replace(tmp, out_bin)
        cue = write_cue(out_bin)
        print("   OK: SHA-256 %s matches the English build." % sha)
        print("\nDone in %.0f s. Wrote:\n   %s\n   %s\nLoad the .cue in your emulator."
              % (time.time() - t0, out_bin, cue))
        return 0
    except PatchError as e:
        print("\nerror: %s\n\nNothing was written." % e)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted. Nothing was written.")
        return 130


if __name__ == "__main__":
    sys.exit(main())

# ---------------------------------------------------------------------------------
# Payload format (goemon-en.ggp), version 1
#
#   "GGRP" | u8 version | u32 manifest length | manifest (UTF-8 JSON) | zlib(body)
#
# The manifest names every input the build reads from the player's disc
# (`files`, with SHA-256), every component it builds, and a `recipe` that lays the
# output image out sector by sector:
#
#   ["copy",  t, n, s]              raw sectors s..s+n-1 of the player's disc,
#                                   re-addressed to t..t+n-1 (only the 4-byte
#                                   header changes; Mode 2 EDC/ECC exclude it)
#   ["form1", t, n, name, off, sub] n Form 1 sectors whose 2048-byte user data
#                                   comes from component `name` at byte `off`,
#                                   subheader `sub`, EDC/ECC computed here
#
# A component is either
#   {"kind": "delta", "sources": [...], "ops": [off, len], "adds": [off, len]}
#       ops are varint-coded: 0 src off len = COPY from a source; 1 len = ADD the
#       next len bytes of the component's add stream. Sources are read from the
#       player's disc: "chunk:N" (GOEMON.KGD chunk N, decompressed), "file:NAME",
#       "sectors:LBA,COUNT" (Form 1 user data of a sector range).
#   {"kind": "bits", "sources": ["strframe:LBA,LBA,..."], "ops": .., "adds": ..}
#       a movie frame rebuilt at the BIT level: 0 src off len = COPY bits of the
#       player's own frame (its untouched macroblocks, verbatim); 1 len = ADD bits
#       (the macroblocks this project re-encoded). Other components may read it as
#       the source "comp:NAME".
#   {"kind": "lzss", "of": "<delta component>"}
#       the delta component re-compressed with kgd.compress and sector-padded.
# Every component carries its size and SHA-256, so a fault is caught where it
# happens rather than as a wrong final hash.
# ---------------------------------------------------------------------------------
