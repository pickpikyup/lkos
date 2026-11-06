"""组装各模块：CLI 和测试都通过这里获得服务实例。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lkos.config import Config, deep_merge, default_home, load_config
from lkos.gateway.gateway import ModelGateway
from lkos.indexing import tokenize
from lkos.indexing.indexer import Indexer
from lkos.indexing.scheduler import ActivityMarker, GateConfig, IndexGate
from lkos.models_config import load_models, to_overrides
from lkos.qa.service import QAService
from lkos.qa.summarize import Summarizer
from lkos.qa.extract import Extractor
from lkos.qa.analysis import Analyzer
from lkos.qa.tags import Tagger
from lkos.retrieval.retriever import Retriever
from lkos.storage.db import connect, init_schema
from lkos.storage.repository import Repository

DB_NAME = "lkos.db"
CONFIG_NAME = "config.yaml"


@dataclass
class App:
    home: Path
    cfg: Config
    repo: Repository
    gateway: ModelGateway
    indexer: Indexer
    retriever: Retriever
    qa: QAService
    marker: ActivityMarker
    summarizer: Summarizer = None
    extractor: Extractor = None
    analyzer: Analyzer = None
    tagger: Tagger = None

    @classmethod
    def open(cls, home: Path | None = None, config_path: Path | None = None,
             overrides: dict | None = None, providers: dict | None = None) -> "App":
        home = Path(home).expanduser().resolve() if home else default_home()
        home.mkdir(parents=True, exist_ok=True)
        tokenize.set_cache_dir(home / "cache")
        mc = load_models(home)   # 界面“模型”页的配置（在线 / 本机 / 向量模型）
        merged = deep_merge(to_overrides(mc), overrides or {}) if mc else overrides
        cfg = load_config(config_path or home / CONFIG_NAME, overrides=merged)
        conn = connect(home / DB_NAME)
        try:
            init_schema(conn, cfg.embedding.ref, cfg.embedding.dim)
        except BaseException:
            conn.close()
            raise
        repo = Repository(conn)
        gateway = ModelGateway(cfg, logger=repo, providers=providers)
        marker = ActivityMarker(home / "qa.active")
        retriever = Retriever(cfg, repo, gateway, marker=marker)
        return cls(
            home=home, cfg=cfg, repo=repo, gateway=gateway,
            indexer=Indexer(cfg, repo, gateway), retriever=retriever,
            qa=QAService(cfg, retriever, gateway, marker=marker), marker=marker,
            summarizer=Summarizer(cfg, repo, gateway, marker=marker),
            extractor=Extractor(cfg, repo, retriever, gateway, marker=marker),
            analyzer=Analyzer(cfg, retriever, gateway, marker=marker),
            tagger=Tagger(cfg, repo, gateway),   # 后台任务，不占用“正在问答”标记
        )

    def index_gate(self, on_pause=None, **probes) -> IndexGate:
        i = self.cfg.indexing
        return IndexGate(GateConfig(i.idle_seconds, i.require_ac_power, i.poll_seconds), self.marker,
                         on_pause=on_pause, **probes)

    @staticmethod
    def retire_old_index(home: Path | None = None) -> tuple[Path, list[str]]:
        """旧版本格式的索引无法继续使用：改名备份（不删除），返回 (备份路径, 原来的来源文件夹)。"""
        import sqlite3
        import time

        home = Path(home).expanduser().resolve() if home else default_home()
        db = home / DB_NAME
        sources: list[str] = []
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                sources = [r[0] for r in conn.execute("SELECT path FROM sources ORDER BY id")]
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        backup = home / f"{DB_NAME}.old-{time.strftime('%Y%m%d-%H%M%S')}"
        db.rename(backup)
        for suffix in ("-wal", "-shm"):
            side = home / (DB_NAME + suffix)
            if side.exists():
                side.rename(home / (backup.name + suffix))
        return backup, sources

    @property
    def db_path(self) -> Path:
        return self.home / DB_NAME

    def close(self) -> None:
        self.repo.conn.close()  # 重复关闭是安全的
