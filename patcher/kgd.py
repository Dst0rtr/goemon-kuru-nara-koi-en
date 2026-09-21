#!/usr/bin/env python3
"""
GOEMON.KGD container and LZSS codec.

Owned by `rom-hacker`. Findings and their confidence levels live in
docs/text-system.md, section "Container internals". This module is the toolchain;
docs/prototypes/kgd_lzss.py is the superseded recon prototype.

Container
---------
GOEMON.KGD is 8,134 sectors of 2,048 bytes holding 86 LZSS streams, packed with no
gaps: chunk i starts on a sector boundary and occupies exactly ceil(len/2048)
sectors, the tail of the last sector zero-padded.

The directory is NOT in the container. It is a hardcoded 86 x 16-byte array in
SLPM_861.55 at file offset 0xCAD3C (RAM 0x800DA53C):

    u32 start_sector      sector index within GOEMON.KGD
    u32 compressed_size   bytes, excluding the zero padding
    u32 decompressed_size bytes
    u32 unknown           see docs/text-system.md; not a checksum

Any repack that changes a chunk's compressed size must rewrite this array (and the
start sectors of every chunk after it). There is no checksum anywhere -- see
`python tools/kgd.py checksum` and docs/text-system.md.

Codec
-----
LZSS over a 4,096-byte window, Okumura LZSS.C lineage:

  * one flag byte governs the next eight tokens, LSB first
  * flag bit 1 -> literal byte
  * flag bit 0 -> two-byte back-reference
        distance = b0 | ((b1 & 0xF0) << 4)      1..4078, backwards from write head
        length   = (b1 & 0x0F) + 3              3..18
  * distance == 0 ends the stream. The original packer never writes that token:
    the stream simply stops at compressed_size and the zero padding that fills the
    rest of the last sector decodes as distance 0. Verified in all 86 chunks -- the
    terminator lands on the EXE table's compressed_size (or one byte past it, when
    the stream ended on a full eight-token group and the zero padding supplies the
    flag byte too), and the output it produces equals the EXE table's
    decompressed_size, 86/86.

Overlapping copies (distance < length) occur and must be byte-wise.

Encoder
-------
`compress` reproduces the original encoder's choices for ~99.97% of tokens but is
NOT bit-exact -- see `verify`. The match finder is LZSS.C's binary search tree over
a 4,096-entry ring with an 18-byte lookahead, greedy, unsigned byte comparison,
which is what the original's tie-breaks and its 4,078 maximum distance both imply.
The residual difference is documented in docs/text-system.md.

Usage
-----
    python tools/kgd.py list      iso/files/GOEMON.KGD
    python tools/kgd.py info      iso/files/GOEMON.KGD 5
    python tools/kgd.py extract   iso/files/GOEMON.KGD build/kgd/
    python tools/kgd.py decompress iso/files/GOEMON.KGD 5 build/chunk05.bin
    python tools/kgd.py compress  build/chunk05.bin build/chunk05.lz
    python tools/kgd.py verify    iso/files/GOEMON.KGD [--jobs N] [--chunks 0,5,41]
    python tools/kgd.py checksum  iso/files/GOEMON.KGD
"""

import argparse
import os
import struct
import sys

SECTOR = 2048

# --- codec constants (LZSS.C names kept so the lineage stays legible) -------------
N = 4096            # ring / window size
F = 18              # lookahead, i.e. maximum match length
THRESHOLD = 2       # match_length must exceed this to be worth encoding
NIL = N             # tree null
MAX_DIST = N - F    # 4078; the encoder can never name a farther match

# --- EXE directory ---------------------------------------------------------------
EXE_NAME = "SLPM_861.55"
EXE_TABLE_OFF = 0xCAD3C         # file offset in SLPM_861.55
EXE_TABLE_RAM = 0x800DA53C      # same array at run time
EXE_TABLE_COUNT = 86
CHUNK_MAGIC = b"\x5f\x1c\x00\x00"   # first flag byte + the shared header preamble


class Chunk(object):
    __slots__ = ("index", "start_sector", "csize", "dsize", "field3", "sectors")

    def __init__(self, index, start_sector, csize, dsize, field3):
        self.index = index
        self.start_sector = start_sector
        self.csize = csize
        self.dsize = dsize
        self.field3 = field3
        self.sectors = -(-csize // SECTOR) if csize else 0

    @property
    def offset(self):
        return self.start_sector * SECTOR


# --------------------------------------------------------------------------------
# container
# --------------------------------------------------------------------------------

def find_exe(kgd_path, explicit=None):
    """Locate SLPM_861.55 next to the container unless told otherwise."""
    if explicit:
        return explicit
    cand = os.path.join(os.path.dirname(os.path.abspath(kgd_path)), EXE_NAME)
    return cand if os.path.exists(cand) else None


def read_directory(exe_path):
    """The 86 x 16-byte chunk directory hardcoded in the executable."""
    with open(exe_path, "rb") as fh:
        exe = fh.read()
    out = []
    for i in range(EXE_TABLE_COUNT):
        rec = exe[EXE_TABLE_OFF + i * 16:EXE_TABLE_OFF + i * 16 + 16]
        if len(rec) != 16:
            raise ValueError("%s is too short for the chunk directory" % exe_path)
        start, csize, dsize, x = struct.unpack("<4I", rec)
        out.append(Chunk(i, start, csize, dsize, x))
    return out


def scan_directory(kgd):
    """Fallback: recover the chunk table from the container alone.

    Every chunk begins on a sector boundary with CHUNK_MAGIC, and the compressed
    size is where the end-of-stream terminator lands. Used when the EXE is absent
    and as an independent cross-check of the EXE table.
    """
    starts = [i for i in range(len(kgd) // SECTOR)
              if kgd[i * SECTOR:i * SECTOR + 4] == CHUNK_MAGIC]
    ends = starts[1:] + [len(kgd) // SECTOR]
    out = []
    for i, (s, e) in enumerate(zip(starts, ends)):
        raw = kgd[s * SECTOR:e * SECTOR]
        dsize, csize = decompress(raw, want_sizes=True)
        out.append(Chunk(i, s, csize, dsize, None))
    return out


def chunk_table(kgd, exe_path=None):
    if exe_path:
        return read_directory(exe_path)
    return scan_directory(kgd)


def chunk_bytes(kgd, chunk):
    """One chunk's compressed stream, plus a few padding bytes so the decoder
    always has the distance==0 terminator the packer left to the padding."""
    return kgd[chunk.offset:chunk.offset + chunk.csize + 8]


# --------------------------------------------------------------------------------
# decoder
# --------------------------------------------------------------------------------

def decompress(data, want_sizes=False):
    """Decode one LZSS stream. Stops at the distance==0 terminator.

    With want_sizes, returns (decompressed_size, compressed_size). The packer
    writes no terminator, so the real stream ends either at the terminator token
    (the zero padding supplied its two bytes) or one byte earlier (the padding
    supplied the flag byte as well, which is only possible when the terminator is
    the first token of its group and that flag byte is zero).
    """
    out = bytearray()
    i = 0
    n = len(data)
    append = out.append
    while i < n:
        flagpos = i
        flags = data[i]
        i += 1
        for bit in range(8):
            if i >= n:
                return (len(out), i) if want_sizes else bytes(out)
            if flags >> bit & 1:
                append(data[i])
                i += 1
            else:
                if i + 1 >= n:
                    return (len(out), i) if want_sizes else bytes(out)
                b0 = data[i]
                b1 = data[i + 1]
                dist = b0 | ((b1 & 0xF0) << 4)
                if dist == 0:                       # end of stream
                    if not want_sizes:
                        return bytes(out)
                    end = flagpos if (bit == 0 and flags == 0) else i
                    return (len(out), end)
                length = (b1 & 0x0F) + 3
                i += 2
                p = len(out) - dist
                if p < 0:
                    raise ValueError("window underflow at output offset %d "
                                     "(distance %d)" % (len(out), dist))
                for k in range(length):
                    append(out[p + k])
    return (len(out), i) if want_sizes else bytes(out)


# --------------------------------------------------------------------------------
# encoder
# --------------------------------------------------------------------------------

class _Tree(object):
    """LZSS.C's binary search tree over the ring, verbatim apart from names."""

    __slots__ = ("text", "lson", "rson", "dad", "match_length", "match_position")

    def __init__(self, fill=0x20):
        self.text = bytearray(N + F - 1)
        for i in range(N - F):
            self.text[i] = fill
        self.lson = [NIL] * (N + 1)
        self.rson = [NIL] * (N + 257)
        self.dad = [NIL] * (N + 1)
        self.match_length = 0
        self.match_position = 0

    def insert(self, r):
        text = self.text
        lson = self.lson
        rson = self.rson
        dad = self.dad
        cmp_ = 1
        p = N + 1 + text[r]
        rson[r] = lson[r] = NIL
        self.match_length = 0
        while True:
            if cmp_ >= 0:
                q = rson[p]
                if q != NIL:
                    p = q
                else:
                    rson[p] = r
                    dad[r] = p
                    return
            else:
                q = lson[p]
                if q != NIL:
                    p = q
                else:
                    lson[p] = r
                    dad[r] = p
                    return
            i = 1
            while i < F:
                cmp_ = text[r + i] - text[p + i]
                if cmp_:
                    break
                i += 1
            else:
                cmp_ = 0
            if i > self.match_length:
                self.match_position = p
                self.match_length = i
                if i >= F:
                    break
        dad[r] = dad[p]
        lson[r] = lson[p]
        rson[r] = rson[p]
        dad[lson[p]] = r
        dad[rson[p]] = r
        if rson[dad[p]] == p:
            rson[dad[p]] = r
        else:
            lson[dad[p]] = r
        dad[p] = NIL

    def delete(self, p):
        lson = self.lson
        rson = self.rson
        dad = self.dad
        if dad[p] == NIL:
            return
        if rson[p] == NIL:
            q = lson[p]
        elif lson[p] == NIL:
            q = rson[p]
        else:
            q = lson[p]
            if rson[q] != NIL:
                while rson[q] != NIL:
                    q = rson[q]
                rson[dad[q]] = lson[q]
                dad[lson[q]] = dad[q]
                lson[q] = lson[p]
                dad[lson[p]] = q
            rson[q] = rson[p]
            dad[rson[p]] = q
        dad[q] = dad[p]
        if rson[dad[p]] == p:
            rson[dad[p]] = q
        else:
            lson[dad[p]] = q
        dad[p] = NIL


def compress(data):
    """Encode one LZSS stream, terminator included, no sector padding."""
    t = _Tree()
    text = t.text
    out = bytearray()
    code = bytearray(17)
    code[0] = 0
    cp = 1
    mask = 1

    s = 0
    r = N - F
    src = 0
    n = len(data)
    length = 0
    while length < F and src < n:
        text[r + length] = data[src]
        src += 1
        length += 1
    if length == 0:
        return bytes(b"\x00\x00\x00")           # empty stream: flag + terminator

    for i in range(1, F + 1):
        t.insert((r - i) & (N - 1))
    t.insert(r)

    while True:
        ml = t.match_length
        if ml > length:
            ml = length
        if ml <= THRESHOLD:
            ml = 1
            code[0] |= mask
            code[cp] = text[r]
            cp += 1
        else:
            v = (r - t.match_position) & (N - 1)
            code[cp] = v & 0xFF
            code[cp + 1] = ((v >> 4) & 0xF0) | (ml - (THRESHOLD + 1))
            cp += 2
        mask = (mask << 1) & 0xFF
        if mask == 0:
            out += code[:cp]
            code[0] = 0
            cp = 1
            mask = 1
        last = ml
        i = 0
        while i < last and src < n:
            c = data[src]
            src += 1
            t.delete(s)
            text[s] = c
            if s < F - 1:
                text[s + N] = c
            s = (s + 1) & (N - 1)
            r = (r + 1) & (N - 1)
            t.insert(r)
            i += 1
        while i < last:
            i += 1
            t.delete(s)
            s = (s + 1) & (N - 1)
            r = (r + 1) & (N - 1)
            length -= 1
            if length:
                t.insert(r)
        if length <= 0:
            break

    # end of stream: a back-reference with distance 0
    code[cp] = 0
    code[cp + 1] = 0
    cp += 2
    out += code[:cp]
    return bytes(out)


def pad_to_sector(stream):
    rem = len(stream) % SECTOR
    return stream + b"\x00" * (SECTOR - rem) if rem else stream


# --------------------------------------------------------------------------------
# sub-file index inside a decompressed chunk
# --------------------------------------------------------------------------------

def segment_table(chunk_data):
    """The u32 offset table every decompressed chunk opens with.

    entry[0] is the table's own byte length, so the entry count is entry[0] // 4
    (always 7 in this game). Segment k spans entry[k] .. entry[k+1]; the last
    segment runs to the end of the chunk.
    """
    first = struct.unpack_from("<I", chunk_data, 0)[0]
    if first % 4 or not (4 <= first <= 256):
        raise ValueError("offset table length %d is not plausible" % first)
    count = first // 4
    entries = list(struct.unpack_from("<%dI" % count, chunk_data, 0))
    bounds = entries + [len(chunk_data)]
    segments = [(entries[k], bounds[k + 1] - entries[k]) for k in range(count)]
    return entries, segments


# --------------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------------

def _load(args):
    with open(args.kgd, "rb") as fh:
        kgd = fh.read()
    exe = find_exe(args.kgd, getattr(args, "exe", None))
    return kgd, exe


def cmd_list(args):
    kgd, exe = _load(args)
    table = chunk_table(kgd, exe)
    print("%s: %d bytes, %d sectors" % (args.kgd, len(kgd), len(kgd) // SECTOR))
    print("directory: %s" % (exe if exe else "recovered by scanning the container"))
    print("  #  sector  sectors   compressed  decompressed  ratio  field3")
    for c in table:
        print("%3d  %6d  %7d  %11d  %12d  %5.2f  %6s"
              % (c.index, c.start_sector, c.sectors, c.csize, c.dsize,
                 c.dsize / float(c.csize) if c.csize else 0,
                 "-" if c.field3 is None else c.field3))
    return 0


def cmd_info(args):
    kgd, exe = _load(args)
    table = chunk_table(kgd, exe)
    c = table[args.index]
    data = decompress(chunk_bytes(kgd, c))
    entries, segments = segment_table(data)
    print("chunk %d: sector %d, %d compressed -> %d decompressed"
          % (c.index, c.start_sector, c.csize, c.dsize))
    if len(data) != c.dsize:
        print("  WARNING: decoded %d bytes, directory says %d" % (len(data), c.dsize))
    print("  offset table (%d entries): %s" % (len(entries), entries))
    for k, (off, size) in enumerate(segments):
        head = data[off:off + 16].hex(" ")
        print("  segment %d  0x%06X  %9d bytes  %s" % (k, off, size, head))
    return 0


def cmd_decompress(args):
    kgd, exe = _load(args)
    table = chunk_table(kgd, exe)
    c = table[args.index]
    data = decompress(chunk_bytes(kgd, c))
    with open(args.out, "wb") as fh:
        fh.write(data)
    print("chunk %d -> %s (%d bytes)" % (c.index, args.out, len(data)))
    return 0


def cmd_extract(args):
    kgd, exe = _load(args)
    table = chunk_table(kgd, exe)
    os.makedirs(args.outdir, exist_ok=True)
    for c in table:
        data = decompress(chunk_bytes(kgd, c))
        path = os.path.join(args.outdir, "chunk%02d.bin" % c.index)
        with open(path, "wb") as fh:
            fh.write(data)
    print("wrote %d chunks to %s" % (len(table), args.outdir))
    return 0


def cmd_compress(args):
    with open(args.src, "rb") as fh:
        data = fh.read()
    stream = compress(data)
    if decompress(stream) != data:
        print("ERROR: the encoder's output does not decode back to the input")
        return 1
    with open(args.out, "wb") as fh:
        fh.write(stream)
    print("%s (%d) -> %s (%d, %.1f%%)"
          % (args.src, len(data), args.out, len(stream),
             100.0 * len(stream) / len(data) if data else 0))
    return 0


def _verify_one(job):
    path, off, csize, dsize, index = job
    with open(path, "rb") as fh:
        fh.seek(off)
        raw = fh.read(csize + 8)
    data = decompress(raw)
    ok_dsize = (len(data) == dsize) if dsize is not None else None
    stream = compress(data)
    # measure our stream the packer's way: without the terminator the padding gives
    # away for free.
    _, mylen = decompress(stream, want_sizes=True)
    exact = (mylen == csize) and (stream[:csize] == raw[:csize])
    roundtrip = decompress(stream) == data
    return index, ok_dsize, exact, roundtrip, csize, mylen


def cmd_verify(args):
    kgd, exe = _load(args)
    table = chunk_table(kgd, exe)
    if args.chunks:
        want = set(int(x) for x in args.chunks.split(","))
        table = [c for c in table if c.index in want]
    jobs = [(args.kgd, c.offset, c.csize, c.dsize, c.index) for c in table]

    results = []
    if args.jobs and args.jobs > 1:
        import multiprocessing
        pool = multiprocessing.Pool(args.jobs)
        try:
            for res in pool.imap_unordered(_verify_one, jobs):
                results.append(res)
                print("  chunk %2d done" % res[0], file=sys.stderr)
        finally:
            pool.close()
            pool.join()
    else:
        for job in jobs:
            results.append(_verify_one(job))
    results.sort()

    exact = sum(1 for r in results if r[2])
    rt = sum(1 for r in results if r[3])
    dl = sum(1 for r in results if r[1])
    smaller = sum(1 for r in results if r[5] <= r[4])
    print("  #   dsize  byte-identical  round-trip     original      ours   delta")
    for index, ok_dsize, ex, rtok, olen, nlen in results:
        print("%3d  %6s  %14s  %10s  %11d  %8d  %+6d"
              % (index, "ok" if ok_dsize else "FAIL",
                 "yes" if ex else "no", "yes" if rtok else "FAIL",
                 olen, nlen, nlen - olen))
    n = len(results)
    print()
    print("decompressed size matches the EXE directory : %d/%d" % (dl, n))
    print("compress(decompress(chunk)) == chunk        : %d/%d" % (exact, n))
    print("decompress(compress(x)) == x                : %d/%d" % (rt, n))
    print("our stream no larger than the original      : %d/%d" % (smaller, n))
    return 0 if (rt == n and smaller == n) else 1


def cmd_checksum(args):
    """Everything we looked at when asking whether the container is integrity-checked."""
    import zlib
    kgd, exe = _load(args)
    table = chunk_table(kgd, exe)
    scanned = scan_directory(kgd)
    print("1. container-internal header")
    print("   Every chunk's decompressed header is a u32 offset table whose first")
    print("   entry is the table's own length; every entry is a monotonically")
    print("   increasing offset inside the chunk. No spare word.")
    bad = 0
    for c in table:
        data = decompress(chunk_bytes(kgd, c))
        e, _ = segment_table(data)
        if list(e) != sorted(e) or e[-1] > len(data):
            bad += 1
    print("   non-monotonic or out-of-range offset tables: %d/%d" % (bad, len(table)))

    print("2. per-chunk trailer")
    nz = 0
    for c in table:
        tail = kgd[c.offset + c.csize + 2:c.offset + c.sectors * SECTOR]
        if any(tail):
            nz += 1
    print("   chunks whose post-terminator padding is not all zero: %d/%d"
          % (nz, len(table)))

    print("3. EXE directory field 3")
    vals = [c.field3 for c in table if c.field3 is not None]
    if vals:
        zeros = sum(1 for v in vals if v == 0)
        print("   %d/%d entries are 0 and every value is < 0x%X; identical chunks"
              % (zeros, len(vals), max(vals) + 1))
        print("   (15 and 17) carry identical values. Not a checksum -- see below.")
        hits = {"sum8_c": 0, "sum8_d": 0, "xor32_c": 0, "sum32_c": 0,
                "xor32_d": 0, "sum32_d": 0, "crc32_c": 0, "crc32_d": 0,
                "adler32_d": 0}
        for c in table:
            comp = kgd[c.offset:c.offset + c.csize]
            data = decompress(chunk_bytes(kgd, c))
            probes = {
                "sum8_c": sum(comp), "sum8_d": sum(data),
                "crc32_c": zlib.crc32(comp), "crc32_d": zlib.crc32(data),
                "adler32_d": zlib.adler32(data),
            }
            a = b = 0
            for k in range(0, len(comp) - 3, 4):
                v = int.from_bytes(comp[k:k + 4], "little")
                a ^= v
                b = (b + v) & 0xFFFFFFFF
            probes["xor32_c"] = a
            probes["sum32_c"] = b
            a = b = 0
            for k in range(0, len(data) - 3, 4):
                v = int.from_bytes(data[k:k + 4], "little")
                a ^= v
                b = (b + v) & 0xFFFFFFFF
            probes["xor32_d"] = a
            probes["sum32_d"] = b
            for k, v in probes.items():
                if v == c.field3 or (v & 0xFFFF) == c.field3 \
                        or (v & 0xFFFFFFFF) == c.field3:
                    hits[k] += 1
        print("   checksum formulas matching field 3: %s" % hits)

    print("4. directory cross-check")
    mism = sum(1 for a, b in zip(table, scanned)
               if a.start_sector != b.start_sector or a.csize != b.csize
               or a.dsize != b.dsize)
    print("   EXE directory vs. sizes recovered from the streams: %d mismatches"
          % mism)
    print()
    print("Conclusion: no integrity check. The only thing that must stay consistent")
    print("with the container is the EXE directory at 0x%X." % EXE_TABLE_OFF)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="GOEMON.KGD container and LZSS codec",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    ap.add_argument("--exe", help="path to SLPM_861.55 (default: next to the KGD)")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("list"); p.add_argument("kgd"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("info"); p.add_argument("kgd"); p.add_argument("index", type=int)
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("decompress"); p.add_argument("kgd")
    p.add_argument("index", type=int); p.add_argument("out")
    p.set_defaults(fn=cmd_decompress)
    p = sub.add_parser("extract"); p.add_argument("kgd"); p.add_argument("outdir")
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("compress"); p.add_argument("src"); p.add_argument("out")
    p.set_defaults(fn=cmd_compress)
    p = sub.add_parser("verify"); p.add_argument("kgd")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--chunks", help="comma-separated chunk indices")
    p.set_defaults(fn=cmd_verify)
    p = sub.add_parser("checksum"); p.add_argument("kgd")
    p.set_defaults(fn=cmd_checksum)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 2
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
