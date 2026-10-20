"""Focused regression tests that do not depend on checked-in SAS fixtures."""

from pathlib import Path
import types

import pandas as pd
import pytest

from sas_local_mcp import dataset as ds


def _fake_meta():
    return types.SimpleNamespace(
        readstat_variable_types={"ARM": "double"},
        original_variable_informats={},
        original_variable_formats={},
        variable_value_labels={"ARM": {1.0: "Placebo", 2.0: "Drug"}},
        column_labels=["Treatment"],
    )


def test_apply_labels_does_not_mutate_cached_frame(monkeypatch):
    frame = pd.DataFrame({"ARM": [1.0, 2.0]})
    meta = _fake_meta()
    monkeypatch.setattr(ds, "_read", lambda _path, metadata_only=False: (frame, meta))
    path = Path("fixture.sas7bdat")

    labelled = ds.read_rows(path, apply_labels=True)
    raw = ds.read_rows(path, apply_labels=False)

    assert [row["ARM"] for row in labelled["records"]] == ["Placebo", "Drug"]
    assert [row["ARM"] for row in raw["records"]] == [1.0, 2.0]
    assert frame["ARM"].tolist() == [1.0, 2.0]


@pytest.mark.parametrize(
    "sql",
    ["SELECT 1; SELECT 2", "WITH x AS (SELECT 1) DELETE FROM dataset"],
)
def test_query_dataset_accepts_only_one_select(monkeypatch, sql):
    frame = pd.DataFrame({"value": [1]})
    monkeypatch.setattr(ds, "_read", lambda _path, metadata_only=False: (frame, None))

    with pytest.raises(ds.DatasetError, match="单条 SELECT"):
        ds.query_dataset(Path("fixture.sas7bdat"), sql)


def test_query_dataset_cannot_read_external_files(monkeypatch):
    source = Path(__file__).resolve()
    frame = pd.DataFrame({"value": [1]})
    monkeypatch.setattr(ds, "_read", lambda _path, metadata_only=False: (frame, None))

    with pytest.raises(ds.DatasetError, match="SQL 执行失败"):
        ds.query_dataset(Path("fixture.sas7bdat"), f"SELECT * FROM read_csv('{source}')")


def test_search_variables_reports_scan_truncation(monkeypatch):
    paths = [Path("one.sas7bdat"), Path("two.sas7bdat")]
    frame = pd.DataFrame({"USUBJID": ["A"]})
    monkeypatch.setattr(ds, "_iter_dataset_files", lambda *args, **kwargs: iter(paths))
    monkeypatch.setattr(ds, "_read", lambda _path, metadata_only=False: (frame, None))

    result = ds.search_variables("USUBJID", max_datasets=1)

    assert result["datasets_scanned"] == 1
    assert result["scan_truncated"] is True
    assert result["truncated"] is True

