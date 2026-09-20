"""Short join codes.

A code carries the host's address, so no directory, no relay and no internet
connection is involved: decoding a code gives back exactly the address and port
it was made from.

Typical home networks produce a five-character code. The compression is what
makes that possible: almost every LAN address is inside 192.168.0.0/16,
10.0.0.0/8 or 172.16.0.0/12, so the prefix is implied by a three-bit variant
rather than spelled out, and the default port is implied by the variant too.

    192.168.1.24 on the default port  ->  5 characters
    10.0.0.7 on the default port      ->  7 characters
    an address outside those ranges   ->  8 to 12 characters

The alphabet excludes I, L, O and U. The first three are excluded because they
are indistinguishable from 1 and 0 when read aloud or written down, which is the
whole point of a code you say to someone across a room. U is excluded so that
codes cannot spell the obvious words.

A five-bit checksum is packed in, so a mistyped code is rejected rather than
resolving to some other machine on the network.
"""

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
BITS_PER_CHAR = 5
CHECKSUM_BITS = 5
VARIANT_BITS = 3

DEFAULT_PORT = 45881
MAX_PORT_DELTA = 255

# Position-dependent offsets applied to the finished digits. Without them the
# compression shows through: every address in 192.168.0.0/16 starts with the same
# two characters and a nearby address gives a nearly identical code, which looks
# broken and makes two codes easy to confuse. This is not secrecy, only spread.
SALT = [7, 19, 3, 29, 11, 23, 5, 17, 31, 13, 2, 27, 9, 21]

# Characters people write when they meant something else.
SUBSTITUTIONS = {"I": "1", "L": "1", "O": "0", "-": "", " ": "", "_": ""}

V_192_DEFAULT = 0
V_10_DEFAULT = 1
V_172_DEFAULT = 2
V_ANY_DEFAULT = 3
V_192_PORT = 4
V_10_PORT = 5
V_172_PORT = 6
V_ANY_PORT = 7


class CodeError(ValueError):
    """A code that is not a code, or that has been mistyped."""


def _octets(address: str) -> list:
    parts = address.split(".")
    if len(parts) != 4:
        raise CodeError("that is not an IPv4 address")

    octets = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as error:
            raise CodeError("that is not an IPv4 address") from error
        if not 0 <= value <= 255:
            raise CodeError("that is not an IPv4 address")
        octets.append(value)

    return octets


def _pack(bits: int, width: int, value: int, into: int) -> tuple:
    return (into << width) | value, bits + width


def _checksum(value: int, width: int) -> int:
    """Five bits derived from the whole payload."""
    total = 0
    remaining = value
    while remaining:
        total = (total * 31 + (remaining & 0x1F)) & 0xFFFFFFFF
        remaining >>= 5
    return (total ^ width) & 0x1F


def encode(address: str, port: int = DEFAULT_PORT) -> str:
    octets = _octets(address)

    if not 1 <= port <= 65535:
        raise CodeError("that is not a valid port")

    delta = port - DEFAULT_PORT
    near_default = 0 <= delta <= MAX_PORT_DELTA

    if octets[0] == 192 and octets[1] == 168:
        prefix_variant = (V_192_DEFAULT, V_192_PORT)
        payload = (octets[2] << 8) | octets[3]
        width = 16
    elif octets[0] == 10:
        prefix_variant = (V_10_DEFAULT, V_10_PORT)
        payload = (octets[1] << 16) | (octets[2] << 8) | octets[3]
        width = 24
    elif octets[0] == 172 and 16 <= octets[1] <= 31:
        prefix_variant = (V_172_DEFAULT, V_172_PORT)
        payload = ((octets[1] - 16) << 16) | (octets[2] << 8) | octets[3]
        width = 24
    else:
        prefix_variant = None

    if prefix_variant is not None and near_default:
        if delta == 0:
            variant = prefix_variant[0]
        else:
            variant = prefix_variant[1]
            payload = (payload << 8) | delta
            width += 8
    elif delta == 0:
        # An address outside the private ranges, on the default port.
        variant = V_ANY_DEFAULT
        payload, width = 0, 0
        for octet in octets:
            payload = (payload << 8) | octet
            width += 8
    else:
        # Anything else: the address and the port are both carried in full.
        variant = V_ANY_PORT
        payload, width = 0, 0
        for octet in octets:
            payload = (payload << 8) | octet
            width += 8
        payload = (payload << 16) | port
        width += 16

    body = (variant << width) | payload
    body_width = VARIANT_BITS + width

    value = (body << CHECKSUM_BITS) | _checksum(body, body_width)
    total_bits = body_width + CHECKSUM_BITS

    characters = (total_bits + BITS_PER_CHAR - 1) // BITS_PER_CHAR
    value <<= characters * BITS_PER_CHAR - total_bits

    out = []
    for position, index in enumerate(range(characters - 1, -1, -1)):
        digit = (value >> (index * BITS_PER_CHAR)) & 0x1F
        out.append(ALPHABET[(digit + SALT[position % len(SALT)]) % 32])

    return "".join(out)


def normalise(code: str) -> str:
    if not isinstance(code, str):
        raise CodeError("enter the code the host gave you")

    text = code.strip().upper()
    for wrong, right in SUBSTITUTIONS.items():
        text = text.replace(wrong, right)

    if not text:
        raise CodeError("enter the code the host gave you")

    for character in text:
        if character not in ALPHABET:
            raise CodeError("a code does not contain the letter " + character)

    return text


def decode(code: str) -> tuple:
    """Return (address, port). Raises CodeError on anything that is not valid."""
    text = normalise(code)

    value = 0
    for position, character in enumerate(text):
        digit = (ALPHABET.index(character) - SALT[position % len(SALT)]) % 32
        value = (value << BITS_PER_CHAR) | digit

    total_bits = len(text) * BITS_PER_CHAR
    if total_bits <= VARIANT_BITS + CHECKSUM_BITS:
        raise CodeError("that code is too short")

    # The encoder pads the low bits to reach a whole number of characters, so
    # the variant has to be read before the payload width is known.
    for padding in range(BITS_PER_CHAR):
        # The encoder pads with zeros, so a candidate whose padding bits are not
        # zero is not something this encoder produced. Insisting on that removes
        # the aliases where two codes differ only in bits nobody reads, and it is
        # what makes a single mistyped character almost always fail.
        if value & ((1 << padding) - 1):
            continue

        candidate = value >> padding
        candidate_bits = total_bits - padding
        body_width = candidate_bits - CHECKSUM_BITS
        if body_width <= VARIANT_BITS:
            continue

        body = candidate >> CHECKSUM_BITS
        checksum = candidate & 0x1F
        if _checksum(body, body_width) != checksum:
            continue

        variant = body >> (body_width - VARIANT_BITS)
        payload = body & ((1 << (body_width - VARIANT_BITS)) - 1)
        payload_width = body_width - VARIANT_BITS

        try:
            return _unpack(variant, payload, payload_width)
        except CodeError:
            continue

    raise CodeError("that code is not right; check it with the host")


def _unpack(variant: int, payload: int, width: int) -> tuple:
    if variant == V_ANY_PORT:
        if width != 48:
            raise CodeError("wrong length")
        port = payload & 0xFFFF
        rest = payload >> 16
        octets = [(rest >> shift) & 0xFF for shift in (24, 16, 8, 0)]
        return ".".join(str(octet) for octet in octets), port

    port = DEFAULT_PORT
    if variant in (V_192_PORT, V_10_PORT, V_172_PORT):
        port = DEFAULT_PORT + (payload & 0xFF)
        payload >>= 8
        width -= 8

    if variant in (V_192_DEFAULT, V_192_PORT):
        if width != 16:
            raise CodeError("wrong length")
        return f"192.168.{(payload >> 8) & 0xFF}.{payload & 0xFF}", port

    if variant in (V_10_DEFAULT, V_10_PORT):
        if width != 24:
            raise CodeError("wrong length")
        octets = [(payload >> shift) & 0xFF for shift in (16, 8, 0)]
        return "10." + ".".join(str(octet) for octet in octets), port

    if variant in (V_172_DEFAULT, V_172_PORT):
        if width != 24:
            raise CodeError("wrong length")
        octets = [
            16 + ((payload >> 16) & 0xFF),
            (payload >> 8) & 0xFF,
            payload & 0xFF,
        ]
        return "172." + ".".join(str(octet) for octet in octets), port

    if variant == V_ANY_DEFAULT:
        if width != 32:
            raise CodeError("wrong length")
        octets = [(payload >> shift) & 0xFF for shift in (24, 16, 8, 0)]
        return ".".join(str(octet) for octet in octets), DEFAULT_PORT

    raise CodeError("unknown code format")


def looks_like_a_code(text: str) -> bool:
    """True when text is more plausibly a code than an address.

    Used to let one input box accept either, so nobody has to know which of the
    two things they were given.
    """
    if not isinstance(text, str):
        return False

    stripped = text.strip()

    if not stripped or "." in stripped or ":" in stripped:
        return False

    # A code is one word. Prose that happens to use only these letters is not a
    # code, and treating it as one produces a baffling error message.
    if any(character.isspace() for character in stripped):
        return False

    if not 4 <= len(stripped.replace("-", "")) <= 14:
        return False

    try:
        normalise(stripped)
    except CodeError:
        return False

    return True


def pretty(code: str) -> str:
    """Group long codes so they can be read aloud without losing your place."""
    if len(code) <= 5:
        return code
    middle = (len(code) + 1) // 2
    return code[:middle] + "-" + code[middle:]
