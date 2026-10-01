/*
 * internal — edgelib 안쪽에서만 보는 것
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * `struct edgelib` 이 여기 있다. 공개 헤더에는 불투명 포인터만 두어, 필드가 바뀌어도
 * 응용을 다시 컴파일하지 않게 한다.
 */

#ifndef EDGELIB_INTERNAL_H
#define EDGELIB_INTERNAL_H

#include <pthread.h>
#include <stdint.h>

#include "../include/edgelib.h"
#include "config.h"
#include "frame.h"
#include "transport.h"

/* ── 백플레인 커맨드 (코어 §6.1) ─────────────────────────────────────────── */
#define CMD_SET_STATE        0x03u
#define CMD_GET_EVENT        0x06u
#define CMD_CLEAR_ERRORS     0x08u
#define CMD_PARAM_READ       0x10u
#define CMD_PARAM_WRITE      0x11u
#define CMD_SET_CYCLE_TIME   0x12u
#define CMD_CYCLIC_EXCHANGE  0x20u
#define CMD_DISCOVER         0x30u
#define CMD_SET_ADDRESS      0x31u
#define CMD_GET_UID          0x32u

/* 클래스 대역 — IO-Link (카테고리 0x40) */
#define CMD_IOL_MASTER_IDENT     0x40u
#define CMD_IOL_PORT_CFG_WRITE   0x41u
#define CMD_IOL_PORT_CFG_READ    0x42u
#define CMD_IOL_PORT_STATUS      0x43u
#define CMD_IOL_PORT_POWER       0x44u
#define CMD_IOL_PD_IN_IQ         0x45u
#define CMD_IOL_ISDU_READ        0x48u
#define CMD_IOL_ISDU_WRITE       0x49u
#define CMD_IOL_ISDU_RESULT      0x4Eu
#define CMD_IOL_ISDU_ABORT       0x4Fu

/* STATUS 옥텟 (코어 §3.6) */
#define ST_RESULT_MASK   0x0Fu
#define ST_STATE_MASK    0x30u
#define ST_STATE_SHIFT   4u
#define ST_ERR_FLAG      0x40u
#define ST_EVT_FLAG      0x80u

/* ResultCode — 슬레이브가 STATUS 하위 니블에 싣는 값 */
#define BP_ST_OK           0u
#define BP_ST_PENDING      1u
#define BP_ST_BAD_PARAM    2u
#define BP_ST_BAD_STATE    3u
#define BP_ST_BAD_CHANNEL  4u
#define BP_ST_BUSY         5u
#define BP_ST_UNSUPPORTED  6u
#define BP_ST_TIMEOUT      8u
#define BP_ST_DEVICE_ERR   9u

#define UID_LEN  12u

/* ── 한계 ────────────────────────────────────────────────────────────────── */
#define EDGE_IMAGE_MAX   1024u
#define EDGE_EVENTS_MAX  64
#define EDGE_JOBQ_MAX    16
#define EDGE_LOSS_WIN    100      /* 손실률을 재는 창 (§6.5) */

/* ── 비주기 작업 ─────────────────────────────────────────────────────────── */
typedef enum {
    JOB_XACT = 0,     /* 요청 하나 → 응답 하나 */
    JOB_ISDU          /* 개시 → MST.RDY 대기 → 회수 */
} job_kind_e;

typedef struct {
    job_kind_e kind;
    uint8_t    node;
    uint8_t    port;

    uint8_t    req[BP_PDU_MAX];
    uint16_t   req_len;

    /* ISDU 진행 */
    int        phase;         /* 0 개시 전 · 1 회수 대기 */
    uint64_t   deadline_us;

    uint8_t    rsp[BP_PDU_MAX];
    uint16_t   rsp_len;
    int        result;        /* EDGE_* */
    int        done;
} edge_job_t;

/* ── 노드 런타임 ─────────────────────────────────────────────────────────── */
typedef struct {
    uint8_t  evt_pending;     /* 마지막 응답의 STATUS.EVT */
    uint8_t  mst;             /* 한정자 바이트 — PQ 하위 니블 · RDY 상위 니블 */
    uint8_t  online;

    /* 최근 EDGE_LOSS_WIN 사이클의 실패 여부. 링으로 굴린다 */
    uint8_t  hist[EDGE_LOSS_WIN];
    int      hist_pos;
    int      hist_fill;
    int      hist_bad;

    int      warn_up;         /* 0x0102 를 이미 세웠나 */
    int      err_up;          /* 0x0101 */
} edge_node_rt_t;

/* ── 버스 ────────────────────────────────────────────────────────────────── */
struct edgelib {
    edge_transport_t tr;
    edge_config_t    cfg;
    int              auto_config;

    pthread_mutex_t  lock;
    pthread_cond_t   cv;          /* 작업 완료 · 상태 변화 */
    pthread_t        thread;
    int              thread_up;
    int              stop;

    EdgeState_e      state;

    uint8_t          image_in[EDGE_IMAGE_MAX];
    uint8_t          image_out[EDGE_IMAGE_MAX];

    edge_node_rt_t   rt[EDGE_MAX_NODES];

    edge_event_t     ev[EDGE_EVENTS_MAX];
    int              ev_n;

    edge_job_t      *q[EDGE_JOBQ_MAX];
    int              q_head;
    int              q_n;

    char             last_err[192];
};

/* ── cycle.c ─────────────────────────────────────────────────────────────── */
void edge_thread_start(edgelib_t *bus);
void edge_thread_stop(edgelib_t *bus);

/** 작업을 큐에 넣고 끝날 때까지 기다린다. 잠금은 안에서 잡는다. */
int  edge_job_run(edgelib_t *bus, edge_job_t *job, double timeout_s);

/** 이벤트 사본에 한 건 반영. 잠금을 이미 쥔 채로 부른다. */
void edge_event_put(edgelib_t *bus, const edge_event_t *e);

/** 라이브러리 자신이 세우는 이벤트 (§6.5). 잠금을 쥔 채로 부른다. */
void edge_event_self(edgelib_t *bus, uint8_t node_addr, uint16_t code,
                     uint8_t type, int appeared, uint16_t qualifier);

/* ── edgelib.c 가 나눠 쓰는 것 ───────────────────────────────────────────── */
int  edge_node_index(const edgelib_t *bus, uint8_t node);
void edge_set_err(edgelib_t *bus, const char *fmt, ...);
int  edge_map_status(uint8_t status);

#endif /* EDGELIB_INTERNAL_H */
