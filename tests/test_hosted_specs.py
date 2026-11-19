"""托管后端的静态契约：产物、docker 参数、OneBot11 配置。

这些是「安装能不能成功」的根因所在（端口映射写错、配置字段写错都不会当场报错，
只会在探测阶段超时），所以用测试钉住。
"""

from __future__ import annotations

import json

from livepush.qq.hosted.base import HostPorts, HostedContext, container_name_for
from livepush.qq.hosted.napcat import NapCatBackend, onebot11_config
from livepush.qq.hosted.snowluma import SnowLumaBackend


def make_ctx(tmp_path, uin: int = 12345) -> HostedContext:
    return HostedContext(
        bot_id=7,
        uin=uin,
        workdir=tmp_path / "work",
        token="tok-123",
        ports=HostPorts(ws=30100, http=30101, webui=30102, remote=30103),
        container_name="livepush-napcat-7",
    )


def test_container_name_is_sanitized():
    assert container_name_for("NapCat", 3) == "livepush-napcat-3"
    assert container_name_for("snow luma", 3) == "livepush-snow-luma-3"


def test_backends_expose_artifacts_with_checksums():
    for backend in (NapCatBackend(), SnowLumaBackend()):
        art = backend.artifact()
        assert art.name.endswith(".tar")
        assert art.url.startswith("https://")
        assert len(art.sha256) == 64
        assert art.size > 100 * 1024 * 1024
        assert backend.key and backend.title and backend.notes()


def test_onebot11_config_shape():
    data = onebot11_config("tok", http_port=3000, ws_port=3001)
    ws = data["network"]["websocketServers"][0]
    assert ws["port"] == 3001
    assert ws["token"] == "tok"
    assert ws["enable"] is True
    assert data["network"]["httpServers"][0]["port"] == 3000
    assert data["timeout"]["baseTimeout"] == 10000


def test_napcat_docker_args_map_host_ports_to_container_ports(tmp_path):
    args = NapCatBackend().docker_args(make_ctx(tmp_path))
    joined = " ".join(args)
    assert "127.0.0.1:30100:3001" in joined
    assert "127.0.0.1:30101:3000" in joined
    assert "127.0.0.1:30102:6099" in joined
    assert "ACCOUNT=12345" in joined
    assert f"{tmp_path / 'work' / 'config'}:/app/napcat/config" in joined


def test_napcat_write_config_uses_container_ports(tmp_path):
    ctx = make_ctx(tmp_path)
    files = NapCatBackend().write_config(ctx)
    names = sorted(path.name for path in files)
    assert names == ["onebot11.json", "onebot11_12345.json"]
    data = json.loads((ctx.workdir / "config" / "onebot11_12345.json").read_text(encoding="utf-8"))
    # 容器内监听 3001，宿主机 30100 通过 -p 映射过来
    assert data["network"]["websocketServers"][0]["port"] == 3001
    assert data["network"]["websocketServers"][0]["token"] == "tok-123"


def test_snowluma_docker_args_include_required_caps(tmp_path):
    args = SnowLumaBackend().docker_args(make_ctx(tmp_path))
    joined = " ".join(args)
    assert "--cap-add=SYS_PTRACE" in args
    assert "SNOWLUMA_WEBUI_PORT=5099" in joined
    assert "127.0.0.1:30100:3001" in joined
    assert "127.0.0.1:30103:6081" in joined


def test_snowluma_login_is_internal(tmp_path):
    backend = SnowLumaBackend()
    assert backend.login_kind == "internal"
    # 它自己在容器里登录，我们不写配置
    assert backend.write_config(make_ctx(tmp_path)) == []
