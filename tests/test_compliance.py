"""The OWASP page cannot claim a test that does not exist, and cannot skip a risk."""

import yaml

from counsel import compliance


def test_the_map_is_valid_and_the_published_page_is_current():
    assert compliance.build() == compliance.OUTPUT.read_text(encoding="utf-8")


def test_a_renamed_test_breaks_the_map(tmp_path):
    doc = yaml.safe_load(compliance.SOURCE.read_text(encoding="utf-8"))
    doc["risks"][0]["evidence"] = ["tests/test_support.py::test_that_was_renamed"]
    assert any("has no test test_that_was_renamed" in p for p in compliance.check(doc))


def test_every_agentic_risk_must_have_a_row():
    doc = yaml.safe_load(compliance.SOURCE.read_text(encoding="utf-8"))
    doc["risks"] = [r for r in doc["risks"] if r["id"] != "ASI07"]
    assert any("missing ASI07" in p for p in compliance.check(doc))


def test_partial_rows_name_what_remains():
    doc = yaml.safe_load(compliance.SOURCE.read_text(encoding="utf-8"))
    for risk in doc["risks"]:
        if risk["coverage"] == "partial":
            assert "none" not in risk["remains"].lower(), risk["id"]
