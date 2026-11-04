"""Regressions for the duckdb 2.0 / filter / path / performance fixes."""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pyreadstat
import pytest

from sas_local_mcp import dataset as ds
from sas_local_mcp.filtering import RowFilter
from sas_local_mcp.sql_labels import referenced_columns


@pytest.fixture(autouse=True)
def empty_caches():
    ds.clear_caches()
    yield
    ds.clear_caches()


def _no_meta(monkeypatch, frame):
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, None))


@pytest.mark.parametrize("sql", [
    "SELECT x FROM dataset; -- trailing comment",
    "SELECT x FROM dataset /* a */ ; /* b */\n-- c\n",
    "SELECT ';' AS s, x FROM dataset;",
])
def test_sql_trailing_semicolon_and_comments(monkeypatch, sql):
    _no_meta(monkeypatch, pd.DataFrame({"x": [1, 2, 3]}))
    result = ds.query_dataset(Path("fake.sas7bdat"), sql, limit=2)
    assert result["total_rows"] == 3 and result["returned"] == 2
    assert result["sql"] == sql


def test_sql_total_derived_from_short_page_without_second_execution(monkeypatch):
    _no_meta(monkeypatch, pd.DataFrame({"x": range(5)}))
    import duckdb
    original = duckdb.connect
    counted = []

    class Relation:
        def __init__(self, inner):
            self.inner = inner
        def limit(self, *args, **kwargs):
            return self.inner.limit(*args, **kwargs)
        def aggregate(self, *args):
            counted.append(1)
            return self.inner.aggregate(*args)

    class Connection:
        def __init__(self, **kwargs):
            self.inner = original(**kwargs)
        def sql(self, sql):
            return Relation(self.inner.sql(sql))
        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(duckdb, "connect", Connection)
    short = ds.query_dataset(Path("fake.sas7bdat"), "SELECT x FROM dataset ORDER BY x", limit=10)
    assert short["total_rows"] == 5 and counted == []
    long = ds.query_dataset(Path("fake.sas7bdat"), "SELECT x FROM dataset ORDER BY x", limit=2)
    assert long["total_rows"] == 5 and long["truncated"] and counted == [1]
    past_end = ds.query_dataset(Path("fake.sas7bdat"), "SELECT x FROM dataset", offset=50)
    assert past_end["total_rows"] == 5 and past_end["returned"] == 0 and counted == [1, 1]


@pytest.mark.parametrize("expression", ["0 in SEX", "'F' in SEX", "AGE not in SEX", "AGE in [SEX]"])
def test_in_requires_constant_list(expression):
    with pytest.raises(ds.DatasetError) as exc:
        RowFilter(expression, ["SEX", "AGE"])
    assert exc.value.code == "INVALID_FILTER"


def test_in_with_constant_list_still_works():
    frame = pd.DataFrame({"SEX": ["F", "M", None]})
    assert RowFilter("SEX in ('F',)", ["SEX"]).apply(frame).tolist() == [True, False, False]
    assert RowFilter("SEX not in ['F']", ["SEX"]).apply(frame).tolist() == [False, True, False]


def test_blank_mask_handles_mixed_and_non_string_columns():
    mixed = pd.Series(["", "  ", "a", None, 3], dtype=object)
    assert ds._blank_mask(mixed).tolist() == [True, True, False, False, False]
    assert ds._blank_mask(pd.Series([1.0, None])).tolist() == [False, False]
    assert ds._blank_mask(pd.Series(["", "x"], dtype="string")).tolist() == [True, False]


@pytest.mark.skipif(not hasattr(Path, "is_junction") or __import__("os").name != "nt", reason="Windows junction")
def test_absolute_path_through_junction_root(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.xpt").write_bytes(b"x")
    link = tmp_path / "link"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(real)], check=True, capture_output=True)
    try:
        monkeypatch.setattr(ds, "SAS_DATA_ROOT", link.resolve())
        assert ds.resolve_dataset_path(str(link / "a.xpt")) == real / "a.xpt"
        with pytest.raises(ds.DatasetError) as exc:
            ds.resolve_dataset_path(str(tmp_path / "outside.xpt"))
        assert exc.value.code == "PATH_OUTSIDE_ROOT"
    finally:
        subprocess.run(["cmd", "/c", "rmdir", str(link)], check=False)


def test_vanished_files_do_not_abort_listing_or_search(monkeypatch, tmp_path):
    gone = tmp_path / "gone.sas7bdat"
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", tmp_path)
    monkeypatch.setattr(ds, "_iter_dataset_files", lambda *args, **kwargs: iter([gone]))
    assert ds.list_datasets()["count"] == 0
    result = ds.search_variables("USUBJID")
    assert result["datasets_scanned"] == 1 and result["errors"][0]["path"] == "gone.sas7bdat"


def _xport(tmp_path, rows=13):
    frame = pd.DataFrame({"ID": [float(i) for i in range(rows)], "TEXT": ["x"] * rows})
    path = tmp_path / "sample.xpt"
    pyreadstat.write_xport(frame, path, file_format_version=5)
    return path


def _count_reads(monkeypatch):
    calls = []
    original = ds._read_uncached
    def counting(path, metadata_only, *args, **kwargs):
        calls.append((metadata_only, kwargs))
        return original(path, metadata_only, *args, **kwargs)
    monkeypatch.setattr(ds, "_read_uncached", counting)
    return calls


def test_xport_filter_reads_once_and_reuses_cache(monkeypatch, tmp_path):
    path = _xport(tmp_path)
    monkeypatch.setattr(ds, "CHUNK_ROWS", 4)
    calls = _count_reads(monkeypatch)
    result = ds.read_rows(path, where="ID >= 10", limit=2)
    assert [r["ID"] for r in result["records"]] == [10.0, 11.0]
    assert result["rows_after_filter"] == 3 and result["total_rows"] == 13
    assert len([1 for metadata, _ in calls if not metadata]) == 1, "XPORT 过滤不应分块重复扫描"
    calls.clear()
    ds.read_rows(path, where="ID < 2")
    assert not [1 for metadata, _ in calls if not metadata], "缓存的整表应被过滤复用"


def test_xport_count_is_single_column_single_pass(monkeypatch, tmp_path):
    path = _xport(tmp_path)
    monkeypatch.setattr(ds, "CHUNK_ROWS", 4)
    calls = _count_reads(monkeypatch)
    assert ds.read_metadata(path)["number_rows"] == 13
    data = [kwargs for metadata, kwargs in calls if not metadata]
    assert len(data) == 1 and data[0]["usecols"] == ["ID"]
    assert len(ds._CACHE) == 0


def test_oversized_files_read_only_needed_columns(monkeypatch, tmp_path):
    path = _xport(tmp_path)
    monkeypatch.setattr(ds, "CACHE_MAX_BYTES", 1)
    calls = _count_reads(monkeypatch)
    freq = ds.frequency(path, "TEXT")
    assert freq["levels"][0] == {"value": "x", "label": None, "n": 13, "pct": 100.0}
    summary = ds.summarize_dataset(path, columns=["ID"])
    assert summary["summaries"][0]["stats"]["max"] == 12.0 and summary["total_rows"] == 13
    query = ds.query_dataset(path, "SELECT COUNT(*) AS n, MAX(ID) AS m FROM dataset")
    assert query["records"] == [{"n": 13, "m": 12.0}]
    projected = [kwargs.get("usecols") for metadata, kwargs in calls if not metadata]
    assert projected == [["TEXT"], ["ID"], ["ID"]]


def test_query_pruning_keeps_results_identical(monkeypatch, tmp_path):
    path = _xport(tmp_path)
    monkeypatch.setattr(ds, "CACHE_MAX_BYTES", 1)
    for sql in ("SELECT * FROM dataset ORDER BY ID", "SELECT COUNT(*) AS n FROM dataset",
                "SELECT TEXT, COUNT(*) AS n FROM dataset GROUP BY ALL"):
        assert ds.query_dataset(path, sql)["records"], sql


@pytest.mark.parametrize("sql", [
    "SELECT * FROM dataset", "SELECT d.* FROM dataset d", "FROM dataset",
    "SELECT COLUMNS('A.*') FROM dataset", "SELECT #1 FROM dataset",
    "SELECT * FROM dataset NATURAL JOIN lb", "SELECT dataset FROM dataset",
    "SELECT d FROM dataset d", "PIVOT dataset ON A USING SUM(B)",
])
def test_referenced_columns_refuses_implicit_expansion(sql):
    assert referenced_columns(sql, {"dataset": ["A", "B"], "lb": ["A", "C"]}) is None


def test_referenced_columns_collects_case_insensitively():
    tables = {"dataset": ["USUBJID", "ARM", "AGE"], "lb": ["USUBJID", "LBTEST"]}
    sql = "SELECT d.arm, COUNT(*) FROM dataset d JOIN lb USING (usubjid) WHERE \"AGE\" > 1 GROUP BY 1"
    assert referenced_columns(sql, tables) == {"dataset": ["USUBJID", "ARM", "AGE"], "lb": ["USUBJID"]}
    assert referenced_columns("SELECT COUNT(*) FROM dataset", tables) == {"dataset": ["USUBJID"], "lb": ["USUBJID"]}
