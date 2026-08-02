"""好感度插件测试路径初始化。"""

from pathlib import Path

import sys


MAIBOT_ROOT = Path(__file__).resolve().parents[3]
if str(MAIBOT_ROOT) not in sys.path:
    sys.path.insert(0, str(MAIBOT_ROOT))
