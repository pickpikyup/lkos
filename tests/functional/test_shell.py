"""交互式应用：按用户的实际操作顺序输入一串命令。"""

from typer.testing import CliRunner

from lkos.cli import app
from lkos.shell import clean_path


def test_clean_path_accepts_dragged_folders(tmp_path):
    d = tmp_path / "My Docs"
    d.mkdir()
    assert clean_path(str(d).replace(" ", "\\ ")) == d
    assert clean_path(f"'{d}' ") == d
    assert clean_path("~/x").name == "x" and "~" not in str(clean_path("~/x"))


def test_shell_session(tmp_path, corpus, monkeypatch):
    from tests.conftest import fake_overrides
    import lkos.cli as cli_mod
    from lkos.app import App
    from lkos.gateway.fake import FakeProvider
    from tests.conftest import TEST_DIM

    fake = FakeProvider("test", dim=TEST_DIM)
    monkeypatch.setattr(cli_mod, "open_app", lambda: App.open(home=tmp_path / "h", overrides=fake_overrides(),
                                                              providers={"test": fake}))
    opened = []
    monkeypatch.setattr("lkos.shell.open_file", lambda p: opened.append(p))
    steps = [
        "项目二期的预算上限是多少？",       # 还没有索引
        f"/add {str(corpus).replace(' ', chr(92) + ' ')}",
        "项目二期的预算上限是多少？",
        "/open 1", "/open 9", "/ctx",
        "/s retention",
        "/folders", "/update", "/status",
        "/nope", "/remove 1", "y", "/folders",
        "/quit",
    ]
    res = CliRunner().invoke(app, [], input="\n".join(steps) + "\n")
    out = res.output
    assert res.exit_code == 0, out
    assert "还没有添加文件夹" in out and "索引还是空的" in out
    assert "找到 4 个可索引的文件" in out and "新索引 4" in out
    assert "根据资料" in out and "出处" in out and "[1]" in out
    assert len(opened) == 1 and "请输入 1–" in out
    assert "security-policy.md" in out
    assert "4 个文件" in out and "新索引 0" in out and "就绪" in out
    assert "没有这个命令" in out and "已移除，删除了 4 个文件的索引" in out


def test_old_index_is_backed_up_and_folders_kept(tmp_path, corpus, monkeypatch):
    import sqlite3

    from lkos.cli import app as cli_app
    home = tmp_path / "h"
    home.mkdir()
    old = sqlite3.connect(home / "lkos.db")
    old.executescript("CREATE TABLE index_meta(key TEXT PRIMARY KEY, value TEXT);"
                      "INSERT INTO index_meta VALUES ('schema_version', '1');"
                      "CREATE TABLE sources(id INTEGER PRIMARY KEY, path TEXT);")
    old.execute("INSERT INTO sources(path) VALUES (?)", (str(corpus),))
    old.commit()
    old.close()
    from tests.functional.test_cli import write_config

    write_config(home)
    monkeypatch.setenv("LKOS_HOME", str(home))
    res = CliRunner().invoke(cli_app, ["source", "list"])
    assert res.exit_code == 0, res.output
    assert "旧索引已备份为" in res.output and "lkos.db.old-" in res.output
    assert list(home.glob("lkos.db.old-*"))
    res = CliRunner().invoke(cli_app, ["update"])
    assert "新索引 4" in res.output
