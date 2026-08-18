/*
 * transport — RS-485 반이중 링크 (시리얼 + DIR GPIO)
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * 반이중이라 **송신이 끝난 것을 확인한 뒤에야 DIR 을 내릴 수 있다.** tcdrain() 이
 * 커널 버퍼를 비우지만 그것만으로는 마지막 비트가 선로에 실리기 전일 수 있어,
 * 보레이트에서 계산한 만큼 더 기다린다.
 *
 * 커널 RS485 모드를 반드시 켠다. 꺼져 있으면 tcdrain() 이 마지막 비트까지 기다리지
 * 않아 프레임 꼬리가 잘린다 — 실측에서 64 B 왕복이 켜짐 100/100 대 꺼짐 0/100 이었다.
 */

#ifndef EDGELIB_TRANSPORT_H
#define EDGELIB_TRANSPORT_H

#include <stddef.h>
#include <stdint.h>

#define EDGE_DEFAULT_PORT      "/dev/ttyAMA3"
#define EDGE_DEFAULT_BAUD      3000000
#define EDGE_DEFAULT_CHIP      "/dev/gpiochip0"
#define EDGE_DEFAULT_DIR_GPIO  22

typedef struct {
    int      fd;            /* 시리얼 */
    int      gpio_fd;       /* GPIO 라인 (문자 장치 v2) */
    unsigned dir_line;
    int      baud;
    uint32_t guard_us;      /* 마지막 비트가 선로를 떠날 때까지 */
    uint32_t idle_us;       /* 이만큼 조용하면 프레임이 끝난 것으로 본다 */
    char     port_name[64]; /* 오류 문구에 적어 준다 */
    char     err[160];
} edge_transport_t;

/**
 * 포트를 열고 DIR 선을 잡는다.
 *
 * @param dir_gpio 음수면 GPIO 를 잡지 않는다 — 커널 RS485 가 RTS 로 방향을 돌리는
 *                 보드용. 이 제품 보드는 GPIO22 를 쓴다.
 * @return 0 이면 성공. 실패하면 tr->err 에 이유가 들어간다
 */
int  edge_transport_open(edge_transport_t *tr, const char *port, int baud,
                         const char *chip, int dir_gpio);
void edge_transport_close(edge_transport_t *tr);

/**
 * 요청 하나 → 응답 하나.
 *
 * @return 응답 PDU 길이, 없으면 0, 링크 오류면 음수
 */
int edge_transport_xact(edge_transport_t *tr, uint8_t addr,
                        const uint8_t *pdu, size_t pdu_len,
                        uint8_t *rsp, size_t rsp_cap, uint32_t timeout_us);

/** 응답이 없는 커맨드. 브로드캐스트는 아무도 답하지 않는다. */
int edge_transport_broadcast(edge_transport_t *tr,
                             const uint8_t *pdu, size_t pdu_len);

/* ── 시간 ────────────────────────────────────────────────────────────────── */
uint64_t edge_now_us(void);

/** 200 µs 아래는 바쁘게 기다린다 — nanosleep 의 깨우기 오차가 그보다 크다. */
void edge_sleep_us(uint32_t us);

#endif /* EDGELIB_TRANSPORT_H */
