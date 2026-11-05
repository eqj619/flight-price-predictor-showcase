import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """テストごとに使い捨ての DB を用意する。

    get_settings は lru_cache されているので、環境変数を差し替えたら
    必ずキャッシュを捨てる。捨て忘れると前のテストの DB を掴んだまま走る。
    """
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    # TestClient は lifespan を走らせるので、切っておかないとテストごとに
    # スケジューラのスレッドが立つ。
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # run_daily は KPI の CSV も書く。DB だけ隔離しても、これを外すと
    # テストが本物の data/kpi_daily.csv に行を足してしまう。
    monkeypatch.setenv("KPI_CSV_PATH", str(tmp_path / "kpi_daily.csv"))
    monkeypatch.setenv("KPI_S3_BUCKET", "")
    # 影の機械学習のモデルも同じ。外すと本物の data/drop_model.pkl を書く。
    monkeypatch.setenv("ML_MODEL_PATH", str(tmp_path / "drop_model.pkl"))
    get_settings.cache_clear()

    from app.db import connection
    connection.init_db()

    yield

    get_settings.cache_clear()
