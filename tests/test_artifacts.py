from __future__ import annotations

from pathlib import Path

from agent import artifacts


def test_artifact_registration_and_deduplication(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "ARTIFACT_HISTORY_PATH", tmp_path / "history.json")
    output = tmp_path / "output"
    output.mkdir()
    xlsx = output / "report.xlsx"
    pptx = output / "slides.pptx"
    zip_file = output / "images.zip"
    xlsx.write_bytes(b"xlsx")
    pptx.write_bytes(b"pptx")
    zip_file.write_bytes(b"zip")

    run = artifacts.new_run("永锋钢铁", "打包带钢卷", "生成报表", output)
    result = {
        "xlsx_path": str(xlsx),
        "pptx_path": str(pptx),
        "zip_files": [str(zip_file), str(zip_file)],
    }
    artifacts.register_artifacts_from_result(run, "test_tool", result)
    artifacts.register_artifacts_from_result(run, "test_tool", result)

    assert [item.kind for item in run.artifacts] == ["xlsx", "pptx", "zip"]
    assert artifacts.artifact_files(run.to_dict()) == [str(xlsx), str(pptx), str(zip_file)]
    assert artifacts.artifact_files_by_kind(
        run.to_dict(), {"xlsx", "pptx"}
    ) == [str(xlsx), str(pptx)]
    assert artifacts.artifact_files_by_kind(
        run.to_dict(), {"zip"}
    ) == [str(zip_file)]


def test_new_images_are_scanned(tmp_path):
    output = tmp_path / "images"
    day = output / "2026-11-04"
    day.mkdir(parents=True)
    image = day / "truck_01.jpg"
    image.write_bytes(b"jpg")

    run = artifacts.new_run("盛隆钢铁", "检判原图下载", "下载原图", output)
    artifacts.scan_new_artifacts(run, output)

    assert [item.path for item in run.artifacts] == [str(image)]
    assert artifacts.image_items(run.to_dict()) == [(str(image), image.name)]


def test_history_persistence_roundtrip(tmp_path, monkeypatch):
    history_path = tmp_path / "artifact_history.json"
    monkeypatch.setattr(artifacts, "ARTIFACT_HISTORY_PATH", history_path)
    run = artifacts.new_run("镔鑫钢铁", "废钢检判", "每日统计")
    artifacts.finalize_run(run)
    session = {}

    artifacts.commit_run(session, run)
    loaded = artifacts.load_artifact_history()

    assert loaded[0]["run_id"] == run.run_id
    assert session["current_run"]["status"] == "success"
    assert history_path.is_file()


def test_corrupt_history_falls_back_to_empty(tmp_path, monkeypatch):
    history_path = tmp_path / "artifact_history.json"
    history_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(artifacts, "ARTIFACT_HISTORY_PATH", history_path)

    assert artifacts.load_artifact_history() == []
