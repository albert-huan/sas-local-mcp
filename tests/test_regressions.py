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



@pytest.mark.parametrize("fmt,kind", [
    ("YYMMDD10.", "date"), ("YYMMDDN8.", "date"), ("MMDDYY10.", "date"), ("DDMMYY8.", "date"),
    ("IS8601DA10.", "date"), ("DATE9.", "date"), ("IS8601DT.", "datetime"), ("DATETIME20.", "datetime"),
    ("TOD8.", "time"), ("IS8601TM.", "time"), ("BEST12.", None), ("8.1", None),
])
def test_temporal_kind_covers_pyreadstat_date_formats(fmt, kind):
    assert ds._temporal_kind(fmt) == kind


def test_yymmdd_date_column_is_not_blanked_by_numeric_coercion():
    from datetime import date
    meta = types.SimpleNamespace(
        readstat_variable_types={"DT": "double"}, original_variable_informats={},
        original_variable_formats={"DT": "YYMMDD10."}, variable_value_labels={}, column_labels=[""],
    )
    frame = pd.DataFrame({"DT": pd.Series([date(2020, 1, 1), date(2021, 5, 5)], dtype=object)})
    assert ds._column_type(meta, "DT", frame["DT"].dtype) == "datetime"


@pytest.mark.parametrize("expr", ["AGE == None", "AGE != None", "None == AGE"])
def test_where_rejects_equality_with_none(expr):
    from sas_local_mcp.errors import DatasetError
    from sas_local_mcp.filtering import RowFilter
    with pytest.raises(DatasetError, match="is None"):
        RowFilter(expr, ["AGE"])


def test_where_is_none_still_works():
    from sas_local_mcp.filtering import RowFilter
    frame = pd.DataFrame({"AGE": [1.0, None, 3.0]})
    assert RowFilter("AGE is None", ["AGE"]).apply(frame).tolist() == [False, True, False]
    assert RowFilter("AGE is not None", ["AGE"]).apply(frame).tolist() == [True, False, True]


def test_catalog_exact_format_name_beats_width_prefix():
    catalog = {"ARM": {1: "a"}, "ARM2": {1: "b"}}
    assert ds._catalog_match(catalog, "ARM2.") == {1: "b"}
    assert ds._catalog_match(catalog, "ARM.") == {1: "a"}
    assert ds._catalog_match(catalog, "ARM8.") == {1: "a"}
    assert ds._catalog_match(catalog, "arm2") == {1: "b"}
    assert ds._catalog_match(catalog, "OTHER.") is None


@pytest.mark.parametrize("sql", [
    "SELECT column_name FROM pragma_table_info('dataset')",
    "SELECT column_name FROM information_schema.columns WHERE table_name = 'dataset'",
    "SELECT a FROM main.dataset",
])
def test_sql_column_pruning_falls_back_for_metadata_queries(sql):
    from sas_local_mcp.sql_labels import referenced_columns
    assert referenced_columns(sql, {"dataset": ["A", "B", "C"]}) is None


def test_sql_column_pruning_still_prunes_plain_queries():
    from sas_local_mcp.sql_labels import referenced_columns
    assert referenced_columns("SELECT b FROM dataset d WHERE d.c > 1", {"dataset": ["A", "B", "C"]}) == {"dataset": ["B", "C"]}


def test_read_cache_evicts_stale_revision_across_column_subsets():
    from sas_local_mcp.cache import ReadCache
    cache = ReadCache()
    opts = dict(capacity=8, max_bytes=1000, weigh=lambda _: 1)
    cache.get_or_load(("f", 1, 10, False, None, None, ("A",)), lambda: "a1", **opts)
    cache.get_or_load(("f", 1, 10, False, None, None, ("B",)), lambda: "b1", **opts)
    cache.get_or_load(("g", 1, 10, False, None, None, ("A",)), lambda: "g1", **opts)
    cache.get_or_load(("f", 2, 11, False, None, None, ("A",)), lambda: "a2", **opts)
    assert len(cache) == 2  # f 的两个旧列子集都被淘汰，g 不受影响
    assert cache.peek(("f", 1, 10, False, None, None, ("B",))) is None
    assert cache.peek(("g", 1, 10, False, None, None, ("A",))) == "g1"


TS_XPT = Path(__file__).resolve().parent / "xpt" / "sdtm" / "ts.xpt"  # 含 cp1252 字节 0x92


@pytest.fixture
def fresh_encoding_state(monkeypatch):
    monkeypatch.setattr(ds, "ENCODING", None)
    ds.clear_caches()
    yield
    ds.clear_caches()


def _tsval(page):
    return next(v for r in page["records"] for v in r.values() if isinstance(v, str) and "Alzheimer" in v and "s Disease" in v)


@pytest.mark.skipif(not TS_XPT.exists(), reason="缺少 ts.xpt")
def test_default_read_falls_back_and_reports_encoding(fresh_encoding_state):
    page = ds.read_rows(TS_XPT, limit=50)
    assert page["encoding_fallback"] == "cp1252"
    assert "’" in _tsval(page)  # cp1252 的 0x92 是右单引号
    again = ds.read_rows(TS_XPT, limit=50)  # 第二次直接使用记住的编码
    assert again["encoding_fallback"] == "cp1252"


@pytest.mark.skipif(not TS_XPT.exists(), reason="缺少 ts.xpt")
def test_explicit_encoding_is_used_and_not_reported_as_fallback(fresh_encoding_state):
    page = ds.read_rows(TS_XPT, limit=50, encoding="latin1")
    assert page["encoding_fallback"] is None
    assert "\x92" in _tsval(page)
    other = ds.read_rows(TS_XPT, limit=50, encoding="cp1252")  # 不同编码不会命中上一次的缓存
    assert "’" in _tsval(other)


@pytest.mark.skipif(not TS_XPT.exists(), reason="缺少 ts.xpt")
def test_explicit_wrong_encoding_does_not_fall_back(fresh_encoding_state):
    with pytest.raises(ds.DatasetError, match="encoding 参数") as info:
        ds.read_rows(TS_XPT, limit=50, encoding="utf-8")
    assert info.value.code == "ENCODING_ERROR"


@pytest.mark.skipif(not TS_XPT.exists(), reason="缺少 ts.xpt")
def test_fallback_can_be_disabled(fresh_encoding_state, monkeypatch):
    monkeypatch.setattr(ds, "ENCODING_FALLBACKS", ())
    with pytest.raises(ds.DatasetError) as info:
        ds.read_rows(TS_XPT, limit=50)  # 非法字节在靠后的行里，limit 太小读不到
    assert info.value.code == "ENCODING_ERROR"


def test_unknown_encoding_is_rejected_up_front(fresh_encoding_state):
    with pytest.raises(ds.DatasetError) as info:
        ds.read_rows(Path("whatever.xpt"), encoding="not-a-codec")
    assert info.value.code == "INVALID_ARGUMENT"


def test_encoding_does_not_leak_between_calls(fresh_encoding_state):
    with pytest.raises(ds.DatasetError):
        ds.read_rows(Path("whatever.xpt"), encoding="not-a-codec")
    assert ds._explicit_encoding() is None
    ds.get_value_labels  # 装饰器保留函数名
    assert ds.read_rows.__name__ == "read_rows"


ADADAS = Path(__file__).resolve().parent / "sasdata" / "adam" / "adadas.sas7bdat"  # 磁盘约 4.4MB，解析后约 17MB


@pytest.fixture
def oversize_setup(monkeypatch):
    # 预算 8MB：磁盘体积（4.4MB）看起来放得下，实际解析后（约 17MB）放不下。
    monkeypatch.setattr(ds, "CACHE_MAX_BYTES", 8 * 1024 * 1024)
    ds.clear_caches()
    reads = []
    real = ds._read_uncached

    def spy(path, metadata_only, catalog_path=None, **options):
        if not metadata_only:
            reads.append(options.get("usecols"))
        return real(path, metadata_only, catalog_path, **options)

    monkeypatch.setattr(ds, "_read_uncached", spy)
    yield reads
    ds.clear_caches()


@pytest.mark.skipif(not ADADAS.exists(), reason="缺少 adadas.sas7bdat")
def test_oversize_full_parse_is_remembered_and_later_reads_prune_columns(oversize_setup):
    reads = oversize_setup
    assert ds._fits_cache(ADADAS) is True  # 只看磁盘体积时的初始猜测

    first = ds.frequency(ADADAS, "TRTP")
    assert reads == [None]  # 第一次：猜错了，读了全部列
    assert ds._fits_cache(ADADAS) is False  # 实测超预算后被记住

    second = ds.frequency(ADADAS, "SITEID")
    assert reads == [None, ["SITEID"]]  # 之后只读需要的列，不再整表重读
    third = ds.frequency(ADADAS, "TRTP")
    assert reads[-1] == ["TRTP"]
    assert first["levels"] == third["levels"] and second["total_rows"] == first["total_rows"]


@pytest.mark.skipif(not ADADAS.exists(), reason="缺少 adadas.sas7bdat")
def test_fitting_file_keeps_sharing_one_cached_frame(monkeypatch):
    monkeypatch.setattr(ds, "CACHE_MAX_BYTES", 64 * 1024 * 1024)
    ds.clear_caches()
    reads = []
    real = ds._read_uncached
    monkeypatch.setattr(ds, "_read_uncached", lambda path, md, cp=None, **o: (reads.append(o.get("usecols")) if not md else None, real(path, md, cp, **o))[1])
    try:
        ds.frequency(ADADAS, "TRTP")
        ds.frequency(ADADAS, "SITEID")
        assert reads == [None]  # 放得下：整表只解析一次，第二次命中缓存
        assert ds._fits_cache(ADADAS) is True
    finally:
        ds.clear_caches()


def test_oversize_memo_is_dropped_when_file_revision_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(ds, "CACHE_MAX_BYTES", 100)
    ds.clear_caches()
    target = tmp_path / "x.sas7bdat"
    target.write_bytes(b"a" * 10)
    stat = target.stat()
    ds._note_full_parse_weight(target, (str(target), stat.st_mtime_ns, stat.st_size), 10_000)
    assert ds._fits_cache(target) is False
    target.write_bytes(b"b" * 20)  # 新版本：旧记录不再适用
    assert ds._fits_cache(target) is True
    new = target.stat()
    ds._note_full_parse_weight(target, (str(target), new.st_mtime_ns, new.st_size), 10_000)
    assert len([k for k in ds._OVERSIZE if k[0] == str(target)]) == 1  # 旧版本记录被清掉
    ds.clear_caches()
