"""Safety regressions for genuine carrier address choices and shipping input."""

import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
	"sf_label_rules_under_test",
	Path(__file__).resolve().parents[1] / "erpnext_shipping/sf_international/sf_label_rules.py",
)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)


def receiver(**changes):
	return dict(country="US", province="Texas", city="San Antonio", county="Bexar", post_code="78216",
		address="239 Sharon Dr", contact="Customer", phone="+1 2107607172", **changes)


def parcel(**changes):
	return {**dict(length=20, width=10, height=5, weight=0.8, count=2), **changes}


def customs(**changes):
	return {**dict(declared_value=20, declared_currency="USD", purchase_currency="USD",
		hs_code="9504200090", ename="Billiard chalk", cname="台球巧克粉"), **changes}


def test_only_actual_postcode_regions_can_be_candidates():
	base = receiver()
	rows = [base, {**base, "city": "Castle Hills"}, {**base, "post_code": "78217"},
		{**base, "country": "CA"}, {**base, "province": ""}, {**base, "city": ""}, None,
		{**base, "city": " san antonio "}]
	actual = rules.postcode_candidates(rows, base)
	assert [r["city"] for r in actual] == ["San Antonio", "Castle Hills"]
	assert all("contact" not in r and "address" not in r for r in actual)


def test_zip_plus_four_can_use_same_zip5_but_preserves_original_postcode():
	current = {**receiver(), "post_code": "78216-1234"}
	result = rules.postcode_candidates([receiver(), {**receiver(), "post_code": "78217"}], current)
	assert len(result) == 1
	assert result[0]["post_code"] == "78216-1234"
	assert result[0]["source_post_code"] == "78216"
	assert result[0]["warning"]


def test_exact_zip_plus_four_takes_precedence_over_zip5_duplicate():
	current = {**receiver(), "post_code": "78216-1234"}
	result = rules.postcode_candidates([receiver(), current], current)
	assert len(result) == 1 and "warning" not in result[0]


@pytest.mark.parametrize("country,actual,wanted", [("CA", "K1A", "K1A0B1"), ("GB", "SW1A", "SW1A1AA"), ("US", "782161234", "78216")])
def test_no_other_prefix_or_fuzzy_postcode_match(country, actual, wanted):
	row = {**receiver(), "country": country, "post_code": actual}
	assert rules.postcode_candidates([row], {**row, "post_code": wanted}) == []


def test_normalized_postcodes_match_without_mutating_input():
	row = {**receiver(), "country": "CA", "post_code": "k1a 0b1"}
	current = {**row, "post_code": "K1A-0B1"}
	assert rules.postcode_candidates([row], current)[0]["post_code"] == "K1A-0B1"
	assert row["post_code"] == "k1a 0b1"
	assert rules.normalize_postcode(" k1a - 0b1 ") == "K1A0B1"


@pytest.mark.parametrize("rows", [None, [], {}, [None]])
def test_missing_parcels_have_no_invented_weight(rows):
	with pytest.raises(rules.LabelInputError):
		rules.validate_parcels(rows)


@pytest.mark.parametrize("field", ["length", "width", "height", "weight"])
@pytest.mark.parametrize("value", [None, True, False, "NaN", "Infinity", -1, 0, "", "1e999"])
def test_nonpositive_or_nonfinite_parcel_measurements_block_booking(field, value):
	with pytest.raises(rules.LabelInputError) as exc:
		rules.validate_parcels([parcel(**{field: value})])
	assert f"parcels[0].{field}" in exc.value.fields


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.2, "1.0000000000000001", "NaN", "Infinity", 2147483648])
def test_piece_count_must_be_an_exact_supported_positive_integer(value):
	with pytest.raises(rules.LabelInputError):
		rules.validate_parcels([parcel(count=value)])


def test_all_missing_measurements_are_reported_together():
	with pytest.raises(rules.LabelInputError) as exc:
		rules.validate_parcels([{}])
	assert len(exc.value.fields) == 5
	assert "单件重量" in str(exc.value) and "件数" in str(exc.value)


def test_totals_use_single_piece_weight_and_each_dimension_max():
	rows = [parcel(weight="0.8", count="2"), parcel(weight=1.1, count=1, width=15, height=9, length=10)]
	assert rules.parcel_totals(rows) == dict(total_weight=2.7, parcel_quantity=3, length=20, width=15, height=9)
	assert rows[0]["weight"] == "0.8"


def test_total_weight_cannot_overflow_to_infinity():
	with pytest.raises(rules.LabelInputError):
		rules.parcel_totals([parcel(weight=1e308, count=10)])


def test_receiver_identity_can_be_corrected_without_geography_change():
	old = receiver()
	new = rules.apply_details(old, {"contact": "New Recipient", "phone": "+1 1234567890"})
	assert new["contact"] == "New Recipient" and new["city"] == old["city"]
	assert old["contact"] == "Customer"


@pytest.mark.parametrize("key", ["country", "province", "city", "county", "post_code", "address", "is_admin"])
def test_details_cannot_bypass_carrier_candidate_selection(key):
	with pytest.raises(rules.LabelInputError):
		rules.apply_details(receiver(), {key: "Invented"})


def test_receiver_required_fields_and_carrier_street_limit_are_explicit():
	with pytest.raises(rules.LabelInputError) as exc:
		rules.apply_details({**receiver(), "phone": "", "contact": ""})
	assert exc.value.fields == ["contact", "phone"]
	with pytest.raises(rules.LabelInputError, match="60"):
		rules.apply_details({**receiver(), "address": "a" * 61})


@pytest.mark.parametrize("address", ["a" * 60, "街" * 60, "🏠" * 60, "A " * 29 + "B."])
def test_exactly_60_address_characters_are_preserved_after_trimming(address):
	assert rules.apply_details({**receiver(), "address": "  " + address + "  "})["address"] == address


@pytest.mark.parametrize("address", ["a" * 61, "街" * 61, "🏠" * 61, "A " * 29 + "B.,"])
def test_61_address_characters_fail_instead_of_being_truncated(address):
	with pytest.raises(rules.LabelInputError, match="当前 61 个") as exc:
		rules.apply_details({**receiver(), "address": address})
	assert exc.value.fields == ["address"]


def test_customs_retains_explicit_values_without_inventing_defaults():
	values = customs(declared_value="15.35", declared_currency="CNY", purchase_currency="USD")
	assert rules.validate_customs(values) == {**values, "declared_value": 15.35}
	with pytest.raises(rules.LabelInputError) as exc:
		rules.validate_customs({})
	assert set(exc.value.fields) == set(customs())


@pytest.mark.parametrize("changes", [dict(declared_value=True), dict(declared_value=0), dict(declared_value="NaN"),
	dict(declared_currency="usd"), dict(declared_currency="ＵＳＤ"), dict(purchase_currency="US"),
	dict(hs_code=""), dict(ename="a" * 101), dict(cname="粉" * 101), dict(something_else="bad")])
def test_invalid_customs_cannot_be_silently_corrected(changes):
	with pytest.raises(rules.LabelInputError):
		rules.validate_customs(customs(**changes))
