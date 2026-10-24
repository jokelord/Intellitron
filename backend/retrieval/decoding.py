"""Strict, bounded-codec decoding of user-maintained memory source bytes."""
import codecs
from importlib.metadata import version
import unicodedata


# Bump when decoding or text acceptance semantics change: old vectors must not
# silently represent text produced by a different decoding policy.
_ALGORITHM_VERSION = 1
_ENCODINGS = frozenset(('ascii', 'utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'big5',
    'iso8859-1', 'shift_jis', 'euc_kr', 'utf-16', 'utf-16-le', 'utf-16-be'))
_AUTO_ENCODINGS = ('ascii', 'utf_8', 'gbk', 'gb18030', 'big5', 'latin_1',
    'shift_jis', 'euc_kr', 'utf_16_le', 'utf_16_be')


def normalize_encoding(value, allow_auto=True):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('memory encoding must be a supported codec name')
    name = value.strip().lower()
    if name == 'auto':
        if allow_auto:
            return name
        raise ValueError('memory encoding override must specify an explicit codec')
    name = {'utf16le': 'utf-16-le', 'utf16be': 'utf-16-be', 'utf8sig': 'utf-8-sig'}.get(name, name)
    try:
        canonical = codecs.lookup(name).name
    except LookupError as error:
        raise ValueError('unsupported memory encoding') from error
    if canonical not in _ENCODINGS:
        raise ValueError('unsupported memory encoding')
    return canonical


def _strict_text(data, encoding):
    text = data.decode(encoding, errors='strict')
    for char in text:
        point = ord(char)
        if (0xD800 <= point <= 0xDFFF or 0xFDD0 <= point <= 0xFDEF
                or point & 0xFFFF in (0xFFFE, 0xFFFF)
                or unicodedata.category(char) == 'Cc' and char not in '\t\r\n\f'):
            raise ValueError('memory source contains binary or invalid text controls')
    return text


def _recognized_bom(data):
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise ValueError('UTF-32 memory sources are not supported')
    for marker, encoding in ((codecs.BOM_UTF8, 'utf-8'),
            (codecs.BOM_UTF16_LE, 'utf-16-le'), (codecs.BOM_UTF16_BE, 'utf-16-be')):
        if data.startswith(marker):
            return encoding, data[len(marker):]
    return None


def decode_source(data, relative, config):
    encoding = normalize_encoding(config.overrides.get(relative, config.encoding))
    bom = _recognized_bom(data)
    if bom is not None:
        detected, payload = bom
        compatible = (encoding == 'auto' or encoding == detected
            or encoding == 'utf-8-sig' and detected == 'utf-8'
            or encoding == 'utf-16' and detected in ('utf-16-le', 'utf-16-be'))
        if not compatible:
            raise ValueError('memory source BOM conflicts with configured encoding')
        # A recognized BOM is authoritative. Malformed content never falls
        # through to a single-byte codec that would conceal the error.
        return _strict_text(payload, detected)
    if encoding != 'auto':
        if encoding == 'utf-16':
            raise ValueError('UTF-16 memory encoding requires a BOM or explicit byte order')
        return _strict_text(data, encoding)
    try:
        return _strict_text(data, 'utf-8')
    except ValueError:
        pass
    from charset_normalizer import from_bytes
    candidate = from_bytes(data, cp_isolation=list(_AUTO_ENCODINGS), enable_fallback=False).best()
    if candidate is None:
        raise ValueError('memory source encoding could not be detected')
    selected = normalize_encoding(candidate.encoding, allow_auto=False)
    return _strict_text(data, selected)


def decoding_fingerprint(config):
    return {'algorithm_version': _ALGORITHM_VERSION,
        'detector': {'name': 'charset-normalizer', 'version': version('charset-normalizer')},
        'encoding': normalize_encoding(config.encoding),
        'overrides': {relative: normalize_encoding(encoding, allow_auto=False)
            for relative, encoding in sorted(config.overrides.items())}}
