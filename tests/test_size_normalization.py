from stockwatch.models import norm_size, Item, SizeInfo, Status
from stockwatch.parser import build_result


def test_myntra_plus_sizes_normalize_to_same_canonical_label():
    assert norm_size("3XL") == "XXXL"
    assert norm_size("3 XL") == "XXXL"
    assert norm_size("XXXL") == "XXXL"
    assert norm_size("4XL") == "XXXXL"


def test_watched_3xl_matches_rendered_xxxl_size():
    result = build_result(Item("123456", ("3XL",)), "Test", "TWINS LADY", [
        SizeInfo(label="XXXL", available=False),
    ])
    assert result.status == Status.ALL_OOS
    assert result.oos_sizes == ["XXXL"]


def test_watched_3xl_matches_rendered_spaced_3_xl_size():
    result = build_result(Item("123456", ("3XL",)), "Test", "TWINS LADY", [
        SizeInfo(label="3 XL", available=True),
    ])
    assert result.status == Status.OK
    assert result.in_sizes == ["3XL"] or result.in_sizes == ["XXXL"]


def test_future_numeric_prefix_alpha_sizes_normalize_generically():
    assert norm_size("5XL") == "XXXXXL"
    assert norm_size("5 XL") == "XXXXXL"
    assert norm_size("4XS") == "XXXXS"
    assert norm_size("7XS") == "XXXXXXXS"
    assert norm_size("XXXXXXL") == "XXXXXXL"


def test_numeric_and_named_sizes_are_preserved_without_fixed_catalogue():
    assert norm_size("28") == "28"
    assert norm_size("44 EU") == "44EU"
    assert norm_size("Free Size") == "FREESIZE"
    assert norm_size("One Size") == "ONESIZE"
    assert norm_size("New-Size-Alpha") == "NEW-SIZE-ALPHA"


def test_future_numeric_prefix_size_matches_rendered_alpha_size():
    result = build_result(Item("123456", ("5XL",)), "Test", "Brand", [
        SizeInfo(label="XXXXXL", available=True),
    ])
    assert result.status == Status.OK
    assert result.in_sizes == ["XXXXXL"]
