"""测试夹具：起一个**隔离**的服务实例。

之前所有测试都直接打 127.0.0.1:7688 上那个跑着的实例——也就是用户的真实库，
跑一次测试就会往用户的 vault 里写一堆文件。这个夹具保证：

- 临时 home（配置 + 数据 + vault 全在临时目录）
- 随机端口起一个自己的 uvicorn
- 用完即毁，绝不碰用户的库
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class IsolatedServer:
    """临时 vault + 临时数据目录 + 独立端口的服务实例。"""

    def __init__(self, with_vault_seed: bool = True) -> None:
        self.with_vault_seed = with_vault_seed
        self.home: Path | None = None
        self.port = 0
        self.proc: subprocess.Popen | None = None

    # -- 生命周期 ---------------------------------------------------------
    def _render_config(self, vault: Path) -> str:
        """以真实 config.toml 为底，只改 vault / 存储路径。"""
        real_cfg = BACKEND_DIR.parent / "config" / "config.toml"
        base_text = ""
        if real_cfg.exists():
            base_text = real_cfg.read_text(encoding="utf-8")
        if not base_text.strip():
            # v34：workbuddy 链路已删，隔离实例的兜底主链用 openai_compat
            base_text = '[ai]\nprovider = "openai_compat"\n'

        data_dir = (self.home / "data").as_posix()
        storage = {
            "data_dir": data_dir,
            "db_path": f"{data_dir}/secondbrain.db",
            "bin_dir": "",
            "prompts_dir": "",
        }
        out_lines = []
        for line in base_text.splitlines():
            stripped = line.strip()
            key = stripped.split("=")[0].strip()
            if key in storage:
                out_lines.append(f'{key} = "{storage[key]}"')
                continue
            out_lines.append(line)
        text = "\n".join(out_lines)

        # vault 路径强制指向临时目录
        import re as _re

        text = _re.sub(
            r'(?m)^path\s*=.*$',
            f'path = "{vault.as_posix()}"',
            text,
            count=1,
        )
        # 确保 vault 段确实写的是临时路径（上面的替换可能改到别的段）
        text = _re.sub(
            r'(?ms)(\[vault\].*?)(?=\n\[)',
            lambda m: _re.sub(
                r'(?m)^path\s*=.*$',
                f'path = "{vault.as_posix()}"',
                m.group(1),
                count=1,
            ),
            text,
        )
        return text + "\n"

    def __enter__(self) -> "IsolatedServer":
        self.home = Path(tempfile.mkdtemp(prefix="sb_test_home_"))
        vault = self.home / "vault"
        for sub in ("01_Inbox", "02_Processed", "03_Notes", "99_Archive", "_reports", "assets"):
            (vault / sub).mkdir(parents=True, exist_ok=True)
        if self.with_vault_seed:
            (vault / "成长树").mkdir(exist_ok=True)
            (vault / "成长树" / "思维逻辑.md").write_text(
                "---\ntags:\n  - 重要检索\n---\n# 思维逻辑\n\n拆解问题、推导因果、抓住核心矛盾。\n",
                encoding="utf-8",
            )

        cfg = self.home / "config.toml"
        cfg.write_text(self._render_config(vault), encoding="utf-8")

        # 继承真实配置里的 provider / 密钥（否则 AI 类用例会误报），
        # 但 vault 与数据一律用临时目录。
        real_secrets = BACKEND_DIR.parent / "config" / "secrets.json"
        if real_secrets.exists():
            shutil.copy2(real_secrets, self.home / "secrets.json")

        self.port = free_port()
        env = os.environ.copy()
        env["SECOND_BRAIN_HOME"] = str(self.home)
        env["SECOND_BRAIN_CONFIG"] = str(cfg)
        env["PYTHONPATH"] = str(BACKEND_DIR)

        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--log-level",
                "warning",
            ],
            cwd=str(BACKEND_DIR),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._wait_ready()
        return self

    def __exit__(self, *exc) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.home is not None:
            shutil.rmtree(self.home, ignore_errors=True)

    def _wait_ready(self, timeout: float = 40.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"{self.base}/api/health", timeout=2
                ) as resp:
                    if resp.status == 200:
                        return
            except Exception:
                time.sleep(0.4)
        raise RuntimeError(f"测试服务未在 {timeout}s 内就绪（端口 {self.port}）")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def vault(self) -> Path:
        assert self.home is not None
        return self.home / "vault"


def run_with_server(main_func, argv_extra: list[str] | None = None) -> int:
    """在隔离实例上跑一个测试脚本的 main()。"""
    with IsolatedServer() as server:
        if argv_extra:
            sys.argv = [sys.argv[0], *argv_extra]
        return main_func(server.base)


if __name__ == "__main__":
    with IsolatedServer() as srv:
        print(f"隔离测试实例: {srv.base}")
        print(f"临时 vault:  {srv.vault}")
        resp = urllib.request.urlopen(f"{srv.base}/api/health", timeout=5)
        print(resp.read().decode())
