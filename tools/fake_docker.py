#!/usr/bin/env python3
"""假的 `docker` 命令，用来在没装 Docker 的机器上试托管流程。

用法（Linux/macOS）：

    export LIVEPUSH_DOCKER_BIN="/path/to/tools/fake_docker.py"
    chmod +x tools/fake_docker.py

它会记住「已载入的镜像」和「已存在的容器」，支持本项目用到的子命令：
version / images / load / run / start / stop / rm / inspect / logs。

看到的所有输出都会写到 stderr，方便你在 Web UI 的「查看日志」里确认调用顺序。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_PATH = Path(os.environ.get("FAKE_DOCKER_STATE") or Path.home() / ".fake-docker.json")


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"images": [], "containers": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def log(msg: str) -> None:
    print(f"[fake-docker] {msg}", file=sys.stderr)


def main(argv: list[str]) -> int:
    state = load_state()
    if not argv:
        print("Usage: fake_docker.py <command> ...", file=sys.stderr)
        return 2

    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        print(state.get("server_version", "24.0.7"))
        return 0
    if cmd == "images":
        if "--format" in rest:
            for image in state["images"]:
                print(image)
            return 0
        print("REPOSITORY\tTAG\tIMAGE ID")
        for i, image in enumerate(state["images"]):
            print(f"{image}\tlatest\tfake{i}")
        return 0
    if cmd == "load":
        if "-i" in rest:
            path = rest[rest.index("-i") + 1]
        else:
            path = rest[0] if rest else ""
        name = Path(path).name
        image = f"livepush/{name.replace('.tar', '')}:loaded"
        state["images"].append(image)
        save_state(state)
        log(f"Loaded image: {image}")
        print(f"Loaded image: {image}")
        return 0
    if cmd == "run":
        joined = " ".join(rest)
        container = ""
        if "--name" in rest:
            container = rest[rest.index("--name") + 1]
        image = next((token for token in reversed(rest) if not token.startswith("-") and "/" in token), "")
        state["containers"][container] = {
            "status": "running",
            "image": image,
            "args": joined,
        }
        save_state(state)
        log(f"run container={container} image={image}")
        print(container)
        return 0
    if cmd in ("start", "stop", "rm"):
        names = [token for token in rest if not token.startswith("-")]
        for name in names:
            if name not in state["containers"]:
                print(f"Error: No such container: {name}", file=sys.stderr)
                return 1
            if cmd == "rm":
                del state["containers"][name]
            else:
                state["containers"][name]["status"] = "running" if cmd == "start" else "exited"
        save_state(state)
        print("\n".join(names))
        return 0
    if cmd == "inspect":
        names = [token for token in rest if not token.startswith("-")]
        name = names[0] if names else ""
        container = state["containers"].get(name)
        if container is None:
            return 1
        print(container["status"])
        return 0
    if cmd == "logs":
        names = [token for token in rest if not token.startswith("-")]
        name = names[0] if names else ""
        container = state["containers"].get(name)
        if container is None:
            print("Error: No such container", file=sys.stderr)
            return 1
        print(f"[fake-docker] {name} 仍在 {container['status']}；这是假日志。")
        print("[fake-docker] 二维码链接（假的）：https://example.invalid/qr")
        return 0
    print(f"fake_docker: 不认识的子命令 {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
