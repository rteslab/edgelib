"""산출물이 어디로 가는지 **한 곳에서 정한다**.

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.

배포본에는 `examples/` 가 빈 폴더로 함께 온다. 설정과 예제는 **거기로** 가야 한다.

    edgelib/
    ├── EdgeConfig/      ← 툴
    ├── install.sh
    └── examples/        ← 여기

부르는 쪽에서 `Path.cwd() / "examples"` 로 잡으면 **어디서 띄웠느냐**에 따라 자리가
달라진다. `edgeconfig` 는 어디서든 부를 수 있는 명령이라, 홈에서 띄우면 홈에 새
폴더가 생기고 배포본의 `examples/` 는 영영 비어 있게 된다.

그래서 위로 거슬러 올라가며 배포본을 찾는다 — `examples/` 와 `EdgeConfig/` 가 나란히
있는 자리가 그것이다. 못 찾으면 (개발 중이거나 다른 자리에서 띄웠거나) 작업 폴더
아래로 물러선다.
"""

import os
from pathlib import Path

#: 이 이름의 환경 변수가 있으면 무조건 그 자리를 쓴다
ENV_EXAMPLES = "EDGECONFIG_EXAMPLES"

#: 배포본이라고 판단하는 근거 — 이 둘이 나란히 있으면 그 자리다
_MARKERS = ("examples", "EdgeConfig")


def _dist_root(start: Path) -> Path | None:
    """`start` 에서 위로 올라가며 배포본 뿌리를 찾는다."""
    for d in (start, *start.parents):
        if all((d / m).is_dir() for m in _MARKERS):
            return d
    return None


def examples_root(create: bool = True) -> Path:
    """설정과 예제가 쌓이는 자리. 없으면 만든다.

    만들 수 없으면 (읽기 전용이거나 권한이 없으면) 작업 폴더로 물러선다 —
    아무것도 못 내는 것보다 낫고, 어디에 냈는지는 화면에 그대로 보여 준다.
    """
    env = os.environ.get(ENV_EXAMPLES)
    if env:
        root = Path(env).expanduser()
    else:
        here = Path.cwd()
        dist = _dist_root(here)
        root = (dist / "examples") if dist else (here / "examples")

    if not create:
        return root
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path.cwd()
    return root
