"""界面“模型”页的桥接方法（混入 Bridge）：在线模型增删改测、本机模型、向量模型更换、离线模式。"""

from __future__ import annotations

from pathlib import Path

from lkos.errors import LkosError, ModelUnavailableError
from lkos.logs import get_logger
from lkos.models_config import (CUSTOM_GUIDE, PRESET_GROUPS, PRESETS, default_models, display_name, load_models, model_choices,
                                new_id, normalize_online, public, save_models)

log = get_logger("ui")


class ModelsMixin:
    # 由 Bridge 提供：self._app、self._kw、self._call、self._emit、self._indexer、self._reload_app、self._restart_indexer

    # ---------- 读取 ----------
    def _models(self) -> dict:
        """当前的模型配置；还没有 models.json 时按当前生效的配置生成一份（不写盘）。"""
        mc = load_models(self._app.home)
        if mc is not None:
            return mc
        mc = default_models()
        cfg = self._app.cfg
        ref = cfg.tasks["knowledge_qa"].default_model
        if ref.startswith("local/"):
            mc["local"]["chat"] = ref.split("/", 1)[1]
            mc["local"]["context_window"] = cfg.chat_model(ref).context_window
        if cfg.embedding.ref.startswith("local/"):
            mc["embedding"] = {"model": cfg.embedding.ref.split("/", 1)[1], "dim": cfg.embedding.dim}
        pc = cfg.providers.get("local")
        if pc and pc.options.get("base_url"):
            mc["local"]["base_url"] = pc.options["base_url"]
        return mc

    def _save_models(self, mc: dict, reload: bool = True) -> None:
        save_models(self._app.home, mc)
        if reload:
            self._reload_app()

    def models(self) -> dict:
        mc = self._models()
        local = {"ok": False, "models": [], "message": ""}
        try:
            local["models"] = self._call(lambda: self._app.gateway.provider("local").list_models())
            local["ok"] = True
        except (LkosError, AttributeError) as e:
            local["message"] = str(e)
        presets = [{**p, "group": p.get("group", "vendor")} for p in PRESETS]
        return {"config": public(mc), "presets": presets, "groups": PRESET_GROUPS, "guide": CUSTOM_GUIDE, "local": local,
                "choices": self.model_choices(), "embedding_in_use": self._app.cfg.embedding.ref,
                "embedding_dim": self._app.cfg.embedding.dim}

    def model_choices(self) -> list[dict]:
        return model_choices(load_models(self._app.home), self._app.cfg)

    def model_name(self, ref: str | None) -> str:
        return display_name(ref, load_models(self._app.home))

    # ---------- 在线模型 ----------
    def _merge_entry(self, entry: dict, mc: dict) -> dict:
        """界面传来的配置：没填密钥时沿用已保存的。"""
        entry = dict(entry or {})
        old = next((m for m in mc["online"] if m["id"] == entry.get("id")), None)
        if old and not entry.get("api_key"):
            entry["api_key"] = old["api_key"]
        return normalize_online(entry)

    def save_online(self, entry: dict) -> dict:
        mc = self._models()
        if not (entry or {}).get("id"):
            entry = {**(entry or {}), "id": new_id(entry.get("name") or entry.get("model") or "model",
                                                   {m["id"] for m in mc["online"]})}
        m = self._merge_entry(entry, mc)
        for key, label in (("base_url", "API 地址"), ("model", "模型名"), ("api_key", "API 密钥")):
            if not m[key]:
                return {"error": f"请填写{label}"}
        mc["online"] = [x for x in mc["online"] if x["id"] != m["id"]] + [m]
        mc["online"].sort(key=lambda x: x["added_at"])
        self._save_models(mc)
        log.info("已保存在线模型：%s（%s，%s）", m["name"], m["model"], m["base_url"])
        return {"ok": True, "id": m["id"], **self.models()}

    def delete_online(self, model_id: str) -> dict:
        mc = self._models()
        mc["online"] = [x for x in mc["online"] if x["id"] != model_id]
        (mc.get("consent") or {}).pop(model_id, None)
        self._save_models(mc)
        log.info("已删除在线模型：%s", model_id)
        return {"ok": True, **self.models()}

    def _provider_for(self, entry: dict):
        from lkos.gateway.online import AnthropicProvider, OpenAICompatProvider

        m = self._merge_entry(entry, self._models())
        cls = AnthropicProvider if m["format"] == "anthropic" else OpenAICompatProvider
        return cls(f"test-{m['id']}", base_url=m["base_url"], api_key=m["api_key"], timeout_s=30), m

    def test_online(self, entry: dict) -> dict:
        """用一句很短的话测试连接（会产生极少量费用）。"""
        try:
            prov, m = self._provider_for(entry)
            if not m["model"]:
                return {"ok": False, "message": "请先填写模型名"}
            r = prov.check_model(m["model"])
        except LkosError as e:
            return {"ok": False, "message": str(e)}
        log.info("测试在线模型 %s：%s", m["model"], r.message)
        return {"ok": r.ok, "message": r.message}

    def list_online_models(self, entry: dict) -> dict:
        try:
            prov, _ = self._provider_for(entry)
            return {"models": prov.list_models()}
        except ModelUnavailableError as e:
            return {"error": str(e)}

    def consent_online(self, model_id: str) -> dict:
        """第一次用某个在线模型前，用户确认过“资料片段会发送到云端”。"""
        mc = self._models()
        mc.setdefault("consent", {})[model_id] = True
        self._save_models(mc, reload=False)
        return {"ok": True}

    def set_offline(self, on: bool) -> dict:
        mc = self._models()
        mc["offline_mode"] = bool(on)
        self._save_models(mc)
        log.info("离线模式已%s", "开启：不能使用在线模型" if on else "关闭")
        return self.models()

    # ---------- 本机模型 ----------
    def set_local(self, chat: str | None = None, context_window: int | None = None,
                  base_url: str | None = None) -> dict:
        mc = self._models()
        if chat:
            mc["local"]["chat"] = str(chat).strip()
        if context_window:
            mc["local"]["context_window"] = max(2048, int(context_window))
        if base_url:
            mc["local"]["base_url"] = str(base_url).strip().rstrip("/")
        self._save_models(mc)
        log.info("本机模型：%s，上下文 %s", mc["local"]["chat"], mc["local"]["context_window"])
        return self.models()

    # ---------- 向量模型 ----------
    def probe_embedding(self, model: str) -> dict:
        """试算一次向量，得到维度；返回当前的文件夹列表供用户勾选要重建的。"""
        model = str(model or "").strip()
        if not model:
            return {"error": "请选择向量模型"}
        try:
            vec = self._call(lambda: self._app.gateway.provider("local").embed(model, ["测试 test"]))[0]
        except (LkosError, AttributeError, IndexError) as e:
            return {"error": f"无法用 {model} 计算向量：{e}"}
        dim = len(vec)
        if dim % 8:
            return {"error": f"{model} 的向量维度是 {dim}，不是 8 的倍数，暂不支持"}
        sources = [{"id": s["id"], "path": s["path"], "files": s["files"]} for s in self._call(self._app.repo.list_sources)]
        return {"model": model, "dim": dim, "sources": sources, "current": self._app.cfg.embedding.ref}

    def apply_embedding(self, model: str, dim: int, source_ids: list[int], when: str = "idle") -> dict:
        """换向量模型：清空旧索引（问答记录保留），只保留勾选的文件夹，按 when（now / idle）重新索引。"""
        mc = self._models()
        ref = f"local/{model}"
        if ref == self._app.cfg.embedding.ref and int(dim) == self._app.cfg.embedding.dim:
            return {"error": "和当前的向量模型一样，不需要更换"}
        keep = [int(x) for x in source_ids or []]
        paths = [s["path"] for s in self._call(self._app.repo.list_sources) if s["id"] in keep]
        self._indexer.shutdown()
        try:
            self._call(lambda: self._app.repo.reset_embeddings(ref, int(dim), keep))
        except ValueError as e:
            return {"error": str(e)}
        mc["embedding"] = {"model": model, "dim": int(dim)}
        save_models(self._app.home, mc)
        self._reload_app()
        self._restart_indexer()
        for p in paths:
            if Path(p).is_dir():
                self._indexer.enqueue(p, background=(when != "now"))
        log.info("已更换向量模型为 %s（%d 维），重新索引 %d 个文件夹（%s）", model, int(dim), len(paths),
                 "现在开始" if when == "now" else "闲时自动")
        self._emit({"type": "library_changed"})
        return {"ok": True, "folders": len(paths), **self.models()}
