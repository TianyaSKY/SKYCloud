import os
import subprocess
import sys
import time

from dotenv import load_dotenv

load_dotenv()


def start_services():
    base_dir = os.path.dirname(os.path.abspath(__file__))

    env = os.environ.copy()
    env["PYTHONPATH"] = base_dir + os.pathsep + env.get("PYTHONPATH", "")

    worker_path = os.path.join(base_dir, "tasks.py")
    print(f"--- 正在启动 Worker: {worker_path} ---")
    worker_proc = subprocess.Popen([sys.executable, worker_path], env=env)

    app_path = os.path.join(base_dir, "app.py")
    print(f"--- 正在启动 Flask App: {app_path} ---")
    app_proc = subprocess.Popen([sys.executable, app_path], env=env)

    try:
        # 持续监控进程
        while True:
            if worker_proc.poll() is not None:
                print("Worker 进程已退出，正在尝试重启...")
                worker_proc = subprocess.Popen([sys.executable, worker_path])

            if app_proc.poll() is not None:
                print("Flask App 进程已退出，正在尝试重启...")
                app_proc = subprocess.Popen([sys.executable, app_path])

            time.sleep(5)
    except KeyboardInterrupt:
        print("正在停止所有服务...")
        worker_proc.terminate()
        app_proc.terminate()


if __name__ == "__main__":
    start_services()
