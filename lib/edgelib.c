/*
 * edgelib — 공개 API
 *
 * Copyright (c) 2026 RTES Co., Ltd. All rights reserved.
 *
 * 이 파일은 API 문서 §6 을 그대로 구현한다. 버스를 실제로 타는 일은 cycle.c 의
 * 비주기 슬롯에 맡긴다 — **모든 요청이 그 자리를 지난다.** 사용자 스레드가 직접
 * 프레임을 내면 주기 교환과 겹쳐 둘 다 깨지기 때문이다.
 */

#ifndef _GNU_SOURCE
#define _GNU_SOURCE
#endif

#include "internal.h"

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* open() 이 실패해 핸들이 없을 때의 이유를 담는다 */
static char g_open_err[192];

/* ── 잡동사니 ────────────────────────────────────────────────────────────── */
void edge_set_err(edgelib_t *bus, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    if (bus != NULL) {
        vsnprintf(bus->last_err, sizeof bus->last_err, fmt, ap);
    } else {
        vsnprintf(g_open_err, sizeof g_open_err, fmt, ap);
    }
    va_end(ap);
}

int edge_map_status(uint8_t status)
{
    switch (status & ST_RESULT_MASK) {
    case BP_ST_OK:          return EDGE_OK;
    case BP_ST_PENDING:     return EDGE_OK;
    case BP_ST_BAD_PARAM:   return EDGE_ERR_BAD_PARAM;
    case BP_ST_BAD_STATE:   return EDGE_ERR_BAD_STATE;
    case BP_ST_BAD_CHANNEL: return EDGE_ERR_BAD_CHANNEL;
    case BP_ST_BUSY:        return EDGE_ERR_BUSY;
    case BP_ST_UNSUPPORTED: return EDGE_ERR_UNSUPPORTED;
    case BP_ST_TIMEOUT:     return EDGE_ERR_TIMEOUT;
    case BP_ST_DEVICE_ERR:  return EDGE_ERR_DEVICE;
    default:                return EDGE_ERR_UNDEFINED;
    }
}

int edge_node_index(const edgelib_t *bus, uint8_t node)
{
    for (int i = 0; i < bus->cfg.node_count; i++) {
        if (bus->cfg.nodes[i].address == node) { return i; }
    }
    return -1;
}

const char *edgelib_error_msg(int result)
{
    switch (result) {
    case EDGE_OK:               return "OK";
    case EDGE_ERR_BAD_PARAM:    return "bad parameter";
    case EDGE_ERR_BAD_STATE:    return "not allowed in this state";
    case EDGE_ERR_BAD_CHANNEL:  return "no such node or port";
    case EDGE_ERR_BUSY:         return "busy - an ISDU is in progress";
    case EDGE_ERR_UNSUPPORTED:  return "unsupported";
    case EDGE_ERR_TIMEOUT:      return "no response";
    case EDGE_ERR_DEVICE:       return "device rejected the request";
    case EDGE_ERR_COMMS:        return "communication error";
    case EDGE_ERR_NO_MEM:       return "out of memory";
    default:                    return "undefined error";
    }
}

const char *edgelib_last_error(edgelib_t *bus)
{
    return (bus != NULL) ? bus->last_err : g_open_err;
}

/* ── 잠금을 쥔 채 쓰는 저수준 요청 ───────────────────────────────────────── */
/*
 * STARTUP·PREOP 에는 주기 교환도 워치독도 없다. 그래서 상태 전이와 탐색은 스레드를
 * 거치지 않고 여기서 곧바로 낸다 — 큐를 지나야 할 이유가 없고, 지나면 오히려
 * 순서를 보장하기 어렵다.
 */
static int direct(edgelib_t *bus, uint8_t addr, const uint8_t *pdu, size_t len,
                  uint8_t *rsp, size_t cap, uint32_t timeout_us, int retries)
{
    for (int i = 0; i <= retries; i++) {
        const int got = edge_transport_xact(&bus->tr, addr, pdu, len,
                                            rsp, cap, timeout_us);
        if (got >= 2 && rsp[0] == pdu[0]) { return got; }
    }
    return 0;
}

static void broadcast_state_us(edgelib_t *bus, uint8_t st, uint32_t settle_us)
{
    const uint8_t pdu[2] = { (uint8_t)CMD_SET_STATE, st };
    edge_transport_broadcast(&bus->tr, pdu, 2u);
    edge_sleep_us(settle_us);
}

static void broadcast_state(edgelib_t *bus, uint8_t st)
{
    /* STARTUP·PREOP 에는 워치독이 없어 넉넉히 기다려도 된다. RUN 만은 다르다 —
       거기서는 이 대기 자체가 워치독을 넘길 수 있다 (go_run 의 주석). */
    broadcast_state_us(bus, st, 20000u);
}

/** GET_UID 로 지금 상태를 묻는다. 못 물으면 -1. */
static int probe_state(edgelib_t *bus, uint8_t addr)
{
    const uint8_t pdu[1] = { (uint8_t)CMD_GET_UID };
    uint8_t rsp[BP_PDU_MAX];
    const int got = direct(bus, addr, pdu, 1u, rsp, sizeof rsp, 30000u, 2);
    if (got < 2) { return -1; }
    return (int)((rsp[1] & ST_STATE_MASK) >> ST_STATE_SHIFT);
}

/* ── 탐색 (코어 §3.5) ────────────────────────────────────────────────────── */
/*
 * **탐색은 STARTUP 에서만 일어난다.** 그 상태에는 워치독도 주기 교환도 없어 실시간
 * 제약이 없다. 주소 배정은 UID 접두 일치로 한다 — 주소가 없는 노드는 자기 주소를
 * 말할 수 없으므로 브로드캐스트로 묻고 UID 로 지목하는 것 말고는 방법이 없다.
 */
typedef struct {
    uint8_t addr;
    uint8_t uid[UID_LEN];
} found_t;

static int sweep(edgelib_t *bus, found_t *out, int max)
{
    const uint8_t pdu[1] = { (uint8_t)CMD_GET_UID };
    int n = 0;
    for (unsigned a = 1u; a <= 126u && n < max; a++) {
        uint8_t rsp[BP_PDU_MAX];
        const int got = edge_transport_xact(&bus->tr, (uint8_t)a, pdu, 1u,
                                            rsp, sizeof rsp, 2500u);
        if (got < (int)(2u + UID_LEN) || rsp[0] != (uint8_t)CMD_GET_UID) { continue; }
        out[n].addr = (uint8_t)a;
        memcpy(out[n].uid, &rsp[2], UID_LEN);
        n++;
    }
    return n;
}

static int assign_unaddressed(edgelib_t *bus, found_t *found, int n, int max)
{
    uint8_t used[128] = {0};
    for (int i = 0; i < n; i++) { used[found[i].addr] = 1u; }

    for (int guard = 0; guard < max && n < max; guard++) {
        /* prefix_len = 0 → 접두 조건 없음. 미할당 노드만 답한다 (§3.5.2) */
        const uint8_t req[2] = { (uint8_t)CMD_DISCOVER, 0u };
        uint8_t rsp[BP_PDU_MAX];
        const int got = edge_transport_xact(&bus->tr, BP_ADDR_BROADCAST, req, 2u,
                                            rsp, sizeof rsp, 100000u);
        if (got != (int)(2u + UID_LEN) || rsp[0] != (uint8_t)CMD_DISCOVER) { break; }

        unsigned next = 1u;
        while (next <= 126u && used[next]) { next++; }
        if (next > 126u) { break; }

        uint8_t set[2 + UID_LEN + 1];
        set[0] = (uint8_t)CMD_SET_ADDRESS;
        memcpy(&set[1], &rsp[2], UID_LEN);
        set[1 + UID_LEN] = (uint8_t)next;
        uint8_t ack[BP_PDU_MAX];
        const int a = edge_transport_xact(&bus->tr, BP_ADDR_BROADCAST, set,
                                          1u + UID_LEN + 1u, ack, sizeof ack, 200000u);
        if (a < 2 || (ack[1] & ST_RESULT_MASK) != BP_ST_OK) { break; }

        found[n].addr = (uint8_t)next;
        memcpy(found[n].uid, &rsp[2], UID_LEN);
        used[next] = 1u;
        n++;
    }
    return n;
}

/**
 * SIO 포트의 PD 길이를 규칙으로 채운다 (클래스 문서 §2 표).
 *
 * `PortStatusList` 의 길이는 IO-Link 의미로 **꽂힌 디바이스의** PD 길이라, 디바이스가
 * 없는 SIO 포트에서 모듈이 0 을 주는 것이 맞다. 그런데 C/Q 는 디바이스와 무관하게
 * 비트 하나를 주고받으므로 이미지에는 자리가 있어야 한다.
 *
 * **출처가 둘이 되는 것이 아니라 규칙이 하나다** — "모드를 보고, IOL 일 때만 디바이스에
 * 묻는다". 모듈의 `app_iol_pd.c` 도 같은 표를 적용해 맵을 만든다. 길이를 전선으로
 * 주고받지 않는 이유가 이것이다.
 */
static void sio_pd_len(edge_port_status_t *ps)
{
    if (ps->status == EDGE_PS_DEACTIVATED) {
        /* 안 쓰겠다고 명시한 포트에는 아무것도 싣지 않는다 */
        ps->pd_in  = 0u;
        ps->pd_out = 0u;
        return;
    }

    if (ps->status == EDGE_PS_DI_CQ) {
        ps->pd_in  = 1u;                 /* C/Q 레벨 */
        ps->pd_out = 0u;
    } else if (ps->status == EDGE_PS_DO_CQ) {
        /* **보낸 값과 실제 레벨은 다를 수 있다.** 단락·과전류로 드라이버가 접히면
         * 1 을 보내도 선은 0 이다. 되읽기 한 바이트가 그것을 알려 준다. */
        ps->pd_in  = 1u;                 /* C/Q 되읽기 */
        ps->pd_out = 1u;                 /* C/Q 구동 */
    } else {
        /* IOL_* 는 디바이스가 준 값 그대로 */
    }

    /* **DI(핀 2) 한 바이트가 뒤에 붙는다.** 살아 있는 포트면 모드와 무관하게
     * 언제나 실린다 — 모듈의 app_iol_pd.c 도 같은 규칙이다. 자리는
     * [디바이스/C-Q PD…][DI] 라, DI 를 더해도 앞쪽 오프셋이 밀리지 않는다. */
    ps->pd_in = (uint8_t)(ps->pd_in + 1u);
}

/** PortStatusList (Table E.4) 를 읽어 채운다. ArgBlock 은 rsp[2] 부터다. */
static int read_port_status(edgelib_t *bus, uint8_t addr, uint8_t port,
                            edge_port_status_t *ps)
{
    const uint8_t req[4] = { (uint8_t)CMD_IOL_PORT_STATUS, port, 0xFFu, 0xF0u };
    uint8_t rsp[BP_PDU_MAX];
    const int got = direct(bus, addr, req, 4u, rsp, sizeof rsp, 200000u, 2);
    if (got < 17 || (rsp[1] & ST_RESULT_MASK) != BP_ST_OK) { return -1; }

    const uint8_t *ab = &rsp[2];
    memset(ps, 0, sizeof *ps);
    ps->status      = ab[2];
    ps->quality     = ab[3];
    ps->revision_id = ab[4];
    ps->rate        = ab[5];
    ps->cycletime   = ab[6];
    ps->pd_in       = ab[7];
    ps->pd_out      = ab[8];
    ps->vendor_id   = (uint16_t)((ab[9] << 8) | ab[10]);
    /* DeviceID 는 octet 11 에서 시작하는 U32 — 유효한 것은 하위 3옥텟 */
    ps->device_id   = ((uint32_t)ab[12] << 16) | ((uint32_t)ab[13] << 8) | ab[14];

    /* Table B.3 — 상위 2비트가 시간 기준, 하위 6비트가 배수 */
    const unsigned mult = ps->cycletime & 0x3Fu;
    switch ((ps->cycletime >> 6) & 0x03u) {
    case 0:  ps->cycletime_us = (mult * 100u < 400u) ? 400u : mult * 100u; break;
    case 1:  ps->cycletime_us = 6400u + mult * 400u;   break;
    case 2:  ps->cycletime_us = 32000u + mult * 1600u; break;
    default: ps->cycletime_us = 400u; break;
    }
    sio_pd_len(ps);
    return 0;
}

static void recompute_offsets(edge_config_t *cfg)
{
    uint16_t in_off = 0u, out_off = 0u;
    for (int i = 0; i < cfg->node_count; i++) {
        cfg->nodes[i].image_in_off  = in_off;
        cfg->nodes[i].image_out_off = out_off;
        in_off  = (uint16_t)(in_off  + cfg->nodes[i].pd_in);
        out_off = (uint16_t)(out_off + cfg->nodes[i].pd_out);
    }
    cfg->image_in_bytes  = in_off;
    cfg->image_out_bytes = out_off;
}

/**
 * STARTUP 시퀀스 — 스캔 → (배정) → PREOP 에서 포트 확인 → PD 크기 대조.
 *
 * **설정과 다르면 여기서 멈춘다.** 라이브러리가 맞춰 주면 "설정 파일대로 돌고 있다"는
 * 믿음이 깨지고, 그 믿음이 없으면 설정 파일을 두는 뜻이 없다.
 */
static int do_startup(edgelib_t *bus)
{
    broadcast_state(bus, (uint8_t)EDGE_STATE_STARTUP);

    found_t found[EDGE_MAX_NODES];
    int n = sweep(bus, found, EDGE_MAX_NODES);
    if (bus->auto_config || n < bus->cfg.node_count) {
        n = assign_unaddressed(bus, found, n, EDGE_MAX_NODES);
    }
    if (n == 0) {
        edge_set_err(bus, "no module answered on the bus");
        return EDGE_ERR_TIMEOUT;
    }

    if (bus->auto_config) {
        bus->cfg.node_count = n;
        for (int i = 0; i < n; i++) {
            edge_node_t *nd = &bus->cfg.nodes[i];
            memset(nd, 0, sizeof *nd);
            nd->address  = found[i].addr;
            nd->category = found[i].uid[0];
            nd->model    = found[i].uid[1];
            nd->variant  = found[i].uid[2];
            nd->hw_rev   = found[i].uid[3];
            for (unsigned k = 0; k < 8u; k++) {
                snprintf(&nd->serial[k * 2u], 3u, "%02X", found[i].uid[4u + k]);
            }
            snprintf(nd->type_name, sizeof nd->type_name, "category 0x%02X",
                     nd->category);
        }
    } else {
        /* 설정에 적힌 노드가 다 있는지, UID 가 같은지 본다 */
        for (int i = 0; i < bus->cfg.node_count; i++) {
            const edge_node_t *want = &bus->cfg.nodes[i];
            int hit = -1;
            for (int k = 0; k < n; k++) {
                if (found[k].addr == want->address) { hit = k; break; }
            }
            if (hit < 0) {
                edge_set_err(bus, "node %u in the config did not answer",
                             want->address);
                return EDGE_ERR_BAD_CHANNEL;
            }
            if (found[hit].uid[0] != want->category
                || found[hit].uid[1] != want->model) {
                edge_set_err(bus,
                             "node %u is category 0x%02X model %u, "
                             "the config says 0x%02X model %u",
                             want->address, found[hit].uid[0], found[hit].uid[1],
                             want->category, want->model);
                return EDGE_ERR_BAD_STATE;
            }
        }
    }

    /* 포트는 PREOP 이라야 돈다 */
    broadcast_state(bus, (uint8_t)EDGE_STATE_PREOP);

    for (int i = 0; i < bus->cfg.node_count; i++) {
        edge_node_t *nd = &bus->cfg.nodes[i];
        if (nd->category != 0x40u) { continue; }      /* IO-Link 마스터만 */

        uint16_t sum_in = 1u, sum_out = 1u;           /* 한정자 한 바이트씩 */
        const int nports = (nd->port_count > 0) ? nd->port_count : 4;
        for (int p = 1; p <= nports && p <= EDGE_MAX_PORTS; p++) {
            edge_port_status_t ps;
            edge_port_t *pt = &nd->ports[p - 1];
            if (nd->port_count < p) { nd->port_count = (uint8_t)p; }
            pt->port = (uint8_t)p;

            if (read_port_status(bus, nd->address, (uint8_t)p, &ps) != 0) {
                continue;
            }
            if (bus->auto_config) {
                pt->pd_in  = ps.pd_in;
                pt->pd_out = ps.pd_out;
                pt->vendor_id = ps.vendor_id;
                pt->device_id = ps.device_id;
                /* **상태가 모드를 말해 준다.** SIO 를 DEACTIVATED 로 접으면
                 * 길이는 맞는데 이름이 안 붙어, 생성 코드가 C/Q 와 DI 를 정체
                 * 모를 덩어리로 낸다. */
                switch (ps.status) {
                case EDGE_PS_OPERATE:
                case 3u:                      /* PREOPERATE */
                    pt->mode = EDGE_PM_IOL_AUTOSTART; break;
                case EDGE_PS_DI_CQ: pt->mode = EDGE_PM_DI_CQ; break;
                case EDGE_PS_DO_CQ: pt->mode = EDGE_PM_DO_CQ; break;
                default:            pt->mode = EDGE_PM_DEACTIVATED; break;
                }
            } else if (pt->mode != EDGE_PM_DEACTIVATED
                       && (ps.pd_in != pt->pd_in || ps.pd_out != pt->pd_out)) {
                /* **여기서 멈추는 것이 이 함수의 존재 이유다.** 크기가 다르면
                   이미지의 그 뒤가 통째로 밀려, 다른 포트 값까지 엉뚱해진다. */
                edge_set_err(bus,
                             "node %u port %d has PD %u/%u but the config says %u/%u",
                             nd->address, p, ps.pd_in, ps.pd_out,
                             pt->pd_in, pt->pd_out);
                return EDGE_ERR_BAD_STATE;
            }
            sum_in  = (uint16_t)(sum_in  + pt->pd_in);
            sum_out = (uint16_t)(sum_out + pt->pd_out);
        }
        if (bus->auto_config) {
            nd->pd_in  = sum_in;
            nd->pd_out = sum_out;
        }
    }

    if (bus->auto_config) { recompute_offsets(&bus->cfg); }

    if (bus->cfg.image_in_bytes > EDGE_IMAGE_MAX
        || bus->cfg.image_out_bytes > EDGE_IMAGE_MAX) {
        edge_set_err(bus, "process image %u/%u B exceeds the %u the library holds",
                     bus->cfg.image_in_bytes, bus->cfg.image_out_bytes,
                     (unsigned)EDGE_IMAGE_MAX);
        return EDGE_ERR_NO_MEM;
    }

    broadcast_state(bus, (uint8_t)EDGE_STATE_STARTUP);
    bus->state = EDGE_STATE_STARTUP;
    memset(bus->image_in, 0, sizeof bus->image_in);
    return EDGE_OK;
}

/* ── 상태 전이 ───────────────────────────────────────────────────────────── */
static int go_preop(edgelib_t *bus)
{
    /* **FAILSAFE 에서 나가는 길은 STARTUP 하나뿐이다** (코어 §5 전이표). 곧바로
       PREOP 을 지시하면 BAD_STATE 로 거절당한다. */
    broadcast_state(bus, (uint8_t)EDGE_STATE_STARTUP);
    broadcast_state(bus, (uint8_t)EDGE_STATE_PREOP);

    for (int i = 0; i < bus->cfg.node_count; i++) {
        const int st = probe_state(bus, bus->cfg.nodes[i].address);
        if (st != (int)EDGE_STATE_PREOP) {
            edge_set_err(bus, "node %u did not enter PREOP (state %d)",
                         bus->cfg.nodes[i].address, st);
            return EDGE_ERR_BAD_STATE;
        }
    }
    bus->state = EDGE_STATE_PREOP;
    return EDGE_OK;
}

static int go_run(edgelib_t *bus)
{
    if (bus->cfg.cycle_min_us != 0u && bus->cfg.cycle_us < bus->cfg.cycle_min_us) {
        edge_set_err(bus,
                     "cycle %u us is below the computed minimum %u us - "
                     "frames would be missed",
                     bus->cfg.cycle_us, bus->cfg.cycle_min_us);
        return EDGE_ERR_BAD_PARAM;
    }

    if (bus->state != EDGE_STATE_PREOP) {
        const int rc = go_preop(bus);
        if (rc != EDGE_OK) { return rc; }
    }

    for (int i = 0; i < bus->cfg.node_count; i++) {
        const uint8_t addr = bus->cfg.nodes[i].address;

        /* 소거되지 않은 에러가 있으면 RUN 에 못 간다 (코어 §5.3) */
        const uint8_t uid[1] = { (uint8_t)CMD_GET_UID };
        uint8_t rsp[BP_PDU_MAX];
        if (direct(bus, addr, uid, 1u, rsp, sizeof rsp, 30000u, 2) >= 2
            && (rsp[1] & ST_ERR_FLAG)) {
            const uint8_t clr[1] = { (uint8_t)CMD_CLEAR_ERRORS };
            direct(bus, addr, clr, 1u, rsp, sizeof rsp, 200000u, 1);
        }

        /* **사이클 시간을 먼저 통보해야 한다** — 안 하면 슬레이브가 RUN 을 거부한다.
           감시 없이 출력을 든 채 올라가는 것을 막는 방어선이다 (코어 §5.2). */
        uint8_t sc[9];
        sc[0] = (uint8_t)CMD_SET_CYCLE_TIME;
        for (unsigned k = 0; k < 4u; k++) {
            sc[1u + k] = (uint8_t)(bus->cfg.cycle_us >> (8u * k));   /* 공칭 */
            sc[5u + k] = (uint8_t)(bus->cfg.cycle_us >> (8u * k));   /* 상한 */
        }
        const int got = direct(bus, addr, sc, 9u, rsp, sizeof rsp, 200000u, 1);
        if (got < 2 || (rsp[1] & ST_RESULT_MASK) != BP_ST_OK) {
            edge_set_err(bus, "node %u refused SET_CYCLE_TIME (%u us)",
                         addr, bus->cfg.cycle_us);
            return EDGE_ERR_BAD_STATE;
        }
    }

    /* **SET_STATE 는 브로드캐스트라 확인이 없다** — 프레임이 유실되면 아무도 모른다.
       그래서 확인이 필요한데, **따로 물어보면 안 된다.** 워치독은 RUN 진입과 동시에
       도는 `cycle x 3` 이고, GET_UID 왕복은 그보다 오래 걸릴 수 있다. 5 ms 주기면
       워치독이 15 ms 인데 확인 왕복만 20 ms 여서, 확인하는 행위가 FAILSAFE 를
       만들어 낸다.
   
       그래서 브로드캐스트 직후 곧바로 주기 교환을 시작하고, **확인은 그 응답의
       STATUS 로 한다** (confirm_run). 어차피 매 사이클 실려 오는 정보다. */
    broadcast_state_us(bus, (uint8_t)EDGE_STATE_RUN, 1000u);

    for (int i = 0; i < bus->cfg.node_count; i++) {
        edge_node_rt_t *rt = &bus->rt[i];
        memset(rt->hist, 0, sizeof rt->hist);
        rt->hist_pos = rt->hist_fill = rt->hist_bad = 0;
        rt->warn_up = rt->err_up = 0;
        rt->online = 0u;
    }
    bus->state = EDGE_STATE_RUN;
    return EDGE_OK;
}

/** 몇 사이클을 돌려 보고 정말 RUN 인지 본다. **잠금을 놓은 채 부른다.** */
static int confirm_run(edgelib_t *bus)
{
    uint32_t cyc;
    pthread_mutex_lock(&bus->lock);
    cyc = bus->cfg.cycle_us;
    pthread_mutex_unlock(&bus->lock);

    /* 열 사이클, 최소 30 ms. 스레드가 그동안 실제로 프레임을 낸다 */
    uint32_t wait = cyc * 10u;
    if (wait < 30000u) { wait = 30000u; }
    edge_sleep_us(wait);

    pthread_mutex_lock(&bus->lock);
    int ok = (bus->state == EDGE_STATE_RUN);
    for (int i = 0; ok && i < bus->cfg.node_count; i++) {
        const edge_node_rt_t *rt = &bus->rt[i];
        if (!rt->online || rt->hist_fill == 0 || rt->hist_bad == rt->hist_fill) {
            ok = 0;
        }
    }
    if (!ok) {
        edge_set_err(bus,
                     "the bus did not stay in RUN at %u us - the watchdog "
                     "(3 x cycle = %u us) is probably tripping",
                     cyc, cyc * 3u);
    }
    pthread_mutex_unlock(&bus->lock);
    return ok ? EDGE_OK : EDGE_ERR_BAD_STATE;
}

int edgelib_setmode(edgelib_t *bus, EdgeState_e target)
{
    if (bus == NULL) { return EDGE_ERR_BAD_PARAM; }

    pthread_mutex_lock(&bus->lock);
    int rc;
    switch (target) {
    case EDGE_STATE_STARTUP:
        bus->state = EDGE_STATE_STARTUP;      /* 스레드의 주기 교환을 먼저 세운다 */
        rc = do_startup(bus);
        break;
    case EDGE_STATE_PREOP:
        bus->state = EDGE_STATE_PREOP;
        rc = go_preop(bus);
        break;
    case EDGE_STATE_RUN:
        rc = EDGE_ERR_BAD_STATE;
        for (int attempt = 0; attempt < 3; attempt++) {
            if (attempt > 0) {
                bus->state = EDGE_STATE_PREOP;
                if (go_preop(bus) != EDGE_OK) { continue; }
            }
            rc = go_run(bus);
            if (rc != EDGE_OK) { continue; }

            pthread_mutex_unlock(&bus->lock);
            rc = confirm_run(bus);
            pthread_mutex_lock(&bus->lock);
            if (rc == EDGE_OK) { break; }
        }
        break;
    case EDGE_STATE_FAILSAFE:
    default:
        edge_set_err(bus, "FAILSAFE is entered by the module, not requested");
        rc = EDGE_ERR_BAD_PARAM;
        break;
    }
    pthread_mutex_unlock(&bus->lock);
    return rc;
}

int edgelib_getmode(edgelib_t *bus, EdgeState_e *state)
{
    if (bus == NULL || state == NULL) { return EDGE_ERR_BAD_PARAM; }
    pthread_mutex_lock(&bus->lock);
    *state = bus->state;
    pthread_mutex_unlock(&bus->lock);
    return EDGE_OK;
}

/* ── 열기 · 닫기 ─────────────────────────────────────────────────────────── */
edgelib_t *edgelib_open(const char *config_path)
{
    edgelib_t *bus = calloc(1, sizeof *bus);
    if (bus == NULL) {
        edge_set_err(NULL, "out of memory");
        return NULL;
    }

    if (config_path == NULL) {
        bus->auto_config = 1;
        edge_config_defaults(&bus->cfg);
    } else {
        char err[160] = {0};
        if (edge_config_load(&bus->cfg, config_path, err, sizeof err) != 0) {
            edge_set_err(NULL, "%s", err);
            free(bus);
            return NULL;
        }
    }

    if (edge_transport_open(&bus->tr, bus->cfg.port, bus->cfg.baud,
                            bus->cfg.chip, bus->cfg.dir_gpio) != 0) {
        edge_set_err(NULL, "%s", bus->tr.err);
        free(bus);
        return NULL;
    }

    pthread_mutex_init(&bus->lock, NULL);
    pthread_cond_init(&bus->cv, NULL);
    bus->state = EDGE_STATE_STARTUP;

    /* **스캔은 STARTUP 에서 자동으로 돈다** (API §6.3). 열자마자 node_info() 가
       뜻을 갖도록 여기서 한 번 밟는다. */
    const int rc = edgelib_setmode(bus, EDGE_STATE_STARTUP);
    if (rc != EDGE_OK) {
        edge_set_err(NULL, "%s", bus->last_err);
        edge_transport_close(&bus->tr);
        pthread_cond_destroy(&bus->cv);
        pthread_mutex_destroy(&bus->lock);
        free(bus);
        return NULL;
    }

    edge_thread_start(bus);
    return bus;
}

int edgelib_close(edgelib_t *bus)
{
    if (bus == NULL) { return EDGE_ERR_BAD_PARAM; }

    edge_thread_stop(bus);

    /* 닫으면서 상태를 내린다. 안 내리면 모듈이 워치독으로 FAILSAFE 에 떨어져,
       다음 접속에서 에러 소거부터 해야 한다 — 안전하지만 번거롭다. */
    broadcast_state(bus, (uint8_t)EDGE_STATE_STARTUP);

    edge_transport_close(&bus->tr);
    pthread_cond_destroy(&bus->cv);
    pthread_mutex_destroy(&bus->lock);
    free(bus);
    return EDGE_OK;
}

/* ── 구성 조회 ───────────────────────────────────────────────────────────── */
int edgelib_node_count(edgelib_t *bus)
{
    if (bus == NULL) { return EDGE_ERR_BAD_PARAM; }
    return bus->cfg.node_count;
}

int edgelib_node_info(edgelib_t *bus, uint8_t node, edge_node_t *out)
{
    if (bus == NULL || out == NULL) { return EDGE_ERR_BAD_PARAM; }
    pthread_mutex_lock(&bus->lock);
    const int i = edge_node_index(bus, node);
    if (i >= 0) { *out = bus->cfg.nodes[i]; }
    pthread_mutex_unlock(&bus->lock);
    return (i >= 0) ? EDGE_OK : EDGE_ERR_BAD_CHANNEL;
}

/* ── 진단 ────────────────────────────────────────────────────────────────── */
int edgelib_get_event(edgelib_t *bus, edge_event_t *buf, int max, int *count)
{
    if (bus == NULL || buf == NULL || max <= 0) { return EDGE_ERR_BAD_PARAM; }
    pthread_mutex_lock(&bus->lock);
    int n = bus->ev_n;
    if (n > max) { n = max; }
    memcpy(buf, bus->ev, (size_t)n * sizeof *buf);
    pthread_mutex_unlock(&bus->lock);
    if (count != NULL) { *count = n; }
    return EDGE_OK;
}

int edgelib_clear_event(edgelib_t *bus, uint8_t node)
{
    if (bus == NULL) { return EDGE_ERR_BAD_PARAM; }

    int rc = EDGE_OK;
    for (int i = 0; i < bus->cfg.node_count; i++) {
        const uint8_t addr = bus->cfg.nodes[i].address;
        if (node != 0u && addr != node) { continue; }

        edge_job_t j;
        memset(&j, 0, sizeof j);
        j.kind = JOB_XACT;
        j.node = addr;
        j.req[0] = (uint8_t)CMD_CLEAR_ERRORS;
        j.req_len = 1u;
        const int r = edge_job_run(bus, &j, 2.0);
        if (r != EDGE_OK) { rc = r; }
    }

    /* 라이브러리가 세운 것도 함께 정리한다 — 링크가 좋아졌으면 다시 서지 않는다 */
    pthread_mutex_lock(&bus->lock);
    int w = 0;
    for (int i = 0; i < bus->ev_n; i++) {
        if (bus->ev[i].node == 0u && (node == 0u || bus->ev[i].channel == node)) {
            continue;
        }
        bus->ev[w++] = bus->ev[i];
    }
    bus->ev_n = w;
    for (int i = 0; i < bus->cfg.node_count; i++) {
        if (node == 0u || bus->cfg.nodes[i].address == node) {
            bus->rt[i].warn_up = bus->rt[i].err_up = 0;
        }
    }
    pthread_mutex_unlock(&bus->lock);
    return rc;
}

/* ── 모듈 파라미터 ───────────────────────────────────────────────────────── */
/*
 * 코어 §6.2 의 두 커맨드를 그대로 옮긴다.
 *
 *     PARAM_READ   [CMD][index:2][channel:1]          → [value:4]
 *     PARAM_WRITE  [CMD][index:2][channel:1][value:4] → (없음)
 *
 * `channel` 을 빠뜨리면 모듈이 길이를 보고 BAD_PARAM 으로 돌려보낸다 — 여기서
 * 값을 채우지 않는 것과 아예 자리를 안 만드는 것은 다르다.
 *
 * 값이 4 바이트 고정인 것은 §6.2 가 그렇게 정해서다. 가변이면 마스터가 응답
 * 크기를 미리 알 수 없고, 그러면 §5.2 의 비주기 스케줄링이 성립하지 않는다.
 */
int edgelib_param_read(edgelib_t *bus, uint8_t node, uint8_t channel,
                       uint16_t index, uint32_t *value)
{
    if (bus == NULL || value == NULL) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.req[0] = (uint8_t)CMD_PARAM_READ;
    j.req[1] = (uint8_t)(index & 0xFFu);
    j.req[2] = (uint8_t)(index >> 8);
    j.req[3] = channel;
    j.req_len = 4u;

    const int rc = edge_job_run(bus, &j, 2.0);
    if (rc != EDGE_OK) { return rc; }

    /* 4 바이트가 아니면 상대가 §6.2 를 지키지 않은 것이다. 앞쪽만 받아 두면
     * 상위 바이트가 쓰레기인 채로 응용까지 흘러간다. */
    if (j.rsp_len != 4u) { return EDGE_ERR_COMMS; }

    *value = (uint32_t)j.rsp[0]
           | ((uint32_t)j.rsp[1] << 8)
           | ((uint32_t)j.rsp[2] << 16)
           | ((uint32_t)j.rsp[3] << 24);
    return EDGE_OK;
}

int edgelib_param_write(edgelib_t *bus, uint8_t node, uint8_t channel,
                        uint16_t index, uint32_t value)
{
    if (bus == NULL) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.req[0] = (uint8_t)CMD_PARAM_WRITE;
    j.req[1] = (uint8_t)(index & 0xFFu);
    j.req[2] = (uint8_t)(index >> 8);
    j.req[3] = channel;
    j.req[4] = (uint8_t)(value & 0xFFu);
    j.req[5] = (uint8_t)((value >> 8) & 0xFFu);
    j.req[6] = (uint8_t)((value >> 16) & 0xFFu);
    j.req[7] = (uint8_t)((value >> 24) & 0xFFu);
    j.req_len = 8u;

    return edge_job_run(bus, &j, 2.0);
}

/* ── IO-Link ─────────────────────────────────────────────────────────────── */
/*
 * PDU 는 언제나 `[CMD][Port][ArgBlockID:2][ArgBlock…]` 한 모양이다 (클래스 §1.0).
 * **ArgBlockID 만 빅엔디안**인 것이 이 대역의 유일한 예외다.
 */
int edgelib_iol_master_ident(edgelib_t *bus, uint8_t node,
                             edge_master_ident_t *out)
{
    if (bus == NULL || out == NULL) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.req[0] = (uint8_t)CMD_IOL_MASTER_IDENT;
    j.req[1] = 0u;                 /* 포트가 없는 서비스다 */
    j.req[2] = 0xFFu; j.req[3] = 0xF0u;      /* VOID */
    j.req_len = 4u;

    const int rc = edge_job_run(bus, &j, 2.0);
    if (rc != EDGE_OK) { return rc; }
    if (j.rsp_len < 12u) { return EDGE_ERR_COMMS; }

    const uint8_t *ab = j.rsp;               /* ab[0..1] = ArgBlockID 0x0001 */
    memset(out, 0, sizeof *out);
    out->vendor_id  = (uint16_t)((ab[2] << 8) | ab[3]);
    out->master_id  = ((uint32_t)ab[4] << 24) | ((uint32_t)ab[5] << 16)
                    | ((uint32_t)ab[6] << 8) | ab[7];
    out->master_type = ab[8];
    out->port_count  = ab[9];
    return EDGE_OK;
}

int edgelib_iol_port_configuration(edgelib_t *bus, uint8_t node, uint8_t port,
                                   const edge_port_cfg_t *cfg)
{
    if (bus == NULL || cfg == NULL || port == 0u) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.port = port;
    j.req[0]  = (uint8_t)CMD_IOL_PORT_CFG_WRITE;
    j.req[1]  = port;
    j.req[2]  = 0x80u; j.req[3] = 0x00u;     /* PortConfigList */
    j.req[4]  = cfg->mode;
    j.req[5]  = cfg->validation;
    j.req[6]  = cfg->iq_behavior;
    j.req[7]  = cfg->cycletime;
    j.req[8]  = (uint8_t)(cfg->vendor_id >> 8);
    j.req[9]  = (uint8_t)(cfg->vendor_id & 0xFFu);
    j.req[10] = (uint8_t)(cfg->device_id >> 24);
    j.req[11] = (uint8_t)(cfg->device_id >> 16);
    j.req[12] = (uint8_t)(cfg->device_id >> 8);
    j.req[13] = (uint8_t)(cfg->device_id & 0xFFu);
    j.req_len = 14u;                          /* [CMD][Port] + 12 B ArgBlock */

    const int rc = edge_job_run(bus, &j, 3.0);
    if (rc != EDGE_OK) { return rc; }

    /* **포트 모드를 바꾸면 그 포트의 PD 길이가 바뀐다.** 이미지 배치를 여기서
     * 다시 잡지 않으면 라이브러리는 옛 크기로 프레임을 내고, 모듈은 길이가 안 맞아
     * 통째로 거절한다 — 증상은 "RUN 에 못 머문다"로 나타나 원인과 멀다.
     *
     * 설정을 바꿨으니 다시 물어보는 것이 맞다. 우리가 보낸 값을 그대로 믿으면
     * 포트가 그 설정을 못 받았을 때 (디바이스가 없거나 검증 실패) 어긋난다. */
    pthread_mutex_lock(&bus->lock);
    const int idx = edge_node_index(bus, node);
    if (idx >= 0) {
        edge_node_t *n = &bus->cfg.nodes[idx];
        edge_port_status_t ps;

        edge_sleep_us(1200000u);              /* 포트가 다시 올라올 틈 */
        if (read_port_status(bus, node, port, &ps) == 0) {
            uint16_t sum_in = 1u, sum_out = 1u;   /* 한정자 한 바이트씩 */
            for (int k = 0; k < n->port_count; k++) {
                if (n->ports[k].port == port) {
                    n->ports[k].mode   = cfg->mode;
                    n->ports[k].pd_in  = ps.pd_in;
                    n->ports[k].pd_out = ps.pd_out;
                    n->ports[k].vendor_id = ps.vendor_id;
                    n->ports[k].device_id = ps.device_id;
                }
                sum_in  = (uint16_t)(sum_in  + n->ports[k].pd_in);
                sum_out = (uint16_t)(sum_out + n->ports[k].pd_out);
            }
            n->pd_in  = sum_in;
            n->pd_out = sum_out;
            recompute_offsets(&bus->cfg);
        }
    }
    pthread_mutex_unlock(&bus->lock);
    return EDGE_OK;
}

int edgelib_iol_readback_port_configuration(edgelib_t *bus, uint8_t node,
                                            uint8_t port, edge_port_cfg_t *out)
{
    if (bus == NULL || out == NULL || port == 0u) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_PORT_CFG_READ;
    j.req[1] = port;
    j.req[2] = 0xFFu; j.req[3] = 0xF0u;
    j.req_len = 4u;

    const int rc = edge_job_run(bus, &j, 2.0);
    if (rc != EDGE_OK) { return rc; }
    if (j.rsp_len < 12u) { return EDGE_ERR_COMMS; }

    const uint8_t *ab = j.rsp;
    memset(out, 0, sizeof *out);
    out->mode        = ab[2];
    out->validation  = ab[3];
    out->iq_behavior = ab[4];
    out->cycletime   = ab[5];
    out->vendor_id   = (uint16_t)((ab[6] << 8) | ab[7]);
    out->device_id   = ((uint32_t)ab[8] << 24) | ((uint32_t)ab[9] << 16)
                     | ((uint32_t)ab[10] << 8) | ab[11];
    return EDGE_OK;
}

int edgelib_iol_port_status(edgelib_t *bus, uint8_t node, uint8_t port,
                            edge_port_status_t *out)
{
    if (bus == NULL || out == NULL || port == 0u) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_PORT_STATUS;
    j.req[1] = port;
    j.req[2] = 0xFFu; j.req[3] = 0xF0u;
    j.req_len = 4u;

    const int rc = edge_job_run(bus, &j, 2.0);
    if (rc != EDGE_OK) { return rc; }
    if (j.rsp_len < 15u) { return EDGE_ERR_COMMS; }

    const uint8_t *ab = j.rsp;
    memset(out, 0, sizeof *out);
    out->status      = ab[2];
    out->quality     = ab[3];
    out->revision_id = ab[4];
    out->rate        = ab[5];
    out->cycletime   = ab[6];
    out->pd_in       = ab[7];
    out->pd_out      = ab[8];
    out->vendor_id   = (uint16_t)((ab[9] << 8) | ab[10]);
    out->device_id   = ((uint32_t)ab[12] << 16) | ((uint32_t)ab[13] << 8) | ab[14];

    const unsigned mult = out->cycletime & 0x3Fu;
    switch ((out->cycletime >> 6) & 0x03u) {
    case 0:  out->cycletime_us = (mult * 100u < 400u) ? 400u : mult * 100u; break;
    case 1:  out->cycletime_us = 6400u + mult * 400u;   break;
    case 2:  out->cycletime_us = 32000u + mult * 1600u; break;
    default: out->cycletime_us = 400u; break;
    }
    /* 응용이 보는 값과 이미지가 쓰는 값이 달라선 안 된다 */
    sio_pd_len(out);
    return EDGE_OK;
}

int edgelib_iol_port_power(edgelib_t *bus, uint8_t node, uint8_t port,
                           int mode, uint16_t off_ms)
{
    if (bus == NULL || port == 0u) { return EDGE_ERR_BAD_PARAM; }

    /* 규격 Table E.9 의 셋이다. 참·거짓으로 접으면 ON 을 보낼 길이 없어진다. */
    if (mode < EDGE_PWR_ONE_TIME_OFF || mode > EDGE_PWR_ON) {
        return EDGE_ERR_BAD_PARAM;
    }
    /* E.9 — 500 ms 보다 짧으면 디바이스가 전원이 내려간 줄 모른다. 여기서 막지
     * 않으면 "껐다 켰는데 아무 일도 안 일어났다"로 나온다. */
    if (mode == EDGE_PWR_ONE_TIME_OFF && off_ms < EDGE_PWR_OFF_MS_MIN) {
        return EDGE_ERR_BAD_PARAM;
    }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_PORT_POWER;
    j.req[1] = port;
    j.req[2] = 0x70u; j.req[3] = 0x03u;      /* PortPowerOffOn */
    j.req[4] = (uint8_t)mode;
    j.req[5] = (uint8_t)(off_ms >> 8);       /* 규격의 U16 은 빅엔디안이다 */
    j.req[6] = (uint8_t)(off_ms & 0xFFu);
    j.req_len = 7u;

    return edge_job_run(bus, &j, 3.0);
}

int edgelib_iol_device_read(edgelib_t *bus, uint8_t node, uint8_t port,
                            uint16_t index, uint8_t subindex,
                            uint8_t *buf, uint16_t *len, double timeout_s)
{
    if (bus == NULL || buf == NULL || len == NULL || port == 0u) {
        return EDGE_ERR_BAD_PARAM;
    }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_ISDU;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_ISDU_READ;
    j.req[1] = port;
    j.req[2] = 0x30u; j.req[3] = 0x01u;      /* OnRequestDataIndex */
    j.req[4] = (uint8_t)(index >> 8);        /* Index 는 빅엔디안 */
    j.req[5] = (uint8_t)(index & 0xFFu);
    j.req[6] = subindex;
    j.req_len = 7u;

    const int rc = edge_job_run(bus, &j, timeout_s);
    uint16_t n = j.rsp_len;
    if (n > *len) { n = *len; }
    memcpy(buf, j.rsp, n);
    *len = n;
    return rc;
}

int edgelib_iol_device_write(edgelib_t *bus, uint8_t node, uint8_t port,
                             uint16_t index, uint8_t subindex,
                             const uint8_t *buf, uint16_t len, double timeout_s)
{
    if (bus == NULL || (buf == NULL && len > 0u) || port == 0u
        || len > BP_PDU_MAX - 7u) {
        return EDGE_ERR_BAD_PARAM;
    }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_ISDU;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_ISDU_WRITE;
    j.req[1] = port;
    j.req[2] = 0x30u; j.req[3] = 0x00u;      /* OnRequestData */
    j.req[4] = (uint8_t)(index >> 8);
    j.req[5] = (uint8_t)(index & 0xFFu);
    j.req[6] = subindex;
    if (len > 0u) { memcpy(&j.req[7], buf, len); }
    j.req_len = (uint16_t)(7u + len);

    return edge_job_run(bus, &j, timeout_s);
}

int edgelib_iol_abort(edgelib_t *bus, uint8_t node, uint8_t port)
{
    if (bus == NULL || port == 0u) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_ISDU_ABORT;
    j.req[1] = port;
    j.req_len = 2u;

    return edge_job_run(bus, &j, 2.0);
}

/*
 * 핀 2 (I/Q) — 주기 맵 밖이다. ArgBlock 은 3 바이트: ID 2 B + 값 1 B.
 */
int edgelib_iol_pd_in_iq(edgelib_t *bus, uint8_t node, uint8_t port,
                         uint8_t *iq)
{
    if (bus == NULL || iq == NULL || port == 0u) { return EDGE_ERR_BAD_PARAM; }

    edge_job_t j;
    memset(&j, 0, sizeof j);
    j.kind = JOB_XACT;
    j.node = node;
    j.port = port;
    j.req[0] = (uint8_t)CMD_IOL_PD_IN_IQ;
    j.req[1] = port;
    j.req[2] = 0xFFu; j.req[3] = 0xF0u;      /* VOID */
    j.req_len = 4u;

    const int rc = edge_job_run(bus, &j, 2.0);
    if (rc != EDGE_OK) { return rc; }
    if (j.rsp_len < 3u) { return EDGE_ERR_COMMS; }
    *iq = j.rsp[2];                          /* ID 두 바이트 뒤가 값이다 */
    return EDGE_OK;
}

/* ── 프로세스 데이터 ─────────────────────────────────────────────────────── */
int edgelib_image_in(edgelib_t *bus, uint8_t *buf, uint16_t *len)
{
    if (bus == NULL || buf == NULL || len == NULL) { return EDGE_ERR_BAD_PARAM; }

    pthread_mutex_lock(&bus->lock);
    uint16_t n = bus->cfg.image_in_bytes;
    if (n > *len) { n = *len; }
    memcpy(buf, bus->image_in, n);
    pthread_mutex_unlock(&bus->lock);

    *len = n;
    return EDGE_OK;
}

int edgelib_image_out(edgelib_t *bus, const uint8_t *buf, uint16_t len)
{
    if (bus == NULL || buf == NULL) { return EDGE_ERR_BAD_PARAM; }
    if (len != bus->cfg.image_out_bytes) {
        /* **부분 갱신을 받지 않는다.** 짧은 것을 받아 앞쪽만 갈아 끼우면 뒤쪽 포트가
           언제 적 값인지 아무도 모르게 된다. 이미지는 통째로 오간다. */
        edge_set_err(bus, "output image is %u B, got %u",
                     bus->cfg.image_out_bytes, len);
        return EDGE_ERR_BAD_PARAM;
    }

    pthread_mutex_lock(&bus->lock);
    memcpy(bus->image_out, buf, len);
    pthread_mutex_unlock(&bus->lock);
    return EDGE_OK;
}

int edgelib_image_size(edgelib_t *bus, uint16_t *in_bytes, uint16_t *out_bytes)
{
    if (bus == NULL) { return EDGE_ERR_BAD_PARAM; }
    if (in_bytes  != NULL) { *in_bytes  = bus->cfg.image_in_bytes; }
    if (out_bytes != NULL) { *out_bytes = bus->cfg.image_out_bytes; }
    return EDGE_OK;
}
