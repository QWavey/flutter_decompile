"""Unit tests for the binary-XML (AXML) decoder."""

import struct

import pytest

from flutter_decompile import axml


def test_decode_rejects_non_axml():
    with pytest.raises(axml.AxmlError):
        axml.decode(b"not axml at all")
    with pytest.raises(axml.AxmlError):
        axml.decode(b"\x02\x00\x08\x00\x00\x00\x00\x00")   # wrong magic


def test_decode_needs_a_string_pool():
    # Valid magic, but no string pool chunk -> honest failure, not a crash.
    buf = struct.pack("<HHI", 0x0003, 8, 8)
    with pytest.raises(axml.AxmlError):
        axml.decode(buf)


def test_len_decoders_handle_short_and_long_forms():
    # 8-bit: high bit means two-byte length.
    assert axml._decode_len8(bytes([0x05]), 0) == (1, 5)
    assert axml._decode_len8(bytes([0x81, 0x02]), 0) == (2, 0x102)
    # 16-bit
    assert axml._decode_len16(struct.pack("<H", 3), 0) == (2, 3)


def test_fmt_value_renders_the_common_types():
    pool = axml._StringPool(["hello", "world"])
    assert axml._fmt_value(pool, axml._TYPE_STRING, 0) == "hello"
    assert axml._fmt_value(pool, axml._TYPE_INT_BOOL, 1) == "true"
    assert axml._fmt_value(pool, axml._TYPE_INT_BOOL, 0) == "false"
    assert axml._fmt_value(pool, axml._TYPE_INT_DEC, 42) == "42"
    assert axml._fmt_value(pool, axml._TYPE_INT_HEX, 0x1F) == "0x1f"
    assert axml._fmt_value(pool, axml._TYPE_REFERENCE, 0x7F010001) == "@0x7f010001"
    # negative decimals round-trip through the sign bit
    assert axml._fmt_value(pool, axml._TYPE_INT_DEC, 0xFFFFFFFF) == "-1"


def test_empty_elements_self_close_but_parents_stay_open():
    events = [
        ("start", "manifest", 'package="p"'),
        ("start", "uses-permission", 'android:name="x"'),
        ("end", "uses-permission", ""),
        ("start", "application", 'android:label="A"'),
        ("start", "activity", 'android:name="Main"'),
        ("end", "activity", ""),
        ("end", "application", ""),
        ("end", "manifest", ""),
    ]
    xml = axml._render(events)
    assert '<uses-permission android:name="x"/>' in xml    # empty -> self-close
    assert '<activity android:name="Main"/>' in xml
    assert '<manifest package="p">' in xml                 # has children -> open
    assert '</manifest>' in xml
    assert '<application android:label="A">' in xml
    # nesting is indented
    assert '  <application' in xml and '    <activity' in xml


def test_summarize_pulls_the_headline_facts():
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<manifest android:versionName="2.3.1" android:versionCode="17" '
        'package="com.example.app">\n'
        '  <uses-permission android:name="android.permission.INTERNET">\n'
        '  </uses-permission>\n'
        '  <uses-permission android:name="android.permission.CAMERA">\n'
        '  </uses-permission>\n'
        '</manifest>\n'
    )
    facts = axml.summarize(xml)
    assert facts["package"] == "com.example.app"
    assert facts["versionName"] == "2.3.1"
    assert facts["versionCode"] == "17"
    assert facts["permissions"] == [
        "android.permission.CAMERA", "android.permission.INTERNET"]
