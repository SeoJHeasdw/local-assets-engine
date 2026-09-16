import json

import pytest

from local_assets_engine.recipes.image import match_generated_files


def write_candidate(directory, name, seed=None):
    (directory / f"{name}.png").write_bytes(b"png")
    if seed is not None:
        (directory / f"{name}.metadata.json").write_text(json.dumps({"seed": seed}), "utf-8")


def test_metadata_decides_which_file_belongs_to_a_seed(tmp_path):
    # 파일 이름이 시드를 말해 주지 않아도 메타데이터로 짝짓는다.
    write_candidate(tmp_path, "candidate", seed=18)
    write_candidate(tmp_path, "candidate_1", seed=7)
    assert [path.name for path in match_generated_files(tmp_path, [7, 18])] == ["candidate_1.png", "candidate.png"]


def test_falls_back_to_the_seed_suffix_when_metadata_is_missing(tmp_path):
    write_candidate(tmp_path, "candidate_seed_7")
    write_candidate(tmp_path, "candidate_seed_18")
    assert [path.name for path in match_generated_files(tmp_path, [18, 7])] == ["candidate_seed_18.png", "candidate_seed_7.png"]


def test_broken_metadata_does_not_hide_a_usable_file(tmp_path):
    write_candidate(tmp_path, "candidate_seed_7")
    (tmp_path / "candidate_seed_7.metadata.json").write_text("{broken", "utf-8")
    assert [path.name for path in match_generated_files(tmp_path, [7])] == ["candidate_seed_7.png"]


def test_missing_seeds_are_named_in_the_error(tmp_path):
    write_candidate(tmp_path, "candidate_seed_7", seed=7)
    with pytest.raises(RuntimeError, match="시드 8"):
        match_generated_files(tmp_path, [7, 8])
