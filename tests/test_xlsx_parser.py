from pathlib import Path
from scanner.xlsx_parser import parse_linkedin_export_xlsx

FIXTURE = str(Path(__file__).resolve().parent / "test_fixture.xlsx")


def test_parse_performance_sheet():
    result = parse_linkedin_export_xlsx(FIXTURE)
    assert result["impressions"] == 500
    assert result["members_reached"] == 200
    assert result["profile_viewers"] == 15
    assert result["followers_gained"] == 3
    assert result["reactions"] == 25
    assert result["comments"] == 8
    assert result["reposts"] == 4
    assert result["saves"] == 2
    assert result["sends"] == 7
    assert result["top_job_title"] == "Software Engineer"
    assert result["top_location"] == "San Francisco Bay Area"
    assert result["top_industry"] == "Technology, Information and Internet"


def test_parse_demographics_sheet():
    result = parse_linkedin_export_xlsx(FIXTURE)
    assert "demographics" in result
    demos = result["demographics"]
    assert len(demos) == 7
    company_size = [d for d in demos if d["category"] == "Company size"]
    assert len(company_size) == 2
    assert company_size[0]["value"] == "51-200 employees"
    assert company_size[0]["percentage"] == 30.0


def test_parse_missing_file():
    result = parse_linkedin_export_xlsx("/nonexistent/file.xlsx")
    assert result == {}