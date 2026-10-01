/*
 * frame — 백플레인 프레이밍 · CRC (코어 스펙 §3)
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * **CRC 파라미터와 폭 규칙은 슬레이브의 bp_crc.c 와 한 글자도 달라선 안 된다.**
 * 어긋나면 두 노드가 서로의 프레임을 영원히 폐기하는데, 증상은 "가끔 통신이 안 된다"로
 * 나타나 추적이 극히 어렵다 (§3.7). frame_selftest() 가 §3.7 테스트 벡터로 확인한다.
 */

#ifndef EDGELIB_FRAME_H
#define EDGELIB_FRAME_H

#include <stddef.h>
#include <stdint.h>

#define BP_SOF              0xA5u
#define BP_ADDR_MASK        0x7Fu
#define BP_ADDR_BROADCAST   0x7Fu
#define BP_ADDR_UNASSIGNED  0x00u
#define BP_HDR_SHORT        3u      /* SOF + [P|ADDR] + LEN */

/* CRC 폭 전환 경계 — 보호 길이 L 기준 (§3.4) */
#define BP_CRC_L_MAX_8      14u
#define BP_CRC_L_MAX_16     4093u

/* PDU 하나의 최대. 짧은 형식은 LEN 이 한 바이트다 */
#define BP_PDU_MAX          255u
#define BP_FRAME_MAX        (BP_HDR_SHORT + BP_PDU_MAX + 4u)

uint8_t  bp_crc8  (const uint8_t *d, size_t n);
uint16_t bp_crc16 (const uint8_t *d, size_t n);   /* init 0xFFFF — XMODEM 이 아니다 */
uint32_t bp_crc32c(const uint8_t *d, size_t n);

uint8_t  bp_parity8  (uint8_t v);
unsigned bp_crc_width(size_t prot_len);

/**
 * PDU 를 프레임으로 감싼다.
 *
 * 보호 구간은 `[P|ADDR]` 부터 PDU 끝까지다 — **SOF 도 CRC 자신도 빠진다** (§3.1).
 *
 * @return 프레임 전체 길이. 인자가 틀리면 0
 */
size_t bp_frame_build(uint8_t addr, const uint8_t *pdu, size_t pdu_len,
                      uint8_t *out, size_t out_cap);

#define BP_PARSE_NEED_MORE  (-1)    /* 아직 덜 왔다 — 더 받아서 다시 부른다 */
#define BP_PARSE_BAD        (-2)    /* 깨졌다 */

/**
 * 버퍼 앞에서 프레임 하나를 해석한다.
 *
 * @param addr_out  ADDR
 * @param pdu_out   PDU 가 시작하는 자리 (buf 안을 가리킨다)
 * @param pdu_len   PDU 길이
 * @return 소비한 바이트 수, 또는 BP_PARSE_*
 */
int bp_frame_parse(const uint8_t *buf, size_t n, uint8_t *addr_out,
                   const uint8_t **pdu_out, size_t *pdu_len);

/** 코어 §3.7 테스트 벡터. 0 이면 통과 — 틀리면 슬레이브와 절대 통신되지 않는다. */
int bp_frame_selftest(void);

#endif /* EDGELIB_FRAME_H */
