/*
 * frame — 백플레인 프레이밍 · CRC (코어 스펙 §3)
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * 표는 처음 쓸 때 한 번 만든다. 슬레이브(bp_crc.c)는 플래시에 상수로 두지만 여기는
 * 리눅스라 그럴 이유가 없고, 다항식이 코드에 그대로 보이는 편이 대조하기 쉽다.
 */

#include "frame.h"

#include <string.h>

/* CRC-8/AUTOSAR · CRC-16/CCITT(init 0xFFFF) · CRC-32C(반사형) — §3.4 */
#define POLY8    0x2Fu
#define POLY16   0x1021u
#define POLY32R  0x82F63B78u

static uint8_t  t8[256];
static uint16_t t16[256];
static uint32_t t32[256];
static int      tables_ready = 0;

static void tables_init(void)
{
    if (tables_ready) { return; }

    for (unsigned i = 0; i < 256u; i++) {
        uint8_t c = (uint8_t)i;
        for (unsigned b = 0; b < 8u; b++) {
            c = (uint8_t)((c & 0x80u) ? ((unsigned)(c << 1) ^ POLY8) : (unsigned)(c << 1));
        }
        t8[i] = c;
    }
    for (unsigned i = 0; i < 256u; i++) {
        uint16_t c = (uint16_t)(i << 8);
        for (unsigned b = 0; b < 8u; b++) {
            c = (uint16_t)((c & 0x8000u) ? ((unsigned)(c << 1) ^ POLY16)
                              : (unsigned)(c << 1));
        }
        t16[i] = c;
    }
    for (unsigned i = 0; i < 256u; i++) {
        uint32_t c = (uint32_t)i;
        for (unsigned b = 0; b < 8u; b++) {
            c = (c & 1u) ? ((c >> 1) ^ POLY32R) : (c >> 1);
        }
        t32[i] = c;
    }
    tables_ready = 1;
}

uint8_t bp_crc8(const uint8_t *d, size_t n)
{
    tables_init();
    uint8_t c = 0xFFu;
    for (size_t i = 0; i < n; i++) { c = t8[c ^ d[i]]; }
    return (uint8_t)(c ^ 0xFFu);
}

uint16_t bp_crc16(const uint8_t *d, size_t n)
{
    tables_init();
    uint16_t c = 0xFFFFu;
    for (size_t i = 0; i < n; i++) {
        c = (uint16_t)((c << 8) ^ t16[((c >> 8) ^ d[i]) & 0xFFu]);
    }
    return c;
}

uint32_t bp_crc32c(const uint8_t *d, size_t n)
{
    tables_init();
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) {
        c = (c >> 8) ^ t32[(c ^ d[i]) & 0xFFu];
    }
    return c ^ 0xFFFFFFFFu;
}

uint8_t bp_parity8(uint8_t v)
{
    v = (uint8_t)(v ^ (v >> 4));
    v = (uint8_t)(v ^ (v >> 2));
    v = (uint8_t)(v ^ (v >> 1));
    return (uint8_t)(v & 1u);
}

unsigned bp_crc_width(size_t prot_len)
{
    /* 보호 길이가 폭을 정한다 (§3.4). 길이를 협상하지 않는 이유가 이것이다. */
    if (prot_len <= BP_CRC_L_MAX_8)  { return 1u; }
    if (prot_len <= BP_CRC_L_MAX_16) { return 2u; }
    return 4u;
}

size_t bp_frame_build(uint8_t addr, const uint8_t *pdu, size_t pdu_len,
                      uint8_t *out, size_t out_cap)
{
    if (pdu == NULL || out == NULL) { return 0u; }
    if (pdu_len == 0u || pdu_len > BP_PDU_MAX) { return 0u; }

    const size_t prot_len = 2u + pdu_len;          /* [P|ADDR] + LEN + PDU */
    const unsigned w = bp_crc_width(prot_len);
    const size_t total = BP_HDR_SHORT + pdu_len + w;
    if (out_cap < total) { return 0u; }

    out[0] = BP_SOF;
    out[1] = (uint8_t)((bp_parity8((uint8_t)pdu_len) << 7) | (addr & BP_ADDR_MASK));
    out[2] = (uint8_t)pdu_len;
    memcpy(&out[3], pdu, pdu_len);

    const uint8_t *prot = &out[1];
    if (w == 1u) {
        out[BP_HDR_SHORT + pdu_len] = bp_crc8(prot, prot_len);
    } else if (w == 2u) {
        const uint16_t c = bp_crc16(prot, prot_len);
        out[BP_HDR_SHORT + pdu_len + 0u] = (uint8_t)(c & 0xFFu);   /* 전선은 리틀엔디안 */
        out[BP_HDR_SHORT + pdu_len + 1u] = (uint8_t)(c >> 8);
    } else {
        const uint32_t c = bp_crc32c(prot, prot_len);
        for (unsigned i = 0; i < 4u; i++) {
            out[BP_HDR_SHORT + pdu_len + i] = (uint8_t)(c >> (8u * i));
        }
    }
    return total;
}

int bp_frame_parse(const uint8_t *buf, size_t n, uint8_t *addr_out,
                   const uint8_t **pdu_out, size_t *pdu_len)
{
    if (n < 1u) { return BP_PARSE_NEED_MORE; }
    if (buf[0] != BP_SOF) { return BP_PARSE_BAD; }
    if (n < BP_HDR_SHORT) { return BP_PARSE_NEED_MORE; }

    const uint8_t p  = (uint8_t)((buf[1] >> 7) & 1u);
    const uint8_t ln = buf[2];

    /* **LEN 을 쓰기 전에 판정한다** (§3.3). 깨진 길이로 버퍼를 읽으면 그 다음이 없다. */
    if (bp_parity8(ln) != p) { return BP_PARSE_BAD; }
    if (ln == 0u) { return BP_PARSE_BAD; }        /* 확장 프레임 — 여기서는 안 쓴다 */

    const size_t prot_len = 2u + (size_t)ln;
    const unsigned w = bp_crc_width(prot_len);
    const size_t total = BP_HDR_SHORT + (size_t)ln + w;
    if (n < total) { return BP_PARSE_NEED_MORE; }

    const uint8_t *prot = &buf[1];
    const uint8_t *got = &buf[BP_HDR_SHORT + ln];
    if (w == 1u) {
        if (got[0] != bp_crc8(prot, prot_len)) { return BP_PARSE_BAD; }
    } else if (w == 2u) {
        const uint16_t c = bp_crc16(prot, prot_len);
        if (got[0] != (uint8_t)(c & 0xFFu) || got[1] != (uint8_t)(c >> 8)) {
            return BP_PARSE_BAD;
        }
    } else {
        const uint32_t c = bp_crc32c(prot, prot_len);
        for (unsigned i = 0; i < 4u; i++) {
            if (got[i] != (uint8_t)(c >> (8u * i))) { return BP_PARSE_BAD; }
        }
    }

    if (addr_out != NULL) { *addr_out = (uint8_t)(buf[1] & BP_ADDR_MASK); }
    if (pdu_out != NULL)  { *pdu_out = &buf[BP_HDR_SHORT]; }
    if (pdu_len != NULL)  { *pdu_len = ln; }
    return (int)total;
}

int bp_frame_selftest(void)
{
    static const uint8_t chk[9] = { '1','2','3','4','5','6','7','8','9' };

    if (bp_crc8(chk, 9u)   != 0xDFu)       { return -1; }
    if (bp_crc16(chk, 9u)  != 0x29B1u)     { return -2; }
    if (bp_crc32c(chk, 9u) != 0xE3069283u) { return -3; }

    /* 왕복 — 폭이 갈리는 두 지점을 다 밟는다 */
    static const size_t lens[3] = { 1u, 41u, 200u };
    for (unsigned k = 0; k < 3u; k++) {
        uint8_t pdu[200];
        for (size_t i = 0; i < lens[k]; i++) { pdu[i] = (uint8_t)(i * 7u + 3u); }

        uint8_t f[BP_FRAME_MAX];
        const size_t nf = bp_frame_build(3u, pdu, lens[k], f, sizeof f);
        if (nf == 0u) { return -4; }

        uint8_t addr = 0u;
        const uint8_t *out = NULL;
        size_t olen = 0u;
        const int used = bp_frame_parse(f, nf, &addr, &out, &olen);
        if (used != (int)nf || addr != 3u || olen != lens[k]) { return -5; }
        if (memcmp(out, pdu, olen) != 0) { return -6; }

        /* 한 비트를 뒤집으면 반드시 걸려야 한다 */
        f[nf / 2u] ^= 0x01u;
        if (bp_frame_parse(f, nf, &addr, &out, &olen) != BP_PARSE_BAD) { return -7; }
    }
    return 0;
}
