from scripts.import_dataset import DEFAULT_CSV, infer_type, read_rows


def test_original_dataset_and_conservative_inference():
    rows = read_rows(DEFAULT_CSV)
    assert len(rows) == 1000
    assert len({row["source_id"] for row in rows}) == 1000
    assert all(row["type_inferred"] and row["source"] == "dataset" for row in rows)
    assert infer_type("Встреча с Иваном") == "meeting"
    assert infer_type("Планёрка команды") == "meeting"
    assert infer_type("Напомнить про договор") == "reminder"
    assert infer_type("Подготовиться к встрече") == "task"
