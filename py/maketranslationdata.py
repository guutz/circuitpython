"""
Process raw qstr file and output qstr data with length, hash and data bytes.

This script works with Python 2.7, 3.3 and 3.4.

For documentation about the format of compressed translated strings, see
supervisor/shared/translate/translate.h
"""

from __future__ import print_function

import bisect
from dataclasses import dataclass
import re
import sys

import collections
import gettext
import os.path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(errors="backslashreplace")

py = os.path.dirname(sys.argv[0])
top = os.path.dirname(py)

sys.path.append(os.path.join(top, "tools/huffman"))

import huffman

# Python 2/3 compatibility:
#   - iterating through bytes is different
#   - codepoint2name lives in a different module
import platform

if platform.python_version_tuple()[0] == "2":
    bytes_cons = lambda val, enc=None: bytearray(val)
    from htmlentitydefs import codepoint2name
elif platform.python_version_tuple()[0] == "3":
    bytes_cons = bytes
    from html.entities import codepoint2name
# end compatibility code

codepoint2name[ord("-")] = "hyphen"

# add some custom names to map characters that aren't in HTML
codepoint2name[ord(" ")] = "space"
codepoint2name[ord("'")] = "squot"
codepoint2name[ord(",")] = "comma"
codepoint2name[ord(".")] = "dot"
codepoint2name[ord(":")] = "colon"
codepoint2name[ord(";")] = "semicolon"
codepoint2name[ord("/")] = "slash"
codepoint2name[ord("%")] = "percent"
codepoint2name[ord("#")] = "hash"
codepoint2name[ord("(")] = "paren_open"
codepoint2name[ord(")")] = "paren_close"
codepoint2name[ord("[")] = "bracket_open"
codepoint2name[ord("]")] = "bracket_close"
codepoint2name[ord("{")] = "brace_open"
codepoint2name[ord("}")] = "brace_close"
codepoint2name[ord("*")] = "star"
codepoint2name[ord("!")] = "bang"
codepoint2name[ord("\\")] = "backslash"
codepoint2name[ord("+")] = "plus"
codepoint2name[ord("$")] = "dollar"
codepoint2name[ord("=")] = "equals"
codepoint2name[ord("?")] = "question"
codepoint2name[ord("@")] = "at_sign"
codepoint2name[ord("^")] = "caret"
codepoint2name[ord("|")] = "pipe"
codepoint2name[ord("~")] = "tilde"

C_ESCAPES = {
    "\a": "\\a",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\v": "\\v",
    "'": "\\'",
    '"': '\\"',
}

# this must match the equivalent function in qstr.c
def compute_hash(qstr, bytes_hash):
    hash = 5381
    for b in qstr:
        hash = (hash * 33) ^ b
    # Make sure that valid hash is never zero, zero means "hash not computed"
    return (hash & ((1 << (8 * bytes_hash)) - 1)) or 1


def translate(translation_file, i18ns):
    with open(translation_file, "rb") as f:
        table = gettext.GNUTranslations(f)

        translations = []
        for original in i18ns:
            unescaped = original
            for s in C_ESCAPES:
                unescaped = unescaped.replace(C_ESCAPES[s], s)
            translation = table.gettext(unescaped)
            # Add in carriage returns to work in terminals
            translation = translation.replace("\n", "\r\n")
            translations.append((original, translation))
        return translations


class TextSplitter:
    def __init__(self, words):
        words = sorted(words, key=lambda x: len(x), reverse=True)
        self.words = set(words)
        if words:
            pat = "|".join(re.escape(w) for w in words) + "|."
        else:
            pat = "."
        self.pat = re.compile(pat, flags=re.DOTALL)

    def iter_words(self, text):
        s = []
        words = self.words
        for m in self.pat.finditer(text):
            t = m.group(0)
            if t in words:
                if s:
                    yield (False, "".join(s))
                    s = []
                yield (True, t)
            else:
                s.append(t)
        if s:
            yield (False, "".join(s))

    def iter(self, text):
        for m in self.pat.finditer(text):
            yield m.group(0)


def iter_substrings(s, minlen, maxlen):
    len_s = len(s)
    maxlen = min(len_s, maxlen)
    for n in range(minlen, maxlen + 1):
        for begin in range(0, len_s - n + 1):
            yield s[begin : begin + n]


translation_requires_uint16 = {"cs", "ja", "ko", "pl", "tr", "zh_Latn_pinyin"}


def compute_unicode_offset(texts):
    all_ch = set(" ".join(texts))
    ch_160 = sorted(c for c in all_ch if 160 <= ord(c) < 255)
    ch_256 = sorted(c for c in all_ch if 255 < ord(c))
    if not ch_256:
        return 0, 0
    min_256 = ord(min(ch_256))
    span = ord(max(ch_256)) - ord(min(ch_256)) + 1

    if ch_160:
        max_160 = ord(max(ch_160)) + 1
    else:
        max_160 = max(160, 255 - span)

    if max_160 + span > 256:
        return 0, 0

    offstart = max_160
    offset = min_256 - max_160
    return offstart, offset


@dataclass
class EncodingTable:
    values: object
    lengths: object
    words: object
    canonical: object
    extractor: object
    apply_offset: object
    remove_offset: object


def compute_huffman_coding(translation_name, translations, f):
    texts = [t[1] for t in translations]
    words = []

    start_unused = 0x80
    end_unused = 0xFF
    max_ord = 0
    offstart, offset = compute_unicode_offset(texts)

    def apply_offset(c):
        oc = ord(c)
        if oc >= offstart:
            oc += offset
        return chr(oc)

    def remove_offset(c):
        oc = ord(c)
        if oc >= offstart:
            oc = oc - offset
        try:
            return chr(oc)
        except Exception as e:
            raise ValueError(f"remove_offset {offstart=} {oc=}") from e

    for text in texts:
        for c in text:
            c = remove_offset(c)
            ord_c = ord(c)
            max_ord = max(ord_c, max_ord)
            if 0x80 <= ord_c < 0xFF:
                end_unused = min(ord_c, end_unused)
    max_words = end_unused - 0x80

    bits_per_codepoint = 16 if max_ord > 255 else 8
    values_type = "uint16_t" if max_ord > 255 else "uint8_t"
    translation_name = translation_name.split("/")[-1].split(".")[0]
    if max_ord > 255 and translation_name not in translation_requires_uint16:
        raise ValueError(
            f"Translation {translation_name} expected to fit in 8 bits but required 16 bits"
        )

    while len(words) < max_words:
        # Until the dictionary is filled to capacity, use a heuristic to find
        # the best "word" (2- to 11-gram) to add to it.
        #
        # The TextSplitter allows us to avoid considering parts of the text
        # that are already covered by a previously chosen word, for example
        # if "the" is in words then not only will "the" not be considered
        # again, neither will "there" or "wither", since they have "the"
        # as substrings.
        extractor = TextSplitter(words)
        counter = collections.Counter()
        for t in texts:
            for atom in extractor.iter(t):
                counter[atom] += 1
        cb = huffman.codebook(counter.items())
        lengths = sorted(dict((v, len(cb[k])) for k, v in counter.items()).items())

        def bit_length(s):
            return sum(len(cb[c]) for c in s)

        def est_len(occ):
            idx = bisect.bisect_left(lengths, (occ, 0))
            return lengths[idx][1] + 1

        # The cost of adding a dictionary word is just its storage size
        # while its savings is close to the difference between the original
        # huffman bit-length of the string and the estimated bit-length
        # of the dictionary word, times the number of times the word appears.
        #
        # The savings is not strictly accurate because including a word into
        # the Huffman tree bumps up the encoding lengths of all words in the
        # same subtree.  In the extreme case when the new word is so frequent
        # that it gets a one-bit encoding, all other words will cost an extra
        # bit each. This is empirically modeled by the constant factor added to
        # cost, but the specific value used isn't "proven" to be correct.
        #
        # Another source of inaccuracy is that compressed strings end up
        # on byte boundaries, not bit boundaries, so saving 1 bit somewhere
        # might not save a byte.
        #
        # In fact, when this change was first made, some translations (luckily,
        # ones on boards not at all close to full) wasted up to 40 bytes,
        # while the most constrained boards typically gained 100 bytes or
        # more.
        #
        # The difference between the two is the estimated net savings, in bits.
        def est_net_savings(s, occ):
            savings = occ * (bit_length(s) - est_len(occ))
            cost = len(s) * bits_per_codepoint + 24
            return savings - cost

        counter = collections.Counter()
        for t in texts:
            for (found, word) in extractor.iter_words(t):
                if not found:
                    for substr in iter_substrings(word, minlen=2, maxlen=11):
                        counter[substr] += 1

        # Score the candidates we found.  This is a semi-empirical formula that
        # attempts to model the number of bits saved as closely as possible.
        #
        # It attempts to compute the codeword lengths of the original word
        # to the codeword length the dictionary entry would get, times
        # the number of occurrences, less the ovehead of the entries in the
        # words[] array.

        scores = sorted(
            ((s, -est_net_savings(s, occ)) for (s, occ) in counter.items() if occ > 1),
            key=lambda x: x[1],
        )

        # Pick the one with the highest score.  The score must be negative.
        if not scores or scores[0][-1] >= 0:
            break

        word = scores[0][0]
        words.append(word)

    words.sort(key=len)
    extractor = TextSplitter(words)
    counter = collections.Counter()
    for t in texts:
        for atom in extractor.iter(t):
            counter[atom] += 1
    cb = huffman.codebook(counter.items())

    word_start = start_unused
    word_end = word_start + len(words) - 1
    f.write(f"// # words {len(words)}\n")
    f.write(f"// words {words}\n")

    values = []
    length_count = {}
    renumbered = 0
    last_length = None
    canonical = {}
    for atom, code in sorted(cb.items(), key=lambda x: (len(x[1]), x[0])):
        values.append(atom)
        length = len(code)
        if length not in length_count:
            length_count[length] = 0
        length_count[length] += 1
        if last_length:
            renumbered <<= length - last_length
        # print(f"atom={repr(atom)} code={code}", file=sys.stderr)
        canonical[atom] = "{0:0{width}b}".format(renumbered, width=length)
        if len(atom) > 1:
            o = words.index(atom) + 0x80
            s = "".join(C_ESCAPES.get(ch1, ch1) for ch1 in atom)
            f.write(f"// {o} {s} {counter[atom]} {canonical[atom]} {renumbered}\n")
        else:
            s = C_ESCAPES.get(atom, atom)
            canonical[atom] = "{0:0{width}b}".format(renumbered, width=length)
            o = ord(atom)
            f.write(f"// {o} {s} {counter[atom]} {canonical[atom]} {renumbered}\n")
        renumbered += 1
        last_length = length
    lengths = bytearray()
    f.write(f"// length count {length_count}\n")

    for i in range(1, max(length_count) + 2):
        lengths.append(length_count.get(i, 0))
    f.write(f"// values {values} lengths {len(lengths)} {lengths}\n")

    f.write(f"// {values} {lengths}\n")
    values = [(atom if len(atom) == 1 else chr(0x80 + words.index(atom))) for atom in values]
    max_translation_encoded_length = max(
        len(translation.encode("utf-8")) for (original, translation) in translations
    )

    maxlen = len(words[-1])
    minlen = len(words[0])
    wlencount = [len([None for w in words if len(w) == l]) for l in range(minlen, maxlen + 1)]

    f.write("typedef {} mchar_t;\n".format(values_type))
    f.write("const uint8_t lengths[] = {{ {} }};\n".format(", ".join(map(str, lengths))))
    f.write(
        "const mchar_t values[] = {{ {} }};\n".format(
            ", ".join(str(ord(remove_offset(u))) for u in values)
        )
    )
    f.write(
        "#define compress_max_length_bits ({})\n".format(
            max_translation_encoded_length.bit_length()
        )
    )
    f.write(
        "const mchar_t words[] = {{ {} }};\n".format(
            ", ".join(str(ord(remove_offset(c))) for w in words for c in w)
        )
    )
    f.write("const uint8_t wlencount[] = {{ {} }};\n".format(", ".join(str(p) for p in wlencount)))
    f.write("#define word_start {}\n".format(word_start))
    f.write("#define word_end {}\n".format(word_end))
    f.write("#define minlen {}\n".format(minlen))
    f.write("#define maxlen {}\n".format(maxlen))
    f.write("#define offstart {}\n".format(offstart))
    f.write("#define offset {}\n".format(offset))

    return EncodingTable(values, lengths, words, canonical, extractor, apply_offset, remove_offset)


def decompress(encoding_table, encoded, encoded_length_bits):
    values = encoding_table.values
    lengths = encoding_table.lengths
    words = encoding_table.words

    dec = []
    this_byte = 0
    this_bit = 7
    b = encoded[this_byte]
    bits = 0
    for i in range(encoded_length_bits):
        bits <<= 1
        if 0x80 & b:
            bits |= 1

        b <<= 1
        if this_bit == 0:
            this_bit = 7
            this_byte += 1
            if this_byte < len(encoded):
                b = encoded[this_byte]
        else:
            this_bit -= 1
    length = bits

    i = 0
    while i < length:
        bits = 0
        bit_length = 0
        max_code = lengths[0]
        searched_length = lengths[0]
        while True:
            bits <<= 1
            if 0x80 & b:
                bits |= 1

            b <<= 1
            bit_length += 1
            if this_bit == 0:
                this_bit = 7
                this_byte += 1
                if this_byte < len(encoded):
                    b = encoded[this_byte]
            else:
                this_bit -= 1
            if max_code > 0 and bits < max_code:
                # print('{0:0{width}b}'.format(bits, width=bit_length))
                break
            max_code = (max_code << 1) + lengths[bit_length]
            searched_length += lengths[bit_length]

        v = values[searched_length + bits - max_code]
        if v >= chr(0x80) and v < chr(0x80 + len(words)):
            v = words[ord(v) - 0x80]
        i += len(v.encode("utf-8"))
        dec.append(v)
    return "".join(dec)


def compress(encoding_table, decompressed, encoded_length_bits, len_translation_encoded):
    if not isinstance(decompressed, str):
        raise TypeError()
    canonical = encoding_table.canonical
    extractor = encoding_table.extractor

    enc = bytearray(len(decompressed) * 3)
    current_bit = 7
    current_byte = 0

    bits = encoded_length_bits + 1
    for i in range(bits - 1, 0, -1):
        if len_translation_encoded & (1 << (i - 1)):
            enc[current_byte] |= 1 << current_bit
        if current_bit == 0:
            current_bit = 7
            current_byte += 1
        else:
            current_bit -= 1

    for atom in extractor.iter(decompressed):
        for b in canonical[atom]:
            if b == "1":
                enc[current_byte] |= 1 << current_bit
            if current_bit == 0:
                current_bit = 7
                current_byte += 1
            else:
                current_bit -= 1

    if current_bit != 7:
        current_byte += 1
    return enc[:current_byte]


def qstr_escape(qst):
    def esc_char(m):
        c = ord(m.group(0))
        try:
            name = codepoint2name[c]
        except KeyError:
            name = "0x%02x" % c
        return "_" + name + "_"

    return re.sub(r"[^A-Za-z0-9_]", esc_char, qst)


def parse_input_headers(infiles):
    i18ns = set()

    # read the qstrs in from the input files
    for infile in infiles:
        with open(infile, "rt") as f:
            for line in f:
                line = line.strip()

                match = re.match(r'^TRANSLATE\("(.*)"\)$', line)
                if match:
                    i18ns.add(match.group(1))
                    continue

    return i18ns


def escape_bytes(qstr):
    if all(32 <= ord(c) <= 126 and c != "\\" and c != '"' for c in qstr):
        # qstr is all printable ASCII so render it as-is (for easier debugging)
        return qstr
    else:
        # qstr contains non-printable codes so render entire thing as hex pairs
        qbytes = bytes_cons(qstr, "utf8")
        return "".join(("\\x%02x" % b) for b in qbytes)


def make_bytes(cfg_bytes_len, cfg_bytes_hash, qstr):
    qbytes = bytes_cons(qstr, "utf8")
    qlen = len(qbytes)
    qhash = compute_hash(qbytes, cfg_bytes_hash)
    if qlen >= (1 << (8 * cfg_bytes_len)):
        print("qstr is too long:", qstr)
        assert False
    qdata = escape_bytes(qstr)
    return '%d, %d, "%s"' % (qhash, qlen, qdata)


def output_translation_data(encoding_table, i18ns, out):
    # print out the starter of the generated C file
    out.write("// This file was automatically generated by maketranslatedata.py\n")
    out.write('#include "supervisor/shared/translate/compressed_string.h"\n')
    out.write("\n")

    total_text_size = 0
    total_text_compressed_size = 0
    max_translation_encoded_length = max(
        len(translation.encode("utf-8")) for original, translation in i18ns
    )
    encoded_length_bits = max_translation_encoded_length.bit_length()
    for i, translation in enumerate(i18ns):
        original, translation = translation
        translation_encoded = translation.encode("utf-8")
        compressed = compress(
            encoding_table, translation, encoded_length_bits, len(translation_encoded)
        )
        total_text_compressed_size += len(compressed)
        decompressed = decompress(encoding_table, compressed, encoded_length_bits)
        assert decompressed == translation
        for c in C_ESCAPES:
            decompressed = decompressed.replace(c, C_ESCAPES[c])
        formatted = ["{:d}".format(x) for x in compressed]
        out.write(
            "const compressed_string_t translation{} = {{ .data = {}, .tail = {{ {} }} }}; // {}\n".format(
                i, formatted[0], ", ".join(formatted[1:]), original, decompressed
            )
        )
        total_text_size += len(translation.encode("utf-8"))

    out.write("\n")
    out.write("// {} bytes worth of translations\n".format(total_text_size))
    out.write("// {} bytes worth of translations compressed\n".format(total_text_compressed_size))
    out.write("// {} bytes saved\n".format(total_text_size - total_text_compressed_size))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Process QSTR definitions into headers for compilation"
    )
    parser.add_argument(
        "infiles", metavar="N", type=str, nargs="+", help="an integer for the accumulator"
    )
    parser.add_argument(
        "--translation", default=None, type=str, help="translations for i18n() items"
    )
    parser.add_argument(
        "--compression_filename",
        type=argparse.FileType("w", encoding="UTF-8"),
        help="header for compression info",
    )
    parser.add_argument(
        "--translation_filename",
        type=argparse.FileType("w", encoding="UTF-8"),
        help="c file for translation data",
    )

    args = parser.parse_args()

    i18ns = parse_input_headers(args.infiles)
    i18ns = sorted(i18ns)
    translations = translate(args.translation, i18ns)
    encoding_table = compute_huffman_coding(
        args.translation, translations, args.compression_filename
    )
    output_translation_data(encoding_table, translations, args.translation_filename)
