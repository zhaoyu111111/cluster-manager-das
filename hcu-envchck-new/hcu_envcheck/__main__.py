"""模块启动入口；发行包和开发环境使用同一个 CLI。"""
from pathlib import Path
import sys

# 按绝对文件路径启动时固定应用根，避免调用者cwd中的旧同名工程抢先导入。
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hcu_envcheck.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
