"""界面“模型”页的桥接方法（混入 Bridge）：在线模型增删改测、本机模型、向量模型更换、离线模式、编辑配置文件。"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from lkos.errors import LkosError, ModelUnavailableError
from lkos.logs import get_logger
from lkos.models_config import (ADVANCED_KEYS, CUSTOM_GUIDE, PRESET_GROUPS, PRESETS, MODELS_VERSION, ModelsFileError,
                                default_models, display_name, load_models, load_models_checked, model_choices,
                                models_path, new_id, normalize_online, parse_models, public, save_models)

log = get_logger("ui")


class ModelsMixin:
    # 由 Bridge 提供：self._app、self._kw、self._call、self._emit、self._indexer、self._reload_app、self._restart_indexer

    # ---------- 读取 ----------
    def _models(self) -> dict:
        """当前的模型配置；还没有 models.json 时按当前生效的配置生成一份（不写盘）。
        文件有错时抛 LkosError：界面上的保存都会被拒绝，免得用默认值覆盖掉原文件（连同密钥）。"""
        mc, err = load_models_checked(self._app.home)
        if err:
            raise LkosError(f"模型配置文件有错误，请先在“编辑配置文件”里修正：{err}")
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
        _, file_error = load_models_checked(self._app.home)
        try:
            mc = self._models()
        except LkosError:
            mc = default_models()             # 只用于显示；保存会被拒绝
        local = {"ok": False, "models": [], "message": ""}
        try:
            local["models"] = self._call(lambda: self._app.gateway.provider("local").list_models())
            local["ok"] = True
        except (LkosError, AttributeError) as e:
            local["message"] = str(e)
        presets = [{**p, "group": p.get("group", "vendor")} for p in PRESETS]
        return {"config": public(mc), "presets": presets, "groups": PRESET_GROUPS, "guide": CUSTOM_GUIDE, "local": local,
                "choices": self.model_choices(), "embedding_in_use": self._app.cfg.embedding.ref,
                "embedding_dim": self._app.cfg.embedding.dim, "file_error": file_error,
                "file_path": str(models_path(self._app.home))}

    def model_choices(self) -> list[dict]:
        return model_choices(load_models(self._app.home), self._app.cfg)

    def model_name(self, ref: str | None) -> str:
        return display_name(ref, load_models(self._app.home))

    # ---------- 在线模型 ----------
    def _merge_entry(self, entry: dict, mc: dict) -> dict:
        """界面传来的配置：没填密钥时沿用已保存的；界面上没有的高级参数和其他字段从已保存的配置里保留。"""
        entry = dict(entry or {})
        old = next((m for m in mc["online"] if m["id"] == entry.get("id")), None)
        if old:
            entry = {**{k: v for k, v in old.items() if k not in entry}, **entry}
            if not entry.get("api_key"):
                entry["api_key"] = old["api_key"]
        return normalize_online(entry)

    def save_online(self, entry: dict) -> dict:
        try:
            mc = self._models()
        except LkosError as e:
            return {"error": str(e)}
        if not (entry or {}).get("id"):
            entry = {**(entry or {}), "id": new_id(entry.get("name") or entry.get("model") or "model",
                                                   {m["id"] for m in mc["online"]})}
        try:
            m = self._merge_entry(entry, mc)
        except (TypeError, ValueError) as e:
            return {"error": str(e)}
        for key, label in (("base_url", "API 地址"), ("model", "模型名"), ("api_key", "API 密钥")):
            if not m[key]:
                return {"error": f"请填写{label}"}
        mc["online"] = [x for x in mc["online"] if x["id"] != m["id"]] + [m]
        mc["online"].sort(key=lambda x: x["added_at"])
        self._save_models(mc)
        log.info("已保存在线模型：%s（%s，%s）", m["name"], m["model"], m["base_url"])
        return {"ok": True, "id": m["id"], **self.models()}

    def delete_online(self, model_id: str) -> dict:
        try:
            mc = self._models()
        except LkosError as e:
            return {"error": str(e)}
        mc["online"] = [x for x in mc["online"] if x["id"] != model_id]
        (mc.get("consent") or {}).pop(model_id, None)
        self._save_models(mc)
        log.info("已删除在线模型：%s", model_id)
        return {"ok": True, **self.models()}

    def _provider_for(self, entry: dict):
        """测试连接、获取模型列表：和网关用同一套参数建 provider（含高级参数）。"""
        from lkos.gateway.online import online_provider

        try:
            mc = self._models()
        except LkosError:
            mc = default_models()
        try:
            m = self._merge_entry(entry, mc)
        except (TypeError, ValueError) as e:
            raise LkosError(str(e)) from e
        opts = {**{k: m[k] for k in ADVANCED_KEYS if k in m}, "base_url": m["base_url"], "api_key": m["api_key"],
                "timeout_s": 30}
        return online_provider(f"test-{m['id']}", m["format"], opts), m

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
        except (ModelUnavailableError, LkosError) as e:
            return {"error": str(e)}

    def consent_online(self, model_id: str) -> dict:
        """第一次用某个在线模型前，用户确认过“资料片段会发送到云端”。"""
        try:
            mc = self._models()
        except LkosError as e:
            return {"error": str(e)}
        mc.setdefault("consent", {})[model_id] = True
        self._save_models(mc, reload=False)
        return {"ok": True}

    def set_offline(self, on: bool) -> dict:
        try:
            mc = self._models()
        except LkosError as e:
            return {"error": str(e)}
        mc["offline_mode"] = bool(on)
        self._save_models(mc)
        log.info("离线模式已%s", "开启：不能使用在线模型" if on else "关闭")
        return self.models()

    # ---------- 本机模型 ----------
    def set_local(self, chat: str | None = None, context_window: int | None = None,
                  base_url: str | None = None) -> dict:
        try:
            mc = self._models()
        except LkosError as e:
            return {"error": str(e)}
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
        try:
            mc = self._models()
        except LkosError as e:
            return {"error": str(e)}
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

    # ---------- 编辑配置文件（JSON / YAML） ----------
    def models_file(self, fmt: str = "json") -> dict:
        """配置文件全文，给界面里的编辑器。文件有错时原样返回内容和错误（让用户就地修改）。
        fmt：json / yaml（YAML 只是换个写法显示和编辑，保存时仍写成 models.json）。"""
        p = models_path(self._app.home)
        mc, err = load_models_checked(self._app.home)
        if err:
            try:
                text = p.read_text("utf-8")
            except OSError:
                text = ""
            return {"text": text, "format": "json", "error": err, "path": str(p), "help": ADVANCED_KEYS}
        if mc is None:
            mc = self._models()
        data = {"version": MODELS_VERSION, **mc}
        if fmt == "yaml":
            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        else:
            text = json.dumps(data, ensure_ascii=False, indent=2)
        return {"text": text, "format": fmt, "error": None, "path": str(p), "help": ADVANCED_KEYS}

    def check_models_text(self, text: str, fmt: str = "json") -> dict:
        """只检查不保存：返回错误说明（带行号）或在线模型数量。"""
        try:
            mc = _parse_text(text, fmt)
        except ModelsFileError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "online": len(mc["online"]), "names": [m["name"] for m in mc["online"]]}

    def save_models_text(self, text: str, fmt: str = "json") -> dict:
        """检查通过才保存（写成 models.json），保存后马上生效。"""
        try:
            mc = _parse_text(text, fmt)
        except ModelsFileError as e:
            return {"error": str(e)}
        save_models(self._app.home, mc)
        self._reload_app()
        log.info("已保存模型配置文件（%s 格式编辑）：%d 个在线模型", fmt.upper(), len(mc["online"]))
        return {"ok": True, **self.models()}

    def reload_models_file(self) -> dict:
        """在别的编辑器里改完 models.json 后：重新读取并生效（文件有错时不生效，返回错误）。"""
        _, err = load_models_checked(self._app.home)
        if err:
            return {"error": err, **self.models()}
        self._reload_app()
        log.info("已重新读取模型配置文件")
        return {"ok": True, **self.models()}

    def open_models_file(self) -> dict:
        """用系统的文本编辑器打开 models.json（没有文件时先按当前配置生成一份）。"""
        import subprocess
        import sys

        p = models_path(self._app.home)
        if not p.exists():
            save_models(self._app.home, self._models())
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-t", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except OSError as e:
            return {"error": f"无法打开：{e}"}
        return {"ok": True, "path": str(p)}


def _parse_text(text: str, fmt: str) -> dict:
    """编辑器里的文字 → 规范化的配置；格式或内容有错时抛 ModelsFileError（写明行号 / 哪一项）。"""
    text = text or ""
    if fmt == "yaml":
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            where = f"第 {mark.line + 1} 行第 {mark.column + 1} 列" if mark else ""
            raise ModelsFileError(f"YAML 格式有错{where}：{getattr(e, 'problem', None) or e}") from e
    else:
        try:
            data = json.loads(text)
        except ValueError as e:
            raise ModelsFileError(f"JSON 格式有错（第 {getattr(e, 'lineno', '?')} 行第 {getattr(e, 'colno', '?')} 列）："
                                  f"{getattr(e, 'msg', e)}") from e
    return parse_models(data)
