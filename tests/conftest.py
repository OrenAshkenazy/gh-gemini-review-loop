import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "gh-review-loop" / "skills" / "gh-review-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))
