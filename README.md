# edgelib — EdgeX Slice I/O 마스터 라이브러리

EdgeX 슬라이스 I/O 를 CM4 에서 다루는 라이브러리입니다. RS-485 백플레인 위의 모듈을
찾고, 설정하고, 프로세스 데이터를 주기적으로 주고받습니다. C 와 Python 두 벌이고 같은
API 입니다.

## 설치

```sh
sudo sh ./install.sh
```

라이브러리, 파이썬 바인딩, 커미셔닝 툴(`edgeconfig`), 그리고 그 셋이 필요로 하는 패키지를
한 번에 깝니다. 전원 enable(GPIO25)은 `edgelib-gpio25.service` 가 부팅마다 세웁니다.
`config.txt` 에 백플레인 포트(`dtoverlay=uart3`)를 넣으므로 **설치 뒤 한 번 재부팅**해야
합니다.

지우려면 `sudo sh ./uninstall.sh` 입니다.

## 시작하기

```sh
edgeconfig gui
```

전원을 켠 장비 앞에서 무엇을 어떤 순서로 하는지는 **문서 §3.3 「따라 해 보기」** 에
화면 그대로 있습니다.

| | |
|---|---|
| `doc/index.html` | **API 문서.** 브라우저로 여세요 |
| `include/` · `lib/` | C 라이브러리 |
| `python/` | 파이썬 바인딩 |
| `EdgeConfig/` | 커미셔닝 툴 |
| `examples/` | **커미셔닝 툴이 낸 것이 여기 쌓입니다** |

## examples/ 가 비어 있는 것이 정상입니다

예제는 설비마다 다릅니다 — 어느 포트에 무엇이 꽂혔고 그 장치의 데이터가 어떤 모양인지에
따라 코드가 달라지므로, 미리 만들어 둘 수가 없습니다.

커미셔닝 툴의 `Generate example code` 가 **내 설비에 맞는 예제**를 만들어 이 폴더에
넣습니다. `edgeconfig` 를 어느 폴더에서 띄우든 이리로 옵니다.

```
examples/<날짜_시각>/
├── c/        edgex.json · edgex.h · edgex_example.c · Makefile
└── python/   edgex.json · edgex_pd.py · edgex_example.py
```

누를 때마다 새 폴더가 생기므로 앞서 낸 것이 지워지지 않습니다. 다른 자리에 쌓고 싶으면
`EDGECONFIG_EXAMPLES` 로 바꿉니다.

```sh
EDGECONFIG_EXAMPLES=/mnt/usb/edgex  edgeconfig gui
```

자세한 것은 문서 §3.3 의 마지막 걸음에 있습니다.

## 접속

LAN 포트는 고정 IP 로 나옵니다. PC 를 같은 대역(예: `192.168.0.11`)으로 맞추고 붙으세요.

| | |
|---|---|
| IP 주소 | `192.168.0.10` |
| 서브넷 마스크 | `255.255.255.0` |

```sh
ssh admin@192.168.0.10
```

초기 계정은 `admin` / `1234` 입니다. **최초 사용 전에 반드시 바꾸세요.**

---

Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
