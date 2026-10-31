from datetime import datetime

import pytest

from app import normalize as n


@pytest.mark.parametrize("raw,expected", [
    ("Stripe, Inc.", "stripe"), ("Stripe", "stripe"), ("STRIPE INC", "stripe"), ("Acme Pvt. Ltd", "acme"),
    ("Johnson & Johnson", "johnson and johnson"), ("Tech Mahindra Limited", "tech mahindra"), ("Mahindra", "mahindra"),
    ("Tata Consultancy Services", "tata consultancy services"), ("Zürich Insurance AG", "zurich insurance"),
    ("Acme Technologies", "acme technologies"), ("Inc", "inc"), ("  ", ""), (None, "")])
def test_company_key(raw, expected):
    assert n.company_key(raw) == expected


def test_similar_company_names_are_not_confused():
    assert n.company_key("Tech Mahindra") != n.company_key("Mahindra")
    assert n.company_key("Acme Technologies") != n.company_key("Acme")


@pytest.mark.parametrize("raw,expected", [
    ("Software Engineer II (Remote)", "software engineer ii"), ("Engineer - Bengaluru", "engineer"),
    ("Backend Engineer | Pune, India", "backend engineer"), ("Data Analyst REQ-123456", "data analyst"),
    ("Senior Staff Engineer [Urgent]", "senior staff engineer"), ("C++ Engineer", "c++ engineer"), (".NET Developer", ".net developer"),
    ("Product Manager - Payments", "product manager payments")])
def test_title_norm(raw, expected):
    assert n.title_norm(raw) == expected


@pytest.mark.parametrize("a,b,same", [
    ("Software Engineer II", "Software Engineer 2", True), ("Software Engineer II", "Software Engineer III", False),
    ("Sr. Data Analyst", "Senior Data Analyst", True), ("Data Analyst", "Senior Data Analyst", False),
    ("Software Engineer", "Software Engineer Intern", False), ("Staff Engineer", "Principal Engineer", False),
    ("Engineer I", "Engineer", False)])
def test_level_tokens_must_match_to_be_the_same_job(a, b, same):
    assert (n.level_tokens(a) == n.level_tokens(b)) is same


def test_seniority_labels():
    assert n.seniority("Senior Backend Engineer") == "senior" and n.seniority("Software Engineer Intern") == "intern"
    assert n.seniority("Backend Engineer") is None and n.seniority("Director of Engineering") == "director"


@pytest.mark.parametrize("loc,cities", [
    ("Bengaluru, Karnataka, India", {"bangalore"}), ("Bangalore", {"bangalore"}), ("Pune | Mumbai", {"pune", "mumbai"}),
    ("Remote - India", set()), ("United States", set()), ("Lisbon, Portugal", {"lisbon"}), ("", set()), ("Gurgaon, Haryana", {"gurugram"})])
def test_city_set(loc, cities):
    assert set(n.city_set(loc)) == cities


def test_locations_compatible_rules():
    assert n.locations_compatible("pune", "pune|mumbai", False, False, "India", "India")
    assert not n.locations_compatible("pune", "mumbai", False, False, "India", "India")
    assert n.locations_compatible("", "", True, True, "India", "India, Canada")
    assert not n.locations_compatible("", "", True, True, "India", "Canada")
    assert not n.locations_compatible("", "pune", False, False, "India", "India"), "unknown is never 'compatible'"


def test_norm_keeps_special_tokens_and_strips_accents():
    assert n.norm("C++ / C# / .NET / Node.js") == "c++ c# .net node.js"
    assert n.norm("Zürich São Paulo Müller") == "zurich sao paulo muller"
    assert n.tokenize("a b c r go 100%") == ["c", "r", "go", "100"]
    assert len(n.tokenize("one two three four five six seven eight nine ten")) == 8


def test_clamp_strips_control_characters_and_truncates():
    assert n.clamp("a\x00b\x07c", 10) == "a b c" or n.clamp("a\x00b\x07c", 10).replace(" ", "") == "abc"
    assert n.clamp("x" * 50, 10) == "x" * 10 and n.clamp(None, 5) == ""


def test_dedup_key_is_stable_and_sensitive_to_each_part():
    k = n.dedup_key("acme", "engineer", "pune")
    assert k == n.dedup_key("acme", "engineer", "pune") and len(k) == 40
    assert len({k, n.dedup_key("acme2", "engineer", "pune"), n.dedup_key("acme", "engineer2", "pune"), n.dedup_key("acme", "engineer", "mumbai")}) == 4


def test_search_text_contains_title_company_location_and_description():
    t = n.search_text("Data Scientist", "Acme, Inc.", "Machine Learning with Python " * 3, "Zürich, Switzerland")
    assert t.startswith("data scientist | acme | zurich switzerland | machine learning with python")
    assert len(n.search_text("t", "c", "word " * 2000)) < 2100


def test_salary_detail_captures_original_currency_period_and_raw_text():
    d = n.salary_detail("We pay $120k - $150k per year")
    assert (d["salary_currency"], d["salary_period"], d["salary_orig_min"], d["salary_orig_max"], d["salary_source"]) == ("USD", "year", 120000.0, 150000.0, "parsed")
    assert "120k" in d["salary_raw"]
    d = n.salary_detail("CTC: 12-18 LPA")
    assert (d["salary_currency"], d["salary_orig_min"], d["salary_orig_max"]) == ("INR", 1200000.0, 1800000.0)
    assert n.salary_detail("competitive salary, great perks") == {}


def test_parse_dt_accepts_iso_epoch_ms_and_rejects_garbage():
    assert n.parse_dt("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5)
    assert n.parse_dt("2026-01-02T03:04:05+05:30") == datetime(2026, 1, 1, 21, 34, 5)
    assert n.parse_dt(1700000000000) == n.parse_dt(1700000000) == datetime(2023, 11, 14, 22, 13, 20)
    assert n.parse_dt("not a date") is None and n.parse_dt(None) is None and n.parse_dt("") is None


def test_shingle_similarity_and_title_similarity():
    a = "build scalable services with python and go for our platform team " * 4
    assert n.jaccard(n.shingles(a), n.shingles(a)) == 1.0
    assert n.jaccard(n.shingles(a), n.shingles("completely different words entirely here " * 8)) < 0.1
    assert n.title_similarity("software engineer", "engineer software") == 1.0
    assert n.title_similarity("software engineer", "data analyst") < 0.6


def test_derived_fields_for_a_legacy_shaped_job():
    d = n.derived_fields({"title": "Senior Backend Engineer", "company": "Acme, Inc.", "location": "Pune, India", "description": "x" * 10,
                          "remote": False, "country": "India", "salary_min": 12, "salary_max": 18}, company_id=7)
    assert d["company_id"] == 7 and d["company_norm"] == "acme" and d["city_norm"] == "pune" and d["salary_source"] == "structured"
    assert d["status"] == "active" and d["miss_count"] == 0 and d["posted_at_source"] == "first_seen" and d["category"] == "software"
