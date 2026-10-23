"""Start the local API and Streamlit UI with one command."""
import argparse
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser(description="启动城市初见")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=8501)
    parser.add_argument("--check", action="store_true", help="只检查依赖，不启动服务")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    venv_python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        return subprocess.call([str(venv_python), str(Path(__file__)), *sys.argv[1:]], cwd=root)
    packages = ["fastapi", "streamlit", "langgraph", "httpx", "pydantic", "pydantic_settings", "sqlalchemy", "uvicorn"]
    missing = [name for name in packages if importlib.util.find_spec(name) is None]
    if missing:
        print("尚缺少依赖：" + ", ".join(missing))
        print(f'请先运行："{sys.executable}" -m pip install -e ".[dev]"')
        return 1
    if args.check:
        print("依赖检查通过。")
        return 0
    if not all(1 <= port <= 65535 for port in [args.api_port, args.ui_port]) or args.api_port == args.ui_port:
        parser.error("端口必须在1到65535之间，且API与页面端口不同")
    for port in [args.api_port, args.ui_port]:
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", port))
        except OSError:
            print(f"端口 {port} 已被占用，请使用 --api-port 或 --ui-port 指定其他端口。")
            return 1
    environment = {**os.environ, "API_BASE_URL": f"http://127.0.0.1:{args.api_port}"}
    children = []
    try:
        backend = subprocess.Popen([sys.executable, "-m", "uvicorn", "urban_explorer.api:app", "--host", "127.0.0.1", "--port", str(args.api_port), "--no-access-log"], cwd=root, env=environment)
        children.append(backend)
        ready = False
        for _ in range(100):
            if backend.poll() is not None:
                print("后端启动失败，请查看上面的错误。")
                return 1
            try:
                with urlopen(environment["API_BASE_URL"] + "/health", timeout=1) as response:
                    ready = response.status == 200
                if ready:
                    break
            except (URLError, OSError):
                time.sleep(0.2)
        if not ready:
            print("后端未能及时启动。")
            return 1
        frontend = subprocess.Popen([sys.executable, "-m", "streamlit", "run", "app.py", "--server.address=127.0.0.1", f"--server.port={args.ui_port}", f"--server.headless={'false' if args.open_browser else 'true'}", "--browser.gatherUsageStats=false"], cwd=root, env=environment)
        children.append(frontend)
        print(f"\n城市初见：http://127.0.0.1:{args.ui_port}")
        print(f"接口文档：{environment['API_BASE_URL']}/docs")
        print("按 Ctrl+C 停止本次启动的两个服务。\n", flush=True)
        while frontend.poll() is None and backend.poll() is None:
            time.sleep(0.5)
        return frontend.returncode or backend.returncode or 0
    except KeyboardInterrupt:
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
