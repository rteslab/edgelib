/*
 * config — 커미셔닝 JSON 파서
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * **라이브러리는 설정을 만들지 않는다.** 무엇이 몇 번 자리에 붙어 있고 PD 가 몇
 * 바이트인지는 사람이 커미셔닝에서 정한 것이고, 여기서는 그것을 읽어 그대로 지킨다.
 * 읽은 것과 버스에서 본 것이 다르면 그것이 곧 사고다 — 라이브러리가 맞춰 주지 않는다.
 *
 * 의존을 늘리지 않으려고 JSON 파서를 안에 둔다. 설정 파일은 한 번, 기동에 한 번만
 * 읽으므로 속도가 아니라 **틀린 파일을 확실히 걸러 내는 것**이 요구사항이다.
 */

#ifndef EDGELIB_CONFIG_H
#define EDGELIB_CONFIG_H

#include <stddef.h>
#include <stdint.h>

#include "../include/edgelib.h"

#define EDGE_MAX_NODES  32

typedef struct {
    /* 링크 */
    char     port[64];
    int      baud;
    char     chip[64];
    int      dir_gpio;

    /* 주기 */
    uint32_t turnaround_us;
    uint32_t cycle_us;
    uint32_t cycle_min_us;

    /* 이미지 */
    uint16_t image_in_bytes;
    uint16_t image_out_bytes;

    int         node_count;
    edge_node_t nodes[EDGE_MAX_NODES];

    /* 주소가 UID 로 고정된 노드인지 — auto 면 0 */
    uint8_t  addr_fixed[EDGE_MAX_NODES];
} edge_config_t;

/** 기본값만 채운다 — 자동 구성으로 열 때 쓴다. */
void edge_config_defaults(edge_config_t *cfg);

/**
 * 파일을 읽어 채운다.
 *
 * @param err   실패 이유가 들어간다 (NULL 가능)
 * @return 0 이면 성공
 */
int edge_config_load(edge_config_t *cfg, const char *path,
                     char *err, size_t err_cap);

#endif /* EDGELIB_CONFIG_H */
