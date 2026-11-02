"""Correctness, bounded reads, concurrency and MCP contracts for optimizations 1–6."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace

import duckdb
import pandas as pd
import pyreadstat
import pytest
from fastmcp import Client

from sas_local_mcp import dataset as ds, responses
from sas_local_mcp.cache import ReadCache
from sas_local_mcp.stdio_server import mcp


def meta_for(frame, *, labels=None, missing=None, formats=None):
    return SimpleNamespace(
        number_rows=len(frame), column_labels=list(frame.columns),
        readstat_variable_types={name: "string" if name == "TEXT" else "double" for name in frame.columns},
        original_variable_types=formats or {}, original_variable_informats={},
        variable_value_labels=labels or {}, missing_user_values=missing or {},
    )


@pytest.fixture(autouse=True)
def empty_caches():
    ds.clear_caches()
    yield
    ds.clear_caches()


def labelled_sources(monkeypatch):
    frame = pd.DataFrame({"ARM": [1.0, 2.0]})
    main = meta_for(frame, labels={"ARM": {1.0: "Placebo", 2.0: "Drug"}})
    other = meta_for(frame, labels={"ARM": {1.0: "Low", 2.0: "High"}})
    monkeypatch.setattr(ds, "_read", lambda path, metadata_only=False: (frame, other if path.stem == "lb" else main))
    return Path("main.sas7bdat")


def test_sql_decoding_tracks_aliases_and_source_tables(monkeypatch):
    path = labelled_sources(monkeypatch)
    result = ds.query_dataset(path, "SELECT l.ARM AS dose, d.ARM AS treatment FROM dataset d JOIN lb l USING (ARM) ORDER BY l.ARM",
                              extra_paths=[Path("lb.sas7bdat")], apply_labels=True)
    assert result["records"] == [{"dose": "Low", "treatment": "Placebo"}, {"dose": "High", "treatment": "Drug"}]
    assert result["label_decoding"]["dose"]["source_table"] == "lb"
    assert result["label_decoding"]["treatment"]["values_decoded"] == 2


@pytest.mark.parametrize("sql", ["SELECT SUM(ARM) - 1 AS ARM FROM dataset", "WITH x AS (SELECT ARM FROM dataset) SELECT ARM FROM x"])
def test_sql_expressions_and_nested_scopes_do_not_guess_labels(monkeypatch, sql):
    path = labelled_sources(monkeypatch)
    result = ds.query_dataset(path, sql, apply_labels=True)
    assert not result["value_labels_applied"]
    assert result["label_decoding"]["ARM"]["status"] == "source_unknown"
    assert all(isinstance(row["ARM"], float) for row in result["records"])


def test_explicit_label_source_and_duplicate_output_names(monkeypatch):
    path = labelled_sources(monkeypatch)
    result = ds.query_dataset(path, "WITH x AS (SELECT ARM FROM dataset) SELECT ARM AS treatment FROM x",
                              apply_labels=True, label_sources={"treatment": {"table": "dataset", "column": "ARM"}})
    assert result["records"][0]["treatment"] == "Placebo"
    duplicate = ds.query_dataset(path, "SELECT ARM, ARM FROM dataset", apply_labels=True)
    assert not duplicate["value_labels_applied"]
    with pytest.raises(ds.DatasetError, match="无效的标签来源"):
        ds.query_dataset(path, "SELECT ARM FROM dataset", apply_labels=True,
                         label_sources={"ARM": {"table": "unknown", "column": "ARM"}})


def test_no_matching_codes_means_no_labels_applied(monkeypatch):
    path = labelled_sources(monkeypatch)
    result = ds.query_dataset(path, "SELECT ARM FROM dataset WHERE ARM > 5", apply_labels=True)
    assert result["records"] == []
    assert not result["value_labels_applied"]
    assert result["label_decoding"]["ARM"]["values_decoded"] == 0


def test_pivot_and_star_replacements_never_borrow_raw_column_labels(monkeypatch):
    frame = pd.DataFrame({"ARM": [1.0, 1.0], "TEXT": ["x", "x"]})
    meta = meta_for(frame, labels={"ARM": {2.0: "wrong label"}})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta))
    for sql in ("SELECT * FROM dataset PIVOT (SUM(ARM) FOR TEXT IN ('x' AS ARM))",
                "SELECT * REPLACE (SUM(ARM) OVER () AS ARM) FROM dataset"):
        result = ds.query_dataset(Path("fake.sas7bdat"), sql, apply_labels=True)
        assert not result["value_labels_applied"]
        assert result["records"][0]["ARM"] == 2.0


def fake_file_reader(monkeypatch, tmp_path, frame, meta=None):
    path = tmp_path / "sample.sas7bdat"
    path.write_bytes(b"fake SAS file read through a controlled reader")
    meta = meta or meta_for(frame)
    calls = []
    def reader(_path, metadataonly=False, **options):
        assert options.get("user_missing") is True
        calls.append((metadataonly, dict(options)))
        result = frame.iloc[:0] if metadataonly else frame
        if options.get("usecols") is not None:
            result = result[options["usecols"]]
        start = options.get("row_offset", 0)
        stop = start + options["row_limit"] if options.get("row_limit") else None
        return result.iloc[start:stop].copy(), meta
    monkeypatch.setattr(pyreadstat, "read_sas7bdat", reader)
    return path, calls


def test_preview_pushes_columns_and_page_into_reader(monkeypatch, tmp_path):
    frame = pd.DataFrame({"ARM": range(20), "TEXT": ["x"] * 20})
    path, calls = fake_file_reader(monkeypatch, tmp_path, frame)
    result = ds.read_rows(path, columns=["TEXT"], offset=5, limit=2)
    assert result["records"] == [{"TEXT": "x"}, {"TEXT": "x"}]
    data_calls = [options for metadata, options in calls if not metadata]
    assert len(data_calls) == 1
    assert data_calls[0]["usecols"] == ["TEXT"]
    assert data_calls[0]["row_offset"] == 5
    assert data_calls[0]["row_limit"] == 3
    assert result["next_offset"] == 7 and result["total_rows"] == 20


def test_file_change_between_header_and_page_is_reported(monkeypatch, tmp_path):
    path, _ = fake_file_reader(monkeypatch, tmp_path, pd.DataFrame({"ARM": [1.0]}))
    original = ds._read
    def read(path, metadata_only, **kwargs):
        result = original(path, metadata_only, **kwargs)
        if metadata_only:
            path.write_bytes(b"changed file after metadata was read")
        return result
    monkeypatch.setattr(ds, "_read", read)
    with pytest.raises(ds.DatasetError) as exc:
        ds.read_rows(path)
    assert exc.value.code == "FILE_CHANGED"


def test_chunk_filter_precedes_projection_and_can_stop_early(monkeypatch, tmp_path):
    frame = pd.DataFrame({"ARM": range(20), "TEXT": [str(i) for i in range(20)]})
    path, calls = fake_file_reader(monkeypatch, tmp_path, frame)
    monkeypatch.setattr(ds, "CHUNK_ROWS", 4)
    result = ds.read_rows(path, where="ARM >= 3 and ARM < 10", columns=["TEXT"], offset=1, limit=2)
    assert result["records"] == [{"TEXT": "4"}, {"TEXT": "5"}]
    assert result["rows_after_filter"] == 7 and result["total_rows"] == 20
    assert all(options["row_limit"] == 4 for metadata, options in calls if not metadata)
    calls.clear()
    result = ds.read_rows(path, where="ARM >= 3", columns=["TEXT"], limit=2, include_row_count=False)
    assert result["rows_after_filter"] is None and result["truncated"]
    assert len([1 for metadata, _ in calls if not metadata]) == 2


@pytest.mark.parametrize("expression", ["ARM.__class__", "ARM.mean() > 0", "@value", "TEXT * 1000000000 == 'x'", "[1] * 1000000000 == ARM"])
def test_filter_rejects_calls_attributes_and_object_expansion(monkeypatch, expression):
    frame = pd.DataFrame({"ARM": [1], "TEXT": ["x"]})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta_for(frame)))
    with pytest.raises(ds.DatasetError) as exc:
        ds.read_rows(Path("fake.sas7bdat"), where=expression)
    assert exc.value.code == "INVALID_FILTER"


def test_xport_counts_stream_without_caching_a_full_frame(monkeypatch, tmp_path):
    frame = pd.DataFrame({"ID": range(13), "TEXT": ["x"] * 13})
    path = tmp_path / "sample.xpt"
    pyreadstat.write_xport(frame, path, file_format_version=5)
    monkeypatch.setattr(ds, "CHUNK_ROWS", 4)
    fast = ds.read_metadata(path, include_row_count=False)
    assert fast["number_rows"] is None and not fast["row_count_exact"]
    counted = ds.read_metadata(path)
    assert counted["number_rows"] == 13
    assert len(ds._CACHE) == 0
    page = ds.read_rows(path, columns=["ID"], offset=10, limit=2)
    assert page["records"] == [{"ID": 10.0}, {"ID": 11.0}]
    assert page["total_rows"] == 13 and page["truncated"]


def test_sql_materializes_only_page_and_count_is_optional(monkeypatch):
    frame = pd.DataFrame({"ARM": [1]})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, None))
    original = duckdb.connect
    limits, fetched, configs = [], [], []
    class Relation:
        def __init__(self, relation, bounded=False):
            self.relation, self.bounded = relation, bounded
        def limit(self, limit, offset):
            limits.append((limit, offset))
            return Relation(self.relation.limit(limit, offset), True)
        def fetchdf(self):
            assert self.bounded
            frame = self.relation.fetchdf()
            fetched.append(len(frame))
            return frame
        def aggregate(self, expression):
            raise AssertionError("include_total=False must not count the complete result")
    class Connection:
        def __init__(self, **kwargs):
            configs.append(kwargs["config"])
            self.connection = original(**kwargs)
        def register(self, *args):
            return self.connection.register(*args)
        def sql(self, sql):
            return Relation(self.connection.sql(sql))
        def unregister(self, *args):
            return self.connection.unregister(*args)
        def close(self):
            self.connection.close()
    monkeypatch.setattr(duckdb, "connect", Connection)
    result = ds.query_dataset(Path("fake.sas7bdat"), "SELECT i FROM range(1000000) t(i) ORDER BY i; -- trailing comment",
                              limit=2, offset=500, include_total=False)
    assert result["records"] == [{"i": 500}, {"i": 501}]
    assert result["total_rows"] is None and not result["row_count_exact"]
    assert limits == [(3, 500)] and fetched == [3]
    assert configs[0]["enable_external_access"] == "false"
    assert configs[0]["memory_limit"] and configs[0]["threads"]


def test_sql_count_respects_original_limit_and_reports_ordering(monkeypatch):
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (pd.DataFrame({"x": [1]}), None))
    result = ds.query_dataset(Path("fake.sas7bdat"), "SELECT * FROM range(30) LIMIT 10", limit=2, offset=8)
    assert result["total_rows"] == 10 and result["returned"] == 2 and not result["truncated"]
    assert not result["order_by_present"] and result["warnings"]


def test_metadata_search_does_not_evict_data_and_revisions_invalidate(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", tmp_path)
    monkeypatch.setattr(ds, "CACHE_SIZE", 1)
    frame = pd.DataFrame({"ARM": [1]})
    calls = []
    def load(path, metadata_only):
        calls.append((path, metadata_only))
        return frame, None
    monkeypatch.setattr(ds, "_read_uncached", load)
    paths = [tmp_path / f"{i}.sas7bdat" for i in range(6)]
    for path in paths:
        path.write_bytes(b"original")
    ds._read(paths[0], False)
    ds.search_variables("ARM")
    ds._read(paths[0], False)
    assert sum(not metadata for _, metadata in calls) == 1
    paths[0].write_bytes(b"new revision with a different size")
    ds._read(paths[0], False)
    assert sum(not metadata for _, metadata in calls) == 2
    monkeypatch.setattr(ds, "ENCODING", "cp1252")
    ds._read(paths[0], False)
    assert sum(not metadata for _, metadata in calls) == 3


def test_cache_byte_budget_and_concurrent_single_flight():
    cache = ReadCache()
    barrier = Barrier(6)
    lock = Lock()
    loads = []
    def load():
        with lock:
            loads.append(1)
        time.sleep(0.05)
        return "large"
    def worker():
        barrier.wait(timeout=3)
        return cache.get_or_load(("file", 1, 1, False), load, capacity=8, max_bytes=100, weigh=lambda value: len(value))
    with ThreadPoolExecutor(max_workers=6) as executor:
        assert list(executor.map(lambda _: worker(), range(6))) == ["large"] * 6
    assert len(loads) == 1
    assert len(cache) == 1 and cache.size_bytes == 5
    small_cache = ReadCache()
    for _ in range(2):
        assert small_cache.get_or_load(("file", 1, 1, False), lambda: "large", capacity=8,
                                       max_bytes=4, weigh=len) == "large"
    assert len(small_cache) == 0 and small_cache.size_bytes == 0


def test_failed_single_flight_can_be_retried():
    cache = ReadCache()
    options = {"capacity": 2, "max_bytes": 100, "weigh": lambda value: 1}
    with pytest.raises(ValueError):
        cache.get_or_load(("file", 1, 1, False), lambda: int("bad"), **options)
    assert cache.get_or_load(("file", 1, 1, False), lambda: 42, **options) == 42


def test_catalog_enriches_labels_without_replacing_cached_raw_codes(monkeypatch, tmp_path):
    frame = pd.DataFrame({"ARM": [1.0, 2.0]})
    meta = meta_for(frame, formats={"ARM": "ARMFMT8."})
    path, calls = fake_file_reader(monkeypatch, tmp_path, frame, meta)
    catalog = tmp_path / "formats.sas7bcat"
    catalog.write_bytes(b"first")
    def read_catalog(path, **kwargs):
        drug = "Drug" if Path(path).read_bytes() == b"first" else "Updated drug"
        return pd.DataFrame(), SimpleNamespace(value_labels={"ARMFMT": {1.0: "Placebo", 2.0: drug}})
    monkeypatch.setattr(pyreadstat, "read_sas7bcat", read_catalog)
    labels = ds.get_value_labels(path, catalog_path=catalog)
    assert labels["variables"]["ARM"]["1"] == "Placebo"
    assert ds.read_rows(path, catalog_path=catalog)["records"][0]["ARM"] == 1.0
    assert ds.read_rows(path, catalog_path=catalog, apply_labels=True)["records"][0]["ARM"] == "Placebo"
    assert all("catalog_file" not in options for _, options in calls)
    assert frame["ARM"].tolist() == [1.0, 2.0] and meta.variable_value_labels == {}
    catalog.write_bytes(b"updated catalog revision")
    assert ds.get_value_labels(path, catalog_path=catalog)["variables"]["ARM"]["2"] == "Updated drug"


def test_sas_missing_semantics_preserve_tags_but_exclude_them_from_statistics(monkeypatch):
    frame = pd.DataFrame({"ARM": [1.0, "A", "_", None], "TEXT": ["A", "", " ", None]})
    meta = meta_for(frame, missing={"ARM": ["A", "_"]})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta))
    path = Path("fake.sas7bdat")
    page = ds.read_rows(path)
    assert [row["ARM"] for row in page["records"]] == [1.0, ".A", "._", None]
    summary = ds.summarize_dataset(path)
    assert all(item["n_missing"] == 3 and item["n_distinct"] == 1 for item in summary["summaries"])
    assert summary["summaries"][0]["stats"]["mean"] == 1.0
    freq = ds.frequency(path, "ARM", include_missing=True)
    assert freq["missing_details"] == {".": 1, ".A": 1, "._": 1}
    assert sum(level["n"] for level in freq["levels"]) == 4
    assert ds.frequency(path, "TEXT", missing_policy="pandas")["n_missing"] == 1
    query = ds.query_dataset(path, "SELECT AVG(ARM) AS mean, COUNT(TEXT) AS n FROM dataset")
    assert query["records"] == [{"mean": 1.0, "n": 1}]
    filtered = ds.read_rows(path, columns=["TEXT"], where="ARM is None")
    assert filtered["returned"] == 3


def test_sas_blank_means_ascii_spaces_not_tabs_or_unicode_whitespace(monkeypatch):
    frame = pd.DataFrame({"TEXT": ["", " ", "\t", "\u00a0", "\u3000", "."]})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta_for(frame)))
    result = ds.frequency(Path("fake.sas7bdat"), "TEXT")
    assert result["n_missing"] == 2
    assert result["n_distinct"] == 4
    assert result["missing_details"] == {"blank": 2}


def test_tags_discovered_only_during_page_read_are_retained(monkeypatch, tmp_path):
    frame = pd.DataFrame({"ARM": [1.0, "A"]})
    path, _ = fake_file_reader(monkeypatch, tmp_path, frame)
    original = pyreadstat.read_sas7bdat
    def reader(*args, **kwargs):
        data, meta = original(*args, **kwargs)
        if not kwargs.get("metadataonly"):
            meta = meta_for(frame, missing={"ARM": ["A"]})
        return data, meta
    monkeypatch.setattr(pyreadstat, "read_sas7bdat", reader)
    assert ds.read_rows(path)["records"][1]["ARM"] == ".A"


def test_date_time_metadata_and_counts_only_statistics(monkeypatch):
    frame = pd.DataFrame({"DATE": [1.0], "TIME": [2.0], "DATETIME": [3.0]})
    meta = meta_for(frame, formats={"DATE": "DATE9.", "TIME": "TIME8.", "DATETIME": "E8601DT19."})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta))
    metadata = ds.read_metadata(Path("fake.sas7bdat"))
    assert [col["logical_type"] for col in metadata["columns"]] == ["date", "time", "datetime"]
    assert metadata["columns"][0]["sas_unit"] == "days_since_1960-01-01"
    monkeypatch.setattr(ds, "_datetime_stats", lambda *_: pytest.fail("counts mode must skip distribution calculations"))
    summary = ds.summarize_dataset(Path("fake.sas7bdat"), stats_mode="counts", offset=1, limit=1)
    assert summary["n_columns"] == 3 and summary["returned"] == 1
    assert summary["summaries"][0]["stats"] == {} and summary["next_offset"] == 2


def test_search_paginates_matches_and_scan_windows(monkeypatch, tmp_path):
    paths = [tmp_path / f"{i}.sas7bdat" for i in range(5)]
    monkeypatch.setattr(ds, "_iter_dataset_files", lambda *args, **kwargs: iter(paths))
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (pd.DataFrame({"ARM": [1]}), None))
    first = ds.search_variables("ARM", limit=1, max_datasets=3)
    assert first["next_offset"] == 1 and first["next_scan_offset"] == 3
    tail = ds.search_variables("ARM", limit=5, max_datasets=3, offset=1)
    assert len(tail["matches"]) == 2 and tail["next_offset"] is None
    next_window = ds.search_variables("ARM", max_datasets=3, scan_offset=3)
    assert len(next_window["matches"]) == 2 and not next_window["scan_truncated"]


def test_response_budget_counts_utf8_and_continues_at_the_actual_page(monkeypatch, tmp_path):
    for index in range(12):
        (tmp_path / f"{index:02}-中文名称.sas7bdat").write_bytes(b"x")
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", tmp_path)
    payload = ds.list_datasets()
    monkeypatch.setattr(responses, "MAX_RESPONSE_BYTES", 650)
    first = responses.respond(responses.Listing, payload).model_dump(mode="json")
    assert first["response_bytes"] == len(json.dumps(first, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= 650
    assert first["response_truncated"] and first["next_offset"] == first["returned"]
    second = responses.respond(responses.Listing, ds.list_datasets(offset=first["next_offset"])).model_dump(mode="json")
    assert first["datasets"][-1]["path"] != second["datasets"][0]["path"]


def test_budget_shortening_keeps_label_diagnostics_accurate(monkeypatch):
    frame = pd.DataFrame({"ARM": [0.0] * 3 + [1.0] * 3, "TEXT": ["x" * 100] * 6})
    meta = meta_for(frame, labels={"ARM": {1.0: "Drug"}})
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta))
    payload = ds.query_dataset(Path("fake.sas7bdat"), "SELECT ARM, TEXT FROM dataset ORDER BY ARM", apply_labels=True)
    monkeypatch.setattr(responses, "MAX_RESPONSE_BYTES", 1100)
    result = responses.respond(responses.Query, payload)
    assert 0 < result.returned <= 3
    assert not result.value_labels_applied and result.label_decoding["ARM"].values_decoded == 0
    monkeypatch.setattr(responses, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(ds.DatasetError) as exc:
        responses.respond(responses.Query, payload)
    assert exc.value.code == "RESPONSE_TOO_LARGE"


def test_nonfinite_values_are_strict_json_safe():
    records = ds._to_records(pd.DataFrame({"VALUE": [float("inf"), float("-inf"), float("nan")]}))
    assert records == [{"VALUE": None}] * 3
    json.dumps(records, allow_nan=False)


async def test_published_schemas_and_label_source_input_work_over_mcp(monkeypatch, tmp_path):
    frame = pd.DataFrame({"ARM": [1.0, 2.0]})
    meta = meta_for(frame, labels={"ARM": {1.0: "Placebo", 2.0: "Drug"}})
    path = tmp_path / "main.sas7bdat"
    path.write_bytes(b"controlled input")
    monkeypatch.setattr(ds, "SAS_DATA_ROOT", tmp_path)
    monkeypatch.setattr(ds, "_read", lambda *args, **kwargs: (frame, meta))
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
        assert len(tools) == 8
        tool = tools["query_dataset"]
        schema = getattr(tool, "output_schema", None)
        if schema is None:  # MCP SDK 1.x
            schema = tool.outputSchema
        assert "records" in schema["properties"]
        result = await client.call_tool("query_dataset", {
            "path": "main.sas7bdat", "sql": "WITH x AS (SELECT ARM FROM dataset) SELECT ARM AS treatment FROM x",
            "apply_labels": True, "include_total": False,
            "label_sources": {"treatment": {"table": "dataset", "column": "ARM"}},
        })
        assert result.structured_content["records"][0]["treatment"] == "Placebo"
        with pytest.raises(Exception, match="PATH_OUTSIDE_ROOT"):
            await client.call_tool("read_dataset", {"path": "../outside.sas7bdat"})
