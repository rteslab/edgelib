# IODD 보관 폴더

IO-Link 디바이스의 **IODD**(XML 또는 zip)를 여기 둡니다. 커미셔닝 툴의 `Browse...` 가
이 폴더에서 열리고, VendorID/DeviceID 가 맞으면 자동으로 물립니다.

**IODD 가 있어야 프로세스 데이터가 값이 됩니다.** 어느 비트가 무엇이고 원시값에 무엇을
곱해야 물리량이 되는지는 디바이스마다 다르고, 그 정보는 IODD 에만 있습니다. 없으면
바이트까지만 보입니다.

## 함께 오는 것

시험에 쓴 SMC 디바이스 둘의 IODD 가 들어 있습니다.

| 파일 | 디바이스 |
|---|---|
| `SMC-PF3A808H-L2x-xxx-*-IODD1.1.xml` | PF3A808H 유량계 |
| `SMC-AMS-ITV-*-IODD1.1.xml` | AMS-ITV 전공 레귤레이터 |

## 다른 디바이스를 쓰려면

제조사가 배포하는 IODD 를 받아 이 폴더에 넣으면 됩니다. 제조사 홈페이지나
[IODDfinder](https://ioddfinder.io-link.com) 에서 받습니다. zip 그대로 두어도 됩니다.

IODD 는 각 제조사의 배포 조건을 따릅니다.
