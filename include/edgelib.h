/*
 * edgelib — EdgeX 슬라이스 I/O 백플레인 마스터 라이브러리
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * 응용은 이 헤더 하나만 include 한다. 설계 근거는 docs/edgelib/edgelib_design.md,
 * 함수별 설명은 docs/edgelib/edgelib_API.md 에 있다.
 *
 * **주기 통신은 라이브러리가 스스로 돌린다.** edgelib_open() 이 배경 스레드를 띄우고,
 * RUN 인 동안 그 스레드가 정해진 간격으로 프레임을 낸다. 응용이 늦어도 워치독이 물지
 * 않는 이유가 이것이다 — 그래서 이 API 의 PD 함수는 버스를 타지 않고 스냅샷만 만진다.
 */

#ifndef EDGELIB_H
#define EDGELIB_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ── ResultCode (API 문서 §5.1) ─────────────────────────────────────────── */
#define EDGE_OK                  0
#define EDGE_ERR_BAD_PARAM      -1
#define EDGE_ERR_BAD_STATE      -2   /* 지금 상태에서 안 되는 요청 */
#define EDGE_ERR_BAD_CHANNEL    -3   /* 없는 노드·포트 */
#define EDGE_ERR_BUSY           -4   /* 그 포트에 ISDU 가 진행 중 */
#define EDGE_ERR_UNSUPPORTED    -5
#define EDGE_ERR_TIMEOUT        -6   /* 응답 없음 */
#define EDGE_ERR_DEVICE         -7   /* 디바이스가 거부 — ErrorType 을 함께 준다 */
#define EDGE_ERR_COMMS          -8   /* 통신 오류 */
#define EDGE_ERR_NO_MEM         -9
#define EDGE_ERR_UNDEFINED     -99

/* ── State (§5.2) ───────────────────────────────────────────────────────── */
typedef enum {
    EDGE_STATE_STARTUP  = 0,
    EDGE_STATE_PREOP    = 1,
    EDGE_STATE_RUN      = 2,
    EDGE_STATE_FAILSAFE = 3
} EdgeState_e;

/* ── PortMode (§5.3) ────────────────────────────────────────────────────── */
typedef enum {
    EDGE_PM_DEACTIVATED   = 0,   /* 안 쓰는 포트. 진단을 내지 않는다 */
    EDGE_PM_IOL_MANUAL    = 1,   /* IO-Link. VendorID/DeviceID 대조 */
    EDGE_PM_IOL_AUTOSTART = 2,   /* IO-Link. 대조 없이 붙는 것을 받는다 */
    EDGE_PM_DI_CQ         = 3,   /* C/Q 를 디지털 입력으로 */
    EDGE_PM_DO_CQ         = 4    /* C/Q 를 디지털 출력으로 */
} EdgePortMode_e;

/* ── PortStatusInfo (§5.4) ──────────────────────────────────────────────── */
#define EDGE_PS_NO_DEVICE       0
#define EDGE_PS_DEACTIVATED     1
#define EDGE_PS_PORT_DIAG       2
#define EDGE_PS_OPERATE         4
#define EDGE_PS_DI_CQ           5
#define EDGE_PS_DO_CQ           6
#define EDGE_PS_PORT_POWER_OFF  254
#define EDGE_PS_NOT_AVAILABLE   255

/* ── 이벤트 (§5.5) ──────────────────────────────────────────────────────── */
typedef struct {
    uint8_t  node;         /* 노드 주소. **0 이면 라이브러리 자신의 관찰이다** */
    uint8_t  channel;      /* 0 = 모듈 자신, 1~n = 포트 */
    uint8_t  mode;         /* 1 single shot · 2 해소 · 3 성립 */
    uint8_t  type;         /* 1 notify · 2 warning · 3 error */
    uint16_t code;         /* EventCode — 대역이 출처를 가른다 */
    uint16_t qualifier;    /* IO-Link EventQualifier 원본 */
    uint32_t timestamp_ms; /* 모듈 기동 후 경과. 최초 발생 시각 */
} edge_event_t;

/* 라이브러리가 스스로 세우는 통신 품질 이벤트 (§6.5) — 코어 §8 코드를 그대로 쓴다 */
#define EDGE_EVT_LINK_DOWN   0x0101   /* error   — 최근 100 주기 중 30 % 이상 손실 */
#define EDGE_EVT_CYCLE_MISS  0x0102   /* warning — 5 % 이상 */

/* ── 구성 조회 (§6.4) ───────────────────────────────────────────────────── */
#define EDGE_MAX_PORTS  8

typedef struct {
    uint8_t  port;
    uint8_t  mode;          /* EdgePortMode_e */
    uint8_t  pd_in;         /* 바이트 수 */
    uint8_t  pd_out;
    uint16_t vendor_id;
    uint32_t device_id;
} edge_port_t;

typedef struct {
    uint8_t  address;
    uint8_t  category;      /* UID 앞 4 바이트 — 맞는 모듈인지 보는 근거 */
    uint8_t  model;
    uint8_t  variant;
    uint8_t  hw_rev;
    char     serial[24];
    char     type_name[48];
    uint16_t pd_in;         /* 한정자 1 B 를 포함한다 */
    uint16_t pd_out;
    uint16_t image_in_off;  /* 버스 전체 이미지 안에서 이 노드가 시작하는 자리 */
    uint16_t image_out_off;
    uint8_t  port_count;
    edge_port_t ports[EDGE_MAX_PORTS];
} edge_node_t;

/* ── IO-Link 서비스가 주고받는 것 (§6.7) ────────────────────────────────── */
typedef struct {
    uint16_t vendor_id;
    uint32_t master_id;
    uint8_t  port_count;
    uint8_t  master_type;
    char     product[32];
} edge_master_ident_t;

/* I/Q (M12 핀 2) 의 쓰임 — `edge_port_cfg_t.iq_behavior` (§6.7).
 * 출력 둘은 이 보드에 경로가 없어 설정해도 서비스가 거절됩니다. */
#define EDGE_IQ_NOT_SUPPORTED   0
#define EDGE_IQ_DIGITAL_INPUT   1
#define EDGE_IQ_DIGITAL_OUTPUT  2
#define EDGE_IQ_POWER2          5   /* Port class B */

/* 포트 전원 — `edgelib_iol_port_power()` 의 `mode` (규격 Table E.9).
 * 참·거짓이 아니라 셋이다. 끄고 그대로 두는 것과 잠깐 껐다 켜는 것은 다른 일이다. */
#define EDGE_PWR_ONE_TIME_OFF   0   /* 껐다가 off_ms 뒤에 자동으로 켠다 */
#define EDGE_PWR_OFF            1   /* 끄고 그대로 둔다 */
#define EDGE_PWR_ON             2   /* 켠다 */

/** 규격 E.9 — "Minimum PowerOffTime shall be 500 ms". ONE_TIME_OFF 에서만 쓴다. */
#define EDGE_PWR_OFF_MS_MIN     500

typedef struct {
    uint8_t  mode;          /* EdgePortMode_e */
    uint8_t  validation;    /* 0 없음 · 1 호환 · 2 동일 */
    uint8_t  iq_behavior;   /* EDGE_IQ_* */
    uint8_t  cycletime;     /* Table B.3 부호화 옥텟. 0 이면 디바이스에 맡긴다 */
    uint16_t vendor_id;     /* validation 이 0 이 아닐 때만 본다 */
    uint32_t device_id;
} edge_port_cfg_t;

typedef struct {
    uint8_t  status;        /* EDGE_PS_* */
    uint8_t  quality;       /* bit0 PD invalid · bit1 PDout invalid */
    uint8_t  revision_id;   /* 상위 니블 major, 하위 minor */
    uint8_t  rate;          /* 0 미검출 · 1~3 COM1~COM3 */
    uint8_t  cycletime;     /* Table B.3 부호화 옥텟 */
    uint8_t  pd_in;
    uint8_t  pd_out;
    uint16_t vendor_id;
    uint32_t device_id;
    uint32_t cycletime_us;  /* 위 옥텟을 푼 값 — 편의 */
} edge_port_status_t;

/* ── 불투명 핸들 ────────────────────────────────────────────────────────── */
typedef struct edgelib edgelib_t;

/* ── 버스 (§6.2) ────────────────────────────────────────────────────────── */

/**
 * 설정 파일을 읽고, 버스를 열고, **배경 스레드를 시작한다.**
 *
 * @param config_path EdgeConfig 가 낸 JSON. NULL 이면 자동 구성 — 붙어 있는 것을
 *                    찾아 그대로 쓴다. 브링업에는 이쪽이 편하다.
 * @return 실패하면 NULL
 */
edgelib_t  *edgelib_open(const char *config_path);
int         edgelib_close(edgelib_t *bus);
const char *edgelib_error_msg(int result);

/**
 * 마지막 실패에 대한 한 줄 설명. `edgelib_open()` 이 NULL 을 준 이유도 여기 있다
 * (그때는 `bus` 에 NULL 을 준다).
 */
const char *edgelib_last_error(edgelib_t *bus);

/* ── 상태 (§6.3) ────────────────────────────────────────────────────────── */

/**
 * 상태를 옮긴다. **STARTUP 에서는 스캔이 자동으로 돈다.**
 *
 * RUN 으로 갈 때 설정의 `cycle_us` 를 `SET_CYCLE_TIME` 으로 먼저 통보한다 —
 * 안 하면 슬레이브가 RUN 을 거부한다. FAILSAFE 에서 나가는 길은 STARTUP 하나뿐이라,
 * PREOP 을 지시해도 라이브러리가 STARTUP 을 거쳐 간다.
 */
int edgelib_setmode(edgelib_t *bus, EdgeState_e target);
int edgelib_getmode(edgelib_t *bus, EdgeState_e *state);

/* ── 구성 조회 (§6.4) ───────────────────────────────────────────────────── */
int edgelib_node_count(edgelib_t *bus);
int edgelib_node_info (edgelib_t *bus, uint8_t node, edge_node_t *out);

/* ── 진단 (§6.5) ────────────────────────────────────────────────────────── */

/**
 * 지금 **성립 중인 사실**을 준다. PD 의 `valid` 가 0 일 때 왜 그런지 가르는
 * 유일한 수단이다. 버스를 타지 않는다 — 사본을 복사할 뿐이다.
 */
int edgelib_get_event  (edgelib_t *bus, edge_event_t *buf, int max, int *count);

/** 이미 **해소된 것만** 지운다. `node` 가 0 이면 모든 노드. */
int edgelib_clear_event(edgelib_t *bus, uint8_t node);

/* ── 모듈 파라미터 (§6.6) ───────────────────────────────────────────────── */
/**
 * **모듈 자신의 설정**이다 — 포트에 꽂힌 디바이스의 파라미터(ISDU)가 아니다.
 *
 * `channel` 은 **0 이 모듈 자신**, 1~n 이 채널이다 — 이벤트의 `channel` 과 같은
 * 규약이라, "3번 채널 단선"을 보고 같은 번호로 그 채널의 설정을 읽을 수 있다.
 *
 * 값이 `uint32_t` 인 것은 코어 §6.2 가 **언제나 4 바이트**로 못 박았기 때문이다.
 * 가변 길이면 마스터가 응답 크기를 미리 몰라 비주기 스케줄링이 성립하지 않는다.
 * 작은 값은 상위를 0 으로 채운다.
 */
int edgelib_param_read (edgelib_t *bus, uint8_t node, uint8_t channel,
                        uint16_t index, uint32_t *value);
/** 쓰기는 **PREOP 에서만** 된다. 읽기는 RUN 중에도 된다 (코어 §6.1). */
int edgelib_param_write(edgelib_t *bus, uint8_t node, uint8_t channel,
                        uint16_t index, uint32_t value);

/* ── IO-Link — 규격의 SMI 서비스 그대로 (§6.7) ──────────────────────────── */
int edgelib_iol_master_ident (edgelib_t *bus, uint8_t node,
                              edge_master_ident_t *out);
int edgelib_iol_port_configuration(edgelib_t *bus, uint8_t node, uint8_t port,
                              const edge_port_cfg_t *cfg);
int edgelib_iol_readback_port_configuration(edgelib_t *bus, uint8_t node,
                              uint8_t port, edge_port_cfg_t *out);
int edgelib_iol_port_status  (edgelib_t *bus, uint8_t node, uint8_t port,
                              edge_port_status_t *out);
/**
 * 그 포트의 `L+` 를 끊었다 붙인다 — **디바이스를 다시 세우는 마지막 수단**이다.
 *
 * `mode` 는 `EDGE_PWR_*` 셋 중 하나다. `off_ms` 는 `ONE_TIME_OFF` 에서만 뜻이 있고
 * `EDGE_PWR_OFF_MS_MIN` 보다 짧으면 디바이스가 전원이 내려간 줄 모른다 (규격 E.9).
 */
int edgelib_iol_port_power   (edgelib_t *bus, uint8_t node, uint8_t port,
                              int mode, uint16_t off_ms);

/**
 * ISDU 읽기. **개시와 회수를 라이브러리가 묶는다.** 호출은 블록되지만 주기 통신은
 * 계속 돈다. `timeout_s` 가 0 이면 기본 6 초.
 *
 * 디바이스가 거부하면 EDGE_ERR_DEVICE 를 주고 `buf` 앞 2 바이트에 ErrorType 이 담긴다.
 */
int edgelib_iol_device_read  (edgelib_t *bus, uint8_t node, uint8_t port,
                              uint16_t index, uint8_t subindex,
                              uint8_t *buf, uint16_t *len, double timeout_s);
int edgelib_iol_device_write (edgelib_t *bus, uint8_t node, uint8_t port,
                              uint16_t index, uint8_t subindex,
                              const uint8_t *buf, uint16_t len, double timeout_s);
int edgelib_iol_abort        (edgelib_t *bus, uint8_t node, uint8_t port);

/**
 * 핀 2 (I/Q) 의 지금 입력 상태. `SMI_PDInIQ`.
 *
 * **그 포트의 `iq_behavior` 가 `EDGE_IQ_DIGITAL_INPUT` 이라야 답합니다.** 아니면
 * `EDGE_ERR_UNSUPPORTED` 입니다 — 설정하지 않은 핀을 읽으려 한 것이지 고장이 아닙니다.
 *
 * **주기 데이터가 아닙니다.** 핀 2 는 보조 입력이라 포트마다 쓰이지도 않고 매 사이클
 * 갱신이 필요한 경우가 드물어, 주기 맵에 넣으면 안 쓰는 포트까지 매 사이클 비용을
 * 냅니다 (클래스 문서 §1.4). 부를 때마다 버스를 한 번 탑니다.
 *
 * 핀 2 에 출력은 없습니다. MAX14819 의 I/Q 채널에 드라이버 단이 없어
 * `SMI_PDOutIQ` 는 언제나 거절되고, 그래서 여기에 대응하는 함수를 두지 않습니다.
 *
 * @param iq 0 또는 1
 */
int edgelib_iol_pd_in_iq     (edgelib_t *bus, uint8_t node, uint8_t port,
                              uint8_t *iq);

/* ── 프로세스 데이터 (§6.8) ─────────────────────────────────────────────── */

/**
 * 입력 이미지 **전체**를 복사한다. 버스를 타지 않으므로 즉시 돌아온다.
 *
 * 포트별 조각이 아니라 통째인 이유는, 설정 파일의 `byte` 오프셋이 애초에 버스 전체
 * 이미지 기준이기 때문이다. 조각을 주면 응용이 오프셋을 다시 맞춰야 한다.
 *
 * @param len 들어갈 때 `buf` 의 크기, 나올 때 채운 바이트 수
 */
int edgelib_image_in (edgelib_t *bus, uint8_t *buf, uint16_t *len);

/** 출력 이미지 **전체**를 갈아 끼운다. 실제 전송은 다음 주기다. */
int edgelib_image_out(edgelib_t *bus, const uint8_t *buf, uint16_t len);

/** 이미지 크기 — 자동 구성으로 열었을 때 버퍼를 잡는 데 쓴다. */
int edgelib_image_size(edgelib_t *bus, uint16_t *in_bytes, uint16_t *out_bytes);

#ifdef __cplusplus
}
#endif

#endif /* EDGELIB_H */
