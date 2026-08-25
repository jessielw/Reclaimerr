from backend.api.candidate_views import normalize_reason_parts


def test_candidate_reason_parts_use_current_rule_descriptions() -> None:
    reason_parts = normalize_reason_parts(
        [
            {
                "rule_id": 7,
                "rule_name": "Short name",
                "target_scope": "movie_version",
                "conditions": [],
            }
        ],
        {},
        {7: "Current explanation"},
    )

    assert reason_parts[0].rule_description == "Current explanation"


def test_candidate_reason_parts_omit_deleted_rule_descriptions() -> None:
    reason_parts = normalize_reason_parts(
        [
            {
                "rule_id": 7,
                "rule_name": "Deleted rule",
                "target_scope": "movie_version",
                "conditions": [],
            }
        ],
        {},
        {},
    )

    assert reason_parts[0].rule_description is None
