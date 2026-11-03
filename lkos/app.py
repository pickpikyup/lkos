"""组装各模块：CLI 和测试都通过这里获得服务实例。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lkos.config import Config, default_home, load_config
from lkos.gateway.gateway import ModelGateway
from lkos.indexing import tokenize
from lkos.indexing.indexer import Indexer
from lkos.indexing.scheduler import ActivityMarker, GateConfig, IndexGate
from lkos.qa.service import QAService
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

    @classmethod
    def open(cls, home: Path | None = None, config_path: Path | None = None,
             overrides: dict | None = None, providers: dict | None = None) -> "App":
        home = Path(home).expanduser().resolve() if home else default_home()
        home.mkdir(parents=True, exist_ok=True)
        tokenize.set_cache_dir(home / "cache")
        cfg = load_config(config_path or home / CONFIG_NAME, overrides=overrides)
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
        )

    def index_gate(self, on_pause=None, **probes) -> IndexGate:
        i = self.cfg.indexing
        return IndexGate(GateConfig(i.idle_seconds, i.require_ac_power, i.poll_seconds), self.marker,
                         on_pause=on_pause, **probes)

    @property
    def db_path(self) -> Path:
        return self.home / DB_NAME

    def close(self) -> None:
        self.repo.conn.close()  # 重复关闭是安全的
