/*
 * cycle — 주기 스레드 · 비주기 슬롯 · 스냅샷 · 이벤트 사본
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * 한 사이클은 정확히 이 모양이다 (설계 §4):
 *
 *     사이클 시작
 *       ├─ 노드마다 CMD_CYCLIC_EXCHANGE  하나씩. PD 를 주고받는다
 *       ├─ 비주기 프레임 **최대 하나**   아래 우선순위로 큐에서 하나만
 *       └─ 다음 사이클까지 대기
 *
 * 셋을 지켜야 **최악 사이클이 계산으로 나온다.** 그것이 설비 설계자에게 응답성을
 * 약속할 수 있는 근거이고, EdgeConfig 가 내는 `cycle_min_us` 가 바로 그 계산이다.
 *
 * 우선순위는 이벤트 회수가 먼저다 — **놓치면 되돌릴 수 없는 유일한 정보**이기
 * 때문이다. 나머지는 다음 사이클에 해도 된다.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "internal.h"

#include <stdio.h>
#include <string.h>
#include <time.h>

/* ── 이벤트 사본 ─────────────────────────────────────────────────────────── */
/*
 * 슬레이브는 **미확인분만** 돌려준다. 한 번 읽은 항목은 다음 회수에 안 나오므로,
 * 마스터가 사본을 들고 있지 않으면 화면에서 사라진다.
 *
 *   mode 3 성립       → 유지
 *   mode 2 해소       → 제거
 *   mode 1 single shot→ 지나간 일이다. 사본에 남기지 않는다
 */
void edge_event_put(edgelib_t *bus, const edge_event_t *e)
{
    if (e->mode == 1u) { return; }

    int slot = -1;
    for (int i = 0; i < bus->ev_n; i++) {
        if (bus->ev[i].node == e->node && bus->ev[i].channel == e->channel
            && bus->ev[i].code == e->code) {
            slot = i;
            break;
        }
    }

    if (e->mode == 2u) {                       /* 해소됐다 */
        if (slot >= 0) {
            bus->ev[slot] = bus->ev[bus->ev_n - 1];
            bus->ev_n--;
        }
        return;
    }

    if (slot >= 0) { bus->ev[slot] = *e; return; }
    if (bus->ev_n < EDGE_EVENTS_MAX) { bus->ev[bus->ev_n++] = *e; }
}

void edge_event_self(edgelib_t *bus, uint8_t node_addr, uint16_t code,
                     uint8_t type, int appeared, uint16_t qualifier)
{
    edge_event_t e;
    memset(&e, 0, sizeof e);
    /* **`node = 0` 이 라이브러리 자신의 관찰이라는 표시다** (API §6.5). 어느 링크를
       본 것인지는 `channel` 에 담는다 — 모듈이 올린 이벤트에서 channel 이 포트를
       가리키듯, 여기서는 노드를 가리킨다. */
    e.node      = 0u;
    e.channel   = node_addr;
    e.mode      = (uint8_t)(appeared ? 3u : 2u);
    e.type      = type;
    e.code      = code;
    e.qualifier = qualifier;                   /* 관측한 손실률 (%) */
    edge_event_put(bus, &e);
}

/* ── 손실 창 ─────────────────────────────────────────────────────────────── */
/*
 * 한두 번 놓치는 것은 다음 주기에 다시 보내면 되는 일이라 아무것도 올리지 않는다.
 * 그것이 이어지면 "잠깐 튄 것"이 아니라 링크가 나빠진 것이다 (API §6.5).
 */
static void loss_record(edgelib_t *bus, int idx, int bad)
{
    edge_node_rt_t *rt = &bus->rt[idx];

    if (rt->hist_fill == EDGE_LOSS_WIN) {
        rt->hist_bad -= rt->hist[rt->hist_pos];
    } else {
        rt->hist_fill++;
    }
    rt->hist[rt->hist_pos] = (uint8_t)(bad ? 1 : 0);
    rt->hist_bad += (bad ? 1 : 0);
    rt->hist_pos = (rt->hist_pos + 1) % EDGE_LOSS_WIN;

    if (rt->hist_fill < EDGE_LOSS_WIN / 4) { return; }   /* 아직 판단하기 이르다 */

    const int pct = (rt->hist_bad * 100) / rt->hist_fill;
    const uint8_t addr = bus->cfg.nodes[idx].address;

    if (pct >= 30 && !rt->err_up) {
        rt->err_up = 1;
        edge_event_self(bus, addr, EDGE_EVT_LINK_DOWN, 3u, 1, (uint16_t)pct);
    } else if (pct < 5 && rt->err_up) {
        rt->err_up = 0;
        edge_event_self(bus, addr, EDGE_EVT_LINK_DOWN, 3u, 0, (uint16_t)pct);
    }

    if (pct >= 5 && !rt->warn_up) {
        rt->warn_up = 1;
        edge_event_self(bus, addr, EDGE_EVT_CYCLE_MISS, 2u, 1, (uint16_t)pct);
    } else if (pct < 5 && rt->warn_up) {
        rt->warn_up = 0;
        edge_event_self(bus, addr, EDGE_EVT_CYCLE_MISS, 2u, 0, (uint16_t)pct);
    }
}

/* ── 한 노드의 주기 교환 ─────────────────────────────────────────────────── */
/*
 * 요청 = [CMD][OE][포트 PD_OUT…]  — 곧 이미지의 그 노드 구간 전체다
 * 응답 = [CMD][STATUS][MST][포트 PD_IN…]
 *
 * 이미지가 노드 구간을 이어 붙인 것이라, 복사가 한 번으로 끝난다.
 */
static void cyclic_one(edgelib_t *bus, int idx)
{
    const edge_node_t *n = &bus->cfg.nodes[idx];
    edge_node_rt_t *rt = &bus->rt[idx];

    uint8_t req[BP_PDU_MAX];
    const uint16_t olen = n->pd_out;
    if ((size_t)olen + 1u > sizeof req) { return; }

    req[0] = (uint8_t)CMD_CYCLIC_EXCHANGE;
    memcpy(&req[1], &bus->image_out[n->image_out_off], olen);

    uint8_t rsp[BP_PDU_MAX];
    /* **기다리는 시간은 사이클을 넘지 않는다.** 고정으로 크게 잡으면 응답 하나를
       놓쳤을 때 그동안 버스가 조용해지고, 워치독(사이클의 3배)이 물려 모듈이
       FAILSAFE 로 떨어진다. 한 번 놓치는 대가는 한 사이클이어야 한다. */
    uint32_t wait = bus->cfg.cycle_us;
    if (wait > 20000u) { wait = 20000u; }
    if (wait < 3000u)  { wait = 3000u; }

    const int got = edge_transport_xact(&bus->tr, n->address, req,
                                        (size_t)olen + 1u,
                                        rsp, sizeof rsp, wait);

    int bad = 1;
    if (got >= 2 && rsp[0] == (uint8_t)CMD_CYCLIC_EXCHANGE) {
        const uint8_t status = rsp[1];
        if ((status & ST_RESULT_MASK) == BP_ST_OK) {
            bad = 0;
            rt->online = 1u;
            rt->evt_pending = (uint8_t)((status & ST_EVT_FLAG) ? 1u : 0u);

            uint16_t ilen = n->pd_in;
            if ((size_t)ilen > (size_t)got - 2u) { ilen = (uint16_t)(got - 2); }
            memcpy(&bus->image_in[n->image_in_off], &rsp[2], ilen);
            rt->mst = (ilen > 0u) ? rsp[2] : 0u;
        } else if ((status & ST_RESULT_MASK) == BP_ST_BAD_STATE) {
            /* 상태가 틀려서 거절당하는 것은 놓친 프레임과 전혀 다른 일이다.
               워치독에 걸려 FAILSAFE 로 떨어졌다면 이 뒤로 영영 안 돌아온다 —
               조용히 손실만 세면 사용자는 주기를 의심하게 된다. */
            const EdgeState_e got_st =
                (EdgeState_e)((status & ST_STATE_MASK) >> ST_STATE_SHIFT);
            if (got_st != EDGE_STATE_RUN) {
                bus->state = got_st;
                edge_set_err(bus,
                             "node %u left RUN - now %s (watchdog? cycle too short)",
                             n->address,
                             got_st == EDGE_STATE_FAILSAFE ? "FAILSAFE" : "PREOP");
            }
        }
    }

    if (bad) {
        /* 이번 사이클 입력은 못 믿는다. PQ 를 내려 응용이 그 값을 쓰지 않게 한다 */
        rt->mst = 0u;
        if (n->pd_in > 0u) { bus->image_in[n->image_in_off] = 0u; }
    }
    loss_record(bus, idx, bad);
}

/* ── 비주기 슬롯 ─────────────────────────────────────────────────────────── */
static int fetch_events(edgelib_t *bus, int idx)
{
    const edge_node_t *n = &bus->cfg.nodes[idx];
    uint8_t req[3] = { (uint8_t)CMD_GET_EVENT, 0x00u, 0x00u };
    uint8_t rsp[BP_PDU_MAX];

    const int got = edge_transport_xact(&bus->tr, n->address, req, 3u,
                                        rsp, sizeof rsp, 20000u);
    if (got < 5 || rsp[0] != (uint8_t)CMD_GET_EVENT
        || (rsp[1] & ST_RESULT_MASK) != BP_ST_OK) {
        return 0;
    }

    const int body = got - 5;
    const int cnt = body / 12;
    for (int i = 0; i < cnt; i++) {
        const uint8_t *e = &rsp[5 + i * 12];
        edge_event_t ev;
        ev.node      = e[0];
        ev.channel   = e[1];
        ev.mode      = e[2];
        ev.type      = e[3];
        ev.code      = (uint16_t)(e[4] | (e[5] << 8));
        ev.qualifier = (uint16_t)(e[6] | (e[7] << 8));
        ev.timestamp_ms = (uint32_t)e[8] | ((uint32_t)e[9] << 8)
                        | ((uint32_t)e[10] << 16) | ((uint32_t)e[11] << 24);
        edge_event_put(bus, &ev);
    }

    /* 확인 처리 뒤의 EVT 가 "더 있음"을 말한다. 별도 비트가 없는 이유다. */
    bus->rt[idx].evt_pending =
        (uint8_t)(((rsp[1] & ST_EVT_FLAG) && cnt > 0) ? 1u : 0u);
    return 1;
}

/** ISDU 회수는 RDY 가 섰을 때만 슬롯을 쓴다 — 대기 비용을 0 으로 만드는 것이 요점. */
static int isdu_ready(const edgelib_t *bus, const edge_job_t *j)
{
    if (bus->state != EDGE_STATE_RUN) { return 1; }   /* 주기가 없으면 폴링뿐이다 */
    const int idx = edge_node_index(bus, j->node);
    if (idx < 0 || j->port == 0u || j->port > 4u) { return 1; }
    return ((bus->rt[idx].mst >> (4u + j->port - 1u)) & 1u) ? 1 : 0;
}

static void job_finish(edge_job_t *j, int result)
{
    j->result = result;
    j->done = 1;
}

/** 작업 하나를 한 걸음 진행시킨다. 끝나지 않았으면 0 을 준다. */
static int job_step(edgelib_t *bus, edge_job_t *j)
{
    uint8_t rsp[BP_PDU_MAX];

    if (j->kind == JOB_XACT) {
        const int got = edge_transport_xact(&bus->tr, j->node, j->req, j->req_len,
                                            rsp, sizeof rsp, 20000u);
        if (got < 2 || rsp[0] != j->req[0]) {
            job_finish(j, EDGE_ERR_TIMEOUT);
            return 1;
        }
        const int rc = edge_map_status(rsp[1]);
        j->rsp_len = (uint16_t)(got - 2);
        memcpy(j->rsp, &rsp[2], j->rsp_len);
        job_finish(j, rc);
        return 1;
    }

    /* ── ISDU ────────────────────────────────────────────────────────────── */
    if (j->phase == 0) {
        const int got = edge_transport_xact(&bus->tr, j->node, j->req, j->req_len,
                                            rsp, sizeof rsp, 20000u);
        if (got < 2 || rsp[0] != j->req[0]) {
            job_finish(j, EDGE_ERR_TIMEOUT);
            return 1;
        }
        if ((rsp[1] & ST_RESULT_MASK) != BP_ST_OK) {
            job_finish(j, edge_map_status(rsp[1]));
            return 1;
        }
        j->phase = 1;
        return 0;
    }

    if (edge_now_us() >= j->deadline_us) {
        job_finish(j, EDGE_ERR_TIMEOUT);
        return 1;
    }
    if (!isdu_ready(bus, j)) { return 0; }      /* 슬롯을 쓰지 않고 다음 사이클로 */

    uint8_t req[2] = { (uint8_t)CMD_IOL_ISDU_RESULT, j->port };
    const int got = edge_transport_xact(&bus->tr, j->node, req, 2u,
                                        rsp, sizeof rsp, 20000u);
    if (got < 2 || rsp[0] != (uint8_t)CMD_IOL_ISDU_RESULT) {
        /* **응답이 없는 것은 실패가 아니다.** 회수는 몇 번을 물어도 같은 답이
           나오는 질문이라, 놓쳤으면 다시 물으면 된다. 끝을 정하는 것은 deadline 이다. */
        return 0;
    }

    const uint8_t rc = (uint8_t)(rsp[1] & ST_RESULT_MASK);
    if (rc == BP_ST_PENDING) { return 0; }

    const int ab = got - 2;
    if (rc == BP_ST_OK) {
        if (ab >= 2 && rsp[2] == 0xFFu && rsp[3] == 0xF0u) {
            j->rsp_len = 0u;                    /* VoidBlock — 쓰기 완료 */
        } else if (ab > 5) {
            j->rsp_len = (uint16_t)(ab - 5);
            memcpy(j->rsp, &rsp[2 + 5], j->rsp_len);
        } else {
            j->rsp_len = 0u;
        }
        job_finish(j, EDGE_OK);
        return 1;
    }

    if (ab >= 4 && rsp[2] == 0xFFu && rsp[3] == 0xFFu) {
        j->rsp[0] = rsp[4];                     /* ErrorType 2 바이트 */
        j->rsp[1] = rsp[5];
        j->rsp_len = 2u;
        job_finish(j, EDGE_ERR_DEVICE);
        return 1;
    }
    job_finish(j, edge_map_status(rsp[1]));
    return 1;
}

/** 사이클당 하나. 이벤트가 먼저다. 잠금을 쥔 채로 부른다. */
static void async_slot(edgelib_t *bus)
{
    for (int i = 0; i < bus->cfg.node_count; i++) {
        if (bus->rt[i].evt_pending) {
            if (fetch_events(bus, i)) { return; }
            bus->rt[i].evt_pending = 0u;
        }
    }

    if (bus->q_n == 0) { return; }

    edge_job_t *j = bus->q[bus->q_head];
    if (job_step(bus, j)) {
        bus->q_head = (bus->q_head + 1) % EDGE_JOBQ_MAX;
        bus->q_n--;
        pthread_cond_broadcast(&bus->cv);
    }
}

/* ── 스레드 ──────────────────────────────────────────────────────────────── */
static void *cycle_thread(void *arg)
{
    edgelib_t *bus = (edgelib_t *)arg;

    for (;;) {
        pthread_mutex_lock(&bus->lock);
        if (bus->stop) { pthread_mutex_unlock(&bus->lock); break; }

        const uint64_t t0 = edge_now_us();
        const int running = (bus->state == EDGE_STATE_RUN);

        if (running) {
            for (int i = 0; i < bus->cfg.node_count; i++) {
                cyclic_one(bus, i);
                if (bus->state != EDGE_STATE_RUN) { break; }
            }
        }
        async_slot(bus);

        const uint32_t cyc = running ? bus->cfg.cycle_us : 2000u;
        pthread_mutex_unlock(&bus->lock);

        const uint64_t spent = edge_now_us() - t0;
        if (spent < cyc) { edge_sleep_us((uint32_t)(cyc - spent)); }
    }
    return NULL;
}

void edge_thread_start(edgelib_t *bus)
{
    if (bus->thread_up) { return; }
    bus->stop = 0;
    if (pthread_create(&bus->thread, NULL, cycle_thread, bus) == 0) {
        bus->thread_up = 1;
    }
}

void edge_thread_stop(edgelib_t *bus)
{
    if (!bus->thread_up) { return; }
    pthread_mutex_lock(&bus->lock);
    bus->stop = 1;
    pthread_cond_broadcast(&bus->cv);
    pthread_mutex_unlock(&bus->lock);
    pthread_join(bus->thread, NULL);
    bus->thread_up = 0;
}

/* ── 작업 제출 ───────────────────────────────────────────────────────────── */
int edge_job_run(edgelib_t *bus, edge_job_t *job, double timeout_s)
{
    if (timeout_s <= 0.0) { timeout_s = 6.0; }

    job->done = 0;
    job->result = EDGE_ERR_UNDEFINED;
    job->rsp_len = 0u;
    job->phase = 0;
    job->deadline_us = edge_now_us() + (uint64_t)(timeout_s * 1e6);

    pthread_mutex_lock(&bus->lock);
    if (bus->q_n >= EDGE_JOBQ_MAX) {
        pthread_mutex_unlock(&bus->lock);
        edge_set_err(bus, "async queue is full (%d)", EDGE_JOBQ_MAX);
        return EDGE_ERR_BUSY;
    }
    bus->q[(bus->q_head + bus->q_n) % EDGE_JOBQ_MAX] = job;
    bus->q_n++;

    /* 스레드가 안 돌고 있으면(닫히는 중) 여기서 직접 처리한다 — 안 그러면 영원히 기다린다 */
    if (!bus->thread_up) {
        while (!job->done && edge_now_us() < job->deadline_us) {
            async_slot(bus);
        }
        if (!job->done) {
            bus->q_head = (bus->q_head + 1) % EDGE_JOBQ_MAX;
            bus->q_n--;
            job->result = EDGE_ERR_TIMEOUT;
        }
        pthread_mutex_unlock(&bus->lock);
        return job->result;
    }

    while (!job->done) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        ts.tv_nsec += 20 * 1000 * 1000;             /* 20 ms 마다 깨어 확인 */
        if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
        pthread_cond_timedwait(&bus->cv, &bus->lock, &ts);

        if (!job->done && edge_now_us() > job->deadline_us + 500000ull) {
            /* 큐에서 걷어낸다 — 스레드가 죽었거나 슬롯이 오지 않는 상황 */
            for (int k = 0; k < bus->q_n; k++) {
                const int at = (bus->q_head + k) % EDGE_JOBQ_MAX;
                if (bus->q[at] == job) {
                    for (int m = k; m < bus->q_n - 1; m++) {
                        bus->q[(bus->q_head + m) % EDGE_JOBQ_MAX] =
                            bus->q[(bus->q_head + m + 1) % EDGE_JOBQ_MAX];
                    }
                    bus->q_n--;
                    break;
                }
            }
            job->result = EDGE_ERR_TIMEOUT;
            job->done = 1;
        }
    }
    pthread_mutex_unlock(&bus->lock);
    return job->result;
}
