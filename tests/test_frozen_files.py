from scripts.verify_frozen_files import verify_frozen_files


def test_user_approved_frozen_files_match_baseline() -> None:
    assert verify_frozen_files() == []
