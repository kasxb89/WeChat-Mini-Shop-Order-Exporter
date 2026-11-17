"""The small protobuf envelope used by the local miniapp debug transport."""
import json
import zlib


def varint(value: int) -> bytes:
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def field(number: int, value: int | str | bytes) -> bytes:
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode('utf-8')
    return varint(number << 3 | 2) + varint(len(value)) + value


def decode(data: bytes) -> dict:
    cursor = 0
    result = {}

    def integer():
        nonlocal cursor
        value = shift = 0
        while cursor < len(data) and shift < 70:
            byte = data[cursor]
            cursor += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
            shift += 7
        raise ValueError('Incomplete protobuf integer')

    while cursor < len(data):
        tag = integer()
        kind = tag & 7
        if kind == 0:
            value = integer()
        elif kind in (1, 2, 5):
            size = integer() if kind == 2 else 8 if kind == 1 else 4
            if cursor + size > len(data):
                raise ValueError('Incomplete protobuf field')
            value = data[cursor:cursor + size]
            cursor += size
        else:
            raise ValueError(f'Unsupported protobuf wire type {kind}')
        result[tag >> 3] = value
    return result


def command(sequence: int, method: str, params: dict) -> bytes:
    payload = json.dumps({'id': sequence, 'method': method, 'params': params}, separators=(',', ':'))
    body = field(1, sequence) + field(2, payload) + field(3, '')
    return field(1, sequence) + field(3, 'chromeDevtools') + field(4, body) + field(5, 0) + field(6, 0)


def response(data: bytes) -> dict | None:
    outer = decode(data)
    if outer.get(3) != b'chromeDevtoolsResult':
        return None
    body = outer.get(4, b'')
    if outer.get(5, 0) & 1:
        body = zlib.decompress(body)
    inner = decode(body)
    return json.loads(inner.get(2, b'{}'))
